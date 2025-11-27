# File Vault API

A production-ready Django-based file management system with intelligent deduplication, asynchronous processing, rate limiting, and comprehensive storage management.

## 🎯 What We Are Building

This is a **File Vault API** - a sophisticated file storage and management system that provides:

1. **Intelligent File Deduplication**: Automatically detects and eliminates duplicate files across all users using SHA-256 content hashing, saving significant storage space
2. **Asynchronous File Processing**: Handles file uploads asynchronously using Kafka message queues for better scalability and user experience
3. **Storage Quota Management**: Enforces per-user storage limits with real-time validation and tracking
4. **Rate Limiting**: Protects the API from abuse with configurable per-user request limits
5. **Advanced Search & Filtering**: Powerful query capabilities to find files by name, type, size, and date ranges
6. **Performance Optimization**: Built with caching, database indexing, and query optimization for high performance
7. **Comprehensive Monitoring**: Detailed logging, performance metrics, and health checks

## 🏗️ How We Are Building It

### Architecture Overview

The system follows a **service-oriented architecture** with clear separation of concerns:

```
┌─────────────────┐
│   API Layer     │  Django REST Framework Views
│  (views.py)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Service Layer  │  Business Logic Services
│  (services/)    │  - File Services (hashing, deduplication)
│                 │  - Kafka Services (async processing)
│                 │  - Storage Services (quota, statistics)
│                 │  - Quota Services (validation)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Data Layer     │  PostgreSQL + Redis + File Storage
│  (models.py)    │
└─────────────────┘
```

### Core Technologies

#### Backend Framework
- **Django 4.x**: Web framework providing ORM, middleware, and request handling
- **Django REST Framework**: API development with serializers, viewsets, and pagination
- **Gunicorn**: Production WSGI server for handling concurrent requests

#### Data Storage
- **PostgreSQL**: Primary database for file metadata, user stats, and rate limiting records
- **Redis**: Caching layer for performance optimization and rate limiting counters
- **Local File System**: Physical file storage with organized directory structure

#### Message Queue
- **Apache Kafka**: Asynchronous file processing queue
- **Zookeeper**: Kafka coordination service

#### Infrastructure
- **Docker & Docker Compose**: Containerization and orchestration
- **WhiteNoise**: Static file serving in production

### Key Implementation Details

#### 1. File Deduplication System

**How it works:**
- When a file is uploaded, we calculate its **SHA-256 hash** by reading the file content in chunks (8KB for normal files, 64KB for large files)
- We search the database for existing files with the same hash
- If a duplicate is found:
  - We create a **reference** entry pointing to the original file
  - We increment the `reference_count` on the original file
  - **No physical file is stored** - only metadata
- If no duplicate exists:
  - We store the file physically and create an original file record
- When deleting:
  - If it's a reference, we decrement the original's reference count
  - If it's an original with references, we only decrement the count
  - Physical files are only deleted when reference count reaches 0

**Benefits:**
- Cross-user deduplication saves storage across the entire system
- Reference counting ensures files aren't deleted while still referenced
- Database-level locking prevents race conditions

#### 2. Asynchronous File Processing

**How it works:**
- User uploads a file via POST `/api/files/`
- API immediately validates quota and creates an `UploadJob` record
- File content is base64-encoded and sent to **Kafka** topic
- API returns immediately with job ID (HTTP 202 Accepted)
- **Kafka Consumer** (separate process) picks up the message:
  - Decodes file content
  - Calculates SHA-256 hash
  - Checks for duplicates
  - Stores file or creates reference
  - Updates job status
- User can poll `/api/files/upload-status/<job_id>/` to check progress

**Benefits:**
- Non-blocking uploads - users don't wait for processing
- Scalable - multiple consumers can process in parallel
- Resilient - Kafka handles message persistence and retries
- Better user experience - instant response

#### 3. Storage Quota Management

**How it works:**
- Each user has a configurable storage limit (default: 100MB)
- **Fast Mode**: Uses Redis cache for ultra-fast quota checks (no database queries)
- **Standard Mode**: Queries database for accurate current usage
- Before upload, quota is validated:
  - Current usage + new file size ≤ quota limit
- Storage statistics track:
  - Total storage used (after deduplication)
  - Original storage (without deduplication)
  - Storage savings percentage
- Quota is enforced at upload time and tracked incrementally

**Implementation:**
- `QuotaService.validate_quota()` - Fast Redis-based validation
- `StorageService.get_user_storage_usage()` - Current usage calculation
- `StatisticsService.get_storage_stats()` - Comprehensive statistics

#### 4. Rate Limiting

**How it works:**
- `RateLimitMiddleware` intercepts all API requests
- Uses Redis to track request counts per user per time window
- Default: 60 requests per minute per user
- Time-window based: 60-second sliding windows
- Returns HTTP 429 (Too Many Requests) when limit exceeded
- Includes `Retry-After` header with seconds until next window

**Implementation:**
- Cache key: `rate_limit:{user_id}:{window_start}`
- Atomic increment operations in Redis
- Automatic expiration after window ends

#### 5. Search & Filtering

**How it works:**
- Database indexes on commonly queried fields:
  - `user_id + uploaded_at`
  - `user_id + file_type`
  - `user_id + size`
  - `user_id + original_filename`
- Query optimization using `select_related()` and `only()` to minimize data transfer
- Supports filtering by:
  - **Search**: Filename contains text (case-insensitive)
  - **File Type**: Exact MIME type match
  - **Size Range**: `min_size` and `max_size` parameters
  - **Date Range**: `start_date` and `end_date` ISO format
- Pagination: 20 items per page (configurable up to 100)

**Query Example:**
```
GET /api/files/?search=report&file_type=application/pdf&min_size=1000&max_size=5000000
```

#### 6. Performance Optimizations

**Caching Strategy:**
- File list queries cached with versioned keys based on file count
- Storage statistics cached for 5 minutes
- Storage usage cached in Redis
- Cache invalidation on file upload/delete operations

**Database Optimizations:**
- Comprehensive database indexes on all searchable fields
- Composite indexes for common query patterns
- `select_related()` to prevent N+1 queries
- `only()` to limit fields loaded from database

**Query Optimization:**
- `QueryOptimizer` service for efficient filtering
- Concurrent query optimization
- Smart field selection to minimize data transfer

**Memory Management:**
- Chunked file reading for large files
- Lazy evaluation for small files
- Cache compression utilities (available but not active)

#### 7. Security & Validation

**Middleware Stack:**
1. **ApiValidationMiddleware**: Validates UserId header, adds CORS headers
2. **RateLimitMiddleware**: Enforces rate limits
3. **SecurityMiddleware**: Validates file sizes, checks for suspicious patterns
4. **PerformanceMiddleware**: Tracks request processing time

**Security Features:**
- User-scoped access control (users can only access their own files)
- File size validation (configurable max size)
- Suspicious pattern detection in URLs
- Comprehensive security event logging

#### 8. Monitoring & Logging

**Structured Logging:**
- Request logging with user context
- File operation logging (upload, delete, access)
- Performance metrics (operation duration, cache hits/misses)
- Security event logging (unauthorized access, quota exceeded)
- Error logging with full context

**Performance Metrics:**
- Operation duration tracking
- Cache hit/miss rates
- Database query performance
- File processing times

**Health Checks:**
- `/health/` endpoint for container health checks
- Database connectivity checks
- Service availability monitoring

## 📋 Prerequisites

- **Docker** (20.10.x or higher) and **Docker Compose** (2.x or higher)
- **Python 3.9+** (for local development without Docker)

## 🚀 Quick Start

### Using Docker (Recommended)

1. **Start all services:**
```bash
docker-compose up --build
```

2. **Run database migrations:**
   ```bash
   docker-compose exec backend python manage.py migrate
   ```

3. **Start Kafka consumer** (in a separate terminal):
   ```bash
   docker-compose exec backend python manage.py run_kafka_consumer
   ```

4. **Access the API:**
   - API Base URL: `http://localhost:8000/api`
   - Health Check: `http://localhost:8000/health/`

### Services Started

- **Backend API**: Port 8000
- **PostgreSQL**: Port 5432
- **Redis**: Port 6379
- **Kafka**: Port 9092
- **Zookeeper**: Port 2181

## 📝 API Documentation

### Authentication

All API endpoints require a `UserId` HTTP header:
```
UserId: user_123
```

### Core Endpoints

#### 1. Upload File (Async)
```http
POST /api/files/
Headers: UserId: <user_id>
Content-Type: multipart/form-data
Body: file=<file_data>
```

**Response (202 Accepted):**
```json
{
  "job_id": "uuid",
  "status": "queued",
  "message": "File queued for processing",
  "estimated_completion_time": "2-5 minutes",
  "quota_info": {...},
  "status_url": "/api/files/upload-status/<job_id>/"
}
```

#### 2. Check Upload Status
```http
GET /api/files/upload-status/<job_id>/
Headers: UserId: <user_id>
```

**Response:**
```json
{
  "job_id": "uuid",
  "status": "completed|processing|failed|queued",
  "filename": "example.txt",
  "file_size": 1024,
  "created_at": "2024-01-01T12:00:00Z",
  "completed_at": "2024-01-01T12:01:00Z",
  "file_id": "uuid",  // if completed
  "is_duplicate": false,
  "error_message": null  // if failed
}
```

#### 3. List Files
```http
GET /api/files/
Headers: UserId: <user_id>
Query Parameters:
  - search: Filename search (case-insensitive)
  - file_type: MIME type filter
  - min_size: Minimum file size in bytes
  - max_size: Maximum file size in bytes
  - start_date: ISO datetime (e.g., 2024-01-01T00:00:00Z)
  - end_date: ISO datetime
  - page: Page number (default: 1)
  - page_size: Items per page (default: 20, max: 100)
```

**Response:**
```json
{
  "count": 100,
  "next": "http://localhost:8000/api/files/?page=2",
  "previous": null,
  "results": [
    {
      "id": "uuid",
      "original_filename": "example.txt",
      "file_type": "text/plain",
      "size": 1024,
      "uploaded_at": "2024-01-01T12:00:00Z",
      "is_reference": false,
      "reference_count": 1
    }
  ],
  "performance": {
    "total_count": 100,
    "page_size": 20,
    "total_pages": 5
  }
}
```

#### 4. Get File Details
```http
GET /api/files/<file_id>/
Headers: UserId: <user_id>
```

#### 5. Delete File
```http
DELETE /api/files/<file_id>/
Headers: UserId: <user_id>
```

**Response:** 204 No Content

#### 6. Storage Statistics
```http
GET /api/files/storage_stats/
Headers: UserId: <user_id>
```

**Response:**
```json
{
  "user_id": "user_123",
  "total_storage_used": 52428800,
  "total_storage_used_mb": 50.0,
  "storage_savings": 10485760,
  "storage_savings_mb": 10.0,
  "savings_percent": 20.0,
  "original_files": 8,
  "reference_files": 2,
  "file_count": 10
}
```

## 🧪 Testing

Run the comprehensive test suite:

```bash
# Using Docker
docker-compose exec backend python manage.py test

# Expected: All tests passing ✅
```

Test coverage includes:
- File hashing and deduplication
- Storage quota validation
- Rate limiting
- Kafka producer/consumer flow
- Storage statistics
- Performance optimizations

## 📦 Project Structure

```
backend/
├── core/                      # Django project configuration
│   ├── settings/              # Environment-specific settings
│   │   ├── base.py           # Base settings
│   │   ├── development.py    # Development settings
│   │   ├── production.py     # Production settings
│   │   └── testing.py        # Test settings
│   ├── logging_config.py     # Structured logging configuration
│   └── monitoring.py         # Performance monitoring
│
├── files/                     # Main application
│   ├── models.py             # Database models (File, UserStorageStats, RateLimitRecord, UploadJob)
│   ├── views.py              # API viewsets with business logic
│   ├── serializers.py        # Data serialization
│   ├── urls.py               # URL routing
│   ├── middleware.py         # Custom middleware (validation, rate limiting, security)
│   │
│   ├── services/             # Business logic services
│   │   ├── file_services.py  # File hashing & deduplication
│   │   ├── kafka_service.py # Kafka producer
│   │   ├── kafka_consumer.py # Kafka consumer (async processing)
│   │   ├── storage_services.py # Storage quota & statistics
│   │   ├── quota_service.py  # Quota validation
│   │   ├── performance_services.py # Query optimization
│   │   └── memory_optimizer.py # Cache compression
│   │
│   ├── utils/                # Utility functions
│   │   ├── file_utils.py     # File path generation
│   │   ├── hash_utils.py     # Hashing utilities
│   │   ├── cache_utils.py    # Cache management
│   │   ├── validation_utils.py # Input validation
│   │   └── performance_utils.py # Performance utilities
│   │
│   ├── tests/                # Comprehensive test suite
│   │   ├── test_file_services.py
│   │   ├── test_kafka_service.py
│   │   ├── test_kafka_consumer_flow.py
│   │   ├── test_quota_service.py
│   │   ├── test_storage_services.py
│   │   └── test_performance_services.py
│   │
│   └── management/           # Django management commands
│       └── commands/
│           └── run_kafka_consumer.py # Kafka consumer command
│
├── requirements/             # Environment-specific dependencies
│   ├── base.txt             # Core dependencies
│   ├── development.txt      # Development tools
│   ├── production.txt       # Production dependencies
│   └── testing.txt         # Testing dependencies
│
├── Dockerfile               # Backend container definition
├── start.sh                # Startup script
└── manage.py               # Django management script
```

## 🔧 Configuration

### Environment Variables

Key configuration options (set in `backend/venv/env/development.env`):

- `DJANGO_ENVIRONMENT`: `development|production|testing`
- `KAFKA_BOOTSTRAP_SERVERS`: Kafka broker address
- `REDIS_URL`: Redis connection URL
- `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`: PostgreSQL settings
- `USER_STORAGE_QUOTA_MB`: Per-user storage limit (default: 100MB)
- `MAX_FILE_SIZE_MB`: Maximum file upload size (default: 5MB)
- `KAFKA_FILE_UPLOAD_TOPIC`: Kafka topic for file uploads
- `KAFKA_CONSUMER_GROUP_ID`: Kafka consumer group ID

## 🚀 Production Ready Features

- ✅ Comprehensive unit and integration tests
- ✅ Performance optimizations (caching, indexing, query optimization)
- ✅ Production-grade settings and configurations
- ✅ Security best practices (validation, rate limiting, access control)
- ✅ Detailed logging and metrics
- ✅ Database indexing for efficient queries
- ✅ Health checks and monitoring
- ✅ Docker containerization
- ✅ Async processing for scalability
- ✅ Error handling and graceful degradation

## 📊 Key Metrics & Statistics

The system tracks:
- **Storage Savings**: Percentage of storage saved through deduplication
- **File Counts**: Original files vs references
- **Performance Metrics**: Request duration, cache hit rates, query performance
- **User Statistics**: Per-user storage usage and file counts
- **Global Statistics**: System-wide deduplication effectiveness

## 🔍 How Deduplication Works in Detail

1. **Upload Request**: User uploads file via API
2. **Quota Check**: Fast Redis-based validation
3. **Job Creation**: UploadJob record created with status 'queued'
4. **Kafka Message**: File content sent to Kafka topic
5. **Consumer Processing**:
   - Decode base64 file content
   - Calculate SHA-256 hash in chunks
   - Search database for existing file with same hash
   - If duplicate found:
     - Create File record with `is_reference=True`
     - Link to original via `original_file` ForeignKey
     - Increment `reference_count` on original
     - **No physical file stored**
   - If unique:
     - Store physical file to disk
     - Create File record with `is_reference=False`
     - Set `reference_count=1`
6. **Job Update**: UploadJob status updated to 'completed'
7. **Cache Invalidation**: User's file list cache cleared

## 🎯 Design Decisions

1. **Async Processing**: Files processed asynchronously to prevent API blocking and improve scalability
2. **Reference Counting**: Ensures files aren't deleted while still referenced by other users
3. **Cross-User Deduplication**: Maximum storage savings by deduplicating across all users
4. **Fast Quota Validation**: Redis-based checks for instant feedback
5. **Comprehensive Indexing**: Database indexes on all searchable fields for fast queries
6. **Service Layer**: Business logic separated from views for testability and maintainability
7. **Structured Logging**: All operations logged with context for debugging and monitoring

## 📚 Additional Resources

- See `API_TESTING_GUIDE.md` for detailed API testing examples and workflows
- Check `docker-compose.yml` for service configuration
- Review test files in `backend/files/tests/` for usage examples

---

**Built with ❤️ using Django, Kafka, PostgreSQL, and Redis**

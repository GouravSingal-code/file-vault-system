from django.shortcuts import render
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.db import transaction
from django.db.models import Q, Max
from django.core.cache import cache
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from core.logging_config import (
    log_request, log_file_operation, log_error, 
    log_security_event, log_performance_metric
)
from .models import File
from .serializers import FileSerializer, UserStorageStatsSerializer
from .services.file_services import (
    FileHashService,
    DeduplicationService
)
from .services.storage_services import (
    StorageService,
    StatisticsService
)
from .services.performance_services import performance_monitor
from .services.kafka_service import KafkaService
from .services.quota_service import QuotaService
from .services.memory_optimizer import CacheCompressionService
from .models import UploadJob
from django.utils import timezone
import time
import base64
import json


class ApiViewMixin:
    """Mixin to access user context from middleware"""
    
    def get_user_id(self, request):
        """Get user_id from request (set by middleware)"""
        return getattr(request, 'user_id', None)


class FilePagination(PageNumberPagination):
    """
    Optimized pagination for file listings with performance improvements.
    """
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
    
    def get_paginated_response(self, data):
        """
        Override to add performance metrics and optimize response.
        """
        response = super().get_paginated_response(data)
        
        # Add performance metadata
        if hasattr(self, 'queryset'):
            response.data['performance'] = {
                'total_count': self.page.paginator.count,
                'page_size': self.page_size,
                'total_pages': self.page.paginator.num_pages,
                'has_next': self.page.has_next(),
                'has_previous': self.page.has_previous()
            }
        
        return response


class FileViewSet(ApiViewMixin, viewsets.ModelViewSet):
    """
    ViewSet for File operations with deduplication, search, and quota management.
    
    Features:
    - File upload with automatic deduplication (Feature 1)
    - Search and filtering (Feature 2)
    - User-scoped operations (Feature 3)
    - Storage quota enforcement (Feature 3)
    """
    serializer_class = FileSerializer
    pagination_class = FilePagination
    
    def get_queryset(self):
        """
        Get files for the current user with optimized queries, filtering, and smart caching.
        Feature 2: Search & Filtering with performance optimizations and caching.
        """
        user_id = self.get_user_id(self.request)
        if not user_id:
            return File.objects.none()
        
        # Create smart cache key using user's file count (more efficient than Max query)
        file_count = cache.get(f"user_file_count:{user_id}", 0)
        query_params = str(sorted(self.request.GET.items()))
        cache_key = f"user_files:{user_id}:{query_params}:v{file_count}"
        
        # QuerySet objects can't be cached directly; cache the serialized results
        # in the list() view instead of here.
        log_performance_metric('cache_miss', {
            'user_id': user_id,
            'cache_key': cache_key,
            'operation': 'file_list'
        })
        
        # Base queryset with optimization - use only() to limit fields loaded
        # Add select_related to prevent N+1 queries for original_file relationships
        queryset = File.objects.filter(user_id=user_id).select_related('original_file').only(
            'id', 'original_filename', 'file_type', 'size', 
            'uploaded_at', 'is_reference', 'reference_count', 'original_file'
        )
        
        # Apply filters using optimized query methods
        filters = {
            'search': self.request.GET.get('search'),
            'file_type': self.request.GET.get('file_type'),
            'min_size': self.request.GET.get('min_size'),
            'max_size': self.request.GET.get('max_size'),
            'start_date': self.request.GET.get('start_date'),
            'end_date': self.request.GET.get('end_date')
        }
        
        # Use QueryOptimizer for efficient filtering and concurrency
        from .services.performance_services import QueryOptimizer
        queryset = QueryOptimizer.optimize_file_search_queryset(queryset, filters)
        queryset = QueryOptimizer.optimize_concurrent_queries(queryset)
        
        # Apply additional filters that QueryOptimizer doesn't handle
        if filters['file_type']:
            queryset = queryset.filter(file_type=filters['file_type'])
        
        if filters['min_size']:
            try:
                queryset = queryset.filter(size__gte=int(filters['min_size']))
            except ValueError:
                pass
        
        if filters['max_size']:
            try:
                queryset = queryset.filter(size__lte=int(filters['max_size']))
            except ValueError:
                pass
        
        if filters['start_date']:
            parsed_date = parse_datetime(filters['start_date'])
            if parsed_date:
                queryset = queryset.filter(uploaded_at__gte=parsed_date)
        
        if filters['end_date']:
            parsed_date = parse_datetime(filters['end_date'])
            if parsed_date:
                queryset = queryset.filter(uploaded_at__lte=parsed_date)
        
        final_queryset = queryset.order_by('-uploaded_at')
        
        return final_queryset

    def _invalidate_user_cache(self, user_id):
        """Invalidate all cached data for a user using simple key deletion"""
        try:
            from .utils.cache_utils import CacheUtils
            
            # Simple cache invalidation - delete known cache keys
            cache_keys_to_delete = [
                f"user_files:{user_id}:*",      # All file list queries
                f"storage_stats:{user_id}",       # Storage statistics  
                f"file_types:{user_id}",          # File type lists
                f"search_results:{user_id}:*",   # Search results
            ]
            
            # Update file count cache for new cache key strategy
            try:
                current_count = File.objects.filter(user_id=user_id).count()
                cache.set(f"user_file_count:{user_id}", current_count, timeout=3600)  # 1 hour
            except Exception as e:
                log_error(e, {'operation': 'file_count_cache_update', 'user_id': user_id})
            
            # Actually perform cache invalidation using CacheUtils
            try:
                # Invalidate file listing caches
                CacheUtils.invalidate_user_cache(user_id, "files")
                
                # Invalidate storage statistics cache
                CacheUtils.invalidate_user_cache(user_id, "storage_stats")
                
                # Invalidate file types cache
                cache.delete(f"file_types:{user_id}")
                
                # Invalidate search results cache
                CacheUtils.invalidate_user_cache(user_id, "search_results")
                
                log_performance_metric('cache_invalidation', {
                    'user_id': user_id,
                    'keys_deleted': cache_keys_to_delete,
                    'operation': 'user_cache_clear',
                    'status': 'success'
                })
                
            except Exception as cache_error:
                log_error(cache_error, {
                    'user_id': user_id,
                    'operation': 'cache_invalidation',
                    'keys_attempted': cache_keys_to_delete
                })
            
        except Exception as e:
            log_error('cache_invalidation_error', {
                'user_id': user_id,
                'error': str(e),
                'operation': 'user_cache_clear'
            })

    def _invalidate_partial_cache(self, user_id, operation_type):
        """Invalidate only relevant cache entries based on operation type"""
        try:
            from .utils.cache_utils import CacheUtils
            
            if operation_type == 'upload':
                # Only invalidate file lists, not storage stats
                CacheUtils.invalidate_user_cache(user_id, "files")
                log_performance_metric('partial_cache_invalidation', {
                    'user_id': user_id,
                    'operation_type': operation_type,
                    'keys_invalidated': f"user_files:{user_id}:*",
                    'operation': 'partial_cache_clear'
                })
            elif operation_type == 'delete':
                # Invalidate both file lists and storage stats
                CacheUtils.invalidate_user_cache(user_id, "files")
                CacheUtils.invalidate_user_cache(user_id, "storage_stats")
                log_performance_metric('partial_cache_invalidation', {
                    'user_id': user_id,
                    'operation_type': operation_type,
                    'keys_invalidated': [f"user_files:{user_id}:*", f"storage_stats:{user_id}"],
                    'operation': 'partial_cache_clear'
                })
            elif operation_type == 'update':
                # Invalidate file lists and search results
                CacheUtils.invalidate_user_cache(user_id, "files")
                CacheUtils.invalidate_user_cache(user_id, "search_results")
                log_performance_metric('partial_cache_invalidation', {
                    'user_id': user_id,
                    'operation_type': operation_type,
                    'keys_invalidated': [f"user_files:{user_id}:*", f"search_results:{user_id}:*"],
                    'operation': 'partial_cache_clear'
                })
                    
        except Exception as e:
            log_error('partial_cache_invalidation_error', {
                'user_id': user_id,
                'operation_type': operation_type,
                'error': str(e),
                'operation': 'partial_cache_clear'
            })

    @performance_monitor('file_list_api')
    def list(self, request, *args, **kwargs):
        """
        List files with filtering (returns paginated response).
        Feature 2: Search & Filtering
        """
        start_time = time.time()
        user_id = self.get_user_id(request)
        
        try:
            # Log the request
            log_request(request)
            
            queryset = self.get_queryset()
            page = self.paginate_queryset(queryset)
            
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                response = self.get_paginated_response(serializer.data)
            else:
                serializer = self.get_serializer(queryset, many=True)
                response = Response(serializer.data)
            
            # Log performance metric
            duration = (time.time() - start_time) * 1000  # Convert to milliseconds
            log_performance_metric('list_files', duration, 'ms', {
                'user_id': user_id,
                'file_count': len(serializer.data),
                'filters_applied': {
                    'search': request.query_params.get('search'),
                    'file_type': request.query_params.get('file_type'),
                    'min_size': request.query_params.get('min_size'),
                    'max_size': request.query_params.get('max_size'),
                    'start_date': request.query_params.get('start_date'),
                    'end_date': request.query_params.get('end_date'),
                }
            })
            
            return response
            
        except Exception as e:
            log_error(e, {
                'user_id': user_id,
                'operation': 'list_files',
                'query_params': dict(request.query_params)
            })
            raise

    def create(self, request, *args, **kwargs):
        """
        Async file upload endpoint - the only upload method available.
        Validates quota, creates job, and sends to Kafka for processing.
        """
        return self.upload_async(request)

    def retrieve(self, request, *args, **kwargs):
        """
        Get details of a specific file.
        Feature 3: User-scoped access
        """
        start_time = time.time()
        user_id = self.get_user_id(request)
        
        try:
            # Log the request
            log_request(request)
            
            # Get the file with proper error handling
            try:
                instance = self.get_object()
            except File.DoesNotExist:
                return Response(
                    {'error': 'Not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Ensure user can only access their own files
            if instance.user_id != user_id:
                log_security_event('unauthorized_access', user_id, {
                    'file_id': str(instance.id),
                    'file_owner': instance.user_id,
                    'operation': 'retrieve'
                })
                return Response(
                    {'error': 'Not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            serializer = self.get_serializer(instance)
            response = Response(serializer.data)
            
            # Log file access
            log_file_operation('file_accessed', user_id, instance.id, {
                'filename': instance.original_filename,
                'is_reference': instance.is_reference
            })
            
            # Log performance metric
            duration = (time.time() - start_time) * 1000
            log_performance_metric('file_retrieve', duration, 'ms', {
                'user_id': user_id,
                'file_id': str(instance.id)
            })
            
            return response
            
        except Exception as e:
            log_error(e, {
                'user_id': user_id,
                'operation': 'file_retrieve',
                'file_id': kwargs.get('pk')
            })
            raise

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        """
        Delete a file with reference counting.
        
        Feature 1: Reference Counting
        Feature 3: User-scoped access
        """
        start_time = time.time()
        user_id = self.get_user_id(request)
        
        try:
            # Log the request
            log_request(request)
            
            # Get the file with proper error handling
            try:
                instance = self.get_object()
            except File.DoesNotExist:
                return Response(
                    {'error': 'Not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Ensure user can only delete their own files
            if instance.user_id != user_id:
                log_security_event('unauthorized_access', user_id, {
                    'file_id': str(instance.id),
                    'file_owner': instance.user_id,
                    'operation': 'delete'
                })
                return Response(
                    {'error': 'Not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Log file deletion attempt
            log_file_operation('delete_attempt', user_id, instance.id, {
                'filename': instance.original_filename,
                'is_reference': instance.is_reference,
                'reference_count': instance.reference_count
            })
            
            # Feature 1: Handle reference counting with proper locking
            # Lock the file to prevent race conditions during deletion
            locked_instance = File.objects.select_for_update().get(id=instance.id)
            
            if locked_instance.is_reference:
                # This is a reference - decrement original's reference count
                original_file_id = locked_instance.original_file.id if locked_instance.original_file else None
                
                if locked_instance.original_file:
                    # Lock the original file to prevent concurrent modifications
                    original = File.objects.select_for_update().get(id=locked_instance.original_file.id)
                    original.reference_count -= 1
                    
                    if original.reference_count <= 0:
                        # No more references, delete the original file too
                        log_file_operation('original_file_deleted', user_id, original_file_id, {
                            'reason': 'no_more_references',
                            'filename': original.original_filename
                        })
                        original.delete()
                    else:
                        original.save()
                
                # Delete this reference
                locked_instance.delete()
                
                log_file_operation('reference_deleted', user_id, instance.id, {
                    'original_file_id': original_file_id,
                    'filename': instance.original_filename
                })
                
            else:
                # This is an original file
                if locked_instance.reference_count > 1:
                    # Other references exist, just decrement count
                    locked_instance.reference_count -= 1
                    locked_instance.save()
                    
                    log_file_operation('reference_count_decremented', user_id, instance.id, {
                        'filename': instance.original_filename,
                        'new_reference_count': locked_instance.reference_count
                    })
                else:
                    # No references, safe to delete
                    log_file_operation('original_file_deleted', user_id, instance.id, {
                        'filename': instance.original_filename,
                        'reason': 'no_references'
                    })
                    locked_instance.delete()
            
            # Update storage stats incrementally
            file_size = instance.size
            StatisticsService.update_storage_stats_incremental(user_id, -file_size, -1)
            
            # Invalidate cache for file deletion
            self._invalidate_partial_cache(user_id, 'delete')
            
            # Log performance metric
            duration = (time.time() - start_time) * 1000
            log_performance_metric('file_delete', duration, 'ms', {
                'user_id': user_id,
                'file_id': str(instance.id),
                'was_reference': instance.is_reference
            })
            
            response = Response(status=status.HTTP_204_NO_CONTENT)
            
            return response
            
        except Exception as e:
            log_error(e, {
                'user_id': user_id,
                'operation': 'file_delete',
                'file_id': kwargs.get('pk')
            })
            raise

    @action(detail=False, methods=['get'], url_path='storage_stats')
    def storage_stats(self, request):
        """
        Get storage statistics for the user.
        
        Feature 1: Storage savings calculation
        """
        start_time = time.time()
        user_id = self.get_user_id(request)
        
        try:
            # Log the request
            log_request(request)
            
            stats = StatisticsService.get_storage_stats(user_id)
            # Add user_id to stats for serializer
            stats['user_id'] = user_id
            serializer = UserStorageStatsSerializer(stats)
            response = Response(serializer.data)
            
            # Log performance metric
            duration = (time.time() - start_time) * 1000
            log_performance_metric('storage_stats', duration, 'ms', {
                'user_id': user_id,
                'total_storage_used': stats['total_storage_used'],
                'storage_savings': stats['storage_savings']
            })
            
            return response
            
        except Exception as e:
            log_error(e, {
                'user_id': user_id,
                'operation': 'storage_stats'
            })
            raise

    @action(detail=False, methods=['get'], url_path='file_types')
    def file_types(self, request):
        """
        Get list of unique file types for the user.
        
        Feature 2: File type listing
        """
        user_id = self.get_user_id(request)
        file_types = File.objects.filter(
            user_id=user_id
        ).values_list('file_type', flat=True).distinct().order_by('file_type')
        
        return Response(list(file_types))

    def upload_async(self, request):
        """
        Async file upload endpoint - the only upload method available.
        Validates quota, creates job, and sends to Kafka for processing.
        
        Returns job ID immediately for status tracking.
        """
        start_time = time.time()
        user_id = self.get_user_id(request)
        
        try:
            # Get uploaded file
            file_obj = request.FILES.get('file')
            if not file_obj:
                return Response(
                    {'error': 'No file provided'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Basic validation
            max_file_size = getattr(settings, 'MAX_FILE_SIZE_MB', 5) * 1024 * 1024
            if file_obj.size > max_file_size:
                return Response(
                    {'error': f'File size exceeds maximum allowed size of {max_file_size // (1024*1024)}MB'},
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                )
            
            # Ultra-fast quota validation (Redis-only, no database locks)
            is_valid, message, quota_info = QuotaService.validate_quota(user_id, file_obj.size, fast_mode=True)
            if not is_valid:
                return Response(
                    {
                        'error': message,
                        'quota_info': quota_info
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS
                )
            
            # Create upload job
            upload_job = UploadJob.objects.create(
                user_id=user_id,
                filename=file_obj.name,
                file_size=file_obj.size,
                file_type=file_obj.content_type or 'application/octet-stream',
                status='queued'
            )
            
            # Prepare file data for Kafka
            try:
                file_obj.seek(0)
                file_content = base64.b64encode(file_obj.read()).decode('utf-8')
            except Exception as file_error:
                # Handle file reading errors
                upload_job.status = 'failed'
                upload_job.error_message = f'Failed to read file: {str(file_error)}'
                upload_job.completed_at = timezone.now()
                upload_job.save()
                
                return Response(
                    {'error': 'Failed to read file'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            file_data = {
                'filename': file_obj.name,
                'file_size': file_obj.size,
                'file_type': file_obj.content_type or 'application/octet-stream',
                'file_content': file_content
            }
            
            # Send to Kafka for async processing with hash-based partitioning
            try:
                kafka_success = KafkaService.send_upload_request(str(upload_job.id), user_id, file_data, optimized=True)
                
                if not kafka_success:
                    # If Kafka fails, mark job as failed
                    upload_job.status = 'failed'
                    upload_job.error_message = 'Failed to queue upload for processing - Kafka service unavailable'
                    upload_job.completed_at = timezone.now()
                    upload_job.save()
                    
                    log_security_event('kafka_send_failed', user_id, {
                        'job_id': str(upload_job.id),
                        'filename': file_obj.name,
                        'error': 'Kafka service returned False'
                    })
                    
                    return Response(
                        {'error': 'Upload service temporarily unavailable - Kafka service down'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE
                    )
            except Exception as kafka_error:
                # Handle Kafka connection errors gracefully
                upload_job.status = 'failed'
                upload_job.error_message = f'Kafka service unavailable: {str(kafka_error)}'
                upload_job.completed_at = timezone.now()
                upload_job.save()
                
                log_security_event('kafka_connection_error', user_id, {
                    'job_id': str(upload_job.id),
                    'filename': file_obj.name,
                    'error': str(kafka_error)
                })
                
                return Response(
                    {'error': 'Upload service temporarily unavailable - Kafka not running'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE
                )
            
            # Log performance
            duration = (time.time() - start_time) * 1000
            log_performance_metric('async_upload_queued', duration, 'ms', {
                'user_id': user_id,
                'job_id': str(upload_job.id),
                'filename': file_obj.name,
                'file_size': file_obj.size
            })
            
            # Return job info immediately
            return Response({
                'job_id': str(upload_job.id),
                'status': 'queued',
                'message': 'File queued for processing',
                'estimated_completion_time': '2-5 minutes',
                'quota_info': quota_info,
                'status_url': f'/api/files/upload-status/{upload_job.id}/'
            }, status=status.HTTP_202_ACCEPTED)
            
        except Exception as e:
            # file_obj may be unbound if the exception occurred before assignment
            _file_obj = locals().get('file_obj')
            log_error(e, {
                'user_id': user_id,
                'operation': 'async_upload',
                'filename': getattr(_file_obj, 'name', None),
                'size': getattr(_file_obj, 'size', None),
            })
            return Response(
                {'error': 'Upload failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'], url_path='upload-status/(?P<job_id>[^/.]+)')
    def upload_status(self, request, job_id=None):
        """
        Get upload job status.
        
        Args:
            pk: Upload job ID
        """
        user_id = self.get_user_id(request)
        
        try:
            upload_job = UploadJob.objects.get(id=job_id, user_id=user_id)
            
            response_data = {
                'job_id': str(upload_job.id),
                'status': upload_job.status,
                'filename': upload_job.filename,
                'file_size': upload_job.file_size,
                'created_at': upload_job.created_at.isoformat(),
                'started_at': upload_job.started_at.isoformat() if upload_job.started_at else None,
                'completed_at': upload_job.completed_at.isoformat() if upload_job.completed_at else None,
                'retry_count': upload_job.retry_count,
            }
            
            # Add result data based on status
            if upload_job.status == 'completed':
                response_data['file_id'] = str(upload_job.file_id)
                response_data['is_duplicate'] = upload_job.is_duplicate
                if upload_job.is_duplicate:
                    response_data['duplicate_file_id'] = str(upload_job.duplicate_file_id)
            elif upload_job.status == 'failed':
                response_data['error_message'] = upload_job.error_message
            
            return Response(response_data)
            
        except UploadJob.DoesNotExist:
            return Response(
                {'error': 'Upload job not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            log_error(e, {
                'user_id': user_id,
                'operation': 'upload_status',
                'job_id': job_id
            })
            return Response(
                {'error': 'Failed to get upload status'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _is_allowed_file_type(self, file_obj):
        """
        Check if the file type is allowed for security reasons.
        
        Args:
            file_obj: Django UploadedFile object
            
        Returns:
            bool: True if file type is allowed, False otherwise
        """
        # Define allowed file types for security
        allowed_extensions = {
            # Text files
            '.txt', '.md', '.csv', '.json', '.xml', '.yaml', '.yml',
            # Document files
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            # Image files
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp',
            # Archive files
            '.zip', '.rar', '.7z', '.tar', '.gz',
            # Code files
            '.py', '.js', '.html', '.css', '.java', '.cpp', '.c', '.h'
        }
        
        # Get file extension
        file_extension = self._get_file_extension(file_obj.name).lower()
        
        # Check extension
        if file_extension not in allowed_extensions:
            return False
        
        # Additional content type validation for executable files
        dangerous_content_types = {
            'application/x-executable',
            'application/x-msdownload',
            'application/x-msdos-program',
            'application/x-winexe',
            'application/x-executable-file',
            'application/octet-stream'  # Often used for executables
        }
        
        # If content type is dangerous, reject even if extension is allowed
        if file_obj.content_type in dangerous_content_types:
            return False
        
        return True
    
    def _get_file_extension(self, filename):
        """
        Extract file extension from filename.
        
        Args:
            filename: Name of the file
            
        Returns:
            str: File extension including the dot (e.g., '.txt')
        """
        if not filename:
            return ''
        
        # Find the last dot in the filename
        last_dot = filename.rfind('.')
        if last_dot == -1:
            return ''
        
        return filename[last_dot:]

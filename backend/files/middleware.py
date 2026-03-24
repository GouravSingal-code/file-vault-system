"""
Custom middleware stack for the File Vault API.

Order (applied top-to-bottom, response bottom-to-top):
  1. ApiValidationMiddleware  — validate UserId header, inject CORS headers
  2. RateLimitMiddleware      — per-user request rate limiting via Redis
  3. SecurityMiddleware       — URL sanity checks, file-size guard
  4. PerformanceMiddleware    — request timing
"""
import re
import time
import logging
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger('files')

# Only /api/ paths require the UserId header
_SUSPICIOUS_PATTERNS = re.compile(
    r'(\.\./|\.\.\\|<script|javascript:|vbscript:|data:|on\w+=|%00)',
    re.IGNORECASE,
)
# Alphanumeric + common safe separators only; rejects injection attempts
_VALID_USER_ID = re.compile(r'^[\w@.\-]+$')


class ApiValidationMiddleware:
    """
    Enforce the UserId header on all /api/ requests and add standard
    CORS / security response headers.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/api/'):
            user_id = request.META.get('HTTP_USERID', '').strip()

            if not user_id:
                return JsonResponse(
                    {'error': 'UserId header is required'},
                    status=401,
                    headers=self._cors_headers(),
                )

            if len(user_id) > 255:
                return JsonResponse(
                    {'error': 'UserId header is too long'},
                    status=400,
                    headers=self._cors_headers(),
                )

            if not _VALID_USER_ID.match(user_id):
                return JsonResponse(
                    {'error': 'UserId contains invalid characters'},
                    status=400,
                    headers=self._cors_headers(),
                )

            request.user_id = user_id

        response = self.get_response(request)
        for key, value in self._cors_headers().items():
            response[key] = value
        return response

    @staticmethod
    def _cors_headers() -> dict:
        return {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, UserId',
            'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY',
        }


class RateLimitMiddleware:
    """
    Sliding-window rate limiter backed by Redis.

    Default: 60 requests per 60-second window per user.
    Unauthenticated requests and non-API paths are not rate-limited.
    """

    REQUESTS_PER_WINDOW = getattr(settings, 'RATE_LIMIT_REQUESTS_PER_MINUTE', 60)
    WINDOW_SECONDS = 60

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith('/api/'):
            return self.get_response(request)

        user_id = getattr(request, 'user_id', None)
        if not user_id:
            return self.get_response(request)

        window_start = int(time.time() // self.WINDOW_SECONDS)
        cache_key = f'rate_limit:{user_id}:{window_start}'

        try:
            # Use add+incr pattern: add returns False if key exists (atomic on Redis)
            count = cache.get(cache_key, 0)
            if count >= self.REQUESTS_PER_WINDOW:
                retry_after = max(1, self.WINDOW_SECONDS - (int(time.time()) % self.WINDOW_SECONDS))
                logger.warning(
                    'rate_limit_exceeded',
                    extra={'event': 'security', 'user_id': user_id, 'count': count},
                )
                return JsonResponse(
                    {'error': 'Too many requests. Please slow down.'},
                    status=429,
                    headers={'Retry-After': str(retry_after)},
                )
            cache.set(cache_key, count + 1, timeout=self.WINDOW_SECONDS * 2)
        except Exception as exc:
            # Redis unavailable — degrade gracefully, don't block legitimate traffic
            logger.warning('rate_limit_cache_error', extra={'error': str(exc)})

        return self.get_response(request)


class SecurityMiddleware:
    """
    Light security checks:
    - Reject requests with suspicious patterns in the URL path / query string
    - Enforce the maximum upload size declared in settings
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        full_path = request.get_full_path()
        if _SUSPICIOUS_PATTERNS.search(full_path):
            user_id = getattr(request, 'user_id', 'anonymous')
            logger.warning(
                'suspicious_request_blocked',
                extra={'event': 'security', 'user_id': user_id, 'path': full_path},
            )
            return JsonResponse({'error': 'Bad request'}, status=400)

        if request.method == 'POST' and request.path.startswith('/api/'):
            max_bytes = getattr(settings, 'MAX_FILE_SIZE_MB', 5) * 1024 * 1024
            content_length = int(request.META.get('CONTENT_LENGTH', 0) or 0)
            if content_length > max_bytes:
                return JsonResponse(
                    {'error': f'Request body exceeds maximum size of {max_bytes // (1024*1024)} MB'},
                    status=413,
                )

        return self.get_response(request)


class PerformanceMiddleware:
    """Attach X-Request-Duration-Ms to every response for observability."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        response['X-Request-Duration-Ms'] = str(elapsed_ms)
        return response

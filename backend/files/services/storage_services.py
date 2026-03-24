"""
Storage statistics and incremental tracking services.
"""
import logging
from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum, Count, Q

from ..models import File, UserStorageStats

logger = logging.getLogger('files')

_STATS_CACHE_TTL = 300  # 5 minutes


class StorageService:
    """Low-level storage usage calculations."""

    @staticmethod
    def get_user_storage_usage(user_id: str) -> int:
        """
        Return the total bytes stored for a user (original files only;
        references don't consume additional physical space).
        """
        result = File.objects.filter(user_id=user_id, is_reference=False).aggregate(
            total=Sum('size')
        )
        return result['total'] or 0


class StatisticsService:
    """Rich storage statistics with caching."""

    @staticmethod
    def _cache_key(user_id: str) -> str:
        return f'storage_stats:{user_id}'

    @staticmethod
    def get_storage_stats(user_id: str) -> dict:
        """
        Return a statistics dict for a user.

        Values are read from cache when available; otherwise computed from
        the database and cached for 5 minutes.
        """
        cache_key = StatisticsService._cache_key(user_id)
        cached = cache.get(cache_key)
        if cached:
            return cached

        qs = File.objects.filter(user_id=user_id)
        agg = qs.aggregate(
            total_size=Sum('size'),
            original_size=Sum('size', filter=Q(is_reference=False)),
            total_count=Count('id'),
            original_count=Count('id', filter=Q(is_reference=False)),
            reference_count=Count('id', filter=Q(is_reference=True)),
        )

        # total_size = all file records (original + references)
        # original_storage_used = physical bytes on disk (originals only)
        # storage_savings = how many bytes were avoided via deduplication
        total_logical = agg['total_size'] or 0          # what users think they own
        original_storage_used = agg['original_size'] or 0  # actual bytes on disk
        storage_savings = max(0, total_logical - original_storage_used)
        savings_percent = (
            round((storage_savings / total_logical) * 100, 2)
            if total_logical > 0
            else 0.0
        )

        stats = {
            'user_id': user_id,
            'total_storage_used': original_storage_used,
            'total_storage_used_mb': round(original_storage_used / (1024 * 1024), 2),
            'original_storage_used': original_storage_used,
            'storage_savings': storage_savings,
            'storage_savings_mb': round(storage_savings / (1024 * 1024), 2),
            'savings_percent': savings_percent,
            'file_count': agg['total_count'] or 0,
            'original_files': agg['original_count'] or 0,
            'reference_files': agg['reference_count'] or 0,
        }

        cache.set(cache_key, stats, timeout=_STATS_CACHE_TTL)
        return stats

    @staticmethod
    @transaction.atomic
    def update_storage_stats_incremental(user_id: str, size_delta: int, count_delta: int) -> None:
        """
        Adjust stored statistics by a delta rather than recomputing from scratch.

        Called after upload (+size, +1) and delete (-size, -1).
        """
        stats, _ = UserStorageStats.objects.select_for_update().get_or_create(
            user_id=user_id,
            defaults={'total_storage_used': 0, 'original_storage_used': 0, 'file_count': 0},
        )
        stats.total_storage_used = max(0, stats.total_storage_used + size_delta)
        stats.original_storage_used = max(0, stats.original_storage_used + size_delta)
        stats.file_count = max(0, stats.file_count + count_delta)
        stats.save(update_fields=['total_storage_used', 'original_storage_used', 'file_count'])

        # Invalidate stats cache so the next read is fresh
        cache.delete(StatisticsService._cache_key(user_id))

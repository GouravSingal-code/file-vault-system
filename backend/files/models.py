from django.db import models
import uuid
from .utils.file_utils import generate_file_upload_path

class File(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.FileField(upload_to=generate_file_upload_path)
    original_filename = models.CharField(max_length=255, db_index=True)
    file_type = models.CharField(max_length=100, db_index=True)
    size = models.BigIntegerField(db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user_id = models.CharField(max_length=255, db_index=True)
    file_hash = models.CharField(max_length=64, db_index=True, help_text='SHA-256 hash of file content')
    is_reference = models.BooleanField(default=False, db_index=True, help_text='True if this file is a reference to another file (duplicate)')
    reference_count = models.IntegerField(default=1, help_text='Number of references pointing to this file')
    original_file = models.ForeignKey('self', blank=True, null=True, on_delete=models.CASCADE, 
                                    related_name='references', help_text='Points to the original file if this is a reference')
    
    class Meta:
        ordering = ['-uploaded_at']
        indexes = [
            # Primary search indexes for file operations
            models.Index(fields=['user_id', 'uploaded_at'], name='idx_user_uploaded'),
            models.Index(fields=['user_id', 'file_type'], name='idx_user_filetype'),
            models.Index(fields=['user_id', 'size'], name='idx_user_size'),
            models.Index(fields=['user_id', 'original_filename'], name='idx_user_filename'),
            
            # Deduplication and file hash indexes
            models.Index(fields=['file_hash', 'is_reference'], name='idx_hash_reference'),
            models.Index(fields=['file_hash'], name='idx_hash_lookup'),
            
            # Date range search indexes
            models.Index(fields=['uploaded_at'], name='idx_uploaded_date'),
            models.Index(fields=['user_id', 'uploaded_at', 'file_type'], name='idx_user_date_type'),
            
            # Size range search indexes
            models.Index(fields=['size'], name='idx_size'),
            models.Index(fields=['user_id', 'size', 'uploaded_at'], name='idx_user_size_date'),
            
            # Reference counting indexes
            models.Index(fields=['is_reference', 'reference_count'], name='idx_reference_count'),
            models.Index(fields=['original_file'], name='idx_original_file'),
        ]
    
    def __str__(self):
        return f"{self.original_filename} ({self.user_id})"


class UserStorageStats(models.Model):
    """Track storage usage statistics per user"""
    user_id = models.CharField(max_length=255, primary_key=True)
    total_storage_used = models.BigIntegerField(default=0, help_text='Actual storage used (bytes) after deduplication')
    original_storage_used = models.BigIntegerField(default=0, help_text='Storage that would be used without deduplication')
    file_count = models.IntegerField(default=0, help_text='Total number of files uploaded by user')
    last_updated = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'User Storage Statistics'
        verbose_name_plural = 'User Storage Statistics'
    
    def __str__(self):
        return f"Storage stats for {self.user_id}"


class RateLimitRecord(models.Model):
    """Track API call rate limiting per user per time window"""
    id = models.BigAutoField(primary_key=True)
    user_id = models.CharField(max_length=255, db_index=True)
    window_start = models.DateTimeField(db_index=True)
    request_count = models.IntegerField(default=0)
    
    class Meta:
        ordering = ['-window_start']
        unique_together = [['user_id', 'window_start']]
        indexes = [
            models.Index(fields=['user_id', 'window_start']),
        ]
    
    def __str__(self):
        return f"Rate limit for {self.user_id} at {self.window_start}"


class UploadJob(models.Model):
    """Track async file upload jobs"""
    JOB_STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=255, db_index=True)
    filename = models.CharField(max_length=255)
    file_size = models.BigIntegerField()
    file_type = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=JOB_STATUS_CHOICES, default='queued', db_index=True)
    
    # Job tracking
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    # Results
    file_id = models.UUIDField(null=True, blank=True, help_text='ID of created file if successful')
    error_message = models.TextField(blank=True, null=True)
    is_duplicate = models.BooleanField(default=False)
    duplicate_file_id = models.UUIDField(null=True, blank=True, help_text='ID of existing file if duplicate')
    
    retry_count = models.IntegerField(default=0)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user_id', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['user_id', 'created_at']),
        ]
    
    def __str__(self):
        return f"Upload job {self.id} for {self.user_id} - {self.status}"

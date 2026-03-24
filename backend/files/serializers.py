from rest_framework import serializers
from .models import File, UserStorageStats


class FileSerializer(serializers.ModelSerializer):
    """Serializer for File model with deduplication support"""
    
    original_file = serializers.PrimaryKeyRelatedField(read_only=True)
    
    def validate_size(self, value):
        """Validate that size is positive."""
        if value < 0:
            raise serializers.ValidationError("File size must be positive.")
        return value
    
    def to_representation(self, instance):
        """Custom representation to handle reference counting correctly."""
        data = super().to_representation(instance)
        
        # For reference files, set reference_count to 0 since they don't count as references
        if instance.is_reference:
            data['reference_count'] = 0
        
        # Convert UUID to string for original_file field
        if data.get('original_file'):
            data['original_file'] = str(data['original_file'])
        
        return data
    
    class Meta:
        model = File
        fields = [
            'id',
            'file',
            'original_filename',
            'file_type',
            'size',
            'uploaded_at',
            'user_id',
            'file_hash',
            'reference_count',
            'is_reference',
            'original_file'
        ]
        read_only_fields = [
            'id',
            'uploaded_at',
            'file_hash',
            'reference_count',
            'is_reference',
            'original_file'
        ]


class UserStorageStatsSerializer(serializers.Serializer):
    """Serializer for storage statistics"""
    
    # All fields are optional since we're handling both model instances and dictionaries
    user_id = serializers.CharField(max_length=255, required=False)
    total_storage_used = serializers.IntegerField(required=False)
    total_storage_used_mb = serializers.FloatField(required=False)
    total_storage_used_gb = serializers.FloatField(required=False)
    original_storage_used = serializers.IntegerField(required=False)
    storage_savings = serializers.IntegerField(required=False)
    storage_savings_mb = serializers.FloatField(required=False)
    storage_savings_gb = serializers.FloatField(required=False)
    savings_percent = serializers.FloatField(required=False)
    savings_percentage = serializers.SerializerMethodField()
    file_count = serializers.IntegerField(required=False)
    total_files = serializers.IntegerField(required=False)
    original_files = serializers.IntegerField(required=False)
    reference_files = serializers.IntegerField(required=False)
    file_types = serializers.ListField(required=False)
    size_distribution = serializers.DictField(required=False)
    average_file_size = serializers.FloatField(required=False)
    largest_file_size = serializers.IntegerField(required=False)
    smallest_file_size = serializers.IntegerField(required=False)
    last_updated = serializers.DateTimeField(required=False, read_only=True)
    
    def get_savings_percentage(self, obj):
        """Calculate savings percentage."""
        if isinstance(obj, dict):
            original = obj.get('original_storage_used', 0)
            if original > 0:
                savings = original - obj.get('total_storage_used', 0)
                return round((savings / original) * 100, 2)
            return 0.0
        
        if obj.original_storage_used > 0:
            savings = obj.original_storage_used - obj.total_storage_used
            return round((savings / obj.original_storage_used) * 100, 2)
        return 0.0
    
    def validate_total_storage_used(self, value):
        """Validate that total storage used is not negative."""
        if value < 0:
            raise serializers.ValidationError("Total storage used must be positive.")
        return value
    
    def validate_original_storage_used(self, value):
        """Validate that original storage used is not negative."""
        if value < 0:
            raise serializers.ValidationError("Original storage used must be positive.")
        return value
    
    def validate_file_count(self, value):
        """Validate that file count is not negative."""
        if value < 0:
            raise serializers.ValidationError("File count must be positive.")
        return value
    
    class Meta:
        model = UserStorageStats
        fields = [
            'user_id',
            'total_storage_used',
            'original_storage_used',
            'storage_savings',
            'savings_percentage',
            'file_count',
            'last_updated'
        ]
        read_only_fields = ['last_updated'] 
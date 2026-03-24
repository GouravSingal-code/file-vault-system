"""File path generation utilities."""
import os
import re
import uuid
from django.utils import timezone


def sanitize_filename(filename: str) -> str:
    """
    Remove path separators and dangerous characters from a filename.

    Keeps alphanumerics, hyphens, underscores, dots, and spaces.
    Falls back to 'upload' if the result is empty.
    """
    # Strip any directory components first
    filename = os.path.basename(filename)
    # Remove characters that are not safe in filenames
    filename = re.sub(r'[^\w.\- ]', '_', filename)
    return filename or 'upload'


def generate_file_upload_path(instance, filename: str) -> str:
    """
    Generate a unique, collision-free upload path for a file.

    Path format: uploads/<year>/<month>/<uuid>/<sanitized_filename>

    This structure:
    - Avoids directory listing issues at scale (files spread across date dirs)
    - Uses UUID to ensure uniqueness even for identical filenames
    - Sanitizes the filename to prevent path-traversal attacks
    """
    today = timezone.now()
    unique_dir = str(uuid.uuid4())
    safe_name = sanitize_filename(filename)
    return os.path.join('uploads', str(today.year), f'{today.month:02d}', unique_dir, safe_name)

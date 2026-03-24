"""
Shared pytest fixtures for the files app test suite.
"""
import io
import pytest


@pytest.fixture
def sample_file_bytes():
    """10-byte test file content."""
    return b'helloworld'


@pytest.fixture
def sample_file_obj(sample_file_bytes):
    """In-memory file-like object."""
    return io.BytesIO(sample_file_bytes)


@pytest.fixture
def make_file(db):
    """
    Factory fixture for creating File model instances.

    Usage::

        def test_something(make_file):
            f = make_file(user_id='u1', size=100)
    """
    from files.models import File

    def _make(user_id='test_user', filename='file.txt', file_hash=None,
               size=100, file_type='text/plain', is_reference=False):
        import uuid
        h = file_hash or str(uuid.uuid4()).replace('-', '')
        return File.objects.create(
            user_id=user_id,
            original_filename=filename,
            file_type=file_type,
            size=size,
            file_hash=h,
            is_reference=is_reference,
            reference_count=0 if is_reference else 1,
        )

    return _make

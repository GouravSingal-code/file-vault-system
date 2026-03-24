#!/bin/sh
set -e
trap 'echo "Error at line $LINENO — exiting"; exit 1' ERR

ENVIRONMENT=${DJANGO_ENVIRONMENT:-development}
export DJANGO_SETTINGS_MODULE="core.settings.${ENVIRONMENT}"

echo "Starting File Vault — environment: ${ENVIRONMENT}"

# Ensure required directories exist with restricted permissions
mkdir -p media/uploads staticfiles
mkdir -p -m 700 logs

# Apply database migrations
echo "Running migrations..."
python manage.py migrate --noinput

# Collect static files (already done at build time in production, harmless here)
python manage.py collectstatic --noinput

if [ "${ENVIRONMENT}" = "production" ]; then
    # Calculate optimal worker count: 2 × CPU cores + 1
    WORKERS=${GUNICORN_WORKERS:-$(( 2 * $(nproc) + 1 ))}
    echo "Starting Gunicorn with ${WORKERS} workers..."
    exec gunicorn core.wsgi:application \
        --bind 0.0.0.0:8000 \
        --workers "${WORKERS}" \
        --worker-class sync \
        --timeout 60 \
        --keep-alive 5 \
        --max-requests 1000 \
        --max-requests-jitter 100 \
        --access-logfile - \
        --error-logfile - \
        --log-level info
else
    echo "Starting Django development server on http://0.0.0.0:8000/"
    exec python manage.py runserver 0.0.0.0:8000
fi

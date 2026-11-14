#!/bin/sh
set -e

# Apply migrations, collect static files, then serve. The lab lease scheduler
# runs as its own container sharing the same image (compose service lab-tick).
python manage.py migrate --noinput
python manage.py collectstatic --noinput

case "$1" in
  scheduler)
    exec python manage.py lab_tick --loop
    ;;
  python)
    # Pass-through for one-off management commands (compose run).
    exec "$@"
    ;;
  *)
    exec gunicorn portal.wsgi --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 60
    ;;
esac

#!/usr/bin/env sh
# Runs before the web server starts: apply migrations and create or update the manager login.
set -e
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  python manage.py migrate --no-input
  python manage.py ensure_admin
  if [ "${SALON_SEED_DEMO:-false}" = "true" ]; then
    python manage.py seed_demo --no-admin
  fi
fi
exec "$@"

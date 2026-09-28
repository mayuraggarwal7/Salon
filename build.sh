#!/usr/bin/env bash
# Build step for Render (or any host): install, collect static files, migrate, create the manager login.
set -o errexit
pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py ensure_admin
if [ "${SALON_SEED_DEMO:-false}" = "true" ]; then
  python manage.py seed_demo --no-admin
fi

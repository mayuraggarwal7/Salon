# Production image for the salon app (used by docker-compose.yml on a VPS such as OVH).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SALON_MEDIA_ROOT=/data/media

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Static files are baked into the image and served by whitenoise.
RUN DJANGO_DEBUG=false DJANGO_SECRET_KEY=build-only-not-secret python manage.py collectstatic --no-input \
    && useradd --create-home --uid 1000 salon \
    && mkdir -p /data/media && chown -R salon:salon /data /app

USER salon
EXPOSE 8000
ENTRYPOINT ["deploy/entrypoint.sh"]
CMD ["gunicorn", "salon_project.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60", "--access-logfile", "-"]

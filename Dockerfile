# Production image for the API (Lightsail containers, or any container host).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Collect static files (admin CSS, served by whitenoise). The settings need values to import, so
# throwaway ones are used here, for this one command only; the running container gets real ones.
RUN DJANGO_SECRET_KEY="build-only-value-never-used-at-runtime-0123456789" \
    DATABASE_URL="postgres://build:build@localhost:5432/build" \
    DJANGO_ALLOWED_HOSTS="build.invalid" \
    CORS_ALLOWED_ORIGINS="https://build.invalid" \
    python manage.py collectstatic --no-input

RUN chmod +x docker-entrypoint.sh && useradd --create-home --uid 1000 app && chown -R app /app
USER app

EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--worker-class", "gthread", "--timeout", "90", "--access-logfile", "-"]

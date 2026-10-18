"""Production settings. Run `python manage.py check --deploy` against these.

Unlike local settings, nothing security-relevant has a default here: the secret key, allowed hosts,
frontend origin and database must all be set in the environment, or the app refuses to start.
"""

from django.core.exceptions import ImproperlyConfigured

from config.settings.base import *  # noqa: F403  (settings modules are the one place star-imports are idiomatic)
from config.settings.base import env

DEBUG = False

# --- Required settings (no defaults) -----------------------------------------------------------------

# base.py already requires DJANGO_SECRET_KEY and DATABASE_URL. A placeholder key is as bad as none.
_PLACEHOLDER_KEYS = ("change-me", "changeme", "django-insecure")
if len(SECRET_KEY) < 32 or any(marker in SECRET_KEY.lower() for marker in _PLACEHOLDER_KEYS):  # noqa: F405
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be a real secret (32+ characters, not a placeholder). "
        'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(50))"'
    )

# Exact hostnames, e.g. weather-truck-api.xxxx.eu-north-1.cs.amazonlightsail.com (set by the deploy script).
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must list exact hostnames (no '*').")

# Only the frontend may call the API from a browser, e.g. https://weather-truck.vercel.app
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS")
if not CORS_ALLOWED_ORIGINS or any(not origin.startswith("https://") for origin in CORS_ALLOWED_ORIGINS):
    raise ImproperlyConfigured("CORS_ALLOWED_ORIGINS must list the frontend's https:// origin(s).")
CORS_ALLOW_ALL_ORIGINS = False

# --- HTTPS only -----------------------------------------------------------------------------------------

# The host's proxy terminates TLS and sets X-Forwarded-Proto.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
# The host's health checker may call over plain HTTP inside the network; don't redirect it.
SECURE_REDIRECT_EXEMPT = [r"^api/health/$"]
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 30)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS: list[str] = env.list("CSRF_TRUSTED_ORIGINS", default=[])

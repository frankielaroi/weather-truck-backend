"""Settings shared by every environment. Values that vary come from env vars / .env."""

from pathlib import Path

import django_stubs_ext
import environ

# Lets generic Django classes (e.g. ModelAdmin[Trip]) be subscripted at runtime for type checking.
django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY: str = env("DJANGO_SECRET_KEY")
DEBUG: bool = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])


# Application definition

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
]
LOCAL_APPS = [
    "apps.users",
    "apps.routing",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

AUTH_USER_MODEL = "users.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    # Compresses large JSON (the trip, the heatmap). Safe here: API responses carry no secrets or CSRF tokens.
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database — PostgreSQL, configured via DATABASE_URL
# e.g. postgres://weather_truck:password@localhost:5432/weather_truck

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}


# CORS — the Next.js frontend calls this API from another origin

CORS_ALLOWED_ORIGINS: list[str] = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:3000"])


# Django REST framework

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "apps.routing.errors.api_exception_handler",
    # Trip creation calls outside providers, so it's rate-limited per visitor (B10).
    "DEFAULT_THROTTLE_RATES": {"trip_create": env("TRIP_CREATE_RATE", default="20/min")},
    # Proxies in front of the app (e.g. 1 on Render), so throttling sees the real client address.
    "NUM_PROXIES": env.int("NUM_PROXIES", default=None),
}


# Cache — in Postgres, so every gunicorn worker shares it and it survives restarts.
# Create the table once per database: `python manage.py createcachetable` (tests do this themselves).

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
        "TIMEOUT": 60 * 60 * 24,
    }
}


# Third-party API keys (routing, geocoding, weather)

ORS_API_KEY: str = env("ORS_API_KEY", default="")
# api.openrouteservice.org shuts down Nov 2026; api.heigit.org is its replacement.
ORS_BASE_URL: str = env("ORS_BASE_URL", default="https://api.heigit.org").rstrip("/")
# Routing, 1st choice (public server, no key; see the README for its limits).
VALHALLA_URL: str = env("VALHALLA_URL", default="https://valhalla1.openstreetmap.de").rstrip("/")
# Weather (no key).
OPEN_METEO_URL: str = env("OPEN_METEO_URL", default="https://api.open-meteo.com/v1/forecast")

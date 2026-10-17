"""Local development settings."""

from config.settings.base import *  # noqa: F403  (settings modules are the one place star-imports are idiomatic)
from config.settings.base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Serve static files straight from app directories, so `collectstatic` isn't needed locally.
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

"""Prints the Lightsail container definition as JSON. Secrets come from the environment, not the repo."""

import json
import os
import sys
from urllib.parse import quote

image, host = sys.argv[1:3]
env = os.environ
database_url = (
    f"postgres://{quote(env['DB_USER'], safe='')}:{quote(env['DB_PASS'], safe='')}"
    f"@{env['DB_HOST']}:{env['DB_PORT']}/{env['DB_NAME']}"
)
definition = {
    "api": {
        "image": image,
        "ports": {"8000": "HTTP"},
        "environment": {
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "DJANGO_SECRET_KEY": env["SECRET_KEY"],
            "DATABASE_URL": database_url,
            "DJANGO_ALLOWED_HOSTS": host,
            "CORS_ALLOWED_ORIGINS": env["CORS_ALLOWED_ORIGINS"],
            "ORS_API_KEY": env["ORS_API_KEY"],
            "NUM_PROXIES": "1",
        },
    }
}
print(json.dumps(definition))

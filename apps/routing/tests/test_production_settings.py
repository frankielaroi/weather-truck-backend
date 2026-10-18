"""B13: production settings refuse to start without real security settings, and pass `check --deploy`."""

import importlib
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import environ
import pytest

BACKEND = Path(__file__).resolve().parents[3]
SECRET = "a-long-random-production-secret-key-0123456789-abcdefghij"
VALID_ENV = {
    "DJANGO_SECRET_KEY": SECRET,
    "DATABASE_URL": "postgres://user:pass@db.example.com:5432/app",
    "DJANGO_ALLOWED_HOSTS": "api.example.com",
    "CORS_ALLOWED_ORIGINS": "https://app.example.com",
}
CONFIG_KEYS = (*VALID_ENV, "DJANGO_DEBUG", "DJANGO_SECURE_SSL_REDIRECT")


def without(key: str) -> dict[str, str]:
    return {k: v for k, v in VALID_ENV.items() if k != key}


@pytest.fixture
def load_production(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Import config.settings.production fresh with exactly the given environment (and no .env file)."""
    saved = {name: sys.modules.get(name) for name in ("config.settings.base", "config.settings.production")}

    def load(**env: str) -> ModuleType:
        for key in CONFIG_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(environ.Env, "read_env", classmethod(lambda cls, *a, **k: None))
        for name in saved:
            sys.modules.pop(name, None)
        return importlib.import_module("config.settings.production")

    yield load
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def test_a_complete_environment_gives_a_hardened_configuration(load_production: Any) -> None:
    settings = load_production(**VALID_ENV)
    assert settings.DEBUG is False
    assert settings.SECRET_KEY == SECRET
    assert settings.ALLOWED_HOSTS == ["api.example.com"]
    assert settings.CORS_ALLOWED_ORIGINS == ["https://app.example.com"]
    assert settings.CORS_ALLOW_ALL_ORIGINS is False
    assert settings.SECURE_SSL_REDIRECT is True
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert settings.SECURE_HSTS_SECONDS > 0
    assert settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
    assert "GZipMiddleware" in " ".join(settings.MIDDLEWARE)


def test_debug_stays_off_even_if_the_environment_says_otherwise(load_production: Any) -> None:
    assert load_production(**VALID_ENV, DJANGO_DEBUG="true").DEBUG is False


def test_the_health_check_is_exempt_from_the_https_redirect(load_production: Any) -> None:
    patterns = load_production(**VALID_ENV).SECURE_REDIRECT_EXEMPT
    assert any(re.match(p, "api/health/") for p in patterns)
    assert not any(re.match(p, "api/trips/1/") for p in patterns)


@pytest.mark.parametrize(
    "secret", ["", "change-me", "django-insecure-" + "x" * 40, "short-key", "Please-CHANGE-ME-" + "x" * 40]
)
def test_a_placeholder_secret_key_stops_startup(load_production: Any, secret: str) -> None:
    with pytest.raises(Exception, match="SECRET_KEY"):
        load_production(**{**VALID_ENV, "DJANGO_SECRET_KEY": secret})


@pytest.mark.parametrize("missing", ["DJANGO_SECRET_KEY", "DATABASE_URL", "CORS_ALLOWED_ORIGINS"])
def test_a_missing_required_setting_stops_startup(load_production: Any, missing: str) -> None:
    with pytest.raises(Exception, match=missing):
        load_production(**without(missing))


@pytest.mark.parametrize("hosts", ["*", "api.example.com,*"])
def test_wildcard_hosts_are_rejected(load_production: Any, hosts: str) -> None:
    with pytest.raises(Exception, match="exact hostnames"):
        load_production(**{**VALID_ENV, "DJANGO_ALLOWED_HOSTS": hosts})


def test_missing_hosts_are_rejected(load_production: Any) -> None:
    with pytest.raises(Exception, match="exact hostnames"):
        load_production(**without("DJANGO_ALLOWED_HOSTS"))


@pytest.mark.parametrize("origins", ["", "http://app.example.com", "https://app.example.com,http://localhost:3000"])
def test_cors_needs_the_frontends_https_origin_only(load_production: Any, origins: str) -> None:
    with pytest.raises(Exception, match="CORS_ALLOWED_ORIGINS"):
        load_production(**{**VALID_ENV, "CORS_ALLOWED_ORIGINS": origins})


def test_check_deploy_passes_with_production_settings() -> None:
    env = {k: v for k, v in os.environ.items() if k not in CONFIG_KEYS}
    env.update(VALID_ENV, DJANGO_SETTINGS_MODULE="config.settings.production")
    code = "\n".join(
        [
            "import environ; environ.Env.read_env = classmethod(lambda cls, *a, **k: None)",
            "import django; django.setup()",
            "from django.core.management import call_command",
            "call_command('check', deploy=True, fail_level='WARNING')",
            "print('CHECK-DEPLOY-OK')",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120
    )
    assert "CHECK-DEPLOY-OK" in result.stdout, result.stderr[-1500:]


def test_the_wsgi_and_asgi_entry_points_use_production_settings() -> None:
    assert "config.settings.production" in (BACKEND / "config" / "wsgi.py").read_text("utf-8")
    assert "config.settings.production" in (BACKEND / "config" / "asgi.py").read_text("utf-8")


# --- Container image and Lightsail deploy -------------------------------------------------------------


def test_dockerfile_uses_production_settings_and_does_not_run_as_root() -> None:
    dockerfile = (BACKEND / "Dockerfile").read_text("utf-8")
    assert "DJANGO_SETTINGS_MODULE=config.settings.production" in dockerfile
    assert "collectstatic --no-input" in dockerfile
    assert "USER app" in dockerfile
    assert 'ENTRYPOINT ["./docker-entrypoint.sh"]' in dockerfile
    assert "gunicorn" in dockerfile and "config.wsgi:application" in dockerfile


def test_dockerfile_bakes_in_no_real_secrets() -> None:
    dockerfile = (BACKEND / "Dockerfile").read_text("utf-8")
    assert "ORS_API_KEY" not in dockerfile
    assert "build-only-value-never-used-at-runtime" in dockerfile  # the throwaway key, for collectstatic only


def test_entrypoint_migrates_before_serving_and_stops_on_failure() -> None:
    script = (BACKEND / "docker-entrypoint.sh").read_text("utf-8")
    assert "set -eu" in script
    assert script.index("migrate --no-input") < script.index("createcachetable") < script.index('exec "$@"')


def test_dockerignore_keeps_secrets_and_local_files_out_of_the_image() -> None:
    ignored = (BACKEND / ".dockerignore").read_text("utf-8").splitlines()
    for entry in (".env", ".venv", ".git", "staticfiles"):
        assert entry in ignored


def test_deploy_script_health_checks_the_right_path_and_keeps_secrets_out_of_the_repo() -> None:
    script = (BACKEND / "deploy" / "lightsail" / "deploy.sh").read_text("utf-8")
    containers = (BACKEND / "deploy" / "lightsail" / "containers.py").read_text("utf-8")
    assert '"path":"/api/health/"' in script
    assert "set -o errexit" in script
    assert "os.environ" in containers or "env[" in containers  # secrets come from the environment
    committed = script + containers
    assert not re.search(r"ORS_API_KEY=\w{20,}", committed)
    assert "DJANGO_ALLOWED_HOSTS" in containers  # the service's own hostname, set at deploy time


def test_requirements_include_the_production_packages() -> None:
    requirements = (BACKEND / "requirements.txt").read_text("utf-8").lower()
    for package in ("gunicorn", "whitenoise", "psycopg", "django-environ", "django-cors-headers"):
        assert package in requirements

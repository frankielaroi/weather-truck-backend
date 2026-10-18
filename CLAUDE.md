# Weather-Aware Truck Routing — API

Django API that plans truck routes, samples weather along each route at the truck's ETA, scores weather risk against load weight, and recommends the safest route.

## Quick Facts
- **Stack:** Python 3.13, Django 6.1, Django REST framework, PostgreSQL
- **Package Manager:** uv (`pyproject.toml` + `uv.lock`; `requirements.txt` is exported for hosts that need it)
- **Environment:** `uv run` uses the project `.venv` automatically. Config comes from `.env` (copy `.env.example`).

## Development Workflow

### Commands
- **Run Dev Server:** `uv run python manage.py runserver`
- **Database Migrations:** `uv run python manage.py makemigrations` and `uv run python manage.py migrate`
- **Django Shell:** `uv run python manage.py shell`
- **Re-export requirements.txt after changing deps:** `uv export --no-dev --no-hashes -o requirements.txt`

### Code Quality & Formatting
- **Lint Check:** `uv run ruff check .`
- **Format Code:** `uv run ruff format .`
- **Type Check:** `uv run mypy .` (mypy + django-stubs plugin, strict)

### Testing
- **Run All Tests:** `uv run pytest`
- **Run Specific Test:** `uv run pytest apps/routing/tests/test_models.py`
- **Coverage:** `uv run pytest --cov=apps`

## Key Directories
- `config/` - Settings (`settings/base.py`, `local.py`, `production.py`) and root urlconf.
- `apps/users/` - Custom `User` model (`AUTH_USER_MODEL = "users.User"`).
- `apps/routing/` - Trips, route options, weather checkpoints, forecast cache, and the API.

## Data Model (`apps/routing/models.py`)
- `Trip` - origin/destination (name + lat/lng), departure time, load weight (lb), checkpoint spacing (10/25/50 mi).
- `Route` - up to 3 options per trip: distance, duration, GeoJSON geometry, risk summary (No Travel / Severe / High miles, average risk), `is_recommended` (at most one per trip, enforced by a DB constraint).
- `Checkpoint` - weather sample on a route at the truck's ETA: wind, gusts, rain, snow, temperature, `risk_level` (null = Unknown), `risk_reasons`.
- `ForecastCache` - hourly forecast per rounded lat/lng grid cell, reused across trips and the heatmap.
- `RiskLevel` / `SampleInterval` - in `choices.py`. `RiskLevel` is LOW(0) → NO_TRAVEL(4), ordered so values can be compared and averaged.
- `risk.py` - pure risk engine: `assess(WeatherSample, load_weight_lb) -> RiskAssessment(level, reasons)`. No DB/network/clock; imports only `choices.py`. Its module docstring records the boundary decisions (shared edges go up, top of scale is strict, sustained wind only, load limits strict, missing = 0, all missing = Unknown/None). It never raises: unusable weather values (text, NaN, inf, negative, beyond plausible max) are ignored and listed in `data_issues`; an unusable load weight is assumed heavy (over 40,000 lb); unexpected errors return Unknown. Use `WeatherSample.from_mapping()` for provider dicts. Property tests (hypothesis) in `tests/test_risk_fallbacks.py` guard these guarantees.
- `recommend.py` - pure B2 logic: `summarize_route(checkpoints, distance)` -> `RouteSummary` (miles per level incl. Unknown, worst level, mile-weighted average risk, incomplete warning) and `recommend(options)` -> `Recommendation` (index, deciding step, reason, per-route warnings). Ranking: No Travel, Severe, High miles (0.1 mi tolerance), average risk (0.01), travel time, then input order.
- Use `Trip.objects.with_details()` to load trips with routes and checkpoints in 3 queries.

## Coding Conventions & Architecture

### Django Philosophy
- **Function-Based Views (FBVs):** Prefer FBVs with explicit decorators (`@api_view`); use CBVs/viewsets only for boilerplate CRUD.
- **Fat Models, Skinny Views:** Business logic (risk rules, route ranking, API clients) lives in model methods, managers/querysets, or `services.py` modules — not in views.
- **Query Optimization:** Never allow N+1 queries. Put `select_related()` / `prefetch_related()` in querysets/managers, and cover them with `django_assert_num_queries` tests.

### Python Style
- Modern Python syntax. Strict type hints on every function signature; minimize `Any`.
- Prefer early returns over nested `if/else`.
- No `import *`, except the settings modules (`local.py`/`production.py` extend `base.py`), which is the Django convention.

### Security Hardening
- Never commit secrets. All config comes from env vars via `django-environ` (`SECRET_KEY`, `DATABASE_URL`, API keys).
- `AUTH_USER_MODEL` is set to `users.User` — keep it.
- Check production readiness: `DJANGO_SETTINGS_MODULE=config.settings.production uv run python manage.py check --deploy`

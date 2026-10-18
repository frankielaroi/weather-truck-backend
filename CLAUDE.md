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
- `Route` - up to 3 options per trip: `label` ("via I-80 W"), `provider` / `profile`, `notes`, distance, duration, GeoJSON geometry, miles per level (No Travel / Severe / High / Moderate / Low / Unknown, adding up to the distance), `average_risk` and `max_risk_level` (null when nothing is known), `warnings`, and on the recommended one `recommendation_reason` + `deciding_step`. `is_recommended` is at most one per trip (DB constraint). Build rows with `Route.from_results(trip, index, planned, summary, recommendation)`.
- `Checkpoint` - weather sample on a route at the truck's ETA: wind, gusts, rain, snow, temperature, description, `forecast_time` (the hour used), `risk_level` (null = Unknown), `risk_reasons`, `data_notes` (why data is missing or stale, e.g. "Beyond forecast range"). Build rows with `Checkpoint.from_results(route, point, weather)`.
- `ForecastCache` - hourly forecast per rounded lat/lng grid cell, reused across trips and the heatmap.
- `RiskLevel` / `SampleInterval` - in `choices.py`. `RiskLevel` is LOW(0) → NO_TRAVEL(4), ordered so values can be compared and averaged.
- `risk.py` - pure risk engine: `assess(WeatherSample, load_weight_lb) -> RiskAssessment(level, reasons)`. No DB/network/clock; imports only `choices.py`. Its module docstring records the boundary decisions (shared edges go up, top of scale is strict, sustained wind only, load limits strict, missing = 0, all missing = Unknown/None). It never raises: unusable weather values (text, NaN, inf, negative, beyond plausible max) are ignored and listed in `data_issues`; an unusable load weight is assumed heavy (over 40,000 lb); unexpected errors return Unknown. Use `WeatherSample.from_mapping()` for provider dicts. Property tests (hypothesis) in `tests/test_risk_fallbacks.py` guard these guarantees.
- `recommend.py` - pure B2 logic: `summarize_route(checkpoints, distance)` -> `RouteSummary` (miles per level incl. Unknown, worst level, mile-weighted average risk, incomplete warning) and `recommend(options)` -> `Recommendation` (index, deciding step, reason, per-route warnings). Ranking: No Travel, Severe, High miles (0.1 mi tolerance), average risk (0.01), travel time, then input order.
- `geocoding.py` - B4 place suggestions from ORS Pelias (`ORS_BASE_URL`, `ORS_API_KEY`): 5 s timeout, US only, 1-day cache in the DB cache table, stale cache (up to 7 days) served if the provider fails.
- `route_providers.py` - B5 clients: `valhalla_routes()` (truck, splits trips over the public server's 1,500 km cap into joined sections) and `ors_routes()` (`driving-hgv` / `driving-car`). Each returns `ProviderRoute` (miles, seconds, [lng, lat] path, per-section times) or raises `NoRouteFound` / `ProviderRefused` / `ProviderUnavailable` / `ProviderTimeout`.
- `routes.py` - B5 `plan_routes(origin, destination, load_lb)` -> up to 3 `PlannedRoute`: Valhalla truck -> ORS truck -> ORS car, then side detours on the same provider, near-duplicate removal (>90% shared) and "via <road>" labels. 15 s per request, 25 s total. Raises `NoRoute` (422) / 502 / 504.
- `checkpoints.py` - B6 `sample_checkpoints(route, departure, interval)` -> `CheckpointPoint`s at mile 0, every interval and the destination (a stop within 1 mile of the end merges into it), placed on the path, with ETAs from the route's section times. Pure; no rest stops.
- `weather.py` - B7 `checkpoint_weather(points, load_lb)` -> `CheckpointWeather` per checkpoint (values in mph / in/hr / °F, nearest-hour forecast time, description, `RiskAssessment`). Open-Meteo in batches of 50 (10 s timeout, parallel), cached in `ForecastCache` per ~1 km cell for 1 h; on failure a cache up to 6 h old is used with a note, else Unknown. Never raises for provider problems.
- `trips.py` - B8 `create_trip(TripRequest)`: route plan (cached 24 h per ends + truck weight) -> checkpoints -> one weather call for all routes -> summaries + recommendation -> trip, routes and checkpoints saved in one transaction as Ready. On any failure a Failed trip is saved with the reason and the error re-raised. Logs per-step times. The only module that uses both providers and the DB.
- `responses.py` - B9 `trip_response(trip)`: the trip JSON (field list in the README; real example in `docs/example-trip-response.json`; shape enforced by `tests/test_responses.py`). Levels as `{value, name}`, GeoJSON paths from `Route.display_path`, display rounding only here. Use with `Trip.objects.with_details()` (no extra queries).
- `geometry.py` - path simplification (Ramer–Douglas–Peucker, 30 m) used for `Route.display_path`.
- `errors.py` - the one API error shape and `api_exception_handler` (set in `REST_FRAMEWORK`); raise `ProviderUnavailable` (502) / `ProviderTimeout` (504) from provider clients.
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

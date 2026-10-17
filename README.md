# Weather-Aware Truck Routing — API

Django 6 + Django REST framework API that plans truck routes, samples weather along each route at the truck's ETA, scores weather risk against load weight, and recommends the safest route.

Frontend: [weather-truck-frontend](https://github.com/frankielaroi/weather-truck-frontend)

## Requirements
- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 14+

## Running locally (http://localhost:8000)

1. Create the database (once):
   ```sql
   CREATE ROLE weather_truck LOGIN PASSWORD 'weather_truck' CREATEDB;
   CREATE DATABASE weather_truck OWNER weather_truck;
   ```
   `CREATEDB` lets the test runner create its own test database.
2. Configure and run:
   ```bash
   cp .env.example .env          # set DJANGO_SECRET_KEY, DATABASE_URL, API keys
   uv sync
   uv run python manage.py migrate
   uv run python manage.py runserver
   ```

Check it: `GET http://localhost:8000/api/health/` → `{"status": "ok"}`

## Quality checks

```bash
uv run pytest            # tests (needs the database above)
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy .            # types
```

## Data model

| Model | What it holds |
| --- | --- |
| `Trip` | Origin, destination, departure time, load weight, checkpoint spacing (10/25/50 mi) |
| `Route` | Up to 3 options per trip: distance, duration, geometry, risk summary, recommended flag |
| `Checkpoint` | Weather at the truck's ETA at a point on a route, plus its risk level and reasons |
| `ForecastCache` | Hourly forecasts per grid cell, reused across trips and the heatmap |

## Environment variables

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key (required) |
| `DJANGO_DEBUG` | `true` locally, `false` in production |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames |
| `DATABASE_URL` | PostgreSQL URL, e.g. `postgres://user:pass@localhost:5432/weather_truck` |
| `CORS_ALLOWED_ORIGINS` | Frontend origin(s) allowed to call the API |
| `CSRF_TRUSTED_ORIGINS` | Production only: trusted origins for admin/CSRF |
| `ORS_API_KEY` | OpenRouteService key (routing + geocoding) |

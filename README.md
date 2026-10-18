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

## Third-party providers

Chosen in MODULES.md B3, from real requests made on 2026-10-18.

| Job | Provider | Key | Units we request | Limits (free tier) |
| --- | --- | --- | --- | --- |
| Weather | [Open-Meteo](https://open-meteo.com/) forecast API | none | mph (`wind_speed_unit=mph`), inches (`precipitation_unit=inch`), °F, UTC | 10,000 calls/day, 5,000/hour, 600/min; non-commercial use |
| Geocoding | [OpenRouteService](https://openrouteservice.org/) `geocode/autocomplete` | `ORS_API_KEY` | — | per-key daily and per-minute quotas (see the ORS dashboard) |
| Routing (primary) | OpenRouteService `directions/driving-hgv` | `ORS_API_KEY` | metres and seconds, converted to miles at the client | 2,000 requests/day, 40/min |
| Routing (fallback) | Public [Valhalla](https://valhalla1.openstreetmap.de/) server, `truck` costing | none (`VALHALLA_URL`) | miles, seconds | fair use only, no service guarantee; 1,500 km cap on requests with via points |

**What the probes showed**

- **Open-Meteo:** one request for 50 points along Chicago → Denver returned 72 hours of hourly wind speed, gusts, rain, showers and snowfall in about 1 s. Hours start at 00:00 UTC today, so we request 3 days to cover 0–48 h from any time of day.
- **Geocoding (ORS):** "Denver" returned "Denver, CO, USA" first, in about 1 s.
- **Routing:** no provider tried returned 3 routes for Chicago → Denver. The public OSRM and Valhalla servers each returned 2 (Valhalla: 1,016 and 1,079 mi, about 1.7 s). During the probe, ORS routing returned 502s and timed out after 60 s even for a 90-mile trip, while ORS geocoding worked, so ORS routing was having an outage. Its alternative routes and timings still need checking once it recovers.

**Decisions**

- **Decision — third route:** when the provider returns fewer than 3 distinct routes, we make more by routing through a point to each side of the direct route (B3's fallback). Near-duplicates are then dropped (B5), so a trip may still end up with fewer than 3.
- **Decision — routing fallback:** ORS is the primary router: it has a truck profile that takes the load weight and published limits. If ORS fails or doesn't answer within the routing timeout, the same request goes to Valhalla. The response says which provider was used.
- **Decision — rain:** rain = Open-Meteo `rain` + `showers` (all liquid precipitation in the hour). Snow = `snowfall` (inches of snow per hour).
- **Decision — wind:** the risk rules use `wind_speed_10m` (sustained); `wind_gusts_10m` is stored and shown only.
- OSRM was ruled out: car profile only, and its demo server isn't for production use.

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
| `VALHALLA_URL` | Fallback routing server (default `https://valhalla1.openstreetmap.de`) |

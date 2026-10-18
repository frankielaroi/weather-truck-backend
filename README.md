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
   uv run python manage.py createcachetable   # cache for place searches and forecasts
   uv run python manage.py runserver
   ```

Check it: `GET http://localhost:8000/api/health/` → `{"status": "ok"}`

## Third-party providers

Chosen in MODULES.md B3, from real requests made on 2026-10-18.

| Job | Provider | Key | Units we request | Limits (free tier) |
| --- | --- | --- | --- | --- |
| Weather | [Open-Meteo](https://open-meteo.com/) forecast API | none | mph (`wind_speed_unit=mph`), inches (`precipitation_unit=inch`), °F, UTC | 10,000 calls/day, 5,000/hour, 600/min; non-commercial use |
| Geocoding | [OpenRouteService](https://openrouteservice.org/) (Pelias) `api.heigit.org/pelias/v1/autocomplete` | `ORS_API_KEY` | — | 3,000/day, 100/min |
| Routing, 1st | Public [Valhalla](https://valhalla1.openstreetmap.de/) server, `truck` costing with the load weight | none (`VALHALLA_URL`) | miles, seconds | fair use only, no service guarantee; 1,500 km cap on requests with via points |
| Routing, 2nd | OpenRouteService `api.heigit.org/openrouteservice/v2/directions/driving-hgv` | `ORS_API_KEY` | metres and seconds, converted to miles at the client | 2,000/day, 40/min (shared with the 3rd option) |
| Routing, 3rd | OpenRouteService `…/directions/driving-car` | `ORS_API_KEY` | metres and seconds | counts against the same Directions quota |

**ORS address change:** `api.openrouteservice.org` is deprecated and shuts down on **November 2–6, 2026**; its quota has already been cut (its responses report 200/day). We only use the new `api.heigit.org` addresses (set by `ORS_BASE_URL`); the quotas above are the ones shown for this key in the [HeiGIT account dashboard](https://account.heigit.org/manage/key). Announcement: <https://ask.openrouteservice.org/t/deprecating-api-openrouteservice-org-in-favour-of-api-heigit-org/7912>.

**What the probes showed**

- **Open-Meteo:** one request for 50 points along Chicago → Denver returned 72 hours of hourly wind speed, gusts, rain, showers and snowfall in about 1 s. Hours start at 00:00 UTC today, so we request 3 days to cover 0–48 h from any time of day.
- **Geocoding (ORS, new address):** "Denver" returned "Denver, CO, USA" first, in 0.8 s.
- **Valhalla:** Chicago → Denver returned 2 truck routes (1,016 and 1,079 mi, 15.7 h and 17.0 h) in 1.7 s on every try; maneuver times add up to the route's total time. A request through a via point 120 miles off the direct line was refused by the 1,500 km cap.
- **ORS truck (`driving-hgv`), new address:** every request timed out at 20–37 s, including a 90-mile Chicago → Milwaukee trip, so alternative routes couldn't be checked. On the old address, the two requests that did answer took 55 s and 1.7 s (1,002 mi / 23.0 h direct; 1,111 mi / 26.0 h via a point to the south); their step times add up to the total exactly.
- **ORS car (`driving-car`), new address:** Chicago → Milwaukee, 92 mi in 1.0 s.
- **OSRM public demo:** 2 routes for Chicago → Denver, car profile only.

No provider returned 3 routes for a ~1,000-mile trip.

**Decisions**

- **Decision — routing order:** Valhalla (truck) first; if it fails, times out or refuses the request, ORS truck; if that fails too, ORS car. Valhalla goes first because it was fast and reliable on the day while ORS truck routing timed out on every distance. Each route records which provider and profile made it, and a route from the car profile carries a note that it isn't truck-specific.
- **Decision — third route:** when the provider returns fewer than 3 distinct routes, we make more by routing through a point to each side of the direct route (B3's fallback). Near-duplicates are then dropped (B5), so a trip may still end up with fewer than 3. If Valhalla refuses a detour (its 1,500 km cap), the detour goes to the next provider in the order.
- **Decision — rain:** rain = Open-Meteo `rain` + `showers` (all liquid precipitation in the hour). Snow = `snowfall` (inches of snow per hour).
- **Decision — wind:** the risk rules use `wind_speed_10m` (sustained); `wind_gusts_10m` is stored and shown only.
- OSRM was ruled out: car profile only, and its demo server isn't for production use.
## API

Every error uses one shape: `{"error": {"code": "...", "message": "...", "fields": {...}}}`. `fields` appears only on 400s and maps each bad input to its messages.

### `GET /api/places/?q=<text>` — place suggestions

Up to 5 US places matching `q`. Fewer than 3 non-space characters returns an empty list without calling the provider. Identical searches are cached for a day.

```http
GET /api/places/?q=Denver
```

```json
{"results": [{"name": "Denver, CO, USA", "lat": 39.740959, "lng": -104.985798}, {"name": "Denver County, CO, USA", "lat": 39.7618, "lng": -104.8811}]}
```

| Status | When |
| --- | --- |
| 200 | Results (possibly empty) |
| 400 | `q` missing or longer than 100 characters (`invalid_input`, with `fields.q`) |
| 502 | Geocoder failed or refused the request (`provider_unavailable`) |
| 504 | Geocoder didn't answer within 5 s (`provider_timeout`) |

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
| `ORS_API_KEY` | OpenRouteService key (geocoding + 2nd/3rd routing options), from the [HeiGIT dashboard](https://account.heigit.org/manage/key) |
| `ORS_BASE_URL` | OpenRouteService base address (default `https://api.heigit.org`) |
| `VALHALLA_URL` | First-choice routing server (default `https://valhalla1.openstreetmap.de`) |

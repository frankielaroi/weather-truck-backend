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
- **Valhalla:** Chicago → Denver returned 2 truck routes (1,016 and 1,079 mi, 15.7 h and 17.0 h) in 1.7 s on every try; maneuver times add up to the route's total time. Requests over 1,500 km straight-line are refused (Chicago → Los Angeles, or a via request whose legs add up past it), but each leg sent on its own works: 4 detour legs ran in parallel in 1.5 s.
- **ORS truck (`driving-hgv`), new address:** every request timed out at 20–37 s, including a 90-mile Chicago → Milwaukee trip, so alternative routes couldn't be checked. On the old address, the two requests that did answer took 55 s and 1.7 s (1,002 mi / 23.0 h direct; 1,111 mi / 26.0 h via a point to the south); their step times add up to the total exactly.
- **ORS car (`driving-car`), new address:** Chicago → Milwaukee, 92 mi in 1.0 s.
- **OSRM public demo:** 2 routes for Chicago → Denver, car profile only.

No provider returned 3 routes for a ~1,000-mile trip.

**Decisions**

- **Decision — routing order:** Valhalla (truck) first; if it fails, times out or refuses the request, ORS truck; if that fails too, ORS car. Each request has a 15 s timeout and the whole plan a 25 s budget. Valhalla goes first because it was fast and reliable on the day while ORS truck routing timed out on every distance. Each route records which provider and profile made it, and a route from the car profile carries a note that it isn't truck-specific.
- **Decision — third route:** when the provider returns fewer than 3 distinct routes, we make more by routing through a point to each side of the first route (B3's fallback), sent as separate legs and joined, with the same provider and profile so travel times compare fairly. Near-duplicates (more than 90% shared road) are dropped, so a trip may still end up with fewer than 3.
- **Decision — Valhalla's cap:** the public server refuses any request over 1,500 km straight-line (e.g. Chicago → Los Angeles). Such trips are split into sections under 1,490 km, routed in parallel and joined; the route carries a note saying so.
- **Decision — truck weight:** we send gross weight = load + 35,000 lb (typical empty tractor-trailer), capped at 80,000 lb.
- **Decision — rain:** rain = Open-Meteo `rain` + `showers` (all liquid precipitation in the hour). Snow = `snowfall` (inches of snow per hour).
- **Decision — wind:** the risk rules use `wind_speed_10m` (sustained); `wind_gusts_10m` is stored and shown only.
- OSRM was ruled out: car profile only, and its demo server isn't for production use.
## Checkpoints and arrival times

Weather is checked at mile 0, every 10 / 25 / 50 miles (the driver's choice) and at the destination. A regular checkpoint less than 1 mile before the destination is merged into it.

- **ETA** = departure + the time the route takes to reach that point, from the routing provider's per-section times, so slow city stretches and fast interstate stretches both count. It is not distance ÷ an average speed.
- **Decision — no breaks:** no rest stops or driving-hours breaks are added; arrival times assume continuous driving.
- **Decision — mile markers** are scaled so the path's length matches the provider's distance, and arrival at the destination is exactly departure + the route's total time.

## Weather at each checkpoint

- Each checkpoint is snapped to a ~1 km grid cell, so nearby points share a forecast. Cells without a fresh forecast are fetched from Open-Meteo in batches of 50 points per request (in parallel, 10 s timeout each) and cached in Postgres (`ForecastCache`) for 1 hour.
- Each checkpoint uses the forecast hour **nearest its arrival time**; exactly half past rounds up (14:40 → 15:00, 14:20 → 14:00, 14:30 → 15:00).
- We request SI units and convert once: m/s × 2.23694 → mph, mm ÷ 25.4 → in, cm of snow ÷ 2.54 → in, °C → °F.
- **Decision — beyond the forecast:** an arrival outside the forecast's hours is **Unknown** with the note "Beyond forecast range", never Low.
- **Decision — failures:** if a batch fails, its checkpoints use a cached forecast up to 6 hours old (with a note saying how old); otherwise they are Unknown with "Weather unavailable for this checkpoint". The trip still completes.
- Measured on Chicago → Denver (3 routes, 134 checkpoints at 25 mi): 3 requests and 2.8 s on a cold cache, 0 requests and 0.1 s when repeated within the hour.

## Planning a trip

`apps.routing.trips.create_trip()` runs routing → checkpoints → weather and risk → route scores and recommendation, then saves the trip, its routes and their checkpoints in **one transaction**. If any step fails, the trip is saved as **Failed** with the reason and nothing half-done is kept.

- **Decision — cached route plans:** a route plan is cached for 24 h per origin, destination and truck weight (routes don't depend on the departure time). With the 1-hour forecast cache, a repeated trip makes no provider calls.
- Each step's time is logged, e.g. `routing 3314 ms, checkpoints 34 ms, weather 1429 ms, save 217 ms`.
- Measured on Chicago → Denver at 25 mi: **5.0 s** on a cold cache, **0.28 s** warm; loading a saved trip takes 3 queries.

## Corridor heatmap

`apps.routing.heatmap.get_or_build_heatmap(trip)` covers the corridor around all of a trip's routes and scores it for every hour from 0 to 48. It's built on the first request and saved (`TripHeatmap`), so it's computed once.

- **Corridor:** every point within ~25 miles (40 km) of any route, covered with a square grid. The spacing is chosen so the heatmap stays under ~400 points (Chicago → Denver: 393 points, ~20-mile spacing). Every checkpoint's own forecast cell is included too, so **a point on a route at a checkpoint's arrival hour has exactly that checkpoint's level**.
- **Forecasts** come through the same 1-hour cache as the trip's checkpoints. Every point and hour is scored with the risk engine and the trip's load weight.
- **Decision — hour 0** is the hour the heatmap is built in (UTC), not the departure hour. `hours` gives each slot's clock time, and `departure_hour` marks the departure.
- **Truck position:** for each hour, on the recommended route. It's interpolated between checkpoints by time and placed along the road by distance; it sits at the origin before departure and at the destination after arrival.
- **Size:** Chicago → Denver is ~244 KB raw, **~13 KB gzipped**. A first build takes ~4 s (mostly weather); reading it back takes ~0.04 s with no provider calls.

Payload (compact: positions once, then one 49-value list per point per measure):

| Field | Type | Unit / values |
| --- | --- | --- |
| `version`, `trip_id` | int | |
| `start_time` | string | UTC time of hour 0 |
| `hours` | string[49] | UTC time of each hour slot |
| `departure_hour` | int \| null | Slot of the departure (null if outside 0–48) |
| `load_weight_lb` | int | pounds |
| `corridor_miles`, `spacing_miles` | number | miles |
| `points` | `[lng, lat][]` | degrees |
| `levels` | `(int \| null)[49][]` | Risk level 0–4 per point per hour; null = Unknown |
| `wind_mph` | `(int \| null)[49][]` | mph |
| `rain_in_per_hr`, `snow_in_per_hr` | `(number \| null)[49][]` | inches per hour (0.01) |
| `truck.route_index` | int | Which route the truck is on (the recommended one) |
| `truck.positions` | `[lng, lat][49]` | Truck position at each hour |

## API

Every error uses one shape: `{"error": {"code": "...", "message": "...", "fields": {...}}}`. `fields` appears only on 400s and maps each bad input to its messages; nested inputs use dotted names (`"origin.lat"`). No login is needed.

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

### `POST /api/trips/` — plan a trip

Validates the inputs, plans and scores the trip (routing → checkpoints → weather → risk → recommendation), saves it and returns it with **201** in the [trip response](#trip-response) shape. Takes about 5 s on a cold cache, under 1 s for a repeated trip.

```http
POST /api/trips/
Content-Type: application/json

{
  "origin": {"name": "Chicago, IL, USA", "lat": 41.8781, "lng": -87.6298},
  "destination": {"name": "Denver, CO, USA", "lat": 39.7392, "lng": -104.9903},
  "departure_at": "2026-10-20T14:00:00+00:00",
  "load_weight_lb": 32000,
  "interval_miles": 25
}
```

Input rules (each failure is a 400 with a message under its field):

| Field | Rule |
| --- | --- |
| `origin`, `destination` | `name` (1–255 characters), `lat` −90…90, `lng` −180…180; the two must be more than 50 m apart |
| `departure_at` | ISO 8601; no more than 1 hour in the past and no more than 48 hours ahead (**Decision:** beyond 48 h the forecast can't cover the trip). A time without a zone is read as UTC |
| `load_weight_lb` | whole number, 1–200,000 |
| `interval_miles` | 10, 25 or 50 |

| Status | When |
| --- | --- |
| 201 | Trip planned (full trip in the body) |
| 400 | Bad input (`invalid_input` with `fields`), or a body that isn't JSON (`parse_error`) |
| 422 | No road route between the places (`no_route`, "No route found between these places.") |
| 429 | Too many trips from one visitor (`throttled`; default 20 per minute, `TRIP_CREATE_RATE`) |
| 502 | A provider failed (`provider_unavailable`) |
| 504 | A provider timed out (`provider_timeout`) |

A trip that fails at 422 / 502 / 504 is still saved, as `failed` with its reason, and can be fetched.

### `GET /api/trips/<id>/` — a saved trip

Returns the same body as when the trip was created, so results can be reloaded or shared by link. Unknown id → **404** (`not_found`, "Trip not found.").

### `GET /api/trips/<id>/heatmap/` — corridor weather heatmap

The weather heatmap for hours 0–48 around all of the trip's routes, in the [heatmap payload](#corridor-heatmap) shape. It's built on the first request (about 1–4 s, mostly weather) and saved, so later requests read it back in well under a second with no provider calls. It's separate from the trip response so the trip can appear first and the heatmap load right after.

| Status | When |
| --- | --- |
| 200 | The heatmap |
| 404 | Unknown trip (`not_found`, "Trip not found.") |
| 409 | The trip failed and has no routes (`trip_has_no_routes`, "Trip has no routes.") |

**Compression:** responses are gzip-compressed when the client sends `Accept-Encoding: gzip` (responses under ~200 bytes are left alone). Chicago → Denver on the wire: heatmap **14 KB** (250 KB uncompressed), trip **28 KB** (113 KB uncompressed). Safe for this API because no response carries secrets or CSRF tokens.

### Trip response

The body for a trip (`GET` / `POST` trip endpoints, B10). A full real example is committed at [`docs/example-trip-response.json`](docs/example-trip-response.json); the frontend's types mirror it, and `apps/routing/tests/test_responses.py` checks every response against the same shape.

- **Risk levels** are sent as number and name: `{"value": 3, "name": "Severe"}`. Unknown is `{"value": null, "name": "Unknown"}` — never treated as Low.
- **Times** are ISO 8601 in UTC with an offset, e.g. `"2026-10-20T14:00:00+00:00"`. The frontend shows them in local time.
- **Rounding** happens only here, for display: miles to 0.1, speeds and °F to whole numbers, rain/snow to 0.01 in/hr, average risk to 0.01, coordinates to 5 decimals (~1 m). Stored values keep full precision.
- **Decision — simplified paths:** route paths are simplified to within 30 m of the road when the trip is saved (often 10,000 → ~1,000 points), so a 1,000-mile trip with 3 routes is ~110 KB (~27 KB gzipped). Checkpoints keep their exact positions.

**Trip**

| Field | Type | Unit / values |
| --- | --- | --- |
| `id` | int | |
| `status` | string | `"pending"`, `"ready"`, `"failed"` |
| `error` | string \| null | Why a failed trip failed |
| `created_at` | string | UTC time |
| `inputs.origin`, `inputs.destination` | object | `name` (string), `lat`, `lng` (degrees) |
| `inputs.departure_at` | string | UTC time |
| `inputs.load_weight_lb` | int | pounds |
| `inputs.interval_miles` | int | miles: 10, 25 or 50 |
| `recommended_route_index` | int \| null | `index` of the recommended route |
| `routes` | array | Route objects, in provider order (empty for a failed trip) |

**Route**

| Field | Type | Unit / values |
| --- | --- | --- |
| `index` | int | 0, 1, 2 |
| `letter` | string | `"A"`, `"B"`, `"C"` (the map's labels; reasons use them) |
| `label` | string | e.g. `"via I-80 W"` |
| `provider`, `profile` | string | e.g. `"valhalla"`, `"truck"` / `"car"` |
| `notes` | string[] | Caveats, e.g. "Not truck-specific: …" |
| `distance_miles` | number | miles (0.1) |
| `duration_seconds` | int | seconds |
| `arrival_at` | string | UTC time: departure + duration |
| `path` | GeoJSON LineString | `coordinates` are `[lng, lat]` pairs (degrees) |
| `risk.no_travel_miles`, `severe_miles`, `high_miles`, `moderate_miles`, `low_miles`, `unknown_miles` | number | miles (0.1); add up to `distance_miles` |
| `risk.worst_level` | level | Worst known level, or Unknown |
| `risk.average_risk` | number \| null | 0–4, mile-weighted, Unknown left out (0.01); null if nothing is known |
| `is_recommended` | bool | Exactly one route per ready trip |
| `recommendation_reason` | string \| null | On the recommended route, e.g. "Recommended: 12 fewer Severe miles than Route B." |
| `deciding_step` | string \| null | `no_travel_miles`, `severe_miles`, `high_miles`, `average_risk`, `travel_time`, `route_order`, `only_route` |
| `warnings` | string[] | e.g. "No safe route — consider delaying departure." |
| `checkpoints` | array | Checkpoint objects, from origin to destination |

**Checkpoint**

| Field | Type | Unit / values |
| --- | --- | --- |
| `sequence` | int | 0 at the origin |
| `mile` | number | miles from the origin (0.1) |
| `lat`, `lng` | number | degrees (5 decimals) |
| `arrival_at` | string | UTC time: the truck's ETA |
| `forecast_time` | string \| null | UTC time of the forecast hour used (nearest the ETA) |
| `weather.wind_mph`, `weather.gust_mph` | int \| null | mph (sustained wind sets the risk; gusts are shown only) |
| `weather.rain_in_per_hr`, `weather.snow_in_per_hr` | number \| null | inches per hour (0.01) |
| `weather.temperature_f` | int \| null | °F |
| `weather.description` | string \| null | e.g. "Light snow" |
| `risk.level` | level | |
| `risk.reasons` | string[] | Every factor at Moderate or above, e.g. "Wind 47 mph with 32,000 lb load → No Travel" |
| `risk.notes` | string[] | Data problems, e.g. "Beyond forecast range" |

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
| `Route` | Up to 3 options per trip: label, provider and profile, distance, duration, GeoJSON path, miles per risk level (incl. Unknown), average and worst risk, warnings, and the recommendation flag, reason and deciding step |
| `Checkpoint` | A point on a route: mile, position, ETA, the forecast hour used, wind / gust / rain / snow / temperature, risk level (null = Unknown), reasons and data notes |
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
| `TRIP_CREATE_RATE` | Trip creations allowed per visitor (default `20/min`) |
| `NUM_PROXIES` | Proxies in front of the app in production (e.g. `1`), so rate limits see the real client address |
| `VALHALLA_URL` | First-choice routing server (default `https://valhalla1.openstreetmap.de`) |

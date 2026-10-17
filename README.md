# Weather-Aware Truck Routing — API

Django 6 + Django REST framework API that plans truck routes, samples weather along each route at the truck's ETA, scores weather risk against load weight, and recommends the safest route.

Frontend: [weather-truck-frontend](https://github.com/frankielaroi/weather-truck-frontend)

## Running locally (http://localhost:8000)

```bash
python -m venv .venv
.venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add API keys
python manage.py migrate
python manage.py runserver
```

Check it: `GET http://localhost:8000/api/health/` → `{"status": "ok"}`

Tests: `python manage.py test`

## Environment variables

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key |
| `DJANGO_DEBUG` | `true` locally, `false` in production |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames |
| `CORS_ALLOWED_ORIGINS` | Frontend origin(s) allowed to call the API |
| `ORS_API_KEY` | OpenRouteService key (routing + geocoding) |

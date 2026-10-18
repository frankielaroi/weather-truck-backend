"""Weather at each checkpoint (MODULES.md B7): Open-Meteo hourly forecasts, cached per ~1 km grid cell.

1. Each checkpoint is snapped to a grid cell (ForecastCache.grid_key), so nearby points share one forecast.
2. Cells without a fresh cached forecast are fetched in batches of BATCH_SIZE points per request
   (requests run in parallel, REQUEST_TIMEOUT_SECONDS each) and saved to ForecastCache for FRESH_SECONDS.
3. Each checkpoint takes the forecast hour nearest its arrival time (half past rounds up).
4. Values are converted once, here, to mph / inches per hour / °F, and scored by the risk engine.

Decisions:
- Rain = Open-Meteo `rain` + `showers` (all liquid precipitation in the hour); snow = `snowfall`.
- An arrival outside the forecast's hours is Unknown with the note "Beyond forecast range" — never Low.
- If a batch fails, its checkpoints use a cached forecast up to STALE_SECONDS old if there is one
  (with a note saying how old), otherwise they are Unknown with a note. The trip still completes.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.routing.checkpoints import CheckpointPoint
from apps.routing.models import ForecastCache
from apps.routing.risk import RiskAssessment, WeatherSample, assess

logger = logging.getLogger(__name__)

PROVIDER: Final = "open-meteo"
BATCH_SIZE: Final = 50
REQUEST_TIMEOUT_SECONDS: Final = 10.0
FORECAST_DAYS: Final = 5  # from 00:00 UTC today: covers a departure 48 h ahead plus a long trip
FRESH_SECONDS: Final = 60 * 60
STALE_SECONDS: Final = 6 * 60 * 60
HOURLY_FIELDS: Final = (
    "wind_speed_10m",
    "wind_gusts_10m",
    "rain",
    "showers",
    "snowfall",
    "temperature_2m",
    "weather_code",
)

BEYOND_FORECAST_NOTE: Final = "Beyond forecast range"
UNAVAILABLE_NOTE: Final = "Weather unavailable for this checkpoint"

type Cell = tuple[float, float]  # (lat, lng) rounded to the cache grid

# --- Units (provider -> ours, converted once here) ------------------------------------------

MPH_PER_MS: Final = 2.2369362920544
MM_PER_INCH: Final = 25.4
CM_PER_INCH: Final = 2.54


def ms_to_mph(value: float | None) -> float | None:
    return None if value is None else value * MPH_PER_MS


def mm_to_inches(value: float | None) -> float | None:
    return None if value is None else value / MM_PER_INCH


def cm_to_inches(value: float | None) -> float | None:
    return None if value is None else value / CM_PER_INCH


def celsius_to_fahrenheit(value: float | None) -> float | None:
    return None if value is None else value * 9 / 5 + 32


# --- Results ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HourlyForecast:
    """One cell's forecast in our units. Lists are aligned with `times` (unix seconds, UTC hours)."""

    times: tuple[int, ...]
    wind_mph: tuple[float | None, ...]
    gust_mph: tuple[float | None, ...]
    rain_in_per_hr: tuple[float | None, ...]
    snow_in_per_hr: tuple[float | None, ...]
    temperature_f: tuple[float | None, ...]
    weather_code: tuple[int | None, ...]
    fetched_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "times": list(self.times),
            "wind_mph": list(self.wind_mph),
            "gust_mph": list(self.gust_mph),
            "rain_in_per_hr": list(self.rain_in_per_hr),
            "snow_in_per_hr": list(self.snow_in_per_hr),
            "temperature_f": list(self.temperature_f),
            "weather_code": list(self.weather_code),
        }

    @classmethod
    def from_json(cls, data: object, fetched_at: datetime) -> HourlyForecast | None:
        if not isinstance(data, dict):
            return None
        try:
            times = tuple(int(t) for t in data["times"])
            series = {k: tuple(_number(v) for v in data[k]) for k in _SERIES}
        except (KeyError, TypeError, ValueError):
            return None
        if any(len(values) != len(times) for values in series.values()):
            return None
        codes = tuple(None if c is None else int(c) for c in series.pop("weather_code"))
        return cls(times=times, weather_code=codes, fetched_at=fetched_at, **series)

    def hour_index(self, at: datetime) -> int | None:
        """Index of the forecast hour nearest `at` (half past rounds up); None if outside the forecast."""
        if not self.times:
            return None
        offset = (at.timestamp() - self.times[0]) / 3600
        index = math.floor(offset + 0.5)
        return index if 0 <= index < len(self.times) else None


_SERIES: Final = ("wind_mph", "gust_mph", "rain_in_per_hr", "snow_in_per_hr", "temperature_f", "weather_code")


@dataclass(frozen=True, slots=True)
class CheckpointWeather:
    """The weather and risk for one checkpoint at its arrival time."""

    forecast_time: datetime | None
    wind_mph: float | None
    gust_mph: float | None
    rain_in_per_hr: float | None
    snow_in_per_hr: float | None
    temperature_f: float | None
    description: str
    risk: RiskAssessment


# --- Public entry point ----------------------------------------------------------------------


def checkpoint_weather(points: Sequence[CheckpointPoint], load_weight_lb: float) -> list[CheckpointWeather]:
    """Weather and risk for each checkpoint, in order. Never raises for provider problems."""
    cells = [ForecastCache.grid_key(p.lat, p.lng) for p in points]
    forecasts, stale_notes = load_forecasts(set(cells))
    return [
        _weather_for(point, forecasts.get(cell), stale_notes.get(cell), load_weight_lb)
        for point, cell in zip(points, cells, strict=True)
    ]


def load_forecasts(cells: set[Cell]) -> tuple[dict[Cell, HourlyForecast], dict[Cell, str]]:
    """Forecasts for `cells`: fresh from the cache, else fetched; stale cache if fetching fails."""
    now = timezone.now()
    cached = _read_cache(cells, now - timedelta(seconds=STALE_SECONDS))
    fresh = {cell: f for cell, f in cached.items() if (now - f.fetched_at).total_seconds() < FRESH_SECONDS}
    missing = sorted(cells - fresh.keys())
    fetched = fetch_forecasts(missing) if missing else {}
    if fetched:
        _write_cache(fetched, now)

    forecasts = {**fresh, **fetched}
    notes: dict[Cell, str] = {}
    for cell in missing:
        if cell in fetched or cell not in cached:
            continue
        forecasts[cell] = cached[cell]
        age_hours = max(1, round((now - cached[cell].fetched_at).total_seconds() / 3600))
        notes[cell] = f"Using a forecast from about {age_hours} h ago (weather service unavailable)"
    return forecasts, notes


# --- Provider ----------------------------------------------------------------------------------


def fetch_forecasts(cells: Sequence[Cell]) -> dict[Cell, HourlyForecast]:
    """Fetch cells in parallel batches. Cells in a failed batch are simply absent from the result."""
    batches = [list(cells[i : i + BATCH_SIZE]) for i in range(0, len(cells), BATCH_SIZE)]
    with ThreadPoolExecutor(max_workers=min(4, len(batches)) or 1) as pool:
        results = list(pool.map(_safe_fetch_batch, batches))
    return {cell: forecast for batch in results for cell, forecast in batch.items()}


def _safe_fetch_batch(cells: list[Cell]) -> dict[Cell, HourlyForecast]:
    """A batch that fails in any way just leaves its cells out; it never fails the trip."""
    try:
        return _fetch_batch(cells)
    except Exception:
        logger.exception("Weather batch of %d cells failed unexpectedly", len(cells))
        return {}


def _fetch_batch(cells: list[Cell]) -> dict[Cell, HourlyForecast]:
    params: dict[str, str | int] = {
        "latitude": ",".join(f"{lat:.2f}" for lat, _ in cells),
        "longitude": ",".join(f"{lng:.2f}" for _, lng in cells),
        "hourly": ",".join(HOURLY_FIELDS),
        "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
        "temperature_unit": "celsius",
        "timeformat": "unixtime",
        "timezone": "GMT",
        "forecast_days": FORECAST_DAYS,
    }
    try:
        response = requests.get(settings.OPEN_METEO_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning("Weather batch of %d cells failed: %s", len(cells), exc)
        return {}
    if not response.ok:
        logger.warning("Weather batch of %d cells got HTTP %s: %.200s", len(cells), response.status_code, response.text)
        return {}
    try:
        data = response.json()
    except ValueError:
        logger.warning("Weather batch of %d cells returned non-JSON", len(cells))
        return {}

    locations = data if isinstance(data, list) else [data]
    if len(locations) != len(cells):
        logger.warning("Weather batch asked for %d cells, got %d", len(cells), len(locations))
        return {}
    fetched_at = timezone.now()
    out: dict[Cell, HourlyForecast] = {}
    for cell, location in zip(cells, locations, strict=True):
        forecast = parse_location(location, fetched_at)
        if forecast is not None:
            out[cell] = forecast
    return out


def parse_location(location: object, fetched_at: datetime) -> HourlyForecast | None:
    """One Open-Meteo location (SI units) -> HourlyForecast in our units. Malformed data -> None."""
    hourly = location.get("hourly") if isinstance(location, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        return None
    times = hourly["time"]
    try:
        series = {name: _aligned(hourly.get(name), len(times)) for name in HOURLY_FIELDS}
        hours = tuple(int(t) for t in times)
    except (TypeError, ValueError):
        return None
    rain = tuple(_add(r, s) for r, s in zip(series["rain"], series["showers"], strict=True))
    return HourlyForecast(
        times=hours,
        wind_mph=tuple(ms_to_mph(v) for v in series["wind_speed_10m"]),
        gust_mph=tuple(ms_to_mph(v) for v in series["wind_gusts_10m"]),
        rain_in_per_hr=tuple(mm_to_inches(v) for v in rain),
        snow_in_per_hr=tuple(cm_to_inches(v) for v in series["snowfall"]),
        temperature_f=tuple(celsius_to_fahrenheit(v) for v in series["temperature_2m"]),
        weather_code=tuple(None if v is None else int(v) for v in series["weather_code"]),
        fetched_at=fetched_at,
    )


# --- Cache -------------------------------------------------------------------------------------


def _read_cache(cells: Iterable[Cell], not_before: datetime) -> dict[Cell, HourlyForecast]:
    match = Q(pk__in=[])
    for lat, lng in cells:
        match |= Q(lat=_decimal(lat), lng=_decimal(lng))
    out: dict[Cell, HourlyForecast] = {}
    try:
        rows = ForecastCache.objects.filter(match, provider=PROVIDER, fetched_at__gte=not_before)
        for row in rows:
            forecast = HourlyForecast.from_json(row.hourly, row.fetched_at)
            if forecast is not None:
                out[(float(row.lat), float(row.lng))] = forecast
    except Exception:
        logger.warning("Forecast cache read failed", exc_info=True)
    return out


def _write_cache(forecasts: dict[Cell, HourlyForecast], now: datetime) -> None:
    rows = [
        ForecastCache(
            provider=PROVIDER,
            lat=_decimal(lat),
            lng=_decimal(lng),
            hourly=forecast.to_json(),
            fetched_at=forecast.fetched_at,
            expires_at=forecast.fetched_at + timedelta(seconds=FRESH_SECONDS),
        )
        for (lat, lng), forecast in forecasts.items()
    ]
    try:
        ForecastCache.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["provider", "lat", "lng"],
            update_fields=["hourly", "fetched_at", "expires_at"],
        )
    except Exception:
        logger.warning("Forecast cache write failed", exc_info=True)


# --- Checkpoint lookup -------------------------------------------------------------------------


def _weather_for(
    point: CheckpointPoint, forecast: HourlyForecast | None, stale_note: str | None, load_weight_lb: float
) -> CheckpointWeather:
    if forecast is None:
        return _unknown(UNAVAILABLE_NOTE)
    index = forecast.hour_index(point.eta)
    if index is None:
        return _unknown(BEYOND_FORECAST_NOTE)

    wind = forecast.wind_mph[index]
    rain = forecast.rain_in_per_hr[index]
    snow = forecast.snow_in_per_hr[index]
    risk = assess(WeatherSample(wind_mph=wind, rain_in_per_hr=rain, snow_in_per_hr=snow), load_weight_lb)
    if stale_note:
        risk = RiskAssessment(risk.level, risk.reasons, (*risk.data_issues, stale_note), risk.missing)
    return CheckpointWeather(
        forecast_time=datetime.fromtimestamp(forecast.times[index], tz=UTC),
        wind_mph=wind,
        gust_mph=forecast.gust_mph[index],
        rain_in_per_hr=rain,
        snow_in_per_hr=snow,
        temperature_f=forecast.temperature_f[index],
        description=describe_weather_code(forecast.weather_code[index]),
        risk=risk,
    )


def _unknown(note: str) -> CheckpointWeather:
    return CheckpointWeather(
        forecast_time=None,
        wind_mph=None,
        gust_mph=None,
        rain_in_per_hr=None,
        snow_in_per_hr=None,
        temperature_f=None,
        description="",
        risk=RiskAssessment(level=None, data_issues=(note,)),
    )


# WMO weather interpretation codes, as used by Open-Meteo.
_WEATHER_CODES: Final = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Light freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light rain showers",
    81: "Rain showers",
    82: "Violent rain showers",
    85: "Light snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with heavy hail",
}


def describe_weather_code(code: int | None) -> str:
    return _WEATHER_CODES.get(code, "") if code is not None else ""


# --- Helpers ---------------------------------------------------------------------------------


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _aligned(values: object, length: int) -> list[float | None]:
    """A provider series as numbers, padded with None if missing or short."""
    items = values if isinstance(values, list) else []
    numbers = [_number(v) for v in items[:length]]
    return numbers + [None] * (length - len(numbers))


def _add(a: float | None, b: float | None) -> float | None:
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)


def _decimal(value: float) -> Decimal:
    return Decimal(f"{value:.{ForecastCache.GRID_PRECISION}f}")

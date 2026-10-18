"""Corridor weather heatmap (MODULES.md B11): risk for every point around a trip's routes, hours 0–48.

1. The corridor is every point within CORRIDOR_KM (~25 miles) of any of the trip's routes.
2. It's covered with a square grid whose spacing is chosen so the whole heatmap stays under about
   MAX_POINTS points. Every checkpoint's own forecast cell is added too, so a point on a route at a
   checkpoint's arrival hour shows exactly that checkpoint's level.
3. Hourly forecasts come through the weather module's cache (B7), so they're shared with the trip.
4. Every point is scored for every hour with the risk engine (B1) and the trip's load weight.

Decision — hour 0 is the hour the heatmap is built in (UTC), not the departure hour; `hours` gives
each slot's clock time and `departure_hour` marks the departure. The truck's position on the
recommended route is given for every hour (at the origin before departure, at the destination after
arrival).

The payload is compact: positions once, then one list of 49 values per point for each of level,
wind, rain and snow. Unknown (no forecast for that place and hour) is null — never Low.
"""

from __future__ import annotations

import bisect
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.routing.errors import TripHasNoRoutes
from apps.routing.models import Checkpoint, ForecastCache, Route, Trip, TripHeatmap
from apps.routing.risk import WeatherSample, assess
from apps.routing.route_providers import LngLat, haversine_km
from apps.routing.weather import HourlyForecast, load_forecasts

logger = logging.getLogger(__name__)

HOURS: Final = 49  # hour 0 .. hour 48
CORRIDOR_KM: Final = 40.0  # ~25 miles each side
MAX_POINTS: Final = 400
MIN_GRID_POINTS: Final = 150  # the grid keeps at least this many even when checkpoints are many
MIN_SPACING_KM: Final = 8.0
MAX_SPACING_KM: Final = 50.0  # must stay under CORRIDOR_KM * sqrt(2) so the grid has no gaps along routes
_SAMPLE_KM: Final = 4.0
_PAYLOAD_VERSION: Final = 1

type Cell = tuple[float, float]  # (lat, lng) on the forecast cache grid


@dataclass(frozen=True, slots=True)
class Grid:
    points: list[LngLat]
    spacing_km: float


# --- Entry point ------------------------------------------------------------------------------------


def get_or_build_heatmap(trip: Trip, now: datetime | None = None) -> TripHeatmap:
    """The trip's saved heatmap, building and saving it on first request. Raises TripHasNoRoutes (409)."""
    existing = TripHeatmap.objects.filter(trip=trip).first()
    if existing is not None:
        return existing
    routes = sorted(trip.routes.all(), key=lambda r: r.index)
    if trip.status != Trip.Status.READY or not routes:
        raise TripHasNoRoutes()

    start = _hour_floor(now or timezone.now())
    data = build_heatmap_data(trip, routes, start)
    try:
        with transaction.atomic():
            return TripHeatmap.objects.create(trip=trip, start_time=start, data=data)
    except IntegrityError:  # another request built it at the same time; use theirs
        return TripHeatmap.objects.get(trip=trip)


def build_heatmap_data(trip: Trip, routes: Sequence[Route], start: datetime) -> dict[str, Any]:
    paths = [_path(route) for route in routes]
    checkpoints = [c for route in routes for c in sorted(route.checkpoints.all(), key=lambda c: c.sequence)]
    checkpoint_cells = list(dict.fromkeys(ForecastCache.grid_key(c.lat, c.lng) for c in checkpoints))
    grid = corridor_grid(paths, max(MIN_GRID_POINTS, MAX_POINTS - len(checkpoint_cells)))

    cells = list(dict.fromkeys([*(ForecastCache.grid_key(lat, lng) for lng, lat in grid.points), *checkpoint_cells]))
    forecasts, _stale = load_forecasts(set(cells))
    hours = [start + timedelta(hours=h) for h in range(HOURS)]

    levels: list[list[int | None]] = []
    wind: list[list[int | None]] = []
    rain: list[list[float | None]] = []
    snow: list[list[float | None]] = []
    for cell in cells:
        lv, wd, rn, sn = _score_point(forecasts.get(cell), hours, trip.load_weight_lb)
        levels.append(lv)
        wind.append(wd)
        rain.append(rn)
        snow.append(sn)

    recommended = next((r for r in routes if r.is_recommended), routes[0])
    departure_hour = round((trip.departure_at - start).total_seconds() / 3600)
    return {
        "version": _PAYLOAD_VERSION,
        "trip_id": trip.pk,
        "start_time": _iso(start),
        "hours": [_iso(h) for h in hours],
        "departure_hour": departure_hour if 0 <= departure_hour < HOURS else None,
        "load_weight_lb": trip.load_weight_lb,
        "corridor_miles": round(CORRIDOR_KM / 1.609344),
        "spacing_miles": round(grid.spacing_km / 1.609344, 1),
        "points": [[lng, lat] for lat, lng in cells],
        "levels": levels,
        "wind_mph": wind,
        "rain_in_per_hr": rain,
        "snow_in_per_hr": snow,
        "truck": {
            "route_index": recommended.index,
            "positions": truck_positions(recommended, _path(recommended), trip.departure_at, hours),
        },
    }


# --- Scoring --------------------------------------------------------------------------------------


def _score_point(
    forecast: HourlyForecast | None, hours: Sequence[datetime], load_weight_lb: float
) -> tuple[list[int | None], list[int | None], list[float | None], list[float | None]]:
    levels: list[int | None] = []
    wind: list[int | None] = []
    rain: list[float | None] = []
    snow: list[float | None] = []
    for hour in hours:
        index = forecast.hour_index(hour) if forecast is not None else None
        if forecast is None or index is None:
            levels.append(None)
            wind.append(None)
            rain.append(None)
            snow.append(None)
            continue
        w, r, s = forecast.wind_mph[index], forecast.rain_in_per_hr[index], forecast.snow_in_per_hr[index]
        level = assess(WeatherSample(wind_mph=w, rain_in_per_hr=r, snow_in_per_hr=s), load_weight_lb).level
        levels.append(None if level is None else int(level))
        wind.append(None if w is None else round(w))
        rain.append(None if r is None else round(r, 2))
        snow.append(None if s is None else round(s, 2))
    return levels, wind, rain, snow


# --- Corridor grid -----------------------------------------------------------------------------------


def corridor_grid(paths: Sequence[Sequence[LngLat]], budget: int) -> Grid:
    """Square grid points within CORRIDOR_KM of any path, spaced so there are at most `budget` of them."""
    samples = [p for path in paths for p in _sample_path(path, _SAMPLE_KM)]
    if not samples:
        return Grid([], MIN_SPACING_KM)
    projection = _Projection(sum(p[1] for p in samples) / len(samples))
    xy = [projection.to_xy(p) for p in samples]
    index = _SpatialIndex(xy, CORRIDOR_KM)

    spacing = MIN_SPACING_KM
    points = _grid_points(xy, index, spacing)
    if len(points) > budget:  # area scales with 1/spacing², so jump close, then nudge up
        spacing = min(MAX_SPACING_KM, spacing * math.sqrt(len(points) / budget))
        points = _grid_points(xy, index, spacing)
        while len(points) > budget and spacing < MAX_SPACING_KM:
            spacing = min(MAX_SPACING_KM, spacing * 1.05)
            points = _grid_points(xy, index, spacing)
    return Grid([projection.to_lnglat(p) for p in points], spacing)


def _grid_points(xy: Sequence[tuple[float, float]], index: _SpatialIndex, spacing: float) -> list[tuple[float, float]]:
    xs = [p[0] for p in xy]
    ys = [p[1] for p in xy]
    x0 = math.floor((min(xs) - CORRIDOR_KM) / spacing)
    x1 = math.ceil((max(xs) + CORRIDOR_KM) / spacing)
    y0 = math.floor((min(ys) - CORRIDOR_KM) / spacing)
    y1 = math.ceil((max(ys) + CORRIDOR_KM) / spacing)
    return [
        (i * spacing, j * spacing)
        for i in range(x0, x1 + 1)
        for j in range(y0, y1 + 1)
        if index.within((i * spacing, j * spacing))
    ]


class _SpatialIndex:
    """Answers "is any sample within `radius` km of this point?" using a hash of radius-sized cells."""

    def __init__(self, points: Sequence[tuple[float, float]], radius: float) -> None:
        self.radius = radius
        self.cells: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for p in points:
            self.cells.setdefault(self._key(p), []).append(p)

    def _key(self, p: tuple[float, float]) -> tuple[int, int]:
        return (math.floor(p[0] / self.radius), math.floor(p[1] / self.radius))

    def within(self, p: tuple[float, float]) -> bool:
        cx, cy = self._key(p)
        r2 = self.radius * self.radius
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in self.cells.get((cx + dx, cy + dy), ()):
                    if (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 <= r2:
                        return True
        return False


class _Projection:
    """Local equirectangular projection in km, good enough for a corridor a few hundred km wide."""

    def __init__(self, mid_lat: float) -> None:
        self.kx = 111.320 * math.cos(math.radians(mid_lat))
        self.ky = 110.574

    def to_xy(self, p: LngLat) -> tuple[float, float]:
        return (p[0] * self.kx, p[1] * self.ky)

    def to_lnglat(self, p: tuple[float, float]) -> LngLat:
        return (round(p[0] / self.kx, 4), round(p[1] / self.ky, 4))


def _sample_path(path: Sequence[LngLat], step_km: float) -> list[LngLat]:
    """Points along the path at most `step_km` apart (vertices plus interpolated points)."""
    if not path:
        return []
    out = [path[0]]
    for a, b in zip(path, path[1:], strict=False):
        pieces = max(1, math.ceil(haversine_km(a, b) / step_km))
        for k in range(1, pieces + 1):
            t = k / pieces
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


# --- Truck position -----------------------------------------------------------------------------------


def truck_positions(
    route: Route, path: Sequence[LngLat], departure: datetime, hours: Sequence[datetime]
) -> list[list[float]]:
    """Where the truck is on `route` at each hour: interpolated between checkpoints by time, then placed
    along the road path by distance. At the origin before departure, at the destination after arrival."""
    if not path:
        return []
    checkpoints: list[Checkpoint] = sorted(route.checkpoints.all(), key=lambda c: c.sequence)
    if len(checkpoints) < 2:
        return [[path[0][0], path[0][1]] for _ in hours]

    seconds = [(c.eta - departure).total_seconds() for c in checkpoints]
    miles = [c.mile_marker for c in checkpoints]
    cum_km = [0.0]
    for a, b in zip(path, path[1:], strict=False):
        cum_km.append(cum_km[-1] + haversine_km(a, b))
    scale = (route.distance_miles * 1.609344 / cum_km[-1]) if cum_km[-1] > 0 and route.distance_miles > 0 else 1.0

    out: list[list[float]] = []
    for hour in hours:
        t = (hour - departure).total_seconds()
        mile = _interpolate(seconds, miles, t)
        lng, lat = _point_at_km(path, cum_km, mile * 1.609344 / scale)
        out.append([round(lng, 5), round(lat, 5)])
    return out


def _interpolate(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)
    span = xs[i] - xs[i - 1]
    t = (x - xs[i - 1]) / span if span > 0 else 0.0
    return ys[i - 1] + (ys[i] - ys[i - 1]) * t


def _point_at_km(path: Sequence[LngLat], cum_km: Sequence[float], km: float) -> LngLat:
    if km <= 0:
        return path[0]
    if km >= cum_km[-1]:
        return path[-1]
    i = bisect.bisect_right(cum_km, km)
    span = cum_km[i] - cum_km[i - 1]
    t = (km - cum_km[i - 1]) / span if span > 0 else 0.0
    a, b = path[i - 1], path[i]
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


# --- Helpers -------------------------------------------------------------------------------------------


def _path(route: Route) -> list[LngLat]:
    coords = route.display_path or (route.geometry or {}).get("coordinates", [])
    return [(float(c[0]), float(c[1])) for c in coords if isinstance(c, list | tuple) and len(c) >= 2]


def _hour_floor(value: datetime) -> datetime:
    value = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return value.replace(minute=0, second=0, microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()

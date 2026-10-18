"""Route planning (MODULES.md B5): up to 3 really different truck routes between two places.

1. Ask providers in order until one answers (Decision — routing order):
     Valhalla truck (in sections, joined, when the trip is over its distance cap)
     → OpenRouteService truck
     → OpenRouteService car (marked "not truck-specific")
2. If that gives fewer than 3 distinct routes, route again through a point off to each side of the
   first route (B3's fallback), with the same provider and profile so travel times compare fairly.
   Detours that fail are skipped: fewer than 3 routes is allowed.
3. Drop near-duplicates (more than DUPLICATE_SHARE of the shorter route on the same road).
4. Label each route by its main road, e.g. "via I-80 W", unique within the trip.

Every request has a REQUEST_TIMEOUT_SECONDS timeout, and the whole plan a BUDGET_SECONDS budget so
a dead provider can't stall the trip. If no provider gives a route: NoRoute (422) when a provider
said there's no road route, otherwise ProviderTimeout (504) or ProviderUnavailable (502).
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Final

from apps.routing.errors import NoRoute, ProviderError, ProviderTimeout, ProviderUnavailable
from apps.routing.route_providers import (
    LngLat,
    NoRouteFound,
    OrsProfile,
    ProviderRefused,
    ProviderRoute,
    Section,
    haversine_km,
    ors_routes,
    valhalla_routes,
)

logger = logging.getLogger(__name__)

MAX_ROUTES: Final = 3
DUPLICATE_SHARE: Final = 0.9
REQUEST_TIMEOUT_SECONDS: Final = 15.0
BUDGET_SECONDS: Final = 25.0
MIN_REQUEST_SECONDS: Final = 2.0  # don't start a request with less time than this left
SAME_PLACE_KM: Final = 0.05

# Detour points sit this share of the trip's straight-line length off to each side of the first route.
DETOUR_SHARE: Final = 0.12
DETOUR_MIN_KM: Final = 8.0
DETOUR_MAX_KM: Final = 190.0

_SAMPLE_KM: Final = 0.5  # path sampling step for comparing routes
_CELL_DEGREES: Final = 0.01  # ~1 km grid used for "same road"

type Fetch = Callable[[Sequence[LngLat], int, float], list[ProviderRoute]]


@dataclass(frozen=True, slots=True)
class PlannedRoute:
    label: str
    path: tuple[LngLat, ...]  # [lng, lat] pairs
    distance_miles: float
    duration_seconds: float
    sections: tuple[Section, ...]
    provider: str
    profile: str
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Attempt:
    name: str
    fetch: Fetch


def plan_routes(origin: LngLat, destination: LngLat, load_weight_lb: float) -> list[PlannedRoute]:
    """Up to MAX_ROUTES distinct routes, best first as the provider ranked them, then detours."""
    if not _valid(origin) or not _valid(destination) or haversine_km(origin, destination) < SAME_PLACE_KM:
        raise NoRoute()

    deadline = _now() + BUDGET_SECONDS
    attempt, base = _first_answer(_attempts(load_weight_lb), origin, destination, deadline)
    routes = _drop_duplicates(base)
    if len(routes) < MAX_ROUTES:
        routes = _drop_duplicates(routes + _detours(attempt, routes[0], origin, destination, deadline))
    return label_routes(routes[:MAX_ROUTES])


# --- Provider order ----------------------------------------------------------------


def _attempts(load: float) -> list[_Attempt]:
    def valhalla(points: Sequence[LngLat], alternates: int, timeout: float) -> list[ProviderRoute]:
        return valhalla_routes(points, load, alternates=alternates, timeout=timeout)

    def ors(profile: OrsProfile, points: Sequence[LngLat], _alternates: int, timeout: float) -> list[ProviderRoute]:
        return ors_routes(points, load, profile=profile, timeout=timeout)

    return [
        _Attempt("Valhalla truck", valhalla),
        _Attempt("ORS truck", partial(ors, "driving-hgv")),
        _Attempt("ORS car", partial(ors, "driving-car")),
    ]


def _first_answer(
    attempts: Sequence[_Attempt], origin: LngLat, destination: LngLat, deadline: float
) -> tuple[_Attempt, list[ProviderRoute]]:
    no_route = timed_out = False
    for attempt in attempts:
        timeout = _time_left(deadline)
        if timeout is None:
            timed_out = True
            break
        try:
            routes = attempt.fetch([origin, destination], MAX_ROUTES - 1, timeout)
        except NoRouteFound as exc:
            logger.info("%s found no route: %s", attempt.name, exc)
            no_route = True
        except ProviderRefused as exc:
            logger.info("%s refused the request: %s", attempt.name, exc)
        except ProviderTimeout:
            logger.warning("%s timed out", attempt.name)
            timed_out = True
        except ProviderError as exc:
            logger.warning("%s failed: %s", attempt.name, exc)
        else:
            if routes:
                return attempt, routes
    if no_route:
        raise NoRoute()
    if timed_out:
        raise ProviderTimeout("The routing service took too long to respond. Try again in a moment.")
    raise ProviderUnavailable("The routing service isn't responding right now. Try again in a moment.")


def _now() -> float:
    return time.monotonic()


def _time_left(deadline: float) -> float | None:
    left = deadline - _now()
    return min(REQUEST_TIMEOUT_SECONDS, left) if left >= MIN_REQUEST_SECONDS else None


# --- Detours (B3's via-point fallback) ---------------------------------------------


def detour_points(route: ProviderRoute, origin: LngLat, destination: LngLat) -> list[LngLat]:
    """One point to each side of the route's midpoint, perpendicular to the overall trip direction."""
    mid_lng, mid_lat = point_at_fraction(route.path, 0.5)
    offset_km = min(max(haversine_km(origin, destination) * DETOUR_SHARE, DETOUR_MIN_KM), DETOUR_MAX_KM)
    km_per_deg_lat = 110.574
    km_per_deg_lng = 111.320 * math.cos(math.radians(mid_lat))
    east = (destination[0] - origin[0]) * km_per_deg_lng
    north = (destination[1] - origin[1]) * km_per_deg_lat
    length = math.hypot(east, north) or 1.0
    left = (-north / length, east / length)  # unit vector 90° counter-clockwise, in km space
    return [
        (mid_lng + side * left[0] * offset_km / km_per_deg_lng, mid_lat + side * left[1] * offset_km / km_per_deg_lat)
        for side in (1, -1)
    ]


def _detours(
    attempt: _Attempt, first: ProviderRoute, origin: LngLat, destination: LngLat, deadline: float
) -> list[ProviderRoute]:
    timeout = _time_left(deadline)
    if timeout is None:
        return []

    def one(via: LngLat) -> list[ProviderRoute]:
        try:
            return attempt.fetch([origin, via, destination], 0, timeout)[:1]
        except (NoRouteFound, ProviderRefused, ProviderError) as exc:
            logger.info("Detour via %s with %s failed: %s", via, attempt.name, exc)
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(one, detour_points(first, origin, destination)))
    return [route for found in results for route in found]


# --- Near-duplicates -----------------------------------------------------------------


def road_cells(path: Sequence[LngLat]) -> set[tuple[int, int]]:
    """~1 km grid cells the path passes through, sampled every _SAMPLE_KM."""
    cells: set[tuple[int, int]] = set()
    for a, b in zip(path, path[1:], strict=False):
        steps = max(1, math.ceil(haversine_km(a, b) / _SAMPLE_KM))
        for i in range(steps + 1):
            t = i / steps
            cells.add(_cell((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)))
    if len(path) == 1:
        cells.add(_cell(path[0]))
    return cells


def shared_share(a: set[tuple[int, int]], b: set[tuple[int, int]]) -> float:
    """Share of the smaller route's cells that the other route also covers."""
    smaller = min(len(a), len(b))
    return len(a & b) / smaller if smaller else 1.0


def _drop_duplicates(routes: Sequence[ProviderRoute]) -> list[ProviderRoute]:
    kept: list[tuple[ProviderRoute, set[tuple[int, int]]]] = []
    for route in routes:
        cells = road_cells(route.path)
        if all(shared_share(cells, other) <= DUPLICATE_SHARE for _, other in kept):
            kept.append((route, cells))
    return [route for route, _ in kept]


# --- Labels ----------------------------------------------------------------------------


def label_routes(routes: Sequence[ProviderRoute]) -> list[PlannedRoute]:
    """'via <main road>' using each route's longest road not already naming an earlier route."""
    used: set[str] = set()
    planned: list[PlannedRoute] = []
    for i, route in enumerate(routes):
        label = f"Route {chr(ord('A') + i)}"
        for road, _miles in sorted(route.road_miles.items(), key=lambda item: (-item[1], item[0])):
            pretty = pretty_road_name(road)
            key = _road_key(pretty)
            if key and key not in used:
                label = f"via {pretty}"
                used.add(key)
                break
        planned.append(
            PlannedRoute(
                label=label,
                path=route.path,
                distance_miles=route.distance_miles,
                duration_seconds=route.duration_seconds,
                sections=route.sections,
                provider=route.provider,
                profile=route.profile,
                notes=route.notes,
            )
        )
    return planned


_HIGHWAY = re.compile(r"\b(I|US|SR|CA|TX|IL|IA|NE|CO)\s+(\d+[A-Z]?)\b")
_INTERSTATE = re.compile(r"\bInterstate\s+(\d+)\b", re.IGNORECASE)
_DIRECTION = {"North": "N", "South": "S", "East": "E", "West": "W"}


def _road_key(pretty: str) -> str:
    """'US-41 N' and 'US-41' are the same road for labelling."""
    words = pretty.casefold().split(" ")
    return " ".join(words[:-1] if len(words) > 1 and words[-1] in {"n", "s", "e", "w"} else words)


def pretty_road_name(raw: str) -> str:
    """'I 80 West' -> 'I-80 W'; 'Interstate 76' -> 'I-76'. Long names are cut to 40 characters."""
    name = " ".join(raw.replace(";", ",").split(",")[0].split())
    name = _INTERSTATE.sub(r"I-\1", name)
    name = _HIGHWAY.sub(r"\1-\2", name)
    words = name.split(" ")
    if len(words) > 1 and words[-1] in _DIRECTION:
        words[-1] = _DIRECTION[words[-1]]
    return " ".join(words)[:40].strip()


# --- Geometry ------------------------------------------------------------------------------


def point_at_fraction(path: Sequence[LngLat], fraction: float) -> LngLat:
    """The point `fraction` of the way along the path, by distance."""
    lengths = [haversine_km(a, b) for a, b in zip(path, path[1:], strict=False)]
    target = sum(lengths) * min(max(fraction, 0.0), 1.0)
    walked = 0.0
    for (a, b), length in zip(zip(path, path[1:], strict=False), lengths, strict=True):
        if walked + length >= target and length > 0:
            t = (target - walked) / length
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        walked += length
    return path[-1]


def _cell(point: LngLat) -> tuple[int, int]:
    return (math.floor(point[0] / _CELL_DEGREES), math.floor(point[1] / _CELL_DEGREES))


def _valid(point: LngLat) -> bool:
    lng, lat = point
    return math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90

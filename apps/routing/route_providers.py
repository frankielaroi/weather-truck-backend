"""Routing provider clients (MODULES.md B5): Valhalla and OpenRouteService.

Each client turns one request into ProviderRoute objects in our units (miles, seconds,
[lng, lat] paths) and maps every failure onto one of four outcomes:

- NoRouteFound      the provider answered: there is no road route (e.g. a point in the ocean).
- ProviderRefused   the provider won't take this request (e.g. Valhalla's distance cap); try another.
- ProviderUnavailable / ProviderTimeout (from errors.py)  the provider failed or was too slow.

The planner (routes.py) decides what to do with those.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import requests
from django.conf import settings

from apps.routing.errors import ProviderTimeout, ProviderUnavailable

logger = logging.getLogger(__name__)

type LngLat = tuple[float, float]
type OrsProfile = Literal["driving-hgv", "driving-car"]

METERS_PER_MILE: Final = 1609.344
LB_PER_TONNE: Final = 2204.62

# Decision: providers want the truck's gross weight; the driver enters the load. We add a typical
# empty tractor-trailer and cap at the US federal gross limit, so heavy loads stay routable.
TARE_WEIGHT_LB: Final = 35_000
MAX_GROSS_WEIGHT_LB: Final = 80_000

# The public Valhalla server refuses requests longer than 1,500 km (straight line). Legs are kept
# under VALHALLA_MAX_LEG_KM; if the server still refuses, legs are halved once and retried.
VALHALLA_MAX_LEG_KM: Final = 1_490.0
VALHALLA_RETRY_LEG_KM: Final = 745.0

_VALHALLA_NO_ROUTE_CODES: Final = frozenset({170, 171, 442, 443})
_VALHALLA_REFUSED_CODES: Final = frozenset({154, 155, 156, 157, 158})
_ORS_NO_ROUTE_CODES: Final = frozenset({2009, 2010})
_ORS_REFUSED_CODES: Final = frozenset({2004})


class NoRouteFound(Exception):
    """The provider answered that no road route connects these points."""


class ProviderRefused(Exception):
    """The provider declined this request (limits, unsupported); another provider may accept it."""


@dataclass(frozen=True, slots=True)
class Section:
    """A stretch of the path (indices into ProviderRoute.path) with its own travel time."""

    start: int
    end: int
    duration_seconds: float
    distance_miles: float


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    path: tuple[LngLat, ...]
    distance_miles: float
    duration_seconds: float
    sections: tuple[Section, ...]
    road_miles: dict[str, float]  # named road -> miles driven on it, for labels
    provider: str
    profile: str
    notes: tuple[str, ...] = field(default_factory=tuple)


def gross_weight_tonnes(load_weight_lb: float) -> float:
    load = load_weight_lb if math.isfinite(load_weight_lb) and load_weight_lb > 0 else 0.0
    return round(min(load + TARE_WEIGHT_LB, MAX_GROSS_WEIGHT_LB) / LB_PER_TONNE, 2)


def haversine_km(a: LngLat, b: LngLat) -> float:
    lng1, lat1, lng2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


# --- HTTP ----------------------------------------------------------------------


def _post(url: str, body: dict[str, Any], timeout: float, headers: dict[str, str] | None = None) -> requests.Response:
    try:
        return requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        raise ProviderTimeout("The routing service took too long to respond. Try again in a moment.") from exc
    except requests.RequestException as exc:
        logger.warning("Routing request to %s failed: %s", url, exc)
        raise ProviderUnavailable("The routing service isn't responding right now. Try again in a moment.") from exc


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _unavailable(provider: str, response: requests.Response) -> ProviderUnavailable:
    logger.warning("%s returned HTTP %s: %.200s", provider, response.status_code, response.text)
    return ProviderUnavailable("The routing service isn't responding right now. Try again in a moment.")


# --- Valhalla ------------------------------------------------------------------


def valhalla_routes(
    waypoints: Sequence[LngLat], load_weight_lb: float, *, alternates: int, timeout: float
) -> list[ProviderRoute]:
    """Truck routes through `waypoints`. Legs longer than the server's cap are split into shorter
    requests, run in parallel and joined; provider alternatives only come from a single request."""
    try:
        return _valhalla_legs(waypoints, load_weight_lb, VALHALLA_MAX_LEG_KM, alternates=alternates, timeout=timeout)
    except ProviderRefused:
        logger.info("Valhalla refused the request; retrying in shorter sections")
        return _valhalla_legs(waypoints, load_weight_lb, VALHALLA_RETRY_LEG_KM, alternates=0, timeout=timeout)


def _valhalla_legs(
    waypoints: Sequence[LngLat], load_weight_lb: float, max_km: float, *, alternates: int, timeout: float
) -> list[ProviderRoute]:
    legs = split_long_legs(waypoints, max_km)
    if len(legs) == 2:
        return _valhalla_request(legs, load_weight_lb, alternates=alternates, timeout=timeout)

    pairs = list(zip(legs, legs[1:], strict=False))
    with ThreadPoolExecutor(max_workers=min(4, len(pairs))) as pool:
        parts = list(pool.map(lambda p: _valhalla_request(p, load_weight_lb, alternates=0, timeout=timeout)[0], pairs))
    joined = join_routes(parts)
    if len(legs) > len(waypoints):
        note = "Routed in sections because the trip is longer than the routing server handles in one request."
        joined = _with_note(joined, note)
    return [joined]


def _valhalla_request(
    points: Sequence[LngLat], load_weight_lb: float, *, alternates: int, timeout: float
) -> list[ProviderRoute]:
    body: dict[str, Any] = {
        "locations": [{"lat": lat, "lon": lng} for lng, lat in points],
        "costing": "truck",
        "costing_options": {"truck": {"weight": gross_weight_tonnes(load_weight_lb)}},
        "units": "miles",
        "directions_options": {"units": "miles"},
    }
    if alternates and len(points) == 2:
        body["alternates"] = alternates
    response = _post(f"{settings.VALHALLA_URL}/route", body, timeout)
    data = _json(response)

    if response.status_code == 400 and isinstance(data, dict):
        code = data.get("error_code")
        if code in _VALHALLA_NO_ROUTE_CODES:
            raise NoRouteFound(str(data.get("error", "No route")))
        if code in _VALHALLA_REFUSED_CODES:
            raise ProviderRefused(str(data.get("error", "Refused")))
    if not response.ok or not isinstance(data, dict):
        raise _unavailable("Valhalla", response)

    trips = [data.get("trip")] + [alt.get("trip") for alt in data.get("alternates", []) if isinstance(alt, dict)]
    routes = [route for trip in trips if (route := parse_valhalla_trip(trip)) is not None]
    if not routes:
        raise _unavailable("Valhalla", response)
    return routes


def parse_valhalla_trip(trip: object) -> ProviderRoute | None:
    if not isinstance(trip, dict) or not isinstance(trip.get("legs"), list):
        return None
    summary = trip.get("summary") or {}
    parts: list[ProviderRoute] = []
    for leg in trip["legs"]:
        if not isinstance(leg, dict) or not isinstance(leg.get("shape"), str):
            return None
        path = tuple((lng, lat) for lat, lng in decode_polyline(leg["shape"], precision=6))
        sections: list[Section] = []
        road_miles: dict[str, float] = {}
        for m in leg.get("maneuvers", []):
            start, end = int(m.get("begin_shape_index", 0)), int(m.get("end_shape_index", 0))
            duration, miles = float(m.get("time", 0.0)), float(m.get("length", 0.0))
            if end > start or duration > 0:
                sections.append(Section(start, end, duration, miles))
            names = m.get("street_names") or []
            if names and isinstance(names[0], str):
                road_miles[names[0]] = road_miles.get(names[0], 0.0) + miles
        leg_summary = leg.get("summary") or {}
        parts.append(
            ProviderRoute(
                path=path,
                distance_miles=float(leg_summary.get("length", sum(s.distance_miles for s in sections))),
                duration_seconds=float(leg_summary.get("time", sum(s.duration_seconds for s in sections))),
                sections=tuple(sections),
                road_miles=road_miles,
                provider="valhalla",
                profile="truck",
            )
        )
    if not parts or any(len(p.path) < 2 for p in parts):
        return None
    route = join_routes(parts)
    if "length" in summary and "time" in summary:
        route = _replace_totals(route, float(summary["length"]), float(summary["time"]))
    return route


# --- OpenRouteService ----------------------------------------------------------


def ors_routes(
    waypoints: Sequence[LngLat], load_weight_lb: float, *, profile: OrsProfile, timeout: float
) -> list[ProviderRoute]:
    if not settings.ORS_API_KEY:
        raise ProviderRefused("ORS_API_KEY is not set")
    body: dict[str, Any] = {
        "coordinates": [[lng, lat] for lng, lat in waypoints],
        "radiuses": [-1] * len(waypoints),
        "instructions": True,
    }
    if profile == "driving-hgv":
        body["options"] = {
            "vehicle_type": "hgv",
            "profile_params": {"restrictions": {"weight": gross_weight_tonnes(load_weight_lb)}},
        }
    url = f"{settings.ORS_BASE_URL}/openrouteservice/v2/directions/{profile}/geojson"
    response = _post(url, body, timeout, headers={"Authorization": settings.ORS_API_KEY})
    data = _json(response)

    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if code in _ORS_NO_ROUTE_CODES:
        raise NoRouteFound(str(error.get("message", "No route")) if isinstance(error, dict) else "No route")
    if code in _ORS_REFUSED_CODES:
        raise ProviderRefused(str(error.get("message", "Refused")) if isinstance(error, dict) else "Refused")
    if not response.ok or not isinstance(data, dict):
        raise _unavailable("OpenRouteService", response)

    routes = [r for f in data.get("features", []) if (r := parse_ors_feature(f, profile)) is not None]
    if not routes:
        raise _unavailable("OpenRouteService", response)
    return routes


def parse_ors_feature(feature: object, profile: OrsProfile) -> ProviderRoute | None:
    if not isinstance(feature, dict):
        return None
    geometry = feature.get("geometry") or {}
    properties = feature.get("properties") or {}
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(coords, list) or len(coords) < 2 or not isinstance(properties, dict):
        return None
    path = tuple((float(c[0]), float(c[1])) for c in coords)
    sections: list[Section] = []
    road_miles: dict[str, float] = {}
    for segment in properties.get("segments", []):
        for step in segment.get("steps", []):
            start, end = (int(i) for i in step.get("way_points", [0, 0])[:2])
            duration, miles = float(step.get("duration", 0.0)), float(step.get("distance", 0.0)) / METERS_PER_MILE
            if end > start or duration > 0:
                sections.append(Section(start, end, duration, miles))
            name = step.get("name")
            if isinstance(name, str) and name not in ("", "-"):
                road_miles[name] = road_miles.get(name, 0.0) + miles
    summary = properties.get("summary") or {}
    notes: tuple[str, ...] = ()
    if profile == "driving-car":
        notes = ("Not truck-specific: truck routing was unavailable, so this route uses a car profile.",)
    return ProviderRoute(
        path=path,
        distance_miles=float(summary.get("distance", 0.0)) / METERS_PER_MILE,
        duration_seconds=float(summary.get("duration", 0.0)),
        sections=tuple(sections),
        road_miles=road_miles,
        provider="openrouteservice",
        profile="truck" if profile == "driving-hgv" else "car",
        notes=notes,
    )


# --- Geometry helpers ------------------------------------------------------------


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Google/Valhalla encoded polyline -> [(lat, lng), ...]."""
    coords: list[tuple[float, float]] = []
    index = lat = lng = 0
    factor = 10**precision
    while index < len(encoded):
        deltas: list[int] = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        coords.append((lat / factor, lng / factor))
    return coords


def split_long_legs(waypoints: Sequence[LngLat], max_km: float) -> list[LngLat]:
    """Insert evenly spaced points so no straight-line leg is longer than max_km."""
    out: list[LngLat] = [waypoints[0]]
    for a, b in zip(waypoints, waypoints[1:], strict=False):
        pieces = max(1, math.ceil(haversine_km(a, b) / max_km))
        for i in range(1, pieces):
            t = i / pieces
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out.append(b)
    return out


def join_routes(parts: Sequence[ProviderRoute]) -> ProviderRoute:
    """Join consecutive legs into one route, re-indexing sections onto the joined path."""
    if len(parts) == 1:
        return parts[0]
    path: list[LngLat] = []
    sections: list[Section] = []
    road_miles: dict[str, float] = {}
    for part in parts:
        offset = len(path)
        if path and path[-1] == part.path[0]:
            offset -= 1
            path.extend(part.path[1:])
        else:
            path.extend(part.path)
        sections.extend(
            Section(s.start + offset, s.end + offset, s.duration_seconds, s.distance_miles) for s in part.sections
        )
        for road, miles in part.road_miles.items():
            road_miles[road] = road_miles.get(road, 0.0) + miles
    first = parts[0]
    return ProviderRoute(
        path=tuple(path),
        distance_miles=sum(p.distance_miles for p in parts),
        duration_seconds=sum(p.duration_seconds for p in parts),
        sections=tuple(sections),
        road_miles=road_miles,
        provider=first.provider,
        profile=first.profile,
        notes=tuple(dict.fromkeys(n for p in parts for n in p.notes)),
    )


def _replace_totals(route: ProviderRoute, distance_miles: float, duration_seconds: float) -> ProviderRoute:
    return ProviderRoute(
        route.path, distance_miles, duration_seconds, route.sections, route.road_miles,
        route.provider, route.profile, route.notes,
    )  # fmt: skip


def _with_note(route: ProviderRoute, note: str) -> ProviderRoute:
    return ProviderRoute(
        route.path, route.distance_miles, route.duration_seconds, route.sections, route.road_miles,
        route.provider, route.profile, (*route.notes, note),
    )  # fmt: skip

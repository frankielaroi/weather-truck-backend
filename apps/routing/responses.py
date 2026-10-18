"""What the API sends for a trip (MODULES.md B9). Field names, units and types are listed in the README;
the frontend's types mirror them, and `docs/example-trip-response.json` is a real example.

- Risk levels are sent as both number and name: {"value": 3, "name": "Severe"}; Unknown is
  {"value": null, "name": "Unknown"}.
- Paths are GeoJSON LineStrings ([lng, lat] pairs), which the map library reads directly.
- Numbers are rounded here, for display, and nowhere else: miles to 0.1, speeds and °F to whole
  numbers, rain/snow to 0.01 in/hr, average risk to 0.01, coordinates to 5 decimals (~1 m).
- Decision — simplified paths: the stored path (often 10,000+ points) is simplified to within 30 m of
  the road when the trip is saved (Route.display_path), so the trip loads fast; checkpoints keep
  exact positions. Rows saved before that column existed are simplified on the fly.
- Times are ISO 8601 in UTC with an offset ("2026-10-20T14:00:00+00:00").

Build responses from `Trip.objects.with_details()` so serializing a trip takes no extra queries.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeIs

from apps.routing.choices import RiskLevel
from apps.routing.geometry import COORD_DECIMALS, simplified_coordinates
from apps.routing.models import Checkpoint, Route, Trip

UNKNOWN: Final = "Unknown"

type Json = dict[str, Any]


def trip_response(trip: Trip) -> Json:
    routes = sorted(trip.routes.all(), key=lambda r: r.index)
    recommended = next((r.index for r in routes if r.is_recommended), None)
    return {
        "id": trip.pk,
        "status": trip.status,
        "error": trip.error_message or None,
        "created_at": _time(trip.created_at),
        "inputs": {
            "origin": _place(trip.origin_name, trip.origin_lat, trip.origin_lng),
            "destination": _place(trip.destination_name, trip.destination_lat, trip.destination_lng),
            "departure_at": _time(trip.departure_at),
            "load_weight_lb": trip.load_weight_lb,
            "interval_miles": trip.sample_interval_miles,
        },
        "recommended_route_index": recommended,
        "routes": [route_response(route, trip.departure_at) for route in routes],
    }


def route_response(route: Route, departure_at: datetime) -> Json:
    checkpoints = sorted(route.checkpoints.all(), key=lambda c: c.sequence)
    return {
        "index": route.index,
        "letter": chr(ord("A") + route.index),
        "label": route.label,
        "provider": route.provider,
        "profile": route.profile,
        "notes": list(route.notes),
        "distance_miles": _round(route.distance_miles, 1),
        "duration_seconds": route.duration_seconds,
        "arrival_at": _time(departure_at + timedelta(seconds=route.duration_seconds)),
        "path": {"type": "LineString", "coordinates": route.display_path or simplified_coordinates(route.geometry)},
        "risk": {
            "no_travel_miles": _round(route.no_travel_miles, 1),
            "severe_miles": _round(route.severe_miles, 1),
            "high_miles": _round(route.high_miles, 1),
            "moderate_miles": _round(route.moderate_miles, 1),
            "low_miles": _round(route.low_miles, 1),
            "unknown_miles": _round(route.unknown_miles, 1),
            "worst_level": risk_level(route.max_risk_level),
            "average_risk": _round(route.average_risk, 2),
        },
        "is_recommended": route.is_recommended,
        "recommendation_reason": route.recommendation_reason or None,
        "deciding_step": route.deciding_step or None,
        "warnings": list(route.warnings),
        "checkpoints": [checkpoint_response(c) for c in checkpoints],
    }


def checkpoint_response(checkpoint: Checkpoint) -> Json:
    return {
        "sequence": checkpoint.sequence,
        "mile": _round(checkpoint.mile_marker, 1),
        "lat": _round(checkpoint.lat, COORD_DECIMALS),
        "lng": _round(checkpoint.lng, COORD_DECIMALS),
        "arrival_at": _time(checkpoint.eta),
        "forecast_time": _time(checkpoint.forecast_time),
        "weather": {
            "wind_mph": _whole(checkpoint.wind_mph),
            "gust_mph": _whole(checkpoint.wind_gust_mph),
            "rain_in_per_hr": _round(checkpoint.rain_in_per_hr, 2),
            "snow_in_per_hr": _round(checkpoint.snow_in_per_hr, 2),
            "temperature_f": _whole(checkpoint.temperature_f),
            "description": checkpoint.weather_description or None,
        },
        "risk": {
            "level": risk_level(checkpoint.risk_level),
            "reasons": list(checkpoint.risk_reasons),
            "notes": list(checkpoint.data_notes),
        },
    }


def risk_level(value: int | None) -> Json:
    """{"value": 3, "name": "Severe"}; anything that isn't a known level is Unknown."""
    try:
        level = RiskLevel(value) if value is not None else None
    except ValueError:
        level = None
    if level is None:
        return {"value": None, "name": UNKNOWN}
    return {"value": int(level), "name": str(level.label)}


# --- Formatting ----------------------------------------------------------------------------------


def _place(name: str, lat: float, lng: float) -> Json:
    return {"name": name, "lat": _round(lat, COORD_DECIMALS), "lng": _round(lng, COORD_DECIMALS)}


def _time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if _finite(value) else None


def _whole(value: float | None) -> int | None:
    return round(value) if _finite(value) else None


def _finite(value: object) -> TypeIs[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)

"""Faked providers built from saved real responses, shared by the trip-level tests. No real calls."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any
from unittest import mock

import requests

from apps.routing.route_providers import ProviderRoute, join_routes, parse_valhalla_trip
from apps.routing.routes import PlannedRoute, label_routes
from apps.routing.weather import MPH_PER_MS

FIXTURES = Path(__file__).parent / "fixtures"
CHICAGO = (-87.6298, 41.8781)
DENVER = (-104.9903, 39.7392)
_WEATHER = json.loads((FIXTURES / "weather" / "open_meteo_3_points.json").read_text("utf-8"))


def valhalla_route(name: str, alternate: int | None = None) -> ProviderRoute:
    data = json.loads((FIXTURES / "routing" / f"{name}.json").read_text("utf-8"))
    route = parse_valhalla_trip(data["trip"] if alternate is None else data["alternates"][alternate]["trip"])
    assert route is not None
    return route


@cache
def chicago_denver_routes() -> list[PlannedRoute]:
    """The three Chicago -> Denver truck routes the planner gives: Valhalla's two, plus a southern detour."""
    south = join_routes([valhalla_route("valhalla_south_leg1"), valhalla_route("valhalla_south_leg2")])
    return label_routes(
        [valhalla_route("valhalla_chicago_denver"), valhalla_route("valhalla_chicago_denver", 0), south]
    )


def forecast_location(
    start: datetime, wind_mph: float = 7.0, wind_by_hour: Callable[[int], float] | None = None
) -> dict[str, Any]:
    """A real Open-Meteo location, re-timed to hourly from `start`, with a constant wind
    (or `wind_by_hour(hour_index)` mph, to make levels change over time)."""
    loc: dict[str, Any] = copy.deepcopy(_WEATHER[0])
    hours = len(loc["hourly"]["time"])
    first = int(start.timestamp()) // 3600 * 3600
    loc["hourly"]["time"] = [first + 3600 * i for i in range(hours)]
    winds = [wind_by_hour(i) if wind_by_hour else wind_mph for i in range(hours)]
    loc["hourly"]["wind_speed_10m"] = [w / MPH_PER_MS for w in winds]
    return loc


def weather_get(
    wind_mph: float = 7.0, start: datetime | None = None, wind_by_hour: Callable[[int], float] | None = None
) -> mock.Mock:
    """A fake `requests.get` for Open-Meteo: one location per requested point.
    Hours start at 00:00 UTC on `start`'s day (default: today), like the real API."""
    day = (start or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)

    def get(url: str, *, params: dict[str, Any], timeout: float) -> mock.Mock:
        count = len(str(params["latitude"]).split(","))
        res = mock.Mock(spec=requests.Response)
        res.status_code, res.ok, res.text = 200, True, ""
        res.json.return_value = [forecast_location(day, wind_mph, wind_by_hour) for _ in range(count)]
        return res

    return mock.Mock(side_effect=get)

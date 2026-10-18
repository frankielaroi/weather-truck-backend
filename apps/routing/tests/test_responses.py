"""B9 response shapes. TRIP_SCHEMA is the contract the frontend's types mirror; the committed example
(docs/example-trip-response.json, from a real trip) and freshly built responses must both match it."""

import copy
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import requests
from django.core.cache import cache
from pytest_django import DjangoAssertNumQueries

from apps.routing import trips
from apps.routing.choices import RiskLevel
from apps.routing.errors import NoRoute
from apps.routing.geometry import simplified_coordinates, simplify_path
from apps.routing.models import Route, Trip
from apps.routing.responses import risk_level, trip_response
from apps.routing.route_providers import join_routes, parse_valhalla_trip
from apps.routing.routes import PlannedRoute, label_routes
from apps.routing.trips import TripRequest, create_trip
from apps.routing.weather import MPH_PER_MS

BACKEND = Path(__file__).resolve().parents[3]
EXAMPLE = json.loads((BACKEND / "docs" / "example-trip-response.json").read_text("utf-8"))
FIXTURES = Path(__file__).parent / "fixtures"
WEATHER = json.loads((FIXTURES / "weather" / "open_meteo_3_points.json").read_text("utf-8"))
FORECAST_START = datetime.fromtimestamp(WEATHER[0]["hourly"]["time"][0], tz=UTC)

pytestmark = pytest.mark.django_db

# --- The contract ----------------------------------------------------------------------------------


class Nullable:
    def __init__(self, inner: Any) -> None:
        self.inner = inner


NUMBER = (int, float)
LEVEL = {"value": Nullable(int), "name": str}
PLACE = {"name": str, "lat": NUMBER, "lng": NUMBER}
CHECKPOINT = {
    "sequence": int,
    "mile": NUMBER,
    "lat": NUMBER,
    "lng": NUMBER,
    "arrival_at": str,
    "forecast_time": Nullable(str),
    "weather": {
        "wind_mph": Nullable(int),
        "gust_mph": Nullable(int),
        "rain_in_per_hr": Nullable(NUMBER),
        "snow_in_per_hr": Nullable(NUMBER),
        "temperature_f": Nullable(int),
        "description": Nullable(str),
    },
    "risk": {"level": LEVEL, "reasons": [str], "notes": [str]},
}
ROUTE = {
    "index": int,
    "letter": str,
    "label": str,
    "provider": str,
    "profile": str,
    "notes": [str],
    "distance_miles": NUMBER,
    "duration_seconds": int,
    "arrival_at": str,
    "path": {"type": str, "coordinates": [[NUMBER]]},
    "risk": {
        "no_travel_miles": NUMBER,
        "severe_miles": NUMBER,
        "high_miles": NUMBER,
        "moderate_miles": NUMBER,
        "low_miles": NUMBER,
        "unknown_miles": NUMBER,
        "worst_level": LEVEL,
        "average_risk": Nullable(NUMBER),
    },
    "is_recommended": bool,
    "recommendation_reason": Nullable(str),
    "deciding_step": Nullable(str),
    "warnings": [str],
    "checkpoints": [CHECKPOINT],
}
TRIP_SCHEMA = {
    "id": int,
    "status": str,
    "error": Nullable(str),
    "created_at": str,
    "inputs": {
        "origin": PLACE,
        "destination": PLACE,
        "departure_at": str,
        "load_weight_lb": int,
        "interval_miles": int,
    },
    "recommended_route_index": Nullable(int),
    "routes": [ROUTE],
}


def check(value: Any, spec: Any, where: str = "trip") -> None:
    """Assert `value` has exactly the shape `spec` describes (no missing or extra keys)."""
    if isinstance(spec, Nullable):
        if value is not None:
            check(value, spec.inner, where)
        return
    if isinstance(spec, dict):
        assert isinstance(value, dict), f"{where}: expected object, got {type(value).__name__}"
        assert set(value) == set(spec), f"{where}: keys differ: {set(value) ^ set(spec)}"
        for key, inner in spec.items():
            check(value[key], inner, f"{where}.{key}")
        return
    if isinstance(spec, list):
        assert isinstance(value, list), f"{where}: expected array, got {type(value).__name__}"
        for i, item in enumerate(value):
            check(item, spec[0], f"{where}[{i}]")
        return
    types = spec if isinstance(spec, tuple) else (spec,)
    assert isinstance(value, types) and not (bool in (type(value),) and bool not in types), (
        f"{where}: expected {spec}, got {value!r}"
    )


# --- Building real-shaped trips with faked providers ---------------------------------------------


def _valhalla(name: str, alternate: int | None = None) -> Any:
    data = json.loads((FIXTURES / "routing" / f"{name}.json").read_text("utf-8"))
    route = parse_valhalla_trip(data["trip"] if alternate is None else data["alternates"][alternate]["trip"])
    assert route is not None
    return route


@pytest.fixture(scope="module")
def three_routes() -> list[PlannedRoute]:
    south = join_routes([_valhalla("valhalla_south_leg1"), _valhalla("valhalla_south_leg2")])
    return label_routes([_valhalla("valhalla_chicago_denver"), _valhalla("valhalla_chicago_denver", 0), south])


def weather(wind_mph: float) -> mock.Mock:
    def get(url: str, *, params: dict[str, Any], timeout: float) -> mock.Mock:
        loc = copy.deepcopy(WEATHER[0])
        loc["hourly"]["wind_speed_10m"] = [wind_mph / MPH_PER_MS] * len(loc["hourly"]["time"])
        res = mock.Mock(spec=requests.Response)
        res.status_code, res.ok, res.text = 200, True, ""
        res.json.return_value = [copy.deepcopy(loc) for _ in str(params["latitude"]).split(",")]
        return res

    return mock.Mock(side_effect=get)


def request(hours_ahead: float = 2) -> TripRequest:
    return TripRequest(
        "Chicago, IL, USA",
        (-87.6298, 41.8781),
        "Denver, CO, USA",
        (-104.9903, 39.7392),
        FORECAST_START + timedelta(hours=hours_ahead),
        32_000,
        25,
    )


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def make_trip(three_routes: list[PlannedRoute], wind_mph: float = 40, hours_ahead: float = 2) -> Trip:
    with (
        mock.patch.object(trips, "plan_routes", return_value=three_routes),
        mock.patch("requests.get", weather(wind_mph)),
    ):
        trip = create_trip(request(hours_ahead))
    return Trip.objects.with_details().get(pk=trip.pk)


# --- Acceptance criteria ---------------------------------------------------------------------------


def test_the_committed_example_matches_the_contract() -> None:
    check(EXAMPLE, TRIP_SCHEMA)
    assert EXAMPLE["status"] == "ready"
    assert len(EXAMPLE["routes"]) == 3


def test_a_built_trip_response_matches_the_contract(three_routes: list[PlannedRoute]) -> None:
    check(trip_response(make_trip(three_routes)), TRIP_SCHEMA)


def test_a_failed_trip_response_matches_the_contract() -> None:
    with mock.patch.object(trips, "plan_routes", side_effect=NoRoute()), pytest.raises(NoRoute):
        create_trip(request())
    body = trip_response(Trip.objects.with_details().get())
    check(body, TRIP_SCHEMA)
    assert (body["status"], body["error"], body["routes"], body["recommended_route_index"]) == (
        "failed",
        "No route found between these places.",
        [],
        None,
    )


# --- Content ---------------------------------------------------------------------------------------------


def test_serializing_a_loaded_trip_needs_no_more_queries(
    three_routes: list[PlannedRoute], django_assert_max_num_queries: DjangoAssertNumQueries
) -> None:
    trip_id = make_trip(three_routes).pk
    with django_assert_max_num_queries(3):
        trip_response(Trip.objects.with_details().get(pk=trip_id))


def test_levels_are_sent_as_number_and_name(three_routes: list[PlannedRoute]) -> None:
    body = trip_response(make_trip(three_routes, wind_mph=40))  # 40 mph with 32,000 lb -> High
    first = body["routes"][0]["checkpoints"][0]
    assert first["risk"]["level"] == {"value": 2, "name": "High"}
    assert first["risk"]["reasons"] == ["Wind 40 mph → High"]
    assert body["routes"][0]["risk"]["worst_level"] == {"value": 2, "name": "High"}


def test_unknown_checkpoints_say_so_with_their_note(three_routes: list[PlannedRoute]) -> None:
    body = trip_response(make_trip(three_routes, hours_ahead=24 * 30))  # far beyond the forecast
    route = body["routes"][0]
    assert {c["risk"]["level"]["name"] for c in route["checkpoints"]} == {"Unknown"}
    assert route["checkpoints"][0]["risk"]["level"] == {"value": None, "name": "Unknown"}
    assert route["checkpoints"][0]["risk"]["notes"] == ["Beyond forecast range"]
    assert route["checkpoints"][0]["weather"]["wind_mph"] is None
    assert route["risk"]["worst_level"] == {"value": None, "name": "Unknown"}
    assert route["risk"]["average_risk"] is None


def test_numbers_are_rounded_for_display(three_routes: list[PlannedRoute]) -> None:
    body = trip_response(make_trip(three_routes))
    for route in body["routes"]:
        assert route["distance_miles"] == round(route["distance_miles"], 1)
        assert all(math.isclose(v, round(v, 1)) for k, v in route["risk"].items() if k.endswith("_miles"))
        for c in route["checkpoints"]:
            assert c["mile"] == round(c["mile"], 1)
            assert c["lat"] == round(c["lat"], 5) and c["lng"] == round(c["lng"], 5)
            assert isinstance(c["weather"]["wind_mph"], int)
            if c["weather"]["rain_in_per_hr"] is not None:
                assert c["weather"]["rain_in_per_hr"] == round(c["weather"]["rain_in_per_hr"], 2)
        assert all(lng == round(lng, 5) and lat == round(lat, 5) for lng, lat in route["path"]["coordinates"])


def test_rounding_happens_only_in_the_response(three_routes: list[PlannedRoute]) -> None:
    trip = make_trip(three_routes)
    route = trip.routes.all()[0]
    assert route.distance_miles != round(route.distance_miles, 1)  # stored at full precision


def test_paths_are_geojson_and_simplified_but_close_to_the_road(three_routes: list[PlannedRoute]) -> None:
    trip = make_trip(three_routes)
    body = trip_response(trip)
    for route_body, stored in zip(body["routes"], sorted(trip.routes.all(), key=lambda r: r.index), strict=True):
        coords = route_body["path"]["coordinates"]
        full = stored.geometry["coordinates"]
        assert route_body["path"]["type"] == "LineString"
        assert 100 < len(coords) < len(full) / 4
        assert coords[0] == [round(v, 5) for v in full[0]] and coords[-1] == [round(v, 5) for v in full[-1]]


def test_times_are_utc_iso_8601_with_offset(three_routes: list[PlannedRoute]) -> None:
    body = trip_response(make_trip(three_routes))
    route = body["routes"][0]
    for value in (body["created_at"], body["inputs"]["departure_at"], route["arrival_at"]):
        assert value.endswith("+00:00")
        datetime.fromisoformat(value)
    departure = datetime.fromisoformat(body["inputs"]["departure_at"])
    assert datetime.fromisoformat(route["arrival_at"]) == departure + timedelta(seconds=route["duration_seconds"])


def test_the_recommended_route_is_marked_and_explained(three_routes: list[PlannedRoute]) -> None:
    body = trip_response(make_trip(three_routes))
    index = body["recommended_route_index"]
    recommended = body["routes"][index]
    assert recommended["is_recommended"]
    assert recommended["recommendation_reason"].startswith("Recommended:")
    others = [r for r in body["routes"] if r["index"] != index]
    assert all(r["recommendation_reason"] is None and r["deciding_step"] is None for r in others)
    assert [r["letter"] for r in body["routes"]] == ["A", "B", "C"]


def test_rows_saved_without_a_display_path_are_simplified_on_the_fly(three_routes: list[PlannedRoute]) -> None:
    trip = make_trip(three_routes)
    Route.objects.update(display_path=[])
    body = trip_response(Trip.objects.with_details().get(pk=trip.pk))
    assert len(body["routes"][0]["path"]["coordinates"]) > 100


# --- Helpers -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [(0, "Low"), (4, "No Travel"), (None, "Unknown"), (9, "Unknown")])
def test_risk_level_names(value: int | None, expected: str) -> None:
    assert risk_level(value)["name"] == expected


def test_simplify_keeps_the_ends_and_stays_within_tolerance() -> None:
    zigzag = [(i * 0.001, (0.0001 if i % 2 else 0.0)) for i in range(2001)]  # ~11 m wiggles
    kept = simplify_path(zigzag, 30)
    assert kept[0] == zigzag[0] and kept[-1] == zigzag[-1]
    assert len(kept) < 10
    sharp = [(0.0, 0.0), (0.01, 0.01), (0.02, 0.0)]  # ~1.1 km corner
    assert simplify_path(sharp, 30) == sharp


def test_every_dropped_point_is_within_tolerance_of_the_simplified_line() -> None:
    path = _valhalla("valhalla_north_leg1").path
    kept = simplify_path(path, 30)
    assert len(kept) < len(path) / 4
    kx = 111_320.0 * math.cos(math.radians(sum(p[1] for p in path) / len(path)))
    ky = 110_574.0

    def metres(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        px, py, ax, ay, bx, by = p[0] * kx, p[1] * ky, a[0] * kx, a[1] * ky, b[0] * kx, b[1] * ky
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    # Each original point lies between two kept points; it must be within 30 m of that segment.
    kept_set = set(kept)
    kept_at = [i for i, p in enumerate(path) if p in kept_set]
    for start, end in zip(kept_at, kept_at[1:], strict=False):
        for i in range(start + 1, end):
            assert metres(path[i], path[start], path[end]) <= 30.0 + 1e-6


@pytest.mark.parametrize(
    "geometry", [None, {}, {"coordinates": "x"}, {"coordinates": [[1], ["a", 2], [float("nan"), 1]]}]
)
def test_bad_geometry_gives_an_empty_path(geometry: Any) -> None:
    assert simplified_coordinates(geometry) == []


def test_short_paths_are_kept_as_is() -> None:
    assert simplified_coordinates({"coordinates": [[1.123456, 2.0], [3.0, 4.0]]}) == [[1.12346, 2.0], [3.0, 4.0]]
    assert simplify_path([(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)], 30) == [(0.0, 0.0), (0.0, 0.0)]


def test_naive_times_are_sent_as_utc() -> None:
    from apps.routing.responses import _time

    assert _time(datetime(2026, 10, 20, 14, 0)) == "2026-10-20T14:00:00+00:00"
    assert _time(None) is None


def test_levels_cover_every_risk_level() -> None:
    assert [risk_level(int(level))["name"] for level in RiskLevel] == ["Low", "Moderate", "High", "Severe", "No Travel"]

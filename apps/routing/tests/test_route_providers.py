"""B5 provider clients, against saved real Valhalla / OpenRouteService responses. No real calls."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import requests
from pytest_django.fixtures import Settings

from apps.routing import route_providers as rp
from apps.routing.errors import ProviderTimeout, ProviderUnavailable
from apps.routing.route_providers import (
    NoRouteFound,
    ProviderRefused,
    ProviderRoute,
    Section,
    decode_polyline,
    gross_weight_tonnes,
    join_routes,
    ors_routes,
    parse_valhalla_trip,
    split_long_legs,
    valhalla_routes,
)

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
CHICAGO = (-87.6298, 41.8781)
DENVER = (-104.9903, 39.7392)
MILWAUKEE = (-87.9065, 43.0389)


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))


def response(status: int, body: Any) -> mock.Mock:
    res = mock.Mock(spec=requests.Response)
    res.status_code = status
    res.ok = 200 <= status < 400
    res.text = json.dumps(body) if not isinstance(body, Exception) else "<html>"
    if isinstance(body, Exception):
        res.json.side_effect = body
    else:
        res.json.return_value = body
    return res


@pytest.fixture(autouse=True)
def configured(settings: Settings) -> None:
    settings.VALHALLA_URL = "https://valhalla.test"
    settings.ORS_BASE_URL = "https://ors.test"
    settings.ORS_API_KEY = "test-key"


@pytest.fixture
def post() -> Iterator[mock.Mock]:
    with mock.patch("requests.post") as fake:
        yield fake


def assert_sections_add_up(route: ProviderRoute) -> None:
    total = sum(s.duration_seconds for s in route.sections)
    assert total == pytest.approx(route.duration_seconds, rel=0.01)
    assert all(0 <= s.start <= s.end < len(route.path) for s in route.sections)


# --- Valhalla ------------------------------------------------------------------------


def test_valhalla_chicago_denver_parses_two_truck_routes(post: mock.Mock) -> None:
    post.return_value = response(200, load("valhalla_chicago_denver"))
    routes = valhalla_routes([CHICAGO, DENVER], 32_000, alternates=2, timeout=15)
    assert len(routes) == 2
    first = routes[0]
    assert first.distance_miles == pytest.approx(1016.4, abs=0.1)  # miles, not km
    assert first.duration_seconds == pytest.approx(56672.8, abs=1)  # seconds
    assert first.path[0] == pytest.approx(CHICAGO, abs=0.01)  # [lng, lat]
    assert first.path[-1] == pytest.approx(DENVER, abs=0.05)
    assert (first.provider, first.profile, first.notes) == ("valhalla", "truck", ())
    for route in routes:
        assert_sections_add_up(route)


def test_valhalla_request_uses_truck_profile_weight_and_timeout(post: mock.Mock) -> None:
    post.return_value = response(200, load("valhalla_chicago_denver"))
    valhalla_routes([CHICAGO, DENVER], 32_000, alternates=2, timeout=7.5)
    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url == "https://valhalla.test/route"
    assert kwargs["timeout"] == 7.5
    body = kwargs["json"]
    assert body["costing"] == "truck"
    assert body["costing_options"]["truck"]["weight"] == gross_weight_tonnes(32_000)
    assert body["alternates"] == 2
    assert body["locations"][0] == {"lat": CHICAGO[1], "lon": CHICAGO[0]}


def test_valhalla_via_point_is_two_requests_joined(post: mock.Mock) -> None:
    post.side_effect = [response(200, load("valhalla_north_leg1")), response(200, load("valhalla_north_leg2"))]
    via = (-96.68, 42.53)
    routes = valhalla_routes([CHICAGO, via, DENVER], 32_000, alternates=0, timeout=15)
    assert len(routes) == 1
    route = routes[0]
    leg1 = parse_valhalla_trip(load("valhalla_north_leg1")["trip"])
    leg2 = parse_valhalla_trip(load("valhalla_north_leg2")["trip"])
    assert leg1 is not None and leg2 is not None
    assert route.distance_miles == pytest.approx(leg1.distance_miles + leg2.distance_miles)
    assert route.notes == ()
    assert_sections_add_up(route)
    assert all("alternates" not in call.kwargs["json"] for call in post.call_args_list)


def test_valhalla_no_route_codes_raise_no_route_found(post: mock.Mock) -> None:
    post.return_value = response(400, load("valhalla_no_route"))
    with pytest.raises(NoRouteFound):
        valhalla_routes([CHICAGO, (-50.0, 35.0)], 1, alternates=2, timeout=15)


def test_distance_cap_refusal_retries_in_shorter_sections(post: mock.Mock) -> None:
    leg = load("valhalla_north_leg1")
    post.side_effect = [response(400, load("valhalla_too_far"))] + [response(200, leg)] * 4
    routes = valhalla_routes([CHICAGO, DENVER], 1, alternates=2, timeout=15)
    assert post.call_count >= 3  # one refused request, then at least two shorter ones
    assert routes[0].notes and "in sections" in routes[0].notes[0]


def test_long_trips_are_split_before_asking(post: mock.Mock) -> None:
    post.return_value = response(200, load("valhalla_north_leg1"))
    los_angeles = (-118.2437, 34.0522)
    valhalla_routes([CHICAGO, los_angeles], 1, alternates=2, timeout=15)
    assert post.call_count == 2
    assert all(len(call.kwargs["json"]["locations"]) == 2 for call in post.call_args_list)


@pytest.mark.parametrize(
    "res",
    [response(500, {"error": "boom"}), response(429, {}), response(200, ValueError("html")), response(200, {})],
)
def test_valhalla_failures_are_provider_unavailable(post: mock.Mock, res: mock.Mock) -> None:
    post.return_value = res
    with pytest.raises(ProviderUnavailable):
        valhalla_routes([CHICAGO, DENVER], 1, alternates=2, timeout=15)


def test_valhalla_unknown_400_is_provider_unavailable(post: mock.Mock) -> None:
    post.return_value = response(400, {"error_code": 999, "error": "Something new"})
    with pytest.raises(ProviderUnavailable):
        valhalla_routes([CHICAGO, DENVER], 1, alternates=2, timeout=15)


def test_valhalla_trip_without_summary_uses_leg_totals() -> None:
    trip = load("valhalla_north_leg1")["trip"]
    leg_summary = trip["legs"][0]["summary"]
    del trip["summary"]
    route = parse_valhalla_trip(trip)
    assert route is not None
    assert route.distance_miles == pytest.approx(leg_summary["length"])
    assert route.duration_seconds == pytest.approx(leg_summary["time"])


def test_valhalla_timeout_is_provider_timeout(post: mock.Mock) -> None:
    post.side_effect = requests.Timeout()
    with pytest.raises(ProviderTimeout):
        valhalla_routes([CHICAGO, DENVER], 1, alternates=2, timeout=15)


def test_valhalla_connection_error_is_provider_unavailable(post: mock.Mock) -> None:
    post.side_effect = requests.ConnectionError()
    with pytest.raises(ProviderUnavailable):
        valhalla_routes([CHICAGO, DENVER], 1, alternates=2, timeout=15)


@pytest.mark.parametrize(
    "trip",
    [None, {}, {"legs": "x"}, {"legs": [{"shape": 5}]}, {"legs": ["x"]}, {"legs": [{"shape": "_p~iF~ps|U"}]}],
)
def test_malformed_valhalla_trips_are_skipped(trip: Any) -> None:
    assert parse_valhalla_trip(trip) is None


# --- OpenRouteService ---------------------------------------------------------------


def test_ors_truck_route_parses_in_our_units(post: mock.Mock) -> None:
    post.return_value = response(200, load("ors_hgv_chicago_milwaukee"))
    [route] = ors_routes([CHICAGO, MILWAUKEE], 32_000, profile="driving-hgv", timeout=15)
    assert route.distance_miles == pytest.approx(143165.2 / 1609.344, abs=0.1)
    assert route.path[0] == pytest.approx(CHICAGO, abs=0.01)
    assert (route.provider, route.profile, route.notes) == ("openrouteservice", "truck", ())
    assert_sections_add_up(route)
    body = post.call_args.kwargs["json"]
    assert body["options"]["profile_params"]["restrictions"]["weight"] == gross_weight_tonnes(32_000)
    assert post.call_args.args[0] == "https://ors.test/openrouteservice/v2/directions/driving-hgv/geojson"
    assert post.call_args.kwargs["headers"] == {"Authorization": "test-key"}


def test_ors_car_route_is_marked_not_truck_specific(post: mock.Mock) -> None:
    post.return_value = response(200, load("ors_car_chicago_milwaukee"))
    [route] = ors_routes([CHICAGO, MILWAUKEE], 32_000, profile="driving-car", timeout=15)
    assert route.profile == "car"
    assert route.notes and route.notes[0].startswith("Not truck-specific")
    assert "options" not in post.call_args.kwargs["json"]


def test_ors_no_route_raises_no_route_found(post: mock.Mock) -> None:
    post.return_value = response(404, load("ors_car_no_route"))
    with pytest.raises(NoRouteFound):
        ors_routes([CHICAGO, MILWAUKEE], 1, profile="driving-car", timeout=15)


def test_ors_limit_error_is_a_refusal(post: mock.Mock) -> None:
    post.return_value = response(400, {"error": {"code": 2004, "message": "Request parameters exceed limits"}})
    with pytest.raises(ProviderRefused):
        ors_routes([CHICAGO, MILWAUKEE], 1, profile="driving-hgv", timeout=15)


@pytest.mark.parametrize("res", [response(401, {}), response(502, ValueError("html")), response(200, {"features": []})])
def test_ors_failures_are_provider_unavailable(post: mock.Mock, res: mock.Mock) -> None:
    post.return_value = res
    with pytest.raises(ProviderUnavailable):
        ors_routes([CHICAGO, MILWAUKEE], 1, profile="driving-hgv", timeout=15)


def test_ors_without_key_is_a_refusal_and_makes_no_call(settings: Settings, post: mock.Mock) -> None:
    settings.ORS_API_KEY = ""
    with pytest.raises(ProviderRefused):
        ors_routes([CHICAGO, MILWAUKEE], 1, profile="driving-hgv", timeout=15)
    post.assert_not_called()


@pytest.mark.parametrize("feature", [None, {}, {"geometry": {"coordinates": [[1, 2]]}, "properties": {}}])
def test_malformed_ors_features_are_skipped(feature: Any) -> None:
    assert rp.parse_ors_feature(feature, "driving-hgv") is None


def test_odd_road_names_are_ignored_for_labels() -> None:
    trip = load("valhalla_north_leg1")["trip"]
    for m in trip["legs"][0]["maneuvers"]:
        m["street_names"] = [42]
    route = parse_valhalla_trip(trip)
    assert route is not None
    assert route.road_miles == {}
    feature = load("ors_hgv_chicago_milwaukee")["features"][0]
    for step in feature["properties"]["segments"][0]["steps"]:
        step["name"] = None
    ors = rp.parse_ors_feature(feature, "driving-hgv")
    assert ors is not None
    assert ors.road_miles == {}


# --- Helpers -------------------------------------------------------------------------------


def test_decode_polyline_matches_the_reference_example() -> None:
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert points == pytest.approx([(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])


@pytest.mark.parametrize(
    ("load", "expected_lb"),
    [(32_000, 67_000), (0, 35_000), (200_000, 80_000), (float("nan"), 35_000), (-5, 35_000)],
)
def test_gross_weight_adds_tare_and_caps_at_the_federal_limit(load: float, expected_lb: float) -> None:
    assert gross_weight_tonnes(load) == pytest.approx(expected_lb / 2204.62, abs=0.01)


def test_split_long_legs_keeps_every_leg_under_the_limit() -> None:
    los_angeles = (-118.2437, 34.0522)
    points = split_long_legs([CHICAGO, los_angeles], 1_000)
    assert points[0] == CHICAGO and points[-1] == los_angeles
    assert all(rp.haversine_km(a, b) <= 1_000 for a, b in zip(points, points[1:], strict=False))
    assert split_long_legs([CHICAGO, MILWAUKEE], 1_000) == [CHICAGO, MILWAUKEE]


def test_join_routes_reindexes_sections_and_merges_the_shared_point() -> None:
    a = ProviderRoute(((0, 0), (1, 0)), 1, 10, (Section(0, 1, 10, 1),), {"A": 1}, "p", "truck")
    b = ProviderRoute(((1, 0), (2, 0), (3, 0)), 2, 20, (Section(0, 2, 20, 2),), {"A": 1, "B": 1}, "p", "truck", ("n",))
    joined = join_routes([a, b])
    assert joined.path == ((0, 0), (1, 0), (2, 0), (3, 0))
    assert joined.sections == (Section(0, 1, 10, 1), Section(1, 3, 20, 2))
    assert (joined.distance_miles, joined.duration_seconds) == (3, 30)
    assert joined.road_miles == {"A": 2, "B": 1}
    assert joined.notes == ("n",)


def test_join_routes_keeps_both_points_when_legs_do_not_touch() -> None:
    a = ProviderRoute(((0, 0), (1, 0)), 1, 10, (), {}, "p", "truck")
    b = ProviderRoute(((1.1, 0), (2, 0)), 1, 10, (Section(0, 1, 10, 1),), {}, "p", "truck")
    joined = join_routes([a, b])
    assert len(joined.path) == 4
    assert joined.sections == (Section(2, 3, 10, 1),)

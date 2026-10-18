"""B5 route planning: provider order, detours, near-duplicates, labels, errors. No real calls."""

import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import requests
from pytest_django.fixtures import Settings

from apps.routing import routes as planner
from apps.routing.errors import NoRoute, ProviderTimeout, ProviderUnavailable
from apps.routing.route_providers import (
    LngLat,
    NoRouteFound,
    ProviderRefused,
    ProviderRoute,
    Section,
    haversine_km,
    parse_ors_feature,
    parse_valhalla_trip,
)
from apps.routing.routes import (
    PlannedRoute,
    detour_points,
    label_routes,
    plan_routes,
    point_at_fraction,
    pretty_road_name,
    road_cells,
    shared_share,
)

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
CHICAGO: LngLat = (-87.6298, 41.8781)
DENVER: LngLat = (-104.9903, 39.7392)
MILWAUKEE: LngLat = (-87.9065, 43.0389)


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))


def valhalla_trip(name: str, alternate: int | None = None) -> ProviderRoute:
    data = load(name)
    trip = data["trip"] if alternate is None else data["alternates"][alternate]["trip"]
    route = parse_valhalla_trip(trip)
    assert route is not None
    return route


def ors_route(name: str, profile: str = "driving-hgv") -> ProviderRoute:
    route = parse_ors_feature(load(name)["features"][0], profile)  # type: ignore[arg-type]
    assert route is not None
    return route


def line_route(
    points: Sequence[LngLat], provider: str = "stub", roads: dict[str, float] | None = None
) -> ProviderRoute:
    return ProviderRoute(
        tuple(points), 100, 3600, (Section(0, len(points) - 1, 3600, 100),), roads or {}, provider, "truck"
    )


@pytest.fixture(autouse=True)
def configured(settings: Settings) -> None:
    settings.VALHALLA_URL = "https://valhalla.test"
    settings.ORS_BASE_URL = "https://ors.test"
    settings.ORS_API_KEY = "test-key"


# --- Chicago -> Denver through the faked HTTP layer (saved real responses) -------------


def fake_valhalla_http(url: str, *, json: dict[str, Any], **_: Any) -> mock.Mock:
    locations = json["locations"]
    if len(locations) == 2 and json.get("alternates"):
        body = load("valhalla_chicago_denver")
    else:
        first, second = ((loc["lat"], loc["lon"]) for loc in locations)
        starts_at_origin = abs(first[0] - CHICAGO[1]) < 0.01
        via_lat = second[0] if starts_at_origin else first[0]
        side = "north" if via_lat > 40.8 else "south"
        body = load(f"valhalla_{side}_leg{1 if starts_at_origin else 2}")
    res = mock.Mock(spec=requests.Response)
    res.status_code, res.ok, res.text = 200, True, ""
    res.json.return_value = body
    return res


@pytest.fixture
def chicago_denver() -> list[PlannedRoute]:
    with mock.patch("requests.post", side_effect=fake_valhalla_http):
        return plan_routes(CHICAGO, DENVER, 32_000)


def test_chicago_denver_gives_three_routes_with_different_labels_and_paths(chicago_denver: list[PlannedRoute]) -> None:
    assert len(chicago_denver) == 3
    assert len({r.label for r in chicago_denver}) == 3
    cells = [road_cells(r.path) for r in chicago_denver]
    for i in range(3):
        for j in range(i + 1, 3):
            assert shared_share(cells[i], cells[j]) <= planner.DUPLICATE_SHARE


def test_units_are_miles_seconds_and_lng_lat(chicago_denver: list[PlannedRoute]) -> None:
    for route in chicago_denver:
        assert 900 < route.distance_miles < 1300
        assert 12 * 3600 < route.duration_seconds < 24 * 3600
        assert route.path[0] == pytest.approx(CHICAGO, abs=0.05)
        assert route.path[-1] == pytest.approx(DENVER, abs=0.05)
        assert all(-180 <= lng <= 180 and -90 <= lat <= 90 for lng, lat in route.path)


def test_section_times_add_up_to_the_total(chicago_denver: list[PlannedRoute]) -> None:
    for route in chicago_denver:
        assert sum(s.duration_seconds for s in route.sections) == pytest.approx(route.duration_seconds, rel=0.01)


def test_all_routes_come_from_valhalla_truck(chicago_denver: list[PlannedRoute]) -> None:
    assert {(r.provider, r.profile) for r in chicago_denver} == {("valhalla", "truck")}


def test_provider_routes_come_first_then_detours(chicago_denver: list[PlannedRoute]) -> None:
    base = valhalla_trip("valhalla_chicago_denver")
    assert chicago_denver[0].distance_miles == pytest.approx(base.distance_miles)


# --- Provider order -----------------------------------------------------------------------

type Fetcher = Callable[..., list[ProviderRoute]]


@pytest.fixture
def providers() -> Iterator[dict[str, mock.Mock]]:
    with (
        mock.patch.object(planner, "valhalla_routes") as valhalla,
        mock.patch.object(planner, "ors_routes") as ors,
    ):
        yield {"valhalla": valhalla, "ors": ors}


def ors_by_profile(**by_profile: Any) -> Fetcher:
    def fetch(points: Sequence[LngLat], load: float, *, profile: str, timeout: float) -> list[ProviderRoute]:
        outcome = by_profile[profile.replace("driving-", "")]
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)

    return fetch


def test_valhalla_failure_falls_back_to_ors_truck(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].side_effect = ProviderUnavailable()
    providers["ors"].side_effect = ors_by_profile(
        hgv=[ors_route("ors_hgv_chicago_milwaukee")], car=ProviderUnavailable()
    )
    routes = plan_routes(CHICAGO, MILWAUKEE, 32_000)
    assert routes[0].provider == "openrouteservice"
    assert routes[0].profile == "truck"


def test_truck_routing_down_falls_back_to_ors_car_with_note(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].side_effect = ProviderTimeout()
    car = ors_route("ors_car_chicago_milwaukee", "driving-car")
    providers["ors"].side_effect = ors_by_profile(hgv=ProviderTimeout(), car=[car])
    routes = plan_routes(CHICAGO, MILWAUKEE, 32_000)
    assert routes[0].profile == "car"
    assert routes[0].notes[0].startswith("Not truck-specific")


def test_detours_use_the_same_provider_as_the_first_route(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].side_effect = ProviderRefused("cap")
    seen: list[str] = []

    def fetch(points: Sequence[LngLat], load: float, *, profile: str, timeout: float) -> list[ProviderRoute]:
        seen.append(profile)
        if profile == "driving-hgv":
            return [line_route(points)]
        raise AssertionError("car profile should not be used")

    providers["ors"].side_effect = fetch
    plan_routes(CHICAGO, MILWAUKEE, 32_000)
    assert seen == ["driving-hgv"] * 3  # base + two detours


def test_unroutable_trip_is_no_route(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].side_effect = NoRouteFound("ocean")
    providers["ors"].side_effect = NoRouteFound("ocean")
    with pytest.raises(NoRoute) as exc:
        plan_routes(CHICAGO, (-50.0, 35.0), 1)
    assert exc.value.status_code == 422
    assert str(exc.value.detail) == "No route found between these places."


def test_no_route_wins_over_other_failures(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].side_effect = NoRouteFound("ocean")
    providers["ors"].side_effect = ProviderTimeout()
    with pytest.raises(NoRoute):
        plan_routes(CHICAGO, (-50.0, 35.0), 1)


def test_all_providers_down_is_502(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].side_effect = ProviderUnavailable()
    providers["ors"].side_effect = ProviderRefused("no key")
    with pytest.raises(ProviderUnavailable) as exc:
        plan_routes(CHICAGO, MILWAUKEE, 1)
    assert exc.value.status_code == 502


def test_slow_providers_are_504(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].side_effect = ProviderTimeout()
    providers["ors"].side_effect = ProviderUnavailable()
    with pytest.raises(ProviderTimeout) as exc:
        plan_routes(CHICAGO, MILWAUKEE, 1)
    assert exc.value.status_code == 504


def test_empty_answer_moves_to_the_next_provider(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].return_value = []
    providers["ors"].side_effect = ors_by_profile(hgv=[line_route([CHICAGO, MILWAUKEE])], car=[])
    assert len(plan_routes(CHICAGO, MILWAUKEE, 1)) >= 1


def test_budget_stops_trying_more_providers(providers: dict[str, mock.Mock]) -> None:
    clock = iter([0.0, planner.BUDGET_SECONDS, planner.BUDGET_SECONDS + 1])
    providers["valhalla"].side_effect = ProviderTimeout()
    with mock.patch.object(planner, "_now", side_effect=lambda: next(clock)), pytest.raises(ProviderTimeout):
        plan_routes(CHICAGO, MILWAUKEE, 1)
    providers["ors"].assert_not_called()


def test_detours_are_skipped_when_the_budget_is_nearly_spent(providers: dict[str, mock.Mock]) -> None:
    clock = iter([0.0, 0.0, planner.BUDGET_SECONDS - 1])
    providers["valhalla"].return_value = [line_route([CHICAGO, MILWAUKEE])]
    with mock.patch.object(planner, "_now", side_effect=lambda: next(clock)):
        routes = plan_routes(CHICAGO, MILWAUKEE, 1)
    assert len(routes) == 1
    assert providers["valhalla"].call_count == 1


def test_request_timeout_never_exceeds_the_per_request_limit(providers: dict[str, mock.Mock]) -> None:
    providers["valhalla"].return_value = [line_route([CHICAGO, MILWAUKEE])]
    plan_routes(CHICAGO, MILWAUKEE, 1)
    assert all(
        call.kwargs["timeout"] <= planner.REQUEST_TIMEOUT_SECONDS for call in providers["valhalla"].call_args_list
    )


@pytest.mark.parametrize(
    ("origin", "destination"),
    [(CHICAGO, CHICAGO), (CHICAGO, (-87.6298, 41.87811)), ((float("nan"), 41.0), DENVER), (CHICAGO, (200.0, 0.0))],
)
def test_same_or_invalid_places_are_no_route_without_calls(
    providers: dict[str, mock.Mock], origin: LngLat, destination: LngLat
) -> None:
    with pytest.raises(NoRoute):
        plan_routes(origin, destination, 1)
    providers["valhalla"].assert_not_called()


# --- Detours and near-duplicates -----------------------------------------------------------


def test_failed_detours_leave_fewer_routes(providers: dict[str, mock.Mock]) -> None:
    def valhalla(points: Sequence[LngLat], load: float, *, alternates: int, timeout: float) -> list[ProviderRoute]:
        if len(points) == 2:
            return [line_route(points)]
        raise ProviderUnavailable()

    providers["valhalla"].side_effect = valhalla
    routes = plan_routes(CHICAGO, MILWAUKEE, 1)
    assert len(routes) == 1


def test_near_duplicate_routes_are_dropped(providers: dict[str, mock.Mock]) -> None:
    base = line_route([CHICAGO, MILWAUKEE])
    providers["valhalla"].side_effect = lambda points, load, **_: [base, base] if len(points) == 2 else [base]
    assert len(plan_routes(CHICAGO, MILWAUKEE, 1)) == 1


def test_provider_alternatives_skip_detours_when_already_three(providers: dict[str, mock.Mock]) -> None:
    west, mid, east = (-88.5, 42.5), (-87.9, 42.5), (-87.3, 42.5)
    three = [line_route([CHICAGO, p, MILWAUKEE]) for p in (west, mid, east)]
    providers["valhalla"].return_value = three
    assert len(plan_routes(CHICAGO, MILWAUKEE, 1)) == 3
    assert providers["valhalla"].call_count == 1


def test_detour_points_sit_on_opposite_sides_at_the_right_distance() -> None:
    route = valhalla_trip("valhalla_chicago_denver")
    left, right = detour_points(route, CHICAGO, DENVER)
    mid = point_at_fraction(route.path, 0.5)
    expected = haversine_km(CHICAGO, DENVER) * planner.DETOUR_SHARE
    assert haversine_km(left, mid) == pytest.approx(expected, rel=0.05)
    assert haversine_km(right, mid) == pytest.approx(expected, rel=0.05)
    assert left[1] < mid[1] < right[1] or right[1] < mid[1] < left[1]  # trip runs east-west: one north, one south


def test_short_trips_still_get_a_minimum_detour() -> None:
    route = line_route([CHICAGO, MILWAUKEE])
    left, _ = detour_points(route, CHICAGO, MILWAUKEE)
    assert haversine_km(left, point_at_fraction(route.path, 0.5)) >= planner.DETOUR_MIN_KM * 0.95


def test_shared_share_of_identical_and_disjoint_paths() -> None:
    a = road_cells([CHICAGO, MILWAUKEE])
    b = road_cells([DENVER, (-104.0, 40.5)])
    assert shared_share(a, a) == 1.0
    assert shared_share(a, b) == 0.0
    assert shared_share(set(), a) == 1.0


def test_single_point_path_has_one_cell() -> None:
    assert len(road_cells([CHICAGO])) == 1


def test_point_at_fraction_ends_and_middle() -> None:
    path = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    assert point_at_fraction(path, 0) == (0.0, 0.0)
    assert point_at_fraction(path, 0.5) == pytest.approx((1.0, 0.0))
    assert point_at_fraction(path, 1) == (2.0, 0.0)
    assert point_at_fraction([(5.0, 5.0)], 0.5) == (5.0, 5.0)


# --- Labels -----------------------------------------------------------------------------------


def test_labels_use_each_routes_main_road_without_repeats() -> None:
    a = line_route([CHICAGO, DENVER], roads={"I 80 West": 600, "I 76 West": 200})
    b = line_route([CHICAGO, DENVER], roads={"I 80 West": 500, "I 70 West": 300})
    c = line_route([CHICAGO, DENVER], roads={"I 80": 400})
    assert [r.label for r in label_routes([a, b, c])] == ["via I-80 W", "via I-70 W", "Route C"]


def test_road_direction_does_not_make_a_label_unique() -> None:
    a = line_route([CHICAGO, MILWAUKEE], roads={"US 41 North": 50})
    b = line_route([CHICAGO, MILWAUKEE], roads={"US 41": 40, "I 94 West": 30})
    assert [r.label for r in label_routes([a, b])] == ["via US-41 N", "via I-94 W"]


@pytest.mark.parametrize(
    ("raw", "pretty"),
    [
        ("I 80 West", "I-80 W"),
        ("Interstate 76", "I-76"),
        ("US 34; US 6", "US-34"),
        ("I 80, I 94", "I-80"),
        ("South Federal Street", "South Federal Street"),
        ("North", "North"),
        ("A" * 60, "A" * 40),
    ],
)
def test_pretty_road_names(raw: str, pretty: str) -> None:
    assert pretty_road_name(raw) == pretty

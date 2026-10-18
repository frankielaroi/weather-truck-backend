"""B6 checkpoint sampler — see MODULES.md for the acceptance criteria each test maps to."""

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from apps.routing.checkpoints import CheckpointPoint, checkpoint_miles, sample_checkpoints
from apps.routing.route_providers import LngLat, Section, haversine_km, parse_valhalla_trip

DEPARTURE = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
KM_PER_MILE = 1.609344
FIXTURES = Path(__file__).parent / "fixtures" / "routing"


@dataclass(frozen=True)
class FakeRoute:
    path: tuple[LngLat, ...]
    sections: tuple[Section, ...]
    distance_miles: float
    duration_seconds: float


def straight_route(
    miles: float, hours: float = 2.0, points: int = 11, sections: tuple[Section, ...] | None = None
) -> FakeRoute:
    """A route due east along the equator, `points` vertices, one section covering it all."""
    degrees = miles * KM_PER_MILE / 111.195  # ~km per degree of longitude at the equator
    path = tuple((degrees * i / (points - 1), 0.0) for i in range(points))
    real_miles = haversine_km(path[0], path[-1]) / KM_PER_MILE
    if sections is None:
        sections = (Section(0, points - 1, hours * 3600, real_miles),)
    return FakeRoute(path, sections, real_miles, hours * 3600)


def miles_of(points: list[CheckpointPoint]) -> list[float]:
    return [round(p.mile_marker, 6) for p in points]


def distance_to_path_m(point: LngLat, path: tuple[LngLat, ...]) -> float:
    """Distance from a point to the nearest segment of the path, in metres (local flat approximation)."""
    best = math.inf
    kx = 111_320 * math.cos(math.radians(point[1]))
    ky = 110_574.0
    px, py = point[0] * kx, point[1] * ky
    for a, b in zip(path, path[1:], strict=False):
        ax, ay, bx, by = a[0] * kx, a[1] * ky, b[0] * kx, b[1] * ky
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


# --- Placement ---------------------------------------------------------------------------


def test_100_miles_at_25_gives_five_checkpoints() -> None:
    assert checkpoint_miles(100, 25) == [0, 25, 50, 75, 100]


def test_103_miles_at_50() -> None:
    assert checkpoint_miles(103, 50) == [0, 50, 100, 103]


def test_100_and_a_half_miles_at_50_merges_the_last_regular_stop() -> None:
    assert checkpoint_miles(100.5, 50) == [0, 50, 100.5]


@pytest.mark.parametrize(("distance", "expected"), [(0.4, [0, 0.4]), (0, [0]), (float("nan"), [0]), (10, [0, 10])])
def test_short_or_unusable_distances(distance: float, expected: list[float]) -> None:
    assert checkpoint_miles(distance, 10) == expected


def test_origin_is_never_merged_away() -> None:
    assert checkpoint_miles(0.9, 10)[0] == 0


@pytest.mark.parametrize("interval", [0, -10, float("nan"), float("inf")])
def test_invalid_interval_raises(interval: float) -> None:
    with pytest.raises(ValueError):
        checkpoint_miles(100, interval)


def test_sampled_route_uses_provider_distance_for_mile_markers() -> None:
    route = straight_route(100)
    stretched = FakeRoute(route.path, route.sections, 103.0, route.duration_seconds)
    assert miles_of(sample_checkpoints(stretched, DEPARTURE, 50)) == [0, 50, 100, 103]


@given(
    distance=st.floats(min_value=0.01, max_value=3000),
    interval=st.sampled_from([10, 25, 50]),
)
def test_checkpoint_miles_are_ordered_and_spaced(distance: float, interval: int) -> None:
    miles = checkpoint_miles(distance, interval)
    assert miles[0] == 0 and miles[-1] == distance
    assert all(b > a for a, b in zip(miles, miles[1:], strict=False))
    assert all(b - a <= interval + 1 for a, b in zip(miles, miles[1:], strict=False))


# --- Real route: on the path, arrival time ---------------------------------------------------


@pytest.fixture(scope="module")
def chicago_denver() -> FakeRoute:
    trip = json.loads((FIXTURES / "valhalla_chicago_denver.json").read_text("utf-8"))["trip"]
    route = parse_valhalla_trip(trip)
    assert route is not None
    return FakeRoute(route.path, route.sections, route.distance_miles, route.duration_seconds)


def test_every_checkpoint_lies_on_the_path(chicago_denver: FakeRoute) -> None:
    points = sample_checkpoints(chicago_denver, DEPARTURE, 10)
    assert len(points) > 100
    assert max(distance_to_path_m((p.lng, p.lat), chicago_denver.path) for p in points) < 50


def test_arrival_at_destination_is_departure_plus_total_time(chicago_denver: FakeRoute) -> None:
    last = sample_checkpoints(chicago_denver, DEPARTURE, 25)[-1]
    expected = DEPARTURE + timedelta(seconds=chicago_denver.duration_seconds)
    assert abs((last.eta - expected).total_seconds()) < 60
    assert last.mile_marker == pytest.approx(chicago_denver.distance_miles)


def test_etas_increase_along_the_route(chicago_denver: FakeRoute) -> None:
    points = sample_checkpoints(chicago_denver, DEPARTURE, 10)
    assert points[0].eta == DEPARTURE
    assert all(b.eta >= a.eta for a, b in zip(points, points[1:], strict=False))
    assert [p.sequence for p in points] == list(range(len(points)))


# --- ETAs follow section times, not average speed ---------------------------------------------


def test_slow_first_half_puts_the_midpoint_at_two_thirds_of_the_time() -> None:
    base = straight_route(100, hours=3, points=11)
    half = base.distance_miles / 2
    sections = (Section(0, 5, 2 * 3600, half), Section(5, 10, 1 * 3600, half))  # first half twice as slow
    route = FakeRoute(base.path, sections, base.distance_miles, 3 * 3600)
    points = sample_checkpoints(route, DEPARTURE, 25)
    midpoint = next(p for p in points if p.mile_marker == 50)
    assert midpoint.seconds_from_departure == pytest.approx(2 * 3600, rel=0.02)  # 2/3 of 3 h, not 1.5 h


def test_time_within_a_section_is_spread_by_distance() -> None:
    route = straight_route(100, hours=2, points=11)
    quarter = next(p for p in sample_checkpoints(route, DEPARTURE, 25) if p.mile_marker == 25)
    assert quarter.seconds_from_departure == pytest.approx(1800, rel=0.01)


def test_section_times_are_scaled_to_the_route_total() -> None:
    base = straight_route(100, hours=2, points=11)
    route = FakeRoute(base.path, (Section(0, 10, 7000, 100),), base.distance_miles, 7200)
    assert sample_checkpoints(route, DEPARTURE, 50)[-1].seconds_from_departure == pytest.approx(7200)


# --- Fault tolerance ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sections",
    [
        (),
        (Section(0, 99, 3600, 1),),  # points past the end of the path
        (Section(5, 5, 600, 0),),  # zero length
        (Section(0, 10, float("nan"), 1),),
        (Section(0, 6, 3600, 1), Section(3, 10, 3600, 1)),  # overlapping
        (Section(2, 4, 1200, 1),),  # covers only part of the path
        (Section(0, 10, 7200, 1), Section(2, 4, 1200, 1)),  # second lies inside the first
        (Section(0, 10, 0.0, 1),),  # no time at all
    ],
)
def test_odd_sections_still_give_ordered_etas_ending_at_the_total(sections: tuple[Section, ...]) -> None:
    base = straight_route(100, hours=2, points=11)
    route = FakeRoute(base.path, sections, base.distance_miles, 7200)
    points = sample_checkpoints(route, DEPARTURE, 25)
    assert miles_of(points) == [0, 25, 50, 75, round(base.distance_miles, 6)]
    assert all(b.seconds_from_departure >= a.seconds_from_departure for a, b in zip(points, points[1:], strict=False))
    assert points[-1].seconds_from_departure == pytest.approx(7200)


def test_bad_coordinates_are_dropped_and_average_pace_is_used() -> None:
    base = straight_route(100, hours=2, points=11)
    path = (base.path[0], (float("nan"), 0.0), *base.path[1:])
    route = FakeRoute(path, (Section(0, 11, 7200, 100),), base.distance_miles, 7200)
    points = sample_checkpoints(route, DEPARTURE, 50)
    assert points[1].seconds_from_departure == pytest.approx(3600, rel=0.01)


def test_naive_departure_is_read_as_utc() -> None:
    points = sample_checkpoints(straight_route(10), datetime(2026, 10, 20, 8, 0), 10)
    assert points[0].eta == DEPARTURE


def test_departure_timezone_is_kept() -> None:
    eastern = datetime(2026, 10, 20, 4, 0, tzinfo=timezone(timedelta(hours=-4)))
    points = sample_checkpoints(straight_route(10), eastern, 10)
    assert points[0].eta == DEPARTURE


def test_missing_duration_gives_zero_offsets() -> None:
    base = straight_route(50)
    route = FakeRoute(base.path, base.sections, base.distance_miles, float("nan"))
    assert {p.seconds_from_departure for p in sample_checkpoints(route, DEPARTURE, 25)} == {0.0}


def test_missing_distance_falls_back_to_the_path_length() -> None:
    base = straight_route(100)
    route = FakeRoute(base.path, base.sections, 0.0, base.duration_seconds)
    assert sample_checkpoints(route, DEPARTURE, 50)[-1].mile_marker == pytest.approx(base.distance_miles)


def test_single_point_and_empty_paths() -> None:
    one = FakeRoute(((1.0, 2.0),), (), 0.0, 0.0)
    [point] = sample_checkpoints(one, DEPARTURE, 10)
    assert (point.lng, point.lat, point.mile_marker, point.eta) == (1.0, 2.0, 0.0, DEPARTURE)
    assert sample_checkpoints(FakeRoute((), (), 0.0, 0.0), DEPARTURE, 10) == []


def test_repeated_path_points_do_not_break_interpolation() -> None:
    base = straight_route(100, points=11)
    path = (base.path[0], base.path[0], *base.path[1:5], base.path[5], base.path[5], *base.path[6:])
    route = FakeRoute(path, (), base.distance_miles, 7200)
    points = sample_checkpoints(route, DEPARTURE, 25)
    assert points[2].seconds_from_departure == pytest.approx(3600, rel=0.01)

"""B11 corridor heatmap, built from a trip planned with faked providers (tests/fakes.py)."""

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.routing import heatmap, trips
from apps.routing.errors import NoRoute, TripHasNoRoutes
from apps.routing.heatmap import CORRIDOR_KM, HOURS, corridor_grid, get_or_build_heatmap
from apps.routing.models import ForecastCache, Trip, TripHeatmap
from apps.routing.route_providers import haversine_km
from apps.routing.tests.fakes import CHICAGO, DENVER, chicago_denver_routes, weather_get
from apps.routing.trips import TripRequest, create_trip

pytestmark = pytest.mark.django_db


def wind_ramp(hour_index: int) -> float:
    """Wind that changes every hour (0–59 mph over and over), so levels differ hour to hour."""
    return float((hour_index * 7) % 60)


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def now() -> datetime:
    return timezone.now()


@pytest.fixture
def trip(now: datetime) -> Trip:
    request = TripRequest("Chicago, IL, USA", CHICAGO, "Denver, CO, USA", DENVER, now + timedelta(hours=2), 32_000, 25)
    with (
        mock.patch.object(trips, "plan_routes", return_value=chicago_denver_routes()),
        mock.patch("requests.get", weather_get(wind_by_hour=wind_ramp)),
    ):
        created = create_trip(request)
    return Trip.objects.with_details().get(pk=created.pk)


@pytest.fixture
def built(trip: Trip, now: datetime) -> dict[str, Any]:
    with mock.patch("requests.get", weather_get(wind_by_hour=wind_ramp)):
        data: dict[str, Any] = get_or_build_heatmap(trip, now).data
    return data


def nearest_point_km(point: tuple[float, float], points: list[list[float]]) -> float:
    return min(haversine_km(point, (p[0], p[1])) for p in points)


# --- Acceptance criteria ---------------------------------------------------------------------------


def test_grid_covers_all_three_routes_without_gaps_and_stays_under_400_points(
    trip: Trip, built: dict[str, Any]
) -> None:
    points = built["points"]
    assert len(points) <= heatmap.MAX_POINTS
    spacing_km = built["spacing_miles"] * 1.609344
    for route in trip.routes.all():
        path = route.display_path
        for lng, lat in path[:: max(1, len(path) // 150)]:  # ~150 spots along each route
            assert nearest_point_km((lng, lat), points) <= spacing_km * 0.75


def test_every_point_has_49_hourly_values(built: dict[str, Any]) -> None:
    assert len(built["hours"]) == HOURS == 49
    for key in ("levels", "wind_mph", "rain_in_per_hr", "snow_in_per_hr"):
        assert len(built[key]) == len(built["points"])
        assert all(len(row) == 49 for row in built[key])


def test_a_point_on_a_route_at_the_arrival_hour_matches_the_checkpoint(trip: Trip, built: dict[str, Any]) -> None:
    start = datetime.fromisoformat(built["start_time"])
    index = {(round(lat, 2), round(lng, 2)): i for i, (lng, lat) in enumerate(built["points"])}
    compared = 0
    levels_seen = set()
    for route in trip.routes.all():
        for c in route.checkpoints.all():
            hour = round((c.eta - start).total_seconds() / 3600)
            if not 0 <= hour < HOURS or c.risk_level is None:
                continue
            point = index[ForecastCache.grid_key(c.lat, c.lng)]
            assert built["levels"][point][hour] == c.risk_level, (route.index, c.sequence, hour)
            compared += 1
            levels_seen.add(c.risk_level)
    assert compared > 100
    assert len(levels_seen) >= 3  # the ramping wind makes this a real comparison, not all-Low


def test_built_once_then_read_back_without_provider_calls(trip: Trip, built: dict[str, Any], now: datetime) -> None:
    with mock.patch("requests.get") as provider:
        again = get_or_build_heatmap(Trip.objects.with_details().get(pk=trip.pk), now + timedelta(hours=5))
    provider.assert_not_called()
    assert again.data == built
    assert TripHeatmap.objects.count() == 1


def test_payload_is_under_500_kb_compressed(built: dict[str, Any]) -> None:
    raw = json.dumps(built, separators=(",", ":")).encode()
    assert len(gzip.compress(raw)) < 500 * 1024


# --- Content ---------------------------------------------------------------------------------------


def test_hour_zero_is_the_build_hour_and_departure_is_marked(built: dict[str, Any], now: datetime) -> None:
    start = datetime.fromisoformat(built["start_time"])
    assert start == now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    assert built["hours"][0] == built["start_time"]
    assert datetime.fromisoformat(built["hours"][48]) == start + timedelta(hours=48)
    assert built["departure_hour"] in (2, 3)  # departure is now + 2 h, rounded to the nearest hour


def test_values_are_rounded_and_unknown_is_null(trip: Trip, now: datetime) -> None:
    with mock.patch("requests.get", side_effect=OSError("down")):
        TripHeatmap.objects.all().delete()
        ForecastCache.objects.all().delete()
        data = get_or_build_heatmap(trip, now).data
    assert all(v is None for row in data["levels"] for v in row)
    assert all(v is None for row in data["wind_mph"] for v in row)


def test_hours_past_the_forecast_are_unknown(trip: Trip, now: datetime) -> None:
    with mock.patch("requests.get", weather_get(wind_by_hour=wind_ramp, start=now - timedelta(days=4))):
        TripHeatmap.objects.all().delete()
        ForecastCache.objects.all().delete()
        data = get_or_build_heatmap(trip, now).data
    last = [row[48] for row in data["levels"]]
    assert all(v is None for v in last)  # the faked forecast ran out about a day ago


def test_values_keep_display_precision(built: dict[str, Any]) -> None:
    assert all(isinstance(v, int) for row in built["wind_mph"] for v in row if v is not None)
    assert all(v == round(v, 2) for row in built["rain_in_per_hr"] for v in row if v is not None)


def test_truck_sits_at_the_origin_before_departure_and_the_destination_after_arrival(
    trip: Trip, built: dict[str, Any]
) -> None:
    positions = built["truck"]["positions"]
    recommended = trip.routes.get(is_recommended=True)
    assert built["truck"]["route_index"] == recommended.index
    assert len(positions) == 49
    assert haversine_km(tuple(positions[0]), CHICAGO) < 1  # hour 0 is before the 2 h-later departure
    assert haversine_km(tuple(positions[48]), DENVER) < 1  # ~16 h trip, long arrived by hour 48


def test_truck_three_hours_after_departure_is_where_the_timing_says(trip: Trip, built: dict[str, Any]) -> None:
    start = datetime.fromisoformat(built["start_time"])
    recommended = trip.routes.get(is_recommended=True)
    hour = built["departure_hour"] + 3
    at = start + timedelta(hours=hour)
    checkpoints = sorted(recommended.checkpoints.all(), key=lambda c: c.sequence)
    before = max((c for c in checkpoints if c.eta <= at), key=lambda c: c.eta)
    after = min((c for c in checkpoints if c.eta >= at), key=lambda c: c.eta)
    truck = tuple(built["truck"]["positions"][hour])
    gap_km = haversine_km((before.lng, before.lat), (after.lng, after.lat))
    assert haversine_km(truck, (before.lng, before.lat)) <= gap_km + 1
    assert haversine_km(truck, (after.lng, after.lat)) <= gap_km + 1


# --- Errors and races ------------------------------------------------------------------------------


def test_a_failed_trip_has_no_heatmap(now: datetime) -> None:
    request = TripRequest("A", CHICAGO, "B", DENVER, now + timedelta(hours=1), 1, 25)
    with mock.patch.object(trips, "plan_routes", side_effect=NoRoute()), pytest.raises(NoRoute):
        create_trip(request)
    with pytest.raises(TripHasNoRoutes) as exc:
        get_or_build_heatmap(Trip.objects.get())
    assert exc.value.status_code == 409
    assert str(exc.value.detail) == "Trip has no routes."


def test_a_concurrent_build_uses_the_saved_one(trip: Trip, now: datetime) -> None:
    # Another request saved its heatmap after our "is there one?" check but before our insert.
    TripHeatmap.objects.create(trip=trip, start_time=now, data={"from": "the other request"})
    nothing = mock.Mock()
    nothing.first.return_value = None
    with (
        mock.patch("requests.get", weather_get()),
        mock.patch.object(TripHeatmap.objects, "filter", return_value=nothing),
    ):
        result = get_or_build_heatmap(trip, now)
    assert result.data == {"from": "the other request"}


# --- Grid -------------------------------------------------------------------------------------------


def test_grid_respects_the_budget_on_a_cross_country_trip() -> None:
    path = [(-74.0 - i * 0.5, 40.7 - i * 0.03) for i in range(100)]  # ~4,200 km east-west
    grid = corridor_grid([path], 400)
    assert 200 < len(grid.points) <= 400
    assert grid.spacing_km <= heatmap.MAX_SPACING_KM


def test_short_trips_use_the_finest_spacing() -> None:
    grid = corridor_grid([[(-87.63, 41.88), (-87.91, 43.04)]], 400)
    assert grid.spacing_km == heatmap.MIN_SPACING_KM
    assert len(grid.points) <= 400


def test_grid_points_stay_inside_the_corridor() -> None:
    path = [(-100.0 + i * 0.1, 40.0) for i in range(60)]
    grid = corridor_grid([path], 400)
    samples = heatmap._sample_path(path, 2)
    for lng, lat in grid.points:
        assert min(haversine_km((lng, lat), s) for s in samples) <= CORRIDOR_KM + 1


def test_no_paths_give_an_empty_grid() -> None:
    assert corridor_grid([[]], 400).points == []
    assert heatmap.truck_positions(mock.Mock(), [], datetime.now(UTC), []) == []


def test_a_route_with_one_checkpoint_keeps_the_truck_at_the_start() -> None:
    route = mock.Mock()
    route.checkpoints.all.return_value = [mock.Mock()]
    hours = [datetime(2026, 10, 20, h, tzinfo=UTC) for h in range(3)]
    assert heatmap.truck_positions(route, [(1.0, 2.0), (3.0, 4.0)], hours[0], hours) == [[1.0, 2.0]] * 3

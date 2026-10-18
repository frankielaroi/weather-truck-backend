from datetime import UTC, datetime

import pytest
from django.db import IntegrityError
from pytest_django import DjangoAssertNumQueries

from apps.routing.choices import RiskLevel
from apps.routing.models import Checkpoint, ForecastCache, Route, Trip

DEPARTURE = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def make_trip() -> Trip:
    return Trip.objects.create(
        origin_name="Denver, CO",
        origin_lat=39.74,
        origin_lng=-104.99,
        destination_name="Salt Lake City, UT",
        destination_lat=40.76,
        destination_lng=-111.89,
        departure_at=DEPARTURE,
        load_weight_lb=35_000,
    )


def make_route(trip: Trip, index: int, **overrides: object) -> Route:
    fields: dict[str, object] = {
        "distance_miles": 520.0,
        "duration_seconds": 8 * 3600,
        "geometry": {"type": "LineString", "coordinates": [[-104.99, 39.74], [-111.89, 40.76]]},
    }
    fields.update(overrides)
    return Route.objects.create(trip=trip, index=index, **fields)


def test_risk_levels_are_ordered() -> None:
    assert RiskLevel.LOW < RiskLevel.MODERATE < RiskLevel.HIGH < RiskLevel.SEVERE < RiskLevel.NO_TRAVEL


@pytest.mark.django_db
def test_only_one_recommended_route_per_trip() -> None:
    trip = make_trip()
    make_route(trip, 0, is_recommended=True)
    with pytest.raises(IntegrityError):
        make_route(trip, 1, is_recommended=True)


@pytest.mark.django_db
def test_with_details_prefetches_without_n_plus_one(django_assert_num_queries: DjangoAssertNumQueries) -> None:
    for _ in range(2):
        trip = make_trip()
        for index in range(3):
            route = make_route(trip, index, is_recommended=index == 1)
            for seq in range(3):
                Checkpoint.objects.create(
                    route=route, sequence=seq, mile_marker=seq * 25, lat=40, lng=-105, eta=DEPARTURE
                )

    # trips (+ user join), routes, checkpoints
    with django_assert_num_queries(3):
        trips = list(Trip.objects.with_details())
        for trip in trips:
            assert trip.recommended_route is not None
            assert trip.recommended_route.index == 1
            for route in trip.routes.all():
                assert [c.sequence for c in route.checkpoints.all()] == [0, 1, 2]


def test_forecast_grid_key_rounds_to_grid() -> None:
    assert ForecastCache.grid_key(39.7392, -104.9903) == (39.74, -104.99)

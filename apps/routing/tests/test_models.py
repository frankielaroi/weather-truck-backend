from datetime import UTC, datetime

import pytest
from django.db import IntegrityError
from pytest_django import DjangoAssertNumQueries

from apps.routing.checkpoints import CheckpointPoint
from apps.routing.choices import RiskLevel
from apps.routing.models import Checkpoint, ForecastCache, Route, Trip
from apps.routing.recommend import (
    INCOMPLETE_FORECAST_WARNING,
    NO_SAFE_ROUTE_WARNING,
    DecidingStep,
    RouteOption,
    RouteSummary,
    ScoredPoint,
    recommend,
    summarize_route,
)
from apps.routing.risk import RiskAssessment
from apps.routing.routes import PlannedRoute
from apps.routing.weather import CheckpointWeather

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


def _point(sequence: int = 3) -> CheckpointPoint:
    return CheckpointPoint(
        sequence=sequence, mile_marker=75.0, lng=-100.0, lat=40.0, seconds_from_departure=4500, eta=DEPARTURE
    )


@pytest.mark.django_db
def test_checkpoint_from_results_stores_weather_risk_and_notes() -> None:
    route = make_route(make_trip(), 0)
    forecast_time = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    result = CheckpointWeather(
        forecast_time=forecast_time,
        wind_mph=47.0,
        gust_mph=60.0,
        rain_in_per_hr=0.3,
        snow_in_per_hr=None,
        temperature_f=40.0,
        description="Rain",
        risk=RiskAssessment(
            level=RiskLevel.NO_TRAVEL,
            reasons=("Wind 47 mph with 35,000 lb load → No Travel", "Rain 0.3 in/hr → High"),
            data_issues=("Using a forecast from about 2 h ago (weather service unavailable)",),
        ),
    )
    Checkpoint.from_results(route, _point(), result).save()

    saved = Checkpoint.objects.get(route=route, sequence=3)
    assert (saved.mile_marker, saved.lat, saved.lng, saved.eta) == (75.0, 40.0, -100.0, DEPARTURE)
    assert (saved.wind_mph, saved.wind_gust_mph, saved.rain_in_per_hr, saved.snow_in_per_hr) == (47.0, 60.0, 0.3, None)
    assert saved.forecast_time == forecast_time
    assert saved.weather_description == "Rain"
    assert saved.risk_level == RiskLevel.NO_TRAVEL
    assert saved.risk_reasons == ["Wind 47 mph with 35,000 lb load → No Travel", "Rain 0.3 in/hr → High"]
    assert saved.data_notes == ["Using a forecast from about 2 h ago (weather service unavailable)"]
    assert not saved.is_unknown


@pytest.mark.django_db
def test_unknown_checkpoint_keeps_its_note() -> None:
    route = make_route(make_trip(), 0)
    unknown = CheckpointWeather(
        None, None, None, None, None, None, "", RiskAssessment(None, (), ("Beyond forecast range",))
    )
    Checkpoint.from_results(route, _point(0), unknown).save()

    saved = Checkpoint.objects.get(route=route)
    assert saved.is_unknown
    assert saved.risk_level is None
    assert saved.forecast_time is None
    assert saved.data_notes == ["Beyond forecast range"]
    assert saved.risk_reasons == []


def test_long_descriptions_are_cut_to_the_column_size() -> None:
    route = Route(trip_id=1, index=0, distance_miles=1, duration_seconds=1, geometry={})
    long = CheckpointWeather(None, None, None, None, None, None, "x" * 300, RiskAssessment(None))
    assert len(Checkpoint.from_results(route, _point(), long).weather_description) == 100


def _planned(label: str = "via I-80 W", notes: tuple[str, ...] = ()) -> PlannedRoute:
    path = ((-87.63, 41.88), (-96.0, 41.0), (-104.99, 39.74))
    return PlannedRoute(label, path, 1016.4, 56672.8, (), "valhalla", "truck", notes)


def _summary(points: list[ScoredPoint], distance: float = 1016.4) -> RouteSummary:
    return summarize_route(points, distance)


@pytest.mark.django_db
def test_route_from_results_stores_plan_summary_and_recommendation() -> None:
    trip = make_trip()
    summaries = [
        _summary([ScoredPoint(0, RiskLevel.LOW), ScoredPoint(500, RiskLevel.SEVERE), ScoredPoint(1016.4, None)]),
        _summary(
            [ScoredPoint(0, RiskLevel.LOW), ScoredPoint(500, RiskLevel.MODERATE), ScoredPoint(1016.4, RiskLevel.LOW)]
        ),
    ]
    options = [RouteOption(f"Route {c}", s, 56672.8) for c, s in zip("AB", summaries, strict=True)]
    recommendation = recommend(options)
    assert recommendation is not None and recommendation.recommended_index == 1

    for index, summary in enumerate(summaries):
        Route.from_results(trip, index, _planned(f"via road {index}", ("A note",)), summary, recommendation).save()

    risky, best = Route.objects.filter(trip=trip).order_by("index")
    assert (best.label, best.provider, best.profile, best.notes) == ("via road 1", "valhalla", "truck", ["A note"])
    assert best.is_recommended and not risky.is_recommended
    assert best.recommendation_reason == recommendation.reason
    assert best.deciding_step == DecidingStep.SEVERE_MILES.value
    assert risky.recommendation_reason == "" and risky.deciding_step == ""
    assert best.duration_seconds == 56673
    assert best.geometry["type"] == "LineString"
    assert best.geometry["coordinates"][0] == [-87.63, 41.88]
    assert risky.severe_miles == pytest.approx(508.2)
    assert risky.unknown_miles == pytest.approx(258.2)  # halfway from 500 (758.2) to 1016.4
    assert risky.max_risk_level == RiskLevel.SEVERE
    for route in (risky, best):
        assert route.total_risk_miles == pytest.approx(route.distance_miles, abs=0.1)


@pytest.mark.django_db
def test_route_with_no_known_weather_stores_null_average_and_worst_level() -> None:
    summary = _summary([ScoredPoint(0, None), ScoredPoint(1016.4, None)])
    Route.from_results(make_trip(), 0, _planned(), summary, recommend([RouteOption("Route A", summary, 1)])).save()
    saved = Route.objects.get()
    assert saved.average_risk is None
    assert saved.max_risk_level is None
    assert saved.unknown_miles == pytest.approx(1016.4)
    assert saved.warnings == [INCOMPLETE_FORECAST_WARNING]
    assert saved.deciding_step == DecidingStep.ONLY_ROUTE.value


@pytest.mark.django_db
def test_route_warnings_come_from_the_recommendation() -> None:
    summaries = [_summary([ScoredPoint(0, RiskLevel.NO_TRAVEL), ScoredPoint(1016.4, RiskLevel.LOW)]) for _ in range(2)]
    recommendation = recommend([RouteOption(f"Route {c}", s, 1) for c, s in zip("AB", summaries, strict=True)])
    trip = make_trip()
    rows = [Route.from_results(trip, i, _planned(f"via {i}"), s, recommendation) for i, s in enumerate(summaries)]
    assert rows[0].warnings == [NO_SAFE_ROUTE_WARNING]
    assert rows[1].warnings == []


def test_route_without_a_recommendation_keeps_its_own_warnings() -> None:
    summary = _summary([ScoredPoint(0, None), ScoredPoint(1016.4, None)])
    row = Route.from_results(Trip(id=1), 0, _planned(), summary, None)
    assert row.warnings == [INCOMPLETE_FORECAST_WARNING]
    assert not row.is_recommended and row.recommendation_reason == ""

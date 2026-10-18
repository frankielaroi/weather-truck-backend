"""B8 trip coordinator, with faked providers built from saved real responses. No real calls."""

import copy
import json
import logging
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
from apps.routing.errors import NoRoute, ProviderUnavailable
from apps.routing.models import Checkpoint, ForecastCache, Route, Trip
from apps.routing.recommend import INCOMPLETE_FORECAST_WARNING, NO_SAFE_ROUTE_WARNING
from apps.routing.route_providers import ProviderRoute, join_routes, parse_valhalla_trip
from apps.routing.routes import PlannedRoute, label_routes
from apps.routing.trips import TripRequest, create_trip
from apps.routing.weather import MPH_PER_MS

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures"
WEATHER = json.loads((FIXTURES / "weather" / "open_meteo_3_points.json").read_text("utf-8"))
FORECAST_START = datetime.fromtimestamp(WEATHER[0]["hourly"]["time"][0], tz=UTC)
CHICAGO = (-87.6298, 41.8781)
DENVER = (-104.9903, 39.7392)


def _valhalla(name: str, alternate: int | None = None) -> ProviderRoute:
    data = json.loads((FIXTURES / "routing" / f"{name}.json").read_text("utf-8"))
    trip = data["trip"] if alternate is None else data["alternates"][alternate]["trip"]
    route = parse_valhalla_trip(trip)
    assert route is not None
    return route


def _join_legs(side: str) -> ProviderRoute:
    return join_routes([_valhalla(f"valhalla_{side}_leg1"), _valhalla(f"valhalla_{side}_leg2")])


@pytest.fixture(scope="module")
def three_routes() -> list[PlannedRoute]:
    return label_routes(
        [_valhalla("valhalla_chicago_denver"), _valhalla("valhalla_chicago_denver", 0), _join_legs("south")]
    )


def weather_answering(wind_mph: float = 7.0) -> mock.Mock:
    def get(url: str, *, params: dict[str, Any], timeout: float) -> mock.Mock:
        count = len(str(params["latitude"]).split(","))
        location = copy.deepcopy(WEATHER[0])
        hours = len(location["hourly"]["time"])
        location["hourly"]["wind_speed_10m"] = [wind_mph / MPH_PER_MS] * hours
        res = mock.Mock(spec=requests.Response)
        res.status_code, res.ok, res.text = 200, True, ""
        res.json.return_value = [copy.deepcopy(location) for _ in range(count)]
        return res

    return mock.Mock(side_effect=get)


def make_request(load: int = 32_000, interval: int = 25) -> TripRequest:
    return TripRequest(
        origin_name="Chicago, IL, USA",
        origin=CHICAGO,
        destination_name="Denver, CO, USA",
        destination=DENVER,
        departure_at=FORECAST_START + timedelta(hours=2),
        load_weight_lb=load,
        interval_miles=interval,
    )


@pytest.fixture(autouse=True)
def clean_caches() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def providers(three_routes: list[PlannedRoute]) -> Iterator[dict[str, mock.Mock]]:
    with (
        mock.patch.object(trips, "plan_routes", return_value=three_routes) as plan,
        mock.patch("requests.get", weather_answering()) as weather,
    ):
        yield {"plan": plan, "weather": weather}


# --- Acceptance criteria ---------------------------------------------------------------------


def test_trip_ends_ready_with_three_scored_routes_and_one_recommended(providers: dict[str, mock.Mock]) -> None:
    trip = create_trip(make_request())

    assert trip.status == Trip.Status.READY
    routes = list(trip.routes.order_by("index"))
    assert len(routes) == 3
    assert sum(r.is_recommended for r in routes) == 1
    for route in routes:
        checkpoints = list(route.checkpoints.all())
        assert len(checkpoints) > 30
        assert checkpoints[0].mile_marker == 0
        assert checkpoints[-1].mile_marker == pytest.approx(route.distance_miles)
        assert all(c.risk_level == RiskLevel.LOW for c in checkpoints)
        assert route.total_risk_miles == pytest.approx(route.distance_miles, abs=0.1)
        assert route.max_risk_level == RiskLevel.LOW
        assert route.provider == "valhalla" and route.label.startswith("via ")
    recommended = next(r for r in routes if r.is_recommended)
    assert recommended.recommendation_reason.startswith("Recommended:")
    assert recommended.deciding_step == "travel_time"  # all Low, so the fastest wins


def test_routing_failure_marks_the_trip_failed_and_saves_nothing_else() -> None:
    down = ProviderUnavailable("The routing service is down.")
    with mock.patch.object(trips, "plan_routes", side_effect=down), pytest.raises(ProviderUnavailable):
        create_trip(make_request())
    trip = Trip.objects.get()
    assert trip.status == Trip.Status.FAILED
    assert trip.error_message == "The routing service is down."
    assert not Route.objects.exists()
    assert not Checkpoint.objects.exists()


def test_loading_a_saved_trip_takes_at_most_three_queries(
    providers: dict[str, mock.Mock], django_assert_max_num_queries: DjangoAssertNumQueries
) -> None:
    trip_id = create_trip(make_request()).pk
    with django_assert_max_num_queries(3):
        trip = Trip.objects.with_details().get(pk=trip_id)
        assert trip.recommended_route is not None
        assert sum(len(route.checkpoints.all()) for route in trip.routes.all()) > 90


# --- Failures ------------------------------------------------------------------------------------


def test_no_route_is_recorded_with_its_message() -> None:
    with mock.patch.object(trips, "plan_routes", side_effect=NoRoute()), pytest.raises(NoRoute):
        create_trip(make_request())
    assert Trip.objects.get().error_message == "No route found between these places."


def test_a_failure_while_saving_rolls_back_routes_and_checkpoints(providers: dict[str, mock.Mock]) -> None:
    with (
        mock.patch.object(Checkpoint.objects, "bulk_create", side_effect=RuntimeError("disk full")),
        pytest.raises(RuntimeError),
    ):
        create_trip(make_request())
    assert list(Trip.objects.values_list("status", "error_message")) == [(Trip.Status.FAILED, trips.UNEXPECTED_FAILURE)]
    assert not Route.objects.exists()
    assert not Checkpoint.objects.exists()


def test_an_unexpected_error_is_recorded_without_internals(providers: dict[str, mock.Mock]) -> None:
    with (
        mock.patch.object(trips, "sample_checkpoints", side_effect=KeyError("secret detail")),
        pytest.raises(KeyError),
    ):
        create_trip(make_request())
    assert Trip.objects.get().error_message == trips.UNEXPECTED_FAILURE


def test_failing_to_record_the_failure_still_raises_the_original_error() -> None:
    with (
        mock.patch.object(trips, "plan_routes", side_effect=NoRoute()),
        mock.patch.object(Trip.objects, "create", side_effect=RuntimeError("db down")),
        pytest.raises(NoRoute),
    ):
        create_trip(make_request())


def test_weather_outage_still_gives_a_ready_trip_with_unknown_checkpoints(three_routes: list[PlannedRoute]) -> None:
    with (
        mock.patch.object(trips, "plan_routes", return_value=three_routes),
        mock.patch("requests.get", side_effect=requests.ConnectionError("down")),
    ):
        trip = create_trip(make_request())
    assert trip.status == Trip.Status.READY
    assert not Checkpoint.objects.exclude(risk_level=None).exists()
    assert all(c.data_notes == ["Weather unavailable for this checkpoint"] for c in Checkpoint.objects.all())
    for route in trip.routes.all():
        assert route.average_risk is None
        assert INCOMPLETE_FORECAST_WARNING in route.warnings
    assert trip.routes.filter(is_recommended=True).count() == 1


def test_storm_everywhere_recommends_the_best_with_a_no_safe_route_warning(three_routes: list[PlannedRoute]) -> None:
    with (
        mock.patch.object(trips, "plan_routes", return_value=three_routes),
        mock.patch("requests.get", weather_answering(wind_mph=60)),
    ):
        trip = create_trip(make_request())
    recommended = trip.routes.get(is_recommended=True)
    assert recommended.max_risk_level == RiskLevel.NO_TRAVEL
    assert NO_SAFE_ROUTE_WARNING in recommended.warnings
    assert Checkpoint.objects.filter(risk_level=RiskLevel.NO_TRAVEL).count() == Checkpoint.objects.count()


# --- Caching and timing ------------------------------------------------------------------------


def test_a_repeated_trip_reuses_the_route_plan_and_forecasts(providers: dict[str, mock.Mock]) -> None:
    create_trip(make_request())
    providers["plan"].reset_mock()
    providers["weather"].reset_mock()
    create_trip(make_request())
    providers["plan"].assert_not_called()
    providers["weather"].assert_not_called()
    assert Trip.objects.filter(status=Trip.Status.READY).count() == 2


def test_a_different_truck_weight_plans_again(providers: dict[str, mock.Mock]) -> None:
    create_trip(make_request(load=10_000))
    create_trip(make_request(load=40_000))
    assert providers["plan"].call_count == 2


def test_a_broken_cache_does_not_stop_trips(providers: dict[str, mock.Mock]) -> None:
    with (
        mock.patch.object(cache, "get", side_effect=RuntimeError("cache down")),
        mock.patch.object(cache, "set", side_effect=RuntimeError("cache down")),
    ):
        assert create_trip(make_request()).status == Trip.Status.READY


def test_each_step_time_is_logged(providers: dict[str, mock.Mock], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="apps.routing.trips"):
        create_trip(make_request())
    line = next(r.getMessage() for r in caplog.records if "ready" in r.getMessage())
    for step in ("routing", "checkpoints", "weather", "save", "total"):
        assert step in line


def test_trip_inputs_are_saved(providers: dict[str, mock.Mock]) -> None:
    trip = create_trip(make_request(load=12_345, interval=50))
    saved = Trip.objects.get(pk=trip.pk)
    assert (saved.origin_name, saved.destination_name) == ("Chicago, IL, USA", "Denver, CO, USA")
    assert (saved.origin_lng, saved.origin_lat) == CHICAGO
    assert (saved.load_weight_lb, saved.sample_interval_miles) == (12_345, 50)
    assert saved.departure_at == FORECAST_START + timedelta(hours=2)
    assert ForecastCache.objects.exists()

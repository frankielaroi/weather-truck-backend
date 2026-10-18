"""B10 trip API: input rules, create/get, error shape and CORS. Providers are faked (tests/fakes.py)."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client
from pytest_django.fixtures import Settings

from apps.routing import trips
from apps.routing.errors import NoRoute, ProviderTimeout, ProviderUnavailable
from apps.routing.models import Route, Trip
from apps.routing.tests.fakes import CHICAGO, DENVER, chicago_denver_routes, weather_get
from apps.routing.tests.test_responses import TRIP_SCHEMA, check

pytestmark = pytest.mark.django_db

FRONTEND = "http://localhost:3000"


def payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "origin": {"name": "Chicago, IL, USA", "lat": CHICAGO[1], "lng": CHICAGO[0]},
        "destination": {"name": "Denver, CO, USA", "lat": DENVER[1], "lng": DENVER[0]},
        "departure_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "load_weight_lb": 32_000,
        "interval_miles": 25,
    }
    body.update(overrides)
    return body


def post(client: Client, body: dict[str, Any], origin: str | None = None) -> Any:
    headers = {"Origin": origin} if origin else None
    return client.post("/api/trips/", body, content_type="application/json", headers=headers)


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def providers() -> Iterator[dict[str, mock.Mock]]:
    with (
        mock.patch.object(trips, "plan_routes", return_value=chicago_denver_routes()) as plan,
        mock.patch("requests.get", weather_get()) as weather,
    ):
        yield {"plan": plan, "weather": weather}


def field_errors(res: Any) -> dict[str, Any]:
    assert res.status_code == 400, res.content
    error = res.json()["error"]
    assert error["code"] == "invalid_input"
    return dict(error["fields"])


# --- Each input rule rejects bad input with a 400 and a field message -----------------------------


@pytest.mark.parametrize(
    ("load", "message"),
    [(0, "Load must be at least 1 lb."), (200_001, "Load can't be more than 200,000 lb."), (-5, "at least")],
)
def test_load_weight_range(client: Client, providers: dict[str, mock.Mock], load: int, message: str) -> None:
    fields = field_errors(post(client, payload(load_weight_lb=load)))
    assert any(message in m for m in fields["load_weight_lb"])


@pytest.mark.parametrize("load", [1, 200_000])
def test_load_weight_edges_are_allowed(client: Client, providers: dict[str, mock.Mock], load: int) -> None:
    assert post(client, payload(load_weight_lb=load)).status_code == 201


@pytest.mark.parametrize("load", ["heavy", 12.5, None])
def test_load_weight_must_be_a_whole_number(client: Client, load: Any) -> None:
    assert "load_weight_lb" in field_errors(post(client, payload(load_weight_lb=load)))


@pytest.mark.parametrize("interval", [5, 30, 100, "ten", 0])
def test_interval_must_be_10_25_or_50(client: Client, interval: Any) -> None:
    fields = field_errors(post(client, payload(interval_miles=interval)))
    assert fields["interval_miles"] == ["Interval must be 10, 25 or 50 miles."]


@pytest.mark.parametrize("interval", [10, 25, 50, "50"])
def test_allowed_intervals(client: Client, providers: dict[str, mock.Mock], interval: Any) -> None:
    res = post(client, payload(interval_miles=interval))
    assert res.status_code == 201
    assert res.json()["inputs"]["interval_miles"] == int(interval)


def test_departure_more_than_an_hour_ago_is_rejected(client: Client) -> None:
    past = (datetime.now(UTC) - timedelta(hours=1, minutes=5)).isoformat()
    assert field_errors(post(client, payload(departure_at=past)))["departure_at"] == [
        "Departure can't be more than 1 hour in the past."
    ]


def test_departure_more_than_48_hours_ahead_is_rejected(client: Client) -> None:
    later = (datetime.now(UTC) + timedelta(hours=48, minutes=5)).isoformat()
    assert "48 hours" in field_errors(post(client, payload(departure_at=later)))["departure_at"][0]


@pytest.mark.parametrize("offset", [timedelta(minutes=-55), timedelta(hours=47)])
def test_departure_inside_the_window_is_allowed(
    client: Client, providers: dict[str, mock.Mock], offset: timedelta
) -> None:
    assert post(client, payload(departure_at=(datetime.now(UTC) + offset).isoformat())).status_code == 201


@pytest.mark.parametrize("value", ["tomorrow", "", None])
def test_departure_must_be_a_date_time(client: Client, value: Any) -> None:
    assert "departure_at" in field_errors(post(client, payload(departure_at=value)))


def test_naive_departure_is_read_as_utc(client: Client, providers: dict[str, mock.Mock]) -> None:
    naive = (datetime.now(UTC) + timedelta(hours=2)).replace(tzinfo=None, microsecond=0)
    res = post(client, payload(departure_at=naive.isoformat()))
    assert res.json()["inputs"]["departure_at"] == naive.replace(tzinfo=UTC).isoformat()


def test_origin_and_destination_must_differ(client: Client) -> None:
    same = {"name": "Chicago again", "lat": CHICAGO[1] + 0.0001, "lng": CHICAGO[0]}
    assert field_errors(post(client, payload(destination=same)))["destination"] == [
        "Destination must be different from the origin."
    ]


@pytest.mark.parametrize(
    ("place", "field"),
    [
        ({"name": "X", "lat": 91, "lng": 0}, "lat"),
        ({"name": "X", "lat": 0, "lng": -181}, "lng"),
        ({"name": "X", "lat": "NaN", "lng": 0}, "lat"),
        ({"name": "X", "lat": 0, "lng": "Infinity"}, "lng"),
        ({"name": "X", "lat": "north", "lng": 0}, "lat"),
        ({"name": "", "lat": 0, "lng": 0}, "name"),
        ({"name": "X" * 256, "lat": 0, "lng": 0}, "name"),
        ({"lat": 0, "lng": 0}, "name"),
    ],
)
def test_places_need_a_name_and_valid_coordinates(client: Client, place: dict[str, Any], field: str) -> None:
    fields = field_errors(post(client, payload(origin=place)))
    assert list(fields) == [f"origin.{field}"]
    assert all(isinstance(m, str) and "ErrorDetail" not in m for m in fields[f"origin.{field}"])


@pytest.mark.parametrize("missing", ["origin", "destination", "departure_at", "load_weight_lb", "interval_miles"])
def test_every_field_is_required(client: Client, missing: str) -> None:
    body = payload()
    del body[missing]
    assert missing in field_errors(post(client, body))


def test_bad_input_never_reaches_the_providers(client: Client, providers: dict[str, mock.Mock]) -> None:
    post(client, payload(load_weight_lb=0))
    providers["plan"].assert_not_called()
    assert not Trip.objects.exists()


def test_a_bare_nan_in_the_json_is_rejected(client: Client) -> None:
    raw = json.dumps(payload()).replace(str(CHICAGO[1]), "NaN", 1)
    res = client.post("/api/trips/", raw, content_type="application/json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "parse_error"


def test_a_non_json_body_is_a_400_in_the_error_shape(client: Client) -> None:
    res = client.post("/api/trips/", "not json", content_type="application/json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "parse_error"


# --- Create and get ------------------------------------------------------------------------------


def test_a_valid_request_returns_201_with_the_full_trip(client: Client, providers: dict[str, mock.Mock]) -> None:
    res = post(client, payload())
    assert res.status_code == 201
    body = res.json()
    check(body, TRIP_SCHEMA)
    assert body["status"] == "ready"
    assert len(body["routes"]) == 3
    assert sum(r["is_recommended"] for r in body["routes"]) == 1
    assert {c["risk"]["level"]["name"] for r in body["routes"] for c in r["checkpoints"]} == {"Low"}


def test_getting_a_trip_returns_the_same_body_as_when_it_was_created(
    client: Client, providers: dict[str, mock.Mock]
) -> None:
    created = post(client, payload()).json()
    fetched = client.get(f"/api/trips/{created['id']}/")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_unknown_trip_is_404_in_the_error_shape(client: Client) -> None:
    res = client.get("/api/trips/999999/")
    assert res.status_code == 404
    assert res.json() == {"error": {"code": "not_found", "message": "Trip not found."}}


def test_a_failed_trip_can_still_be_fetched(client: Client) -> None:
    with mock.patch.object(trips, "plan_routes", side_effect=NoRoute()):
        assert post(client, payload()).status_code == 422
    trip = Trip.objects.get()
    body = client.get(f"/api/trips/{trip.pk}/").json()
    assert (body["status"], body["error"], body["routes"]) == ("failed", "No route found between these places.", [])


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (NoRoute(), 422, "no_route"),
        (ProviderUnavailable("The routing service isn't responding right now."), 502, "provider_unavailable"),
        (ProviderTimeout("The routing service took too long to respond."), 504, "provider_timeout"),
    ],
)
def test_provider_problems_use_the_error_shape(client: Client, error: Exception, status: int, code: str) -> None:
    with mock.patch.object(trips, "plan_routes", side_effect=error):
        res = post(client, payload())
    assert res.status_code == status
    body = res.json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert not Route.objects.exists()


def test_get_on_the_create_url_is_not_allowed(client: Client) -> None:
    res = client.get("/api/trips/")
    assert res.status_code == 405
    assert res.json()["error"]["code"] == "method_not_allowed"


def test_no_login_is_needed(client: Client, providers: dict[str, mock.Mock]) -> None:
    assert post(client, payload()).status_code == 201  # anonymous client


def test_trip_creation_is_rate_limited(client: Client, providers: dict[str, mock.Mock], settings: Settings) -> None:
    rates = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {"trip_create": "2/min"}}
    with mock.patch("rest_framework.throttling.SimpleRateThrottle.THROTTLE_RATES", rates["DEFAULT_THROTTLE_RATES"]):
        assert post(client, payload()).status_code == 201
        assert post(client, payload()).status_code == 201
        res = post(client, payload())
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "throttled"
    assert client.get("/api/trips/1/").status_code in (200, 404)  # reading isn't limited


# --- CORS ----------------------------------------------------------------------------------------


def test_requests_from_the_frontend_are_allowed(client: Client, providers: dict[str, mock.Mock]) -> None:
    res = post(client, payload(), origin=FRONTEND)
    assert res.headers["Access-Control-Allow-Origin"] == FRONTEND


def test_preflight_from_the_frontend_is_allowed(client: Client) -> None:
    res = client.options(
        "/api/trips/",
        HTTP_ORIGIN=FRONTEND,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
    )
    assert res.headers["Access-Control-Allow-Origin"] == FRONTEND
    assert "POST" in res.headers["Access-Control-Allow-Methods"]


def test_requests_from_other_sites_are_not_allowed(client: Client) -> None:
    res = client.get("/api/trips/1/", HTTP_ORIGIN="https://evil.example")
    assert "Access-Control-Allow-Origin" not in res.headers

"""B12 heatmap API. Providers are faked (tests/fakes.py)."""

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client

from apps.routing import trips
from apps.routing.errors import NoRoute
from apps.routing.models import Trip, TripHeatmap
from apps.routing.tests.fakes import CHICAGO, DENVER, chicago_denver_routes, weather_get

pytestmark = pytest.mark.django_db


def body(hours_ahead: float = 2) -> dict[str, Any]:
    return {
        "origin": {"name": "Chicago, IL, USA", "lat": CHICAGO[1], "lng": CHICAGO[0]},
        "destination": {"name": "Denver, CO, USA", "lat": DENVER[1], "lng": DENVER[0]},
        "departure_at": (datetime.now(UTC) + timedelta(hours=hours_ahead)).isoformat(),
        "load_weight_lb": 32_000,
        "interval_miles": 25,
    }


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def trip_id(client: Client) -> Iterator[int]:
    with (
        mock.patch.object(trips, "plan_routes", return_value=chicago_denver_routes()),
        mock.patch("requests.get", weather_get()),
    ):
        res = client.post("/api/trips/", body(), content_type="application/json")
        assert res.status_code == 201
        yield int(res.json()["id"])


def test_a_ready_trip_returns_200_with_the_heatmap(client: Client, trip_id: int) -> None:
    with mock.patch("requests.get", weather_get()):
        res = client.get(f"/api/trips/{trip_id}/heatmap/")
    assert res.status_code == 200
    data = res.json()
    assert data["trip_id"] == trip_id
    assert len(data["hours"]) == 49
    assert 0 < len(data["points"]) <= 400
    assert all(len(row) == 49 for row in data["levels"])
    assert data["truck"]["route_index"] in (0, 1, 2)


def test_the_second_request_reads_the_saved_heatmap_without_provider_calls(client: Client, trip_id: int) -> None:
    with mock.patch("requests.get", weather_get()):
        first = client.get(f"/api/trips/{trip_id}/heatmap/")
    with mock.patch("requests.get") as provider:
        second = client.get(f"/api/trips/{trip_id}/heatmap/")
    provider.assert_not_called()
    assert second.json() == first.json()
    assert TripHeatmap.objects.count() == 1


def test_unknown_trip_is_404_in_the_error_shape(client: Client) -> None:
    res = client.get("/api/trips/999999/heatmap/")
    assert res.status_code == 404
    assert res.json() == {"error": {"code": "not_found", "message": "Trip not found."}}


def test_a_failed_trip_is_409_with_its_message(client: Client) -> None:
    with mock.patch.object(trips, "plan_routes", side_effect=NoRoute()):
        assert client.post("/api/trips/", body(), content_type="application/json").status_code == 422
    failed = Trip.objects.get()
    res = client.get(f"/api/trips/{failed.pk}/heatmap/")
    assert res.status_code == 409
    assert res.json() == {"error": {"code": "trip_has_no_routes", "message": "Trip has no routes."}}
    assert not TripHeatmap.objects.exists()


def test_the_response_is_sent_compressed(client: Client, trip_id: int) -> None:
    with mock.patch("requests.get", weather_get()):
        res = client.get(f"/api/trips/{trip_id}/heatmap/", headers={"Accept-Encoding": "gzip"})
    assert res.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in res.headers["Vary"]
    plain = json.loads(gzip.decompress(res.content))
    assert plain["trip_id"] == trip_id
    assert len(res.content) < 500 * 1024
    assert len(res.content) < len(gzip.decompress(res.content)) / 5


def test_clients_without_gzip_get_plain_json(client: Client, trip_id: int) -> None:
    with mock.patch("requests.get", weather_get()):
        res = client.get(f"/api/trips/{trip_id}/heatmap/", headers={"Accept-Encoding": "identity"})
    assert "Content-Encoding" not in res.headers
    assert res.json()["trip_id"] == trip_id


def test_the_trip_response_is_compressed_too(client: Client, trip_id: int) -> None:
    res = client.get(f"/api/trips/{trip_id}/", headers={"Accept-Encoding": "gzip"})
    assert res.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(res.content))["id"] == trip_id


def test_small_responses_are_left_alone(client: Client) -> None:
    res = client.get("/api/health/", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in res.headers


def test_cors_allows_only_the_frontend(client: Client, trip_id: int) -> None:
    with mock.patch("requests.get", weather_get()):
        allowed = client.get(f"/api/trips/{trip_id}/heatmap/", headers={"Origin": "http://localhost:3000"})
    other = client.get(f"/api/trips/{trip_id}/heatmap/", headers={"Origin": "https://evil.example"})
    assert allowed.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "Access-Control-Allow-Origin" not in other.headers


def test_the_heatmap_endpoint_only_accepts_get(client: Client, trip_id: int) -> None:
    res = client.post(f"/api/trips/{trip_id}/heatmap/", {}, content_type="application/json")
    assert res.status_code == 405
    assert res.json()["error"]["code"] == "method_not_allowed"

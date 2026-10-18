"""B4 geocoding: tests use a faked provider (a saved real Pelias response); no real calls."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import requests
from django.core.cache import cache
from django.test import Client
from pytest_django.fixtures import Settings as SettingsWrapper

from apps.routing import geocoding
from apps.routing.errors import ProviderTimeout, ProviderUnavailable
from apps.routing.geocoding import Place, parse_features, suggest

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pelias_autocomplete_denver.json").read_text("utf-8"))

pytestmark = pytest.mark.django_db


def fake_response(status: int = 200, body: Any = FIXTURE) -> mock.Mock:
    response = mock.Mock(spec=requests.Response)
    response.status_code = status
    response.ok = 200 <= status < 400
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    return response


@pytest.fixture(autouse=True)
def configured(settings: SettingsWrapper) -> Iterator[None]:
    settings.ORS_API_KEY = "test-key"
    settings.ORS_BASE_URL = "https://geocoder.test"
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def provider() -> Iterator[mock.Mock]:
    with mock.patch("requests.get", return_value=fake_response()) as get:
        yield get


# --- Acceptance criteria -----------------------------------------------------


def test_denver_returns_denver_co_with_coordinates(provider: mock.Mock) -> None:
    places = suggest("Denver")
    assert places[0].name == "Denver, CO, USA"
    assert places[0].lat == pytest.approx(39.74, abs=0.05)
    assert places[0].lng == pytest.approx(-104.99, abs=0.05)
    assert len(places) <= geocoding.MAX_RESULTS


@pytest.mark.parametrize("query", ["", "  ", "De", " De ", "d e", " d  e "])
def test_fewer_than_three_characters_returns_empty_without_calling_provider(provider: mock.Mock, query: str) -> None:
    assert suggest(query) == []
    provider.assert_not_called()


def test_request_is_us_only_with_timeout_and_key_in_header(provider: mock.Mock) -> None:
    suggest("Denver")
    _, kwargs = provider.call_args
    assert provider.call_args.args[0] == "https://geocoder.test/pelias/v1/autocomplete"
    assert kwargs["params"]["boundary.country"] == "US"
    assert kwargs["params"]["size"] == 5
    assert kwargs["timeout"] == geocoding.TIMEOUT_SECONDS
    assert kwargs["headers"]["Authorization"] == "test-key"
    assert "test-key" not in str(kwargs["params"])


# --- Caching -------------------------------------------------------------------


def test_identical_searches_are_cached(provider: mock.Mock) -> None:
    first = suggest("Denver")
    second = suggest("  denver ")
    assert first == second
    assert provider.call_count == 1


def test_cache_expires_after_a_day(provider: mock.Mock) -> None:
    with mock.patch.object(geocoding, "_now", return_value=1_000_000.0):
        suggest("Denver")
    with mock.patch.object(geocoding, "_now", return_value=1_000_000.0 + geocoding.FRESH_SECONDS + 1):
        suggest("Denver")
    assert provider.call_count == 2


def test_provider_failure_serves_stale_cached_results(provider: mock.Mock) -> None:
    with mock.patch.object(geocoding, "_now", return_value=1_000_000.0):
        cached = suggest("Denver")
    provider.side_effect = requests.ConnectionError("down")
    with mock.patch.object(geocoding, "_now", return_value=1_000_000.0 + geocoding.FRESH_SECONDS + 1):
        assert suggest("Denver") == cached


def test_broken_cache_does_not_break_search(provider: mock.Mock) -> None:
    with (
        mock.patch.object(cache, "get", side_effect=RuntimeError("cache down")),
        mock.patch.object(cache, "set", side_effect=RuntimeError("cache down")),
    ):
        assert suggest("Denver")[0].name == "Denver, CO, USA"


def test_corrupt_cache_entry_is_ignored(provider: mock.Mock) -> None:
    cache.set(geocoding._cache_key("Denver"), {"places": "nonsense"})
    assert suggest("Denver")[0].name == "Denver, CO, USA"
    assert provider.call_count == 1


# --- Provider failures ---------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (requests.Timeout("slow"), ProviderTimeout),
        (requests.ConnectionError("down"), ProviderUnavailable),
    ],
)
def test_network_failures_raise_provider_errors(failure: Exception, expected: type[Exception]) -> None:
    with mock.patch("requests.get", side_effect=failure), pytest.raises(expected):
        suggest("Denver")


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_bad_statuses_raise_provider_unavailable(status: int) -> None:
    with (
        mock.patch("requests.get", return_value=fake_response(status, {})),
        pytest.raises(ProviderUnavailable),
    ):
        suggest("Denver")


def test_non_json_body_raises_provider_unavailable() -> None:
    with (
        mock.patch("requests.get", return_value=fake_response(200, ValueError("not json"))),
        pytest.raises(ProviderUnavailable),
    ):
        suggest("Denver")


def test_missing_key_raises_without_calling_provider(settings: SettingsWrapper, provider: mock.Mock) -> None:
    settings.ORS_API_KEY = ""
    with pytest.raises(ProviderUnavailable):
        suggest("Denver")
    provider.assert_not_called()


# --- Parsing -------------------------------------------------------------------


def test_malformed_features_are_skipped_and_duplicates_dropped() -> None:
    good = {"properties": {"label": "Boulder, CO, USA"}, "geometry": {"coordinates": [-105.27, 40.01]}}
    data = {
        "features": [
            "not a feature",
            {"properties": "label", "geometry": {"coordinates": [1, 2]}},
            {"properties": {}, "geometry": {"coordinates": [1, 2]}},
            {"properties": {"label": "No coords"}, "geometry": {}},
            {"properties": {"label": "Bad lat"}, "geometry": {"coordinates": [-105.0, 95.0]}},
            {"properties": {"label": "NaN"}, "geometry": {"coordinates": [float("nan"), 40.0]}},
            {"properties": {"label": "Bool"}, "geometry": {"coordinates": [True, 40.0]}},
            good,
            {"properties": {"label": "boulder, co, usa"}, "geometry": {"coordinates": [-105.0, 40.0]}},
        ]
    }
    assert parse_features(data) == [Place("Boulder, CO, USA", 40.01, -105.27)]


@pytest.mark.parametrize("data", [None, [], "text", {"features": "nope"}, {}])
def test_unexpected_response_shapes_give_no_results(data: object) -> None:
    assert parse_features(data) == []


def test_results_are_capped_at_five() -> None:
    feature = {"geometry": {"coordinates": [-100.0, 40.0]}}
    data = {"features": [{**feature, "properties": {"label": f"Place {i}"}} for i in range(9)]}
    assert len(parse_features(data)) == 5


# --- Endpoint ------------------------------------------------------------------


def test_endpoint_returns_results(client: Client, provider: mock.Mock) -> None:
    res = client.get("/api/places/", {"q": "Denver"})
    assert res.status_code == 200
    first = res.json()["results"][0]
    assert first["name"] == "Denver, CO, USA"
    assert set(first) == {"name", "lat", "lng"}


def test_endpoint_short_query_is_empty(client: Client, provider: mock.Mock) -> None:
    res = client.get("/api/places/", {"q": "De"})
    assert res.status_code == 200
    assert res.json() == {"results": []}
    provider.assert_not_called()


def test_endpoint_missing_query_is_a_field_error(client: Client) -> None:
    res = client.get("/api/places/")
    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "invalid_input"
    assert "q" in error["fields"]


def test_endpoint_overlong_query_is_a_field_error(client: Client, provider: mock.Mock) -> None:
    res = client.get("/api/places/", {"q": "x" * 101})
    assert res.status_code == 400
    assert "q" in res.json()["error"]["fields"]
    provider.assert_not_called()


def test_endpoint_provider_down_is_502_with_readable_message(client: Client) -> None:
    with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
        res = client.get("/api/places/", {"q": "Denver"})
    assert res.status_code == 502
    assert res.json() == {"error": {"code": "provider_unavailable", "message": geocoding.UNAVAILABLE_MESSAGE}}


def test_endpoint_provider_slow_is_504_with_readable_message(client: Client) -> None:
    with mock.patch("requests.get", side_effect=requests.Timeout("slow")):
        res = client.get("/api/places/", {"q": "Denver"})
    assert res.status_code == 504
    assert res.json() == {"error": {"code": "provider_timeout", "message": geocoding.TIMEOUT_MESSAGE}}


def test_endpoint_never_leaks_the_key(client: Client, provider: mock.Mock) -> None:
    res = client.get("/api/places/", {"q": "Denver"})
    assert b"test-key" not in res.content


def test_unexpected_error_is_a_plain_500_in_the_error_shape(client: Client) -> None:
    with mock.patch.object(geocoding, "suggest", side_effect=RuntimeError("bug")):
        res = client.get("/api/places/", {"q": "Denver"})
    assert res.status_code == 500
    assert res.json()["error"]["code"] == "server_error"
    assert "bug" not in res.content.decode()

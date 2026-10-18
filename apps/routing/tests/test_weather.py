"""B7 weather provider and cache. Tests use a faked provider built from a saved real Open-Meteo response."""

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import requests
from django.utils import timezone

from apps.routing import weather
from apps.routing.checkpoints import CheckpointPoint
from apps.routing.choices import RiskLevel
from apps.routing.models import ForecastCache
from apps.routing.weather import (
    BEYOND_FORECAST_NOTE,
    UNAVAILABLE_NOTE,
    HourlyForecast,
    celsius_to_fahrenheit,
    checkpoint_weather,
    cm_to_inches,
    describe_weather_code,
    mm_to_inches,
    ms_to_mph,
    parse_location,
)

pytestmark = pytest.mark.django_db

REAL = json.loads((Path(__file__).parent / "fixtures" / "weather" / "open_meteo_3_points.json").read_text("utf-8"))
ERROR = json.loads((Path(__file__).parent / "fixtures" / "weather" / "open_meteo_error.json").read_text("utf-8"))
START = datetime.fromtimestamp(REAL[0]["hourly"]["time"][0], tz=UTC)  # first forecast hour (00:00 UTC)


def location(
    wind_ms: float = 3.0, rain_mm: float = 0.0, showers_mm: float = 0.0, snow_cm: float = 0.0
) -> dict[str, Any]:
    """A real Open-Meteo location with every hour's values replaced by the given ones."""
    loc: dict[str, Any] = copy.deepcopy(REAL[0])
    hours = len(loc["hourly"]["time"])
    loc["hourly"].update(
        wind_speed_10m=[wind_ms] * hours,
        rain=[rain_mm] * hours,
        showers=[showers_mm] * hours,
        snowfall=[snow_cm] * hours,
    )
    return loc


def fake_response(status: int = 200, body: Any = None) -> mock.Mock:
    res = mock.Mock(spec=requests.Response)
    res.status_code, res.ok = status, 200 <= status < 400
    res.text = "<html>" if isinstance(body, Exception) else json.dumps(body)[:200]
    if isinstance(body, Exception):
        res.json.side_effect = body
    else:
        res.json.return_value = body
    return res


def provider_answering(**loc_kwargs: float) -> mock.Mock:
    """A fake requests.get that returns one location per requested point."""

    def get(url: str, *, params: dict[str, Any], timeout: float) -> mock.Mock:
        count = len(str(params["latitude"]).split(","))
        return fake_response(200, [location(**loc_kwargs) for _ in range(count)])

    return mock.Mock(side_effect=get)


def checkpoint(lat: float, lng: float, eta: datetime, sequence: int = 0) -> CheckpointPoint:
    return CheckpointPoint(sequence, sequence * 25.0, lng, lat, (eta - START).total_seconds(), eta)


def spread_checkpoints(count: int, eta: datetime) -> list[CheckpointPoint]:
    """`count` checkpoints in distinct ~1 km cells along a line."""
    return [checkpoint(40.0, -100.0 + i * 0.05, eta, i) for i in range(count)]


@pytest.fixture
def provider() -> Iterator[mock.Mock]:
    fake = provider_answering()
    with mock.patch("requests.get", fake):
        yield fake


# --- Acceptance criteria -----------------------------------------------------------------


def test_sixty_checkpoints_cold_cache_take_at_most_three_requests(provider: mock.Mock) -> None:
    results = checkpoint_weather(spread_checkpoints(60, START + timedelta(hours=10)), 30_000)
    assert len(results) == 60
    assert provider.call_count <= 3
    assert all(r.risk.level is not None for r in results)


def test_repeating_within_the_hour_makes_no_requests(provider: mock.Mock) -> None:
    points = spread_checkpoints(60, START + timedelta(hours=10))
    checkpoint_weather(points, 30_000)
    provider.reset_mock()
    checkpoint_weather(points, 30_000)
    provider.assert_not_called()


def test_cache_older_than_an_hour_is_refetched(provider: mock.Mock) -> None:
    points = spread_checkpoints(3, START + timedelta(hours=10))
    checkpoint_weather(points, 30_000)
    ForecastCache.objects.update(fetched_at=timezone.now() - timedelta(minutes=61))
    provider.reset_mock()
    checkpoint_weather(points, 30_000)
    assert provider.call_count == 1


@pytest.mark.parametrize(
    ("arrival", "expected_hour"),
    [
        ("14:40", 15),
        ("14:20", 14),
        ("14:30", 15),  # half past rounds up
        ("14:00", 14),
        ("14:59", 15),
    ],
)
def test_checkpoint_uses_the_nearest_forecast_hour(provider: mock.Mock, arrival: str, expected_hour: int) -> None:
    hour, minute = (int(x) for x in arrival.split(":"))
    eta = START + timedelta(days=1, hours=hour, minutes=minute)
    [result] = checkpoint_weather([checkpoint(40.0, -100.0, eta)], 30_000)
    assert result.forecast_time == START + timedelta(days=1, hours=expected_hour)


def test_unit_conversions_against_known_values() -> None:
    assert ms_to_mph(10) == pytest.approx(22.4, abs=0.05)
    assert mm_to_inches(25.4) == pytest.approx(1.00)
    assert cm_to_inches(2.54) == pytest.approx(1.00)
    assert celsius_to_fahrenheit(0) == 32 and celsius_to_fahrenheit(100) == 212
    assert ms_to_mph(None) is mm_to_inches(None) is cm_to_inches(None) is celsius_to_fahrenheit(None) is None


def test_arrival_after_the_forecast_ends_is_unknown_with_note(provider: mock.Mock) -> None:
    [result] = checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(days=30))], 30_000)
    assert result.risk.level is None
    assert result.risk.data_issues == (BEYOND_FORECAST_NOTE,)


def test_arrival_before_the_forecast_starts_is_unknown_with_note(provider: mock.Mock) -> None:
    [result] = checkpoint_weather([checkpoint(40.0, -100.0, START - timedelta(hours=2))], 30_000)
    assert result.risk.data_issues == (BEYOND_FORECAST_NOTE,)


def test_one_failed_batch_leaves_its_checkpoints_unknown_and_the_rest_scored() -> None:
    calls = {"n": 0}

    def get(url: str, *, params: dict[str, Any], timeout: float) -> mock.Mock:
        calls["n"] += 1
        if str(params["latitude"]).count(",") + 1 == weather.BATCH_SIZE:  # the first, full batch fails
            raise requests.Timeout("slow")
        count = len(str(params["latitude"]).split(","))
        return fake_response(200, [location() for _ in range(count)])

    points = spread_checkpoints(60, START + timedelta(hours=10))
    with mock.patch("requests.get", side_effect=get):
        results = checkpoint_weather(points, 30_000)
    unknown = [r for r in results if r.risk.level is None]
    assert len(results) == 60
    assert len(unknown) == weather.BATCH_SIZE
    assert all(r.risk.data_issues == (UNAVAILABLE_NOTE,) for r in unknown)
    assert sum(r.risk.level is not None for r in results) == 60 - weather.BATCH_SIZE


# --- Values and risk ----------------------------------------------------------------------------


def test_real_response_parses_in_our_units() -> None:
    forecast = parse_location(REAL[0], timezone.now())
    assert forecast is not None
    assert len(forecast.times) == 120
    hourly = REAL[0]["hourly"]
    assert forecast.wind_mph[0] == pytest.approx(hourly["wind_speed_10m"][0] * weather.MPH_PER_MS)
    expected_rain = (hourly["rain"][0] + hourly["showers"][0]) / 25.4
    assert forecast.rain_in_per_hr[0] == pytest.approx(expected_rain)
    assert forecast.snow_in_per_hr[0] == pytest.approx(hourly["snowfall"][0] / 2.54)


def test_rain_is_rain_plus_showers() -> None:
    forecast = parse_location(location(rain_mm=3.0, showers_mm=4.0), timezone.now())
    assert forecast is not None
    assert forecast.rain_in_per_hr[0] == pytest.approx(7.0 / 25.4)


def test_values_are_scored_with_the_load_rules() -> None:
    windy = provider_answering(wind_ms=47 / weather.MPH_PER_MS)  # 47 mph
    with mock.patch("requests.get", windy):
        light, heavy = (
            checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], load)[0]
            for load in (20_000, 32_000)
        )
    assert light.wind_mph == pytest.approx(47)
    assert light.risk.level == RiskLevel.SEVERE
    assert heavy.risk.level == RiskLevel.NO_TRAVEL


def test_heavy_snow_is_severe() -> None:
    snowy = provider_answering(snow_cm=2.5 * 2.54)  # 2.5 in/hr
    with mock.patch("requests.get", snowy):
        [result] = checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], 10_000)
    assert result.snow_in_per_hr == pytest.approx(2.5)
    assert result.risk.level == RiskLevel.SEVERE


def test_nearby_checkpoints_share_one_forecast(provider: mock.Mock) -> None:
    eta = START + timedelta(hours=5)
    checkpoint_weather([checkpoint(40.0001, -100.0001, eta), checkpoint(40.0002, -100.0003, eta, 1)], 1)
    assert str(provider.call_args.kwargs["params"]["latitude"]) == "40.00"


def test_request_asks_for_si_units_unix_time_and_has_a_timeout(provider: mock.Mock) -> None:
    checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], 1)
    params, timeout = provider.call_args.kwargs["params"], provider.call_args.kwargs["timeout"]
    assert (params["wind_speed_unit"], params["precipitation_unit"], params["timeformat"]) == ("ms", "mm", "unixtime")
    assert timeout == weather.REQUEST_TIMEOUT_SECONDS


def test_weather_descriptions() -> None:
    assert describe_weather_code(73) == "Snow"
    assert describe_weather_code(None) == describe_weather_code(1234) == ""


# --- Fallbacks -----------------------------------------------------------------------------------


def seed_cache(age: timedelta, wind_ms: float = 20 / weather.MPH_PER_MS) -> None:
    fetched = timezone.now() - age
    forecast = parse_location(location(wind_ms=wind_ms), fetched)
    assert forecast is not None
    weather._write_cache({(40.0, -100.0): forecast}, fetched)
    ForecastCache.objects.update(fetched_at=fetched)


def test_provider_down_uses_a_recent_stale_forecast_with_a_note() -> None:
    seed_cache(timedelta(hours=3))
    with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
        [result] = checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], 1)
    assert result.wind_mph == pytest.approx(20)
    assert result.risk.level == RiskLevel.LOW
    assert any("from about 3 h ago" in note for note in result.risk.data_issues)


def test_provider_down_and_old_cache_is_unknown() -> None:
    seed_cache(timedelta(hours=7))
    with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
        [result] = checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], 1)
    assert result.risk.level is None
    assert result.risk.data_issues == (UNAVAILABLE_NOTE,)


@pytest.mark.parametrize(
    "res",
    [
        fake_response(400, ERROR),
        fake_response(429, {"error": True, "reason": "Too many requests"}),
        fake_response(500, {}),
        fake_response(200, ValueError("html")),
        fake_response(200, []),  # wrong number of locations
        fake_response(200, [{"hourly": "nonsense"}]),
    ],
)
def test_bad_provider_answers_leave_checkpoints_unknown(res: mock.Mock) -> None:
    with mock.patch("requests.get", return_value=res):
        [result] = checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], 1)
    assert result.risk.level is None
    assert result.risk.data_issues == (UNAVAILABLE_NOTE,)


def test_single_location_answer_as_an_object_is_accepted() -> None:
    with mock.patch("requests.get", return_value=fake_response(200, location())):
        [result] = checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], 1)
    assert result.risk.level == RiskLevel.LOW


def test_missing_series_count_as_missing_values() -> None:
    loc = location()
    del loc["hourly"]["snowfall"]
    loc["hourly"]["rain"] = loc["hourly"]["rain"][:3]  # short series
    forecast = parse_location(loc, timezone.now())
    assert forecast is not None
    assert forecast.snow_in_per_hr[0] is None
    assert forecast.rain_in_per_hr[10] == 0.0  # rain missing past hour 3, but showers (0 mm) still count
    assert len(forecast.rain_in_per_hr) == len(forecast.times)


def test_odd_values_become_missing_not_zero() -> None:
    loc = location()
    loc["hourly"]["wind_speed_10m"][0] = float("nan")
    loc["hourly"]["wind_speed_10m"][1] = True
    loc["hourly"]["wind_speed_10m"][2] = "7"
    loc["hourly"]["rain"][0] = None
    loc["hourly"]["showers"][0] = None
    forecast = parse_location(loc, timezone.now())
    assert forecast is not None
    assert forecast.wind_mph[:3] == (None, None, None)
    assert forecast.rain_in_per_hr[0] is None


@pytest.mark.parametrize("bad", [None, "x", {"hourly": {"time": ["a", "b"]}}, {"hourly": {}}])
def test_malformed_locations_are_rejected(bad: Any) -> None:
    assert parse_location(bad, timezone.now()) is None


def test_corrupt_cache_rows_are_refetched(provider: mock.Mock) -> None:
    ForecastCache.objects.create(
        provider=weather.PROVIDER, lat="40.00", lng="-100.00", hourly={"times": "bad"}, expires_at=timezone.now()
    )
    [result] = checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], 1)
    assert provider.call_count == 1
    assert result.risk.level is not None


def test_broken_cache_does_not_stop_scoring(provider: mock.Mock) -> None:
    with (
        mock.patch.object(ForecastCache.objects, "filter", side_effect=RuntimeError("db down")),
        mock.patch.object(ForecastCache.objects, "bulk_create", side_effect=RuntimeError("db down")),
    ):
        [result] = checkpoint_weather([checkpoint(40.0, -100.0, START + timedelta(hours=5))], 1)
    assert result.risk.level is not None


def test_from_json_rejects_misaligned_series() -> None:
    good = parse_location(location(), timezone.now())
    assert good is not None
    data = good.to_json()
    assert HourlyForecast.from_json(data, good.fetched_at) == good
    data["wind_mph"] = data["wind_mph"][:5]
    assert HourlyForecast.from_json(data, good.fetched_at) is None
    assert HourlyForecast.from_json("nope", good.fetched_at) is None


def test_empty_forecast_has_no_hour() -> None:
    empty = HourlyForecast((), (), (), (), (), (), (), timezone.now())
    assert empty.hour_index(START) is None


def test_no_checkpoints_no_requests(provider: mock.Mock) -> None:
    assert checkpoint_weather([], 1) == []
    provider.assert_not_called()

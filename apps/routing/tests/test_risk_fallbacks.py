"""Fault tolerance of the risk engine: bad input never raises and never silently lowers risk."""

from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from apps.routing import risk
from apps.routing.choices import RiskLevel
from apps.routing.risk import RiskAssessment, WeatherSample, assess

LIGHT_LOAD_LB = 10_000


# --- Input coercion ---------------------------------------------------------


@pytest.mark.parametrize("raw", [47, 47.0, Decimal("47"), "47", " 47.0 "])
def test_numeric_types_and_numeric_strings_are_accepted(raw: float | Decimal | str) -> None:
    result = assess(WeatherSample(wind_mph=raw), 32_000)
    assert result.level == RiskLevel.NO_TRAVEL
    assert result.data_issues == ()


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_strings_count_as_missing(raw: str) -> None:
    result = assess(WeatherSample(wind_mph=raw, rain_in_per_hr=0.3), LIGHT_LOAD_LB)
    assert result.level == RiskLevel.HIGH
    assert result.missing == ("wind", "snow")
    assert result.data_issues == ()


@pytest.mark.parametrize(
    "raw",
    [
        "windy",
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        True,
        -5,
        9999,  # common provider "missing" sentinel, far beyond any real wind
        10**400,  # overflows float
        [60],
        object(),
    ],
)
def test_unusable_wind_is_ignored_and_flagged(raw: Any) -> None:
    result = assess(WeatherSample(wind_mph=raw, rain_in_per_hr=0.3), LIGHT_LOAD_LB)
    assert result.level == RiskLevel.HIGH  # still scored from rain
    assert len(result.data_issues) == 1
    assert result.data_issues[0].startswith("Ignored unusable wind value")
    assert not result.is_complete


def test_tiny_negative_from_rounding_reads_as_zero() -> None:
    result = assess(WeatherSample(wind_mph=10, rain_in_per_hr=-0.001, snow_in_per_hr=0), LIGHT_LOAD_LB)
    assert result.level == RiskLevel.LOW
    assert result.data_issues == ()


def test_all_values_unusable_is_unknown_with_issues() -> None:
    result = assess(WeatherSample(wind_mph="x", rain_in_per_hr=float("nan"), snow_in_per_hr=-3), LIGHT_LOAD_LB)
    assert result.level is None
    assert result.label == "Unknown"
    assert len(result.data_issues) == 3


def test_plausibility_limit_is_inclusive() -> None:
    assert assess(WeatherSample(wind_mph=risk.MAX_PLAUSIBLE_WIND_MPH), 1).level == RiskLevel.NO_TRAVEL
    assert assess(WeatherSample(wind_mph=risk.MAX_PLAUSIBLE_WIND_MPH + 1), 1).level is None


def test_very_long_bad_values_are_truncated_in_issues() -> None:
    result = assess(WeatherSample(wind_mph="x" * 500, rain_in_per_hr=0), LIGHT_LOAD_LB)
    assert len(result.data_issues[0]) < 100


def test_complete_input_is_complete() -> None:
    assert assess(WeatherSample(wind_mph=10, rain_in_per_hr=0, snow_in_per_hr=0), LIGHT_LOAD_LB).is_complete


# --- Load weight fallback ---------------------------------------------------


@pytest.mark.parametrize("load", [None, "heavy", float("nan"), -1, True, ""])
def test_unknown_load_is_assumed_heavy(load: Any) -> None:
    severe_band = assess(WeatherSample(wind_mph=47), load)
    high_band = assess(WeatherSample(wind_mph=40), load)
    assert severe_band.level == RiskLevel.NO_TRAVEL
    assert high_band.level == RiskLevel.SEVERE
    assert severe_band.reasons == ("Wind 47 mph with unknown (assumed heavy) load → No Travel",)
    assert any("Load weight unknown" in issue for issue in severe_band.data_issues)


@pytest.mark.parametrize("load", [0, "32000", Decimal("32000.0")])
def test_valid_load_forms_are_used_as_given(load: Any) -> None:
    result = assess(WeatherSample(wind_mph=47), load)
    assert not any("Load weight" in issue for issue in result.data_issues)


def test_unknown_load_does_not_matter_when_wind_is_calm() -> None:
    result = assess(WeatherSample(wind_mph=5, rain_in_per_hr=0, snow_in_per_hr=0), None)
    assert result.level == RiskLevel.LOW
    assert result.reasons == ()


# --- Malformed containers and the last-resort barrier -----------------------


def test_none_weather_is_unknown() -> None:
    assert assess(None, LIGHT_LOAD_LB).level is None


def test_non_sample_weather_is_unknown() -> None:
    not_a_sample: Any = {"wind_mph": 60}
    assert assess(not_a_sample, LIGHT_LOAD_LB).level is None


def test_from_mapping_reads_provider_dicts() -> None:
    sample = WeatherSample.from_mapping({"wind_mph": "47", "rain_in_per_hr": 0.3, "extra": "ignored"})
    assert sample == WeatherSample(wind_mph="47", rain_in_per_hr=0.3, snow_in_per_hr=None)
    assert assess(sample, 32_000).level == RiskLevel.NO_TRAVEL


@pytest.mark.parametrize("data", [None, [], "not a dict", 42])
def test_from_mapping_tolerates_non_dicts(data: Any) -> None:
    assert WeatherSample.from_mapping(data) == WeatherSample()


def test_from_mapping_flags_odd_value_types() -> None:
    sample = WeatherSample.from_mapping({"wind_mph": [60], "rain_in_per_hr": 0.3})
    result = assess(sample, LIGHT_LOAD_LB)
    assert result.level == RiskLevel.HIGH
    assert any("wind" in issue for issue in result.data_issues)


def test_unexpected_error_returns_unknown_instead_of_raising() -> None:
    with mock.patch.object(risk, "_assess", side_effect=RuntimeError("boom")):
        result = assess(WeatherSample(wind_mph=60), LIGHT_LOAD_LB)
    assert result == RiskAssessment(level=None, data_issues=("Risk could not be calculated for this checkpoint",))


# --- Properties over arbitrary input ----------------------------------------

anything = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.decimals(allow_nan=True, allow_infinity=True),
    st.text(max_size=20),
)
real_wind = st.floats(min_value=0, max_value=risk.MAX_PLAUSIBLE_WIND_MPH)
real_precip = st.floats(min_value=0, max_value=risk.MAX_PLAUSIBLE_RAIN_IN_PER_HR)
real_load = st.integers(min_value=0, max_value=200_000)


@given(wind=anything, rain=anything, snow=anything, load=anything)
def test_never_raises_and_always_returns_a_valid_result(wind: Any, rain: Any, snow: Any, load: Any) -> None:
    result = assess(WeatherSample(wind, rain, snow), load)
    assert result.level is None or result.level in RiskLevel
    assert result.level is None or result.level == RiskLevel.LOW or result.reasons


@given(wind=real_wind, extra=st.floats(min_value=0, max_value=50), rain=real_precip, load=real_load)
def test_more_wind_never_lowers_risk(wind: float, extra: float, rain: float, load: int) -> None:
    stronger = min(wind + extra, risk.MAX_PLAUSIBLE_WIND_MPH)
    base = assess(WeatherSample(wind_mph=wind, rain_in_per_hr=rain), load).level
    more = assess(WeatherSample(wind_mph=stronger, rain_in_per_hr=rain), load).level
    assert base is not None and more is not None
    assert more >= base


@given(wind=real_wind, load=real_load, extra=st.integers(min_value=0, max_value=100_000))
def test_heavier_load_never_lowers_risk(wind: float, load: int, extra: int) -> None:
    lighter = assess(WeatherSample(wind_mph=wind), load).level
    heavier = assess(WeatherSample(wind_mph=wind), load + extra).level
    assert lighter is not None and heavier is not None
    assert heavier >= lighter


@given(wind=real_wind, load=anything)
def test_unknown_load_is_never_safer_than_any_given_load(wind: float, load: Any) -> None:
    assumed = assess(WeatherSample(wind_mph=wind), None).level
    with_load = assess(WeatherSample(wind_mph=wind), load).level
    assert assumed is not None and with_load is not None
    assert assumed >= with_load


@given(wind=real_wind, rain=real_precip, snow=real_precip, load=real_load)
def test_level_is_the_worst_single_factor(wind: float, rain: float, snow: float, load: int) -> None:
    combined = assess(WeatherSample(wind, rain, snow), load).level
    singles = [
        assess(WeatherSample(wind_mph=wind), load).level,
        assess(WeatherSample(rain_in_per_hr=rain), load).level,
        assess(WeatherSample(snow_in_per_hr=snow), load).level,
    ]
    assert combined == max(level for level in singles if level is not None)

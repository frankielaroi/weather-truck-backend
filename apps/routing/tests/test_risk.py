import pytest

from apps.routing.choices import RiskLevel
from apps.routing.risk import RiskAssessment, WeatherSample, assess

LIGHT_LOAD_LB = 10_000

LOW = RiskLevel.LOW
MODERATE = RiskLevel.MODERATE
HIGH = RiskLevel.HIGH
SEVERE = RiskLevel.SEVERE
NO_TRAVEL = RiskLevel.NO_TRAVEL


@pytest.mark.parametrize(
    ("wind_mph", "expected"),
    [
        (24.9, LOW),
        (25, MODERATE),
        (34.9, MODERATE),
        (35, HIGH),
        (44.9, HIGH),
        (45, SEVERE),
        (54.9, SEVERE),
        (55, NO_TRAVEL),
    ],
)
def test_wind_boundaries(wind_mph: float, expected: RiskLevel) -> None:
    assert assess(WeatherSample(wind_mph=wind_mph), LIGHT_LOAD_LB).level == expected


@pytest.mark.parametrize(
    ("rain_in_per_hr", "expected"),
    [
        (0.09, LOW),
        (0.10, MODERATE),
        (0.24, MODERATE),
        (0.25, HIGH),
        (0.49, HIGH),
        (0.50, SEVERE),
        (1.00, SEVERE),
        (1.01, NO_TRAVEL),
    ],
)
def test_rain_boundaries(rain_in_per_hr: float, expected: RiskLevel) -> None:
    assert assess(WeatherSample(rain_in_per_hr=rain_in_per_hr), LIGHT_LOAD_LB).level == expected


@pytest.mark.parametrize(
    ("snow_in_per_hr", "expected"),
    [
        (0.49, LOW),
        (0.5, MODERATE),
        (0.99, MODERATE),
        (1.0, HIGH),
        (1.99, HIGH),
        (2.0, SEVERE),
        (3.0, SEVERE),
        (3.01, NO_TRAVEL),
    ],
)
def test_snow_boundaries(snow_in_per_hr: float, expected: RiskLevel) -> None:
    assert assess(WeatherSample(snow_in_per_hr=snow_in_per_hr), LIGHT_LOAD_LB).level == expected


@pytest.mark.parametrize(
    ("wind_mph", "load_weight_lb", "expected"),
    [
        (47, 30_000, SEVERE),
        (47, 30_001, NO_TRAVEL),
        (40, 40_000, HIGH),
        (40, 40_001, SEVERE),
        (60, 1, NO_TRAVEL),
    ],
)
def test_load_rules(wind_mph: float, load_weight_lb: int, expected: RiskLevel) -> None:
    assert assess(WeatherSample(wind_mph=wind_mph), load_weight_lb).level == expected


def test_heavy_load_does_not_raise_light_wind() -> None:
    assert assess(WeatherSample(wind_mph=30), 80_000).level == MODERATE


def test_worst_factor_wins() -> None:
    result = assess(WeatherSample(wind_mph=10, rain_in_per_hr=0, snow_in_per_hr=2.5), LIGHT_LOAD_LB)
    assert result.level == SEVERE


def test_all_missing_is_unknown() -> None:
    result = assess(WeatherSample(), LIGHT_LOAD_LB)
    assert result == RiskAssessment(level=None, reasons=())
    assert result.label == "Unknown"


def test_partly_missing_still_scores() -> None:
    result = assess(WeatherSample(wind_mph=None, rain_in_per_hr=0.3, snow_in_per_hr=None), LIGHT_LOAD_LB)
    assert result.level == HIGH
    assert result.reasons == ("Rain 0.3 in/hr → High",)


def test_low_has_no_reasons() -> None:
    result = assess(WeatherSample(wind_mph=5, rain_in_per_hr=0.01, snow_in_per_hr=0), LIGHT_LOAD_LB)
    assert result.level == LOW
    assert result.reasons == ()


def test_load_rule_reason_names_wind_and_load() -> None:
    result = assess(WeatherSample(wind_mph=47), 32_000)
    assert result.reasons == ("Wind 47 mph with 32,000 lb load → No Travel",)


def test_reason_without_load_rule_omits_load() -> None:
    assert assess(WeatherSample(wind_mph=47), 20_000).reasons == ("Wind 47 mph → Severe",)


def test_every_factor_at_moderate_or_above_gets_a_reason() -> None:
    result = assess(WeatherSample(wind_mph=26, rain_in_per_hr=0.6, snow_in_per_hr=1.2), LIGHT_LOAD_LB)
    assert result.level == SEVERE
    assert result.reasons == (
        "Wind 26 mph → Moderate",
        "Rain 0.6 in/hr → Severe",
        "Snow 1.2 in/hr → High",
    )


@pytest.mark.parametrize(
    "weather",
    [
        WeatherSample(wind_mph=25),
        WeatherSample(rain_in_per_hr=0.10),
        WeatherSample(snow_in_per_hr=0.5),
        WeatherSample(wind_mph=55),
        WeatherSample(rain_in_per_hr=1.01),
        WeatherSample(snow_in_per_hr=3.01),
    ],
)
def test_every_result_above_low_names_a_factor_and_value(weather: WeatherSample) -> None:
    result = assess(weather, LIGHT_LOAD_LB)
    assert result.level is not None
    assert result.level > LOW
    assert result.reasons
    value = next(v for v in (weather.wind_mph, weather.rain_in_per_hr, weather.snow_in_per_hr) if v is not None)
    assert any(f"{value:g}" in reason for reason in result.reasons)
    assert any(reason.split()[0] in {"Wind", "Rain", "Snow"} for reason in result.reasons)


def test_same_input_same_output() -> None:
    weather = WeatherSample(wind_mph=47, rain_in_per_hr=0.3, snow_in_per_hr=1.5)
    assert assess(weather, 35_000) == assess(weather, 35_000)

"""Weather risk engine: scores one checkpoint's weather against the truck's load.

Pure logic — no database, network or clock — so the same input always gives the same output.

Rules (from the assessment):

    Condition  Low        Moderate    High        Severe      No Travel
    Wind       <25 mph    25–34       35–44       45–54       ≥55 mph
    Rain       <0.10 in/h 0.10–0.25   0.25–0.50   0.50–1.00   >1.00
    Snow       <0.5 in/h  0.5–1.0     1.0–2.0     2.0–3.0     >3.0

    Load rules (applied to the wind level):
      wind ≥55 mph                    → No Travel for any load
      wind 45–54 mph and load >30,000 → No Travel
      wind 35–44 mph and load >40,000 → Severe

Interpretation decisions:
- A value on a shared edge belongs to the higher level (rain 0.25 is High), except at the top of the
  scale, where No Travel starts strictly above the Severe range (rain 1.00 and snow 3.0 are Severe).
- Wind rules use sustained wind speed; gusts are stored and shown but don't set the level.
- "> 30,000 lb" / "> 40,000 lb" are strict: exactly 30,000 / 40,000 lb doesn't trigger the rule.
- A missing value counts as zero for that factor. If every value is missing, the level is Unknown (None).

Fault tolerance — `assess` never raises, and bad data never lowers the risk silently:
- Weather values may arrive as int, float, Decimal or numeric strings; all are accepted.
- A weather value that isn't a usable number (text, bool, NaN, infinity, a negative reading, or beyond
  the physically plausible maximum — typical of provider sentinels like 9999) is ignored like a missing
  value, and the result records it in `data_issues` so callers can show the checkpoint as unreliable.
  Tiny negatives from rounding (down to -NEGATIVE_NOISE) are read as 0.
- An unusable load weight (missing, text, NaN, negative) is assumed to be heavy — over both load
  limits — so the load rules err toward caution. This is also recorded in `data_issues`.
- Any unexpected error yields an Unknown result with a data issue instead of an exception.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from operator import ge, gt
from typing import Final

from apps.routing.choices import RiskLevel

logger = logging.getLogger(__name__)

UNKNOWN_LABEL: Final = "Unknown"

# (lower bound, comparison, level), checked from worst to best; the first bound the value
# meets wins. `ge` means the bound itself is in that level, `gt` means it's in the level below.
type _Threshold = tuple[float, Callable[[float, float], bool], RiskLevel]

WIND_MPH_THRESHOLDS: Final[tuple[_Threshold, ...]] = (
    (55, ge, RiskLevel.NO_TRAVEL),
    (45, ge, RiskLevel.SEVERE),
    (35, ge, RiskLevel.HIGH),
    (25, ge, RiskLevel.MODERATE),
)
RAIN_IN_PER_HR_THRESHOLDS: Final[tuple[_Threshold, ...]] = (
    (1.00, gt, RiskLevel.NO_TRAVEL),
    (0.50, ge, RiskLevel.SEVERE),
    (0.25, ge, RiskLevel.HIGH),
    (0.10, ge, RiskLevel.MODERATE),
)
SNOW_IN_PER_HR_THRESHOLDS: Final[tuple[_Threshold, ...]] = (
    (3.0, gt, RiskLevel.NO_TRAVEL),
    (2.0, ge, RiskLevel.SEVERE),
    (1.0, ge, RiskLevel.HIGH),
    (0.5, ge, RiskLevel.MODERATE),
)

NO_TRAVEL_HEAVY_LOAD_LB: Final = 30_000  # with Severe-band wind (45–54 mph)
SEVERE_HEAVY_LOAD_LB: Final = 40_000  # with High-band wind (35–44 mph)

# Readings above these are treated as bad data, not weather. Generous on purpose: the highest
# recorded surface wind gust is ~253 mph, and hourly rain/snow rates above ~15 in/hr are unheard of.
MAX_PLAUSIBLE_WIND_MPH: Final = 250.0
MAX_PLAUSIBLE_RAIN_IN_PER_HR: Final = 15.0
MAX_PLAUSIBLE_SNOW_IN_PER_HR: Final = 15.0
NEGATIVE_NOISE: Final = 0.01

type RawNumber = float | int | Decimal | str | None


@dataclass(frozen=True, slots=True)
class WeatherSample:
    """Forecast at one checkpoint, as received. None means the provider had no value."""

    wind_mph: RawNumber = None
    rain_in_per_hr: RawNumber = None
    snow_in_per_hr: RawNumber = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object] | None) -> WeatherSample:
        """Build from a provider/JSON dict. Unknown keys are ignored; non-dict input gives an empty sample."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            wind_mph=_as_raw(data.get("wind_mph")),
            rain_in_per_hr=_as_raw(data.get("rain_in_per_hr")),
            snow_in_per_hr=_as_raw(data.get("snow_in_per_hr")),
        )


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    level: RiskLevel | None  # None = Unknown (no usable weather data)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    data_issues: tuple[str, ...] = field(default_factory=tuple)
    missing: tuple[str, ...] = field(default_factory=tuple)  # factors with no value: "wind", "rain", "snow"

    @property
    def label(self) -> str:
        return UNKNOWN_LABEL if self.level is None else self.level.label

    @property
    def is_complete(self) -> bool:
        """True when every factor had a usable value and the load weight was known."""
        return not self.data_issues and not self.missing


@dataclass(frozen=True, slots=True)
class _Factor:
    name: str
    unit: str
    max_plausible: float


_WIND = _Factor("wind", "mph", MAX_PLAUSIBLE_WIND_MPH)
_RAIN = _Factor("rain", "in/hr", MAX_PLAUSIBLE_RAIN_IN_PER_HR)
_SNOW = _Factor("snow", "in/hr", MAX_PLAUSIBLE_SNOW_IN_PER_HR)


class _Invalid:
    """Marker for a value that was present but unusable."""


_INVALID: Final = _Invalid()


def classify(value: float, thresholds: tuple[_Threshold, ...]) -> RiskLevel:
    for bound, compare, level in thresholds:
        if compare(value, bound):
            return level
    return RiskLevel.LOW


def wind_level(wind_mph: float, load_weight_lb: float) -> RiskLevel:
    """Wind level from the table, raised by the load rules."""
    level = classify(wind_mph, WIND_MPH_THRESHOLDS)
    if level == RiskLevel.SEVERE and load_weight_lb > NO_TRAVEL_HEAVY_LOAD_LB:
        return RiskLevel.NO_TRAVEL
    if level == RiskLevel.HIGH and load_weight_lb > SEVERE_HEAVY_LOAD_LB:
        return RiskLevel.SEVERE
    return level


def assess(weather: WeatherSample | None, load_weight_lb: RawNumber) -> RiskAssessment:
    """Risk level for one checkpoint: the worst of wind (after load rules), rain and snow. Never raises."""
    try:
        return _assess(weather, load_weight_lb)
    except Exception:  # last-resort barrier: one bad checkpoint must not fail a whole trip
        logger.exception("Risk assessment failed for weather=%r load=%r", weather, load_weight_lb)
        return RiskAssessment(level=None, data_issues=("Risk could not be calculated for this checkpoint",))


def _assess(weather: WeatherSample | None, load_weight_lb: RawNumber) -> RiskAssessment:
    if not isinstance(weather, WeatherSample):
        weather = WeatherSample()

    data_issues: list[str] = []
    missing: list[str] = []
    values: dict[str, float | None] = {}
    for factor, raw in ((_WIND, weather.wind_mph), (_RAIN, weather.rain_in_per_hr), (_SNOW, weather.snow_in_per_hr)):
        value = _parse_measurement(raw, factor.max_plausible)
        if value is None:
            missing.append(factor.name)
        elif isinstance(value, _Invalid):
            data_issues.append(f"Ignored unusable {factor.name} value {_describe(raw)}")
        values[factor.name] = value if isinstance(value, float) else None

    if all(v is None for v in values.values()):
        return RiskAssessment(level=None, data_issues=tuple(data_issues), missing=tuple(missing))

    load, load_known = _parse_load(load_weight_lb)
    if not load_known:
        data_issues.append(
            f"Load weight unknown ({_describe(load_weight_lb)}); assumed over {SEVERE_HEAVY_LOAD_LB:,} lb"
        )

    wind = values["wind"] or 0.0
    rain = values["rain"] or 0.0
    snow = values["snow"] or 0.0

    wind_risk = wind_level(wind, load)
    rain_risk = classify(rain, RAIN_IN_PER_HR_THRESHOLDS)
    snow_risk = classify(snow, SNOW_IN_PER_HR_THRESHOLDS)

    reasons: list[str] = []
    if wind_risk >= RiskLevel.MODERATE:
        load_raised = wind_risk != classify(wind, WIND_MPH_THRESHOLDS)
        load_note = ""
        if load_raised:
            load_note = f" with {load:,.0f} lb load" if load_known else " with unknown (assumed heavy) load"
        reasons.append(f"Wind {_fmt(wind)} mph{load_note} → {wind_risk.label}")
    if rain_risk >= RiskLevel.MODERATE:
        reasons.append(f"Rain {_fmt(rain)} in/hr → {rain_risk.label}")
    if snow_risk >= RiskLevel.MODERATE:
        reasons.append(f"Snow {_fmt(snow)} in/hr → {snow_risk.label}")

    return RiskAssessment(
        level=max(wind_risk, rain_risk, snow_risk),
        reasons=tuple(reasons),
        data_issues=tuple(data_issues),
        missing=tuple(missing),
    )


def _to_float(raw: object) -> float | None | _Invalid:
    """None/blank → None; a finite number → float; anything else → _INVALID."""
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is an int subclass; True is not 1 mph
        return _INVALID
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            raw = float(text)
        except ValueError:
            return _INVALID
    if not isinstance(raw, int | float | Decimal):
        return _INVALID
    try:
        value = float(raw)
    except (ValueError, OverflowError, ArithmeticError):
        return _INVALID
    return value if math.isfinite(value) else _INVALID


def _parse_measurement(raw: object, max_plausible: float) -> float | None | _Invalid:
    value = _to_float(raw)
    if not isinstance(value, float):
        return value
    if value < 0:
        return 0.0 if value >= -NEGATIVE_NOISE else _INVALID
    if value > max_plausible:
        return _INVALID
    return value


def _parse_load(raw: object) -> tuple[float, bool]:
    """(load in lb, whether it was known). Unknown loads fall back to heavier than every load limit."""
    value = _to_float(raw)
    if isinstance(value, float) and value >= 0:
        return value, True
    return float(max(NO_TRAVEL_HEAVY_LOAD_LB, SEVERE_HEAVY_LOAD_LB) + 1), False


def _as_raw(value: object) -> RawNumber:
    if value is None or isinstance(value, int | float | Decimal | str):
        return value
    return str(value)  # keeps it visible in data_issues; parses as invalid unless it is numeric text


def _describe(raw: object) -> str:
    text = repr(raw)
    return text if len(text) <= 40 else text[:37] + "..."


def _fmt(value: float) -> str:
    """Shortest exact-looking form: 47 not 47.0, 0.25 not 0.250."""
    return f"{value:g}"

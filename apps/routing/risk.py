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
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from operator import ge, gt

from apps.routing.choices import RiskLevel

UNKNOWN_LABEL = "Unknown"

# (lower bound, comparison, level), checked from worst to best; the first bound the value
# meets wins. `ge` means the bound itself is in that level, `gt` means it's in the level below.
type _Threshold = tuple[float, Callable[[float, float], bool], RiskLevel]

WIND_MPH_THRESHOLDS: tuple[_Threshold, ...] = (
    (55, ge, RiskLevel.NO_TRAVEL),
    (45, ge, RiskLevel.SEVERE),
    (35, ge, RiskLevel.HIGH),
    (25, ge, RiskLevel.MODERATE),
)
RAIN_IN_PER_HR_THRESHOLDS: tuple[_Threshold, ...] = (
    (1.00, gt, RiskLevel.NO_TRAVEL),
    (0.50, ge, RiskLevel.SEVERE),
    (0.25, ge, RiskLevel.HIGH),
    (0.10, ge, RiskLevel.MODERATE),
)
SNOW_IN_PER_HR_THRESHOLDS: tuple[_Threshold, ...] = (
    (3.0, gt, RiskLevel.NO_TRAVEL),
    (2.0, ge, RiskLevel.SEVERE),
    (1.0, ge, RiskLevel.HIGH),
    (0.5, ge, RiskLevel.MODERATE),
)

NO_TRAVEL_HEAVY_LOAD_LB = 30_000  # with Severe-band wind (45–54 mph)
SEVERE_HEAVY_LOAD_LB = 40_000  # with High-band wind (35–44 mph)


@dataclass(frozen=True, slots=True)
class WeatherSample:
    """Forecast at one checkpoint. None means the provider had no value."""

    wind_mph: float | None = None
    rain_in_per_hr: float | None = None
    snow_in_per_hr: float | None = None


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    level: RiskLevel | None  # None = Unknown (no weather data at all)
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return UNKNOWN_LABEL if self.level is None else self.level.label


def classify(value: float, thresholds: tuple[_Threshold, ...]) -> RiskLevel:
    for bound, compare, level in thresholds:
        if compare(value, bound):
            return level
    return RiskLevel.LOW


def wind_level(wind_mph: float, load_weight_lb: int) -> RiskLevel:
    """Wind level from the table, raised by the load rules."""
    level = classify(wind_mph, WIND_MPH_THRESHOLDS)
    if level == RiskLevel.SEVERE and load_weight_lb > NO_TRAVEL_HEAVY_LOAD_LB:
        return RiskLevel.NO_TRAVEL
    if level == RiskLevel.HIGH and load_weight_lb > SEVERE_HEAVY_LOAD_LB:
        return RiskLevel.SEVERE
    return level


def assess(weather: WeatherSample, load_weight_lb: int) -> RiskAssessment:
    """Risk level for one checkpoint: the worst of wind (after load rules), rain and snow."""
    if weather.wind_mph is None and weather.rain_in_per_hr is None and weather.snow_in_per_hr is None:
        return RiskAssessment(level=None)

    wind = weather.wind_mph or 0.0
    rain = weather.rain_in_per_hr or 0.0
    snow = weather.snow_in_per_hr or 0.0

    wind_risk = wind_level(wind, load_weight_lb)
    rain_risk = classify(rain, RAIN_IN_PER_HR_THRESHOLDS)
    snow_risk = classify(snow, SNOW_IN_PER_HR_THRESHOLDS)

    reasons: list[str] = []
    if wind_risk >= RiskLevel.MODERATE:
        load_raised = wind_risk != classify(wind, WIND_MPH_THRESHOLDS)
        load_note = f" with {load_weight_lb:,} lb load" if load_raised else ""
        reasons.append(f"Wind {_fmt(wind)} mph{load_note} → {wind_risk.label}")
    if rain_risk >= RiskLevel.MODERATE:
        reasons.append(f"Rain {_fmt(rain)} in/hr → {rain_risk.label}")
    if snow_risk >= RiskLevel.MODERATE:
        reasons.append(f"Snow {_fmt(snow)} in/hr → {snow_risk.label}")

    return RiskAssessment(level=max(wind_risk, rain_risk, snow_risk), reasons=tuple(reasons))


def _fmt(value: float) -> str:
    """Shortest exact-looking form: 47 not 47.0, 0.25 not 0.250."""
    return f"{value:g}"

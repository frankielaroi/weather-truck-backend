"""Route scoring and recommendation (MODULES.md B2).

Pure logic — no database, network or clock — like the risk engine.

Scoring: each checkpoint stands for the road from halfway back to the previous checkpoint to halfway
on to the next (the first starts at mile 0, the last ends at the destination). A route's summary
totals those miles by risk level and takes a mile-weighted average risk, leaving Unknown out.

Ranking (lower is better; each step only breaks ties left by the one before):
    1. No Travel miles  (Decision: not in the spec — a route through No Travel weather never wins on time)
    2. Severe miles
    3. High miles
    4. average risk     (a route with no known weather at all ranks last here; Unknown is never Low)
    5. travel time
Values within MILES_TOLERANCE / AVERAGE_RISK_TOLERANCE of the best count as tied, so rounding noise
never picks the winner. If every step ties, the original route order decides.

Fault tolerance: checkpoints may arrive unsorted, outside [0, distance], with unknown or out-of-range
levels, or not at all; a route's distance or duration may be missing or invalid. None of these raise:
bad positions are clamped, bad levels count as Unknown, a route with no checkpoints is all Unknown,
and an unusable duration ranks last on travel time.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Protocol

from apps.routing.choices import RiskLevel

MILES_TOLERANCE: Final = 0.1
AVERAGE_RISK_TOLERANCE: Final = 0.01
DURATION_TOLERANCE_SECONDS: Final = 0.0
_FLOAT_SLACK: Final = 1e-9  # so 5.2 - 5.1 (= 0.10000000000000053) still counts as within 0.1

INCOMPLETE_UNKNOWN_SHARE: Final = 0.5  # warn when more than this share of a route's miles is Unknown

NO_SAFE_ROUTE_WARNING: Final = "No safe route — consider delaying departure."
INCOMPLETE_FORECAST_WARNING: Final = (
    "Incomplete forecast — over half of this route has no weather data, so its score may be off."
)


class PointRisk(Protocol):
    """Anything with a position along the route and a risk level — a Checkpoint row or a plain object."""

    @property
    def mile_marker(self) -> float: ...

    @property
    def risk_level(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class ScoredPoint:
    mile_marker: float
    risk_level: int | None


@dataclass(frozen=True, slots=True)
class RouteSummary:
    distance_miles: float
    miles_by_level: dict[RiskLevel, float]
    unknown_miles: float
    worst_level: RiskLevel | None  # None when no stretch of the route has known weather
    average_risk: float | None  # mile-weighted over known stretches; None when there are none
    warnings: tuple[str, ...] = ()

    @property
    def no_travel_miles(self) -> float:
        return self.miles_by_level[RiskLevel.NO_TRAVEL]

    @property
    def severe_miles(self) -> float:
        return self.miles_by_level[RiskLevel.SEVERE]

    @property
    def high_miles(self) -> float:
        return self.miles_by_level[RiskLevel.HIGH]

    @property
    def total_miles(self) -> float:
        """Miles across every level including Unknown — equals distance_miles."""
        return sum(self.miles_by_level.values()) + self.unknown_miles


@dataclass(frozen=True, slots=True)
class RouteOption:
    label: str  # e.g. "Route B" — used in the recommendation reason
    summary: RouteSummary
    duration_seconds: float | None


class DecidingStep(Enum):
    NO_TRAVEL_MILES = "no_travel_miles"
    SEVERE_MILES = "severe_miles"
    HIGH_MILES = "high_miles"
    AVERAGE_RISK = "average_risk"
    TRAVEL_TIME = "travel_time"
    ROUTE_ORDER = "route_order"  # every measure tied
    ONLY_ROUTE = "only_route"


@dataclass(frozen=True, slots=True)
class Recommendation:
    recommended_index: int
    deciding_step: DecidingStep
    reason: str
    warnings: tuple[tuple[str, ...], ...] = field(default_factory=tuple)  # one tuple per route, input order


# --- Scoring -----------------------------------------------------------------


def segment_bounds(mile_markers: Sequence[float], distance_miles: float) -> list[tuple[float, float]]:
    """(start, end) of the road each checkpoint stands for. Markers must be sorted and within [0, distance]."""
    bounds: list[tuple[float, float]] = []
    for i, mile in enumerate(mile_markers):
        start = 0.0 if i == 0 else (mile_markers[i - 1] + mile) / 2
        end = distance_miles if i == len(mile_markers) - 1 else (mile + mile_markers[i + 1]) / 2
        bounds.append((start, end))
    return bounds


def summarize_route(checkpoints: Sequence[PointRisk], distance_miles: float | None) -> RouteSummary:
    """Risk summary for one route from its scored checkpoints."""
    points: list[tuple[float, RiskLevel | None]] = sorted(
        ((_finite_or(cp.mile_marker, 0.0), _as_level(cp.risk_level)) for cp in checkpoints),
        key=lambda p: p[0],
    )
    furthest = points[-1][0] if points else 0.0
    distance = _finite_or(distance_miles, furthest)
    distance = max(distance, 0.0)
    if not points:
        return _summary(distance, {level: 0.0 for level in RiskLevel}, unknown_miles=distance)

    markers, levels = _merge_colocated(points, distance)
    miles_by_level = {level: 0.0 for level in RiskLevel}
    unknown_miles = 0.0
    for level, (start, end) in zip(levels, segment_bounds(markers, distance), strict=True):
        length = end - start
        if level is None:
            unknown_miles += length
        else:
            miles_by_level[level] += length
    return _summary(distance, miles_by_level, unknown_miles)


def _merge_colocated(
    points: Sequence[tuple[float, RiskLevel | None]], distance: float
) -> tuple[list[float], list[RiskLevel | None]]:
    """Clamp positions to the route, then merge checkpoints at the same mile into one.

    A merged point takes the worst known level among them (Unknown only if all are Unknown), so a
    checkpoint clamped onto a neighbour never loses its weather by ending up with zero miles.
    """
    markers: list[float] = []
    levels: list[RiskLevel | None] = []
    for raw_mile, level in points:
        mile = min(max(raw_mile, 0.0), distance)
        if markers and mile == markers[-1]:
            known = [lvl for lvl in (levels[-1], level) if lvl is not None]
            levels[-1] = max(known) if known else None
            continue
        markers.append(mile)
        levels.append(level)
    return markers, levels


def _summary(distance: float, miles_by_level: dict[RiskLevel, float], unknown_miles: float) -> RouteSummary:
    known_miles = sum(miles_by_level.values())
    present = [level for level, miles in miles_by_level.items() if miles > 0]
    average = sum(level * miles for level, miles in miles_by_level.items()) / known_miles if known_miles > 0 else None
    warnings: tuple[str, ...] = ()
    if distance > 0 and unknown_miles > distance * INCOMPLETE_UNKNOWN_SHARE:
        warnings = (INCOMPLETE_FORECAST_WARNING,)
    return RouteSummary(
        distance_miles=distance,
        miles_by_level=miles_by_level,
        unknown_miles=unknown_miles,
        worst_level=max(present) if present else None,
        average_risk=average,
        warnings=warnings,
    )


# --- Recommendation ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Step:
    step: DecidingStep
    value: Callable[[RouteOption], float]
    tolerance: float
    describe: Callable[[float, float, str], str]  # (winner value, runner-up value, runner-up label) -> reason


def _miles_reason(level_name: str) -> Callable[[float, float, str], str]:
    def describe(winner: float, runner_up: float, label: str) -> str:
        diff = round(runner_up - winner, 1)
        unit = "mile" if diff == 1 else "miles"
        return f"Recommended: {diff:g} fewer {level_name} {unit} than {label}."

    return describe


def _average_reason(winner: float, runner_up: float, label: str) -> str:
    if math.isinf(runner_up):
        return f"Recommended: {label} has no usable weather data to compare."
    return f"Recommended: lower average risk than {label} ({winner:.2f} vs {runner_up:.2f})."


def _time_reason(winner: float, runner_up: float, label: str) -> str:
    if math.isinf(runner_up):
        return f"Recommended: {label} has no usable travel time to compare."
    return f"Recommended: {_format_duration(runner_up - winner)} faster than {label}."


_STEPS: Final = (
    _Step(
        DecidingStep.NO_TRAVEL_MILES, lambda r: r.summary.no_travel_miles, MILES_TOLERANCE, _miles_reason("No Travel")
    ),
    _Step(DecidingStep.SEVERE_MILES, lambda r: r.summary.severe_miles, MILES_TOLERANCE, _miles_reason("Severe")),
    _Step(DecidingStep.HIGH_MILES, lambda r: r.summary.high_miles, MILES_TOLERANCE, _miles_reason("High")),
    _Step(
        DecidingStep.AVERAGE_RISK,
        lambda r: _finite_or(r.summary.average_risk, math.inf),
        AVERAGE_RISK_TOLERANCE,
        _average_reason,
    ),
    _Step(
        DecidingStep.TRAVEL_TIME,
        lambda r: _non_negative_or_inf(r.duration_seconds),
        DURATION_TOLERANCE_SECONDS,
        _time_reason,
    ),
)


def recommend(routes: Sequence[RouteOption]) -> Recommendation | None:
    """Pick exactly one route. None only when there are no routes."""
    if not routes:
        return None

    warnings = [list(route.summary.warnings) for route in routes]
    if len(routes) == 1:
        return Recommendation(0, DecidingStep.ONLY_ROUTE, "Recommended: the only route found.", _freeze(warnings))

    remaining = list(range(len(routes)))
    eliminated_at: dict[int, int] = {}  # route index -> step number it dropped out at
    for step_no, step in enumerate(_STEPS):
        best = min(step.value(routes[i]) for i in remaining)
        survivors = [i for i in remaining if _within(step.value(routes[i]), best, step.tolerance)]
        for i in remaining:
            if i not in survivors:
                eliminated_at[i] = step_no
        remaining = survivors
        if len(remaining) == 1:
            break

    winner = remaining[0]  # lowest original index among any routes still tied
    if all(routes[i].summary.no_travel_miles > 0 for i in range(len(routes))):
        warnings[winner].append(NO_SAFE_ROUTE_WARNING)

    if len(remaining) > 1:
        tied = routes[remaining[1]].label
        reason = f"Recommended: tied with {tied} on every measure; listed first."
        return Recommendation(winner, DecidingStep.ROUTE_ORDER, reason, _freeze(warnings))

    # Runner-up: the route that lasted longest, then the best on the step that removed it, then input order.
    last_step = max(eliminated_at.values())
    step = _STEPS[last_step]
    runner_up = min(
        (i for i, at in eliminated_at.items() if at == last_step),
        key=lambda i: (step.value(routes[i]), i),
    )
    reason = step.describe(step.value(routes[winner]), step.value(routes[runner_up]), routes[runner_up].label)
    return Recommendation(winner, step.step, reason, _freeze(warnings))


# --- Helpers -----------------------------------------------------------------


def _within(value: float, best: float, tolerance: float) -> bool:
    if math.isinf(best):
        return math.isinf(value)
    return value - best <= tolerance + _FLOAT_SLACK


def _as_level(raw: object) -> RiskLevel | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    try:
        return RiskLevel(raw)
    except ValueError:
        return None


def _finite_or(raw: object, fallback: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return fallback
    value = float(raw)
    return value if math.isfinite(value) else fallback


def _non_negative_or_inf(raw: object) -> float:
    value = _finite_or(raw, math.inf)
    return value if value >= 0 else math.inf


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{max(round(seconds), 1)} s"
    minutes = round(seconds / 60)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"


def _freeze(warnings: list[list[str]]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(w) for w in warnings)

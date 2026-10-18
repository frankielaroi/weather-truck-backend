"""B2 scoring and recommendation — see MODULES.md for the acceptance criteria each test maps to."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from apps.routing.choices import RiskLevel
from apps.routing.recommend import (
    INCOMPLETE_FORECAST_WARNING,
    NO_SAFE_ROUTE_WARNING,
    DecidingStep,
    RouteOption,
    RouteSummary,
    ScoredPoint,
    recommend,
    segment_bounds,
    summarize_route,
)

LOW, MODERATE, HIGH, SEVERE, NO_TRAVEL = (
    RiskLevel.LOW,
    RiskLevel.MODERATE,
    RiskLevel.HIGH,
    RiskLevel.SEVERE,
    RiskLevel.NO_TRAVEL,
)


def points(*pairs: tuple[float, int | None]) -> list[ScoredPoint]:
    return [ScoredPoint(mile, level) for mile, level in pairs]


def summary(
    *,
    no_travel: float = 0,
    severe: float = 0,
    high: float = 0,
    low: float = 100,
    unknown: float = 0,
    average: float | None = 0.0,
    warnings: tuple[str, ...] = (),
) -> RouteSummary:
    miles = {LOW: low, MODERATE: 0.0, HIGH: high, SEVERE: severe, NO_TRAVEL: no_travel}
    present = [level for level, m in miles.items() if m > 0]
    return RouteSummary(
        distance_miles=sum(miles.values()) + unknown,
        miles_by_level=miles,
        unknown_miles=unknown,
        worst_level=max(present) if present else None,
        average_risk=average,
        warnings=warnings,
    )


def option(label: str, duration_seconds: float | None = 3600, **summary_kwargs: Any) -> RouteOption:
    return RouteOption(label=label, summary=summary(**summary_kwargs), duration_seconds=duration_seconds)


# --- Miles per checkpoint ----------------------------------------------------


def test_each_checkpoint_covers_halfway_to_its_neighbours() -> None:
    assert segment_bounds([0, 25, 50, 75, 100], 100) == [
        (0, 12.5),
        (12.5, 37.5),
        (37.5, 62.5),
        (62.5, 87.5),
        (87.5, 100),
    ]


def test_uneven_last_segment_ends_at_destination() -> None:
    assert segment_bounds([0, 50, 100, 103], 103) == [(0, 25), (25, 75), (75, 101.5), (101.5, 103)]


def test_summary_totals_by_level() -> None:
    result = summarize_route(points((0, LOW), (25, HIGH), (50, SEVERE), (75, None), (100, LOW)), 100)
    assert result.miles_by_level[LOW] == 25  # 12.5 at each end
    assert result.high_miles == 25
    assert result.severe_miles == 25
    assert result.no_travel_miles == 0
    assert result.unknown_miles == 25
    assert result.worst_level == SEVERE


def test_average_risk_is_mile_weighted_and_leaves_unknown_out() -> None:
    # Low 12.5 mi + Severe 25 mi + Low 12.5 mi known (50 mi); Unknown 50 mi ignored.
    result = summarize_route(points((0, LOW), (25, None), (50, SEVERE), (75, None), (100, LOW)), 100)
    assert result.average_risk == pytest.approx((3 * 25) / 50)


def test_unknown_is_never_counted_as_low() -> None:
    result = summarize_route(points((0, None), (50, None), (100, None)), 100)
    assert result.miles_by_level[LOW] == 0
    assert result.worst_level is None
    assert result.average_risk is None


def test_no_checkpoints_means_the_whole_route_is_unknown() -> None:
    result = summarize_route([], 80)
    assert result.unknown_miles == 80
    assert result.total_miles == 80
    assert result.warnings == (INCOMPLETE_FORECAST_WARNING,)


def test_unsorted_and_out_of_range_checkpoints_are_tolerated() -> None:
    result = summarize_route(points((100, LOW), (-5, HIGH), (130, SEVERE), (50, LOW)), 100)
    assert result.total_miles == pytest.approx(100)
    assert result.worst_level == SEVERE


@pytest.mark.parametrize("level", [7, -1, "High", True, 2.0])
def test_invalid_levels_count_as_unknown(level: Any) -> None:
    result = summarize_route([ScoredPoint(0, LOW), ScoredPoint(50, level), ScoredPoint(100, LOW)], 100)
    assert result.unknown_miles == 50


@pytest.mark.parametrize("distance", [None, float("nan"), float("inf"), "far"])
def test_unusable_distance_falls_back_to_the_last_checkpoint(distance: Any) -> None:
    result = summarize_route(points((0, LOW), (40, LOW)), distance)
    assert result.distance_miles == 40
    assert result.total_miles == pytest.approx(40)


@given(
    distance=st.floats(min_value=0.5, max_value=3000),
    markers=st.lists(st.floats(min_value=-50, max_value=3500), min_size=0, max_size=120),
    levels=st.lists(st.sampled_from([None, 0, 1, 2, 3, 4, 9]), min_size=120, max_size=120),
)
def test_miles_across_all_levels_add_up_to_the_distance(
    distance: float, markers: list[float], levels: list[int | None]
) -> None:
    route = [ScoredPoint(mile, level) for mile, level in zip(markers, levels, strict=False)]
    result = summarize_route(route, distance)
    assert result.total_miles == pytest.approx(distance, abs=0.1)
    assert all(m >= -1e-9 for m in result.miles_by_level.values())
    assert result.unknown_miles >= -1e-9


def test_checkpoints_at_the_same_mile_share_the_stretch_at_the_worst_level() -> None:
    result = summarize_route(points((0, LOW), (50, LOW), (50, SEVERE), (50, None), (100, LOW)), 100)
    assert result.severe_miles == 50
    assert result.unknown_miles == 0


@given(
    distance=st.floats(min_value=0.5, max_value=3000),
    markers=st.lists(st.floats(min_value=-50, max_value=3500), min_size=1, max_size=60),
    levels=st.lists(st.sampled_from([None, 0, 1, 2, 3, 4]), min_size=60, max_size=60),
)
def test_no_checkpoint_weather_is_lost(distance: float, markers: list[float], levels: list[int | None]) -> None:
    route = [ScoredPoint(mile, level) for mile, level in zip(markers, levels, strict=False)]
    known = [RiskLevel(lvl) for lvl in levels[: len(route)] if lvl is not None]
    assert summarize_route(route, distance).worst_level == (max(known) if known else None)


# --- Warnings ----------------------------------------------------------------


def test_mostly_unknown_route_is_flagged_incomplete() -> None:
    result = summarize_route(points((0, None), (25, None), (50, None), (75, LOW), (100, LOW)), 100)
    assert result.unknown_miles == 62.5
    assert INCOMPLETE_FORECAST_WARNING in result.warnings


def test_exactly_half_unknown_is_not_flagged() -> None:
    result = summarize_route(points((0, None), (50, LOW), (100, None)), 100)
    assert result.unknown_miles == 50
    assert result.warnings == ()


def test_no_safe_route_warning_when_every_route_has_no_travel_miles() -> None:
    result = recommend([option("Route A", no_travel=10), option("Route B", no_travel=3)])
    assert result is not None
    assert result.recommended_index == 1
    assert result.warnings == ((), (NO_SAFE_ROUTE_WARNING,))


def test_no_safe_route_warning_absent_when_one_route_is_clear() -> None:
    result = recommend([option("Route A", no_travel=10), option("Route B")])
    assert result is not None
    assert all(NO_SAFE_ROUTE_WARNING not in w for w in result.warnings)


def test_route_warnings_are_passed_through_per_route() -> None:
    result = recommend(
        [option("Route A"), option("Route B", unknown=80, low=20, warnings=(INCOMPLETE_FORECAST_WARNING,))]
    )
    assert result is not None
    assert result.warnings == ((), (INCOMPLETE_FORECAST_WARNING,))


# --- Ranking: each step deciding on its own ----------------------------------


def test_no_travel_miles_decide() -> None:
    result = recommend([option("Route A", no_travel=12, duration_seconds=100), option("Route B", no_travel=0)])
    assert result is not None
    assert result.recommended_index == 1
    assert result.deciding_step == DecidingStep.NO_TRAVEL_MILES
    assert result.reason == "Recommended: 12 fewer No Travel miles than Route A."


def test_severe_miles_decide() -> None:
    result = recommend([option("Route A", severe=0), option("Route B", severe=12)])
    assert result is not None
    assert result.recommended_index == 0
    assert result.deciding_step == DecidingStep.SEVERE_MILES
    assert result.reason == "Recommended: 12 fewer Severe miles than Route B."


def test_high_miles_decide() -> None:
    result = recommend([option("Route A", high=30.25), option("Route B", high=1)])
    assert result is not None
    assert result.recommended_index == 1
    assert result.deciding_step == DecidingStep.HIGH_MILES
    assert result.reason == "Recommended: 29.2 fewer High miles than Route A."


def test_average_risk_decides() -> None:
    result = recommend([option("Route A", average=1.85), option("Route B", average=1.2)])
    assert result is not None
    assert result.recommended_index == 1
    assert result.deciding_step == DecidingStep.AVERAGE_RISK
    assert result.reason == "Recommended: lower average risk than Route A (1.20 vs 1.85)."


def test_travel_time_decides() -> None:
    result = recommend([option("Route A", duration_seconds=5 * 3600), option("Route B", duration_seconds=4 * 3600)])
    assert result is not None
    assert result.recommended_index == 1
    assert result.deciding_step == DecidingStep.TRAVEL_TIME
    assert result.reason == "Recommended: 1 h faster than Route A."


def test_all_five_tied_original_order_breaks_the_tie() -> None:
    result = recommend([option("Route A"), option("Route B"), option("Route C")])
    assert result is not None
    assert result.recommended_index == 0
    assert result.deciding_step == DecidingStep.ROUTE_ORDER
    assert result.reason == "Recommended: tied with Route B on every measure; listed first."


def test_shorter_route_with_severe_miles_loses_to_longer_clean_route() -> None:
    result = recommend(
        [option("Route A", severe=5, duration_seconds=6 * 3600), option("Route B", duration_seconds=9 * 3600)]
    )
    assert result is not None
    assert result.recommended_index == 1
    assert result.deciding_step == DecidingStep.SEVERE_MILES
    assert result.reason == "Recommended: 5 fewer Severe miles than Route A."


# --- Ranking: tolerances and tie-breaking ------------------------------------


def test_miles_within_tolerance_tie_and_fall_through_to_the_next_step() -> None:
    result = recommend([option("Route A", severe=5.1, high=0), option("Route B", severe=5.0, high=20)])
    assert result is not None
    assert result.recommended_index == 0
    assert result.deciding_step == DecidingStep.HIGH_MILES


def test_tolerance_survives_float_rounding() -> None:
    # 5.2 - 5.1 is 0.10000000000000053 in floating point; still a tie.
    result = recommend([option("Route A", severe=5.2), option("Route B", severe=5.1, duration_seconds=4000)])
    assert result is not None
    assert result.deciding_step == DecidingStep.TRAVEL_TIME


def test_average_risk_within_tolerance_ties() -> None:
    result = recommend(
        [option("Route A", average=1.005, duration_seconds=100), option("Route B", average=1.0, duration_seconds=200)]
    )
    assert result is not None
    assert result.recommended_index == 0
    assert result.deciding_step == DecidingStep.TRAVEL_TIME


def test_reason_compares_against_the_route_that_lasted_longest() -> None:
    result = recommend(
        [
            option("Route A", severe=40),  # out at step 2
            option("Route B", high=10),  # out at step 3
            option("Route C", high=2),
        ]
    )
    assert result is not None
    assert result.recommended_index == 2
    assert result.reason == "Recommended: 8 fewer High miles than Route B."


def test_singular_mile_in_reason() -> None:
    result = recommend([option("Route A", severe=1), option("Route B")])
    assert result is not None
    assert result.reason == "Recommended: 1 fewer Severe mile than Route A."


def test_sub_minute_time_difference_is_shown_in_seconds() -> None:
    result = recommend([option("Route A", duration_seconds=3630), option("Route B", duration_seconds=3600)])
    assert result is not None
    assert result.reason == "Recommended: 30 s faster than Route A."


def test_hours_and_minutes_in_time_reason() -> None:
    result = recommend(
        [option("Route A", duration_seconds=9 * 3600), option("Route B", duration_seconds=9 * 3600 - 5100)]
    )
    assert result is not None
    assert result.reason == "Recommended: 1 h 25 min faster than Route A."


def test_route_without_known_weather_ranks_last_on_average_risk() -> None:
    result = recommend([option("Route A", low=0, unknown=100, average=None), option("Route B", average=2.5)])
    assert result is not None
    assert result.recommended_index == 1
    assert result.reason == "Recommended: Route A has no usable weather data to compare."


@pytest.mark.parametrize("bad_duration", [None, float("nan"), -10, float("inf")])
def test_unusable_duration_ranks_last_on_time(bad_duration: Any) -> None:
    result = recommend([option("Route A", duration_seconds=bad_duration), option("Route B", duration_seconds=99999)])
    assert result is not None
    assert result.recommended_index == 1
    assert result.reason == "Recommended: Route A has no usable travel time to compare."


def test_single_route_is_recommended() -> None:
    result = recommend([option("Route A", no_travel=50)])
    assert result is not None
    assert result.recommended_index == 0
    assert result.deciding_step == DecidingStep.ONLY_ROUTE


def test_no_routes_gives_no_recommendation() -> None:
    assert recommend([]) is None


# --- Exactly one recommended -------------------------------------------------

mile_values = st.floats(min_value=0, max_value=500)
route_options = st.builds(
    lambda nt, sv, hi, avg, dur, i: option(
        f"Route {i}", no_travel=nt, severe=sv, high=hi, average=avg, duration_seconds=dur
    ),
    nt=st.sampled_from([0.0, 0.05, 3.0, 10.0]) | mile_values,
    sv=st.sampled_from([0.0, 0.05, 5.0]) | mile_values,
    hi=st.sampled_from([0.0, 20.0]) | mile_values,
    avg=st.none() | st.floats(min_value=0, max_value=4),
    dur=st.none() | st.integers(min_value=0, max_value=100_000),
    i=st.integers(min_value=0, max_value=99),
)


@given(routes=st.lists(route_options, min_size=1, max_size=5))
def test_exactly_one_valid_route_is_recommended_with_a_reason(routes: list[RouteOption]) -> None:
    result = recommend(routes)
    assert result is not None
    assert 0 <= result.recommended_index < len(routes)
    assert result.reason.startswith("Recommended:")
    assert len(result.warnings) == len(routes)


@given(routes=st.lists(route_options, min_size=2, max_size=4))
def test_winner_never_has_more_no_travel_miles_than_any_route_beyond_tolerance(routes: list[RouteOption]) -> None:
    result = recommend(routes)
    assert result is not None
    winner = routes[result.recommended_index].summary.no_travel_miles
    assert all(winner <= r.summary.no_travel_miles + 0.1 + 1e-9 for r in routes)

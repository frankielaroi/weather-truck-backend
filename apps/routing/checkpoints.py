"""Checkpoint sampler (MODULES.md B6): weather sample points along a route, with the truck's ETA at each.

Pure logic — no database, network or clock (the departure time is passed in).

- Checkpoints go at mile 0, every `interval_miles`, and at the destination. A regular checkpoint less
  than MERGE_WITHIN_MILES before the destination is merged into it (mile 0 is never merged).
- Distances are measured along the road path, scaled so the path's length equals the provider's
  reported distance; points are interpolated onto the path between its coordinates.
- ETA = departure + the time the route takes to reach that point, from the provider's per-section
  times (time within a section is spread by distance). It is not distance ÷ an average speed.
  Stretches no section covers use the route's average pace, and the times are scaled so arrival at
  the destination is exactly departure + the route's total time.
- Decision: no rest stops or driving-hours breaks are added; arrival times assume continuous driving.

Fault tolerance: non-finite path coordinates are dropped; sections that are missing, out of range,
overlapping or zero-length are tolerated (falling back to average pace where needed); a naive
departure time is read as UTC. Only a non-positive or non-finite interval raises (ValueError).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from apps.routing.route_providers import LngLat, Section, haversine_km

MERGE_WITHIN_MILES: Final = 1.0
KM_PER_MILE: Final = 1.609344


class RouteLike(Protocol):
    @property
    def path(self) -> tuple[LngLat, ...]: ...

    @property
    def sections(self) -> tuple[Section, ...]: ...

    @property
    def distance_miles(self) -> float: ...

    @property
    def duration_seconds(self) -> float: ...


@dataclass(frozen=True, slots=True)
class CheckpointPoint:
    sequence: int
    mile_marker: float
    lng: float
    lat: float
    seconds_from_departure: float
    eta: datetime


def checkpoint_miles(distance_miles: float, interval_miles: float) -> list[float]:
    """0, interval, 2·interval, … and the destination; a stop within 1 mile of the end merges into it."""
    if not math.isfinite(interval_miles) or interval_miles <= 0:
        raise ValueError(f"interval_miles must be positive, got {interval_miles!r}")
    total = distance_miles if math.isfinite(distance_miles) and distance_miles > 0 else 0.0
    if total == 0:
        return [0.0]
    miles = [0.0]
    step = 1
    while step * interval_miles < total:
        miles.append(step * interval_miles)
        step += 1
    if len(miles) > 1 and total - miles[-1] < MERGE_WITHIN_MILES:
        miles.pop()
    miles.append(total)
    return miles


def sample_checkpoints(route: RouteLike, departure: datetime, interval_miles: float) -> list[CheckpointPoint]:
    """Checkpoints along `route` for a truck leaving at `departure`."""
    path = [p for p in route.path if _finite_point(p)]
    if not path:
        return []
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=UTC)

    cum_km = _cumulative_km(path)
    path_miles = cum_km[-1] / KM_PER_MILE
    reported = route.distance_miles if _positive(route.distance_miles) else path_miles
    scale = reported / path_miles if path_miles > 0 else 0.0  # route miles per path mile
    cum_miles = [km / KM_PER_MILE * scale for km in cum_km]
    total_seconds = route.duration_seconds if _positive(route.duration_seconds) else 0.0
    cum_seconds = _cumulative_seconds(path, cum_km, route.sections, total_seconds, len(route.path))

    points: list[CheckpointPoint] = []
    for sequence, mile in enumerate(checkpoint_miles(cum_miles[-1], interval_miles)):
        (lng, lat), seconds = _at_mile(path, cum_miles, cum_seconds, mile)
        points.append(
            CheckpointPoint(
                sequence=sequence,
                mile_marker=mile,
                lng=lng,
                lat=lat,
                seconds_from_departure=seconds,
                eta=departure + timedelta(seconds=seconds),
            )
        )
    return points


# --- Internals -----------------------------------------------------------------------------


def _cumulative_km(path: Sequence[LngLat]) -> list[float]:
    cum = [0.0]
    for a, b in zip(path, path[1:], strict=False):
        cum.append(cum[-1] + haversine_km(a, b))
    return cum


def _cumulative_seconds(
    path: Sequence[LngLat],
    cum_km: Sequence[float],
    sections: Sequence[Section],
    total_seconds: float,
    original_length: int,
) -> list[float]:
    """Seconds from departure to reach each path vertex."""
    n = len(path)
    if total_seconds <= 0 or cum_km[-1] <= 0:
        return [0.0] * n
    usable = [] if original_length != n else _clean_sections(sections, n)
    pace = total_seconds / cum_km[-1]  # route-average seconds per km, for uncovered stretches

    cum = [0.0] * n
    reached = 0  # highest vertex with a known time
    for section in usable:
        start, end = section.start, section.end
        if start < reached:  # overlap: keep the part not yet timed
            start = reached
            if end <= start:
                continue
        for i in range(reached + 1, start + 1):  # gap before this section
            cum[i] = cum[i - 1] + (cum_km[i] - cum_km[i - 1]) * pace
        base = cum[start]
        span = cum_km[end] - cum_km[start]
        for i in range(start + 1, end + 1):
            share = (cum_km[i] - cum_km[start]) / span if span > 0 else (i - start) / (end - start)
            cum[i] = base + section.duration_seconds * share
        reached = end
    for i in range(reached + 1, n):
        cum[i] = cum[i - 1] + (cum_km[i] - cum_km[i - 1]) * pace

    if cum[-1] <= 0:
        return [km * pace for km in cum_km]
    factor = total_seconds / cum[-1]  # land exactly on the route's total time
    return [t * factor for t in cum]


def _clean_sections(sections: Sequence[Section], n: int) -> list[Section]:
    """Sections that point inside the path, have a finite non-negative time and some length, in order."""
    clean = [
        s
        for s in sections
        if 0 <= s.start < s.end < n and math.isfinite(s.duration_seconds) and s.duration_seconds >= 0
    ]
    return sorted(clean, key=lambda s: (s.start, s.end))


def _at_mile(
    path: Sequence[LngLat], cum_miles: Sequence[float], cum_seconds: Sequence[float], mile: float
) -> tuple[LngLat, float]:
    if mile <= 0 or len(path) == 1:
        return path[0], cum_seconds[0]
    if mile >= cum_miles[-1]:
        return path[-1], cum_seconds[-1]
    i = bisect.bisect_right(cum_miles, mile)  # cum_miles[i-1] <= mile < cum_miles[i]
    a, b = path[i - 1], path[i]
    span = cum_miles[i] - cum_miles[i - 1]
    t = (mile - cum_miles[i - 1]) / span if span > 0 else 0.0
    point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    seconds = cum_seconds[i - 1] + (cum_seconds[i] - cum_seconds[i - 1]) * t
    return point, seconds


def _finite_point(point: LngLat) -> bool:
    return len(point) >= 2 and math.isfinite(point[0]) and math.isfinite(point[1])


def _positive(value: float) -> bool:
    return isinstance(value, int | float) and math.isfinite(value) and value > 0

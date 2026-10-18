"""Path geometry shared by the models and the API responses."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final, TypeIs

PATH_TOLERANCE_M: Final = 30.0
COORD_DECIMALS: Final = 5


def simplified_coordinates(geometry: object, tolerance_m: float = PATH_TOLERANCE_M) -> list[list[float]]:
    """GeoJSON LineString coordinates, simplified to within `tolerance_m` and rounded to ~1 m."""
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(coords, list):
        return []
    points = [
        (float(c[0]), float(c[1]))
        for c in coords
        if isinstance(c, list | tuple) and len(c) >= 2 and all(_finite(v) for v in c[:2])
    ]
    kept = simplify_path(points, tolerance_m)
    return [[round(lng, COORD_DECIMALS), round(lat, COORD_DECIMALS)] for lng, lat in kept]


def simplify_path(points: Sequence[tuple[float, float]], tolerance_m: float) -> list[tuple[float, float]]:
    """Ramer–Douglas–Peucker on [lng, lat] points (iterative, so long paths can't hit recursion limits).
    Keeps both ends; every dropped point is within `tolerance_m` of the simplified line."""
    if len(points) <= 2:
        return list(points)
    mid_lat = math.radians(sum(p[1] for p in points) / len(points))
    kx, ky = 111_320.0 * math.cos(mid_lat), 110_574.0  # metres per degree, locally
    xy = [(p[0] * kx, p[1] * ky) for p in points]

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        worst, worst_at = -1.0, start
        for i in range(start + 1, end):
            d = _segment_distance(xy[i], xy[start], xy[end])
            if d > worst:
                worst, worst_at = d, i
        if worst > tolerance_m:
            keep[worst_at] = True
            stack.append((start, worst_at))
            stack.append((worst_at, end))
    return [p for p, k in zip(points, keep, strict=True) if k]


def _segment_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _finite(value: object) -> TypeIs[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)

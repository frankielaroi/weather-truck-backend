"""Trip coordinator (MODULES.md B8): runs the whole job for one trip and saves it.

    routing (B5) → checkpoints (B6) → weather + risk (B7, B1) → route scores + recommendation (B2)

All outside calls happen first; then the trip, its routes and their checkpoints are saved in one
transaction and the trip is marked Ready. If any step fails, the trip is saved as Failed with the
reason and nothing half-done is kept; the original error is re-raised so the API can answer with
its status (422 / 502 / 504, or 500 for a bug). Each step's time is logged.

Decision — cached route plans: routes don't depend on the departure time, so a plan is cached for
ROUTE_CACHE_SECONDS per origin, destination and truck weight. With the 1-hour forecast cache, a
repeated trip makes no provider calls at all.

This is the only module that uses both outside services and the database.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from django.core.cache import cache
from django.db import transaction
from rest_framework.exceptions import APIException

from apps.routing.checkpoints import CheckpointPoint, sample_checkpoints
from apps.routing.models import Checkpoint, Route, Trip
from apps.routing.recommend import RouteOption, ScoredPoint, recommend, summarize_route
from apps.routing.route_providers import LngLat, gross_weight_tonnes
from apps.routing.routes import PlannedRoute, plan_routes
from apps.routing.weather import CheckpointWeather, checkpoint_weather

logger = logging.getLogger(__name__)

ROUTE_CACHE_SECONDS: Final = 60 * 60 * 24
_ROUTE_CACHE_VERSION: Final = "v1"
UNEXPECTED_FAILURE: Final = "Something went wrong while planning this trip."


@dataclass(frozen=True, slots=True)
class TripRequest:
    origin_name: str
    origin: LngLat
    destination_name: str
    destination: LngLat
    departure_at: datetime
    load_weight_lb: int
    interval_miles: int


@dataclass(slots=True)
class StepTimes:
    """Milliseconds per step, logged once per trip."""

    ms: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.ms[name] = (time.perf_counter() - start) * 1000

    def summary(self) -> str:
        total = sum(self.ms.values())
        parts = ", ".join(f"{name} {ms:.0f} ms" for name, ms in self.ms.items())
        return f"{parts} (total {total:.0f} ms)"


@dataclass(frozen=True, slots=True)
class _ScoredRoute:
    planned: PlannedRoute
    points: list[CheckpointPoint]
    weather: list[CheckpointWeather]


def create_trip(request: TripRequest) -> Trip:
    """Plan, score and save a trip. Returns it Ready, or saves it Failed and re-raises the error."""
    times = StepTimes()
    try:
        scored = _run_pipeline(request, times)
        with times.step("save"):
            trip = _save_ready(request, scored)
    except APIException as exc:
        _save_failed(request, str(exc.detail))
        logger.info(
            "Trip %s -> %s failed: %s [%s]", request.origin_name, request.destination_name, exc, times.summary()
        )
        raise
    except Exception:
        _save_failed(request, UNEXPECTED_FAILURE)
        logger.exception("Trip %s -> %s crashed [%s]", request.origin_name, request.destination_name, times.summary())
        raise
    logger.info("Trip %s ready: %d routes [%s]", trip.pk, len(scored), times.summary())
    return trip


# --- Pipeline (outside calls only, no trip rows written) -----------------------------------


def _run_pipeline(request: TripRequest, times: StepTimes) -> list[_ScoredRoute]:
    with times.step("routing"):
        planned = cached_plan(request.origin, request.destination, request.load_weight_lb)
    with times.step("checkpoints"):
        points = [sample_checkpoints(route, request.departure_at, request.interval_miles) for route in planned]
    with times.step("weather"):
        weather = _weather_for_all(points, request.load_weight_lb)
    return [_ScoredRoute(r, p, w) for r, p, w in zip(planned, points, weather, strict=True)]


def _weather_for_all(points: Sequence[list[CheckpointPoint]], load_weight_lb: int) -> list[list[CheckpointWeather]]:
    """One weather call for every route's checkpoints, so shared cells are fetched once."""
    flat = [point for route_points in points for point in route_points]
    results = checkpoint_weather(flat, load_weight_lb)
    out: list[list[CheckpointWeather]] = []
    start = 0
    for route_points in points:
        out.append(results[start : start + len(route_points)])
        start += len(route_points)
    return out


def cached_plan(origin: LngLat, destination: LngLat, load_weight_lb: float) -> list[PlannedRoute]:
    key = _route_cache_key(origin, destination, load_weight_lb)
    hit = _cache_call(lambda: cache.get(key), None)
    if isinstance(hit, list) and hit and all(isinstance(r, PlannedRoute) for r in hit):
        return hit
    planned = plan_routes(origin, destination, load_weight_lb)
    _cache_call(lambda: cache.set(key, planned, timeout=ROUTE_CACHE_SECONDS), None)
    return planned


def _route_cache_key(origin: LngLat, destination: LngLat, load_weight_lb: float) -> str:
    ends = f"{origin[0]:.4f},{origin[1]:.4f}|{destination[0]:.4f},{destination[1]:.4f}"
    raw = f"{ends}|{gross_weight_tonnes(load_weight_lb)}"
    return f"routes:{_ROUTE_CACHE_VERSION}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def _cache_call[T](call: Callable[[], T], fallback: T) -> T:
    try:
        return call()
    except Exception:
        logger.warning("Route cache unavailable", exc_info=True)
        return fallback


# --- Saving -------------------------------------------------------------------------------------


def _trip_fields(request: TripRequest) -> dict[str, object]:
    return {
        "origin_name": request.origin_name,
        "origin_lng": request.origin[0],
        "origin_lat": request.origin[1],
        "destination_name": request.destination_name,
        "destination_lng": request.destination[0],
        "destination_lat": request.destination[1],
        "departure_at": request.departure_at,
        "load_weight_lb": request.load_weight_lb,
        "sample_interval_miles": request.interval_miles,
    }


def _save_ready(request: TripRequest, scored: Sequence[_ScoredRoute]) -> Trip:
    summaries = [
        summarize_route(
            [ScoredPoint(p.mile_marker, w.risk.level) for p, w in zip(s.points, s.weather, strict=True)],
            s.planned.distance_miles,
        )
        for s in scored
    ]
    options = [
        RouteOption(f"Route {chr(ord('A') + i)}", summary, s.planned.duration_seconds)
        for i, (s, summary) in enumerate(zip(scored, summaries, strict=True))
    ]
    recommendation = recommend(options)

    with transaction.atomic():
        trip = Trip.objects.create(**_trip_fields(request), status=Trip.Status.READY)
        routes = Route.objects.bulk_create(
            [
                Route.from_results(trip, i, s.planned, summary, recommendation)
                for i, (s, summary) in enumerate(zip(scored, summaries, strict=True))
            ]
        )
        Checkpoint.objects.bulk_create(
            [
                Checkpoint.from_results(route, point, weather)
                for route, s in zip(routes, scored, strict=True)
                for point, weather in zip(s.points, s.weather, strict=True)
            ]
        )
    return trip


def _save_failed(request: TripRequest, reason: str) -> None:
    try:
        Trip.objects.create(**_trip_fields(request), status=Trip.Status.FAILED, error_message=reason)
    except Exception:
        logger.exception("Couldn't record the failed trip")

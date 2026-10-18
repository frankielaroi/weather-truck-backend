from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Prefetch, Q

from apps.routing.choices import RiskLevel, SampleInterval
from apps.routing.recommend import DecidingStep

if TYPE_CHECKING:
    from apps.routing.checkpoints import CheckpointPoint
    from apps.routing.recommend import Recommendation, RouteSummary
    from apps.routing.routes import PlannedRoute
    from apps.routing.weather import CheckpointWeather

DECIDING_STEP_CHOICES = [(step.value, step.value.replace("_", " ").capitalize()) for step in DecidingStep]

LATITUDE_VALIDATORS = [MinValueValidator(-90), MaxValueValidator(90)]
LONGITUDE_VALIDATORS = [MinValueValidator(-180), MaxValueValidator(180)]


class TripQuerySet(models.QuerySet["Trip"]):
    def with_details(self) -> TripQuerySet:
        """Trips with their routes and each route's checkpoints, in display order, in three queries."""
        checkpoints = Prefetch("checkpoints", queryset=Checkpoint.objects.order_by("sequence"))
        routes = Prefetch(
            "routes",
            queryset=Route.objects.order_by("index").prefetch_related(checkpoints),
        )
        return self.select_related("user").prefetch_related(routes)


class Trip(models.Model):
    """One routing request: where the driver is going, when they leave, and how heavy the load is."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="trips",
    )

    origin_name = models.CharField(max_length=255)
    origin_lat = models.FloatField(validators=LATITUDE_VALIDATORS)
    origin_lng = models.FloatField(validators=LONGITUDE_VALIDATORS)
    destination_name = models.CharField(max_length=255)
    destination_lat = models.FloatField(validators=LATITUDE_VALIDATORS)
    destination_lng = models.FloatField(validators=LONGITUDE_VALIDATORS)

    departure_at = models.DateTimeField()
    load_weight_lb = models.PositiveIntegerField(validators=[MaxValueValidator(200_000)])
    sample_interval_miles = models.PositiveSmallIntegerField(
        choices=SampleInterval.choices,
        default=SampleInterval.EVERY_25_MILES,
    )

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TripQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.origin_name} → {self.destination_name} ({self.departure_at:%Y-%m-%d %H:%M})"

    @property
    def recommended_route(self) -> Route | None:
        """Uses prefetched routes when available, so it adds no query after `with_details()`."""
        return next((route for route in self.routes.all() if route.is_recommended), None)


class Route(models.Model):
    """One of the (up to 3) route options returned by the routing API for a trip, with its risk summary."""

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="routes")
    index = models.PositiveSmallIntegerField(help_text="Order the routing API returned this option in.")
    label = models.CharField(max_length=255, blank=True, help_text="Short label, e.g. 'via I-80 W'.")
    provider = models.CharField(max_length=30, blank=True, help_text="Routing provider used, e.g. 'valhalla'.")
    profile = models.CharField(max_length=10, blank=True, help_text="'truck' or 'car'.")
    notes = models.JSONField(
        default=list, blank=True, help_text="Caveats about how the route was made, e.g. car profile or sections."
    )

    distance_miles = models.FloatField(validators=[MinValueValidator(0)])
    duration_seconds = models.PositiveIntegerField()
    geometry = models.JSONField(help_text="GeoJSON LineString of the route path.")

    # Risk summary (B2), from this route's checkpoints. Each checkpoint stands for the road halfway to
    # its neighbours; these are those miles by level, and they add up to distance_miles.
    no_travel_miles = models.FloatField(default=0)
    severe_miles = models.FloatField(default=0)
    high_miles = models.FloatField(default=0)
    moderate_miles = models.FloatField(default=0)
    low_miles = models.FloatField(default=0)
    unknown_miles = models.FloatField(default=0)
    average_risk = models.FloatField(
        null=True, blank=True, help_text="Mile-weighted mean of RiskLevel values; null when no stretch is known."
    )
    max_risk_level = models.PositiveSmallIntegerField(
        choices=RiskLevel.choices, null=True, blank=True, help_text="Worst known level; null when none is known."
    )
    warnings = models.JSONField(
        default=list, blank=True, help_text="E.g. 'No safe route — consider delaying departure.'"
    )

    is_recommended = models.BooleanField(default=False)
    recommendation_reason = models.CharField(
        max_length=255, blank=True, help_text="Set on the recommended route, e.g. '12 fewer Severe miles than Route B.'"
    )
    deciding_step = models.CharField(
        max_length=20, choices=DECIDING_STEP_CHOICES, blank=True, help_text="Which ranking step picked it."
    )

    class Meta:
        ordering = ["trip", "index"]
        constraints = [
            models.UniqueConstraint(fields=["trip", "index"], name="route_unique_index_per_trip"),
            models.UniqueConstraint(
                fields=["trip"],
                condition=Q(is_recommended=True),
                name="route_one_recommended_per_trip",
            ),
        ]

    def __str__(self) -> str:
        return f"Trip {self.trip_id} route {self.index}: {self.label or 'unnamed'}"

    @classmethod
    def from_results(
        cls,
        trip: Trip,
        index: int,
        planned: PlannedRoute,
        summary: RouteSummary,
        recommendation: Recommendation | None,
    ) -> Route:
        """An unsaved row from the planner's route (B5), its risk summary and the recommendation (B2)."""
        recommended = recommendation is not None and recommendation.recommended_index == index
        warnings = summary.warnings
        if recommendation is not None and index < len(recommendation.warnings):
            warnings = recommendation.warnings[index]
        return cls(
            trip=trip,
            index=index,
            label=planned.label[:255],
            provider=planned.provider,
            profile=planned.profile,
            notes=list(planned.notes),
            distance_miles=summary.distance_miles,
            duration_seconds=max(0, round(planned.duration_seconds)),
            geometry={"type": "LineString", "coordinates": [[lng, lat] for lng, lat in planned.path]},
            no_travel_miles=summary.no_travel_miles,
            severe_miles=summary.severe_miles,
            high_miles=summary.high_miles,
            moderate_miles=summary.miles_by_level[RiskLevel.MODERATE],
            low_miles=summary.miles_by_level[RiskLevel.LOW],
            unknown_miles=summary.unknown_miles,
            average_risk=summary.average_risk,
            max_risk_level=summary.worst_level,
            warnings=list(warnings),
            is_recommended=recommended,
            recommendation_reason=recommendation.reason[:255] if recommended and recommendation else "",
            deciding_step=recommendation.deciding_step.value if recommended and recommendation else "",
        )

    @property
    def total_risk_miles(self) -> float:
        """Miles across every level including Unknown — equals distance_miles."""
        return (
            self.no_travel_miles + self.severe_miles + self.high_miles
            + self.moderate_miles + self.low_miles + self.unknown_miles
        )  # fmt: skip


class Checkpoint(models.Model):
    """A weather sample point on a route, taken at the truck's estimated arrival time there."""

    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="checkpoints")
    sequence = models.PositiveSmallIntegerField()
    mile_marker = models.FloatField(validators=[MinValueValidator(0)], help_text="Miles from the origin.")
    lat = models.FloatField(validators=LATITUDE_VALIDATORS)
    lng = models.FloatField(validators=LONGITUDE_VALIDATORS)
    eta = models.DateTimeField()

    # Forecast at the ETA. Null means the provider had no value for that field.
    wind_mph = models.FloatField(null=True, blank=True)
    wind_gust_mph = models.FloatField(null=True, blank=True)
    rain_in_per_hr = models.FloatField(null=True, blank=True)
    snow_in_per_hr = models.FloatField(null=True, blank=True)
    temperature_f = models.FloatField(null=True, blank=True)
    weather_description = models.CharField(max_length=100, blank=True)

    forecast_time = models.DateTimeField(
        null=True, blank=True, help_text="The forecast hour used (nearest the ETA). Null when there was none."
    )

    risk_level = models.PositiveSmallIntegerField(
        choices=RiskLevel.choices,
        null=True,
        blank=True,
        help_text="Null means Unknown (no usable forecast); data_notes says why.",
    )
    risk_reasons = models.JSONField(
        default=list,
        blank=True,
        help_text="Why this level was assigned, e.g. ['Wind 47 mph with 32,000 lb load → No Travel'].",
    )
    data_notes = models.JSONField(
        default=list,
        blank=True,
        help_text="Problems with this checkpoint's data, e.g. ['Beyond forecast range'] or a stale-forecast note.",
    )

    class Meta:
        ordering = ["route", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["route", "sequence"], name="checkpoint_unique_sequence_per_route"),
        ]

    def __str__(self) -> str:
        return f"Route {self.route_id} checkpoint {self.sequence} @ mile {self.mile_marker:.0f}"

    @classmethod
    def from_results(cls, route: Route, point: CheckpointPoint, weather: CheckpointWeather) -> Checkpoint:
        """An unsaved row from the sampler's point (B6) and the weather client's result (B7)."""
        return cls(
            route=route,
            sequence=point.sequence,
            mile_marker=point.mile_marker,
            lat=point.lat,
            lng=point.lng,
            eta=point.eta,
            wind_mph=weather.wind_mph,
            wind_gust_mph=weather.gust_mph,
            rain_in_per_hr=weather.rain_in_per_hr,
            snow_in_per_hr=weather.snow_in_per_hr,
            temperature_f=weather.temperature_f,
            weather_description=weather.description[:100],
            forecast_time=weather.forecast_time,
            risk_level=weather.risk.level,
            risk_reasons=list(weather.risk.reasons),
            data_notes=list(weather.risk.data_issues),
        )

    @property
    def is_unknown(self) -> bool:
        return self.risk_level is None


class ForecastCache(models.Model):
    """Hourly forecast for a grid cell, cached so repeated trips and the heatmap don't refetch it.

    Coordinates are rounded to a grid (see `grid_key`) so nearby lookups share a row.
    """

    GRID_PRECISION = 2  # decimal places, ~1.1 km at the equator

    provider = models.CharField(max_length=50)
    lat = models.DecimalField(max_digits=5, decimal_places=GRID_PRECISION)
    lng = models.DecimalField(max_digits=6, decimal_places=GRID_PRECISION)
    hourly = models.JSONField(help_text="Provider's hourly forecast covering at least the next 48 hours.")
    fetched_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "lat", "lng"], name="forecast_unique_grid_cell"),
        ]
        indexes = [models.Index(fields=["expires_at"])]

    def __str__(self) -> str:
        return f"{self.provider} forecast @ {self.lat},{self.lng}"

    @classmethod
    def grid_key(cls, lat: float, lng: float) -> tuple[float, float]:
        return round(lat, cls.GRID_PRECISION), round(lng, cls.GRID_PRECISION)

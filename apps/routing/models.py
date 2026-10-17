from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Prefetch, Q


class RiskLevel(models.IntegerChoices):
    """Weather risk at a checkpoint. Integer values are ordered so they can be averaged and compared."""

    LOW = 0, "Low"
    MODERATE = 1, "Moderate"
    HIGH = 2, "High"
    SEVERE = 3, "Severe"
    NO_TRAVEL = 4, "No Travel"


class SampleInterval(models.IntegerChoices):
    """How far apart weather checkpoints are placed along a route."""

    EVERY_10_MILES = 10, "Every 10 miles"
    EVERY_25_MILES = 25, "Every 25 miles"
    EVERY_50_MILES = 50, "Every 50 miles"


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
    summary = models.CharField(max_length=255, blank=True, help_text="Short label, e.g. 'via I-80 W'.")

    distance_miles = models.FloatField(validators=[MinValueValidator(0)])
    duration_seconds = models.PositiveIntegerField()
    geometry = models.JSONField(help_text="GeoJSON LineString of the route path.")

    # Risk summary, derived from this route's checkpoints. Miles are the route distance
    # each checkpoint stands in for, bucketed by that checkpoint's risk level.
    no_travel_miles = models.FloatField(default=0)
    severe_miles = models.FloatField(default=0)
    high_miles = models.FloatField(default=0)
    average_risk = models.FloatField(default=0, help_text="Mile-weighted mean of RiskLevel values.")
    max_risk_level = models.PositiveSmallIntegerField(choices=RiskLevel.choices, default=RiskLevel.LOW)

    is_recommended = models.BooleanField(default=False)

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
        return f"Trip {self.trip_id} route {self.index}: {self.summary or 'unnamed'}"

    def ranking_key(self) -> tuple[float, float, float, float, int]:
        """Sort key for picking the recommended route — lower is better.

        Follows the assessment's preference order: fewest Severe miles, then fewest High miles,
        then lowest average risk, then shortest travel time. No Travel miles rank ahead of all
        of those, since a route that crosses a No Travel zone should never win on travel time.
        """
        return (
            self.no_travel_miles,
            self.severe_miles,
            self.high_miles,
            self.average_risk,
            self.duration_seconds,
        )


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

    risk_level = models.PositiveSmallIntegerField(choices=RiskLevel.choices, default=RiskLevel.LOW)
    risk_reasons = models.JSONField(
        default=list,
        blank=True,
        help_text="Why this level was assigned, e.g. ['Wind 47 mph with 32,000 lb load'].",
    )

    class Meta:
        ordering = ["route", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["route", "sequence"], name="checkpoint_unique_sequence_per_route"),
        ]

    def __str__(self) -> str:
        return f"Route {self.route_id} checkpoint {self.sequence} @ mile {self.mile_marker:.0f}"


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

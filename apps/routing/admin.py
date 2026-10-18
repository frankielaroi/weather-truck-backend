from django.contrib import admin

from apps.routing.models import Checkpoint, ForecastCache, Route, Trip


class RouteInline(admin.TabularInline[Route, Trip]):
    model = Route
    extra = 0
    fields = ("index", "label", "distance_miles", "duration_seconds", "severe_miles", "high_miles", "is_recommended")
    readonly_fields = fields
    show_change_link = True


class CheckpointInline(admin.TabularInline[Checkpoint, Route]):
    model = Checkpoint
    extra = 0
    fields = (
        "sequence",
        "mile_marker",
        "eta",
        "wind_mph",
        "rain_in_per_hr",
        "snow_in_per_hr",
        "risk_level",
        "data_notes",
    )
    readonly_fields = fields


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin[Trip]):
    list_display = ["id", "origin_name", "destination_name", "departure_at", "load_weight_lb", "status"]
    list_filter = ["status", "sample_interval_miles"]
    search_fields = ["origin_name", "destination_name"]
    list_select_related = ["user"]
    inlines = [RouteInline]


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin[Route]):
    list_display = ["id", "trip", "index", "label", "provider", "distance_miles", "max_risk_level", "is_recommended"]
    list_select_related = ["trip"]
    exclude = ["geometry"]
    inlines = [CheckpointInline]


@admin.register(ForecastCache)
class ForecastCacheAdmin(admin.ModelAdmin[ForecastCache]):
    list_display = ["provider", "lat", "lng", "fetched_at", "expires_at"]
    exclude = ["hourly"]

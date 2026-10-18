"""Request validation. Every rule gives a message under its own field (B10), which the error handler
returns as {"error": {"code": "invalid_input", "fields": {...}}}."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from django.utils import timezone
from rest_framework import serializers

from apps.routing import geocoding
from apps.routing.choices import SampleInterval
from apps.routing.route_providers import haversine_km
from apps.routing.trips import TripRequest

MIN_LOAD_LB: Final = 1
MAX_LOAD_LB: Final = 200_000
DEPARTURE_GRACE: Final = timedelta(hours=1)
# Decision: no more than 48 h ahead, because beyond that the forecast can't cover the trip.
DEPARTURE_HORIZON: Final = timedelta(hours=48)
SAME_PLACE_KM: Final = 0.05


class PlaceQuerySerializer(serializers.Serializer[None]):
    q = serializers.CharField(max_length=geocoding.MAX_QUERY_LENGTH, allow_blank=True, trim_whitespace=True)


class PlaceSerializer(serializers.Serializer[None]):
    name = serializers.CharField(max_length=255, trim_whitespace=True)
    # DRF's FloatField already rejects NaN and infinity, and its JSON parser rejects bare NaN literals.
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)


class TripCreateSerializer(serializers.Serializer[None]):
    origin = PlaceSerializer()
    destination = PlaceSerializer()
    departure_at = serializers.DateTimeField(default_timezone=UTC)
    load_weight_lb = serializers.IntegerField(
        min_value=MIN_LOAD_LB,
        max_value=MAX_LOAD_LB,
        error_messages={
            "min_value": f"Load must be at least {MIN_LOAD_LB:,} lb.",
            "max_value": f"Load can't be more than {MAX_LOAD_LB:,} lb.",
        },
    )
    interval_miles = serializers.ChoiceField(
        choices=[int(choice) for choice in SampleInterval],
        error_messages={"invalid_choice": "Interval must be 10, 25 or 50 miles."},
    )

    def validate_departure_at(self, value: datetime) -> datetime:
        now = timezone.now()
        if value < now - DEPARTURE_GRACE:
            raise serializers.ValidationError("Departure can't be more than 1 hour in the past.")
        if value > now + DEPARTURE_HORIZON:
            raise serializers.ValidationError("Departure can't be more than 48 hours ahead (the forecast's range).")
        return value

    def validate_interval_miles(self, value: object) -> int:
        return int(str(value))

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        origin, destination = attrs["origin"], attrs["destination"]
        if haversine_km((origin["lng"], origin["lat"]), (destination["lng"], destination["lat"])) < SAME_PLACE_KM:
            raise serializers.ValidationError({"destination": ["Destination must be different from the origin."]})
        return attrs

    def to_trip_request(self) -> TripRequest:
        data = self.validated_data
        return TripRequest(
            origin_name=data["origin"]["name"],
            origin=(data["origin"]["lng"], data["origin"]["lat"]),
            destination_name=data["destination"]["name"],
            destination=(data["destination"]["lng"], data["destination"]["lat"]),
            departure_at=data["departure_at"],
            load_weight_lb=data["load_weight_lb"],
            interval_miles=data["interval_miles"],
        )

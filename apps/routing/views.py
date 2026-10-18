from rest_framework import status
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.routing import geocoding
from apps.routing.heatmap import get_or_build_heatmap
from apps.routing.models import Trip
from apps.routing.responses import trip_response
from apps.routing.serializers import PlaceQuerySerializer, TripCreateSerializer
from apps.routing.trips import create_trip


class TripCreateThrottle(AnonRateThrottle):
    """Each new trip can cost several provider requests, so creation is rate-limited per visitor."""

    scope = "trip_create"


@api_view(["GET"])
def health(request: Request) -> Response:
    return Response({"status": "ok"})


@api_view(["GET"])
def place_suggestions(request: Request) -> Response:
    """Up to 5 US places matching `q` (3+ characters), each with a name and coordinates."""
    query = PlaceQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    places = geocoding.suggest(query.validated_data["q"])
    return Response({"results": [place.as_dict() for place in places]})


@api_view(["POST"])
@throttle_classes([TripCreateThrottle])
def trip_create(request: Request) -> Response:
    """Plan a trip: validate, run the coordinator, return the full trip (B9 shape) with 201."""
    serializer = TripCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    trip = create_trip(serializer.to_trip_request())
    return Response(trip_response(Trip.objects.with_details().get(pk=trip.pk)), status=status.HTTP_201_CREATED)


@api_view(["GET"])
def trip_detail(request: Request, trip_id: int) -> Response:
    """A saved trip, so a result can be reloaded or shared by link."""
    trip = Trip.objects.with_details().filter(pk=trip_id).first()
    if trip is None:
        raise NotFound("Trip not found.")
    return Response(trip_response(trip))


@api_view(["GET"])
def trip_heatmap(request: Request, trip_id: int) -> Response:
    """The trip's corridor weather heatmap for hours 0-48, built on first request and then saved.

    Separate from the trip response so the trip can show first and the heatmap load right after.
    """
    trip = Trip.objects.with_details().filter(pk=trip_id).first()
    if trip is None:
        raise NotFound("Trip not found.")
    return Response(get_or_build_heatmap(trip).data)

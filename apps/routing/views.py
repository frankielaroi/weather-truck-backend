from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.routing import geocoding
from apps.routing.serializers import PlaceQuerySerializer


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

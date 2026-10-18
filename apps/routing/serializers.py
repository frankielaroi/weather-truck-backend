from rest_framework import serializers

from apps.routing import geocoding


class PlaceQuerySerializer(serializers.Serializer[None]):
    q = serializers.CharField(max_length=geocoding.MAX_QUERY_LENGTH, allow_blank=True, trim_whitespace=True)

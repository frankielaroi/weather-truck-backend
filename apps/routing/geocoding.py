"""Place suggestions for the trip form (MODULES.md B4), from OpenRouteService's Pelias geocoder.

- Queries with fewer than MIN_QUERY_LENGTH non-space characters return [] without calling the provider.
- Limited to the US (the risk rules use US units). Up to MAX_RESULTS places, each a name + coordinates.
- Cities and towns only (CITY_LAYERS). Left unfiltered, the top results fill up with duplicates ("Springfield,
  MO", "City of Springfield, MO", "Springfield Township, MO"), venues and neighbourhoods, which pushes real
  cities like Springfield, IL out of the list. So we ask for FETCH_SIZE results, drop the "City of" prefix
  and the duplicates it creates, and keep MAX_RESULTS. If a search finds no city or town at all, it is
  repeated once over BROAD_LAYERS (neighbourhoods, townships, counties) so small places still show up.
- Every request has a TIMEOUT_SECONDS timeout. Failures raise ProviderUnavailable (502) or
  ProviderTimeout (504) with a message the driver can read; never a 500.
- Identical searches (case and spacing ignored) are cached for FRESH_SECONDS. If the provider fails,
  an older cached answer up to STALE_SECONDS old is served instead of an error.
- A cache that's unreachable is skipped, not fatal. The API key never leaves the backend.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Final

import requests
from django.conf import settings
from django.core.cache import cache

from apps.routing.errors import ProviderError, ProviderTimeout, ProviderUnavailable

logger = logging.getLogger(__name__)

_NAME_PREFIX = re.compile(r"^(?:City|Town|Village|Borough|Municipality|Township) of\s+", re.IGNORECASE)
MIN_QUERY_LENGTH: Final = 3
MAX_QUERY_LENGTH: Final = 100
MAX_RESULTS: Final = 10
FETCH_SIZE: Final = 15  # fetched, then cleaned and trimmed to MAX_RESULTS
CITY_LAYERS: Final = "locality,borough"
BROAD_LAYERS: Final = "locality,localadmin,neighbourhood,county"
COUNTRY: Final = "US"
TIMEOUT_SECONDS: Final = 5
FRESH_SECONDS: Final = 60 * 60 * 24
STALE_SECONDS: Final = 60 * 60 * 24 * 7
_CACHE_VERSION: Final = "v2"

UNAVAILABLE_MESSAGE: Final = "Place search isn't responding right now. Try again in a moment."
TIMEOUT_MESSAGE: Final = "Place search took too long to respond. Try again in a moment."
BUSY_MESSAGE: Final = "Place search is busy right now. Try again in a minute."


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    lat: float
    lng: float

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lat": self.lat, "lng": self.lng}


def normalize_query(raw: str) -> str:
    """Trim and collapse whitespace, so "  denver   co " and "denver co" are the same search."""
    return " ".join(raw.split())[:MAX_QUERY_LENGTH]


def suggest(query: str) -> list[Place]:
    text = normalize_query(query)
    if len(text.replace(" ", "")) < MIN_QUERY_LENGTH:
        return []

    key = _cache_key(text)
    cached = _cache_get(key)
    if cached is not None and _now() - cached[0] < FRESH_SECONDS:
        return cached[1]

    try:
        places = _fetch(text)
    except ProviderError:
        if cached is None:
            raise
        logger.warning("Geocoding provider failed; serving cached results for %r", text)
        return cached[1]

    _cache_set(key, places)
    return places


# --- Provider ----------------------------------------------------------------


def _fetch(text: str) -> list[Place]:
    if not settings.ORS_API_KEY:
        logger.error("ORS_API_KEY is not set; place search can't run")
        raise ProviderUnavailable(UNAVAILABLE_MESSAGE)

    places = _request(text, CITY_LAYERS)
    if not places:  # no city or town matched: look wider, so small places and odd spellings still show up
        places = _request(text, BROAD_LAYERS)
    return places


def _request(text: str, layers: str) -> list[Place]:
    params: dict[str, str | int] = {"text": text, "boundary.country": COUNTRY, "size": FETCH_SIZE, "layers": layers}
    try:
        response = requests.get(
            f"{settings.ORS_BASE_URL}/pelias/v1/autocomplete",
            params=params,
            headers={"Authorization": settings.ORS_API_KEY, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise ProviderTimeout(TIMEOUT_MESSAGE) from exc
    except requests.RequestException as exc:
        logger.warning("Geocoding request failed: %s", exc)
        raise ProviderUnavailable(UNAVAILABLE_MESSAGE) from exc

    if response.status_code == 429:
        raise ProviderUnavailable(BUSY_MESSAGE)
    if response.status_code in (401, 403):
        logger.error("Geocoding provider rejected the API key (HTTP %s)", response.status_code)
        raise ProviderUnavailable(UNAVAILABLE_MESSAGE)
    if not response.ok:
        logger.warning("Geocoding provider returned HTTP %s", response.status_code)
        raise ProviderUnavailable(UNAVAILABLE_MESSAGE)

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("Geocoding provider returned a non-JSON body")
        raise ProviderUnavailable(UNAVAILABLE_MESSAGE) from exc
    return parse_features(data)


def parse_features(data: object) -> list[Place]:
    """GeoJSON FeatureCollection -> places. Malformed features are skipped; the "City of" prefix is dropped
    and duplicates (case-insensitive, after that) are removed, keeping the provider's order."""
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        logger.warning("Geocoding response had no feature list")
        return []

    places: list[Place] = []
    seen: set[str] = set()
    for feature in features:
        place = _parse_feature(feature)
        if place is None or place.name.casefold() in seen:
            continue
        seen.add(place.name.casefold())
        places.append(place)
        if len(places) == MAX_RESULTS:
            break
    return places


def clean_name(label: str) -> str:
    """ "City of Ogallala, NE, USA" -> "Ogallala, NE, USA" (the provider labels some cities that way)."""
    cleaned = _NAME_PREFIX.sub("", label.strip())
    return cleaned or label.strip()


def _parse_feature(feature: object) -> Place | None:
    if not isinstance(feature, dict):
        return None
    properties = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        return None
    name = properties.get("label") or properties.get("name")
    coords = geometry.get("coordinates")
    if not isinstance(name, str) or not name.strip() or not isinstance(coords, list) or len(coords) < 2:
        return None
    lng, lat = coords[0], coords[1]
    if not _is_coordinate(lat, 90) or not _is_coordinate(lng, 180):
        return None
    return Place(name=clean_name(name), lat=float(lat), lng=float(lng))


def _is_coordinate(value: object, limit: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and -limit <= value <= limit


# --- Cache (best effort: a broken cache never breaks search) -----------------


def _now() -> float:
    return time.time()


def _cache_key(text: str) -> str:
    digest = hashlib.sha256(text.casefold().encode()).hexdigest()[:32]
    return f"geocode:{_CACHE_VERSION}:{COUNTRY}:{digest}"


def _cache_get(key: str) -> tuple[float, list[Place]] | None:
    try:
        entry = cache.get(key)
    except Exception:
        logger.warning("Geocoding cache read failed", exc_info=True)
        return None
    if not isinstance(entry, dict):
        return None
    try:
        places = [Place(**place) for place in entry["places"]]
        return float(entry["fetched_at"]), places
    except (KeyError, TypeError, ValueError):
        return None


def _cache_set(key: str, places: list[Place]) -> None:
    entry = {"fetched_at": _now(), "places": [place.as_dict() for place in places]}
    try:
        cache.set(key, entry, timeout=STALE_SECONDS)
    except Exception:
        logger.warning("Geocoding cache write failed", exc_info=True)

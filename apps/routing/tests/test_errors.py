"""The one API error shape (MODULES.md B10), for every kind of exception a view can raise."""

from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404
from rest_framework import exceptions
from rest_framework.test import APIRequestFactory

from apps.routing.errors import ProviderTimeout, ProviderUnavailable, api_exception_handler

CONTEXT: dict[str, Any] = {"request": APIRequestFactory().get("/api/x/"), "view": None}


def handle(exc: Exception) -> tuple[int, Any]:
    response = api_exception_handler(exc, CONTEXT)
    return response.status_code, response.data


def test_django_404_uses_the_error_shape() -> None:
    status, body = handle(Http404("Trip 9 not found"))
    assert status == 404
    assert body["error"]["code"] == "not_found"
    assert "Trip 9" not in body["error"]["message"]


def test_django_permission_denied_uses_the_error_shape() -> None:
    status, body = handle(PermissionDenied())
    assert status == 403
    assert body["error"]["code"] == "permission_denied"


def test_field_errors_are_listed_per_field() -> None:
    status, body = handle(exceptions.ValidationError({"load_weight_lb": ["Must be at least 1."], "interval": "Bad"}))
    assert status == 400
    assert body == {
        "error": {
            "code": "invalid_input",
            "message": "Some inputs need fixing.",
            "fields": {"load_weight_lb": ["Must be at least 1."], "interval": ["Bad"]},
        }
    }


def test_nested_field_errors_use_dotted_names() -> None:
    detail: Any = {"origin": {"lat": ["Too far north."], "name": ["Required."]}, "stops": [{"lng": ["Bad."]}, {}]}
    _, body = handle(exceptions.ValidationError(detail))
    assert body["error"]["fields"] == {
        "origin.lat": ["Too far north."],
        "origin.name": ["Required."],
        "stops.0.lng": ["Bad."],
    }


def test_mixed_lists_keep_plain_messages_under_the_field() -> None:
    _, body = handle(exceptions.ValidationError({"stops": ["Too many.", {"lat": ["Bad."]}]}))
    assert body["error"]["fields"] == {"stops": ["Too many."], "stops.1.lat": ["Bad."]}


@pytest.mark.parametrize("detail", [["Origin and destination must differ."], "Origin and destination must differ."])
def test_non_field_errors_are_grouped(detail: Any) -> None:
    _, body = handle(exceptions.ValidationError(detail))
    assert body["error"]["fields"] == {"non_field_errors": ["Origin and destination must differ."]}


def test_custom_codes_are_kept() -> None:
    status, body = handle(exceptions.APIException("No route found between these places.", code="no_route"))
    assert status == 500
    assert body == {"error": {"code": "no_route", "message": "No route found between these places."}}


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [(ProviderUnavailable(), 502, "provider_unavailable"), (ProviderTimeout(), 504, "provider_timeout")],
)
def test_provider_errors(exc: Exception, status: int, code: str) -> None:
    got_status, body = handle(exc)
    assert got_status == status
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def test_unexpected_exception_is_a_500_without_details() -> None:
    status, body = handle(KeyError("secret internals"))
    assert status == 500
    assert body["error"]["code"] == "server_error"
    assert "secret" not in str(body)

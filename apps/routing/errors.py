"""One error shape for the whole API (MODULES.md B10).

Every error response body is:

    {"error": {"code": "<machine code>", "message": "<sentence for the driver>", "fields": {...}}}

`fields` appears only on 400s and maps each bad input to its messages, so the frontend can show
each one under its field. Unexpected exceptions become a plain 500 in this shape too; tracebacks
are logged, never sent.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class ProviderError(APIException):
    """A third-party service failed. Subclasses set the status and wording."""

    status_code: int = status.HTTP_502_BAD_GATEWAY
    default_code = "provider_unavailable"
    default_detail = "An outside service didn't respond properly. Try again in a moment."


class ProviderUnavailable(ProviderError):
    pass


class ProviderTimeout(ProviderError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    default_code = "provider_timeout"
    default_detail = "An outside service took too long to respond. Try again in a moment."


def error_body(code: str, message: str, fields: dict[str, list[str]] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return {"error": error}


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    # Django's own 404/403 become DRF exceptions first, so they get the same shape as everything else.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, PermissionDenied):
        exc = exceptions.PermissionDenied()
    response = exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled API error", exc_info=exc)
        return Response(
            error_body("server_error", "Something went wrong on our side. Try again in a moment."),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, ValidationError):
        response.data = error_body("invalid_input", "Some inputs need fixing.", _field_messages(exc.detail))
        return response

    if not isinstance(exc, APIException):  # pragma: no cover - DRF only answers APIExceptions
        return response
    codes = exc.get_codes()
    code = codes if isinstance(codes, str) else exc.default_code
    response.data = error_body(str(code), str(exc.detail))
    return response


def _field_messages(detail: object) -> dict[str, list[str]]:
    if isinstance(detail, dict):
        return {str(field): [str(m) for m in _as_list(messages)] for field, messages in detail.items()}
    return {"non_field_errors": [str(m) for m in _as_list(detail)]}


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else [value]


class NoRoute(APIException):
    status_code: int = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "no_route"
    default_detail = "No route found between these places."

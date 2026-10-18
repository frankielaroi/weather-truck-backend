from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

HEALTH_PATH = "/api/health/"


class HealthCheckMiddleware:
    """Answers the health check before Django's host validation.

    Hosts probe a container by its private IP (e.g. `Host: 172.26.4.7:8000`), which ALLOWED_HOSTS
    would reject with a 400. This answers that one trivial path directly; every other request goes
    through normal host validation.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path == HEALTH_PATH and request.method in ("GET", "HEAD"):
            return JsonResponse({"status": "ok"})
        return self.get_response(request)

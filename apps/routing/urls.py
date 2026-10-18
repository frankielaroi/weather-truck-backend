from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("places/", views.place_suggestions, name="place-suggestions"),
]

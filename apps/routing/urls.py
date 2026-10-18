from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("places/", views.place_suggestions, name="place-suggestions"),
    path("trips/", views.trip_create, name="trip-create"),
    path("trips/<int:trip_id>/", views.trip_detail, name="trip-detail"),
]

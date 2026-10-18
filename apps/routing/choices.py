"""Choice enums shared by models and pure logic (importable without the Django app registry)."""

from django.db import models


class RiskLevel(models.IntegerChoices):
    """Weather risk at a checkpoint. Integer values are ordered so they can be averaged and compared."""

    LOW = 0, "Low"
    MODERATE = 1, "Moderate"
    HIGH = 2, "High"
    SEVERE = 3, "Severe"
    NO_TRAVEL = 4, "No Travel"


class SampleInterval(models.IntegerChoices):
    """How far apart weather checkpoints are placed along a route."""

    EVERY_10_MILES = 10, "Every 10 miles"
    EVERY_25_MILES = 25, "Every 25 miles"
    EVERY_50_MILES = 50, "Every 50 miles"

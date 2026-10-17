from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Custom user, set as AUTH_USER_MODEL from the start so it can grow without a painful swap."""

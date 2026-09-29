"""Django application configuration for django-jj (SPEC § 17)."""

from __future__ import annotations

from django.apps import AppConfig


class JjConfig(AppConfig):
    """App config: module ``django_jj``, app label ``jj``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "django_jj"
    label = "jj"
    verbose_name = "Jujutsu store"
    default = True

    def ready(self) -> None:
        """Register system checks. No queries, no model instantiation, no seam resolution."""
        from . import checks  # noqa: F401

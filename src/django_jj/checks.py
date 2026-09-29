"""System checks for django-jj (SPEC § 17)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.apps import AppConfig
from django.core.checks import CheckMessage, Error, register
from django.db import connections, router

APP_LABEL = "jj"


@register()
def check_database_vendor(
    app_configs: Sequence[AppConfig] | None, **kwargs: Any
) -> list[CheckMessage]:
    """``jj.E001``: every database that may receive ``jj`` migrations is PostgreSQL."""
    errors: list[CheckMessage] = []
    for alias in connections:
        if not router.allow_migrate(alias, APP_LABEL):
            continue
        vendor = connections[alias].vendor
        if vendor != "postgresql":
            errors.append(
                Error(
                    f"django-jj requires PostgreSQL, but database {alias!r} is {vendor!r}.",
                    hint=(
                        "Use django.db.backends.postgresql for this database, or route "
                        "the 'jj' app's migrations away from it."
                    ),
                    id="jj.E001",
                )
            )
    return errors

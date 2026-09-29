"""Settings access for django-jj (SPEC § 16).

Every setting is a flat ``JJ_*`` name with a default here. Values are read lazily on
attribute access and validated by system checks, never at import time.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

# Built-in derivers land in milestone M5 (SPEC § 11); hosts extend this tuple.
DEFAULT_DERIVERS: tuple[str, ...] = ()

DEFAULTS: dict[str, Any] = {
    "JJ_AUTHENTICATOR": None,  # must be set: header-only authentication (SPEC § 13.2)
    "JJ_AUTHORIZER": "django_jj.authz.DjangoModelPermissionsAuthorizer",
    "JJ_DERIVERS": DEFAULT_DERIVERS,
    "JJ_FILE_STORAGE": "default",
    "JJ_FILE_INLINE_MAX_BYTES": 262_144,
    "JJ_MAX_BATCH_ITEMS": 1_000,
    "JJ_MAX_REQUEST_BYTES": 67_108_864,
    "JJ_VERIFY_COMMITTER": True,
    "JJ_CONCURRENCY_HINT": 64,
    "JJ_RESOLVE_TIMEOUT_SECONDS": 5,
    "JJ_MERGE_LEASE_SECONDS": 30,
    "JJ_WORKER_COMMAND": None,
    "JJ_TEST_TOKENS": {},
}


class AppSettings:
    """Attribute access to ``JJ_*`` settings with library defaults."""

    def __getattr__(self, name: str) -> Any:
        if name not in DEFAULTS:
            raise AttributeError(name)
        return getattr(settings, name, DEFAULTS[name])


app_settings = AppSettings()

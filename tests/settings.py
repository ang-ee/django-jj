"""Test settings. PostgreSQL is required; connection details come from the environment."""

from __future__ import annotations

import os

SECRET_KEY = "django-jj-tests-not-secret"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django_jj",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("JJ_TEST_DATABASE_NAME", "django_jj"),
        "USER": os.environ.get("JJ_TEST_DATABASE_USER", ""),
        "PASSWORD": os.environ.get("JJ_TEST_DATABASE_PASSWORD", ""),
        "HOST": os.environ.get("JJ_TEST_DATABASE_HOST", ""),
        "PORT": os.environ.get("JJ_TEST_DATABASE_PORT", ""),
    }
}

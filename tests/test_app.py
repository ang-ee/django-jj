"""App configuration, settings defaults and the PostgreSQL check."""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db import connections

import django_jj
from django_jj.checks import check_database_vendor
from django_jj.conf import DEFAULTS, app_settings


def test_app_label_and_module():
    config = apps.get_app_config("jj")
    assert config.name == "django_jj"
    assert config.verbose_name == "Jujutsu store"


def test_version_is_exposed():
    assert django_jj.__version__


def test_settings_defaults(settings):
    assert app_settings.JJ_VERIFY_COMMITTER is True
    settings.JJ_VERIFY_COMMITTER = False
    assert app_settings.JJ_VERIFY_COMMITTER is False
    assert all(name.startswith("JJ_") for name in DEFAULTS)


def test_unknown_setting_raises():
    with pytest.raises(AttributeError):
        _ = app_settings.JJ_NOT_A_SETTING


def test_postgresql_passes_check():
    assert [m.id for m in check_database_vendor(None)] == []


def test_other_vendor_fails_check(monkeypatch):
    monkeypatch.setattr(connections["default"], "vendor", "sqlite")
    assert [m.id for m in check_database_vendor(None)] == ["jj.E001"]

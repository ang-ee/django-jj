"""django-jj: a Jujutsu (jj) repository store for Django.

jj's objects (files, symlinks, trees, commits, copy histories) and its operation log
(operations, views, the operation-heads set) are stored as PostgreSQL rows and served to
the ``jj-django`` client over a batch HTTP protocol. See ``docs/SPEC.md``.

The public API (SPEC § 18) is added milestone by milestone; model classes are never
imported here eagerly, because this module loads during ``INSTALLED_APPS`` setup.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

__all__ = ["__version__"]

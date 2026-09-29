# Contributing

Read [AGENTS.md](./AGENTS.md) (or [CLAUDE.md](./CLAUDE.md)) and
[docs/SPEC.md](./docs/SPEC.md) first. Structural changes start as a spec change or a
[proposal](./docs/proposals/).

## Local setup

- Python 3.14+
- `uv`
- PostgreSQL 17 (required — the library does not support other databases)

```bash
uv venv --python 3.14
make install-dev
createdb django_jj            # or point JJ_TEST_DATABASE_* at another server
```

Test database settings come from the environment (see `tests/settings.py`):
`JJ_TEST_DATABASE_NAME` (default `django_jj`), `JJ_TEST_DATABASE_USER`,
`JJ_TEST_DATABASE_PASSWORD`, `JJ_TEST_DATABASE_HOST`, `JJ_TEST_DATABASE_PORT`.

## Canonical commands

```bash
make lint
make test
make check      # ruff lint + format check, mypy --strict, pyright, pytest
```

From milestone M1 the Rust client lives in `client/`:
`cargo fmt --check && cargo clippy -- -D warnings && cargo test`.

## CI matrix

Python 3.14 × Django 6.0 × PostgreSQL 17.

## Commit hygiene

- One concern per change; tests with every behaviour change.
- Record breaking changes in `CHANGELOG.md`.

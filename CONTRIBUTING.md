# Contributing

Read [AGENTS.md](./AGENTS.md) (or [CLAUDE.md](./CLAUDE.md)) and
[docs/SPEC.md](./docs/SPEC.md) first. Structural changes start as a spec change or a
[proposal](./docs/proposals/).

## Local setup

- Python 3.14+
- `uv`
- PostgreSQL 17 (required — the library does not support other databases)
- [jj](https://github.com/jj-vcs/jj) (Jujutsu) 0.45 or later

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

## Version control

The repository is a colocated jj + git repository, developed with jj. Clone it with
`jj git clone --colocate https://github.com/ang-ee/django-jj.git`, or run
`jj git init --colocate` in an existing git clone. Use jj for every write; the rules and
the publish recipe are in [AGENTS.md](./AGENTS.md#version-control-jj). In short:

```bash
jj git fetch
jj new main
# edit
jj commit -m "<what changed>"
jj bookmark create <topic> -r @-
jj git push -b <topic>            # then open a pull request
```

## Change hygiene

- One concern per change; tests with every behaviour change.
- Record breaking changes in `CHANGELOG.md`.

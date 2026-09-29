# django-jj

> A [Jujutsu](https://github.com/jj-vcs/jj) repository store for Django: jj's commits,
> trees and operation log as PostgreSQL rows, served to a thin jj client over HTTP.

---

> **Status: pre-implementation.** This repository currently holds the
> [specification](./docs/SPEC.md), the [roadmap](./docs/ROADMAP.md) and a package
> skeleton. Nothing below works yet; APIs shown are the planned ones.

---

## What it is

jj stores a repository through pluggable stores: a commit backend, an operation store
and an operation-heads store. `django-jj` implements all three **on the server**, inside
a Django app, and ships **`jj-django`** — stock `jj` built with store implementations
that talk to that server.

- **Real jj, shared.** People and agents run `jj new`, `describe`, `squash`, `log`,
  `op log` and `undo` against one server-hosted repository. Concurrent commands never
  lose work, and none is rejected because another landed first: jj's lock-free operation
  log merges divergent operations, as it does locally. Ids are jj's own — computed by
  the writer, verified by the server.
- **Server-side writes.** Your application can edit files in a named jj workspace; each
  edit becomes a commit and an operation, exactly as if a client had made it. Conflicts
  are stored as data, not refused.
- **Publications.** A publication is a server-owned, versioned pointer to a commit —
  what your application treats as published. It is the one place where policy (who may
  publish, fast-forward only, no conflicts) can say no.
- **History in SQL.** Changed paths, the commit graph, refs and conflicts are derived
  into ordinary tables, asynchronously and rebuildably. Register your own derivers to
  project a repository into search indexes, sites or knowledge bases.
- **Permissions down to paths.** Every request names a repository and every object read
  names a path, so your authorization layer can grant access per repository,
  publication, workspace and path — including hiding the contents of `hr/` from readers
  of the rest of the repository (those readers work in a sparse checkout that excludes
  it).

## How it works

```
 jj-django (stock jj + remote stores)          your Django project
 ┌──────────────────────────────┐             ┌──────────────────────────────────┐
 │ jj commands                  │  batch HTTP  │ django_jj (app label `jj`)       │
 │ RemoteBackend / OpStore /    │─────────────▶│  objects, operations, op heads   │
 │ OpHeadsStore                 │              │  publications, derived tables    │
 │ local index + working copy   │              │  your authenticator / authorizer │
 └──────────────────────────────┘             └──────────────────────────────────┘
```

## Planned quickstart

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "django_jj",
]
JJ_AUTHENTICATOR = "myproject.vcs.TokenAuthenticator"  # required; header tokens only
JJ_AUTHORIZER = "myproject.vcs.MyAuthorizer"          # default: Django model permissions

# urls.py
urlpatterns = [
    path("jj/", include("django_jj.urls")),
]
```

```bash
python manage.py migrate
python manage.py jj create-repo notes
jj-django clone https://example.com/jj/v1/repos/notes
cd notes && echo "hello" > README.md && jj describe -m "first"
```

```python
from django_jj import edit_workspace, publish

result = edit_workspace(repo, actor, "web@alice",
                        edits=[("docs/intro.md", b"# Intro\n")],
                        description="Edit intro")
publish(repo, "site", result.commit_id, expected_version=3, actor=actor)
```

## What it is not

Not a forge, not a git server (git is import/export only), not an authentication system,
not a permission engine, not a real-time editor, and not a fork of jj. It requires
PostgreSQL.

## Documentation

- [docs/SPEC.md](./docs/SPEC.md) — the specification
- [docs/ROADMAP.md](./docs/ROADMAP.md) — milestones and gates
- [docs/proposals/](./docs/proposals/) — design changes

## License

Apache-2.0. See [LICENSE](./LICENSE).

# AGENTS.md

Guidance for Codex working in the `django-jj` repository.

> See `docs/SPEC.md` for the design contract. It is the source of truth.

---

## Project overview

`django-jj` is a **standalone Jujutsu (jj) repository store for any Django 6.0 project
on PostgreSQL**. jj's objects (files, symlinks, trees, commits, copy histories) and its
operation log (operations, views, the operation-heads set) are ORM rows, served over a
batch HTTP protocol to a thin jj client, **`jj-django`**, built from the pinned `jj-lib`
and `jj-cli` crates. The server can also write (edits become commits in a workspace),
defines **publications** (the one refusing compare-and-swap), computes rebuildable
**derived data**, and exposes **authentication and authorization seams** down to path
granularity.

**Status: pre-implementation.** The repository holds the specification, agent
instructions and a minimal package skeleton. Build order and gates are in
`docs/ROADMAP.md`. Layout:

```
django-jj/
├── README.md                 # Public pitch
├── AGENTS.md                 # This file
├── docs/
│   ├── SPEC.md               # The specification
│   ├── ROADMAP.md            # Milestones and gates
│   └── proposals/            # Numbered design changes
├── pyproject.toml
├── src/django_jj/            # Python package (app label `jj`)
├── tests/
├── proto/                    # Protocol messages (from M1)
├── client/                   # Rust workspace: jj-django (from M1)
└── vectors/                  # Golden id vectors generated from jj-lib (from M1)
```

---

## Documentation hierarchy

| Doc | Purpose | When to read |
|---|---|---|
| `README.md` | What the package is and is not. | Always. |
| `docs/SPEC.md` | Architecture, invariants, data model, protocol, seams, settings, checks, client, testing. | Before adding any code, changing a public API, a model, a setting, or the protocol. |
| `docs/ROADMAP.md` | Milestones, deliverables and gates. | Before starting a milestone. |
| `docs/proposals/` | Design changes under discussion. | Before changing an invariant or a public surface. |

**If a behaviour isn't specified, propose a spec change first.** Don't implement
undocumented behaviour and then patch the spec to match.

---

## Naming — locked

| Concept | Value |
|---|---|
| Pip distribution | `django-jj` |
| Python module | `django_jj` (the top-level name `jj` belongs to an unrelated PyPI package) |
| App label | `jj` |
| Settings prefix | `JJ_*` |
| Management command | `manage.py jj <subcommand>` |
| System-check IDs | `jj.E001` … / `jj.W001` … |
| jj store type name | `django-jj` |
| Client binary | `jj-django` |
| Client adapter crate | `jj-django-store` |
| URL include | `include("django_jj.urls")`, endpoints under `jj/v1/` |
| Public Python imports | `from django_jj import ...` |

Don't propose alternatives without a spec change first.

---

## Critical project invariants

Non-negotiable. Each is argued in `docs/SPEC.md § 5`.

### 1. jj semantics are the public contract

The server stores what jj writes and serves what jj reads. Store endpoints map to
`jj-lib` trait methods (`Backend`, `OpStore`, `OpHeadsStore`); their names and meaning
follow jj.

- **Don't** add server behaviour jj cannot represent or would not produce.
- **Don't** reimplement jj algorithms in Python — view merge, rebase, conflict
  resolution, `Merge<TreeId>` simplification beyond "all terms equal". They run in jj
  (`jj-django worker`).

### 2. Op heads never refuse on staleness

`update_op_heads(old_ids, new_id)` inserts the new head, deletes the old ones, and logs —
in one transaction — **whatever the current head set is**. It refuses only for
authentication, authorization, a missing operation/view, or an attempt to remove a head
that is not an ancestor of the new operation (`SPEC § 8.1`). A compare-and-swap that
fails because `old_ids` is stale is a bug: jj assumes "the operation cannot fail to
commit". Lock waits and deadlocks inside it are retried, never surfaced. The one refusing
CAS is the **publication** (`SPEC § 9`).

### 3. Canonical rows are insert-only

`File`, `Symlink`, `Tree`, `Commit`, `Copy`, `View`, `Operation` rows are never updated.
Only garbage collection deletes them. Decoded columns are filled at insert and are
denormalizations, never a second source of truth.

### 4. jj-native ids, verified by the server, never rewritten

Writers compute ids with jj's own hashing — the client with jj-lib itself, server-side
writes with the Python port. The server **strictly decodes** every upload with its own
wire parser and requires canonical bytes (`SPEC § 6.2`, `§ 6.7`), recomputes the id of
every new object, requires a re-upload of an existing id to equal the stored value (proof
of possession), and rejects mismatches; it never trusts a claimed id and never rewrites a
value (the client stamps the committer before hashing; the server *verifies* it —
`SPEC § 6.5`). Stored ids are never rehashed. Golden vectors in `vectors/` are the
authority for the hashing scheme; Python/jj-lib parity is load-bearing.

### 5. No grants on content-addressed objects

Authorization is decided per repository, publication, workspace, ref and path — through
the `Authorizer` seam — never per object id. **Don't** add a permission field, owner or
ACL to a canonical model.

### 6. Placement before path decisions

In a repository with path rules, serve an object at a path only if `Placement` records it
there (or it is a leaf the actor wrote), and record a new placement — **including every
descendant of a moved tree** — only if it passes the graft rules (`SPEC § 13.4`; "written
by the actor" applies to leaves only). Skipping any of this lets a reader fetch restricted
content under a permitted path — directly, or by wrapping a restricted subtree in a tree
of their own. Decide readability before any lookup and report invisible references as
missing: a response that depends on hidden state is an existence oracle.

### 7. Batch-only protocol

Every client endpoint takes a list and returns per-item status. **Don't** add a
one-object-per-request endpoint; per-object round trips are how database-backed VCS
stores fail.

### 8. Derived data is rebuildable and off the write path

Derivers run after the op-heads transaction commits, from `OpHeadLog` and
`PublicationLog` — never in the transaction that logs the change, and never able to fail
or roll back a write. Every derived table is reproducible by `manage.py jj rebuild` (built
side by side, `SPEC § 11.5`); tests assert incremental equals rebuild. **Don't** derive
inside `update_op_heads` or in a signal handler that runs before commit.

### 9. Determinism

Encoding and hashing are deterministic. No timestamps, randomness or dict/set iteration
order in anything that feeds an id. Server-generated change ids are the only random
values, and only where jj itself generates them randomly.

### 10. Exact jj pin

`jj-lib` and `jj-cli` are pinned with `=` across the client workspace (`jj-django-store`,
the `jj-django` binary, the vector generator). Store logic touches jj-lib only in
`jj-django-store`. Moving the pin is its own change: regenerate vectors, run the full e2e
suite, record id changes in `CHANGELOG.md`.

### 11. PostgreSQL only

The design relies on `bytea`, TOAST, transactional DDL, `SELECT … FOR UPDATE`, advisory
locks and recursive CTEs. `jj.E001` fails on any other backend. **Don't** add SQLite or
MongoDB compatibility shims.

### 12. No wedges

jj clients fix ids locally and assume writes succeed, so a deterministic refusal of a
journaled object is permanent for that client. Refuse only input the pinned jj writer
never emits, unauthorized changes, and policy; constrain deprecated fields the writer
still emits instead of rejecting them; keep the client's recovery path for permanently
refused items working (`SPEC § 5`, `§ 6.3`).

---

## Standalone-ness rule

This package **must work in any Django project**:

- **No imports from any consumer framework** — in source, tests, examples or docs.
- **No references to a specific consumer framework** in `README.md` or `docs/`.
- **No dependency on a permission engine** (for example `django-zed-rebac`). Hosts adapt
  their engine through `JJ_AUTHORIZER`. A permission-engine model mixin cannot be applied
  to this package's models without depending on that engine — which is why
  permission-bearing records belong to the host, attached to `Repo` / `Publication`.
- **No `[tool.<framework>]` config** in `pyproject.toml`.

Adapter points: `JJ_AUTHENTICATOR`, `JJ_AUTHORIZER`, `JJ_DERIVERS`, `JJ_FILE_STORAGE`,
`JJ_DIRECT_UPLOAD_ADAPTER`, `JJ_WORKER_ENABLED` / `JJ_WORKER_COMMAND`, the
`django_jj.tasks.derive` task, and the `op_heads_updated` / `publication_moved` /
`worker_requested` signals.
Consumers depend on the library, never the reverse.

---

## What this package is NOT (drift signals)

Reject scope creep. These belong elsewhere:

- **Not a forge** — no issues, reviews, pull requests or web UI.
- **Not a git server** — no git wire protocol. Git is import/export (`[git]` extra).
- **Not an authentication system** — no tokens, logins or user models.
- **Not a permission engine** — no roles, relations or ACL storage.
- **Not a knowledge base or CMS** — hosts project repositories into those via derivers.
- **Not a real-time editor** — no CRDT/OT.
- **Not a jj fork** — the client is `jj-cli` + three store implementations + `init`,
  `clone`, `publications`, `journal`, `worker`.

If a request blurs one of these lines, the answer is "different package, not here".

---

## Implementation guidelines

### Tooling

- **Build:** `setuptools` via `pyproject.toml` (PEP 621); source layout `src/django_jj/`.
- **Lint/format:** `ruff` (line length 100) and `ruff format`.
- **Types:** `mypy --strict` and `pyright`, both clean. Ship `py.typed`.
- **Test:** `pytest` + `pytest-django` against **PostgreSQL** (`JJ_TEST_DATABASE_*`
  environment variables; see `tests/settings.py`).
- **Rust (from M1):** `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test` in
  `client/`.
- **Matrix:** Python 3.14 × Django 6.0 × PostgreSQL 17. `ruff` targets 3.14: an
  unparenthesised multi-exception clause (`except A, B:`, PEP 758) is the formatter's
  canonical form — don't fight it with `# fmt: skip`.

### AppConfig

Exactly as `docs/SPEC.md § 17`: `name = "django_jj"`, `label = "jj"`, and `ready()` only
imports `checks` (and `signals` once it exists). No queries, no model instantiation, no
seam resolution at import time — seams resolve lazily on first use.

### Migrations

- Indexes and constraints ship in the migration that creates the table.
- Every `RunSQL` has `reverse_sql`.
- PostgreSQL-specific operations are fine (and expected). `django.contrib.postgres` is a
  required co-installed app (`bytea[]` columns).
- Settle the canonical primary-key shape before the first migration (`SPEC § 22` Q14);
  Django cannot migrate between key shapes later.

### Settings

- Flat `JJ_*` names; **no nested dicts**. Read through `django_jj.conf.app_settings`.
- Validate in system checks (`jj.E…`, `jj.W…`), never at import or in `ready()`.

### Public API

`docs/SPEC.md § 18`. Adding to it needs a spec entry; removing is breaking. Anything
under `django_jj._internal` is private.

---

## Common pitfalls

### Don't compare `old_ids` in `update_op_heads`
See invariant 2. The test suite includes a race that must leave two heads, not an error.

### Don't hash the stored protobuf bytes
Ids are jj `ContentHash` over the decoded value (files and symlinks: over raw bytes), so
two valid encodings of one unsigned value get one id. Signed commits: jj signs, and
verifies against, its own prost encoding of the unsigned commit (field 9 `secure_sig`
precedes field 10 `conflict_labels`), so the port needs a prost-exact `Commit` encoder and
uploads must be byte-canonical (`SPEC § 6.2`). Use the port in `django_jj._internal`,
checked by golden vectors.

### Don't validate uploads with a general protobuf runtime
Python's protobuf runtime accepts inputs jj's decoder rejects (a known field with the
wrong wire type, an overflowing ten-byte varint) and reports no unknown fields for them.
Use the project's strict wire parser and the canonical-bytes check (`SPEC § 6.2`).

### Don't refuse what the pinned writer emits
jj 0.45.1 still writes `Commit.predecessors`, `Bookmark.remote_bookmarks` and
`View.git_head`. Rejecting them wedges every client whose journal holds such an object;
require them to equal their derived projection instead (`SPEC § 6.7`).

### Don't normalize trees into entry rows
Trees are opaque rows. Queryable history comes from `ChangedPath`, which grows with
changes rather than tree size × versions.

### Don't query per object
Batch reads use `object_id = ANY(%s::bytea[])` per kind. A loop of `.get()` calls in a protocol
view is a bug.

### Don't write derived data in the request transaction
Emit on `transaction.on_commit`; let `run_pending` do the work.

### Don't rewrite anything
The client computes ids locally and caches what it wrote under them; a server-side rewrite
would give one id two meanings. Verify (committer identity, references, placement) and
refuse — never alter.

### Don't attribute a merge's values to one parent
In an operation with several parents, a value is inherited only if every parent has it and
the operation keeps it; everything else is a change (`SPEC § 8.4`). jj folds N heads
pairwise, in a client-controlled order, with a different base at each step, so
base-relative attribution is forgeable: pairing the current head with a fresh child of an
old operation rolls protected refs back. Merges are the worker's job (`SPEC § 8.2`).

### Don't number log rows with a sequence
`OpHeadLog.seq` and `PublicationLog.seq` come from the per-repository `LogCounter` under
its row lock. A sequence value is assigned at insert, not at commit, so a watermark reader
skips rows that commit late (`SPEC § 8.1`).

### Don't back `lock()` with a blocking server lock
jj takes it on every commit. It is a no-op guard unless the last head read was divergent;
the server side is a non-blocking try-lock that the client polls briefly (`SPEC § 8.3`).

### Don't treat a returned id as uploaded
The client returns ids before the server has the object. Journal every write, deduplicate
only against acknowledged objects, upload leaves eagerly and everything else as the
topologically ordered closure of the head update — never as parallel batches that
reference each other — and resume pending head updates on the next start (`SPEC § 6.3`).

### Don't leak existence through statuses
With path rules, answer about unreadable paths before any lookup, report references the
actor cannot see exactly like missing ones, and never add a "which of these do you have"
endpoint (`SPEC § 13.4`).

### Don't read `request.body` in protocol views
Django caps it at `DATA_UPLOAD_MAX_MEMORY_SIZE` (2.5 MB by default). Read with
`request.read(n)` and enforce `JJ_MAX_REQUEST_BYTES` yourself (`SPEC § 12.1`).

### Don't authenticate from the session
Protocol endpoints are CSRF-exempt because authenticators read the `Authorization` header
only. `request.user` is filled from cookies by Django's session middleware — never use it
in an authenticator (`SPEC § 13.2`).

### Don't let a clone use the workspace name `default`
Every clone would fight over one `wc_commit_ids` entry. `jj-django clone` picks a unique
name (`SPEC § 14.4`).

### Don't import models in `apps.py` at module level
`AppRegistryNotReady`. Import inside `ready()` or the function that uses them.

### Don't query the database in `ready()`
It breaks `migrate`, `makemigrations` and test database setup.

### Don't let jj-lib types leak out of `jj-django-store`
The binary crate and tests talk to the adapter's own types so a pin move touches one crate.

---

## Workflow

1. **Read the relevant spec section** before changing behaviour.
2. **One concern per change.** Protocol, models and client changes are separate changes
   unless the spec ties them.
3. **Spec first for structural changes** — a new endpoint, model, setting, check or
   public name lands in `docs/SPEC.md` (or a proposal) before code.
4. **Run the verification chain** before reporting complete:
   - `make check` (ruff lint + format check, mypy --strict, pyright, pytest on PostgreSQL)
   - from M1: `cargo fmt --check && cargo clippy -- -D warnings && cargo test` in `client/`
   - from M1: the golden-vector suite
   - `make agents-check` (included in `make check`) — `CLAUDE.md` must equal `AGENTS.md`
     apart from its three header lines; edit both together
5. **No backwards-compat shims during 0.x.** Record breaking changes in `CHANGELOG.md`.

---

## Version control: jj

The project stores jj repositories, and it is developed with jj. The repository is a
**colocated jj + git repository** — `.jj/` sits beside `.git/` — so GitHub, CI and read-only
git keep working.

- **Use jj for every write:** `jj new`, `jj describe`, `jj commit`, `jj squash`,
  `jj rebase`, `jj bookmark`, `jj git fetch`, `jj git push`. Read-only git (`log`, `show`,
  `diff`, `blame`) is fine; don't `git commit`, `git checkout`, `git reset`, `git rebase` or
  `git stash` here.
- **One concern per change**, each with a description. Changes reach `main` through a
  bookmark and a pull request.
- **Never rewrite published history.** Commits on `main@origin` are immutable to jj; never
  pass `--ignore-immutable`, and never push `main` backwards.

```sh
jj git fetch
jj new main                       # start a change on the latest main
# edit; jj snapshots the working copy on every command
jj commit -m "<what changed>"     # seal it; @ becomes a new empty change
jj bookmark create <topic> -r @-
jj git push -b <topic>            # then open a pull request for <topic>
```

To revise a pushed topic, change it in place (`jj edit <change>`, or `jj squash` from a
change on top) and run `jj git push -b <topic>` again. After the pull request merges,
`jj git fetch` and start the next change with `jj new main`.

---

## Open design questions tracked in the spec

`docs/SPEC.md § 22` lists them (Q1–Q14: server-side jj semantics, id authority,
divergence resolution, wire format, large-file upload, garbage collection, name hiding,
redaction, the `jj-core` split, an upstream remote protocol, API names, worker interface,
read-only overlay, canonical primary keys). Q4 and Q5 are settled in draft 3. If a change
touches one, resolve it with a spec update or note it as deferred. **Don't silently land a
partial decision in code.**

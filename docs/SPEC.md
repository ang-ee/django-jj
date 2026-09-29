# `django-jj` — Specification

> Status: **draft 2 — pre-implementation.** Nothing described here is built yet.
> Last updated: 2026-09-29 (drafts 1 and 2 incorporate two review passes against the jj
> 0.45.1 source)
> Pinned Jujutsu: **jj-lib / jj-cli `=0.45.1`**
> Audience: contributors, integrators evaluating fit, reviewers of the design.
>
> Companion docs: [ROADMAP.md](./ROADMAP.md) (milestones M0–M7 and their gates) ·
> [proposals/](./proposals/) (numbered design changes).
>
> Open decisions carry a current default and are listed in
> [§ 22](#22-open-questions) (Q1–Q13). Everything else is the intended contract; changing
> it takes a proposal.

---

## 1. TL;DR

`django-jj` is a **Jujutsu repository store for Django**. jj's objects — files,
symlinks, trees, commits, copy histories — and its operation log — operations, views
and the set of operation heads — live as rows in PostgreSQL, served over HTTP to a thin
jj client, **`jj-django`**, built from the pinned `jj-lib` and `jj-cli` crates.

- People and agents run **real `jj`** against a shared, server-hosted repository: `jj
  new`, `describe`, `squash`, `log`, `op log`, `undo`, workspaces and conflicts behave as
  jj defines them.
- **Ids are jj's own.** The writer computes every id with jj's hashing; the server
  verifies every object before storing it and never rewrites one.
- The server can **write** too: an application edit becomes a jj commit in a named
  workspace; conflicts are stored as data, never rejected.
- A **publication** is a server-owned, versioned pointer to a commit that defines what is
  "published" — the one compare-and-swap that may refuse.
- **Derived data** (changed paths, commit graph, conflicts, and anything a host
  registers) is computed asynchronously from append-only logs and is always rebuildable.
- **Authorization is a seam.** Every request names a repository, and every object read
  names a path, so a host can grant access per repository, publication, workspace, ref
  and **path**. The library ships no permission engine, no user model and no login.

---

## 2. Goals and non-goals

### Goals

1. Stock jj semantics through stock `jj-lib`: the server stores what jj writes and
   serves what jj reads; it invents no VCS semantics jj cannot represent.
2. Concurrency without loss: any number of clients and server-side writers on one
   repository; no well-formed operation is rejected because another landed first.
3. A batch-only wire protocol with pipelined writes and a cold-start path, so a remote
   repository is usable at interactive latency.
4. Server-side writes for applications (editors, automations, agents) without running
   a jj working copy on the server.
5. Rebuildable, queryable projections of history in SQL.
6. Host-supplied identity and authorization, down to path granularity.
7. A standalone package: works in any Django 6.0 project on PostgreSQL.

### Non-goals

- A forge (issues, reviews, web UI). Hosts build those on top.
- Serving the git wire protocol. Git interop is import/export only (`[git]` extra).
- A permission engine, a user model, or an authentication system.
- Real-time collaborative editing (CRDT/OT).
- Forking jj: the client is `jj-cli` plus three store implementations and a few
  commands.
- Databases other than PostgreSQL.

---

## 3. Background: how jj stores a repository

jj splits storage into pluggable stores, each registered by name in
`jj_lib::repo::StoreFactories` and recorded in a `type` file under `.jj/repo/<store>/`:

| Store | Holds | Mutable? | `django-jj` |
| --- | --- | --- | --- |
| `Backend` | files, symlinks, trees, commits, copy histories | immutable, content-addressed | **remote** |
| `OpStore` | operations and views | immutable, content-addressed | **remote** |
| `OpHeadsStore` | the current set of operation heads | the only mutable pointer | **remote** |
| `IndexStore` | commit-graph index | derived cache | local (stock) |
| `WorkingCopy` | checked-out files, snapshot state | local | local (stock) |

Facts from jj 0.45.1 this design depends on (paths under the jj repository at tag
`v0.45.1`):

- **The backend chooses ids.** `write_file`, `write_symlink`, `write_tree`, `write_copy`
  return ids; `write_commit` returns `(CommitId, Commit)`; `OpStore::write_view` and
  `write_operation` return ids (`lib/src/backend.rs`, `lib/src/op_store.rs`). The simple
  stores hash files and symlinks as Blake2b-512 of the raw bytes, and trees, commits,
  views and operations as `ContentHash` of the value (`lib/src/simple_backend.rs`,
  `lib/src/simple_op_store.rs`).
- **Reads carry the path.** `read_file(path, id)`, `read_tree(path, id)` and
  `read_symlink(path, id)` receive the repository path of the object.
- **Trees are written one at a time.** `TreeBuilder::write_tree` awaits each
  `write_tree` in turn ("TODO: Writing trees concurrently should help on high-latency
  backends", `lib/src/tree_builder.rs`). A backend that makes each write a round trip
  pays one round trip per changed directory.
- **Operations cannot fail to commit.** A command writes its operation with the heads it
  started from as parents, then calls `update_op_heads(old_ids, new_id)`: "remove the old
  op heads and add the new one". `get_op_heads` "must not be empty"; `lock()` "is not
  needed for correctness" (`lib/src/op_heads_store.rs`,
  `docs/technical/concurrency.md`).
- **Loading merges divergence.** `load_at_head` resolves multiple heads by writing a
  "reconcile divergent operations" operation; `merge_operations` may **rebase
  descendants** and update workspace commits, so a merge can move refs to commits that
  appear in no parent view (`lib/src/repo.rs`). Any client that loads a divergent
  repository writes.
- **Roots are virtual.** The root commit, root change, root operation and root view have
  all-zero ids and are synthesized on read. "All empty trees must have the same ID
  regardless of the path." `write_commit` rejects parentless commits; `write_operation`
  rejects parentless operations.
- **Conflicts are commit-level.** A commit's `root_tree` is a `Merge<TreeId>`
  (alternating added and removed terms) with matching `conflict_labels`.
- **Copy tracking is in the trait, not in stock components.** `CopyHistory { current_path,
  parents, salt }` has no protobuf message in jj, the stock backends return `Unsupported`
  for copies, and the stock working copy writes `CopyId::placeholder()` — an **empty**
  id — into every file entry (`lib/src/backend.rs`, `lib/src/local_working_copy.rs`).
- **Git submodules cannot be encoded** in `simple_store.proto`.
- **Index building reads the operation log.** Building the index walks every ancestor
  operation, reads its view, and indexes predecessor commits too
  (`lib/src/default_index/store.rs`, `lib/src/operation.rs`).
- **Workspaces are named.** `Workspace::init_*` uses the name `default` unless told
  otherwise and writes an "add workspace" operation (`lib/src/workspace.rs`).
- **Store factories are synchronous**, and jj-lib's `Store` keeps only a small LRU
  (100 commits, 1000 trees) in front of the backend (`lib/src/store.rs`).
- **jj-lib is not a stable API.** Traits change between releases; the crates are pinned
  exactly and upgraded deliberately (§ 14.7).

---

## 4. System overview

```
 jj-django (client)                                    django-jj (server, app label `jj`)
 ┌───────────────────────────────┐                    ┌──────────────────────────────────────┐
 │ jj-cli commands (stock)       │                    │ views: protocol v1 (§ 12)            │
 │ ├─ RemoteBackend ─────────────┼── objects:put/get ─▶│ ├─ authenticate (JJ_AUTHENTICATOR)   │
 │ ├─ RemoteOpStore ─────────────┼── objects:put/get ─▶│ ├─ verify ids, validate refs         │
 │ ├─ RemoteOpHeadsStore ────────┼── op-heads ────────▶│ ├─ authorize (JJ_AUTHORIZER)         │
 │ │   (ids computed locally by  │                    │ ├─ canonical tier + write indexes    │
 │ │    jj-lib; uploads batched) │                    │ ├─ store tier: op heads, logs,       │
 │ ├─ local IndexStore (stock)   │                    │ │   publications                     │
 │ ├─ local WorkingCopy (stock)  │                    │ └─ logs ──▶ derivers ──▶ derived tier │
 │ └─ persistent object cache    │                    │ server writes (§ 10), jj worker      │
 └───────────────────────────────┘                    └──────────────────────────────────────┘
```

A `jj describe -m "fix"` against a remote repository:

1. The client loads op heads (`GET op-heads`; the server resolves divergence first when it
   can, § 8.2), fetches the head operation and view, and — on a cold cache — streams the
   operation log and commit ancestry (`operations:ancestors`, `commits:ancestors`) so the
   local index can be built.
2. It snapshots the working copy. Each `write_*` computes the id locally with jj-lib's
   own hashing, stores the object in the local cache, and returns at once; uploads queue
   in dependency order and flush in batches (`objects:put`).
3. It writes the rewritten commit, the new view and the operation the same way.
4. It flushes the upload queue, then calls `op-heads:update {old_ids: [start op], new_id}`.
   The server inserts the new head, removes the old ones and appends an `OpHeadLog` row —
   in one transaction.
5. After commit, derivers pick the log row up and update projections; readers of derived
   data see it once the watermark passes.

---

## 5. Core invariants

These are the contract. Changing one requires a proposal and a spec update.

1. **Two compare-and-swap points; only one may refuse for concurrency.**
   - *Op heads accept every well-formed update.* `op-heads:update` refuses only when the
     caller is unauthenticated or unauthorized, when the new operation or its view does not
     exist, or when it would remove a head that is not an ancestor of the new operation
     (§ 8.1). It never refuses because `old_ids` is no longer the exact head set.
   - *Publications are the policy CAS* (§ 9). A publication moves only through `publish`,
     conditioned on its version and on policy.
2. **Canonical rows are insert-only.** File, symlink, tree, commit, copy, operation and
   view rows are never updated; only garbage collection deletes them.
3. **jj-native ids, verified by the server.** Ids are computed by the writer with jj's own
   hashing (clients: jj-lib itself; server-side writes: the Python port). The server
   strictly decodes and recomputes the id of every uploaded object before storing it,
   rejects mismatches, never trusts a claimed id, and **never rewrites a value**.
4. **No grants on content-addressed objects.** Authorization is decided per repository,
   publication, workspace, ref and path — never per object id.
5. **Placement before path decisions.** In a repository with path rules, the server serves
   an object at a path only if that object is placed there (§ 13.4), and records a new
   placement — including every descendant of a moved tree — only if it passes the graft
   rules.
6. **Batch-only protocol.** Every client endpoint takes a list and returns per-item status.
7. **Derived data is rebuildable and never on the write path.** Derivation runs after the
   transaction that logged the change commits; `manage.py jj rebuild` reproduces every
   derived table; tests assert incremental projection equals rebuild. (Decoded columns and
   the write-time indexes of § 7.2 describe the object being written and are not derived
   data.)
8. **Determinism.** Encoding, decoding and hashing are deterministic and pinned by golden
   vectors per jj version (§ 6.6).
9. **Exact jj pin.** `jj-lib` / `jj-cli` are pinned with `=`; the store implementations'
   contact with jj-lib lives in one crate; an upgrade is its own release.
10. **PostgreSQL only.** The design relies on transactional DDL, `bytea`, TOAST,
    `SELECT … FOR UPDATE`, advisory locks and recursive CTEs.

## 6. Identity and hashing

### 6.1 Id kinds

| Kind | Length | Id of | Encoding on the wire and in `data` |
| --- | --- | --- | --- |
| `file` | 64 B | Blake2b-512 of the raw bytes | raw bytes |
| `symlink` | 64 B | Blake2b-512 of the UTF-8 target | raw target |
| `tree` | 64 B | `ContentHash` of `Tree` | `simple_store.Tree` |
| `commit` | 64 B | `ContentHash` of `Commit`, including `secure_sig` | `simple_store.Commit` |
| `copy` | 64 B | `ContentHash` of `CopyHistory` | `django_jj.v1.CopyHistory` (this project's message; jj has none) |
| `view` | 64 B | `ContentHash` of `View` | `simple_op_store.View` |
| `operation` | 64 B | `ContentHash` of `Operation` | `simple_op_store.Operation` |
| change id | 16 B | random, generated by whichever writer creates the change | — |

- The **empty copy id** (`CopyId::placeholder()`, zero bytes) is valid in tree entries; it
  is never resolved and needs no `Copy` row.
- Trees containing `GitSubmodule` values are rejected (`invalid_object`).
- `ContentHash` is jj's portable hashing (`core/src/content_hash.rs`): Blake2b-512 over a
  canonical walk of the value — sequences hash a 64-bit little-endian length then their
  elements; unordered containers hash in `Ord` order; enums hash a 32-bit little-endian
  variant ordinal then the variant's fields; integers are little-endian. **The golden
  vectors, not this summary, are the authority.**

### 6.2 Who computes ids

- **Clients** compute ids with jj-lib's `blake2b_hash` (and Blake2b-512 of raw bytes for
  files and symlinks) — the same functions the simple backend and simple op store use — so
  ids are exact by construction. The wire encoders mirror jj's simple stores; jj keeps
  `tree_to_proto`, `view_to_proto` and `operation_to_proto` private (only `commit_to_proto`
  is public), so the client carries copies of them, pinned with the jj version and checked
  by round-trip vectors (§ 6.6).
- **The server** decodes every upload **strictly** (below) and recomputes its id with the
  Python port for the pinned jj version. A mismatch is `ID_MISMATCH`. When the id already
  exists, the upload is still verified before it is acknowledged; a claimed id is never
  trusted (otherwise a writer could plant content under an id another writer will later
  produce).
- **Server-side writes** (§ 10) compute ids with the same Python port.
- Parity between the Python port and jj-lib is therefore load-bearing: it is the M1
  go/no-go gate and a permanent CI suite.
- Ids are never recomputed after storage. A later jj version that changes a value's shape
  changes the ids of newly written objects only — as in jj's own stores.

**Strict decoding.** Readers decode stored bytes with jj-lib, while the server hashes its
own decoding, so the two must never disagree. The server accepts only what the pinned jj
writer emits and rejects (`INVALID`):

- unknown fields;
- deprecated or legacy fields that jj-lib's decoder would interpret through a fallback —
  for example a view's legacy single working-copy field, legacy bookmark fields, or
  migration flags in their pre-migration state;
- `Merge` encodings with an even number of terms, and conflict labels whose count does not
  match the terms;
- values jj-lib cannot represent (git submodules) or would panic on.

Byte-canonical comparison is not possible (jj's protobuf maps are unordered), so these
checks are semantic; the round-trip property in § 6.6 keeps them honest.

### 6.3 Pipelined, journaled writes

Because ids are local, `write_*` calls on the client return immediately. The client:

1. stores each object in its local cache **and** appends it to a persisted upload journal
   (under `.jj/repo/store/`), so an object is never lost between "id returned" and "server
   acknowledged";
2. uploads journal entries in **dependency levels** — files, symlinks and copies; then
   trees bottom-up; then commits; then views; then operations — sending batches within a
   level in parallel (bounded by `concurrency()` and request limits) and starting a level
   only after the previous one is acknowledged. Within one request, an item may reference
   an earlier item of the same request;
3. treats an object as present on the server only once acknowledged: upload deduplication
   is against **acknowledged** objects only;
4. drains the journal before `op-heads:update`, at process exit, and at the start of the
   next command (a crashed flush resumes);
5. on an `INVALID` item caused by missing references (the error lists them), re-sends the
   referenced objects from the cache or journal and retries.

`resolve_operation_id_prefix` consults the journal and cache as well as the server, so an
operation written with `--no-integrate-operation` stays addressable until `jj op
integrate` publishes it (which drains the journal first).

The server validates references on write: a tree's children, a commit's parents and root
trees, a view's commits, and an operation's view and parents must exist, or be roots, the
empty tree, the empty file, or the empty copy id.

### 6.4 Virtual roots, the empty tree and the empty file

- Root commit id, root change id, root operation id and root view id are all-zero values
  of their kind's length, synthesized on read and never stored.
- The empty tree id is computed at repository creation and stored on `Repo`
  (`empty_tree_id`); the empty file id is Blake2b-512 of no bytes. Both are served at any
  path without a row.
- Parentless commits and parentless operations are rejected (only the roots have none).

### 6.5 Committer attestation

The server never rewrites a commit. Instead, when `JJ_VERIFY_COMMITTER = True` (default):

- `GET repos/{repo}` returns the authenticated actor's committer identity (name, email).
  `jj-django` installs it as a configuration layer that takes precedence over user
  configuration and the `JJ_USER` / `JJ_EMAIL` environment overrides, before any signing.
- The server rejects (`FORBIDDEN`) an uploaded commit whose committer name or email
  differs from the uploading actor's identity. Every jj rewrite sets the committer to the
  current user (`CommitBuilder::for_rewrite_from`), so stock workflows — including
  `duplicate`, rebases of children on `abandon`, and merge rebases — comply.
- Commits written by service actors (the jj worker, imports) carry the service identity
  as committer.
- Author fields and signatures are client-asserted and stored as sent. A signed commit
  carries its signed payload in `secure_sig` (jj signs its own encoding of the commit
  without the signature); the id's `ContentHash` covers it, and `data` is stored exactly as
  uploaded.

### 6.6 Golden vectors

`vectors/` holds fixtures generated by `vectors-gen`, a Rust program linked against the
pinned `jj-lib`: for each case, the wire encoding, a readable form of the value, and the id
jj assigns. Python tests assert byte-identical ids. Coverage at minimum:

- files (empty, binary, large), symlinks;
- trees: empty (including the empty root tree), nested, executable bits, placeholder and
  real copy ids;
- commits: resolved, conflicted with labels, conflicted **without** labels, signed,
  multi-parent, with deprecated `predecessors` populated;
- copy histories;
- views: bookmarks, tags, remote views and remote ref states, git refs, `git_heads`,
  workspace commits, conflicted ref targets;
- operations: snapshot flag, workspace name, attributes, `commit_predecessors` present and
  absent.

Two further properties:

- **Round trip:** for every value, `hash(from_proto(to_proto(v))) == hash(v)` using the
  client's encoder copies, so the client never uploads bytes that decode to a different
  value.
- **Negative vectors:** encodings the server must reject — unknown fields, legacy fields,
  pre-migration flags, even term counts, label/term mismatches, submodules.

Vectors are regenerated whenever the jj pin moves.

## 7. Data model

All models live in app label `jj`. Every table is scoped by `repo_id`; object ids are
unique per repository, not globally (no cross-repository deduplication in v1 — it would
entangle authorization).

### 7.1 Canonical tier

Surrogate `BigAutoField` primary keys; natural key `(repo_id, object_id)` unique, with
`object_id` a `BinaryField` (`bytea`). `data` is the wire encoding of § 6.1, stored
**exactly as uploaded** (a signed commit's signature covers its bytes). Decoded columns are
deterministic denormalizations filled at insert; they are never a second source of truth.

| Model | Columns (beyond `repo`, `object_id`, `data`) | Notes |
| --- | --- | --- |
| `File` | `size`, `content` (`bytea`, nullable), `storage_key` (nullable) | bytes inline up to `JJ_FILE_INLINE_MAX_BYTES`, else in `JJ_FILE_STORAGE` under a key derived from the id; `data` unused |
| `Symlink` | `target` | |
| `Tree` | — | opaque; entries are **not** normalized into rows |
| `Commit` | `change_id`, `parent_ids` (`bytea[]`), `root_tree_ids` (`bytea[]`), `is_conflicted`, `author_name`, `author_email`, `author_time`, `committer_name`, `committer_email`, `committer_time`, `description`, `is_signed` | |
| `Copy` | `current_path`, `parent_ids` (`bytea[]`) | |
| `View` | — | |
| `Operation` | `view_id`, `parent_ids` (`bytea[]`), `time_start`, `time_end`, `description`, `hostname`, `username`, `is_snapshot`, `workspace_name` | |

### 7.2 Write-time indexes

Maintained in the same transaction as the object write; insert-only.

| Model | Columns | Purpose |
| --- | --- | --- |
| `ObjectProvenance` | `repo`, `kind`, `object_id`, `actor_ref`, `first_seen_at` | unique per `(repo, kind, object_id, actor_ref)` — **every** writer of an object, including writers whose upload deduplicated; audit and the "own writes" rule of § 13.4 |
| `Placement` | `repo`, `path`, `object_id` | unique `(repo, path, object_id)`; which object has been placed at which path. Maintained only when `Repo.path_rules_enabled` (§ 13.4) |

### 7.3 Store tier

| Model | Columns | Rules |
| --- | --- | --- |
| `Repo` | `name` (unique slug), `created_at`, `empty_tree_id`, `hash_scheme` (e.g. `jj-content-hash@0.45.1`), `path_rules_enabled` | namespace and FK target only — no owner, no grants |
| `OpHead` | `repo`, `operation_id` | unique `(repo, operation_id)`; the table is a **set** |
| `OpHeadLog` | `seq`, `repo`, `added`, `inserted` (bool), `removed` (`bytea[]`), `actor_ref`, `created_at` | append-only audit trail **and** derivation outbox; `inserted` is false when `added` was already a head |
| `MergeLease` | `repo`, `holder` (actor ref), `token`, `expires_at`, `head_set` (hash of the head ids), `last_failure_at` | at most one live lease per repository (§ 8.3) |
| `Publication` | `repo`, `name`, `commit_id` (nullable), `version`, `require_fast_forward`, `forbid_conflicts`, `updated_at` | unique `(repo, name)`; moves only through `publish`; a garbage-collection root |
| `PublicationLog` | `seq`, `repo`, `name`, `old_commit_id`, `new_commit_id`, `version`, `actor_ref`, `created_at` | append-only audit **and** outbox |

### 7.4 Derived tier

Every derived row carries the deriver `version` that produced it.

| Model | Deriver | Contents |
| --- | --- | --- |
| `CommitParent` | `jj.commit_parents` | `(repo, commit_id, parent_id, position)` |
| `CommitGraph` | `jj.commit_graph` | `(repo, commit_id, generation)` — ancestry queries, fast-forward checks |
| `ChangedPath` | `jj.changed_paths` | `(repo, commit_id, term, path, kind, object_id, previous_object_id, change)` for every file and directory entry that differs from the first parent's tree (Fossil's `mlink`); conflicted commits get rows per added term |
| `CopyEdge` | `jj.copy_edges` | `(repo, copy_id, parent_copy_id)` — `get_related_copies` via recursive CTE; immutable history only |
| `ViewRef` | `jj.view_refs` | `(repo, view_id, kind, name, target)` for bookmarks, tags and workspace commits |
| `Conflict` | `jj.conflicts` | `(repo, commit_id, path)` for conflicted commits |
| `DeriverState` | framework | `(repo, deriver, version, last_op_seq, last_publication_seq)` — the watermark |

Hosts add derived models of their own through `JJ_DERIVERS` (§ 11).

### 7.5 Indexes

- Canonical: unique btree on `(repo_id, object_id)` per table; `Commit(repo_id,
  change_id)`; `Operation(repo_id, view_id)`.
- `Placement(repo_id, path, object_id)` and `(repo_id, object_id)`.
- `ChangedPath(repo_id, commit_id)` and `(repo_id, path)`.
- `OpHeadLog(repo_id, seq)`, `PublicationLog(repo_id, seq)`.
- Batch reads use `object_id = ANY(%s)` per kind, never per-object queries.

---

## 8. Operation heads

### 8.1 `update_op_heads(old_ids, new_id)`

One transaction:

1. Authenticate; `can_write_repo`.
2. `new_id` is the root operation, or an existing `Operation` whose view and parents
   exist. Otherwise `INVALID`.
3. Every `old_id` other than `new_id` must be `new_id` itself or one of its ancestors
   (walking `Operation.parent_ids`) — which is all jj ever passes. Otherwise
   `invalid_request`: without this rule a writer could delete every head and roll the
   repository back to an old operation.
4. `INSERT` `(repo, new_id)` into `OpHead` if absent (record whether it was inserted).
5. `DELETE` every `old_id` other than `new_id` (absent rows are ignored — another writer
   already removed them).
6. Append `OpHeadLog(added=new_id, inserted, removed=old_ids, actor_ref)`.
7. On commit: send `op_heads_updated(repo, seq)`.

It **never** compares `old_ids` with the current set. If two writers race, both succeed,
the set holds both new heads, and divergence is resolved as below.

### 8.2 `get_op_heads()` and divergence resolution

- Returns every `OpHead` row; if there are none, returns the root operation id. Never
  empty.
- With `?resolve=1`, when there is more than one head, a jj worker is configured (§ 10.4)
  and the caller is not a service actor, the server first asks the worker to resolve the
  divergence — jj's own `merge_operations`, under the merge lease (§ 8.3) — and waits at
  most `JJ_RESOLVE_TIMEOUT_SECONDS` before returning the heads as they are. The wait holds
  no database transaction or connection (protocol views are non-atomic). The client sends
  `resolve=1` only on the first head read of a command, so a command never waits twice.
- Resolution is deduplicated per head set, and a head set whose resolution failed is not
  retried for `JJ_MERGE_LEASE_SECONDS`.
- If heads are still divergent, the client merges them itself, as jj always does, under
  the lease; its merge operation is authorized like any other (§ 8.4).

### 8.3 `lock()` — the merge lease

jj takes `OpHeadsStore::lock()` so that concurrent processes do not resolve the same
divergence twice (duplicate merges would rebase the same descendants into divergent
changes). Across machines this is a server **lease**, not a database lock:

- `POST op-heads:lock` acquires the repository's `MergeLease` (waiting up to the timeout if
  another holder has it) and returns a token; `POST op-heads:unlock {token}` releases it;
  leases expire after `JJ_MERGE_LEASE_SECONDS`.
- The client's `lock()` acquires the lease and releases it when the guard drops; the worker
  uses the same endpoints.
- Correctness never depends on the lease; an expired lease only risks duplicate merge work,
  as jj's own lock-free design allows.

### 8.4 Authorizing what an operation changes

Checked when an **operation is written** (`objects:put`, kind `operation`), before it can
become a head:

- Parentless operations are rejected.
- For a **single-parent** operation, a ref (bookmark, tag, git ref, remote ref) or
  workspace working-copy entry is **changed** iff its value differs from the parent view's.
- For a **merge** operation (two or more parents), let the base be the parents' common
  ancestor operation (jj's merge base, found by walking `Operation.parent_ids`). A value is
  **inherited** — not a change — iff either
  1. every parent has that same value; or
  2. it equals the value of a parent that **changed** it relative to the base; or
  3. it is a conflicted value whose added terms are values of parents that changed it
     relative to the base and whose removed terms are base values.

  Everything else is a change. Rule 2's "changed relative to the base" is what stops a
  forged merge from rolling a protected ref back by pairing the current head with a fresh
  child of an old operation.
- Removal of a ref or workspace entry is a change.
- Changed workspace entries require `can_write_workspace(actor, repo, name)`; changed refs
  require `can_change_refs(actor, repo, changes)`.
- jj's merge may rebase descendants and move refs to the rebased commits; those moves are
  changes. Server-side resolution (§ 8.2) runs as a service actor, so stock clients rarely
  need such rights.
- A rejected operation leaves its already-written objects in place (unreferenced, reaped by
  garbage collection); the client command fails with the error, like a refused push.

### 8.5 Read-only actors

`GET repos/{repo}` reports whether the actor may write. For a read-only actor the client
runs with a **local overlay**: objects and operations it writes (snapshots, merges) stay in
its local store; its op heads are tracked locally on top of the server's, and every
`update_op_heads` — including jj's ancestor-pruning calls that write no operation — is
applied to the overlay; nothing is uploaded. A read-only clone is usable for reading,
diffing and local experiments. (Details settle in M3; Q13.)

### 8.6 Shared operation logs

- `jj undo` reverts the repository's latest operation, which may be another actor's; if it
  changes refs or workspaces the actor may not change, it is refused (§ 8.4).
- `jj op restore` rewrites every ref and workspace and needs rights over all of them.
- `jj op abandon` rewrites operation history so that removed heads are not ancestors of
  the new head; it is refused on a shared log (§ 8.1).
- `jj op integrate` works: the operation is in the client's journal (§ 6.3).
- All of these are covered by client e2e tests (§ 19).

## 9. Publications

A publication is a named, server-owned pointer to a commit: what an application treats as
"published" (a site, a knowledge base, an export). It exists because op heads must never
refuse, yet something must be able to say no. Publications live only in the store tier; v1
does not mirror them into jj views (a mirrored ref would be merged, and could conflict, in
every client's view).

### 9.1 Publish

`publish(repo, name, commit_id, expected_version, actor)`:

1. The commit must exist.
2. Compute the `PublishRequest`:
   - **changed paths** — a direct diff of the old and new root trees (walk both, skip equal
     subtree ids; cost proportional to the change). For conflicted commits the diff runs per
     added term.
   - **is_fast_forward** — whether the old commit is an ancestor of the new one, answered
     from `jj.commit_graph`. If that deriver has not processed the new commit yet, return
     `not_derived` (retryable) instead of walking history on the write path.
   - **is_conflicted** — from the commit row.
3. Built-in rules: `require_fast_forward`, `forbid_conflicts`.
4. `authorizer.can_publish(actor, publication, request)` returns a `Decision`
   (`allowed`, `reason`). In repositories with path rules this includes
   `can_write_paths` over the changed paths.
5. `UPDATE jj_publication SET commit_id=…, version=version+1 … WHERE id=… AND
   version=expected_version`. Zero rows → `version_conflict`.
6. Append `PublicationLog`; on commit send `publication_moved(repo, name, version)`.

### 9.2 Reading published state

Anything "published" — exports, host projections, default browsing — reads the
publication's `commit_id`, never whichever op head or bookmark is current. Clients list
publications with `GET publications` (and `jj-django publications`).

---

## 10. Server-side writes

The server writes like one more jj process: read heads, write objects, write a view and an
operation with the heads it read as parents, then `update_op_heads` — computing ids with
the Python port (§ 6.2).

### 10.1 `edit_workspace` (the common case)

`edit_workspace(repo, actor, workspace, edits, description, base=None)` applies file edits
(`put` bytes with optional `executable`, or `delete`) to a workspace's working-copy
commit:

1. `can_write_workspace(actor, repo, workspace)`; take
   `pg_advisory_xact_lock(repo, workspace)` so edits to one workspace serialize.
2. Read the heads. If they disagree on `wc_commit_ids[workspace]`, ask for resolution
   (§ 8.2); if they still disagree, return `requires_merge` (retryable). Otherwise use the
   most recent head (all agree on the workspace).
3. If the workspace exists, its working-copy commit is the target:
   - if the target is not in the view's `head_ids`, it has visible descendants → return
     `requires_rebase` (the worker's job, § 10.4);
   - if the target is conflicted, or is also the target of a ref or of another workspace
     (a jj rewrite would move those) → return `requires_worker`;
   - otherwise the new commit keeps the target's change id and replaces its tree.
   If the workspace does not exist, create a new change (random change id) on `base` (a
   commit or a publication; default: the root) and add the workspace entry.
4. Write files, then trees bottom-up by path copy (only directories on edited paths are
   rewritten), then the commit (committer = the actor, § 6.5; deprecated
   `commit.predecessors` empty), the view (`head_ids`, `wc_commit_ids[workspace]`), and the
   operation (`workspace_name`, `description`, `is_snapshot = false`,
   `commit_predecessors = {new: [old]}` or `{new: []}`).
5. `update_op_heads([chosen head], new)`.

Server edits are meant for **server-owned workspaces** (for example one per editing user
of a host application). If a client has the same workspace checked out, its working copy
becomes stale and needs `jj workspace update-stale`.

### 10.2 `operation()` (low-level, provisional)

`with django_jj.operation(repo, actor, description=…) as op:` exposes object writes and view
edits for hosts that need more than `edit_workspace`; the context writes the operation and
updates op heads on exit. Its surface settles in M4.

### 10.3 What Python never does

Python code does not merge views, merge ref targets, rebase commits, resolve conflicts, or
simplify `Merge<TreeId>` beyond "all terms equal". Those are jj algorithms and run in jj.

### 10.4 The jj worker

`jj-django worker` is the client binary running as a long-lived process on the server
side, authenticated with a host-issued credential for a **service actor** (§ 13.1), with
warm caches. It performs jj semantics on request:

- **merge** — load the repository with jj-lib, which resolves divergent heads under the
  merge lease (§ 8.3) and writes the merge operation (§ 8.2); as a service actor its own
  head reads never trigger resolution;
- **rebase** — rebase a workspace's changes onto a publication or a new base, or rebase
  descendants after an amend, recording conflicts as jj does.

The library talks to it through `django_jj.worker` (`JJ_WORKER_COMMAND` configures how it
is started); hosts may also drive it from their own task queue. The invocation interface
settles in M4 (Q12).

## 11. Derived data

### 11.1 Deriver protocol

```python
class Deriver(Protocol):
    name: ClassVar[str]                          # stable key, e.g. "jj.changed_paths"
    version: ClassVar[int]                       # bump to rederive
    depends_on: ClassVar[tuple[str, ...]]
    models: ClassVar[tuple[type[Model], ...]]    # tables this deriver owns (for rebuild)

    def derive(self, ctx: DeriveContext, batch: DeriveBatch) -> None: ...
```

`DeriveBatch` carries the repository, the `OpHeadLog` and `PublicationLog` ranges, and the
operations, views and commits newly visible in them (including predecessor commits). A
deriver writes only its own models.

### 11.2 Execution

- `django_jj.derive.run_pending(repo=None, limit=…)` processes log rows past each deriver's
  watermark, in dependency order, one transaction per (deriver, batch), and advances
  `DeriverState`. Idempotent; safe to run concurrently (per-deriver advisory lock).
- `manage.py jj derive [--once] [--repo NAME]` runs it from the command line; hosts may call
  `run_pending` from a task queue on `op_heads_updated` / `publication_moved`.
- Readers that need consistency read at the watermark: `derived_up_to(repo, deriver)`
  returns the log positions the deriver has processed (Mononoke's "warm bookmark" rule).

### 11.3 Rebuild

`manage.py jj rebuild [--repo NAME] [--deriver NAME]` truncates a deriver's models for the
repository and derives from the beginning of the logs. Tests assert `incremental ==
rebuild` after randomized histories.

---

## 12. Protocol v1

### 12.1 Conventions

- Mounted by the host: `path("jj/", include("django_jj.urls"))`; endpoints live under
  `jj/v1/`. Protocol views are non-atomic (no `ATOMIC_REQUESTS` transaction around them);
  each operation manages its own transactions.
- **Header authentication only.** Every endpoint requires credentials in the
  `Authorization` header, and authenticators never consult the session or
  `request.user` (§ 13.2), so a browser's cookies cannot authenticate a protocol request.
  The endpoints are CSRF-exempt for that reason. Browser applications call the Python APIs
  (`edit_workspace`, `publish`) from their own CSRF-protected views.
- Messages are protobuf, defined in `proto/django_jj/v1/*.proto` in this repository;
  object payloads carry the wire encodings of § 6.1. Content type
  `application/x-django-jj-v1+protobuf`; streamed responses are varint-length-delimited
  message sequences; `?format=json` returns the protobuf JSON mapping for debugging.
  (Current default; Q4.)
- Every batch response carries a **per-item status**: `OK`, `NOT_FOUND`, `FORBIDDEN`,
  `INVALID` (with the missing references, when that is the cause), `ID_MISMATCH`, or
  `RETRY` (the server is not ready to answer for this item yet). One failing item never
  fails the batch.

### 12.2 Endpoints

| Method | Path | Request → Response |
| --- | --- | --- |
| `GET` | `info` | → protocol versions, jj pin, capabilities |
| `POST` | `repos` | `{name}` → repository info (`can_create_repo`) |
| `GET` | `repos/{repo}` | → id lengths, root ids, `empty_tree_id`, `hash_scheme`, concurrency hint, `path_rules_enabled`, and for the actor: committer identity, `can_write`, denied path prefixes (§ 13.4) |
| `POST` | `repos/{repo}/objects:get` | `[{kind, id, path?}]` → stream of `{kind, id, status, data \| url}` |
| `POST` | `repos/{repo}/objects:put` | ordered stream of `{kind, id, path, data}` (`path` required for files, symlinks and trees) → `[{status}]` |
| `POST` | `repos/{repo}/operations:ancestors` | `{heads, known}` → stream of operations with their views, parents before children |
| `POST` | `repos/{repo}/commits:ancestors` | `{heads, known_heads, limit?}` → stream of commits including predecessor commits, parents before children |
| `POST` | `repos/{repo}/operations:resolve-prefix` | `{prefix}` → `{status: unique\|ambiguous\|none, id?}` |
| `GET` | `repos/{repo}/op-heads[?resolve=1]` | → `{ids}` (never empty; § 8.2) |
| `POST` | `repos/{repo}/op-heads:update` | `{old_ids, new_id}` → `{}` |
| `POST` | `repos/{repo}/op-heads:lock` | `{wait_seconds}` → `{token, expires_at}` or `held` |
| `POST` | `repos/{repo}/op-heads:unlock` | `{token}` → `{}` |
| `POST` | `repos/{repo}/copies:related` | `{copy_id}` → related copies |
| `POST` | `repos/{repo}/copies:records` | `{paths?, root, head}` → stream, reverse-topological |
| `GET` | `repos/{repo}/publications[/{name}]` | → publication(s) |
| `POST` | `repos/{repo}/publications/{name}:publish` | `{commit_id, expected_version}` → `{version}` |
| `POST` | `repos/{repo}/workspaces/{name}:edit` | `{edits, description, base?}` → `{commit_id, change_id, operation_id}` |

Later: `GET repos/{repo}/bundle?upto=<operation>` — a static cold-start bundle.

### 12.3 Mapping to jj's store traits

| Trait method | Client implementation |
| --- | --- |
| `Backend::name`, `OpStore::name`, `OpHeadsStore::name` | `"django-jj"` |
| `commit_id_length`, `change_id_length`, `root_commit_id`, `root_change_id`, `empty_tree_id`, `concurrency`, `OpStore::root_operation_id` | from repository info cached in `.jj/repo/store/django-jj.toml` at init/clone (store factories are synchronous) and refreshed on first request |
| `read_file`, `read_symlink`, `read_tree`, `read_commit`, `read_copy`, `read_view`, `read_operation` | local cache, else batched `objects:get` |
| `write_file`, `write_symlink`, `write_tree`, `write_commit`, `write_copy`, `write_view`, `write_operation` | compute id locally, cache, journal, upload by level (§ 6.3) |
| `get_related_copies`, `get_copy_records` | `copies:related`, `copies:records` |
| `resolve_operation_id_prefix` | journal and cache, then `operations:resolve-prefix` |
| `get_op_heads` | `op-heads` (`resolve=1` on the first read of a command) |
| `update_op_heads` | drain the journal, then `op-heads:update` |
| `OpHeadsStore::lock` | `op-heads:lock` / `op-heads:unlock` (§ 8.3) |
| `Backend::gc`, `OpStore::gc` | `Unsupported` in v1 (Q6) |

### 12.4 Limits and large files

- `JJ_MAX_BATCH_ITEMS` items and `JJ_MAX_REQUEST_BYTES` bytes per request; clients split.
- Files above `JJ_FILE_INLINE_MAX_BYTES` are served as a `url` when the configured storage
  can produce one; the upload handshake for large files settles in M2 (Q5).

### 12.5 Errors

`{code, message, details}` with the HTTP status:

| Code | Status | When |
| --- | --- | --- |
| `unauthenticated` | 401 | no `Authorization` header, or invalid credentials |
| `forbidden` | 403 | the authorizer denied, or a committer did not match the actor |
| `not_found` | 404 | repository or object absent (per item inside batches) |
| `invalid_object` | 422 | strict decoding failed (§ 6.2), bad path, missing referenced objects (listed), parentless commit or operation |
| `invalid_request` | 422 | a request that is well-formed but not allowed by the protocol — for example removing heads that are not ancestors of the new head (§ 8.1) |
| `id_mismatch` | 409 | the uploaded id is not the value's id |
| `version_conflict` | 409 | publication moved since `expected_version` — **publications only** |
| `requires_merge` | 409 | server edit found divergent heads for the workspace (retryable) |
| `requires_rebase` | 409 | server edit would orphan descendants |
| `requires_worker` | 409 | the edit needs jj semantics (conflicted target, or a target shared with refs or other workspaces) |
| `not_derived` | 409 | a derived answer the request needs is not ready (retryable) |
| `payload_too_large` | 413 | over the batch or request limit |
| `unsupported_protocol` | 400 | client protocol or jj version not accepted |

## 13. Identity and authorization seams

### 13.1 Actor

```python
class Actor(Protocol):
    ref: str            # stable opaque id recorded in logs ("user:42", "agent:7")
    name: str           # committer name (§ 6.5)
    email: str          # committer email (§ 6.5)
    is_service: bool    # the jj worker, imports
```

A **service actor** is a host-issued identity for the jj worker and for imports. The
library gives it exactly two differences: its head reads never trigger resolution
(§ 8.2), and commits it writes carry its own identity as committer (§ 6.5). What it may
do is still the authorizer's decision — hosts normally grant it everything.

### 13.2 Authenticator

`JJ_AUTHENTICATOR` names a class implementing:

```python
class Authenticator(Protocol):
    def authenticate(self, request: HttpRequest) -> Actor | None: ...
```

It must authenticate from the `Authorization` header alone and never from the session or
`request.user` (which Django's session middleware fills from cookies) — this is what keeps
the CSRF-exempt protocol safe (§ 12.1). There is no default: `jj.E002` fails until it is
set. Shipped building blocks:

- `django_jj.auth.BearerTokenAuthenticator` — parses `Authorization: Bearer <token>` and
  calls `actor_for_token(token)`, which hosts implement over their own token store;
- `django_jj.testing.StaticTokenAuthenticator` — maps tokens to actors from the
  `JJ_TEST_TOKENS` setting; for tests and local development only (deploy check `jj.W003`).

Issuing and storing real tokens is the host's job.

### 13.3 Authorizer

`JJ_AUTHORIZER` names a class implementing:

```python
class Authorizer(Protocol):
    def can_create_repo(self, actor: Actor, name: str) -> bool: ...
    def can_read_repo(self, actor: Actor, repo: Repo) -> bool: ...
    def can_write_repo(self, actor: Actor, repo: Repo) -> bool: ...
    def can_write_workspace(self, actor: Actor, repo: Repo, workspace: str) -> bool: ...
    def can_change_refs(self, actor: Actor, repo: Repo, changes: Sequence[RefChange]) -> bool: ...
    def denied_path_prefixes(self, actor: Actor, repo: Repo) -> Sequence[str]: ...
    def filter_readable_paths(self, actor: Actor, repo: Repo, paths: Sequence[str]) -> set[str]: ...
    def can_write_paths(self, actor: Actor, repo: Repo, paths: Sequence[str]) -> bool: ...
    def can_publish(self, actor: Actor, publication: Publication, request: PublishRequest) -> Decision: ...
```

Shipped: `django_jj.authz.DjangoModelPermissionsAuthorizer` (default; repository-level via
`jj.add_repo` / `jj.view_repo` / `jj.change_repo`, no path rules) and
`django_jj.authz.AllowAllAuthorizer` (tests only; deploy check `jj.W002`).

### 13.4 Path rules

Path rules are opt-in per repository (`Repo.path_rules_enabled`). Enabling them on an
existing repository runs `manage.py jj enable-path-rules --repo NAME`, which backfills
`Placement` by walking every stored commit's trees. Without path rules, repository read
access exposes every object and nothing below runs.

**Placement.** `Placement(path, object_id)` records that an object sits at a path:

- `write_tree(path, tree)` places each entry at `path/name`;
- `write_commit` places each root-tree term at `""`;
- placing a tree at a path where it was not placed before also places its descendants
  (eagerly; cost proportional to the subtree — this covers directory renames, which jj
  performs without rewriting the moved subtree).

**Writing (graft rules).** A placement is recorded only after it passes. Every **new**
`(P, object)` pair — a tree entry, a root-tree term, or a descendant placed eagerly — must
satisfy one of:

1. the object is already placed at `P` (an unchanged entry — the common case, including
   restricted entries the writer cannot read);
2. the object is a **leaf** (file or symlink) written by this actor (`ObjectProvenance`);
3. the object is placed at some path `Q` the actor can read, and — for a tree — no denied
   prefix lies under `Q` for this actor (the whole source subtree is readable);
4. the object is the empty tree or the empty file;
5. the object is a **tree** whose own entries all pass these rules at `P/name` (a new
   directory built from legitimate children).

Otherwise the tree or commit is `FORBIDDEN`. Without these rules a writer could place a
restricted subtree under a permitted path — directly, or wrapped in a tree of its own —
and read it there. Stock workflows pass: renames and `restore --from` move readable
content (rule 3), unchanged entries keep their placement (rule 1), new files are the
actor's leaves (rule 2), conflicted trees line up by path, and a sparse reader's snapshots
never move the subtrees it excludes.

**Reading.** For each path-bearing item `(kind, id, path)` — files, symlinks, trees, copy
records:

1. **Reachability:** `(path, id)` is in `Placement`, or the object is a leaf the actor
   wrote; otherwise `NOT_FOUND`.
2. **Readability:** `path` survives `filter_readable_paths` (evaluated once per batch);
   otherwise `FORBIDDEN`. The rest of the batch is served.

Names under a restricted prefix stay visible in their parent trees; contents do not
("level 1"). Hiding names is later work (Q7).

**Stock jj with restricted paths.** jj expects to read every tree it touches. Readers with
denied prefixes must use a **sparse working copy** that excludes them; `jj-django clone`
configures sparse patterns from the denied prefixes in `repos/{repo}`. Commands that must
read restricted content — a diff, `show -p` or rebase that touches it, merges with
conflicts inside it — fail for that reader with a clear error. This is the documented cost
of path-level read control on a VCS whose clients hash trees.

**Writes to restricted paths** are checked at publish (`can_write_paths` over the changed
paths, § 9.1) and, where a host wants it earlier, through `can_change_refs` at operation
write (§ 8.4).

### 13.5 Audit

`OpHeadLog`, `PublicationLog` and `ObjectProvenance` record who changed what. Reads of
restricted paths and every denial are emitted as structured events on the
`django_jj.audit` logger; persisting them is the host's choice.

---

## 14. The client: `jj-django`

### 14.1 Build

A Rust workspace under `client/`, modelled on jj's `cli/examples/custom-backend`:

- `jj-django-store` — the `Backend`, `OpStore` and `OpHeadsStore` implementations; the only
  crate whose store logic touches jj-lib;
- `jj-django` — the binary: `jj_cli::cli_util::CliRunner` with `StoreFactories`
  registering store type **`django-jj`**, plus the commands `clone`, `init`,
  `publications`, `worker`;
- `vectors-gen` — the golden-vector generator (§ 6.6).

All three depend on the same pinned `jj-lib` / `jj-cli`.

### 14.2 Behaviour

- Computes ids locally, journals and uploads by dependency level (§ 6.3); reports
  `concurrency()` from the repository info (default 64) and queues its own requests.
- Persists fetched and written objects in an on-disk cache under `.jj/repo/store/`, keyed
  by `(kind, id)` and marked acknowledged or not; objects are immutable, so the cache never
  invalidates.
- Installs the server-provided committer identity as the highest-precedence configuration
  layer (§ 6.5).
- Uses the stock local `IndexStore` and `WorkingCopy`.

### 14.3 Cold start

On first load, or when the local index lags, the client streams `operations:ancestors`
(operations and views) and `commits:ancestors` (including predecessor commits) into the
cache before jj-lib builds the index, so indexing never makes one round trip per object.

### 14.4 Clone and workspace naming

- `jj-django clone <url>/jj/v1/repos/<name> [dir] [--workspace NAME]` writes the store
  `type` files and `django-jj.toml` directly, then adds a workspace whose name is unique in
  the repository (default `<username>@<hostname>`, suffixed if taken) — never `default`,
  which would make every clone fight over one working-copy entry.
- `jj-django init --url …` creates a new server repository (`POST repos`) when the actor
  may (`can_create_repo`).
- Credentials come from jj config (`django-jj.token`) or `JJ_DJANGO_TOKEN`.

### 14.5 Compatibility

`GET info` and `GET repos/{repo}` report the protocol version and the server's jj pin; the
client refuses an incompatible server, and the server refuses (`unsupported_protocol`) an
incompatible client.

### 14.6 Read-only mode

See § 8.5.

### 14.7 Version policy

`jj-lib` and `jj-cli` are pinned with `=`. Moving the pin is a release that regenerates the
golden vectors, re-runs the full client e2e suite, and records any change in id computation
in `CHANGELOG.md`.

---

## 15. Git interop (`[git]` extra)

- `manage.py jj import-git PATH --repo NAME` walks a git history with dulwich into canonical
  rows as a service actor (change ids generated, or read from jj's change-id commit header
  when present) and records `GitMapping(repo, commit_id, git_sha)`.
- `manage.py jj export-git --repo NAME --publication NAME DEST` writes the publication's
  history as git objects and refs, reusing `GitMapping`.
- Export of a repository with path rules is refused unless forced: git cannot carry path
  restrictions.
- No two-way sync; no git protocol serving.

---

## 16. Settings

Flat `JJ_*` names; validated by system checks, never at import.

| Setting | Default | Meaning |
| --- | --- | --- |
| `JJ_AUTHENTICATOR` | `None` (must be set) | § 13.2 |
| `JJ_AUTHORIZER` | `"django_jj.authz.DjangoModelPermissionsAuthorizer"` | § 13.3 |
| `JJ_DERIVERS` | `django_jj.conf.DEFAULT_DERIVERS` | ordered dotted paths; hosts extend the default tuple |
| `JJ_FILE_STORAGE` | `"default"` | `STORAGES` alias for large file bytes |
| `JJ_FILE_INLINE_MAX_BYTES` | `262144` | inline threshold |
| `JJ_MAX_BATCH_ITEMS` | `1000` | per request |
| `JJ_MAX_REQUEST_BYTES` | `67108864` | per request |
| `JJ_VERIFY_COMMITTER` | `True` | § 6.5 |
| `JJ_CONCURRENCY_HINT` | `64` | reported to clients |
| `JJ_RESOLVE_TIMEOUT_SECONDS` | `5` | wait for divergence resolution on `op-heads` (§ 8.2) |
| `JJ_MERGE_LEASE_SECONDS` | `30` | merge-lease lifetime and failed-resolution back-off (§ 8.3) |
| `JJ_WORKER_COMMAND` | `None` | how the jj worker is started (§ 10.4) |
| `JJ_TEST_TOKENS` | `{}` | `StaticTokenAuthenticator` only |

## 17. AppConfig, checks, commands

```python
class JjConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_jj"
    label = "jj"
    verbose_name = "Jujutsu store"
    default = True

    def ready(self) -> None:
        from . import checks  # noqa: F401
        from . import signals  # noqa: F401  (from M2)
```

No queries, no model instantiation, no seam resolution in `ready()`.

System checks:

| Id | Condition |
| --- | --- |
| `jj.E001` | a database that may receive `jj` migrations is not PostgreSQL |
| `jj.E002` | `JJ_AUTHENTICATOR` unset, or `JJ_AUTHENTICATOR` / `JJ_AUTHORIZER` not importable or not matching its protocol |
| `jj.E003` | `JJ_DERIVERS` not importable, duplicate names, or a dependency cycle |
| `jj.E004` | `JJ_FILE_STORAGE` is not a configured `STORAGES` alias |
| `jj.W001` | `JJ_VERIFY_COMMITTER = False` (committers are client-asserted) |
| `jj.W002` (deploy) | `AllowAllAuthorizer` configured |
| `jj.W003` (deploy) | `StaticTokenAuthenticator` configured |

Management command `manage.py jj <subcommand>`: `create-repo`, `list-repos`, `derive`,
`rebuild`, `verify` (recompute ids for a sample or all objects; check `incremental ==
rebuild`), `enable-path-rules`, `import-git`, `export-git`; `gc` later.

---

## 18. Public API surface

Names settle in M2 (Q11); this is the intended shape.

```python
from django_jj import (
    Actor, Authenticator, Authorizer, Decision, PublishRequest, RefChange,
    Deriver, DeriveContext, DeriveBatch,
    edit_workspace, publish, operation,
    JjError, Forbidden, NotFound, InvalidObject, InvalidRequest, IdMismatch,
    VersionConflict, RequiresMerge, RequiresRebase, RequiresWorker, NotDerived,
    app_settings,
)
from django_jj.auth import BearerTokenAuthenticator
from django_jj.authz import DjangoModelPermissionsAuthorizer
from django_jj.models import Repo, Publication, OpHead, OpHeadLog, PublicationLog  # and the rest
from django_jj.signals import op_heads_updated, publication_moved
from django_jj.derive import run_pending, derived_up_to
from django_jj import worker
```

Python exceptions mirror the error codes of § 12.5 that Python callers can meet;
`unauthenticated`, `payload_too_large` and `unsupported_protocol` are protocol-only.
Anything under `django_jj._internal` is private; `django_jj.testing` is for tests only.

## 19. Testing

- **Python:** `pytest` + `pytest-django` against PostgreSQL (required; no SQLite path).
- **Golden vectors:** byte-identical ids, round trips and negative vectors (§ 6.6).
- **Concurrency:** two clients and one server writer on one repository; divergent heads
  appear, get resolved once (lease), and no operation is rejected for staleness.
- **Durability:** a client killed mid-flush resumes on its next command without the server
  ever seeing a dangling reference; `--no-integrate-operation` followed by `jj op integrate`
  from another process works.
- **Head safety:** `op-heads:update` removing a non-ancestor head is refused; a forged merge
  (current head + fresh child of an old operation) cannot roll a protected ref back.
- **Oracle:** incremental derivation equals rebuild after randomized histories.
- **Client e2e:** stock workflows (`new`, `describe`, `squash`, `rebase`, `duplicate`,
  `abandon`, `undo`, `op restore`, `op abandon` (refused), `op log`, `evolog`, conflicts,
  multiple workspaces, two clones with distinct workspace names, `JJ_USER` set in the
  environment) against a live test server; placeholder copy ids accepted.
- **Path rules / leak tests:** a restricted object is unreachable through a permitted path,
  a guessed id, a copy record, a **grafted tree**, or a **wrapped graft** (an actor-written
  tree containing a restricted subtree, placed under a permitted path); a restricted reader
  with sparse patterns can run log, describe, new and publish.
- **Rust:** `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test`.

## 20. Security considerations

- Every uploaded object is strictly decoded and verified against its id (§ 6.2), so
  content cannot be planted under another object's id, and the server never authorizes a
  value that jj-lib would read differently.
- Protocol endpoints authenticate from the `Authorization` header only; sessions and
  `request.user` are never consulted (§ 12.1, § 13.2).
- Op-head removals are limited to ancestors of the new head (§ 8.1); merges cannot smuggle
  ref changes past authorization (§ 8.4).
- Repository paths are validated as jj `RepoPath`s (UTF-8, `/`-separated, no empty, `.` or
  `..` components, no NUL).
- References to missing objects are rejected on write (§ 6.3).
- Batch and request limits bound memory; streamed responses bound latency.
- Committers are server-verified (§ 6.5); authors and signatures are client-asserted.
- Without path rules, repository read access exposes every object in the repository.
- Git export discards path restrictions (§ 15).

## 21. Performance notes

- Writes are pipelined: ids are local, uploads batched (§ 6.3). The server's cost is decode
  plus hash per object.
- Small objects stay in `bytea` (TOAST compresses values over ~2 KB); large file bytes go to
  storage. There is no cross-version delta compression in v1 — history-heavy large files
  cost roughly full size per version.
- Trees are opaque rows; row volume grows with changes, not with tree size × versions.
  `Placement` (only with path rules) grows with distinct (path, object) pairs.
- Cold start streams the operation log and commit ancestry (§ 14.3); per-object fetch is
  the documented failure mode of database-backed VCS stores.

---

## 22. Open questions

| Id | Question | Current default |
| --- | --- | --- |
| Q1 | Where jj semantics run server-side | Python for encoding, hashing and simple writes; the jj worker for merges and rebases (§ 10.4) |
| Q2 | Who computes ids | the writer, with jj's hashing; the server verifies (§ 6.2). Alternative: server-minted ids (no parity needed, but one round trip per written tree and commit) |
| Q3 | Who resolves divergent heads | the server, on `op-heads` reads, via the worker; clients as a fallback (§ 8.2) |
| Q4 | Wire format | protobuf envelopes over HTTP (§ 12.1); alternatives: CBOR, gRPC |
| Q5 | Large-file upload handshake | settled in M2 |
| Q6 | Garbage collection and operation retention | not in v1; one operation per jj command makes it necessary before heavy use |
| Q7 | Hiding names under restricted paths (filtered trees) | later |
| Q8 | Retroactive redaction of content by id | later |
| Q9 | jj's `jj-core` crate split and trait changes after 0.45.1 | tracked per pin move |
| Q10 | Converging with an upstream jj-native remote protocol, if one appears | watch |
| Q11 | Final public API names | settled in M2 |
| Q12 | Worker invocation interface | settled in M4 |
| Q13 | Read-only overlay details | settled in M3 |

---

## 23. Design lineage

- **Jujutsu** — store traits, the lock-free operation log and its concurrency model
  (`docs/technical/concurrency.md`), the custom-backend example.
- **Fossil** — a few canonical tables, everything else rebuildable (`fossil rebuild`);
  per-check-in file changes (`mlink`) rather than exploded trees.
- **Mononoke / Sapling** — minimal canonical objects plus asynchronously derived data;
  bookmark moves as compare-and-swap with an update log; readers gated on derived data
  ("warm bookmarks"); redaction.
- **Software Heritage** — object metadata in PostgreSQL, bytes elsewhere; surrogate keys
  behind intrinsic hashes; the cost of normalizing trees.
- **gitgres** — git objects in PostgreSQL; the measured cost of storing every version whole.
- **lakeFS, Dolt** — ref compare-and-swap over a database; batch transfer with presigned
  URLs.
- **Perforce, Subversion, Piper** — path-level read permissions are possible only when a
  server mediates every file read.

---

## 24. Glossary

| Term | Meaning |
| --- | --- |
| change | jj's stable identity for a line of work; survives rewrites (change id) |
| commit | an immutable snapshot with parents, a (possibly conflicted) tree and metadata |
| operation | one jj command's effect on the repository: a view plus parent operations |
| view | refs, heads and workspace working-copy commits at one operation |
| op heads | the set of operations with no descendants; more than one means divergence |
| divergence | concurrent operations from the same parent; resolved by a merge operation |
| publication | a server-owned, versioned pointer to a commit; the policy CAS |
| workspace | a named working copy recorded in the view (`wc_commit_ids`) |
| placement | the record that an object sits at a path (path rules only) |
| deriver | code that projects canonical data into derived tables |
| watermark | the log position a deriver has processed |
| actor | the authenticated party behind a request; service actors run jj semantics and imports |
| host | the Django project that installs `django-jj` and supplies its seams |

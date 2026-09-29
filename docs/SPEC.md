# `django-jj` — Specification

> Status: **draft 3 — pre-implementation.** Nothing described here is built yet.
> Last updated: 2026-09-29. Drafts 1 and 2 incorporated two review passes against the jj
> 0.45.1 source. Draft 3 incorporates a prior-art and source review of every draft-2
> mechanism; [§ 25](#25-changes-in-draft-3) maps each change to the sections it touches.
> Pinned Jujutsu: **jj-lib / jj-cli `=0.45.1`**
> Audience: contributors, integrators evaluating fit, reviewers of the design.
>
> Companion docs: [ROADMAP.md](./ROADMAP.md) (milestones M0–M7 and their gates) ·
> [proposals/](./proposals/) (numbered design changes).
>
> Open decisions carry a current default and are listed in
> [§ 22](#22-open-questions) (Q1–Q14). Everything else is the intended contract; changing
> it takes a proposal.

---

## 1. TL;DR

`django-jj` is a **Jujutsu repository store for Django**. jj's objects — files,
symlinks, trees, commits, copy histories — and its operation log — operations, views
and the set of operation heads — live as rows in PostgreSQL, served over HTTP to a thin
jj client, **`jj-django`**, built from the pinned `jj-lib` and `jj-cli` crates.

- People and agents run **real `jj`** against a shared, server-hosted repository: `jj
  new`, `describe`, `squash`, `log`, `op log`, workspaces and conflicts behave as jj
  defines them.
- **Ids are jj's own.** The writer computes every id with jj's hashing; the server
  strictly decodes and verifies every object before storing it and never rewrites one.
- **Operations never fail for concurrency.** Op-head updates are never refused because
  another writer landed first; divergence is merged by a server-side jj worker.
- The server can **write** too: an application edit becomes a jj commit in a named
  workspace; conflicts are stored as data, never rejected.
- A **publication** is a server-owned, versioned pointer to a commit that defines what is
  "published" — the one compare-and-swap that may refuse.
- **Derived data** (changed paths, commit graph, conflicts, and anything a host
  registers) is computed asynchronously from append-only logs and is always rebuildable.
- **Authorization is a seam.** Every request names a repository, and every object read
  names a path, so a host can grant access per repository, publication, workspace, ref
  and **path**. The library ships no permission engine, no user model and no login.

Other remote jj stores exist (§ 23); what this one adds is SQL rows with queryable derived
data, path-level authorization, server-side writes from Python, and packaging as an
embeddable Django app.

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

After 0.45.1, jj adds another store, `WorkspaceStore` (the workspace registry, with its
own `.jj/repo/workspace_store/type` file; a missing file falls back to `simple` until jj
0.51). `django-jj` keeps it local and stock; `jj-django clone` writes its `type` file from
the pin that introduces it (§ 14.4).

Facts from jj 0.45.1 this design depends on (paths under the jj repository at tag
`v0.45.1`):

- **The backend chooses ids.** `write_file`, `write_symlink`, `write_tree`, `write_copy`
  return ids; `write_commit` returns `(CommitId, Commit)`; `OpStore::write_view` and
  `write_operation` return ids (`lib/src/backend.rs`, `lib/src/op_store.rs`). The simple
  stores hash files and symlinks as Blake2b-512 of the raw bytes, and trees, commits,
  views and operations as `ContentHash` of the value (`lib/src/simple_backend.rs`,
  `lib/src/simple_op_store.rs`).
- **The backend may set the committer.** `write_commit` "may change the committer name to
  an authenticated user's name"; the caller uses the returned commit (`lib/src/backend.rs`).
  The CLI offers no higher-precedence configuration hook: `CliRunner::add_extra_config`
  accepts only default-precedence layers (`cli/src/cli_util.rs`).
- **Reads carry the path** — except copies. `read_file(path, id)`, `read_tree(path, id)`
  and `read_symlink(path, id)` receive the repository path of the object; `read_copy(id)`
  does not.
- **Trees are written one at a time.** `TreeBuilder::write_tree` awaits each
  `write_tree` in turn ("TODO: Writing trees concurrently should help on high-latency
  backends", `lib/src/tree_builder.rs`). A backend that makes each write a round trip
  pays one round trip per changed directory.
- **Operations cannot fail to commit.** A command writes its operation with the heads it
  started from as parents, then calls `update_op_heads(old_ids, new_id)`: "remove the old
  op heads and add the new one". `get_op_heads` "must not be empty"; `lock()` "is not
  needed for correctness" (`lib/src/op_heads_store.rs`, `docs/technical/concurrency.md`).
- **`lock()` is taken on every commit.** `Transaction::publish` takes
  `OpHeadsStore::lock()` before every `update_op_heads`, not only when resolving
  divergence (`lib/src/transaction.rs`).
- **Loading merges divergence, as a fold.** `load_at_head` resolves multiple heads by
  writing a "reconcile divergent operations" operation. `resolve_op_heads` drops heads that
  are ancestors of others, sorts the rest by operation end time (a client-asserted value)
  and folds them pairwise: each next head is merged against the closest common ancestors
  of the heads merged so far, and descendants are rebased after every step. With several
  closest common ancestors, jj first merges them into a virtual base operation that is
  written to the op store but is never an ancestor of the result (`lib/src/op_heads_store.rs`,
  `lib/src/repo.rs`). A merge can therefore move refs and workspace entries to commits that
  appear in no parent view. Any client that loads a divergent repository writes.
- **Roots are virtual.** The root commit, root change, root operation and root view have
  all-zero ids and are synthesized on read. "All empty trees must have the same ID
  regardless of the path." `write_commit` rejects parentless commits; `write_operation`
  rejects parentless operations.
- **Conflicts are commit-level.** A commit's `root_tree` is a `Merge<TreeId>`
  (alternating added and removed terms) with matching `conflict_labels`.
- **Decoders are lenient; the writer still emits deprecated fields.** The `*_from_proto`
  functions default missing messages, apply legacy fallbacks, ignore some fields, and
  panic on at least ten malformed inputs. The 0.45.1 writer still emits three deprecated
  fields: `Commit.predecessors` (by default), `Bookmark.remote_bookmarks` and `View.git_head`
  (`lib/src/simple_backend.rs`, `lib/src/simple_op_store.rs`; § 6.7).
- **Signatures cover a re-encoding.** jj signs `commit_to_proto(commit).encode_to_vec()`
  and, on read, re-derives the signed payload by prost-re-encoding the decoded commit
  without its signature (`lib/src/simple_backend.rs`).
- **Copy tracking is in the trait, not in stock components.** `CopyHistory { current_path,
  parents, salt }` has no protobuf message in jj, the stock backends return `Unsupported`
  for copies, and the stock working copy writes `CopyId::placeholder()` — an **empty**
  id — into every file entry (`lib/src/backend.rs`, `lib/src/local_working_copy.rs`).
- **Git submodules cannot be encoded** in `simple_store.proto`.
- **Index building reads the operation log.** Building the index walks every ancestor
  operation one node at a time, reads its view, and indexes predecessor commits too
  (`lib/src/default_index/store.rs`, `lib/src/operation.rs`).
- **Operations record the command line.** Every transaction stores the full command line
  as an operation attribute, alongside hostname and username (`cli/src/cli_util.rs`).
- **Workspaces are named.** `Workspace::init_*` uses the name `default` unless told
  otherwise and writes an "add workspace" operation (`lib/src/workspace.rs`).
- **Sparse patterns are include-only prefixes** (`lib/src/local_working_copy.rs`);
  exclusions are an open upstream request.
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
 │ ├─ RemoteOpStore ─────────────┼── objects:put/get ─▶│ ├─ strict decode, verify ids, refs   │
 │ ├─ RemoteOpHeadsStore ────────┼── op-heads ────────▶│ ├─ authorize (JJ_AUTHORIZER)         │
 │ │   (ids computed locally by  │                    │ ├─ canonical tier + write indexes    │
 │ │    jj-lib; journaled;       │                    │ ├─ store tier: op heads, logs,       │
 │ │    closure uploads)         │                    │ │   publications, lease              │
 │ ├─ local IndexStore (stock)   │                    │ └─ logs ──▶ derivers ──▶ derived tier │
 │ ├─ local WorkingCopy (stock)  │                    │ server writes (§ 10), jj worker      │
 │ └─ persistent object cache    │                    │                                      │
 └───────────────────────────────┘                    └──────────────────────────────────────┘
```

A `jj describe -m "fix"` against a remote repository:

1. The client loads op heads (`GET op-heads`), fetches the head operation and view, and —
   on a cold cache — streams the operation log and commit ancestry (`operations:ancestors`,
   `commits:ancestors`) so the local index can be built. If the heads are divergent, a
   server-side merge is usually already running (§ 8.2); the client waits for it briefly
   in `lock()` and re-reads.
2. It snapshots the working copy. Each `write_*` computes the id locally with jj-lib's
   own hashing, stores the object in the local cache and journal, and returns at once;
   new file contents start uploading in the background.
3. It writes the rewritten commit, the new view and the operation the same way.
4. At `update_op_heads` it uploads the operation's closure — every journaled object the
   new operation reaches that the server has not acknowledged — in one topologically
   ordered request, and asks the server to update the heads. The server authorizes what
   the new operations change, inserts the new head, removes the old ones and appends an
   `OpHeadLog` row — in one transaction.
5. After commit, derivers pick the log row up and update projections; readers of derived
   data see it once the watermark passes.

---

## 5. Core invariants

These are the contract. Changing one requires a proposal and a spec update.

1. **Two compare-and-swap points; only one may refuse for concurrency.**
   - *Op heads accept every well-formed update.* `op-heads:update` refuses only when the
     caller is unauthenticated or unauthorized (including § 8.4's authorization of what
     the new operations change), when the new operation or its view does not exist, or
     when it would remove a head that is not an ancestor of the new operation (§ 8.1). It
     never refuses because `old_ids` is no longer the exact head set, and it retries lock
     waits and deadlocks internally rather than failing.
   - *Publications are the policy CAS* (§ 9). A publication moves only through `publish`,
     conditioned on its version and on policy.
2. **Canonical rows are insert-only.** File, symlink, tree, commit, copy, operation and
   view rows are never updated; only garbage collection deletes them.
3. **jj-native ids, verified by the server.** Ids are computed by the writer with jj's own
   hashing (clients: jj-lib itself; server-side writes: the Python port). The server
   strictly decodes every upload (§ 6.7), recomputes the id of every new object, requires
   a re-upload of an existing id to equal the stored value, rejects mismatches, never
   trusts a claimed id, and **never rewrites a value**.
4. **No grants on content-addressed objects.** Authorization is decided per repository,
   publication, workspace, ref and path — never per object id. Ref, workspace and
   publication rules govern writes only; read isolation is per repository and per path.
5. **Placement before path decisions.** In a repository with path rules, the server serves
   an object at a path only if that object is placed there (§ 13.4), and records a new
   placement — including every descendant of a moved tree — only if it passes the graft
   rules. Responses about paths or objects the actor cannot see never depend on hidden
   state.
6. **Batch-only protocol.** Every client endpoint takes a list and returns per-item status.
7. **Derived data is rebuildable and never on the write path.** Derivation never runs in
   the transaction that logs the change and can never fail or roll back a write;
   `manage.py jj rebuild` reproduces every derived table; tests assert incremental
   projection equals rebuild. (Decoded columns and the write-time indexes of § 7.2
   describe the object being written and are not derived data.)
8. **Determinism.** Encoding, decoding and hashing are deterministic and pinned by golden
   vectors per jj version (§ 6.6).
9. **Exact jj pin.** `jj-lib` / `jj-cli` are pinned with `=`; the store implementations'
   contact with jj-lib lives in one crate; an upgrade is its own release.
10. **PostgreSQL only.** The design relies on transactional DDL, `bytea`, TOAST,
    `SELECT … FOR UPDATE`, advisory locks and recursive CTEs.
11. **No wedges.** jj's clients fix ids locally and assume writes succeed, so any
    deterministic refusal of a journaled object is permanent for that client. The server
    refuses only input the pinned jj writer never emits, unauthorized changes, and policy;
    deprecated fields the writer still emits are constrained, not rejected; and a client
    has a defined recovery path for items the server refuses permanently (§ 6.3).

## 6. Identity and hashing

### 6.1 Id kinds

| Kind | Length | Id of | Encoding on the wire and in `data` |
| --- | --- | --- | --- |
| `file` | 64 B | Blake2b-512 of the raw bytes | raw bytes |
| `symlink` | 64 B | Blake2b-512 of the UTF-8 target | raw target |
| `tree` | 64 B | `ContentHash` of `Tree` | `simple_store.Tree`, canonical bytes |
| `commit` | 64 B | `ContentHash` of `Commit`, including `secure_sig`, whose `data` is jj's prost encoding of the unsigned commit | `simple_store.Commit`, canonical bytes |
| `copy` | 64 B | `ContentHash` of `CopyHistory` | `django_jj.v1.CopyHistory` (this project's message; jj has none), canonical bytes |
| `view` | 64 B | `ContentHash` of `View` | `simple_op_store.View`, canonical up to map and set order |
| `operation` | 64 B | `ContentHash` of `Operation` | `simple_op_store.Operation`, canonical up to map order |
| change id | 16 B | random, generated by whichever writer creates the change | — |

- The **empty copy id** (`CopyId::placeholder()`, zero bytes) is valid in tree entries; it
  is never resolved and needs no `Copy` row.
- Trees containing `GitSubmodule` values are rejected (`invalid_object`).
- `ContentHash` is jj's portable hashing (`core/src/content_hash.rs`): Blake2b-512 over a
  canonical walk of the value — fields in Rust declaration order; sequences hash a 64-bit
  little-endian length (a UTF-8 byte count for strings) then their elements; unordered
  containers hash in `Ord` order (lexicographic bytes); enums hash a 32-bit little-endian
  variant ordinal (declaration index) then the variant's fields; `Option` hashes a 0/1
  tag; integers are little-endian at their declared width. `Merge<T>` has a hand-written
  impl. **The golden vectors, not this summary, are the authority.**
- Every upload item and every `objects:get` response carries the object's `size` in bytes.
  It is advisory — the id is the key — and lets the server reject oversized or truncated
  items before hashing; a mismatch is `INVALID`.

### 6.2 Who computes ids, and how the server verifies them

- **Clients** compute ids with jj-lib's `blake2b_hash` (and Blake2b-512 of raw bytes for
  files and symlinks) — the same functions the simple backend and simple op store use — so
  ids are exact by construction. The wire encoders mirror jj's simple stores; jj keeps
  `tree_to_proto`, `view_to_proto` and `operation_to_proto` private (only `commit_to_proto`
  is public), so the client carries copies of them, pinned with the jj version and checked
  by round-trip vectors (§ 6.6).
- **Server-side writes** (§ 10) compute ids with the Python port. For signed commits the
  port includes a prost-exact `Commit` encoder: the signed payload is the canonical
  encoding of the unsigned commit, in which field 9 (`secure_sig`) precedes field 10
  (`conflict_labels`), so it is **not** "the uploaded bytes minus a trailing signature".
- Parity between the Python port and jj-lib is load-bearing: it is the M1 go/no-go gate
  and a permanent CI suite.
- Ids are never recomputed after storage under a different scheme. Each canonical row
  records the `hash_epoch` it was verified under (§ 7.1); a later jj version that changes
  a value's shape changes the ids of newly written objects only — as in jj's own stores.

**Verifying an upload.** For every item of `objects:put` (in the order of § 13.4 when path
rules are enabled):

1. **Strict decoding** (§ 6.7) with the project's own wire parser, not a general protobuf
   runtime: Python's runtime accepts inputs prost rejects (a known field with the wrong
   wire type; a ten-byte varint overflow), so a server built on it could store objects no
   jj client can read. The parser bounds recursion and allocation.
2. **Canonical bytes.** For trees, commits and copies, the upload must equal the canonical
   re-encoding of its decoded value, byte for byte; for views and operations it must equal
   it up to the element order of `head_ids`, `wc_commit_ids` and `attributes` (the only
   fields jj encodes from hash maps and sets), and every other repeated field keyed by a
   `BTreeMap` must be strictly increasing by key. This rejects unknown fields, wrong wire
   types, non-minimal and overflowing varints, duplicated or out-of-order fields and
   explicit defaults in one check. Byte-canonical storage is also what keeps signed
   commits honest: jj verifies against its re-encoding, so non-canonical bytes would let
   the server record a signature jj readers reject.
3. **New id:** recompute the id with the port for the repository's current hash scheme;
   a mismatch is `ID_MISMATCH`. This prevents planting content under another object's id.
4. **Existing id:** the upload must equal the stored value — the same bytes for files,
   symlinks, trees, commits and copies; the same decoded value for views and operations —
   or it is `ID_MISMATCH`, logged as a parity alarm. This is **proof of possession**:
   without it, an actor under path rules could claim a restricted file's id with junk
   bytes, be recorded as its writer (§ 7.2) and read it back (the "Dropship" attack on
   deduplicating stores). It also works across hash epochs.
5. **References** must exist, or be roots, the empty tree, the empty file or the empty copy
   id, or be earlier items of the same request: a tree's children, a commit's parents,
   predecessors and root trees, a copy's parents, a view's commits, and an operation's view,
   parents and `commit_predecessors` commits (keys and values: jj's index build reads every
   one). With path rules, "exist" means "visible to the actor" (§ 13.4). Missing
   references are listed in the item's `INVALID` status.
6. **Committer** (§ 6.5), for new commit rows only.
7. **Placement and graft rules** (§ 13.4), with path rules only.

Nothing is inserted for an item that fails any step (quarantine): a canonical row exists
only for an object that passed every check.

### 6.3 Journaled uploads by closure

Because ids are local, `write_*` calls on the client return immediately. The client:

1. stores each object in its local cache **and** appends it to a persisted upload journal
   (under `.jj/repo/store/`), so an object is never lost between "id returned" and "server
   acknowledged";
2. uploads **leaves** — files and symlinks — eagerly, in parallel batches bounded by
   `concurrency()` and the server's advertised limits; large files use an upload session
   (§ 12.4);
3. uploads everything else only as the **closure of an op-heads update**: at
   `update_op_heads(old_ids, new_id)` it collects every journaled, unacknowledged object
   reachable from `new_id` — through parent operations, views, `commit_predecessors`,
   commits, predecessors, trees and copies — orders them topologically (parents before
   children, including commits before child commits, operations before child operations
   and copies before child copies), and sends them in one `objects:put` request. The
   server validates the request as a closed set: each reference must be stored, virtual or
   empty, or an earlier item of the same request. Only when the advertised limits force a
   split is the closure sent as several requests, strictly **sequentially** in topological
   order. Parallel requests never reference each other;
4. may attach the `op-heads:update` itself as the request's trailer (§ 12.2); the server
   applies it, in its own transaction, only if every item is `OK`. Without a trailer it
   calls `op-heads:update` after the upload is acknowledged;
5. treats an object as present on the server only once acknowledged: deduplication is
   against **acknowledged** objects only. Clients never upload the empty file or the empty
   tree;
6. records each pending head update in the journal before uploading, so a crashed flush
   resumes at the start of the next command;
7. on an `INVALID` item caused by missing references (listed in the status), re-sends the
   referenced objects from the cache or journal and retries.

Objects that no head update reaches — for example the views and rebased commits that
`jj op log -p`, `op show` and `op diff` compute for display — stay local and are never
uploaded; the journal prunes them after a client-configured age.

**Permanently refused items.** If the server refuses an item deterministically
(`FORBIDDEN`, `ID_MISMATCH`, or `INVALID` for anything but missing references), the client
marks the pending head update as refused, moves the unacknowledged items of that closure
to a `rejected/` area of the journal (kept for inspection, never retried), reports the
error once, and fails the command. Later commands start from the server's heads; the
refused operation stays addressable locally, like one written with
`--no-integrate-operation`. `jj-django journal` lists and discards rejected items (§ 14.1).

A deterministic refusal of the **head update itself** — `forbidden` from § 8.4,
`invalid_request` from § 8.1, or a refused trailer — is permanent too: the client marks the
pending head update refused and never resumes it. Its items were acknowledged and stay on
the server as unreferenced objects (garbage for Q6).

`resolve_operation_id_prefix` consults the journal and cache as well as the server, so an
operation written with `--no-integrate-operation` stays addressable until `jj op
integrate` publishes it (which uploads its closure first).

### 6.4 Virtual roots, the empty tree and the empty file

- Root commit id, root change id, root operation id and root view id are all-zero values
  of their kind's length, synthesized on read and never stored.
- The empty tree id is computed at repository creation and stored on `Repo`
  (`empty_tree_id`); the empty file id is Blake2b-512 of no bytes. Both are served at any
  path without a row, and clients never upload them.
- Parentless commits and parentless operations are rejected (only the roots have none).
  The port must not reproduce jj's read-path fix-up that appends the root operation to a
  legacy parentless operation.

### 6.5 Committer attestation

The server never rewrites a commit. When `JJ_VERIFY_COMMITTER = True` (default):

- `GET repos/{repo}` returns the authenticated actor's committer identity (name and
  preferred email) with a digest; `GET op-heads` repeats the digest, so the client can
  refresh a stale identity before a command writes its first commit.
- **The client stamps the identity.** `jj-django-store`'s `write_commit` — the extension
  point jj documents for exactly this — replaces the committer name and email with the
  server-provided identity before encoding, signing and hashing, and replaces the author
  name and email too when they equal the local settings identity (so new changes carry
  the server identity as author). This happens before the id exists, so it is not a
  server rewrite, and it covers every path, including `--config user.email=…`,
  `JJ_USER`/`JJ_EMAIL` and code that calls `set_committer`. Users who sign with GPG
  should set `signing.key` explicitly.
- **The server verifies on insert.** When a commit row is first inserted, its committer
  email must match, case-insensitively, one of the uploading actor's `committer_emails`
  (§ 13.1), unless `can_forge_committer(actor, repo)` allows otherwise (imports, § 15).
  The name is not compared. Re-uploads of an existing commit are verified by equality
  (§ 6.2 step 4), not by committer, so a changed identity never wedges a journal of
  commits whose rows already exist. A non-service actor whose email equals the service
  identity's is refused.
- Commits written by service actors (the jj worker) carry the service identity as
  committer; imports carry the original committers (§ 15).
- Author fields, the committer timestamp and signatures are client-asserted and stored as
  sent. A signed commit carries its signed payload in `secure_sig`; the id's `ContentHash`
  covers it, and `data` is stored as uploaded, which § 6.2 step 2 guarantees is canonical.
- Hosts should issue stable, id-based committer emails (for example
  `<id>+<username>@users.example`): verification then survives renames, and insert-only
  rows never hold personal addresses that cannot be scrubbed.

### 6.6 Golden vectors

`vectors/` holds fixtures generated by `vectors-gen`, a Rust program linked against the
pinned `jj-lib`. Python tests assert byte-identical ids and encodings.

**Format.** One directory per hash-scheme major (`vectors/v1/`), kept after the pin moves
so stored ids stay checkable:

- `manifest.json`: jj version and commit, prost version, hash scheme
  (`jj-contenthash-blake2b512/1`), generator commit, file digests;
- `<kind>.valid.jsonl`: `{id, flags, value, wire_hex, object_id}` per case, where `value`
  is a readable form using Rust field names and declaration order; signed commits add
  `signing_payload_hex`; views and operations add `order_free_fields`;
- `<kind>.invalid.jsonl`: `{id, flags, wire_hex, error, jj_behaviour, jj_ref}`, where
  `jj_behaviour` is `panic`, `error`, `alias`, `ignored` or `accepted` and `jj_ref` is the
  jj source line that justifies the rejection;
- `<kind>.noncanonical.jsonl`: `{wire_hex, canonical_wire_hex, object_id}` for inputs jj
  accepts that alias a canonical value; the server rejects them.

CI fails if regenerated vectors differ from the committed ones without a `CHANGELOG.md`
entry.

**Coverage**, at minimum:

- jj's own pinned hashes (a view, an operation, the empty tree) as upstream seeds;
- files (empty, binary, large), symlinks;
- trees: empty (including the empty root tree), nested, executable bits, placeholder and
  real copy ids, symlink and subtree entries, names that sort differently by UTF-8 bytes
  and by UTF-16 code units;
- commits: resolved; conflicted (three and five terms) with and without labels; signed,
  including signed + conflicted + labelled and signed with a non-ASCII description;
  multi-parent including the root commit; negative times and time-zone offsets; integer
  extremes; deprecated `predecessors` populated and empty;
- copy histories: nested paths, zero to two parents, empty and random salt;
- views: bookmarks, tags, remote views and remote ref states (tracked, new, remote tags),
  git refs, `git_heads` for `default` and another workspace, multiple workspace commits,
  conflicted ref targets with absent terms, the deprecated projections (`remote_bookmarks`,
  `git_head`) as **valid** cases, and `head_ids` in two encodings with one id;
- operations: snapshot flag, workspace name absent and empty, attributes in two orders
  with one id, `commit_predecessors` absent, empty and populated, multiple parents;
- negatives: every row of § 6.7, plus the wire-level list: wrong wire type on a known
  field, ten-byte overflow varint, non-minimal varints (tag, length, value), unknown
  fields of every wire type at top level and nested, reserved view fields 4 and 10,
  duplicate singular fields and sub-messages, a oneof set twice, duplicate map keys,
  out-of-order fields, explicit defaults, invalid UTF-8, tag 0, truncated length,
  concatenated messages, out-of-range enum values, a non-sign-extended `int32`.

Three further properties:

- **Round trip:** for every value, `hash(from_proto(to_proto(v))) == hash(v)` using the
  client's encoder copies, and for every valid vector `to_proto(from_proto(b)) == b` (up to
  map order for views and operations), so the client never uploads bytes that decode to a
  different value.
- **Differential fuzzing:** `vectors-gen fuzz --seed N` emits random well-formed values
  with their jj-lib id and encoding for the Python suite; a mutation mode flips wire bytes
  and checks "Python strict-accepts ⇒ jj-lib decodes without panic to a value with the same
  hash". CI runs a fixed seed range; a nightly job runs more.
- **Hash-shape canary:** a CI job extracts the `ContentHash`-derived struct shapes from the
  jj source at the pin and at jj `main`, so a new hashed field fails CI before any vector
  is regenerated (§ 14.7).

### 6.7 Strict decoding rules (jj 0.45.1)

The server accepts only what the pinned writer emits. "SW" says whether the stock 0.45.1
writer emits the input; every "reject" row is a negative vector.

| # | Object / field | jj 0.45.1 behaviour | Server rule | SW |
| --- | --- | --- | --- | --- |
| T1 | tree entry without `value` | panic | reject | no |
| T2 | `TreeValue` oneof unset | panic | reject | no |
| T3 | entry name empty or containing `/` | panic | reject | no |
| T4 | entry name `.`, `..` or containing NUL | accepted; fails at checkout | reject (policy stricter than jj, § 20) | no |
| T5 | entries not strictly increasing by UTF-8 bytes, or duplicates | unchecked in release builds | reject | no |
| T6 | file, symlink or tree id length ≠ 64 | accepted | reject | no |
| T7 | copy id length ∉ {0, 64}; a non-empty copy id whose `current_path` differs from the entry path | accepted | reject | no |
| C1 | `root_tree` term count 0 or even | panic | reject | no |
| C2 | label count even and non-zero | panic | reject | no |
| C3 | label count ≠ term count (labels present) | deferred panic | reject | no |
| C4 | labels on a resolved tree, or all labels empty | normalized (alias) | reject | no |
| C5 | author, committer or timestamp absent | defaulted (alias) | reject | no |
| C6 | no parents | accepted on read | reject (§ 6.4) | no |
| C7 | parent, predecessor or root-tree id ≠ 64 B; change id ≠ 16 B | accepted | reject | no |
| C8 | `predecessors` populated | used | **accept**; references validated | **yes, by default** |
| C9 | `secure_sig` present but empty | accepted | reject | no |
| O1 | no parents | root appended on read | reject | no |
| O2 | metadata, start or end time absent | defaulted (alias) | reject | no |
| O3 | `stores_commit_predecessors = false` with entries | ignored | reject | no |
| O4 | duplicate or unsorted `commit_predecessors` keys; duplicate attribute keys | last wins | reject | no |
| O5 | commit id ≠ 64 B in `commit_predecessors` | accepted; index build fails if missing | reject; references validated | no |
| V1 | legacy `wc_commit_id` non-empty | becomes `default` | reject | no |
| V2 | `Bookmark.remote_bookmarks` (deprecated) | used only if `remote_views` is empty | **accept, constrained** to equal the legacy projection of the decoded remote views | **yes** |
| V3 | bookmark with absent local target and no remote refs | dropped | reject | no |
| V4 | `RemoteBookmark.state` absent | becomes `New` | reject | no |
| V5 | `GitRef.commit_id` non-empty, or `target` absent | legacy path | reject | no |
| V6 | `git_head_legacy` non-empty | legacy path | reject | no |
| V7 | `View.git_head` (deprecated) | used only if `git_heads` is empty | **accept, constrained** to equal `git_heads["default"]` or be absent | **yes, when a default git head exists** |
| V8 | `has_git_refs_migrated_to_remote_tags = false` | migration with two reachable panics | reject | no |
| V9 | `RefTarget` message absent, legacy `commit_id` or `conflict_legacy` | legacy decoding | reject | no |
| V10 | `RefTarget` with unset oneof; conflict with no adds or `adds ≠ removes + 1` | panic | reject | no |
| V11 | `RemoteRef.target_terms` even (including 0) | error | reject | no |
| V12 | `RemoteRefState` ∉ {0, 1} | error | reject | no |
| V13 | duplicate keys in bookmarks, tags, remote views, git refs, git heads, head ids or workspace map | last wins | reject | no |
| V14 | commit id ≠ 64 B anywhere in a view | accepted | reject | no |
| V15 | reserved fields 4 and 10; any unknown field | dropped | reject | no |

Panics matter repository-wide: every client reads the shared operation log and index
building reads every ancestor view, so one accepted poisoned view would crash `jj log` for
every user. `\`, case-fold collisions and `.git` stay legal in entry names, because stock
clients on Linux can snapshot them.

The server's own writers (§ 10) emit the same deprecated projections as jj's writer
(`remote_bookmarks`, `git_head`, `has_git_refs_migrated_to_remote_tags: true`), or the
canonical check of § 6.2 would reject the server's own output.

## 7. Data model

All models live in app label `jj`. Every table is scoped by `repo_id`; object ids are
unique per repository, not globally (no cross-repository deduplication in v1 — it would
entangle authorization). The app requires `django.contrib.postgres` in `INSTALLED_APPS`
(`bytea[]` columns).

### 7.1 Canonical tier

Primary key `CompositePrimaryKey(repo, object_id)`, with `object_id` a `BinaryField`
(`bytea`); no foreign key points into canonical tables, derived rows reference them by
value, and the composite key keeps hash partitioning by repository possible later
(current default; Q14 settles it before the first migration). `data` is the wire encoding
of § 6.1, stored as uploaded — canonical by § 6.2. `hash_epoch` (small integer) names the
hash scheme the row was verified under. Decoded columns are deterministic
denormalizations filled at insert; they are never a second source of truth. Times are
stored as jj stores them — milliseconds since the epoch plus a time-zone offset in
minutes — never as `DateTimeField`, whose meaning depends on the host's `USE_TZ`.

| Model | Columns (beyond `repo`, `object_id`, `data`, `hash_epoch`) | Notes |
| --- | --- | --- |
| `File` | `size`, `content` (`bytea`, nullable), `storage_key` (nullable) | bytes inline up to `JJ_FILE_INLINE_MAX_BYTES`, else in `JJ_FILE_STORAGE` under a key derived from `(repo_id, id)`, storing the name the storage's `save()` returns; `data` unused |
| `Symlink` | `target` | |
| `Tree` | — | opaque; entries are **not** normalized into rows |
| `Commit` | `change_id`, `parent_ids` (`bytea[]`), `root_tree_ids` (`bytea[]`), `is_conflicted`, `author_name`, `author_email`, `author_time_ms`, `author_tz_offset`, `committer_name`, `committer_email`, `committer_time_ms`, `committer_tz_offset`, `description`, `is_signed` | |
| `Copy` | `current_path`, `parent_ids` (`bytea[]`) | |
| `View` | — | |
| `Operation` | `view_id`, `parent_ids` (`bytea[]`), `generation`, `time_start_ms`, `time_start_tz_offset`, `time_end_ms`, `time_end_tz_offset`, `description`, `hostname`, `username`, `is_snapshot`, `workspace_name` | `generation` = 1 + the largest parent generation (root 0), filled at insert from the parent rows; bounds ancestor walks |

### 7.2 Write-time indexes

Maintained in the same transaction as the object write; insert-only.

| Model | Columns | Purpose |
| --- | --- | --- |
| `ObjectProvenance` | `repo`, `kind`, `object_id`, `actor_ref`, `first_seen_at` | unique per `(repo, kind, object_id, actor_ref)` — **every** writer of an object, including writers whose upload deduplicated (possession proven by § 6.2 step 4); audit and the "own leaves" rule of § 13.4. Provenance records possession, not authorship. At least one row per object, so it roughly doubles id-keyed index volume; `actor_ref` may be interned |
| `Placement` | `repo`, `path`, `object_id` | unique `(repo, path, object_id)`; which object has been placed at which path. Maintained only when `Repo.path_rules_enabled` (§ 13.4); inserted in sorted batches, at most `JJ_MAX_PLACEMENTS_PER_REQUEST` per request |

### 7.3 Store tier

| Model | Columns | Rules |
| --- | --- | --- |
| `Repo` | `name` (unique slug), `created_at`, `empty_tree_id`, `hash_scheme` (e.g. `jj-content-hash@0.45.1`), `path_rules_enabled` | namespace and FK target only — no owner, no grants. Never locked with `SELECT … FOR UPDATE`: every canonical insert's foreign-key check takes a share lock on it |
| `LogCounter` | `repo` (primary key), `op_seq`, `publication_seq` | per-repository counters for the two logs; the row lock serializes log appends per repository (§ 8.1) |
| `OpHead` | `repo`, `operation_id` | unique `(repo, operation_id)`; the table is a **set** |
| `OpHeadLog` | `repo`, `seq`, `added`, `inserted` (bool), `removed` (`bytea[]`), `actor_ref`, `on_behalf_of` (nullable), `created_at` | append-only audit trail **and** derivation outbox; `seq` is **per repository, dense and commit-ordered**, `UNIQUE (repo, seq)`, assigned from `LogCounter` (§ 8.1); `inserted` is false when `added` was already a head |
| `MergeLease` | `repo` (unique), `holder` (actor ref), `token`, `expires_at`, `head_set` (hash of the head ids), `last_failure_at` | at most one live lease per repository; times from the database clock; owned by `token`, never by holder (§ 8.3) |
| `Publication` | `repo`, `name`, `commit_id` (nullable), `version`, `require_fast_forward`, `forbid_conflicts`, `updated_at` | unique `(repo, name)`; moves only through `publish`; a garbage-collection root and a derivation root (§ 11.1); a recreated publication continues from the highest version ever logged for its name |
| `PublicationLog` | `repo`, `seq`, `name`, `old_commit_id`, `new_commit_id`, `version`, `op_seq_at_publish`, `actor_ref`, `created_at` | append-only audit **and** outbox; `seq` per repository, dense, commit-ordered, from `LogCounter` |
| `UploadSession` | `repo`, `upload_id` (UUID), `actor_ref`, `kind`, `object_id`, `size`, `path`, `staging_key`, `offset`, `state`, `created_at`, `expires_at` | large-file uploads (§ 12.4); expired sessions are swept by `manage.py jj cleanup` |
| `IdempotencyRecord` | `repo`, `actor_ref`, `endpoint`, `key`, `fingerprint`, `status`, `response`, `created_at` | unique `(repo, actor_ref, endpoint, key)`; retained `JJ_IDEMPOTENCY_RETENTION_SECONDS` (§ 12.1) |

### 7.4 Derived tier

Every derived row carries the deriver `version` and build `generation` that produced it;
readers select the active `(version, generation)` (§ 11.4).

| Model | Deriver | Contents |
| --- | --- | --- |
| `CommitParent` | `jj.commit_parents` | `(repo, commit_id, parent_id, position)` |
| `CommitGraph` | `jj.commit_graph` | `(repo, commit_id, generation)` — ancestry queries, fast-forward checks |
| `ChangedPath` | `jj.changed_paths` | `(repo, commit_id, term, path, kind, object_id, previous_object_id, change)` for every file and directory entry that differs from the first parent's tree (Fossil's `mlink`); conflicted commits get rows per added term. Path-keyed: subject to path rules on read (§ 13.4) |
| `CopyEdge` | `jj.copy_edges` | `(repo, copy_id, parent_copy_id)` — `get_related_copies` via recursive CTE; immutable history only |
| `ViewRef` | `jj.view_refs` | `(repo, view_id, kind, name, target)` for bookmarks, tags and workspace commits |
| `Conflict` | `jj.conflicts` | `(repo, commit_id, path)` for conflicted commits. Path-keyed |
| `DeriverState` | framework | `(repo, deriver, version, generation, status, last_op_seq, last_publication_seq, dep_versions, last_error, error_count, updated_at)`; `status` ∈ {`building`, `active`, `retired`}; at most one `active` row per `(repo, deriver)`. The watermark, the tracking record and the mutex (§ 11.2) |

Hosts add derived models of their own through `JJ_DERIVERS` (§ 11).

### 7.5 Indexes and SQL conventions

- Canonical: the composite primary key `(repo_id, object_id)`; `Commit(repo_id,
  change_id)`; `Operation(repo_id, view_id)`.
- `Placement(repo_id, path, object_id)` and `(repo_id, object_id)`.
- `ChangedPath(repo_id, version, generation, commit_id)` and `(repo_id, version,
  generation, path)`; every derived index leads with `(repo_id, version, generation)`.
- `OpHeadLog(repo_id, seq)` and `PublicationLog(repo_id, seq)`, unique.
- **Batch reads** use `WHERE repo_id = %s AND object_id = ANY(%s::bytea[])` with one list
  parameter per kind — never per-object queries, never `__in` placeholder lists.
- **Inserts** use `INSERT … ON CONFLICT DO NOTHING RETURNING` (the returned ids are
  exactly the rows inserted, which `OpHeadLog.inserted` and first-writer detection
  need), with rows **sorted by key** so concurrent overlapping batches cannot deadlock.
  The server commits `objects:put` in sub-batches of consecutive items, one transaction
  each; every item of a sub-batch is validated before any of its inserts, with no per-item
  savepoints, and the sub-batch is acknowledged when it commits.
- **Bytes travel in binary.** Object reads and writes use a server-binding psycopg cursor
  with binary parameters and results; Django's default client-side binding would send
  `bytea` as hex, doubling request-sized SQL.
- **Streams page by key** in short transactions (parents-first keyset batches), never with
  server-side cursors, which break under transaction-pooling proxies. Insert-only
  canonical rows make non-snapshot batched reads safe: later batches can only see more
  rows, never changed ones.
- **Storage settings**, set in the migrations that create the tables (reversible,
  guarded where the server lacks the feature): `lz4` compression on `data` and
  `File.content`, and a low `autovacuum_vacuum_insert_scale_factor` on the large
  insert-only tables. Later indexes use concurrent creation.
- **Advisory lock keys** come from one documented function,
  `jj_lock_key(purpose, repo_id, name) → int64` (the first eight bytes of Blake2b over a
  tagged tuple), always in the single-`bigint` form. Collisions only over-serialize.

---

## 8. Operation heads

### 8.1 `update_op_heads(old_ids, new_id)`

1. Authenticate; `can_write_repo`.
2. `new_id` is the root operation, or an existing `Operation` whose view and parents
   exist. Otherwise `INVALID`.
3. Every `old_id` other than `new_id` must be one of `new_id`'s ancestors — which is all jj
   passes, except `jj op abandon` (§ 8.6). Otherwise `invalid_request`: without this rule a
   writer could delete every head and roll the repository back to an old operation. The
   check is a level-by-level walk over `Operation.parent_ids` that stops as soon as every
   `old_id` is found and prunes by `generation`; it never walks the whole log.
4. Authorize what the operations newly reachable from `new_id` change (§ 8.4): the
   ancestors-or-self of `new_id` that are not ancestors-or-self of any current head, for
   the acting actor — the caller, or the actor a service caller names in `on_behalf_of`
   (§ 10.4). Reachability from heads only grows, so a race here can only make the
   check more conservative.
5. In one transaction:
   1. Lock the repository's `LogCounter` row and draw the next `op_seq`. Taking this lock
      first serializes the rest per repository, so `seq` is dense and in commit order,
      and concurrent deletes of overlapping head rows cannot deadlock.
   2. If `new_id` was inserted as a head before (an `OpHeadLog` row with `added = new_id`
      and `inserted = true` exists) and is no longer a head, it is an ancestor of a current
      head — a retry after a lost response. Roll back and return success with
      `changed = false`.
   3. `INSERT` `(repo, new_id)` into `OpHead` if absent (record whether it was inserted).
   4. `DELETE` every `old_id` other than `new_id` (absent rows are ignored — another writer
      already removed them).
   5. Append `OpHeadLog(seq, added=new_id, inserted, removed=old_ids, actor_ref,
      on_behalf_of)`.
   6. If the head set now has more than one head, the worker is enabled
      (`JJ_WORKER_ENABLED`) and no live lease exists, grant the worker the merge lease for
      this head set (§ 8.2).
6. On commit: send `op_heads_updated(repo, seq)` (robustly) and enqueue derivation
   (§ 11.3) and, when a lease was granted, the worker merge. Return `{seq, changed}`.

It **never** compares `old_ids` with the current set. If two writers race, both succeed,
the set holds both new heads, and divergence is resolved as below. Lock waits, lock
timeouts and deadlocks inside step 5 are retried internally: an aborted update is a
refusal, which invariant 1 forbids.

### 8.2 `get_op_heads()` and divergence resolution

- Returns every `OpHead` row; if there are none, returns the root operation id. Never
  empty. Head reads never wait and never trigger a merge.
- **Resolution starts at the write that creates divergence.** When § 8.1 leaves more than
  one head and the worker is enabled (§ 10.4), the same transaction grants the worker the
  merge lease for the new head set, and the worker merge is enqueued on commit. The worker
  loads the repository with jj-lib, which merges the heads under that lease with jj's own
  `merge_operations`, as a service actor.
- Clients that read divergent heads take `lock()` (§ 8.3), which polls the non-blocking
  lease endpoint with bounded backoff while the worker holds it, then re-reads the heads —
  usually finding the worker's single merged head. This is jj's own "lock, then re-read"
  path; no request thread waits on the server.
- **Backstops.** Whenever `op-heads:lock` or `edit_workspace` finds divergent heads, no live
  lease and the worker enabled, the server grants the worker the lease and enqueues a
  merge; `manage.py jj resolve --watch` sweeps repositories left divergent. A lost enqueue
  or a crashed worker delays resolution but never stops it.
- Resolution is deduplicated per head set, and a head set whose resolution failed is not
  retried for `JJ_MERGE_LEASE_SECONDS`.
- If heads are still divergent when a client's lock budget runs out — or the worker is
  disabled — the client merges them itself, as jj always does. Its merge operation is
  authorized like any other (§ 8.4), so actors without rights over every ref and workspace
  the merge touches are refused (and recover as § 6.3 describes). Deployments where such
  actors exist must enable the worker (`jj.W004`); without it, divergence clears only when
  a sufficiently privileged client merges.

### 8.3 `lock()` — the merge lease

jj takes `OpHeadsStore::lock()` so that concurrent processes do not resolve the same
divergence twice (duplicate merges would rebase the same descendants into divergent
changes). It also takes it on every transaction commit, so the lease must cost nothing in
the common case:

- The client's `lock()` returns a guard that holds nothing unless the store's most recent
  `get_op_heads` returned more than one head. The trait permits such a guard.
- Otherwise it calls `POST op-heads:lock`, a **non-blocking try-lock**: it returns `{token,
  expires_at}` or `{held, retry_after_ms}` at once. When the worker is enabled and no live
  lease exists, the server grants the lease to the worker and enqueues a merge instead,
  answering `held`. The client polls with backoff up to its lock budget, then proceeds
  without the lease. `POST op-heads:unlock {token}` releases it.
- The lease is a `MergeLease` row acquired with an upsert that succeeds only when no live
  lease exists; `expires_at` uses the database clock; ownership is by `token`, so two
  processes of one actor never share a lease. Leases expire after
  `JJ_MERGE_LEASE_SECONDS`.
- A lease granted to the worker (§ 8.2) carries a token handed to the worker with the merge
  request; the worker's own `lock()` presents it. The holder renews a lease by calling
  `op-heads:lock` with its token before expiry; the worker renews every third of
  `JJ_MERGE_LEASE_SECONDS` while it merges.
- Correctness never depends on the lease. An expired lease risks duplicate merges, which
  jj's lock-free design tolerates but which can leave divergent changes for users to clean
  up — hence renewal.

### 8.4 Authorizing what operations change

Checked at `op-heads:update` (§ 8.1 step 4), over the operations newly reachable from
`new_id`, for the acting actor (the caller, or the actor a service caller names in
`on_behalf_of`). Operations that never become reachable from a head — for
example the virtual bases of criss-cross merges — are never authorized and never needed.

For each such operation, compare its view with its parents' views, value by value. A
**value** is a local bookmark, a tag, a remote ref (target and tracking state), a git ref,
or a workspace entry — `wc_commit_ids[w]` together with `git_heads[w]`. An absent value is
an ordinary value.

- **Single parent:** a value is changed iff it differs from the parent's.
- **Several parents:** a value is inherited only if every parent has the same value and the
  operation keeps it; every other value is a change.

jj's merge folds heads pairwise in a client-controlled order with a different base at each
step (§ 3), so rules that attribute a merged value to "a parent that changed it relative
to the base" can be forged — a three-parent merge built from a fresh child of an old
operation can roll a protected bookmark back — and also refuse legitimate jj output. The
conservative rule above is the one that survives: merges are the worker's job, and the
worker, as a service actor, normally holds every right. This is also why stock jj rebases
performed during a merge — moved bookmarks, rebased working copies of other workspaces —
count as changes.

Changed workspace entries require `can_write_workspace(actor, repo, name)`; changed refs
require `can_change_refs(actor, repo, changes)`, where each `RefChange` carries `(kind,
name, old_values, new)` with one old value per parent. Changes to `head_ids` need no
authorization beyond `can_write_repo`. A refused update leaves its already-uploaded objects in
place (unreferenced; garbage for Q6), and the client command fails like a refused push.

### 8.5 Read-only actors

`GET repos/{repo}` reports whether the actor may write. For a read-only actor the client
runs with a **local overlay**: objects and operations it writes (snapshots, merges) stay in
its local store, and nothing is uploaded. Its op heads are

    (server heads − local whiteouts) ∪ local heads, or the root operation if that is empty,

where the whiteouts are the server head ids removed by the overlay's own `update_op_heads`
calls (including jj's ancestor-pruning calls that write no operation), forgotten once the
server stops reporting them. Every server advance while a local head exists costs one
local merge. The overlay tolerates a missing parent operation after server-side garbage
collection by rebasing onto current server heads. A read-only clone is usable for
reading, diffing and local experiments. (Details settle in M3; Q13.)

### 8.6 Shared operation logs

- **`jj undo`** restores the parent view of the latest operation — which may be another
  actor's, and includes every workspace entry that operation changed. It fails outright
  on a merge operation ("Cannot undo a merge operation"), which after a worker reconcile is
  common. On a shared log, recommend `jj op revert`. Both are authorized by § 8.4.
- **`jj op restore`** needs rights over the values that differ between the current view
  and the restored one.
- **`jj op abandon`** rewrites operation history so that removed heads are not ancestors of
  the new head; it is refused at `op-heads:update` (§ 8.1 step 3) after uploading its
  rewritten operations. `jj-django` refuses it before uploading where it can detect it.
- **`jj op integrate`** works: the operation is in the client's journal (§ 6.3), and its
  closure is uploaded first. It takes no lock and may re-add an operation that is already
  an ancestor of a head; derivers compute "newly reachable" by ancestry, not by the added
  id.
- **`jj op log -p`, `op show`, `op diff`** compute merges for display and write the results
  locally; under § 6.3 they are never uploaded.
- **Operation metadata is client-asserted.** Descriptions, hostnames, usernames, times and
  attributes (including the command line) are stored as sent; `OpHeadLog.actor_ref` is the
  authoritative record of who moved the heads.
- **Irreconcilable histories.** jj histories can become impossible to merge. A service-only
  `manage.py jj resolve --repo NAME --take OP` writes a merge operation whose parents are
  all current heads and whose view is `OP`'s view — legal under § 8.1, because every
  removed id is a parent.
- All of these are covered by client e2e tests (§ 19).

## 9. Publications

A publication is a named, server-owned pointer to a commit: what an application treats as
"published" (a site, a knowledge base, an export). It exists because op heads must never
refuse, yet something must be able to say no. Publications live only in the store tier; v1
does not mirror them into jj views (a mirrored ref would be merged, and could conflict, in
every client's view). The name is not Pulp's "Publication" (an immutable artifact); the
closest analogues are Pulp's Distribution and Mononoke's publishing bookmark.

### 9.1 Publish

`publish(repo, name, commit_id, expected_version, actor, idempotency_key=None)`:

1. The commit must exist.
2. If the publication already points at `commit_id`, authorize (step 5) and return
   `{version: current, unchanged: true}` without logging — a retry whose first attempt
   landed succeeds. If `version ≠ expected_version`, return `version_conflict` with
   `{current_version, current_commit_id}` before doing any diff work.
3. Compute the `PublishRequest`:
   - **changed paths** — a direct diff of the old and new root trees (walk both, skip equal
     subtree ids; cost proportional to the change). For conflicted commits the diff runs per
     added term.
   - **is_fast_forward** — whether the old commit is an ancestor of the new one: from
     `jj.commit_graph` when it holds both commits, otherwise by a bounded walk over the
     canonical `Commit.parent_ids`, pruned by `CommitGraph` generations where known. Only
     when the walk exceeds `JJ_FAST_FORWARD_WALK_LIMIT` commits does publish return
     `not_derived` (retryable). The walk reads canonical rows and writes nothing.
   - **is_conflicted** — from the commit row.
4. Built-in rules: `require_fast_forward`, `forbid_conflicts`.
5. `authorizer.can_publish(actor, publication, request)` returns a `Decision`. In
   repositories with path rules this includes `can_write_paths` over the changed paths;
   a refusal names at most the restriction root, never a deeper restricted path.
6. In one transaction: `UPDATE jj_publication SET commit_id=…, version=version+1 … WHERE
   id=… AND version=expected_version` (zero rows → `version_conflict` with the current
   state); lock the `LogCounter` row, draw `publication_seq`, and append `PublicationLog`
   with `op_seq_at_publish`. On commit send `publication_moved(repo, name, version)`
   robustly and enqueue derivation. Return `{version, seq}`.

Publication targets are derivation roots (§ 11.1) and garbage-collection roots, so a
published commit always receives derived data, whether or not a logged operation reaches
it. A target need not be reachable from any operation's view — for example a commit
uploaded for an operation that was later refused. This is deliberate: publish has its own
authorization (steps 4–5), so an unreachable target gains no rights; hosts that want only
view-reachable targets check it in `can_publish`. Version numbers make the
compare-and-swap immune to a pointer that moves A → B → A. Like server edits, `publish`
runs outside host transactions (§ 10).

### 9.2 Reading published state

Anything "published" — exports, host projections, default browsing — reads the
publication's `commit_id`, never whichever op head or bookmark is current. Host readers
that need the derived data of a publication gate on `derived_up_to` (§ 11.4). Clients list
publications with `GET publications` (and `jj-django publications`).

---

## 10. Server-side writes

The server writes like one more jj process: read heads, write objects, write a view and an
operation with the heads it read as parents, then `update_op_heads` — computing ids with
the Python port (§ 6.2) and running the same verification path as uploads, including
§ 6.5 and the path rules.

`edit_workspace`, `publish` and `operation()` must be called outside any
`transaction.atomic()` block and raise `TransactionManagementError` otherwise: inside a host
transaction, the repository's `LogCounter` lock would be held until the host commits,
stalling every writer to the repository. Host views that call them use
`transaction.non_atomic_requests` when `ATOMIC_REQUESTS` is on.

### 10.1 `edit_workspace` (the common case)

`edit_workspace(repo, actor, workspace, edits, *, message=None, op_description,
base=None, expected_wc_commit_id=None, on_mismatch="refuse", idempotency_key=None)`
applies file edits to a workspace's working-copy commit. Edits are:

| Edit | Meaning |
| --- | --- |
| `Put(path, data, executable=False, expected=None)` | write bytes |
| `PutId(path, file_id, executable=False, expected=None)` | write existing content by id; must pass graft rule 3 at the new path |
| `Symlink(path, target, expected=None)` | write a symlink |
| `Delete(path, expected=None)` | remove a file, symlink or directory |
| `Move(src, dst, expected=None)` | move a file or directory, reusing object ids |
| `Chmod(path, executable, expected=None)` | change the executable bit only |

`expected` is an optional precondition: the file id currently at the path, or `ABSENT`.
`message` is the commit description (`None` keeps the target's); `op_description`
describes the operation.

1. Path checks first: with path rules, every edited path must be readable and writable by
   the actor (`can_write_paths`), whether or not it exists — otherwise `forbidden`,
   uniformly. `can_write_workspace(actor, repo, workspace)`.
2. Read the heads, outside any transaction. If they disagree on
   `wc_commit_ids[workspace]`, return `requires_merge` (retryable; with the worker enabled
   a merge is enqueued if none is running, § 8.2; without it, only a client merge clears
   it).
3. Take `pg_advisory_xact_lock(jj_lock_key("workspace", repo, workspace))` so edits to one
   workspace serialize, and re-read the heads; if they now disagree, `requires_merge`.
   Otherwise use the head with the highest `OpHeadLog.seq` (all agree on the workspace).
4. Resolve the target:
   - If the workspace exists, its working-copy commit is the target:
     - if the target is not in the view's `head_ids`, it has visible descendants → return
       `requires_rebase` (the worker's job, § 10.4);
     - if the target is conflicted, or is also the target of a ref or of another workspace
       (a jj rewrite would move those) → return `requires_worker`;
     - if the target is the commit of any publication, or an ancestor of one, treat it as
       immutable: create a **new change on top** of it (jj's rule for an immutable working
       copy) and move the workspace to it;
     - otherwise the new commit keeps the target's change id and replaces its tree.
   - If the workspace does not exist, create a new change (random change id) on `base` (a
     commit or a publication; default: the root) and add the workspace entry.
   - If `expected_wc_commit_id` is given and differs from the target, return
     `edit_conflict`.
5. Check each edit's `expected`. On a mismatch, `on_mismatch="refuse"` returns
   `edit_conflict` listing the paths; `on_mismatch="conflict"` records a jj conflict
   instead: the new commit's root tree becomes the three-term merge `[current, base,
   edited]` of whole root trees, labelled, where `base` is the current tree with the
   expected values at the mismatched paths. This needs only path copies, never a merge
   algorithm (§ 10.3).
6. If the resulting tree and message equal the target's, write nothing and return the
   existing `{commit_id, change_id, operation_id}` with `unchanged: true`.
7. Write files, then trees bottom-up by path copy (only directories on edited paths are
   rewritten), then the commit — committer = the actor (§ 6.5); author per jj's
   `for_rewrite_from` rules (author kept; an empty author name or email filled from the
   committer; the author timestamp reset when author equals committer and the target is
   discardable); deprecated `commit.predecessors` as jj's writer emits them — the view
   (`head_ids`, `wc_commit_ids[workspace]`, deprecated projections per § 6.7), and the
   operation (`workspace_name`, `op_description`, `is_snapshot = false`,
   `commit_predecessors = {new: [old]}` or `{new: []}`, attributes `django-jj.actor` and,
   when given, `django-jj.request-id`, and deterministic `username` and `hostname`).
8. `update_op_heads([chosen head], new)` (§ 8.1); the counter lock is the last lock taken.
9. Return `{commit_id, change_id, operation_id, seq}`.

Server edits are meant for **server-owned workspaces** (for example one per editing user
of a host application). If a client has the same workspace checked out, its working copy
becomes stale and needs `jj workspace update-stale`.

### 10.2 `operation()` (low-level, provisional)

`with django_jj.operation(repo, actor, description=…) as op:` exposes object writes and view
edits for hosts that need more than `edit_workspace`; the context writes the operation and
updates op heads on exit. It stamps committer = actor by construction and runs the same
verification path as uploads, so host code cannot bypass § 6.5 by accident. Its surface
settles in M4.

### 10.3 What Python never does

Python code does not merge views, merge ref targets, rebase commits, resolve conflicts, or
simplify `Merge<TreeId>` beyond "all terms equal". Those are jj algorithms and run in jj.
Building a conflicted commit from whole root trees by path copy (§ 10.1 step 5) is not a
merge: it records the terms and leaves resolution to jj.

### 10.4 The jj worker

`jj-django worker` is the client binary running as a long-lived process on the server
side, out of the Django process, authenticated with a host-issued credential for a
**service actor** (§ 13.1), with warm caches and a large stack. It performs jj semantics on
request:

- **merge** — load the repository with jj-lib, which resolves divergent heads under the
  lease granted at divergence (§ 8.2) and writes the merge operation;
- **rebase** — rebase a workspace's changes onto a publication or a new base, or rebase
  descendants after an amend, recording conflicts as jj does.

A request a host forwards for a user carries that user as `on_behalf_of`. The library
checks `can_write_workspace` for them before dispatch, and the worker passes
`on_behalf_of` with its `op-heads:update`, so § 8.4 authorizes the changes the rebase
actually makes — including bookmarks it moves — for that user, and
`OpHeadLog.on_behalf_of` and the operation's attributes record them. Merges the worker
starts itself carry no `on_behalf_of`. Without this, a host that forwards user requests to
the all-powerful worker becomes a confused deputy. Requests have a timeout
(`JJ_WORKER_TIMEOUT_SECONDS`); a crash or timeout is retryable and never corrupts the
store, because the worker writes like any client.

With `JJ_WORKER_ENABLED`, the library requests work through `django_jj.worker`: it runs
`JJ_WORKER_COMMAND` when that is set, and always sends the `worker_requested(repo, kind,
token, on_behalf_of)` signal on commit, so hosts can drive the worker from their own task
queue instead. The invocation transport settles in M4 (Q12).

## 11. Derived data

### 11.1 Deriver protocol and the derivation domain

```python
class Deriver(Protocol):
    name: ClassVar[str]                          # stable key, e.g. "jj.changed_paths"
    version: ClassVar[int]                       # bump to rederive
    depends_on: ClassVar[tuple[str, ...]]
    models: ClassVar[tuple[type[Model], ...]]    # tables this deriver owns (for rebuild)

    def derive(self, ctx: DeriveContext, batch: DeriveBatch) -> None: ...
```

The **derivation domain** of a repository is everything reachable from the operations
named in `OpHeadLog.added` (their ancestor operations, their views, and the commits those
views reference, including predecessor commits), plus every publication target. Incremental
derivation and rebuild both compute over this domain — never over all `Commit` rows, which
would include objects uploaded for refused operations and make rebuild differ from
incremental.

`DeriveBatch` carries the repository, the `OpHeadLog` and `PublicationLog` ranges, and the
operations, views and commits that enter the domain in them — computed by ancestry, not by
the added id alone (`jj op integrate` may add an operation that is already reachable). A
deriver writes only its own models, idempotently (`ON CONFLICT DO NOTHING` on
content-addressed keys plus `(version, generation)`).

### 11.2 Execution

`django_jj.derive.run_pending(repo=None, limit=…)` processes log rows past each deriver's
watermark, one transaction per (deriver, batch):

1. Ensure the `DeriverState` row exists.
2. `SELECT … FOR UPDATE SKIP LOCKED` the state row for `(repo, deriver, version,
   generation)`; if another worker holds it, move on.
3. Cap the batch at `min(log head, each dependency's watermark)` — a dependent never gets
   ahead of what it depends on.
4. Read the log rows in `(watermark, cap]`, derive, advance the watermark **in the same
   transaction**, and commit.

The state row is the watermark, the tracking record and the mutex; it works under
transaction-pooling proxies, unlike session advisory locks. A failing batch rolls back,
records `last_error` and pauses that deriver and its dependents — it never skips.
`run_pending(repo=None)` iterates repositories with `SKIP LOCKED`, so several workers
spread across repositories. Batches are bounded by log rows and by objects touched.

### 11.3 Dispatch

- The library ships a Django task, `django_jj.tasks.derive(repo_id)`, that runs
  `run_pending(repo)`. When `JJ_DERIVE_ENQUEUE` is true (default), `update_op_heads`,
  `publish` and server writes enqueue it with `transaction.on_commit(…, robust=True)`. With
  Django's default immediate backend derivation runs right after commit in the same
  request — read-your-writes in development and tests; production hosts configure a real
  task backend. Duplicate enqueues are harmless.
- `manage.py jj derive --watch [--interval S]` polls repositories whose log heads are past
  some watermark — the backstop for lost callbacks and crashed workers. `derive --once` and
  `derive --status` (lag and last error per deriver) serve operations.
- The `op_heads_updated` and `publication_moved` signals are sent with `send_robust` on
  commit and remain host hooks. The library never issues `NOTIFY` inside the op-heads
  transaction.

### 11.4 Reading derived data

- `derived_up_to(repo, deriver)` returns the log positions the active build has processed
  (Mononoke's "warm bookmark" rule). `op-heads:update`, `publish` and `edit_workspace`
  return the `seq` they logged, so a caller can wait for its own write:
  `wait_for_derived(repo, deriver, op_seq=…, publication_seq=…, timeout=…)` polls and holds
  no transaction or connection while waiting.
- Readers resolve the active `(version, generation)` of a deriver once per request and
  filter on it.
- Path-keyed derived rows are subject to path rules: `django_jj.paths.readable(queryset,
  actor, repo, path_field="path")` filters them, and every built-in read API uses it.
  Derivers never follow symlinks across path rules.

### 11.5 Rebuild and version changes

Rebuilds and version bumps build **side by side** and flip:

1. `manage.py jj rebuild [--repo NAME] [--deriver NAME]`, or a deployed deriver version with
   no active build, inserts a `building` state at watermark zero with a new `generation`.
2. `run_pending` advances building and active states independently. A dependent's building
   generation reads its dependency's building generation, or waits until that is active.
3. When the building watermark reaches the log head, one transaction marks it `active` and
   the old state `retired`; retired rows are deleted in batches afterwards.

Readers see the old build until the flip. `rebuild --in-place` — one transaction that
deletes the repository's rows and re-derives — is for small repositories and tests.
`TRUNCATE` is never used: it is table-wide and would wipe every repository. Tests assert
`incremental == rebuild` after randomized histories (§ 19).

---

## 12. Protocol v1

### 12.1 Conventions

- Mounted by the host: `path("jj/", include("django_jj.urls"))`; endpoints live under
  `jj/v1/`. Protocol views are non-atomic — `transaction.non_atomic_requests` is applied to
  the view callables, so `ATOMIC_REQUESTS` never wraps them; each operation manages its
  own transactions.
- **Header authentication only.** Every endpoint requires credentials in the
  `Authorization` header, and authenticators never consult the session or
  `request.user` (§ 13.2), so a browser's cookies cannot authenticate a protocol request.
  The endpoints are CSRF-exempt for that reason. Browser applications call the Python APIs
  (`edit_workspace`, `publish`) from their own CSRF-protected views.
- **Request bodies are read as streams.** Protocol views read with `request.read(n)` and
  enforce `JJ_MAX_REQUEST_BYTES` themselves (early from `Content-Length`, and while
  reading); they never touch `request.body` or `request.POST`, which Django caps at
  `DATA_UPLOAD_MAX_MEMORY_SIZE` (2.5 MB by default). Host middleware that reads the body
  breaks the protocol.
- **Messages** are protobuf, defined in `proto/django_jj/v1/*.proto` in this repository;
  object payloads carry the wire encodings of § 6.1. Content type
  `application/x-django-jj-v1+protobuf`. Unary requests and responses are single
  messages.
- **Streams** — streamed responses, and the streamed request of `objects:put` — use
  Connect-style envelopes: a one-byte flag field and a four-byte big-endian length before
  each message. Frames carry a `StreamItem` (an item or a heartbeat); the terminal frame
  (flag `0x02`) carries `StreamEnd {status, error?, complete, resume?, ignored_known}`. **A
  stream without a terminal frame is an error**: a cut at a message boundary must never
  look complete. Streams send a heartbeat at least every `JJ_STREAM_HEARTBEAT_SECONDS`, set
  `X-Accel-Buffering: no`, and end with `complete = false` plus a resume hint when they
  exceed the server's budget. Views provide sync and async iterators, so streaming works
  under WSGI (threaded or async workers) and ASGI alike.
- **Compression:** `Content-Encoding: zstd` (or `gzip`) on whole streams and large
  requests; consecutive views compress well only as a whole stream.
- `?format=json` returns the protobuf JSON mapping for debugging.
- Every batch response carries a **per-item status**: `OK`, `NOT_FOUND`, `FORBIDDEN`,
  `INVALID` (with a structured `missing: [{kind, id}]` list when that is the cause),
  `ID_MISMATCH`, `TOO_LARGE` (use an upload session), or `RETRY` (with `retry_after_ms`).
  One failing item never fails the batch, and **a batch response is HTTP 200 whenever the
  request was well-formed**; the statuses of § 12.5 apply to whole-request failures.
- **Idempotency.** `publish` and `workspaces/{name}:edit` accept an optional
  `Idempotency-Key` header (Python: `idempotency_key=`). The server stores `(actor,
  endpoint, key) → {fingerprint, response}` and returns the stored response on a retry,
  `request_in_progress` while the first request is in flight, and `idempotency_key_reused`
  when the same key arrives with a different request. Prior art: Stripe; IETF
  draft-ietf-httpapi-idempotency-key-header (expired draft 07).

### 12.2 Endpoints

| Method | Path | Request → Response |
| --- | --- | --- |
| `GET` | `info` | → protocol versions, jj pin, capabilities (§ 12.4) |
| `POST` | `repos` | `{name}` → repository info (`can_create_repo`) |
| `GET` | `repos/{repo}` | → id lengths, root ids, `empty_tree_id`, `hash_scheme`, concurrency hint, capabilities, `path_rules_enabled`, and for the actor: committer identity and digest, `can_write`, outermost denied path prefixes (§ 13.4). `not_found` if the actor cannot read the repository |
| `POST` | `repos/{repo}/objects:get` | `[{kind, id, path?}]` (`path` required for files, symlinks and trees with path rules) → stream of `{kind, id, status, size, data \| url}` |
| `POST` | `repos/{repo}/objects:put` | stream of `{kind, id, path?, size, data}` in topological order (`path` required for files, symlinks and trees), optionally ending with an `op_heads_update {old_ids, new_id, on_behalf_of?}` trailer → stream of per-item `{status}` acknowledgements as sub-batches commit, then the trailer's result |
| `POST` | `repos/{repo}/operations:ancestors` | `{heads, known}` → stream of operations with their views, parents before children |
| `POST` | `repos/{repo}/commits:ancestors` | `{operations, indexed_operations, known_heads?, limit?}` → stream of the commits the operations' views reference and their ancestors, including predecessor commits, parents before children |
| `POST` | `repos/{repo}/operations:resolve-prefix` | `{prefix}` → `{status: unique\|ambiguous\|none, id?}` |
| `GET` | `repos/{repo}/op-heads` | → `{ids, identity_digest}` (never empty; § 8.2) |
| `POST` | `repos/{repo}/op-heads:update` | `{old_ids, new_id, on_behalf_of?}` (`on_behalf_of` from service actors only) → `{seq, changed}` |
| `POST` | `repos/{repo}/op-heads:lock` | `{token?}` → `{token, expires_at}` or `{held, retry_after_ms}` (never waits; a holder's token renews) |
| `POST` | `repos/{repo}/op-heads:unlock` | `{token}` → `{}` |
| `POST` | `repos/{repo}/copies:related` | `{copy_id}` → related copies (filtered, § 13.4) |
| `POST` | `repos/{repo}/copies:records` | `{paths?, root, head}` → stream, reverse-topological (filtered) |
| `POST` | `repos/{repo}/uploads` | `{kind, id, size, path}` → `{upload_id, state, chunk_min_bytes, expires_at}` (§ 12.4) |
| `PATCH` / `HEAD` / `DELETE` | `repos/{repo}/uploads/{upload_id}` | append a chunk at `Upload-Offset` / read the offset / cancel |
| `POST` | `repos/{repo}/uploads/{upload_id}:finish` | → per-item status |
| `GET` | `repos/{repo}/publications[/{name}]` | → publication(s) |
| `POST` | `repos/{repo}/publications/{name}:publish` | `{commit_id, expected_version}` → `{version, seq, unchanged}` |
| `GET` | `repos/{repo}/workspaces/{name}` | `?paths=…` → `{wc_commit_id, change_id, operation_id, files?}` (preconditions for edits) |
| `POST` | `repos/{repo}/workspaces/{name}:edit` | `{edits, message?, op_description, base?, expected_wc_commit_id?, on_mismatch?}` → `{commit_id, change_id, operation_id, seq, unchanged}` |

Stream semantics:

- `known`, `indexed_operations` and `known_heads` are **hints**: ids the server does not
  know are ignored (a client may hold operations the server has never seen) and listed in
  the terminal frame's `ignored_known`.
- Parents-first order means every received prefix is closed under ancestry; a client whose
  stream ends early resumes with a new request naming what it received. The server keeps no
  cursor.
- `objects:put` acknowledges items sub-batch by sub-batch as they commit (§ 7.5), so a
  client whose connection drops can mark the acknowledged prefix and resend less.
- There is **no** general "which of these do you have" endpoint: with path rules it would
  confirm guessed content, and skipping uploads would skip the proof of possession that
  provenance relies on (§ 6.2).

Later: `GET repos/{repo}/bundle?upto=<operation>` — a static cold-start bundle.

### 12.3 Mapping to jj's store traits

| Trait method | Client implementation |
| --- | --- |
| `Backend::name`, `OpStore::name`, `OpHeadsStore::name` | `"django-jj"` |
| `commit_id_length`, `change_id_length`, `root_commit_id`, `root_change_id`, `empty_tree_id`, `concurrency`, `OpStore::root_operation_id` | from repository info cached in `.jj/repo/store/django-jj.toml` at init/clone (store factories are synchronous) and refreshed on first request |
| `read_file`, `read_symlink`, `read_tree`, `read_commit`, `read_copy`, `read_view`, `read_operation` | local cache, else `objects:get`; concurrent single reads are **coalesced** into batched requests (jj-lib leaves queueing to the backend) |
| `write_file`, `write_symlink` | compute id locally, cache, journal, upload eagerly (§ 6.3) |
| `write_tree`, `write_copy`, `write_view`, `write_operation` | compute id locally, cache, journal; uploaded with the closure of a head update |
| `write_commit` | stamp the server identity (§ 6.5), then encode, sign, hash, cache, journal |
| `get_related_copies`, `get_copy_records` | `copies:related`, `copies:records` |
| `resolve_operation_id_prefix` | journal and cache, then `operations:resolve-prefix` |
| `get_op_heads` | `op-heads` (refreshes the committer identity when the digest changed) |
| `update_op_heads` | upload the closure of `new_id`, then `op-heads:update` (or the trailer) |
| `OpHeadsStore::lock` | a no-op guard unless the last head read saw several heads; else `op-heads:lock` try-lock with bounded polling (§ 8.3) |
| `Backend::gc`, `OpStore::gc` | `Unsupported` in v1 (Q6) |

### 12.4 Limits, capabilities and large files

- `GET info` and `GET repos/{repo}` advertise the capabilities clients split on:
  `max_batch_items` (`JJ_MAX_BATCH_ITEMS`), `max_request_bytes` (`JJ_MAX_REQUEST_BYTES`),
  `max_inline_object_bytes` (`JJ_INLINE_UPLOAD_MAX_BYTES`: larger files must use an upload
  session), `max_object_bytes` (`JJ_UPLOAD_SESSION_MAX_BYTES`), `content_encodings`,
  `large_file_transfers` (`session`, optionally `direct`), `protocol_versions`,
  `hash_scheme`.
- **Upload sessions** (OCI/tus-shaped, served by Django):
  1. `POST uploads {kind: file, id, size, path}` creates a session. It answers `present`
     instead only when the repository has no path rules or the actor can already read the
     object; otherwise the client must upload (proof of possession).
  2. `PATCH uploads/{upload_id}` with `Upload-Offset` appends a chunk (at least
     `JJ_UPLOAD_CHUNK_MIN_BYTES` except the last) to a **staging key** in `JJ_FILE_STORAGE`;
     out-of-order appends are refused, and `HEAD` returns the offset for resume. An optional
     per-chunk SHA-256 protects transport only.
  3. `POST uploads/{upload_id}:finish` makes the server **read the staged bytes back and
     compute Blake2b-512** (no object store verifies BLAKE2b, and CPython cannot carry a
     BLAKE2b state across requests), compare id and size, then store the object under its
     repository-scoped key and insert the `File` and `ObjectProvenance` rows. For very large
     files it answers `RETRY` while a background verifier runs, and until verification
     completes, items referencing the id get `RETRY` too — never `INVALID(missing)`, which
     would make the client re-send.
  4. `DELETE` cancels; expired sessions are swept.
  A single-request upload may hash while streaming and skip the read-back.
- **Direct-to-storage uploads** (optional, `JJ_DIRECT_UPLOAD_ADAPTER`) presign only unique
  **staging** keys, never the final id-derived key (an S3 `PUT` overwrites, so a presigned
  final-key URL would let a client overwrite verified content), require a transport
  checksum, and still verify at `finish`.
- **Downloads.** Files above `JJ_FILE_INLINE_MAX_BYTES` are served as a `url` when the
  configured storage can sign one — an explicit opt-in per storage alias; unsigned storage
  URLs are never returned. URLs expire after `JJ_FILE_URL_EXPIRE_SECONDS` and are not cached
  by the client beyond one command. **Clients verify** downloaded bytes against the id
  before caching them.

### 12.5 Errors

`{code, message, details}` with the HTTP status. `message` and `details` echo only ids and
paths the requester supplied or may read.

| Code | Status | Retryable | When |
| --- | --- | --- | --- |
| `unauthenticated` | 401 | no | no `Authorization` header, or invalid credentials; the response carries the authenticator's `WWW-Authenticate` challenge |
| `forbidden` | 403 | no | the authorizer denied, or a committer did not match the actor |
| `not_found` | 404 | no | repository absent **or not readable by the actor**; object absent (per item inside batches) |
| `invalid_object` | 422 | after re-sending missing references | strict decoding failed (§ 6.7), non-canonical bytes, bad path, missing referenced objects (listed), parentless commit or operation |
| `invalid_request` | 422 | no | a request that is well-formed but not allowed by the protocol — for example removing heads that are not ancestors of the new head (§ 8.1) |
| `id_mismatch` | 409 | no | the uploaded id is not the value's id, or a re-upload differs from the stored value |
| `version_conflict` | 409 | no — re-read | publication moved since `expected_version` — **publications only**; details carry `current_version` and `current_commit_id` |
| `edit_conflict` | 409 | no — re-read | a server edit's precondition did not hold; details list the paths |
| `requires_merge` | 409 | yes | server edit found divergent heads for the workspace |
| `requires_rebase` | 409 | no — ask the worker | server edit would orphan descendants |
| `requires_worker` | 409 | no — ask the worker | the edit needs jj semantics (conflicted target, or a target shared with refs or other workspaces) |
| `not_derived` | 409 | yes | a derived answer the request needs is not ready and the bounded walk did not settle it |
| `request_in_progress` | 409 | yes | a request with the same `Idempotency-Key` is still running |
| `idempotency_key_reused` | 422 | no | an `Idempotency-Key` was reused for a different request |
| `payload_too_large` | 413 | no — split | over the batch or request limit |
| `unavailable` | 503 | yes (`Retry-After`) | a transient server condition |
| `unsupported_protocol` | 400 | no | client protocol or jj version not accepted |

## 13. Identity and authorization seams

### 13.1 Actor

```python
class Actor(Protocol):
    ref: str                        # stable opaque id recorded in logs ("user:42", "agent:7")
    name: str                       # committer name (§ 6.5)
    email: str                      # preferred committer email, returned to clients (§ 6.5)
    committer_emails: frozenset[str]  # every email accepted as this actor's committer
    is_service: bool                # the jj worker
    user: AbstractBaseUser | None   # the Django user, when there is one (default authorizer)
```

A **service actor** is a host-issued identity for the jj worker. The library gives it
exactly two differences: commits it writes carry its own identity as committer (§ 6.5),
and its worker requests may act for another actor (`on_behalf_of`, § 10.4). What it may do
is still the authorizer's decision — hosts normally grant it everything.

### 13.2 Authenticator

`JJ_AUTHENTICATOR` names a class implementing:

```python
class Authenticator(Protocol):
    def authenticate(self, request: HttpRequest) -> Actor | None: ...
    def challenge(self, request: HttpRequest) -> str: ...
```

`authenticate` returns `None` when the request carries no credentials of its scheme and
raises `Unauthenticated(reason)` when it carries bad ones, so authenticators compose and
denials are logged with reasons. `challenge` returns the `WWW-Authenticate` value that
every 401 response carries (RFC 9110). Authenticators must
authenticate from the `Authorization` header alone and never from the session or
`request.user` (which Django's session middleware fills from cookies) — this is what keeps
the CSRF-exempt protocol safe (§ 12.1). There is no default: `jj.E002` fails until it is
set, and the first protocol request raises `ImproperlyConfigured` (system checks do not
run under WSGI or ASGI). Shipped building blocks:

- `django_jj.auth.BearerTokenAuthenticator` — parses `Authorization: Bearer <token>`, calls
  `actor_for_token(token)`, which hosts implement over their own token store, and
  challenges with `Bearer realm="jj"`;
- `django_jj.testing.StaticTokenAuthenticator` — maps tokens to actors from the
  `JJ_TEST_TOKENS` setting; for tests and local development only (deploy check `jj.W003`).

Issuing and storing real tokens is the host's job.

### 13.3 Authorizer

`JJ_AUTHORIZER` names a class implementing:

```python
class Authorizer(Protocol):
    def can_create_repo(self, actor: Actor, name: str) -> bool | Decision: ...
    def can_read_repo(self, actor: Actor, repo: Repo) -> bool | Decision: ...
    def can_write_repo(self, actor: Actor, repo: Repo) -> bool | Decision: ...
    def can_write_workspace(self, actor: Actor, repo: Repo, workspace: str) -> bool | Decision: ...
    def can_change_refs(self, actor: Actor, repo: Repo, changes: Sequence[RefChange]) -> bool | Decision: ...
    def can_forge_committer(self, actor: Actor, repo: Repo) -> bool | Decision: ...
    def denied_path_prefixes(self, actor: Actor, repo: Repo) -> Sequence[str]: ...
    def can_write_paths(self, actor: Actor, repo: Repo, paths: Sequence[str]) -> bool | Decision: ...
    def can_publish(self, actor: Actor, publication: Publication, request: PublishRequest) -> bool | Decision: ...
```

`Decision(allowed, reason, code=None)` lets a denial say why; per-item `FORBIDDEN` statuses
carry the reason. In v1, path rules are **prefix denials**: a path is readable by an actor
iff none of the actor's `denied_path_prefixes` is an ancestor-or-self of it. The library
computes readability from that one list, once per request, and never calls the authorizer
with paths derived from hidden state. Authorizer instances are cached per process (they
must be thread-safe) and discarded when the `JJ_AUTHORIZER` or `JJ_AUTHENTICATOR` setting
changes (`setting_changed`), so `override_settings` works in host tests.

Shipped:

- `django_jj.authz.DjangoModelPermissionsAuthorizer` (default) — repository-agnostic: it
  checks the **global** model permissions `jj.add_repo`, `jj.view_repo` and
  `jj.change_repo` of `actor.user` (no per-object check, which Django's `ModelBackend`
  would deny for every non-superuser), grants service actors everything, and has no path
  rules. Per-repository or per-path grants need a host authorizer.
- `django_jj.authz.AllowAllAuthorizer` (tests only; deploy check `jj.W002`).

### 13.4 Path rules

Path rules are opt-in per repository (`Repo.path_rules_enabled`). Enabling them on an
existing repository runs `manage.py jj enable-path-rules --repo NAME`, which backfills
`Placement` by walking every stored commit's trees. Without path rules, repository read
access exposes every object and nothing below runs.

**What path rules protect.** The contents of files, symlinks and trees under a denied
prefix, and the names below it. They do **not** protect:

- names and ids of entries **at** the restriction root — the parent tree shows them
  ("level 1"), and ids are pure content hashes, so a low-entropy restricted file under a
  file-level rule can be confirmed offline. Prefer directory-level rules, and give each
  restricted directory a high-entropy salt entry (a file of random bytes), which makes its
  tree id unguessable from then on;
- commit metadata (descriptions, authors), operation metadata (including the command line,
  hostname and username) and view contents (bookmark and workspace names) — all
  repository-wide, because jj's index needs them. Work that must stay confidential belongs
  in a separate repository;
- reads by ref, workspace or publication — those rules govern writes only.

**Placement.** `Placement(path, object_id)` records that an object sits at a path:

- `write_tree(path, tree)` places each entry at `path/name`;
- `write_commit` places each root-tree term at `""`;
- placing a tree at a path where it was not placed before also places its descendants
  (eagerly; cost proportional to the subtree — this covers directory renames, which jj
  performs without rewriting the moved subtree). The walk stops at `(path, object)` pairs
  already placed, whose descendants are placed already. `JJ_MAX_PLACEMENTS_PER_REQUEST` is
  a soft budget against amplification: the item that crosses it completes, and the
  request's remaining items get `RETRY` with a delay — never a refusal, which would wedge a
  stock rename of a large directory.

**Visibility.** A reference to an object from a path `P` — a tree entry at `P`, or a
root-tree term at `""` — is **visible** to an actor if the object is a root, the empty
tree, the empty file or the empty copy id; an earlier item of the same request; **already
placed at `P`** (an unchanged entry, including a restricted one the actor cannot read); a
leaf (file or symlink) the actor wrote (`ObjectProvenance`); or placed at some path the
actor can read. Visibility is evaluated in a constant number of batched queries against
the actor's denied prefixes.

**Reading** — `objects:get`, per request then per item:

1. If the repository is unreadable: `not_found` for the whole request.
2. A file, symlink or tree without a `path`: `INVALID`.
3. **Readability first:** if the path is unreadable, `FORBIDDEN` — computed from actor and
   path alone, with no database access, identical whether or not anything was ever placed
   there, and audited.
4. The empty tree or empty file: `OK`.
5. **Reachability:** `(path, id)` is in `Placement`, or the object is a leaf the actor
   wrote: `OK`; otherwise `NOT_FOUND`.

**Copies.** `read_copy` carries no path, so a copy history is served only if its
`current_path` is readable (otherwise the same `NOT_FOUND` as an absent id);
`copies:related` omits copies at unreadable paths and parent links into them;
`copies:records` drops every record whose source or target is unreadable (Subversion's
rule for copies from unreadable sources).

**Writing** — `objects:put`, per item:

1. `can_write_repo`.
2. A file, symlink or tree whose `path` is unreadable: `FORBIDDEN`, uniformly and before
   any decoding or lookup. (Stock jj never writes at a path it cannot read.)
3. Strict decoding and id verification (§ 6.2), which depend on no state.
4. **References are validated by visibility:** a reference to an object the actor cannot see
   is reported exactly like a missing one (`INVALID`, listed as missing), whether or not
   it exists. Honest clients lose nothing: they re-send what is missing (§ 6.3).
5. **Graft rules.** Every **new** `(P, object)` placement — a tree entry, a root-tree term,
   or a descendant placed eagerly — must satisfy one of:
   1. the object is already placed at `P` (an unchanged entry — the common case, including
      restricted entries the writer cannot read);
   2. the object is a **leaf** written by this actor (`ObjectProvenance`);
   3. the object is placed at some path `Q` the actor can read, and — for a tree — no
      denied prefix lies under `Q` for this actor (the whole source subtree is readable);
   4. the object is the empty tree or the empty file;
   5. the object is a **tree whose bytes this actor uploaded** — in this request, or
      earlier as recorded in `ObjectProvenance` (possession proven by § 6.2) — and whose own
      entries all pass these rules at `P/name` (a directory built from legitimate
      children).

   Otherwise the tree or commit is `FORBIDDEN`. By this step every referenced object is
   visible to the actor, and every rule depends only on what the actor uploaded or can
   already read — never on where else an object is placed — so the answer reveals nothing
   hidden. Rule 5's possession condition is what stops an actor from wrapping a restricted
   tree whose id it saw at level 1 — after uploading candidate leaf contents — and reading
   its names and structure under a permitted path: referencing a tree by id proves
   nothing, and uploading its bytes requires already knowing every entry. A tree a stock
   client builds is always uploaded by that client, so identical directories elsewhere in
   the repository never affect it.

Stock workflows pass: renames and `restore --from` move readable content (rule 3),
unchanged entries keep their placement (rule 1), new files are the actor's leaves
(rule 2), conflicted trees line up by path, and a sparse reader's snapshots never move the
subtrees it excludes. Moving readable content to a path a wider audience can read is
allowed, as in Subversion and Perforce; the server emits a `declassification` audit event
when it happens.

**Server edits** run the path checks on edited paths before anything else (§ 10.1 step 1);
otherwise a server edit would reveal whether a guessed content equals the restricted file
(the resulting tree id would not change).

**Stock jj with restricted paths.** jj expects to read every tree it touches. Readers with
denied prefixes use a **sparse working copy**. jj's sparse patterns are include-only, so
`jj-django` computes the complement of the denied prefixes — at every level on the path to
each denied prefix, every sibling except the denied one — refreshes it from the working
copy's parent tree on every command, and adds new non-denied names a reader creates before
snapshotting (otherwise a new top-level file beside a denied prefix would never be tracked).
Commands that must read restricted content fail for that reader with a clear error: a
diff, `show -p` or `--stat` over others' restricted changes, and **any merge or rebase in
which both sides changed restricted content**, conflicting or not — including the default
`jj log` template's emptiness check on such merge commits, which renders an inline error.
`jj-django` disables jj's changed-path index for restricted readers. This is the
documented cost of path-level read control on a VCS whose clients hash trees.

**Writes to restricted paths** are checked at publish (`can_write_paths` over the changed
paths, § 9.1) and, where a host wants it earlier, through `can_change_refs` at operation
authorization (§ 8.4). Publishing on top of another actor's unpublished changes to paths
the publisher cannot write is refused; publish from an already-published base.

**Rule changes** take effect by path: tightening a rule denies reads at the new prefix at
once, including history; content moved out earlier stays readable where it now is;
loosening a rule makes history readable again. Client caches are never invalidated
(§ 14.2), and signed download URLs live until they expire. `jj-django` refreshes denied
prefixes on each command's first request and narrows its sparse patterns.

**Error details** name at most a restriction root, and `repos/{repo}` returns only the
outermost denied prefixes.

### 13.5 Audit

`OpHeadLog`, `PublicationLog` and `ObjectProvenance` record who changed what. Every read
refused for an unreadable path, every denial, and every declassification are emitted as
structured events on the `django_jj.audit` logger; persisting them is the host's choice.

---

## 14. The client: `jj-django`

### 14.1 Build

A Rust workspace under `client/`, modelled on jj's `cli/examples/custom-backend`:

- `jj-django-store` — the `Backend`, `OpStore` and `OpHeadsStore` implementations; the only
  crate whose store logic touches jj-lib;
- `jj-django` — the binary: `jj_cli::cli_util::CliRunner` with `StoreFactories`
  registering store type **`django-jj`**, plus the commands `clone`, `init`,
  `publications`, `journal`, `worker`;
- `vectors-gen` — the golden-vector generator and differential fuzzer (§ 6.6).

All three depend on the same pinned `jj-lib` / `jj-cli`.

### 14.2 Behaviour

- Computes ids locally, journals every write, uploads leaves eagerly and everything else as
  the closure of a head update (§ 6.3); reports `concurrency()` from the repository info
  (default 64); coalesces concurrent reads into batched requests; splits on the server's
  advertised limits.
- Persists fetched and written objects in an on-disk cache under `.jj/repo/store/`, keyed
  by `(kind, id)` and marked acknowledged or not; objects are immutable, so the cache never
  invalidates.
- Stamps the server-provided committer identity in `write_commit` (§ 6.5), refreshing it
  when the digest on `op-heads` changes.
- Recovers from permanently refused items (§ 6.3); `jj-django journal` lists and discards
  them.
- Uses the stock local `IndexStore` and `WorkingCopy` — and, from the pin that introduces
  it, the stock `WorkspaceStore`.

### 14.3 Cold start

On first load, or when the local index lags (the head operation is not in the index's
operation links), the client **must** complete the `operations:ancestors` stream
(operations and views), then the `commits:ancestors` stream (keyed by the unindexed
operations; including predecessor commits), into the cache before jj-lib builds the index —
jj's index build walks one node at a time, so without prefetch it would make one round trip
per operation and per commit. Cold-start volume grows with the number of operations times
view size, so the streams are compressed as a whole (§ 12.1), and a static bundle and
operation retention (Q6) are the later remedies.

### 14.4 Clone and workspace naming

- `jj-django clone <url>/jj/v1/repos/<name> [dir] [--workspace NAME]` writes the store
  `type` files (including `workspace_store/type` from the pin that introduces it) and
  `django-jj.toml` directly, then adds a workspace whose name is unique in the repository
  (default `<username>@<hostname>`, suffixed if taken) — never `default`, which would make
  every clone fight over one working-copy entry. For actors with denied prefixes it
  configures the sparse complement (§ 13.4).
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
in `CHANGELOG.md`; server and clients upgrade in lockstep when ids change. A weekly CI job
builds `client/` against jj `main` and runs the hash-shape canary (§ 6.6), so trait churn
and new hashed fields surface before a pin move. The pending upstream removal of
`Commit.predecessors` is a known id change.

---

## 15. Git interop (`[git]` extra)

- `manage.py jj import-git PATH --repo NAME` walks a git history with dulwich into canonical
  rows as a service actor (change ids generated, or read from jj's change-id commit header
  when present) and records `GitMapping(repo, commit_id, git_sha)`. Imported commits keep
  their original committers, which requires `can_forge_committer` (service actors only by
  default); `ObjectProvenance` records the importer.
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
| `JJ_DERIVE_ENQUEUE` | `True` | enqueue `django_jj.tasks.derive` on commit (§ 11.3) |
| `JJ_FILE_STORAGE` | `"default"` | `STORAGES` alias for large file bytes |
| `JJ_FILE_INLINE_MAX_BYTES` | `262144` | files up to this size are stored inline in PostgreSQL |
| `JJ_INLINE_UPLOAD_MAX_BYTES` | `4194304` | largest file accepted inside `objects:put`; larger ones use an upload session |
| `JJ_UPLOAD_SESSION_MAX_BYTES` | `5368709120` | largest file accepted at all |
| `JJ_UPLOAD_CHUNK_MIN_BYTES` | `5242880` | minimum upload-session chunk, except the last |
| `JJ_UPLOAD_EXPIRY_SECONDS` | `86400` | upload-session lifetime |
| `JJ_DIRECT_UPLOAD_ADAPTER` | `None` | optional presigned staging uploads (§ 12.4) |
| `JJ_FILE_URL_EXPIRE_SECONDS` | `300` | lifetime of signed download URLs |
| `JJ_MAX_BATCH_ITEMS` | `1000` | per request |
| `JJ_MAX_REQUEST_BYTES` | `67108864` | per request |
| `JJ_MAX_PLACEMENTS_PER_REQUEST` | `100000` | eager descendant placements per request (§ 13.4) |
| `JJ_STREAM_HEARTBEAT_SECONDS` | `15` | heartbeat interval on streams (§ 12.1) |
| `JJ_VERIFY_COMMITTER` | `True` | § 6.5 |
| `JJ_CONCURRENCY_HINT` | `64` | reported to clients |
| `JJ_MERGE_LEASE_SECONDS` | `30` | merge-lease lifetime and failed-resolution back-off (§ 8.3) |
| `JJ_WORKER_ENABLED` | `False` | the server requests worker merges and rebases (§ 8.2, § 10.4) |
| `JJ_WORKER_COMMAND` | `None` | how the library starts the jj worker, if it does (§ 10.4) |
| `JJ_WORKER_TIMEOUT_SECONDS` | `600` | per worker request |
| `JJ_FAST_FORWARD_WALK_LIMIT` | `10000` | commits a publish may walk before answering `not_derived` (§ 9.1) |
| `JJ_IDEMPOTENCY_RETENTION_SECONDS` | `86400` | how long idempotency records are kept (§ 12.1) |
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

No queries, no model instantiation, no seam resolution in `ready()`. `default_auto_field`
stays explicit even though it is Django 6.0's global default, so hosts that set another
default see no spurious `jj` migrations. `django_jj/__init__.py` re-exports the public API
lazily (PEP 562), so a host's settings module can import `django_jj.conf` before Django is
set up.

System checks (static unless noted; system checks do not run under WSGI or ASGI, so every
seam also raises `ImproperlyConfigured` on first use):

| Id | Condition |
| --- | --- |
| `jj.E001` | a database that may receive `jj` migrations is not PostgreSQL (static: no connection needed) |
| `jj.E002` | `JJ_AUTHENTICATOR` unset, or `JJ_AUTHENTICATOR` / `JJ_AUTHORIZER` not importable or not matching its protocol (method presence and arity; types are checked by the host's type checker) |
| `jj.E003` | `JJ_DERIVERS` not importable, duplicate names, or a dependency cycle |
| `jj.E004` | `JJ_FILE_STORAGE` is not a configured `STORAGES` alias |
| `jj.W001` | `JJ_VERIFY_COMMITTER = False` (committers are client-asserted) |
| `jj.W002` (deploy) | `AllowAllAuthorizer` configured |
| `jj.W003` (deploy) | `StaticTokenAuthenticator` configured |
| `jj.W004` (deploy) | `JJ_WORKER_ENABLED = False`: divergence is merged by clients, which are refused if they lack rights over what the merge touches (§ 8.2) |
| `jj.W005` (deploy) | `JJ_DERIVE_ENQUEUE = False`: nothing derives unless the host runs `manage.py jj derive --watch` or calls `run_pending` |

`django.contrib.postgres` missing from `INSTALLED_APPS` fails Django's own `postgres.E005`.

Management command `manage.py jj <subcommand>`: `create-repo`, `list-repos`, `derive`
(`--once`, `--watch`, `--status`), `rebuild` (`--in-place`), `verify` (recompute ids for a
sample or all objects under each row's hash epoch; `--derived` compares a side-by-side
build with the active one; reports objects unreachable from any operation), `resolve`
(`--take`, `--watch`), `enable-path-rules`, `cleanup` (expired upload sessions and
idempotency records), `import-git`, `export-git`; `gc` later.

## 18. Public API surface

Names settle in M2 (Q11); this is the intended shape.

```python
from django_jj import (
    Actor, Authenticator, Authorizer, Decision, PublishRequest, RefChange,
    Deriver, DeriveContext, DeriveBatch,
    edit_workspace, publish, operation,
    Put, PutId, Symlink, Delete, Move, Chmod, ABSENT,
    JjError, Unauthenticated, Forbidden, NotFound, InvalidObject, InvalidRequest,
    IdMismatch, VersionConflict, EditConflict, RequiresMerge, RequiresRebase,
    RequiresWorker, NotDerived, Unavailable,
    app_settings,
)
from django_jj.auth import BearerTokenAuthenticator
from django_jj.authz import DjangoModelPermissionsAuthorizer
from django_jj.models import Repo, Publication, OpHead, OpHeadLog, PublicationLog  # and the rest
from django_jj.signals import op_heads_updated, publication_moved, worker_requested
from django_jj.derive import run_pending, derived_up_to, wait_for_derived
from django_jj.paths import readable
from django_jj.tasks import derive
from django_jj import worker
```

Python exceptions mirror the error codes of § 12.5 that Python callers can meet;
`payload_too_large` and `unsupported_protocol` are protocol-only. Anything under
`django_jj._internal` is private; `django_jj.testing` is for tests only.

## 19. Testing

- **Python:** `pytest` + `pytest-django` against PostgreSQL (required; no SQLite path),
  with `ATOMIC_REQUESTS = True` in the test settings.
- **Golden vectors:** byte-identical ids and encodings, round trips, negative and
  non-canonical vectors, differential fuzzing (§ 6.6).
- **No wedges:** a view with remote bookmarks and a default git head uploads; an identity
  change after commits were journaled does not refuse re-uploads of existing rows; a
  permanently refused item is quarantined and later commands work.
- **Concurrency:** two clients and one server writer on one repository; divergent heads
  appear, the worker resolves them once (lease granted at divergence), and no operation is
  rejected for staleness. A lost worker enqueue is recovered by the next lock attempt or
  by `resolve --watch`; a merge longer than the lease renews it; a forwarded rebase that
  would move a bookmark the requesting user may not move is refused. With the worker
  disabled and two actors, a rewrite racing a snapshot of another actor's workspace shows
  the documented refusal.
- **Log ordering:** a deterministic two-connection test holds one `update_op_heads` open
  after its log insert while another commits; with the per-repository counter the second
  blocks, and both are derived. A deliberately naive sequence-based `seq` kept in the test
  module must fail it. `seq` is dense per repository.
- **Durability:** a client killed mid-flush resumes on its next command without the server
  ever seeing a dangling reference; a refused head update is not resumed;
  `--no-integrate-operation` followed by `jj op integrate`
  from another process works; a 3,000-commit rebase uploads in order; a lost-response
  retry of `op-heads:update` after a concurrent merge leaves exactly one head.
- **Head safety:** `op-heads:update` removing a non-ancestor head is refused; forged merges
  — two-parent and three-parent, including a parent that is a fresh child of an old
  operation — cannot roll a protected ref back; `manage.py jj resolve --take` recovers a
  wedged log.
- **Publications:** a retried publish whose first attempt landed succeeds; a conflict
  reports the current state; edit → publish → edit → publish with
  `require_fast_forward` succeeds; publish right after an edit succeeds via the bounded
  walk; a recreated publication does not reuse versions.
- **Server edits:** per-edit preconditions refuse or record a conflict; no-op edits write
  nothing; moves keep subtree ids; idempotency keys replay; server-written commits equal
  what jj writes for the same edit with a fixed timestamp.
- **Oracle:** a Hypothesis state machine (a fresh `Repo` per example, real commits in a
  transactional test case) with rules for writes, forks, merges, publishes, derivation in
  random batch sizes, crash injection mid-batch, rebuilds and version bumps; the check
  builds a fresh generation side by side and compares it with the active one. Random
  racing writers on separate connections. A deliberately broken deriver must be caught.
- **Client e2e:** stock workflows (`new`, `describe`, `squash`, `rebase`, `duplicate`,
  `abandon`, `undo`, `op revert`, `op restore`, `op abandon` (refused), `op log`, `op log -p`
  (uploads nothing), `evolog`, conflicts, multiple workspaces, two clones with distinct
  workspace names) against a live test server; placeholder copy ids accepted; every
  mutating command under `--config user.email=…`, `JJ_EMAIL=…` and scoped overrides yields
  the server identity; `jj diff` over 1,000 files issues batched reads only.
- **Protocol:** a 10 MB `objects:put` succeeds with Django's default
  `DATA_UPLOAD_MAX_MEMORY_SIZE`; a stream cut before its terminal frame is rejected by the
  client; streams heartbeat; batch responses are HTTP 200 with per-item failures; no
  per-object queries in protocol views; concurrent overlapping `objects:put` in opposite
  orders do not deadlock; a tampered staged upload fails at `finish`; a presigned staging
  URL cannot write a final key.
- **Path rules / leak tests:** a restricted reader's snapshot beside a denied prefix
  uploads (the unchanged restricted entry is visible at its own path); a reader's new
  directory whose tree is byte-identical to one that exists only under a denied prefix is
  accepted; for an unreadable path, `objects:get` answers identically
  for an id placed there, an id placed elsewhere, a nonexistent id and the empty file; a
  tree upload referencing a nonexistent id and one referencing an id placed only under a
  denied prefix get identical responses; a tree upload at an unreadable path is refused
  regardless of its entries; a server edit at an unreadable path is refused whether or not
  the file exists; an existing restricted id re-uploaded with wrong bytes is `ID_MISMATCH`
  and records no placement; a restricted object is unreachable through a permitted path,
  a guessed id, a copy record, a **grafted tree**, a **wrapped graft**, or the **salary
  wrap** (an actor uploads every candidate leaf, then wraps the restricted tree under a
  readable path); a copy chain through a restricted path does not leak it; built-in derived
  reads filter restricted rows; an unreadable repository answers 404. A restricted reader
  with sparse patterns can run log (including merges where both parents changed restricted
  content), `describe -m`, `new`, `st`, and publish from a published base; a new top-level
  file is tracked; a sibling added by another actor appears after the next command.
- **Seams:** the default authorizer grants a non-superuser with `jj.view_repo` and denies
  one without; `override_settings` of the seams takes effect; `django_jj.conf` imports
  before `django.setup()`; `edit_workspace` and `publish` refuse to run inside
  `transaction.atomic()`.
- **Rust:** `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test`; weekly build
  against jj `main` with the hash-shape canary.

## 20. Security considerations

- Every uploaded object is strictly decoded with the project's own wire parser, required
  to be canonical, and verified against its id (§ 6.2), so content cannot be planted under
  another object's id, the server never stores a value that jj-lib would read differently
  or panic on, and re-uploads prove possession.
- Entry names `.`, `..` and names containing NUL are rejected — a server policy stricter
  than jj, whose path components accept them.
- Protocol endpoints authenticate from the `Authorization` header only; sessions and
  `request.user` are never consulted (§ 12.1, § 13.2).
- Op-head removals are limited to ancestors of the new head (§ 8.1); merges cannot smuggle
  ref changes past authorization (§ 8.4).
- Repository paths are validated as jj `RepoPath`s (UTF-8, `/`-separated, no empty
  components) plus the policy above.
- References to missing objects are rejected on write (§ 6.2); with path rules, references
  to invisible objects are indistinguishable from missing ones.
- Responses about unreadable paths and repositories never depend on hidden state: no
  existence oracles through status codes, reference errors, uploads at unreadable paths,
  server edits, or a "find missing" endpoint (§ 12.2, § 13.4). Timing is kept independent
  of hidden state where practical (readability before any lookup; batched visibility).
  `POST repos` with a taken name necessarily reveals that the name exists; hosts namespace
  repository names or restrict creation.
- Batch and request limits bound memory; streamed responses bound latency.
- Committers are server-verified (§ 6.5); authors, committer timestamps, signatures and
  operation metadata are client-asserted.
- Without path rules, repository read access exposes every object in the repository. With
  them, metadata and level-1 ids remain repository-wide (§ 13.4).
- Signed download URLs are bearer capabilities that outlive a rule change until they
  expire; keep expiry short. Direct uploads never presign final keys (§ 12.4).
- Git export discards path restrictions (§ 15).

## 21. Performance notes

- Writes are pipelined: ids are local, leaves upload during the command, and the rest of a
  command's objects go up as one ordered request — two round trips at `update_op_heads`, or
  one with the trailer (§ 6.3), instead of one per dependency level. The server's cost is
  decode plus hash per object.
- Reference validation is batched per kind (`= ANY`); a rewritten very wide directory still
  costs an existence check per entry, which a benchmark tracks.
- Small objects stay in `bytea` with `lz4` compression; tree and commit data dominated by
  64-byte ids compresses poorly. Large file bytes go to storage. There is no cross-version
  delta compression in v1 — history-heavy large files cost roughly full size per version.
- Bytes travel in binary protocol (§ 7.5); hex encoding would double request-sized SQL.
- Trees are opaque rows; row volume grows with changes, not with tree size × versions.
  `Placement` (only with path rules) grows with distinct (path, object) pairs;
  `ObjectProvenance` roughly doubles id-keyed index volume.
- Op-head updates serialize per repository only for a few short statements.
- Cold start streams the operation log and commit ancestry (§ 14.3); its volume grows with
  operations × view size. Per-object fetch is the documented failure mode of
  database-backed VCS stores.

---

## 22. Open questions

| Id | Question | Current default |
| --- | --- | --- |
| Q1 | Where jj semantics run server-side | Python for strict decoding, ContentHash, canonical encoding (including signed payloads), deprecated-field projections and `for_rewrite_from` author rules; everything that runs a jj algorithm runs in the out-of-process worker (§ 10.4). jj-lib never runs in the Django process. Fallback if parity fails: a small binding exposing only jj-lib's decoders and hashing |
| Q2 | Who computes ids | the writer, with jj's hashing; the server verifies (§ 6.2), with a strict parser, canonical-bytes checks, committer checks on insert only, and per-row hash epochs. Alternative: server-minted ids (no parity needed, but one round trip per written tree and commit, as peers that chose it pay) |
| Q3 | Who resolves divergent heads | the worker, started by the write that creates divergence, under a lease granted in the same transaction and renewed while it merges; lock attempts and `resolve --watch` re-request lost merges; clients wait in `lock()` and re-read, and merge themselves only as a fallback (§ 8.2). Draft 2's resolve-on-read wait is dropped |
| Q4 | Wire format | **settled (draft 3):** protobuf over HTTP/1.1 POST in plain Django views, with Connect-style stream envelopes and a terminal frame, zstd, and the resource paths of § 12.2. No gRPC (needs HTTP/2 trailers Django does not emit), no connect-python dependency |
| Q5 | Large-file upload handshake | **settled (draft 3):** upload sessions served by Django with read-back verification at `finish`, optional presigned staging uploads (§ 12.4) |
| Q6 | Garbage collection and operation retention | not in v1; `OpStore::gc` unsupported; publications are GC roots; `verify` reports unreachable objects. A future GC needs a grace window and a writer/GC shared/exclusive lock, never a "touch" on canonical rows; retention must not rewrite operation ids. One operation per jj command makes it necessary before heavy use |
| Q7 | Hiding names under restricted paths (filtered trees) | out of the core store permanently: filtered trees need projected ids, which invariant 3 forbids. Use level 1 plus salt entries, directory-level rules and separate repositories; a name-hiding projection is a host-level derived repository |
| Q8 | Retroactive redaction of content by id | later: Mononoke-style — tombstone the bytes and keep ids; a `REDACTED` status; a list clients poll to purge caches; a client-side placeholder. Covers file and symlink contents only, and needs an invariant exception ("GC and redaction may erase bytes") |
| Q9 | jj's `jj-core` split and trait changes after 0.45.1 | tracked per pin move: the next pin is id-neutral and needs mechanical edits (`Backend::gc` removed, `RefTarget` alias, `WorkspaceStore`); weekly build against jj `main` and the hash-shape canary |
| Q10 | Converging with an upstream jj-native remote protocol, if one appears | watch jj's commit-cloud proof of concept and ERSC; none exists upstream |
| Q11 | Final public API names | settled in M2; "Publication" stays, with the glossary note of § 9 |
| Q12 | Worker invocation interface | settled in M4: out of process, `on_behalf_of`, timeouts (§ 10.4); the transport is open |
| Q13 | Read-only overlay details | settled in M3 (§ 8.5 gives the head formula) |
| Q14 | Canonical primary keys | `CompositePrimaryKey(repo, object_id)` (§ 7.1); settle before the first migration in M1, because Django cannot migrate between key shapes later. Alternative: a surrogate key plus a unique natural key, which keeps Django admin support |

---

## 23. Design lineage

- **Jujutsu** — store traits, the lock-free operation log and its concurrency model
  (`docs/technical/concurrency.md`), the custom-backend example. Its roadmap describes
  Google's database-backed jj server and a caching daemon that "needs to know the server's
  hashing scheme so it can return the right IDs" — the model § 6.2 follows.
- **Tandem** and jj's **commit-cloud proof of concept** — independent remote jj stores that
  also never drop a head on concurrent updates; Tandem's move of reconciliation off the
  read path informed § 8.2. Maintainers place protected branches on the server — the role
  of publications.
- **Fossil** — a few canonical tables, everything else rebuildable (`fossil rebuild`);
  per-check-in file changes (`mlink`) rather than exploded trees.
- **Mononoke / Sapling** — minimal canonical objects plus asynchronously derived data built
  side by side; bookmark moves as compare-and-swap with an update log; readers gated on
  derived data ("warm bookmarks"); redaction; restricted paths.
- **Software Heritage** — object metadata in PostgreSQL, bytes elsewhere; surrogate keys
  behind intrinsic hashes; the cost of normalizing trees.
- **gitgres** — git objects in PostgreSQL; the measured cost of storing every version whole.
- **lakeFS, Dolt** — ref compare-and-swap over a database; batch transfer with presigned
  URLs; Dolt's upload-then-commit in one call.
- **Bazel Remote Execution API, OCI distribution, Git LFS, tus** — batch content-addressed
  transfer with per-item status, advertised limits, blobs before manifests, resumable
  chunked uploads verified at commit.
- **Connect** — protobuf over plain HTTP with enveloped streams and an end-of-stream frame.
- **Gerrit** — committer verification by email; server-written merges; change edits as
  server-side workspaces; "forge committer" for mirrors. **GitHub, GitLab** — preconditions
  on server-side commits; 404 for private repositories.
- **Pulp** — a Django/PostgreSQL content-addressed store: per-tenant scoping, sorted bulk
  inserts, orphan-cleanup races, the cost of connection-bound locks.
- **Cosmos SDK ADR-020/027, IPLD codec fixtures, git `fsck`** — rejecting encodings a
  decoder would read differently; positive, negative and non-canonical fixtures.
- **Dropbox "Dropship", Harnik et al., Halevi et al.** — deduplication side channels and
  proofs of possession.
- **The Python `eventsourcing` library, Marten, Dudycz** — commit-ordered log positions
  and the gaps a plain sequence leaves.
- **Perforce, Subversion, Piper, TFVC** — path-level read permissions are possible only when
  a server mediates every file read; Subversion requires read access to a copy's source.
  TFVC, Plastic, Monotone and Subversion's Berkeley DB years show where database-backed VCSs
  hit limits: per-client file state, per-path authorization cost, per-object round trips.

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
| closure | the journaled, unacknowledged objects a new operation reaches; uploaded as one ordered request |
| publication | a server-owned, versioned pointer to a commit; the policy CAS |
| workspace | a named working copy recorded in the view (`wc_commit_ids`) |
| placement | the record that an object sits at a path (path rules only) |
| visible | placed where the actor can read, the actor's own leaf, or a root/empty object (path rules only) |
| deriver | code that projects canonical data into derived tables |
| derivation domain | what derivers cover: everything reachable from logged operations, plus publication targets |
| watermark | the log position a deriver's build has processed |
| hash epoch | the hash scheme a canonical row was verified under |
| lease | the merge lease: a database row that keeps two processes from merging the same divergence |
| actor | the authenticated party behind a request; service actors run jj semantics |
| host | the Django project that installs `django-jj` and supplies its seams |

---

## 25. Changes in draft 3

Draft 3 folds in the findings of a prior-art and jj 0.45.1 source review of every draft-2
mechanism. Each change, and where it landed:

| # | Change | Sections |
| --- | --- | --- |
| 1 | Accept and constrain the deprecated fields the writer still emits; reject every panic, alias and ignored-field input; `.`, `..` and NUL as policy | § 5 (11), § 6.7, § 20 |
| 2 | Canonical-bytes checks for trees, commits and copies, and for views and operations up to map order; a project-owned strict wire parser | § 6.2 |
| 3 | A prost-exact `Commit` encoder for signed payloads | § 6.1, § 6.2, § 6.5 |
| 4 | Vector format (valid, invalid, non-canonical, with `jj_behaviour`), upstream seeds, differential fuzzing | § 6.6 |
| 5 | Closure uploads in topological order, leaves first, optional op-heads trailer; `size` on items; never upload empty objects | § 6.3, § 6.1, § 12.2 |
| 6 | Connect-style envelopes with a terminal frame, heartbeats, HTTP 200 for batches, structured missing lists, `RETRY` delays; bodies read as streams | § 12.1 |
| 7 | Committer stamped in the client's `write_commit`; verified on insert only, by email, against a set; recovery path for refused journal items | § 6.3, § 6.5, § 13.1, § 14.2 |
| 8 | `lock()` a no-op unless heads were divergent; a non-blocking try-lock; database clock; ownership by token | § 8.3 |
| 9 | Per-repository, dense, commit-ordered `seq` from `LogCounter`; internal retries; no stale head re-insert; bounded ancestor walks with `generation` | § 7.1, § 7.3, § 8.1, § 9.1 |
| 10 | Schema decisions before the first migration: composite keys, millisecond-plus-offset times, `contrib.postgres`, sorted `ON CONFLICT … RETURNING`, binary byte path, keyset streaming, `lz4` | § 7, § 17, § 22 Q14 |
| 11 | `commits:ancestors` keyed by operations; `known` as an ignorable hint; resumable parents-first streams; client read coalescing | § 12.2, § 12.3, § 14.3 |
| 12 | Default authorizer made workable; `WWW-Authenticate`; `None` versus raise; `Decision.code`; seams reset on settings changes; first-use errors; lazy re-exports | § 13.1–13.3, § 17, § 18 |
| 13 | Upload sessions with read-back verification; staging keys; repository-scoped storage keys; short URL expiry; clients verify downloads | § 7.1, § 12.4, § 16, § 20 |
| 14 | Conservative merge authorization, absent as a value, workspace entries including `git_heads`, authorization at `op-heads:update` | § 8.4, § 13.3, § 19 |
| 15 | Lease granted at divergence and worker enqueued on commit; `?resolve=1` and `JJ_RESOLVE_TIMEOUT_SECONDS` removed; server edits check divergence before locking | § 8.2, § 10.1, § 16 |
| 16 | §8.6 corrected for `undo`, `op restore`, `op abandon`, `op integrate` and inspection commands; `resolve --take`; operation metadata unverified | § 8.6, § 17 |
| 17 | Idempotent publish; conflict details; bounded fast-forward walk; publication targets as derivation roots; versions never reset. Deliberate deviation: targets need not be reachable from a view | § 9.1, § 12.5 |
| 18 | Published commits immutable to edits; preconditions; move, chmod, by-id and symlink edits; no-op detection; message versus operation description; author rules; idempotency keys | § 10.1, § 12.1, § 12.2 |
| 19 | Worker requests carry `on_behalf_of`, passed with the worker's `op-heads:update` and authorized there; out of process; timeouts; `JJ_WORKER_ENABLED`, backstops and lease renewal | § 7.3, § 8.1–8.3, § 10.4, § 13.1 |
| 20 | `DeriverState` row locks per build; side-by-side rebuilds; the derivation domain; a shipped task and `derive --watch`; robust callbacks; returned `seq`; invariant 7 reworded | § 5 (7), § 7.4, § 11 |
| 21 | Readability before reachability; references validated by visibility; rule 5 requires possession of the tree's bytes; uploads at unreadable paths refused; path checks first in edits; prefix-only rules; `path` required; placement walks stop at placed pairs, with a soft budget | § 12.2, § 13.3, § 13.4, § 19 |
| 22 | Copy-history filtering and the copy-path rule; what path rules do not protect; derived-row filtering; rule changes | § 6.7, § 11.4, § 13.4, § 20 |
| 23 | Sparse complement patterns refreshed each command; "both sides changed" wording; publishing over others' restricted changes | § 13.4, § 14.4, § 19 |
| 24 | Forge-committer capability for imports | § 6.5, § 13.3, § 15 |
| 25 | `WorkspaceStore`; weekly build against jj `main`; hash-shape canary; hash epochs | § 3, § 6.6, § 7.1, § 14.4, § 14.7 |
| 26 | Lineage and positioning | § 1, § 23 |

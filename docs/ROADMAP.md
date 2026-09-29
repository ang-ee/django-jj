# ROADMAP

Milestones in build order. A milestone starts only when the previous gate passes.
Section references are to [SPEC.md](./SPEC.md).

## M0 — Scaffold

- [x] Repository, license, agent instructions (`AGENTS.md` / `CLAUDE.md`).
- [x] Specification draft 3 (`docs/SPEC.md`; § 25 lists what the prior-art review changed).
- [x] Package skeleton: `django_jj` app (label `jj`), `jj.E001` PostgreSQL check,
      PostgreSQL test settings, `make check`, CI.
- [ ] Open questions Q1–Q3 and Q14 confirmed or revised (§ 22); Q4 and Q5 are settled in
      draft 3.

## M1 — Spike (go/no-go, about two weeks)

Prove the risky parts end to end before building properly.

- Protocol messages in `proto/`: request and response messages, Connect-style stream
  envelopes with a terminal frame, per-item status, `django_jj.v1.CopyHistory` (§ 12.1).
- Strict wire parser with canonical-bytes checks and the § 6.7 rules; `ContentHash` port and
  prost-exact `Commit` encoder for the pinned jj; `vectors-gen` in Rust producing valid,
  invalid and non-canonical vectors plus a differential fuzzer (§ 6.2, § 6.6).
- Minimal canonical tables with id verification, with the primary-key shape settled first
  (Q14); the per-repository `LogCounter`; the binary byte path and sorted
  `ON CONFLICT … RETURNING` inserts (§ 7); `objects:get` / `objects:put` (closed-set
  validation, optional op-heads trailer), `op-heads`, `repos/{repo}`; a test-only token
  authenticator.
- `jj-django` with the three remote stores (local ids, journaled closure uploads, committer
  stamping in `write_commit`, a `lock()` that is a no-op unless heads were divergent, read
  coalescing, placeholder copy ids), from jj's `cli/examples/custom-backend`; `clone` with
  unique workspace names; recovery from permanently refused journal items (§ 6.3, § 14).
- Two clones and one server-side write on one repository, concurrently.
- A client killed mid-flush, and `--no-integrate-operation` + `jj op integrate`.
- Cold `jj log` on a synthetic repository of 10,000 commits and a long operation log, with
  and without `operations:ancestors` / `commits:ancestors` (keyed by operations) prefetch.
- One publication compare-and-swap, plus a toy deriver checked against rebuild.

**Gate:** Python ids and encodings byte-identical to jj-lib on every vector (including
conflicted commits with and without labels, signed and labelled commits, and complex views
with the deprecated projections); round trips exact; every § 6.7 row and wire-level
negative rejected, including every input jj would panic on; the differential fuzzer clean
on its CI seed range; no client wedged by stock writer output (remote bookmarks, a default
git head, populated predecessors) or by a committer identity change; zero operations
rejected for staleness; the two-connection log-ordering test passes; no dangling references
after an interrupted flush; stock `jj log` and `jj op log` load server-written operations;
a snapshot costs at most two round trips at `update_op_heads`; cold start measured and
acceptable; incremental projection equals rebuild. Results recorded as a proposal or spec
update.

## M2 — Library core

- Full canonical tier, write-time indexes and store tier (§ 7.1–7.3); verification of
  every upload, proof of possession on re-uploads, and reference validation (§ 6.2).
- Protocol v1: framing, heartbeats, capabilities, batching limits, per-item status,
  header-only authentication with `WWW-Authenticate` challenges, idempotency keys, errors
  (§ 12, § 13.2).
- `operations:ancestors` / `commits:ancestors` streaming with resume; large-file upload
  sessions with read-back verification, signed downloads (§ 12.4).
- `JJ_AUTHENTICATOR` / `JJ_AUTHORIZER` with defaults, seams reset on settings changes,
  first-use errors (§ 13); system checks (§ 17); admin; migrations.

**Gate:** suite green on PostgreSQL with no permission engine or consumer framework
installed; no per-object queries in protocol views; a 10 MB `objects:put` under Django's
default request limits; truncated streams rejected by the client.

## M3 — Client v1

- Persistent object cache, closure upload pipeline, `concurrency()`, cold-start prefetch
  (§ 14).
- `init`, `clone`, `publications`, `journal`, credentials, error mapping, compatibility
  handshake.
- Read-only overlay mode (§ 8.5).
- Release builds for macOS and Linux.

**Gate:** stock jj workflows e2e against a live test server (§ 19), including every
mutating command under overridden identity configuration.

## M4 — Server writes and publications

- `edit_workspace` (edit kinds, preconditions, no-op detection, publication targets treated
  as immutable), `publish` (idempotent, bounded fast-forward walk), provisional
  `operation()` (§ 9–10); committer verification and the forge-committer capability (§ 6.5).
- Conservative authorization of what operations change, at `op-heads:update` (§ 8.4);
  ancestor-only head removal (§ 8.1); the try-lock lease granted at divergence and renewed
  by the worker, with lock-time and `resolve --watch` backstops (§ 8.2–8.3);
  `manage.py jj resolve --take` (§ 8.6).
- `jj-django worker` for divergent-head merges and rebases, with `on_behalf_of` and
  timeouts (§ 10.4).

**Gate:** concurrency suite — no lost operations and none rejected for staleness;
divergence resolved once by the worker; forged merges (two- and three-parent) and
non-ancestor head removals refused; publish refuses stale versions and, when configured,
conflicted or non-fast-forward targets, and succeeds on a retry whose first attempt landed;
edit → publish → edit → publish fast-forwards; `undo`, `op revert` and `op restore` behave
as § 8.6 describes.

## M5 — Derived data

- Deriver protocol, the derivation domain, `run_pending` with `DeriverState` row locks,
  watermarks, side-by-side `rebuild`, `verify --derived`, the shipped `derive` task and
  `derive --watch` (§ 11).
- Built-ins: commit parents, commit graph, changed paths, copy edges, view refs,
  conflicts (§ 7.4); `derived_up_to`, `wait_for_derived`, the path-filtering read helper.

**Gate:** the stateful oracle — incremental equals rebuild after randomized histories with
racing writers, random batch boundaries, crash injection and version bumps; a deliberately
broken deriver is caught.

## M6 — Path permissions

- `Placement`, visibility, the graft rules (with rule 5's possession condition),
  readability before reachability, uniform refusals at unreadable paths, copy filtering,
  `enable-path-rules` backfill, sparse complement patterns on clone and on every command,
  publish-time `can_write_paths`, audit events including declassification (§ 13.4–13.5).

**Gate:** leak tests — restricted content unreachable via a permitted path, a guessed id, a
copy record, a grafted tree, a wrapped graft or the salary wrap; identical responses for
placed, unplaced and nonexistent ids at unreadable paths and for invisible versus missing
references; a restricted reader can work in a sparse checkout — snapshots beside denied
prefixes, new top-level files, and `jj log` over merges where both parents changed
restricted content (rendered with inline errors; such merges and rebases themselves fail
for that reader, § 13.4).

## M7 — Git interop (`[git]` extra)

- `import-git` (original committers kept through the forge-committer capability),
  `export-git`, `GitMapping`; refuse export with path rules (§ 15).

## Later

- Garbage collection and operation retention (Q6).
- Redaction by content id (Q8). Hiding names stays out of the core store (Q7).
- Cold-start bundles; copy-tracking working copy; jj pin moves (Q9), starting with the
  `WorkspaceStore` type file before jj 0.51.

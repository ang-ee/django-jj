# ROADMAP

Milestones in build order. A milestone starts only when the previous gate passes.
Section references are to [SPEC.md](./SPEC.md).

## M0 — Scaffold

- [x] Repository, license, agent instructions (`AGENTS.md` / `CLAUDE.md`).
- [x] Specification draft (`docs/SPEC.md`).
- [x] Package skeleton: `django_jj` app (label `jj`), `jj.E001` PostgreSQL check,
      PostgreSQL test settings, `make check`, CI.
- [ ] Open questions Q1–Q4 confirmed or revised (§ 22).

## M1 — Spike (go/no-go, about two weeks)

Prove the risky parts end to end before building properly.

- Protocol messages in `proto/` (envelopes, per-item status, `django_jj.v1.CopyHistory`).
- Strict Python decoder and `ContentHash` port for the pinned jj; `vectors-gen` in Rust
  producing golden, round-trip and negative vectors (§ 6.6).
- Minimal canonical tables with id verification; `objects:get` / `objects:put`,
  `op-heads`, `repos/{repo}`; a test-only token authenticator.
- `jj-django` with the three remote stores (local ids, journaled uploads by dependency
  level, placeholder copy ids), from jj's `cli/examples/custom-backend`; `clone` with
  unique workspace names and the server-provided committer identity.
- Two clones and one server-side write on one repository, concurrently.
- A client killed mid-flush, and `--no-integrate-operation` + `jj op integrate`.
- Cold `jj log` on a synthetic repository of 10,000 commits and a long operation log, with
  and without `operations:ancestors` / `commits:ancestors` prefetch.
- One publication compare-and-swap, plus a toy deriver checked against rebuild.

**Gate:** Python ids byte-identical to jj-lib on every vector (including conflicted
commits with and without labels, signed commits and complex views), round trips exact,
every negative vector rejected; zero operations rejected for staleness; no dangling
references after an interrupted flush; stock `jj log` and `jj op log` load
server-written operations; cold start measured and acceptable; incremental projection
equals rebuild. Results recorded as a proposal or spec update.

## M2 — Library core

- Full canonical tier, write-time indexes and store tier (§ 7.1–7.3); id verification on
  every upload and reference validation (§ 6.2–6.4).
- Protocol v1 framing, batching limits, per-item status, header-only authentication,
  errors (§ 12).
- `operations:ancestors` / `commits:ancestors` streaming; large-file storage and upload
  handshake (Q5).
- `JJ_AUTHENTICATOR` / `JJ_AUTHORIZER` with defaults (§ 13); system checks (§ 17);
  admin; migrations.

**Gate:** suite green on PostgreSQL with no permission engine or consumer framework
installed; no per-object queries in protocol views.

## M3 — Client v1

- Persistent object cache, upload pipeline, `concurrency()`, cold-start prefetch (§ 14).
- `init`, `clone`, `publications`, credentials, error mapping, compatibility handshake.
- Read-only overlay mode (§ 8.5).
- Release builds for macOS and Linux.

**Gate:** stock jj workflows e2e against a live test server (§ 19).

## M4 — Server writes and publications

- `edit_workspace`, `publish`, provisional `operation()` (§ 9–10); committer verification
  (§ 6.5).
- Ref and workspace authorization at operation write, with the merge-base rule (§ 8.4);
  ancestor-only head removal (§ 8.1); the merge lease (§ 8.3).
- `jj-django worker` for divergent-head merges (resolution on `op-heads`, § 8.2) and
  rebases (§ 10.4).

**Gate:** concurrency suite — no lost operations and none rejected for staleness;
divergence resolved once; forged merges and non-ancestor head removals refused; publish
refuses stale versions and, when configured, conflicted or non-fast-forward targets;
`undo` / `op restore` behave as § 8.6 describes.

## M5 — Derived data

- Deriver protocol, `run_pending`, watermarks, `rebuild`, `verify` (§ 11).
- Built-ins: commit parents, commit graph, changed paths, copy edges, view refs,
  conflicts (§ 7.4).

**Gate:** incremental equals rebuild after randomized histories.

## M6 — Path permissions

- `Placement`, the graft check, reachability and readability on reads,
  `enable-path-rules` backfill, sparse patterns on clone, publish-time
  `can_write_paths`, audit events (§ 13.4–13.5).

**Gate:** leak tests — restricted content unreachable via a permitted path, a guessed id,
a copy record, or a grafted tree; a restricted reader can work in a sparse checkout.

## M7 — Git interop (`[git]` extra)

- `import-git`, `export-git`, `GitMapping`; refuse export with path rules (§ 15).

## Later

- Garbage collection and operation retention (Q6).
- Hiding names under restricted paths; redaction by content id (Q7, Q8).
- Cold-start bundles; copy-tracking working copy; jj pin moves (Q9).

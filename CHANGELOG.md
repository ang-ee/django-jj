# Changelog

All notable changes to this project are recorded here. Pre-1.0 releases may break
compatibility in any minor version; breaking changes are listed explicitly.

## Unreleased

- Repository scaffold: specification (`docs/SPEC.md`, draft 2 after two review passes
  against the jj 0.45.1 source), roadmap, agent instructions, package skeleton
  (`django_jj`, app label `jj`), settings defaults, the `jj.E001` PostgreSQL system
  check, PostgreSQL test settings and CI.
- Specification draft 3, from a prior-art and jj 0.45.1 source review of every draft-2
  mechanism (SPEC § 25 maps the 26 changes). Highlights: strict decoding that constrains,
  instead of rejecting, the deprecated fields jj's writer still emits; canonical-bytes
  checks and proof of possession on re-uploads; closure uploads in topological order;
  Connect-style stream envelopes with a terminal frame; committer stamping in the client's
  `write_commit`; a `lock()` that only engages on divergence; per-repository,
  commit-ordered log sequence numbers; conservative merge authorization at
  `op-heads:update`, with divergence merged by the worker from the write that creates it;
  existence-oracle fixes and a narrower graft rule 5 for path rules; side-by-side derived
  rebuilds; upload sessions for large files. Settles Q4 and Q5; adds Q14 (primary keys).
  Removes `JJ_RESOLVE_TIMEOUT_SECONDS` and `?resolve=1`; adds the settings listed in
  SPEC § 16. Agent instructions, roadmap, README and settings defaults follow.

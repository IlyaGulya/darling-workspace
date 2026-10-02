# ADR: retire the patch-stack packaging layer; the workspace manifest commit is the product

Status: accepted (2026-10-02). Supersedes the patch/profile/lock design recorded in
`docs/patch-stack-*.md`.

## Context

The packaging layer (`patches/`, `locks/patch-stack/`, profile compositions, the
materializer and the patch RED->GREEN automaton) existed for one reason: making it
convenient to contribute the fork's work to upstream **piece by piece**, with a
replayable series and patch-archive provenance.

Measured cost and use, read from this repository:

- 135 patch entries across three profiles; 102 `.patch` files; ~147 files under
  `locks/patch-stack/`; ~7,000 lines of patch/profile/materializer code in
  `west_commands/`.
- **Zero upstream PRs were ever opened** from this machinery.
- Repairing the machinery consumed multi-hour sessions: stale `expected_tree` values,
  mirrors that answer only one of two transfer shapes, series whose entries come from
  different lineages, and a materializer that left dead worktrees and `core.worktree`
  pointers behind (158 module gitdirs plus 12 prunable worktrees were repaired by hand
  in one checkout).

The cost of maintaining synthetic multi-repository lineges exceeded the value of the
contribution packaging it produced.

## Decision

**A commit of `darling-workspace` plus the resolved West manifest at that commit is the
reproducible Darling product state.** The manifest pins every managed component to an
exact commit SHA. Branch names are development conveniences and are never product
revision identifiers.

Authorities:

| layer | authority |
| --- | --- |
| source | component Git commits + the workspace manifest commit (exact SHAs) |
| workspace | `darling-workspace` `main`; its commit is the product identifier |
| deployed | deployment receipt: workspace commit, component SHAs, artifact SHA256s |
| behavioral | CTest/testkit fixtures, host contracts, `dwdiag` verdicts |
| planning/evidence | Beads |

Cross-repository changes are ordinary topic commits in the component repositories,
identified as one change set by a workspace topic branch (`change/<bead>-<slug>`) whose
manifest points at the candidate SHAs. Integration is the manifest commit. No synthetic
Git ancestry is created between unrelated component changes, and changes are not exported
as patches in order to be composed.

Reproducibility is a property of the manifest commit, not of a branch tip: `west update`
at a workspace commit reproduces the tree, and `west manifest --freeze` output is
**derived evidence**, never a second source of truth.

Daily development branches from the current accepted state; upstreamability is an
explicit later operation (extract/rebase the relevant logical commits onto `upstream/main`
when a contribution becomes real), never a permanent tax on local work.

## Consequences

- The packaging layer is **frozen read-only** and archived at tag
  `archive/patch-stack-final`. It is deleted in staged normal commits
  (default-command invocations, then patch-specific test registration, then
  materializer/export/preflight, then locks and archives, then obsolete contracts and
  docs), each stage verified by the host/tool smoke tests. No history rewriting.
- The behavioral tests carried only by patch metadata are enumerated in
  `docs/test-ownership-census.md` and migrated into the CTest/testkit authority before
  their metadata is removed.
- Provenance tooling (`dwdiag source --provenance`) validates the new model: workspace
  commit, component SHAs, dirty state, artifact and deployed hashes.
- Build-environment reproducibility is a separate layer and is not owned by source
  orchestration.

## Open item

`darlingserver` has three different commits in play - the manifest pin `89751e64`, the
top-level `darling` gitlink `a693e31a`, and the live working checkout `6ad1bb27` (176
commits one way, 40 the other). The manifest deliberately keeps `89751e64` rather than
guessing; the product owner must name the accepted commit before this pin is accepted.

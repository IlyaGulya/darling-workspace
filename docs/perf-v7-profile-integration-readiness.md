# Perf v7 profile-integration readiness

This review-only package replaces the six divergent perf boundaries with
profile-integration locks. It did not change a workspace commit or profile
archive; immutable hosted publication is recorded separately.

The deterministic rewrite rule is command-scoped Git identity `West Test
<west-test@example.invalid>` and `--committer-date-is-author-date`.  Original
author metadata, subjects, ordered commits, and patch boundaries are retained.

| Module | Patch | previous base | v7 source | expected tree |
| --- | --- | --- | --- | --- |
| darling | mldr-compact-fd-band | `43b4e876…` | `585b0e89…` | `cd4b07ee…` |
| xnu | shmem-ring-guest | `3313e58b…` | `991fa4e4…` (17 commits) | `03a593d3…` |
| xnu | j7e7-lane-wakefd-sentinel | `991fa4e4…` | `c85e7afe…` | `50258531…` |
| darlingserver | perf18-server-ring | `0eb37b2a…` | `f3308be8…` | `ad558edf…` |
| darlingserver | a0-hang-fixes | `f3308be8…` | `409573e6…` | `e6f2a078…` |
| darlingserver | j7e7-postfork-reset-gate | `409573e6…` | `ce6671a…` | `c022fe48…` |

`mldr-compact-fd-band` is a topology/rebase tree mismatch: the historical
lock declared `1fce0600…`, whereas replay from the actual preceding perf
integration base produces the reviewed `cd4b07ee…` tree.  It is not repaired
from a legacy archive.  The perf18 legacy archive remains forensic-only
because its native three-way application requires missing blob
`9d525a42d14010ec55a1cdfa1bcf782e6f259948`; no active West object was
imported or consulted for materialization.

The independent primary and secondary local closures, their review manifest,
and clean-ODB dual-mechanism evidence are retained in the preserved offline
publication-evidence bundle. They must be retained until the
future create-only hosted publication is reviewed.

The final perf XNU integration tip is `92cc4fe7…`, not the historical arch
base `d068e65d…`.  Consequently only the three arch XNU entries are reissued
as v7 profile-integration locks.  Darlingserver and Darling arch v6 bases are
the actual final perf integration tips and remain unchanged.

The focused XNU semantic audit is **ACCEPT_V7**: each restacked arch replay
has the same stable patch-id and exact range-diff as v6, while all additional
tree content is `PERF_INHERITED` from the corrected prerequisite.  The audit
is recorded in `xnu-v6-v7-semantic-audit.md`; the old XNU v6/v1 boundaries are
therefore `SUPERSEDED_UNPUBLISHED`, not behaviorally divergent alternatives.

# Legacy mbox observation and retirement readiness

This document records the boundary for a future, separately approved removal.
It does **not** remove `--legacy-mbox`, the legacy implementation, or patch
archives.

## Call-site inventory

| Consumer | Profile/mode | Class | Legacy dependency |
| --- | --- | --- | --- |
| `ci/run-test-tier.sh` host | homebrew, no flag | regular push CI | No: default-lock-first. |
| `patch-stack-lock-first.yml` control | homebrew, `--legacy-mbox` | manual hosted oracle | Yes: independent A/B control. |
| `patch-stack-lock-first.yml` candidate | homebrew, no flag | manual hosted acceptance | No: default-lock-first. |
| `patch-stack-shadow.yml` | homebrew, no flag plus `--shadow-lock` | manual diagnostic oracle | Yes: legacy/canonical comparison. |
| arch/perf/non-homebrew profiles | no flag / explicit legacy | local, CI test, profile materialization | Yes: still legacy-first. |
| docs and contracts | examples/oracles | documentation/test | Preserve until their modes migrate. |

The CI policy contract fails if regular host materialization adds
`--legacy-mbox`. The manual lock-first workflow is the only hosted explicit
legacy control. Guest-smoke and other guest tiers do not pass
`--materialize-profile`, so they do not independently create homebrew applies.

## Archive dependency classification

Archives and `patches/<profile>/patches.yml` remain required for legacy
materialization of arch/perf/non-homebrew profiles; patch verify, export,
checksums, source provenance, and upstream review `format-patch`; manual
legacy/canonical and shadow oracles; and emergency recovery. Immutable refs
and schema-v2 locks replace homebrew's canonical runtime graph only, not review
payloads or other profiles' portable integration source.

## Future removal plan

**Phase A — runtime fallback only.** After the observation gate, remove the
homebrew `--legacy-mbox` runtime branch and update its manual oracle. Retain
archives, locks, immutable refs, export, verify, and recovery tooling. Rollback
is a normal revert; immutable locks/refs stay canonical and no reconstruction
from mbox files is required.

**Phase B — archive disposition.** Separately classify each archive as retained
review/recovery input, needed by another profile, or archival/deletable only
after export/upstream and recovery owners approve. Do not combine it with Phase
A.

## Current blockers

- fewer than three ordinary post-cutover CI observations with mode markers;
- manual lock-first and shadow workflows need a legacy control/oracle;
- arch/perf/non-homebrew profiles still use legacy runtime;
- verify/export/review and emergency recovery still consume archives.

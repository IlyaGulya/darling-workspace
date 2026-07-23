# Canonical lock-first transition

`west patch apply` remains legacy-mbox-first by default. Lock-first is a
strict opt-in for the typed homebrew batch in
`locks/patch-stack/lock-first-series-v2.yml`.

```
west patch apply --profile homebrew --lock-first \
  --lock-first-evidence /absolute/path/lock-first-oracle.json
```

Before any integration worktree mutation, the command validates the complete
typed batch against the profile's actual grouped execution order: module order
from `_group()`, followed by profile order within each module. A series is
identified by `(module, patch)`, not patch path alone. It independently applies
the retained mbox in a clean disposable object database and compares it to the
schema-v2 immutable lock. It then uses the lock materializer to fetch and
validate the exact immutable graph and advances only allowlisted modules to
their canonical source commits. Every other homebrew patch still goes through
the existing `git am --3way` lifecycle.

The mbox is neither deleted nor an input to canonical graph construction: it is
the independent equivalence oracle and fallback.  Lock-first is mutually
exclusive with the older shadow-only flag.  Any typed-plan, immutable-ref,
oracle, dirty-worktree, existing-result-ref, cleanup, or interrupt failure
uses the normal forced rollback lifecycle; no partially applied integration
branch is retained.  The materializer's transaction result ref is removed
before the apply returns.

Acceptance compares a no-flag control and a lock-first run from the same frozen
manifest. Their integration trees and generated
`patches/homebrew/west.lock.yml` must be byte-identical.  The manual hosted
tier is intentionally separate from default CI and must run exactly once before
expanding the allowlist.  No profile default, legacy archive, or other series
is changed by this phase.

Aggregate lock-first evidence uses `evidence_schema_version: 2`. It records
`batch_id`, `expected_count`, exact `module_order`, exact `(module, patch)`
`series_order`, and a per-series module field. Applied-commit ancestry is
checked only within each module/repository; cross-repository ancestry is neither
meaningful nor required. Batch 1--3 artifacts have implicit legacy evidence v1
and are intentionally incompatible with the v2 compare protocol. OIDs and
generated-lock hashes are compared only within a single control/lock-first run,
because committer identity can make them differ across environments.

The `patch-stack-lock-first` workflow job has a scoped 75-minute timeout. A
fresh Batch 6 local control/lock-first critical path measured about 43 minutes.
The audited 69-series Batch 7 forecast is 54.34 minutes: it adds all 25 XNU
series (32 immutable commits) without cache, alternates, shared ODB, bootstrap
reuse, or parallel replay. A 60-minute limit leaves no safe margin for hosted
network variance, the mandatory `always()` cleanup, and artifact upload; 75
minutes preserves roughly 20 minutes of operational margin. Splitting XNU
would duplicate the two fresh West bootstraps and does not address that
bottleneck. This exception does not alter any other workflow timeout.

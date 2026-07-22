# Canonical lock-first transition

`west patch apply` remains legacy-mbox-first by default.  Phase 1 adds a
strict opt-in only for the typed entry in
`locks/patch-stack/lock-first-series-v2.yml`: homebrew's
`darling/sandbox-exec-pass-through.patch`.

```
west patch apply --profile homebrew --lock-first \
  --lock-first-evidence /absolute/path/lock-first-oracle.json
```

Before any integration worktree mutation, the command validates that exactly
one metadata entry is approved and that the profile contains that patch exactly
once.  It independently applies the retained mbox in a clean disposable object
database and compares it to the schema-v2 immutable lock.  It then uses the
lock materializer to fetch and validate the exact immutable graph and advances
only that module to its canonical source commit.  Every other homebrew patch
still goes through the existing `git am --3way` lifecycle.

The mbox is neither deleted nor an input to canonical graph construction: it is
the independent equivalence oracle and fallback.  Lock-first is mutually
exclusive with the older shadow-only flag.  Any typed-plan, immutable-ref,
oracle, dirty-worktree, existing-result-ref, cleanup, or interrupt failure
uses the normal forced rollback lifecycle; no partially applied integration
branch is retained.  The materializer's transaction result ref is removed
before the apply returns.

Phase 1 acceptance compares a no-flag control and a lock-first run from the
same frozen manifest.  Their integration trees and generated
`patches/homebrew/west.lock.yml` must be byte-identical.  The manual hosted
tier is intentionally separate from default CI and must run exactly once before
expanding the allowlist.  No profile default, legacy archive, or other series
is changed by this phase.

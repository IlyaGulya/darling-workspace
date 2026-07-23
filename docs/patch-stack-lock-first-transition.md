# Canonical lock-first transition

`west patch apply --profile homebrew` uses canonical lock-first materialization
by default for the complete typed 69-series Batch 7 in
`locks/patch-stack/lock-first-series-v2.yml`. `--lock-first` remains a
compatible explicit alias. The retained mbox archive is an emergency fallback:

```
west patch apply --profile homebrew --legacy-mbox
```

Other profiles remain legacy-mbox-first. For them `--legacy-mbox` is an
explicit no-op alias of the same legacy behavior. `--shadow-lock` stays the
existing single-series legacy/canonical diagnostic and does not enable the
homebrew default replay. `--legacy-mbox` is mutually exclusive with
`--lock-first`, `--lock-first-evidence`, `--shadow-lock`, and
`--shadow-evidence`; the CLI rejects every such combination before planning,
fetching, ref creation, or worktree mutation.

```
west patch apply --profile homebrew \
  --lock-first-evidence /absolute/path/lock-first-oracle.json
```

Before any homebrew integration worktree mutation, the command validates the complete
typed batch against the profile's actual grouped execution order: module order
from `_group()`, followed by profile order within each module. A series is
identified by `(module, patch)`, not patch path alone. For each module it creates
one disposable, no-alternates object database, fetches the union of declared
immutable base/source refs once, proves every lock's exact graph, metadata and
tree, then replays the immutable commits in typed order through native
`format-patch` plus `git am --3way`. The retained archive is still the explicit
emergency fallback and independent legacy oracle in differential and hosted
acceptance; it is not replayed once per canonical series at runtime.

The mbox is neither deleted nor an input to canonical graph construction: it is
the independent equivalence oracle and explicit fallback. Lock-first is mutually
exclusive with the older shadow-only flag when requested explicitly. Any typed-plan, immutable-ref,
oracle, dirty-worktree, existing-result-ref, cleanup, or interrupt failure
uses the normal forced rollback lifecycle; no partially applied integration
branch is retained.  The materializer's transaction result ref is removed
before the apply returns.

Default mode writes no persistent oracle diagnostic: the per-series legacy
oracle evidence is transient and removed with its disposable canonical state.
An explicit `--lock-first-evidence` path remains supported, must name a new
non-symlink file, and is published only after integration succeeds. Existing
integration and generated `patches/homebrew/west.lock.yml` lifecycle remains
authoritative.

The manual hosted oracle compares exactly two frozen workspaces: a
`--legacy-mbox` control and the no-flag `default-lock-first` candidate. Its
result labels both modes explicitly, so two canonical runs cannot be accepted as
a false comparison. `--lock-first` remains a locally contracted compatibility
alias, not a third hosted side. The legacy control and default candidate must
have identical module maps, manifest state, integration results, and generated
lock. The manual hosted tier stays intentionally separate from default CI.

The manual hosted acceptance uses two fresh West workspaces from the same frozen
manifest: legacy-mbox control and default-lock-first candidate. It captures
complete module maps, generated-lock metadata, and integration results, then
verifies equality and cleanup of all transaction refs, disposable roots,
worktrees, and West jobs. The workflow remains manual-only; regular CI timeouts
are not a performance workaround.

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

## Default-cutover performance gate

The regular host tier calls `west test --profile homebrew --materialize-profile`.
When its selected metadata requires the profile checkout, `west test` invokes
`west patch apply --profile homebrew`; this is the only regular push/scheduled
path that can exercise the default materializer. Guest-smoke, guest-toolchain,
and guest-full use profile-scoped runtime tests but do not pass
`--materialize-profile`; their normal lifecycle therefore does not create a
homebrew integration branch merely because lock-first is the default. The
manual `patch-stack-lock-first` tier is the explicit two-sided oracle.

The pre-batch implementation measured 1719--1786 seconds after bootstrap,
versus roughly 10 seconds for legacy mbox. Its cost was not native replay: it
ran a clean-ODB materializer, immutable fetch, disposable worktree, preflight,
and legacy shadow oracle independently for each of 69 series. The bounded
batch design validates the typed 69-series plan before mutation, then performs
one immutable union-fetch/disposable validation context per module, while
retaining every per-series graph/metadata/canonical-tree/applied-tree proof and
the existing profile-wide rollback. It introduces no persistent cache,
alternates, shared ODB, or concurrent replay. Production default is gated on a
fresh measured post-bootstrap run of at most 180 seconds (target 120); regular
CI timeouts must not be increased to hide a regression.

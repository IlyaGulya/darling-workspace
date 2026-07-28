# Canonical lock-first profile operation

`west patch apply` uses typed lock-first replay for every production profile.
The no-flag mode reports `PATCH_STACK_MODE=default-lock-first`;
`--lock-first` is an equivalent explicit alias. There is no legacy or shadow
fallback.

Before mutation, the planner binds the exact profile patch list to the mapping
registered in `locks/patch-stack/lock-first-profiles-v1.yml` and to its typed
profile-composition lock. Execution order is `_group()` module insertion order
and then profile order within each module. Series identity is `(module, patch)`.

For each module, one disposable clean ODB fetches the union of declared
immutable base/source refs. Every lock proves:

- exact immutable ref OIDs;
- a linear, no-merge `ordered_commits` range;
- complete author and committer metadata;
- canonical source tree;
- the expected applied profile boundary tree.

The validated objects are transferred into the lifecycle worktree without
alternates or shared ODBs. Each immutable commit is converted to a temporary
mbox outside the production repository with native `git format-patch` and
replayed with native `git am --3way --committer-date-is-author-date`. The
temporary mbox is always removed. Historical profile archives are not read.

Aggregate evidence schema v2 records `batch_id`, `expected_count`, exact
`module_order`, exact `(module, patch)` `series_order`, and per-series
base/source/canonical tree/applied commit/applied tree/verdict. Applied ancestry
is checked separately per repository, never across modules. A result is
published only after integration recording; failure, evidence error, cleanup
error, or SIGINT rolls back every touched repository and emits no VALID marker.

RuntimeSourceMaterializer uses the same typed plans and optimized per-module
batch primitive in lifecycle-owned worktrees. It does not create persistent
integration refs or generated locks. Its success markers distinguish
`materializer=runtime-source`; failures preserve the original exception while
performing lifecycle cleanup.

The manual workflow compares:

1. `immutable-cherry-pick-oracle`: a separate clean-ODB implementation using
   plain Git cherry-pick and declared immutable refs only;
2. `default-lock-first`: the production implementation.

It requires identical module trees, frozen-manifest hash, and ordered generated
profile-lock hashes. It remains `workflow_dispatch` only, uses full checkouts,
and uploads evidence under `always()`.

Canonical review/recovery export is:

```text
west patch export-locks --profile <profile> --output <new-directory>
```

It produces deterministic mboxes and typed evidence from immutable lock
objects. The older `west patch export` remains an authoring command that
refreshes historical review files; it is not a materialization path.

The manual job retains its scoped 75-minute timeout for fresh West bootstrap,
candidate replay, independent oracle, cleanup, and upload. Regular CI timeouts
are unchanged; post-bootstrap canonical replay remains bounded by the accepted
180-second gate.

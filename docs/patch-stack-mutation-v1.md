# Patch-stack mutation and transaction contract v1

`dar-4ush.6` is a local, test-only integrity gate.  It does not publish
immutable refs, change a lock, invoke a remote, alter a workflow, or route a
production consumer.  The later CI work owns scheduling and artifact upload.

Run it with:

```sh
tests/run-patch-stack-mutation-contract.sh
```

The wrapper creates one task-owned temporary namespace and removes exactly
that namespace on success or failure.  The contract reconstructs a trusted
Git workspace with a recorded commit/tree anchor, a schema-v2 immutable lock,
schema-v3 lock-first mapping/composition, a real West generated lock, bundle,
immutable-oracle evidence, capture manifest, lock-first evidence, and compare
result.  It first accepts the unmodified package, then rejects every current
entry in the mutation registry (including composed mutations) before any
publication or destructive action.

The registry covers lock/schema fields, mapping/composition fields, generated
West-lock fields, every typed lock-first evidence field, candidate manifest,
lock-first module map, immutable-oracle fields, candidate-manifest binding,
compare result-path file/symlink/directory preconditions, bundle corruption, checksum
tampering, package-index/anchor/binding tampering, missing/extra package
output, and composed mutations.  Production capture also verifies every
declared profile patch against its SHA-256 field before export/materialization.
The matrix seeds real stale refs in each materializer/results/lock-first
namespace and snapshots every pre-existing result path before and after the
compare, including identity, type, content, and symlink target.
Package locks,
mappings, patches, compositions, and generated locks are checked byte-for-byte
against the anchored reconstructed workspace.  The package-integrity gate
intentionally runs first.  Every structurally valid case is replayed through
the complete production consumer chain; only explicitly unrepresentable
envelope attacks stop before it.  The result records exact case,
schema-inventory, rejection-phase, and consumer-attempt counts.  The
inventory is generated from every leaf in the production JSON artifacts and
every bound YAML input; each leaf receives delete, wrong-type, and wrong-value
mutations, so documentation cannot silently drift when the schema inventory
grows.  The production chain is:
preflight, lock-first planner, exporter, materializer transaction, capture,
immutable oracle, and hosted immutable-compare.  Compare result publication
and materializer/export transaction state are isolated and asserted absent on
every rejected case.  A green matrix therefore cannot hide a test-only
acceptance path.

The contract also runs the existing materializer recovery contract inside the
same temporary namespace.  Its controlled write/ref/cleanup failures and
KeyboardInterrupt boundaries must leave no result ref, worktree, or
transaction namespace.  A successful run prints
`PATCH_STACK_MUTATION_MATRIX_VALID` and reports the exact case count.  A
mutation that is accepted, a workspace/sentinel change, an unclean bundle
import, or a transaction cleanup failure is a hard failure.

This gate is intentionally separate from production `.7` routing and the
real-kernel/guest `.5.2` lane.  It is a bounded package/evidence oracle, not a
publication command.

# Lifecycle operation boundary v1

This is the infrastructure contract for `dar-4ush.2`. The authority lives
in the Rust crate at `lifecycle/operation-boundary`; the Python module under
`west_commands` is only a JSON subprocess adapter. No Python syscall, fd
ownership, or lifecycle policy is authoritative.

The crate also contains the typed `state::Reducer`. It computes the same
terminal, capability-generation, journal, and total recovery semantics as the
`.1` model. The Python reducer and JSON fixtures remain a differential
reference oracle until the explorer consumes the Rust reducer directly.
Rejected reducer events are atomic: catalog, live ownership, checkpoints,
journal and obligations are restored together. A recovery rollback restores
the pre-transaction ownership checkpoint and clears only transaction-failure
obligations, while commit/cleanup closes the transaction explicitly.

## Authority and typestate

The Rust API has separate non-`Clone` capability newtypes: `DirCap`,
`FileCap`, `PidFdCap`, `LockCap`, and `ExclusiveLease`. Each owns a
standard-library `OwnedFd` and releases it through RAII. `flock_exclusive()`
consumes `LockCap` and returns `ExclusiveLease`; mutation APIs accept only the
latter, so an opened-but-unlocked file cannot authorize a write. The compiler
therefore prevents accidental capability copies; moving a capability transfers
ownership. Every capability retains a `(st_dev, st_ino)` identity and
revalidation is explicit before use.

`Boundary::anchor_directory()` is the only path-accepting operation. It walks
components with `openat`, `O_DIRECTORY`, `O_CLOEXEC`, and `O_NOFOLLOW`. All
later operations accept retained capabilities and one-component names only.
Mutation requires an `ExclusiveLease` and an expected child identity.
`unlink_exact` and `rename_exact` first move the named object into a
transaction-owned staging directory, verify the retained identity there, and
only then remove or publish it. Every capability also carries a private
runtime scope brand; an `ExclusiveLease` from another anchored root is rejected
before any mutation. Cleanup moves the currently named object into a unique
private quarantine name, binds a non-read-required object capability, and
returns a typed `QuarantinedObject`. It is not deleted merely because a
directory and flock check passed: an external lifecycle controller must prove
that namespace writers have stopped and hand in a `QuiescentScope`. Without
that scope the object remains in `take_quarantine_obligations()` for recovery;
with it, `gc_quarantine()` revalidates the retained object and name before the
only permitted unlink. Failure restores with `RENAME_NOREPLACE`, so a
replacement is never deleted or overwritten.

The handoff is consuming and fail-closed: `finish()` returns the observer only
when no quarantine obligations remain, otherwise it returns the observer and
the typed obligations in `BoundaryFinishError`. `into_parts()` is the explicit
handoff for callers that intentionally transfer both; dropping an ordinary
observer can never silently discard pending quarantine state. If an object
cannot be rebound to an O_PATH capability after the move, the boundary retains
an `UnboundQuarantine` obligation with its anchored identity and quarantine
name for lifecycle-owned recovery.
File writes likewise require an `ExclusiveLease` carrying the same private
scope as the retained `FileCap`; read-only operations do not acquire a lease.

Every staging mkdir is journaled with its parent/lease/stage capability ids and
the bound `FileIdentity` before a fault checkpoint. Staging and cleanup records
are marked as mutations; rollback failures are reported as
`rollback-incomplete` with `mutated=true`, so an observer never mistakes a
partially rolled-back filesystem for a clean rollback. A successful public
mkdir is likewise recorded as a mutation.

## Transaction checkpoints

The Rust `FaultInjector` trait can stop at stage registration, mkdir binding,
publication, exact-mutation preparation, move verification, and
post-mutation checkpoints (`AfterStageMkdirBeforeBind`,
`AfterStageBindBeforeMove`, `AfterMkdirBeforeBind`,
`AfterBindBeforePublish`, `BeforeExactMutation`, `AfterMoveBeforeVerify`, and
`AfterQuarantineMoveBeforeVerify`, `AfterQuarantineVerifyBeforeGc`, and
`AfterExactMutation`). The verified object is not unlinked at the verify
checkpoint; it remains quarantined until externally scoped GC performs the
identity checks and the only permitted unlink. The boundary does not create a
quiescent scope itself.
`mkdir_child` creates the child under a transaction-owned staging directory,
binds its directory fd, and publishes it with `renameat2(RENAME_NOREPLACE)`
only after binding. If a replacement appears, rollback compares the staged
inode with the retained capability and refuses to remove the replacement. If
binding itself fails, the boundary fails closed without publishing the final
name.

The same production boundary accepts a deterministic `ScriptedClock`,
`Observer`, and fault injector. Tests exercise real fd-relative syscalls
through the same Rust transition methods; they do not run a parallel model.
`Boundary::production()` uses the bounded/no-op observer; `VecObserver` is
test/explorer-only so normal runtime use does not accumulate an unbounded
in-memory journal.
Directory enumeration is incremental and bounded by item count and monotonic
deadline. Pidfd signalling and lock acquisition are capability-kind checked
and deadline-aware; flock checks the absolute deadline before every attempt
and caps each wait to the remaining budget.

The machine-readable policy is
`lifecycle/operation-boundary-v1.json`. The focused contract compiles and
runs the Rust unit suite, checks the Python adapter for transport-only shape,
and invokes the Rust JSON self-check. Rust ingress rejects unknown policy and
trace fields, enforces the authoritative model budget ceilings from
`lifecycle/state-model-v1.json`, and compares computed recovery observations
with the recorded observations. The Python `.1` reducer and JSON golden
traces remain independent reference-oracle artifacts for the later explorer;
the four fixtures are replayed through both implementations and their
normalized results must match; they are not a substitute for this boundary.
The West adapter uses one monotonic deadline for nonblocking stdin/stdout/
stderr transport, a 64 KiB input/output bound, and bounded child reaping; a
child that never reads its request cannot stall the caller.

## Scope and non-goals

This crate is the local authority substrate for the lifecycle lab. It does not
yet wire Rootless shutdown or E-UNION production transitions, publish refs, or
change frozen Darling source. Production-default routing and optimized-overhead
measurement are intentionally deferred to `dar-4ush.7`, whose acceptance
requires connecting the boundary to real product transitions. This `.2`
artifact therefore makes no claim that production lifecycle code has already
been routed through the boundary. `dar-4ush.3` is the next infrastructure
layer after this accepted boundary; its start is blocked by the explicit
`dar-4ush.2.1` journal/ownership handoff bead. `dar-4ush.7` follows `.3` and
connects the real production consumers and overhead measurement.

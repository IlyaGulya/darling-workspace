# dar-4ush.7 Rust lifecycle controller v1

This is an architecture gate, not production routing.  It replaces the
superseded Python authority prototype with one Rust-owned transaction boundary.
No Darling, Darlingserver, XNU, lock, profile, default, workflow, or Bead is
changed by this scope.

## Ownership and phases

The controller consumes one bounded request and owns the full transaction:

`Prepared → Acquired → MembershipBound → ShutdownRequested → Drained → Quiescent → Cleaned → Finalized`

Every phase is a distinct Rust type.  Prefix, marker, launcher, pidfd, and
endpoint capabilities own non-Clone `OwnedFd` values and carry a private
runtime scope brand.  The controller never stores raw fd/PID authority in a
journal or response.  A sealed Rust `QuiescenceBackend` produces a private
`QuiescenceProof`; there is no public unsafe token constructor and ordinary
stat/flock/census checks cannot create the proof.

The inherited anchor is validated by Rust as a directory (and optional
evidence as a regular file) before any relative acquisition.  The sealed
acquisition backend then opens marker, launcher, init and pidfd capabilities
from that anchor; Python cannot supply precomputed identities or launcher
descriptors.  The optional evidence descriptor is retained in the acquisition
bundle for the whole transaction.  Acquisition validates marker identity,
metadata, owner and content digest;
launcher identity is bound to its retained fd and content digest, and that
digest must equal the request's runtime identity before `MembershipBound` is
created.  Membership
is sourced from an anchored Rust cgroup-v2 plus `/proc/<pid>/task/<tid>/children`
implementation
or a separately verified product protocol.  The immediate pre-shutdown census
must equal the startup census exactly, including every retained pidfd and
`(pid,starttime)` identity.  A changed child set, late fork, member GONE, or
PID reuse is a typed failure/obligation, never a guessed success.

Endpoint cleanup accepts only an fd-relative `PrefixCapability`, retained
`EndpointCapability` values, and the sealed proof.  The executor revalidates
each retained FD immediately before its mutation syscall; snapshots are
observations, never authority.  Replacement after the last observation is
rejected and capabilities remain in typed `RecoveryPending` state; there is no
path-based fallback.  The Linux backend moves exact objects into private
quarantine with `renameat2` and returns `QUARANTINE_GC_REQUIRED`; it never
performs a final named `fstatat`→`unlinkat` without an external namespace-writer
authority.  Recovery journal entries are closed enums.

The quarantine handoff has a separate Rust-owned bounded GC consumer. See
`docs/lifecycle-controller-quarantine-gc-v1.md`; it is intentionally not
connected to production routing yet.

`SignalSent` is emitted only by a Rust pidfd operation or a verified typed
`DARLING_SHUTDOWN_V1` product evidence record.  A launcher return code or
post-state cannot synthesize signal events.  The product protocol evidence
branch is an explicit semantic gate and remains unavailable until a separate
product-side review.

## Protocol and transport

The request and response are versioned in:

- `schemas/rootless-lifecycle-controller-request-v1.schema.json`
- `schemas/rootless-lifecycle-controller-response-v1.schema.json`

Python may select the West profile, provision a closure-verified Rust binary,
pass inherited anchor/evidence descriptors, and exchange bounded JSON.  The
transport adapter has no marker, procfs, pidfd, signal, identity, or cleanup
logic.  It retains the executable by FD, validates the closed response schema,
and requires an exact controller-closure digest, runtime digest, and per-
transaction nonce echo.  A timeout kills the whole controller process group
and has a bounded wait.  One controller invocation covers one transaction.

Every phase consumes the event/member/time/recovery budget.  A partial signal,
cleanup, interrupted checkpoint, or budget failure returns `RecoveryPending<S>`
with the original typestate, retained capabilities, ordered recovery journal,
and typed obligations.  `RecoveryPending::finalize_fail_closed()` consumes that
ownership without opening replacement descriptors.  Unresolved obligations
produce `FAIL_CLOSED` (including when the recovery budget itself is exhausted).
Version 1 deliberately has no resumable retry or `RECOVERED` verdict: a
partially-mutated transaction is terminal and fail-closed.  The normal
completed transaction verdict is `SUCCESS`.

## RED fixtures and migration

`tests/fixtures/rootless-controller-v1/` contains deterministic fixtures for
marker in-place mutation, membership drift, endpoint replacement, and launcher
replacement, plus ancestor/leaf swap, late fork, PID reuse, member GONE,
malformed/oversized protocol, deadline, SIGINT, and stale-controller cases.
The Rust unit contracts deserialize all 13 fixtures into a closed enum and
execute their typed mutation fields through a sealed fault backend, including
the marker, membership, launcher, endpoint, late-fork, PID-reuse,
signal-interruption, malformed transport, stale-controller, and deadline
classes.  They are not claims that production routing is already enabled.

`docs/lifecycle-controller-migration-v1.md` maps the superseded Python
prototype to reusable Rust contracts, moved responsibilities, and deleted
authority.  The saved prototype evidence remains
`/tmp/dar-4ush.7-SUPERSEDED_PROTOTYPE.md`.

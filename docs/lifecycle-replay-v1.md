# Lifecycle replay model v1

This is the review-only contract for `dar-4ush.1`. It is an observation and
replay format, not a production shutdown implementation. The model is
intentionally pure so the later injectable operation boundary can drive the
same transitions without making a test double authoritative.

## State model

`lifecycle/state-model-v1.json` is the canonical model document and is checked
against `schemas/lifecycle-state-v1.schema.json` and
`west_commands/lifecycle_state_model.py`.

Stable state and journal phase are independent dimensions:

- stable state: `UNINITIALIZED`, `READY`, `RUNNING`, `DRAINING`, `STOPPED`,
  `CORRUPT`;
- journal phase: `NONE`, `PREPARE`, `PUBLISH`, `CLEANUP`, `COMMIT`, `ABORT`;
- intent is a separate tagged value (`REQUEST_SHUTDOWN`, `RECREATE_PREFIX`,
  and so on), never a collection of lifecycle booleans.

The model contains all 36 stable-state × journal-phase recovery entries. An
unknown state, phase, intent, decision, capability, or recovery action is
invalid. `CapabilityHandle.move()` is a small ownership seam for the model:
the same capability cannot be consumed twice. It carries only symbolic
identity; it does not replace fd anchoring, component-wise `O_NOFOLLOW`,
revalidation, locking, fsync, or atomic publication in production code.

## Replay trace

`schemas/lifecycle-replay-trace-v1.schema.json` is validated by the real
Draft 2020-12 validator. The schema deliberately accepts an extensible
scenario identifier; the four shipped golden fixture names are a contract,
not a closed schema enum. Every trace also carries an immutable source
identity (repository, commit, tree, profile, module) and explicit event/time,
live-capability, and recovery-step budgets.
Historical observations additionally require a 64-hex artifact SHA; the
shipped fixtures intentionally use `golden-scenario` instead of claiming
production provenance.

The initial state separates a capability catalog from live ownership. Events
compute acquire, move, release, and gone transitions; double-acquire,
use-after-release, and signal-before-identity-match are rejected. Capabilities
contain symbolic identity tokens and generations only. Raw paths, raw integer
fds, and unvalidated authority are forbidden in the trace.

The model document owns the replay maxima: 128 events, 1,000,000 virtual
nanoseconds, 64 live capabilities, and 16 recovery steps. A trace may use
lower limits, never higher ones. Capability kinds are policy-bearing: only
session pidfd capabilities may appear in membership snapshots or receive a
signal; shared leases may be revalidated but are not signal targets.

`replay_trace()` is a reducer, not a projection of captured fields. Its
`apply_event()` path computes the state snapshot, journal intent, ownership
ledger, obligations, recovery action, and terminal result. Recorded
`state_after`, `expected`, and invariant names are assertions compared against
that result. Invariants are executable predicates (including budgets,
identity-before-signal, gone barriers, shared-lease bounds, and terminal
closure), and every golden trace must satisfy the complete registry.

Terminal outcomes are observations only: a `FAIL_CLOSED` or `SUCCESS` event
does not invent a new stable state. Recovery records an ownership checkpoint
and restores that exact pre-transaction ownership on rollback; consumed
capability generations remain consumed. Recovery observations are computed
from the reducer and compared with the recorded list. A trace has exactly one
terminal event and it is the final event. A normal success may have no
recovery at all, so its recovery-observation list is legitimately empty;
success is rejected while identity, fault, or incomplete-membership
obligations remain unresolved.

The terminal event is mandatory and unique. A successful terminal requires
fault, identity, and membership obligations to have been resolved; a
fail-closed terminal may preserve the unresolved evidence. The
`recovery_observations` list binds every observed state/phase pair to the
canonical total recovery table and may be empty for a normal success.

## Golden traces

The v1 fixtures are explicitly classified as `golden-scenario` fixtures, not
historical production evidence. Their immutable source identity identifies the
review model baseline; production failures require separate run/artifact
provenance before being called historical observations. They preserve failure
shapes that must remain replayable when the operation boundary is added:

- `session-root-exit-before-snapshot`: a retained root identity is gone, so
  children traversal is not attempted and shutdown fails closed;
- `shared-session-retained-holder-timeout`: a shared lease survives cleanup
  long enough to hit the bounded deadline;
- `shared-session-pid-reuse`: retained identity no longer matches, so no
  signal is sent to a replacement process;
- `shared-session-late-fork`: membership changes after a closed snapshot and
  recovery rejects the incomplete closure.

Run the local contract with:

```sh
tests/run-lifecycle-trace-contract.sh
```

The contract runs schema validation and the pure reducer twice for every
fixture and requires identical terminal results. It checks the canonical
36-entry recovery matrix and executable targets, all golden traces, capability
ownership lifecycle, source provenance, budget enforcement, no-raw-authority
rules, schema/Python differential negatives, and tamper-negative fixtures. It
has no production, filesystem, West, ref, or hosted side effects.

The next layer is specified separately in
`docs/lifecycle-operation-boundary-v1.md`. Its Rust boundary is the
production-quality fd/capability backend for future controllers and supplies a
deterministic observer/clock/fault seam; it is not yet wired into the frozen
Rootless or E-UNION transitions. That production-routing and optimized-overhead
gate belongs to `dar-4ush.7`. The Python module is only a subprocess adapter;
the boundary does not make a trace fixture a second implementation and does
not change the frozen runtime.

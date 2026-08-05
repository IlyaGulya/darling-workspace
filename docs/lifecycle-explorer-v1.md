# Lifecycle explorer v1

`dar-4ush.3` is the bounded deterministic exploration layer above the accepted
Rust operation boundary. It is infrastructure-only: it does not route
Rootless, E-UNION, or any other production transition and it does not inspect
host state.

The report shape is version 2: recovery classification is represented by
independent axes rather than the former overloaded detected boolean.

## Matrix and budgets

The Rust crate at `lifecycle/operation-boundary` enumerates all sixteen
declared before/after labels and the four interleaving scenario inputs. The
first eight labels (`prepare`, `publish`, `cleanup`, and `commit`) are
`REAL_OPERATION_CHECKPOINT` witnesses for the fd-relative Boundary. The final
eight labels (`barrier`, `identity`, `signal`, and `endpoint`) are explicitly
`REDUCER_ONLY_ALIAS` scenarios: they exercise the real operation checkpoint
shown in the report, but do not claim that a process/session lifecycle seam
was executed. A future typed session boundary may promote those labels; until
then the report keeps the distinction machine-readable.

The four interleavings `none`, `inode-aba`, `partial-publication`, and
`cleanup-failure` have typed namespace hooks. A BEFORE hook runs before the
operation can perform its final exact mutation. For both BEFORE and AFTER
cases, the Boundary parks its external quiescence scope while invoking the
hook, so the namespace writer is never run under the final-mutation authority;
a writer irreversibly invalidates that parked proof and recovery establishes a
fresh barrier.
The other eight names remain explicit
reducer-only fixtures (`orphan-cgroup`, `post-deadline-signal`, `restart`,
`late-fork`, `root-exit`, `pid-reuse`, `fork-churn`, and `concurrent-close`);
they are not reported as real filesystem interleavings until a matching
typed process/session seam exists.

Each direct/alias case first executes one real fd-relative `Boundary` operation
(`mkdir_child`, `unlink_exact`, or `rename_exact`) against a disposable
directory. The selected fault is carried by the configured `FaultInjector` and
runs at the declared checkpoint while the operation still owns its anchored
directory/lease capabilities. BEFORE hooks run immediately before the
checkpoint rejects; AFTER faults run at the real checkpoint, without a second
public call after the operation returns. The observer must report the typed
injected checkpoint. The report also exposes the nine operation-boundary
checkpoints from the Rust capability API, and the contract requires all nine
to be reached. The eight additional reducer-only interleaving fixtures execute
the same typed draft/reducer path without claiming a filesystem operation;
their `real_checkpoint_observed` and `interleaving_applied` witnesses remain
false.
The resulting `INJECTED` checkpoint is replayed as a typed
`operation-failure` obligation. Recovery must consume that obligation before a
case can be classified `CLEAN`; a synthetic `fault_injected` event alone is not
evidence of clean recovery. The report uses independent axes rather than
overloading a boolean detection flag:

* `fault_detected` proves that the real checkpoint and reducer witness were
  both observed for direct cases, while reducer-only cases use the typed
  reducer witness alone;
* `recovery_status` is `CLEAN`, `FORENSIC_REQUIRED`, `UNSAFE`, or `UNDETECTED`;
* `safety_preserved` proves the product-visible postcondition and retained
  identity evidence;
* `recovery_completed` reports whether all typed obligations were consumed.

The aggregate is 104 `CLEAN`, 24 `FORENSIC_REQUIRED`, 0 `UNSAFE`, and 0
`UNDETECTED` across 128 scenarios. The original 64-case matrix remains 56
`CLEAN` / 8 `FORENSIC_REQUIRED`; the additional 64-case reducer-only matrix is
48 `CLEAN` / 16 `FORENSIC_REQUIRED`. The 128 scenarios comprise 32 direct Boundary
witnesses, 32 explicit reducer-only boundary aliases, and a full 8-by-8
reducer-only interleaving / boundary matrix. In the eight forensic cases the original inode remains in the
typed stage obligation and the foreign replacement remains at the original
name. The explorer never guesses whether `UNLINK_EXACT` or `RENAME_EXACT`
should be forward-finalized: it retains the exact forensic root and JSON
evidence instead.

Every case has an explicit seed, bounded event count, bounded schedule steps,
and a virtual monotonic clock. The explorer rejects a case that exceeds the
authoritative `.1` model ceilings for events, virtual time, live capabilities,
or recovery observations. The same seed produces identical semantic JSON after
normalizing disposable root paths and run-local device/inode values; each
individual report retains the exact identities observed on disk.

## Reduction and evidence

Cases execute the Rust capability `Boundary` first and then drive the Rust
`state::Reducer`, not a source-text oracle. The reducer records both the typed
operation rejection and its recovery handoff. Safety
invariants and all authoritative budgets are checked after every event;
terminal state, obligations, recovery observations, and the complete
invariant registry are checked before a case is reported.
The explorer performs a deterministic deletion pass to minimize each detected
failure while retaining its fault and interleaving witness. The same seed
produces byte-identical semantic JSON; independent-run comparison normalizes
only PID-qualified disposable forensic paths.

Each result includes the minimized replay trace, source identity, seed, boundary,
interleaving, event/time budgets, the observed operation outcome/mutation state,
the filesystem postcondition, and both typed recovery-obligation queues (with
their exact obligation IDs). It also records independent `fault_detected`,
`recovery_status`, `safety_preserved`, and `recovery_completed` axes plus the
expected/staged/obligation `(device,inode)` identities for retained-stage evidence. It also records independent
`real_checkpoint_observed` and `real_injection_observed` witnesses, so a
synthetic reducer event cannot stand in for a missing Boundary callback. A
probe root is removed only when those queues are empty, the disposable
filesystem is clean, and the observed postcondition is satisfied. Otherwise
the root is retained as forensic evidence and the report marks
`root_preserved=true`; an explicit obligation or failed postcondition is never
dropped to manufacture a clean result. Traces are valid
`lifecycle-replay-trace-v1` documents and are independently replayed by the
Python `.1` oracle and Draft 2020-12 schema validator in
`tests/west_test_contracts/lifecycle_explorer_contract.py`.
Probe roots are unique per process/serial and are never pre-deleted on a
subsequent run; retained roots therefore remain available for forensic
inspection instead of being silently reused.
The trace provenance is a `golden-scenario` identity for the accepted `.2`
operation-boundary reducer; the report's explorer version and seed identify the
deterministic `.3` generator rather than claiming that an uncommitted generator
file is already present in the reducer's source commit.

The focused contract executes all 128 cases (32 direct, 32 aliases, and 64
reducer-only fixtures) and separately validates
the real/reducer-only boundary and interleaving registries. It checks every forensic root's stage
identity, replacement contents, one-stage/zero-quarantine ownership, and
absence of automatic rename or deletion. Tampering with a generated trace is a
negative test and must be rejected by the executable Python oracle.

Run the local gate with:

```text
tests/run-lifecycle-explorer-contract.sh
```

This layer emits deterministic evidence only. Production routing and overhead
measurement remain owned by `dar-4ush.7` after the explorer and its consumers
have been reviewed.

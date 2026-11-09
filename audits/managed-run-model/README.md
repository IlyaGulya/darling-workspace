# Managed-run producer/recovery lifecycle: bounded crash model

This directory is the independent formal model for the combined managed-run
lifecycle implemented by `west_commands/managed_run.py` and its declarative
contract `west_commands/managed_run_lifecycle.py`.  It was written from the
persisted records and visibility boundaries, not from an implementation trace.

## What is modeled

One run has eight persisted phases:

```
preparing -> creating -> running -> capturing -> retiring -> completed
                 |           |          |           |
                 +-----------+----------+-----------+--> blocked
                 +-----------+----------+----------> retained -> retiring
```

`completed` means the resource lifecycle ended: the lane was retired, the
payload was sealed and the reference was handed to retention.  It does **not**
mean workload PASS.  `unknown` is a report-only phase for an unreadable or
foreign record; it is never persisted and never adopted.

The model keeps five facts independent of the phase string:

| Fact | Domain | Meaning |
| --- | --- | --- |
| `launch` | `not_started` / `unknown` / `started` | persisted `command_started` tri-state; absent is the historical `not_started` |
| `commandLaunched` | boolean | the actual OS launch happened |
| `output` | `open` / `closed` | host output writers |
| `lane` | `none` / `live` / `retired` | native lane/scope resource |
| `evidence` | `none` / `unstarted` / `lane` | sealed payload class |

`runPin` and `evidencePin` model the external runtime references.  `outcome` and
`outcomeValue`/`outcomeAudit` model the write-once `outcome.json`.  `owner`,
`ownerAlive`, `lock` and `adopted` model the producing process, its live identity
and the exclusive run lease.  `wasCompleted` records terminality; `handoff`
marks the composition boundary.

Every persistent mutation is a separate action, including the sequence

```
record unknown launch -> launch OS process -> record started
```

so every reachable state is a possible crash point.  A crashed/lingering owner
is modeled by `CrashOwner`/`OwnerLinger`; `RecoverAcquire` and `RecoverAdopt`
model the recoverer, and `CrashRecoverer`/`RecoverReacquire` model a crash of the
recovery process itself.  Recovery shares the same finish actions only after the
lease is held and the record is adopted.  The model has **no fairness or
liveness claim**: an unknown launch may stay blocked forever rather than
fabricate a result.

## Bounds

One run, one producing owner, at most one recoverer, one lane, one payload and
one runtime reference.  `Phases` and the value domains above are fixed in
`ManagedRun.tla`, so the model is finite.  It does not model file locks,
inode/directory identity, journal files, the evidence census, mount namespaces,
pruning order or image bytes; those are production checks covered by the host
contracts and by `audits/retirement-model`.

## Composition with the retirement model

`Retirement.tla` begins with evidence present and runtime images published.  The
`Handoff` action here reaches exactly that boundary:

* phase `completed`;
* sealed evidence (`lane` or `unstarted`);
* for a lane capture, the evidence pin held and the lane retired.

`HandoffReady` states that interface.  This model performs no journal write, pin
release, image census, reader-lease check or image deletion after handoff; those
remain `evidence_retention.py` / `runtime_retention.py` and
`audits/retirement-model`.  Do not reimplement that protocol here.

## Safe matrix

`safe.cfg` checks every invariant in every reachable normal-run state, including
crashes after each boundary. `safe-foreign.cfg` begins with a foreign record and
checks its preservation nonvacuously; the matching unsafe foreign arm uses the
same initial condition and deliberately permits adoption.

| Invariant | Required meaning |
| --- | --- |
| `TypeOK` | finite state components |
| `NoStolenLiveOwner` | a recoverer acquires/adopts only after owner responsibility ended or in the explicit `retained`/`blocked` transfer phases |
| `NoFabricatedLaunch` | `not_started` cannot hide a launched command; `started` requires the launch fact |
| `NoEvidenceWithUnknownLaunch` | no payload is sealed while launch certainty is unknown |
| `NoUnstartedEvidenceWithoutNotStarted` | unstarted evidence requires a known not-started launch |
| `NoUnstartedEvidenceWhenMaybeStarted` | unstarted evidence is impossible once the command was launched |
| `NoEvidenceBeforeClosedOutput` | lane evidence requires closed host writers |
| `NoRemovalBeforeEvidence` | a retired lane requires sealed evidence and its reference |
| `NoCompletedWithoutEvidence` | completion proves evidence, cleanup and a final result |
| `CompletedTerminal` | once completed, the phase never regresses |
| `NoReferenceGap` | a live lane keeps the run reference until evidence exists |
| `NoEvidenceReferenceDrop` | sealed evidence keeps its reference until handoff |
| `HandoffReady` | handoff supplies the retirement model's initial state |
| `OutcomeImmutable` | the published outcome never changes |
| `NoFabricatedPass` | cancellation/unknown workload never becomes rc 0; completed is not PASS |
| `ForeignPreserved` | unreadable/foreign records are never adopted |

## Unsafe matrix (intentionally wrong arms)

Each config flips exactly one guard; the expected counterexample is a model
result, not an execution claim.

| Config | Guard removed | Expected invariant |
| --- | --- | --- |
| `unsafe-downgrade.cfg` | unknown launch may be written as not-started | `NoFabricatedLaunch` VIOLATED |
| `unsafe-unstarted-evidence.cfg` | may-have-started run may be sealed as unstarted | `NoUnstartedEvidenceWithoutNotStarted` VIOLATED |
| `unsafe-unknown-evidence.cfg` | evidence may be sealed with unknown launch | `NoEvidenceWithUnknownLaunch` VIOLATED |
| `unsafe-open-output.cfg` | evidence may be sealed with open host writers | `NoEvidenceBeforeClosedOutput` VIOLATED |
| `unsafe-remove-before-evidence.cfg` | lane may be retired before evidence | `NoRemovalBeforeEvidence` VIOLATED |
| `unsafe-complete-without-evidence.cfg` | run may complete without evidence | `NoCompletedWithoutEvidence` VIOLATED |
| `unsafe-drop-pin.cfg` | evidence pin may be dropped after sealing | `NoEvidenceReferenceDrop` VIOLATED |
| `unsafe-early-release.cfg` | run reference may be released before evidence | `NoReferenceGap` VIOLATED |
| `unsafe-overwrite-outcome.cfg` | published outcome may be overwritten | `OutcomeImmutable` VIOLATED |
| `unsafe-fake-pass.cfg` | cancelled run may report rc 0 | `NoFabricatedPass` VIOLATED |
| `unsafe-fabricate-launch.cfg` | started may be recorded before the OS launch | `NoFabricatedLaunch` VIOLATED |
| `unsafe-adopt-live.cfg` | recoverer may adopt a live owner in an active phase | `NoStolenLiveOwner` VIOLATED |
| `unsafe-adopt-foreign.cfg` | foreign record may be adopted | `ForeignPreserved` VIOLATED |
| `unsafe-revert-completed.cfg` | completed may regress to running | `CompletedTerminal` VIOLATED |

## Source-to-action mapping

| Model action | Production source |
| --- | --- |
| `PrepareRecord` | `run_experiment` initial `_save(..., initial=True)` |
| `PinRun` | `run_experiment` `pin_runtime(..., "run:" + run)` |
| `CreateLane` | `create_lane(..., managed_run=...)` |
| `BeginRun` | `_save(phase="running", command_started=None)` |
| `LaunchCommand` | `_Command(...)` opening host streams |
| `RecordStarted` | `_save(command_started=True)` |
| `RunWorkload` / `CloseOutput` | `command.wait(...)` and `command.close()` |
| `RequestCancel` / `RecordCancel` | signal handler, `workload_rc`, `cancel_signal` |
| `PublishOutcome` | `_outcome` write-once `outcome.json` |
| `BeginCapture` / `SealLaneEvidence` | `_finish` capture callback and `finalize_run_evidence` |
| `SetCreating` / `SealUnstartedEvidence` | `_finish` unstarted branch and `finalize_unstarted_run_evidence` |
| `PinEvidence` / `ReleaseRunPin` | `pin_runtime("evidence:...")`, `unpin_runtime("run:...")` |
| `BeginRetiring` / `RetireLane` | `_save(phase="retiring", evidence=...)`, `destroy_lane(before_retire=capture)` |
| `CompleteRun` | `_save(phase="completed", cleanup=..., final_rc=...)` |
| `KeepRun` | `_keep` retained/blocked records |
| `FailBlocked` | `_finish` blocked cleanup failure and recovery exception path |
| `OwnerLinger` / `CrashOwner` | owner releases the flock but stays alive / owner dies |
| `RecoverAcquire` / `RecoverAdopt` | `recover_runs` `_run_lock` and owner adoption |
| `RecoverReleaseRetained` | `recover_runs(release_kept=True)` on a retained record |
| `CrashRecoverer` / `RecoverReacquire` | a recovery process dies and another acquires the run lease |
| `RecoverCloseOutput` | recovery destroys the orphaned command writers |
| `Handoff` | completed record with sealed evidence passed to retention |
| `DowngradeUnknown` etc. | the unsafe config switch only; no production edge |

## Running the model

Use the shared isolated runner; do not add another invocation implementation.
The parent owns the model output root.

```sh
audits/rpc-plane-model/run-all.sh --jar "$TLA_JAR" \
  --model-dir audits/managed-run-model --module ManagedRun \
  --output-root "${DW_STATE_ROOT:?}/managed-run-model" \
  --case safe.cfg:TypeOK:OK \
  --case safe.cfg:NoStolenLiveOwner:OK \
  --case safe.cfg:NoFabricatedLaunch:OK \
  --case safe.cfg:NoEvidenceWithUnknownLaunch:OK \
  --case safe.cfg:NoUnstartedEvidenceWithoutNotStarted:OK \
  --case safe.cfg:NoUnstartedEvidenceWhenMaybeStarted:OK \
  --case safe.cfg:NoEvidenceBeforeClosedOutput:OK \
  --case safe.cfg:NoRemovalBeforeEvidence:OK \
  --case safe.cfg:NoCompletedWithoutEvidence:OK \
  --case safe.cfg:CompletedTerminal:OK \
  --case safe.cfg:NoReferenceGap:OK \
  --case safe.cfg:NoEvidenceReferenceDrop:OK \
  --case safe.cfg:HandoffReady:OK \
  --case safe.cfg:OutcomeImmutable:OK \
  --case safe.cfg:NoFabricatedPass:OK \
  --case safe.cfg:ForeignPreserved:OK \
  --case safe-foreign.cfg:TypeOK:OK \
  --case safe-foreign.cfg:ForeignPreserved:OK \
  --case unsafe-downgrade.cfg:NoFabricatedLaunch:VIOLATED \
  --case unsafe-unstarted-evidence.cfg:NoUnstartedEvidenceWithoutNotStarted:VIOLATED \
  --case unsafe-unknown-evidence.cfg:NoEvidenceWithUnknownLaunch:VIOLATED \
  --case unsafe-open-output.cfg:NoEvidenceBeforeClosedOutput:VIOLATED \
  --case unsafe-remove-before-evidence.cfg:NoRemovalBeforeEvidence:VIOLATED \
  --case unsafe-complete-without-evidence.cfg:NoCompletedWithoutEvidence:VIOLATED \
  --case unsafe-drop-pin.cfg:NoEvidenceReferenceDrop:VIOLATED \
  --case unsafe-early-release.cfg:NoReferenceGap:VIOLATED \
  --case unsafe-overwrite-outcome.cfg:OutcomeImmutable:VIOLATED \
  --case unsafe-fake-pass.cfg:NoFabricatedPass:VIOLATED \
  --case unsafe-fabricate-launch.cfg:NoFabricatedLaunch:VIOLATED \
  --case unsafe-adopt-live.cfg:NoStolenLiveOwner:VIOLATED \
  --case unsafe-adopt-foreign.cfg:ForeignPreserved:VIOLATED \
  --case unsafe-revert-completed.cfg:CompletedTerminal:VIOLATED
```

The runner copies the module into an isolated directory, so this directory gains
no TLC artifacts.

## Evidence limits

This is model evidence for the persisted transition, launch-certainty,
output/evidence and ownership structure.  It is not host or runtime behavioral
proof; the real process/lease/filesystem arms live in
`tests/west_test_contracts/managed_run_contract.py`.  The model abstracts locks,
inode identity, the evidence payload contents and the retention journal.

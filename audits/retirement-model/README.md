# Managed-run retirement: crash/recovery model

This directory is the independent formal model for the cross-module retirement
protocol implemented by `west_commands/evidence_retention.py`,
`west_commands/runtime_retention.py` and `west_commands/managed_run.py`.  It was
written from the persisted records and their irreversible boundaries, not from
an implementation trace log.

## Protocol being modeled

One declared owner root holds three interacting stores:

* `runs/run-UUID/` — mutable `run.json` control and immutable sealed `payload/`
  evidence, identified by `runtime_root` and an `evidence:<run>` runtime pin.
* `retired-evidence/` — a durable journal (`planned` -> `detached` ->
  `completed`) plus journaled trash.  It keeps the run's runtime reference and
  `run:`/`evidence:` pin keys until the owner releases them and acknowledges.
* `runtime-images/` — catalog registrations, journaled trash and the image
  bytes.  Retirement journals the trash path and inode before the rename.

Required order for one removed run:

1. evidence detach/removal with the journal reaching `completed`;
2. release of the external runtime references (`evidence:` and `run:` pins);
3. acknowledgment of the completed journal;
4. only then may image retention reclaim an image whose references are gone.

Image retention must not depend on pin presence alone: retained evidence, an
unacknowledged journal, a live reader lease, newest-publication rank, a foreign
generation at the recorded path and an inconclusive census each preserve bytes.

## Model shape and boundedness

One run (`r1`) references one image (`i0`); `i1` is the newest publication and
has no run.  `Runs`, `Images` and `Newest` are fixed in the module, so the model
is finite and bounded: 1 run, 2 images, one runtime-root reference per run, no
multiple runs sharing an image and no independent lane pin (the run/evidence
pins stand in for external references).  `Crash` is a stutter on every persisted
variable.  Because each persistent mutation is a separate action, every
reachable state is a possible crash point, and the actions enabled there are the
restart decisions available to the next collection.  Repeated retries are the
same actions re-enabled from persisted preconditions: a completed step's guard
no longer holds, so it cannot repeat, while a crash before release/ack leaves
exactly those actions enabled again.

The model has **no fairness or liveness claim**.  It does not model file locks,
inode numbers, journal temporary-file publication, receipt contents, mount
namespace census, or the privilege fallback; those remain production checks with
host behavioral contracts.  It does not claim a protected copy service.

## Source-to-action mapping

| Model action | Production source |
| --- | --- |
| `PinEvidence(r)` | `managed_run._finish` capture callback `pin_runtime(..., "evidence:" + run)` |
| `ReleaseRunPin(r)` | `managed_run._finish` and `collect_retired` `unpin_runtime(..., "run:" + run)` |
| `EvidencePlan(r)` | `evidence_retention._retire_run` planned journal publication |
| `EvidenceDetach(r)` | `_retire_run` `os.rename(run, trash)` |
| `EvidenceWriteDetached(r)` | `_retire_run` / `_remove_detached` journal phase write |
| `EvidenceDelete(r)` | `_retire_run` / `_remove_detached` `shutil.rmtree(trash)` |
| `EvidenceComplete(r)` | `_retire_run` / `_remove_detached` completed journal write |
| `ReleaseEvidencePin(r)` | `collect_retired._release_run_references` evidence key |
| `Ack(r)` | `collect_retired` `ack_run_retirement(row)` |
| `ImageRetire(i)` | `runtime_retention._retire_and_remove` catalog-lock journal |
| `ImageDetach(i)` | `_detach_and_remove` / `_recover_retirement` rename |
| `ImageDelete(i)` | `_delete_detached` `shutil.rmtree(trash)` |
| `ImageClearRecord(i)` | `_delete_detached` / `_recover_retirement` record unlink |
| `ReaderEnter(i)` / `ReaderLeave(i)` | `runtime_retention.runtime_use` share lease |
| `ReplaceImage(i)` | a foreign generation at the recorded root/trash |

The evidence-reference guard lives in
`evidence_retention.runtime_image_references`, which `prune_runtimes` consults
before any state branch, including retirement recovery.

## Crash points at the irreversible boundaries

| Persisted state after crash | Restart action | Expected outcome |
| --- | --- | --- |
| journal `planned`, run attached | `EvidenceDetach` | re-detach by recorded identity |
| bytes detached, journal `planned` | `EvidenceWriteDetached` | record the detached phase |
| journal `detached`, trash present | `EvidenceDelete` | retry removal |
| bytes removed, journal `detached` | `EvidenceComplete` | complete the ledger |
| journal `completed`, pins held | `ReleaseEvidencePin`/`ReleaseRunPin` | release then acknowledge |
| journal `completed`, refs released | `Ack` | drop journal; image may then be reclaimed |
| image `retiring`, bytes attached | `ImageDetach` | resume after evidence/pin checks |
| image bytes detached | `ImageDelete` | retry removal |
| bytes gone, registration present | `ImageClearRecord` | clear the stale registration |

## Safe matrix

`safe.cfg` runs all invariants in every reachable state, including states that
can only be reached by crashing after each boundary above:

| Invariant | Required order/protection |
| --- | --- |
| `TypeOK` | finite state components |
| `NoReferenceDrop` | no image leaves `published` until every run/journal reference is removed and acknowledged |
| `NoAckBeforeRelease` | acknowledgment never precedes evidence/run pin release |
| `NoReleaseBeforeRemoval` | the evidence pin is released only after the removal ledger completed |
| `NoGcWithReader` | no retention transition while a reader lease holds the image |
| `NoNewestEviction` | the newest publication never starts retirement |
| `NoRegistrationDrop` | a registration is cleared only after its bytes are gone |
| `NoForeignTrash` | bytes whose generation does not match the journal are never detached/deleted as owned |

## Unsafe matrix (intentionally wrong arms)

Each arm flips exactly one guard and must produce the named violation; the
expected counterexample is a model result, not an execution claim.

| Config | Guard removed | Expected invariant |
| --- | --- | --- |
| `unsafe-gc-before-ack.cfg` | image retirement ignores evidence/ack | `NoReferenceDrop` VIOLATED |
| `unsafe-ack-before-release.cfg` | acknowledgment ignores pin release | `NoAckBeforeRelease` VIOLATED |
| `unsafe-early-release.cfg` | evidence pin release ignores removal | `NoReleaseBeforeRemoval` VIOLATED |
| `unsafe-reader.cfg` | retention ignores reader leases | `NoGcWithReader` VIOLATED |
| `unsafe-newest.cfg` | retention may retire the newest image | `NoNewestEviction` VIOLATED |
| `unsafe-registration-drop.cfg` | registration may be cleared before bytes | `NoRegistrationDrop` VIOLATED |
| `unsafe-foreign-identity.cfg` | detach/delete ignores generation match | `NoForeignTrash` VIOLATED |

## Running the model

Use the shared isolated runner; do not add another invocation implementation.

```sh
audits/rpc-plane-model/run-all.sh --jar "$TLA_JAR" \
  --model-dir audits/retirement-model --module Retirement \
  --output-root "${DW_STATE_ROOT:?}/retirement-model" \
  --case safe.cfg:TypeOK:OK \
  --case safe.cfg:NoReferenceDrop:OK \
  --case safe.cfg:NoAckBeforeRelease:OK \
  --case safe.cfg:NoReleaseBeforeRemoval:OK \
  --case safe.cfg:NoGcWithReader:OK \
  --case safe.cfg:NoNewestEviction:OK \
  --case safe.cfg:NoRegistrationDrop:OK \
  --case safe.cfg:NoForeignTrash:OK \
  --case unsafe-gc-before-ack.cfg:NoReferenceDrop:VIOLATED \
  --case unsafe-ack-before-release.cfg:NoAckBeforeRelease:VIOLATED \
  --case unsafe-early-release.cfg:NoReleaseBeforeRemoval:VIOLATED \
  --case unsafe-reader.cfg:NoGcWithReader:VIOLATED \
  --case unsafe-newest.cfg:NoNewestEviction:VIOLATED \
  --case unsafe-registration-drop.cfg:NoRegistrationDrop:VIOLATED \
  --case unsafe-foreign-identity.cfg:NoForeignTrash:VIOLATED
```

The runner copies the module into an isolated run directory, so this directory
gains no TLC artifacts.  `TLA_JAR` is the pinned
`tla2tools-1.7.4.jar`; the parent owns the model output root.

## Evidence limits

This is model evidence for the persisted ordering and guard structure.  It is
not host or runtime behavioral proof; the real-filesystem arms live in
`tests/west_test_contracts/evidence_run_retention_contract.py` and
`tests/west_test_contracts/runtime_retention_contract.py`.  The model abstracts
locks and inode identity, and it does not establish that unknown or foreign
entries are preserved correctly in production -- that is covered by the host
contracts, not by TLC.

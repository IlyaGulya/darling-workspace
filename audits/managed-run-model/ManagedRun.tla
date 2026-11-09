--------------------------- MODULE ManagedRun ---------------------------
(***************************************************************************)
(* Bounded model of the producer/recovery lifecycle of one managed run.     *)
(*                                                                          *)
(* The model covers creation, the non-atomic command-launch boundary,       *)
(* running, cancellation, capture, handoff, lane retirement, retained runs  *)
(* and blocked recovery.  One producing owner and at most one recoverer     *)
(* share the run lease; every persistent mutation is a separate action, so  *)
(* every reachable state is a crash point and the enabled actions from it   *)
(* are exactly the restart choices of the next process.  The model stops at *)
(* the completed record with sealed evidence and its retained reference;    *)
(* audits/retirement-model/Retirement.tla owns the later journal, pin,      *)
(* reader-lease and image-retention protocol.                               *)
(*                                                                          *)
(* There is no fairness or liveness claim.  A run whose launch outcome is   *)
(* unknown may remain blocked forever rather than invent a result.          *)
(*                                                                          *)
(* See README.md for the safe/unsafe matrix and the source-action mapping.  *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS UnsafeDowngradeUnknown, UnsafeUnstartedWhenMaybeStarted,
          UnsafeEvidenceUnknownLaunch, UnsafeEvidenceWithOpenOutput,
          UnsafeRemoveBeforeEvidence, UnsafeCompleteWithoutEvidence,
          UnsafeDropEvidencePin, UnsafeEarlyRelease, UnsafeOverwriteOutcome,
          UnsafeFabricatePass, UnsafeFabricateStarted, UnsafeAdoptLiveOwner,
          UnsafeAdoptForeignRecord, UnsafeRevertCompleted, ForeignRecord

Phases == {"absent", "unknown", "preparing", "creating", "running",
           "capturing", "retiring", "completed", "retained", "blocked"}
LivePhases == {"preparing", "creating", "running", "capturing", "retiring",
               "retained", "blocked"}
Launches == {"not_started", "unknown", "started"}
EvidenceStates == {"none", "unstarted", "lane"}

VARIABLES phase, launch, commandLaunched, output, lane, evidence, runPin,
          evidencePin, keep, cancel, workloadRc, finalRc, outcome, outcomeValue,
          outcomeAudit, owner, ownerAlive, lock, adopted,
          adoptedFromTransferable, cleanupDone, wasCompleted, handoff

vars == <<phase, launch, commandLaunched, output, lane, evidence, runPin,
          evidencePin, keep, cancel, workloadRc, finalRc, outcome, outcomeValue,
          outcomeAudit, owner, ownerAlive, lock, adopted,
          adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

TypeOK ==
  /\ phase \in Phases
  /\ launch \in Launches
  /\ commandLaunched \in BOOLEAN
  /\ output \in {"open", "closed"}
  /\ lane \in {"none", "live", "retired"}
  /\ evidence \in EvidenceStates
  /\ runPin \in BOOLEAN
  /\ evidencePin \in BOOLEAN
  /\ keep \in BOOLEAN
  /\ cancel \in BOOLEAN
  /\ workloadRc \in {"unknown", "7", "cancelled"}
  /\ finalRc \in {"none", "1", "7", "cancel", "0"}
  /\ outcome \in {"none", "published"}
  /\ outcomeValue \in {"none", "unknown", "7", "cancel"}
  /\ outcomeAudit \in {"none", "unknown", "7", "cancel"}
  /\ owner \in {"none", "owner", "recoverer"}
  /\ ownerAlive \in BOOLEAN
  /\ lock \in {"none", "owner", "recoverer"}
  /\ adopted \in BOOLEAN
  /\ adoptedFromTransferable \in BOOLEAN
  /\ cleanupDone \in BOOLEAN
  /\ wasCompleted \in BOOLEAN
  /\ handoff \in BOOLEAN

Active == \/ (owner = "owner" /\ lock = "owner" /\ ownerAlive)
          \/ (adopted /\ lock = "recoverer")
TransferPhase == phase \in {"retained", "blocked"}

Init ==
  /\ phase = IF ForeignRecord THEN "unknown" ELSE "absent"
  /\ launch = "not_started"
  /\ commandLaunched = FALSE
  /\ output = "closed"
  /\ lane = "none"
  /\ evidence = "none"
  /\ runPin = FALSE
  /\ evidencePin = FALSE
  /\ keep \in BOOLEAN
  /\ cancel = FALSE
  /\ workloadRc = "unknown"
  /\ finalRc = "none"
  /\ outcome = "none"
  /\ outcomeValue = "none"
  /\ outcomeAudit = "none"
  /\ owner = "none"
  /\ ownerAlive = FALSE
  /\ lock = "none"
  /\ adopted = FALSE
  /\ adoptedFromTransferable = FALSE
  /\ cleanupDone = FALSE
  /\ wasCompleted = FALSE
  /\ handoff = FALSE

(***************************************************************************)
(* Producer: create / start / run / cancel / finish.                        *)
(***************************************************************************)

PrepareRecord ==
  /\ ~ForeignRecord
  /\ phase = "absent"
  /\ owner = "none"
  /\ phase' = "preparing"
  /\ owner' = "owner"
  /\ ownerAlive' = TRUE
  /\ lock' = "owner"
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, adopted, adoptedFromTransferable,
              cleanupDone, wasCompleted, handoff>>

PinRun ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "creating"
  /\ ~runPin
  /\ runPin' = TRUE
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

CreateLane ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "creating"
  /\ runPin
  /\ lane = "none"
  /\ evidence = "none"
  /\ lane' = "live"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

(* The record says may-have-started before the OS process is launched.  A    *)
(* crash here over-approximates launch, which is the safe direction.         *)
BeginRun ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "creating"
  /\ lane = "live"
  /\ (evidence = "none" \/ UnsafeUnstartedWhenMaybeStarted)
  /\ phase' = "running"
  /\ launch' = "unknown"
  /\ UNCHANGED <<commandLaunched, output, lane, evidence, runPin, evidencePin,
              keep, cancel, workloadRc, finalRc, outcome, outcomeValue,
              outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

LaunchCommand ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "running"
  /\ launch = "unknown"
  /\ ~commandLaunched
  /\ commandLaunched' = TRUE
  /\ output' = "open"
  /\ UNCHANGED <<phase, launch, lane, evidence, runPin, evidencePin, keep,
              cancel, workloadRc, finalRc, outcome, outcomeValue, outcomeAudit,
              owner, ownerAlive, lock, adopted, adoptedFromTransferable,
              cleanupDone, wasCompleted, handoff>>

RecordStarted ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "running"
  /\ launch = "unknown"
  /\ commandLaunched
  /\ launch' = "started"
  /\ UNCHANGED <<phase, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

RunWorkload ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "running"
  /\ commandLaunched
  /\ ~cancel
  /\ workloadRc = "unknown"
  /\ workloadRc' = "7"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, finalRc, outcome, outcomeValue,
              outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

CloseOutput ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "running"
  /\ commandLaunched
  /\ output = "open"
  /\ output' = "closed"
  /\ UNCHANGED <<phase, launch, commandLaunched, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

RequestCancel ==
  /\ ~ForeignRecord
  /\ owner = "owner" /\ lock = "owner" /\ ownerAlive
  /\ phase = "running"
  /\ ~cancel
  /\ cancel' = TRUE
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, workloadRc, finalRc, outcome, outcomeValue,
              outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

RecordCancel ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "running"
  /\ cancel
  /\ workloadRc = "unknown"
  /\ workloadRc' = "cancelled"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, finalRc, outcome, outcomeValue, outcomeAudit,
              owner, ownerAlive, lock, adopted, adoptedFromTransferable,
              cleanupDone, wasCompleted, handoff>>

PublishOutcome ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase \in {"running", "capturing"}
  /\ outcome = "none"
  /\ outcome' = "published"
  /\ outcomeValue' = IF cancel THEN "cancel"
                      ELSE IF workloadRc = "7" THEN "7" ELSE "unknown"
  /\ outcomeAudit' = outcomeValue'
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, owner,
              ownerAlive, lock, adopted, adoptedFromTransferable,
              cleanupDone, wasCompleted, handoff>>

BeginCapture ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase \in {"running", "creating", "retiring", "blocked"}
  /\ lane = "live"
  /\ (evidence = "none" \/ evidence = "lane")
  /\ (launch # "unknown" \/ UnsafeEvidenceUnknownLaunch)
  /\ (output = "closed" \/ UnsafeEvidenceWithOpenOutput)
  /\ phase' = "capturing"
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

SealLaneEvidence ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "capturing"
  /\ evidence = "none"
  /\ (launch # "unknown" \/ UnsafeEvidenceUnknownLaunch)
  /\ (output = "closed" \/ UnsafeEvidenceWithOpenOutput)
  /\ evidence' = "lane"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

SetCreating ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase \in {"preparing", "running", "retiring", "blocked"}
  /\ lane = "none"
  /\ launch = "not_started"
  /\ phase' = "creating"
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

(* A failed or cancelled create has no guest workload.  Only a known         *)
(* not-started launch may be sealed as unstarted evidence.                   *)
SealUnstartedEvidence ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "creating"
  /\ evidence = "none"
  /\ (lane = "none" \/ (UnsafeUnstartedWhenMaybeStarted /\ lane = "live"))
  /\ (launch = "not_started" \/ UnsafeUnstartedWhenMaybeStarted)
  /\ evidence' = "unstarted"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

PinEvidence ==
  /\ ~ForeignRecord
  /\ Active
  /\ evidence = "lane"
  /\ ~evidencePin
  /\ evidencePin' = TRUE
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin, keep,
              cancel, workloadRc, finalRc, outcome, outcomeValue, outcomeAudit,
              owner, ownerAlive, lock, adopted, adoptedFromTransferable,
              cleanupDone, wasCompleted, handoff>>

ReleaseRunPin ==
  /\ ~ForeignRecord
  /\ Active
  /\ runPin
  /\ (evidence # "none" \/ UnsafeEarlyRelease)
  /\ runPin' = FALSE
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

BeginRetiring ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "capturing"
  /\ (evidence # "none" \/ UnsafeRemoveBeforeEvidence)
  /\ (evidence # "lane" \/ (evidencePin /\ (output = "closed" \/ UnsafeEvidenceWithOpenOutput)))
  /\ phase' = "retiring"
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

RetireLane ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase = "retiring"
  /\ lane = "live"
  /\ ( (evidence # "none"
        /\ (evidence # "lane"
            \/ (evidencePin /\ (output = "closed" \/ UnsafeEvidenceWithOpenOutput))))
      \/ UnsafeRemoveBeforeEvidence)
  /\ lane' = "retired"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

CompleteRun ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase \in {"creating", "running", "capturing", "retiring", "blocked"}
  /\ lane \in {"none", "retired"}
  /\ (evidence # "none" \/ UnsafeCompleteWithoutEvidence)
  /\ (evidence # "lane" \/ (evidencePin /\ (output = "closed" \/ UnsafeEvidenceWithOpenOutput)))
  /\ (evidence = "unstarted" => lane = "none")
  /\ (evidence = "unstarted" => ~commandLaunched)
  /\ phase' = "completed"
  /\ cleanupDone' = TRUE
  /\ wasCompleted' = TRUE
  /\ finalRc' = IF cancel \/ workloadRc = "cancelled"
                  THEN (IF UnsafeFabricatePass THEN "0" ELSE "cancel")
                  ELSE (IF workloadRc = "7" THEN "7" ELSE "1")
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, outcome, outcomeValue,
              outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, handoff>>

KeepRun ==
  /\ ~ForeignRecord
  /\ Active
  /\ keep
  /\ phase \in {"creating", "running"}
  /\ lane = "live"
  /\ phase' = "retained"
  /\ output' = "closed"
  /\ finalRc' = IF cancel \/ workloadRc = "cancelled" THEN "cancel"
                 ELSE IF workloadRc = "7" THEN "7" ELSE "1"
  /\ UNCHANGED <<launch, commandLaunched, lane, evidence, runPin, evidencePin,
              keep, cancel, workloadRc, outcome, outcomeValue, outcomeAudit,
              owner, ownerAlive, lock, adopted, adoptedFromTransferable,
              cleanupDone, wasCompleted, handoff>>

FailBlocked ==
  /\ ~ForeignRecord
  /\ Active
  /\ phase \in {"creating", "running", "capturing", "retiring"}
  /\ phase' = "blocked"
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

(***************************************************************************)
(* Owner exit and recovery.                                                 *)
(***************************************************************************)

(* A still-live owner may release the flock; a free lease alone must not      *)
(* authorize takeover of an active phase.                                    *)
OwnerLinger ==
  /\ owner = "owner" /\ ownerAlive /\ lock = "owner"
  /\ phase \in LivePhases
  /\ lock' = "none"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

CrashOwner ==
  /\ owner = "owner" /\ ownerAlive
  /\ ownerAlive' = FALSE
  /\ lock' = "none"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

EndOwnership ==
  /\ owner = "owner" /\ lock = "owner"
  /\ phase \in {"completed", "retained", "blocked"}
  /\ ownerAlive' = FALSE
  /\ lock' = "none"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

EndRecovery ==
  /\ adopted /\ lock = "recoverer"
  /\ phase \in {"completed", "retained", "blocked"}
  /\ lock' = "none"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

RecoverAcquire ==
  /\ ~ForeignRecord
  /\ owner # "recoverer"
  /\ ~adopted
  /\ lock = "none"
  /\ phase \in LivePhases
  /\ (~ownerAlive \/ TransferPhase \/ UnsafeAdoptLiveOwner)
  /\ lock' = "recoverer"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

RecoverAdopt ==
  /\ ~ForeignRecord
  /\ lock = "recoverer"
  /\ ~adopted
  /\ adopted' = TRUE
  /\ owner' = "recoverer"
  /\ adoptedFromTransferable' = TransferPhase
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, ownerAlive, lock, cleanupDone,
              wasCompleted, handoff>>

RecoverReleaseRetained ==
  /\ ~ForeignRecord
  /\ adopted
  /\ phase = "retained"
  /\ phase' = "retiring"
  /\ keep' = FALSE
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, cancel, workloadRc, finalRc, outcome, outcomeValue,
              outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

(* Recovery is itself crashable; the run lease is released and another        *)
(* recoverer may acquire it.  The adopted record identity is unchanged.       *)
CrashRecoverer ==
  /\ adopted /\ lock = "recoverer"
  /\ lock' = "none"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

RecoverReacquire ==
  /\ adopted /\ owner = "recoverer" /\ lock = "none"
  /\ lock' = "recoverer"
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

(* Destruction of the command's writers closes the pipes even when the       *)
(* owner process died.  It grants no evidence: capture still has to prove    *)
(* the launch outcome and seal the bytes.                                    *)
RecoverCloseOutput ==
  /\ ~ForeignRecord
  /\ adopted
  /\ output = "open"
  /\ output' = "closed"
  /\ UNCHANGED <<phase, launch, commandLaunched, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

(* Composition boundary with audits/retirement-model: the completed run, its *)
(* sealed evidence and its held reference are exactly Retirement.tla's       *)
(* initial evidence/pin state.  This model performs no journal, pin release, *)
(* image census or reader-lease action after handoff.                        *)
Handoff ==
  /\ ~ForeignRecord
  /\ ~handoff
  /\ phase = "completed"
  /\ evidence # "none"
  /\ (evidence # "lane" \/ (evidencePin /\ lane = "retired"))
  /\ handoff' = TRUE
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted>>

(***************************************************************************)
(* Intentionally wrong arms: each enabled by exactly one unsafe constant.    *)
(***************************************************************************)

DowngradeUnknown ==
  /\ UnsafeDowngradeUnknown
  /\ launch = "unknown"
  /\ launch' = "not_started"
  /\ UNCHANGED <<phase, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

FabricateStarted ==
  /\ UnsafeFabricateStarted
  /\ launch = "not_started"
  /\ ~commandLaunched
  /\ launch' = "started"
  /\ UNCHANGED <<phase, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

DropEvidencePin ==
  /\ UnsafeDropEvidencePin
  /\ evidence = "lane"
  /\ evidencePin
  /\ evidencePin' = FALSE
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin, keep,
              cancel, workloadRc, finalRc, outcome, outcomeValue, outcomeAudit,
              owner, ownerAlive, lock, adopted, adoptedFromTransferable,
              cleanupDone, wasCompleted, handoff>>

OverwriteOutcome ==
  /\ UnsafeOverwriteOutcome
  /\ outcome = "published"
  /\ \E value \in {"0", "7", "cancel"} : value # outcomeValue /\ outcomeValue' = value
  /\ UNCHANGED <<phase, launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

AdoptForeign ==
  /\ UnsafeAdoptForeignRecord
  /\ ForeignRecord
  /\ phase' = "preparing"
  /\ owner' = "recoverer"
  /\ lock' = "recoverer"
  /\ adopted' = TRUE
  /\ adoptedFromTransferable' = FALSE
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, ownerAlive, cleanupDone,
              wasCompleted, handoff>>

RevertCompleted ==
  /\ UnsafeRevertCompleted
  /\ phase = "completed"
  /\ phase' = "running"
  /\ UNCHANGED <<launch, commandLaunched, output, lane, evidence, runPin,
              evidencePin, keep, cancel, workloadRc, finalRc, outcome,
              outcomeValue, outcomeAudit, owner, ownerAlive, lock, adopted,
              adoptedFromTransferable, cleanupDone, wasCompleted, handoff>>

Next ==
  \/ PrepareRecord \/ PinRun \/ CreateLane \/ BeginRun \/ LaunchCommand
  \/ RecordStarted \/ RunWorkload \/ CloseOutput \/ RequestCancel \/ RecordCancel
  \/ PublishOutcome \/ BeginCapture \/ SealLaneEvidence \/ SetCreating
  \/ SealUnstartedEvidence \/ PinEvidence \/ ReleaseRunPin \/ BeginRetiring
  \/ RetireLane \/ CompleteRun \/ KeepRun \/ FailBlocked \/ OwnerLinger
  \/ CrashOwner \/ EndOwnership \/ EndRecovery \/ RecoverAcquire
  \/ RecoverAdopt \/ RecoverReleaseRetained \/ CrashRecoverer
  \/ RecoverReacquire \/ RecoverCloseOutput \/ Handoff
  \/ DowngradeUnknown \/ FabricateStarted \/ DropEvidencePin
  \/ OverwriteOutcome \/ AdoptForeign \/ RevertCompleted

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Safety properties.                                                       *)
(***************************************************************************)

(* No persisted not-started claim can hide a launched command, and a started  *)
(* claim needs the launch fact.                                              *)
NoFabricatedLaunch ==
  /\ (launch = "not_started") => ~commandLaunched
  /\ (launch = "started") => commandLaunched

(* An unknown launch is never sealed as evidence; the unsafe arm shows the   *)
(* opposite.  Evidence requires a known launch outcome.                      *)
NoEvidenceWithUnknownLaunch == (evidence # "none") => launch # "unknown"
NoUnstartedEvidenceWithoutNotStarted == (evidence = "unstarted") => launch = "not_started"
NoUnstartedEvidenceWhenMaybeStarted == (evidence = "unstarted") => ~commandLaunched

(* Irreversible native removal needs closed writers and sealed evidence.      *)
NoEvidenceBeforeClosedOutput == evidence = "lane" => output = "closed"
NoRemovalBeforeEvidence ==
  lane = "retired" =>
    /\ evidence # "none"
    /\ (evidence # "lane" \/ (output = "closed" /\ evidencePin))

(* A completed run proves evidence, cleanup and the immutable final result.   *)
NoCompletedWithoutEvidence ==
  phase = "completed" => (evidence # "none" /\ cleanupDone /\ wasCompleted)
CompletedTerminal == wasCompleted => phase = "completed"

(* Reference continuity: a live lane hangs onto the run reference until       *)
(* evidence exists; sealed evidence keeps at least one reference until the    *)
(* retirement handoff.                                                      *)
NoReferenceGap == (lane = "live" /\ evidence = "none") => runPin
NoEvidenceReferenceDrop ==
  (evidence = "lane" /\ phase \in {"retiring", "completed"}) => evidencePin
HandoffReady ==
  handoff =>
    /\ phase = "completed"
    /\ evidence # "none"
    /\ (evidence # "lane" \/ (evidencePin /\ lane = "retired"))

(* Outcome publication is write-once; completed is not workload PASS.         *)
OutcomeImmutable == outcome = "published" => outcomeValue = outcomeAudit
NoFabricatedPass ==
  /\ (phase = "completed" /\ (cancel \/ workloadRc = "cancelled")) => finalRc = "cancel"
  /\ (phase = "completed" /\ ~cancel /\ workloadRc # "cancelled" /\ workloadRc # "7") =>
       finalRc = "1"

(* Recovery never takes over an active owner outside the explicit retained or *)
(* blocked transfer phases, and never adopts an unreadable/foreign record.    *)
NoStolenLiveOwner ==
  /\ (~adopted /\ lock = "recoverer") =>
       (~ownerAlive \/ phase \in {"retained", "blocked"})
  /\ (adopted /\ ~adoptedFromTransferable) => ~ownerAlive
ForeignPreserved == ForeignRecord => phase = "unknown"

=============================================================================

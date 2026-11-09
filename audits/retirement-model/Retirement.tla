--------------------------- MODULE Retirement ---------------------------
(***************************************************************************)
(* Bounded crash/recovery model of the cross-module managed-run retirement  *)
(* protocol: evidence journal, external runtime references, acknowledgement,*)
(* reader leases and image retention.  One run references image "i0"; image *)
(* "i1" is the newest publication and has no run.  Every persistent mutation *)
(* is a separate action, so every reachable state is a possible crash point  *)
(* and the enabled actions from that state are exactly the restart options.  *)
(* The model has no fairness or liveness claim.  See README.md.              *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS UnsafeGcBeforeAck, UnsafeAckBeforeRelease, UnsafeEarlyRelease,
          UnsafeReaderIgnored, UnsafeNewestEviction, UnsafeRegistrationDrop,
          UnsafeForeignRegistration

Runs == {"r1"}
Images == {"i0", "i1"}
Newest == "i1"
runImage == [r \in Runs |-> "i0"]
RunOf(i) == {r \in Runs : runImage[r] = i}

VARIABLES evidence, journal, pinRun, pinEvidence, ack, image, catalog, reader,
          owner, earlyRelease, foreignTrash

vars == <<evidence, journal, pinRun, pinEvidence, ack, image, catalog, reader,
          owner, earlyRelease, foreignTrash>>

Init == /\ evidence = [r \in Runs |-> "present"]
        /\ journal = [r \in Runs |-> "none"]
        /\ pinRun = [r \in Runs |-> TRUE]
        /\ pinEvidence = [r \in Runs |-> FALSE]
        /\ ack = [r \in Runs |-> FALSE]
        /\ image = [i \in Images |-> "published"]
        /\ catalog = [i \in Images |-> "present"]
        /\ reader = [i \in Images |-> FALSE]
        /\ owner = [i \in Images |-> TRUE]
        /\ earlyRelease = FALSE
        /\ foreignTrash = FALSE

(***************************************************************************)
(* Derived obligations.                                                      *)
(***************************************************************************)
PinsFree(i) == \A r \in RunOf(i) : ~pinRun[r] /\ ~pinEvidence[r]

EvidenceClear(i) == \A r \in RunOf(i) : /\ evidence[r] = "removed"
                                        /\ journal[r] = "none"
                                        /\ ack[r]

(***************************************************************************)
(* Evidence side.  Production mapping in README.md.                          *)
(***************************************************************************)

(* managed_run._finish: evidence pin published before the run pin is released *)
PinEvidence(r) == /\ evidence[r] = "present" /\ pinRun[r] /\ ~pinEvidence[r]
                  /\ pinEvidence' = [pinEvidence EXCEPT ![r] = TRUE]
                  /\ UNCHANGED <<evidence, journal, pinRun, ack, image, catalog,
                                  reader, owner, earlyRelease, foreignTrash>>

(* _finish / collect_retired: the run pin may be released only after the
   evidence pin took over, or after the evidence bytes are actually gone *)
ReleaseRunPin(r) == /\ pinRun[r]
                    /\ (pinEvidence[r] \/ evidence[r] = "removed")
                    /\ pinRun' = [pinRun EXCEPT ![r] = FALSE]
                    /\ UNCHANGED <<evidence, journal, pinEvidence, ack, image,
                                    catalog, reader, owner, earlyRelease, foreignTrash>>

(* evidence_retention._retire_run: durable planned journal before any rename *)
EvidencePlan(r) == /\ evidence[r] = "present" /\ journal[r] = "none"
                   /\ journal' = [journal EXCEPT ![r] = "planned"]
                   /\ UNCHANGED <<evidence, pinRun, pinEvidence, ack, image,
                                   catalog, reader, owner, earlyRelease, foreignTrash>>

(* _retire_run: rename of the run directory into journaled trash *)
EvidenceDetach(r) == /\ journal[r] = "planned" /\ evidence[r] = "present"
                     /\ evidence' = [evidence EXCEPT ![r] = "detached"]
                     /\ UNCHANGED <<journal, pinRun, pinEvidence, ack, image,
                                     catalog, reader, owner, earlyRelease, foreignTrash>>

(* _retire_run / _reconcile_retirement: durable detached phase after the rename *)
EvidenceWriteDetached(r) == /\ journal[r] = "planned" /\ evidence[r] = "detached"
                            /\ journal' = [journal EXCEPT ![r] = "detached"]
                            /\ UNCHANGED <<evidence, pinRun, pinEvidence, ack, image,
                                            catalog, reader, owner, earlyRelease,
                                            foreignTrash>>

(* _retire_run / _remove_detached: trash bytes are removed *)
EvidenceDelete(r) == /\ journal[r] = "detached" /\ evidence[r] = "detached"
                     /\ evidence' = [evidence EXCEPT ![r] = "removed"]
                     /\ UNCHANGED <<journal, pinRun, pinEvidence, ack, image,
                                     catalog, reader, owner, earlyRelease, foreignTrash>>

(* _retire_run / _remove_detached: completed ledger after the bytes are gone *)
EvidenceComplete(r) == /\ journal[r] = "detached" /\ evidence[r] = "removed"
                       /\ journal' = [journal EXCEPT ![r] = "completed"]
                       /\ UNCHANGED <<evidence, pinRun, pinEvidence, ack, image,
                                       catalog, reader, owner, earlyRelease, foreignTrash>>

(* collect_retired: release only after the removal ledger completed *)
ReleaseEvidencePin(r) == /\ pinEvidence[r]
                         /\ (UnsafeEarlyRelease \/
                             (evidence[r] = "removed" /\ journal[r] = "completed"))
                         /\ earlyRelease' = (earlyRelease \/
                             ~(evidence[r] = "removed" /\ journal[r] = "completed"))
                         /\ pinEvidence' = [pinEvidence EXCEPT ![r] = FALSE]
                         /\ UNCHANGED <<evidence, journal, pinRun, ack, image,
                                         catalog, reader, owner, foreignTrash>>

(* collect_retired: acknowledgement requires removed bytes, completed journal
   and every runtime reference already released *)
Ack(r) == /\ journal[r] = "completed" /\ ~ack[r]
          /\ (UnsafeAckBeforeRelease \/
              (~pinEvidence[r] /\ ~pinRun[r] /\ evidence[r] = "removed"))
          /\ ack' = [ack EXCEPT ![r] = TRUE]
          /\ journal' = [journal EXCEPT ![r] = "none"]
          /\ UNCHANGED <<evidence, pinRun, pinEvidence, image, catalog, reader,
                          owner, earlyRelease, foreignTrash>>

(***************************************************************************)
(* Image side.  Recovery reuses the same actions from the persisted state.   *)
(***************************************************************************)

(* runtime_retention._retire_and_remove: journaled retirement under the locks *)
ImageRetire(i) == /\ image[i] = "published"
                  /\ catalog[i] = "present"
                  /\ PinsFree(i)
                  /\ (UnsafeReaderIgnored \/ ~reader[i])
                  /\ (UnsafeNewestEviction \/ i # Newest)
                  /\ (UnsafeForeignRegistration \/ owner[i])
                  /\ (UnsafeGcBeforeAck \/ EvidenceClear(i))
                  /\ image' = [image EXCEPT ![i] = "retiring"]
                  /\ UNCHANGED <<evidence, journal, pinRun, pinEvidence, ack,
                                  catalog, reader, owner, earlyRelease, foreignTrash>>

(* _detach_and_remove / _recover_retirement: rename to the journaled trash *)
ImageDetach(i) == /\ image[i] = "retiring"
                  /\ catalog[i] = "present"
                  /\ (UnsafeReaderIgnored \/ ~reader[i])
                  /\ (UnsafeForeignRegistration \/ owner[i])
                  /\ (UnsafeGcBeforeAck \/ EvidenceClear(i))
                  /\ image' = [image EXCEPT ![i] = "detached"]
                  /\ foreignTrash' = (foreignTrash \/ ~owner[i])
                  /\ UNCHANGED <<evidence, journal, pinRun, pinEvidence, ack,
                                  catalog, reader, owner, earlyRelease>>

(* _delete_detached: remove the detached bytes *)
ImageDelete(i) == /\ image[i] = "detached"
                  /\ (UnsafeReaderIgnored \/ ~reader[i])
                  /\ (UnsafeForeignRegistration \/ owner[i])
                  /\ (UnsafeGcBeforeAck \/ EvidenceClear(i))
                  /\ image' = [image EXCEPT ![i] = "gone"]
                  /\ foreignTrash' = (foreignTrash \/ ~owner[i])
                  /\ UNCHANGED <<evidence, journal, pinRun, pinEvidence, ack,
                                  catalog, reader, owner, earlyRelease>>

(* _delete_detached / _recover_retirement: clear the matching registration *)
ImageClearRecord(i) == /\ catalog[i] = "present"
                       /\ (UnsafeRegistrationDrop \/ image[i] = "gone")
                       /\ catalog' = [catalog EXCEPT ![i] = "cleared"]
                       /\ UNCHANGED <<evidence, journal, pinRun, pinEvidence, ack,
                                       image, reader, owner, earlyRelease, foreignTrash>>

(* A concurrent image-use lease.  It can start only against a published image. *)
ReaderEnter(i) == /\ image[i] = "published" /\ ~reader[i]
                  /\ reader' = [reader EXCEPT ![i] = TRUE]
                  /\ UNCHANGED <<evidence, journal, pinRun, pinEvidence, ack, image,
                                  catalog, owner, earlyRelease, foreignTrash>>

ReaderLeave(i) == /\ reader[i]
                  /\ reader' = [reader EXCEPT ![i] = FALSE]
                  /\ UNCHANGED <<evidence, journal, pinRun, pinEvidence, ack, image,
                                  catalog, owner, earlyRelease, foreignTrash>>

(* A foreign generation replaces the bytes at the recorded image root or in the
   journaled detached trash; ownership checks must not adopt them. *)
ReplaceImage(i) == /\ image[i] \in {"published", "detached"}
                   /\ owner[i]
                   /\ owner' = [owner EXCEPT ![i] = FALSE]
                   /\ UNCHANGED <<evidence, journal, pinRun, pinEvidence, ack, image,
                                   catalog, reader, earlyRelease, foreignTrash>>

(***************************************************************************)
(* Crash is a stutter on every persisted variable: the state survives and the *)
(* next action is a restart decision, not an in-memory continuation.          *)
(***************************************************************************)
Crash == UNCHANGED vars

Next == \/ (\E r \in Runs : PinEvidence(r) \/ ReleaseRunPin(r) \/ EvidencePlan(r)
                          \/ EvidenceDetach(r) \/ EvidenceWriteDetached(r)
                          \/ EvidenceDelete(r) \/ EvidenceComplete(r)
                          \/ ReleaseEvidencePin(r) \/ Ack(r))
        \/ (\E i \in Images : ImageRetire(i) \/ ImageDetach(i) \/ ImageDelete(i)
                          \/ ImageClearRecord(i) \/ ReaderEnter(i)
                          \/ ReaderLeave(i) \/ ReplaceImage(i))
        \/ Crash

Spec == Init /\ [][Next]_vars

TypeOK == /\ evidence \in [Runs -> {"present", "detached", "removed"}]
          /\ journal \in [Runs -> {"none", "planned", "detached", "completed"}]
          /\ pinRun \in [Runs -> BOOLEAN]
          /\ pinEvidence \in [Runs -> BOOLEAN]
          /\ ack \in [Runs -> BOOLEAN]
          /\ image \in [Images -> {"published", "retiring", "detached", "gone"}]
          /\ catalog \in [Images -> {"present", "cleared"}]
          /\ reader \in [Images -> BOOLEAN]
          /\ owner \in [Images -> BOOLEAN]
          /\ earlyRelease \in BOOLEAN
          /\ foreignTrash \in BOOLEAN

(***************************************************************************)
(* Required order and protection invariants.                                 *)
(***************************************************************************)

(* Image bytes may leave "published" only after every run record and journal
   for that image was removed and acknowledged. *)
NoReferenceDrop == \A i \in Images : image[i] # "published" => EvidenceClear(i)

(* Acknowledgement never precedes release of the evidence and run references. *)
NoAckBeforeRelease == \A r \in Runs : ack[r] => (~pinEvidence[r] /\ ~pinRun[r])

(* The evidence pin is released only after its removal ledger completed. *)
NoReleaseBeforeRemoval == ~earlyRelease

(* No image is retired while an image-use reader lease still holds it. *)
NoGcWithReader == \A i \in Images : image[i] # "published" => ~reader[i]

(* Newest-publication protection is independent of the retention policy rank. *)
NoNewestEviction == image[Newest] = "published"

(* A registration is cleared only after its image bytes are gone. *)
NoRegistrationDrop == \A i \in Images : catalog[i] = "cleared" => image[i] = "gone"

(* Bytes whose generation does not match their journaled identity are never
   detached or deleted as if they belonged to this registration. *)
NoForeignTrash == ~foreignTrash

=============================================================================

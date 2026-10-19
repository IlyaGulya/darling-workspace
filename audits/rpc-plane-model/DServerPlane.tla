------------------------------ MODULE DServerPlane ------------------------------
(***************************************************************************)
(* A small model of ONE accepted request's disposition in darlingserver, at *)
(* the granularity the audit needs: who owns the request, when it runs,     *)
(* when (and whether) its terminal disposition is published, and what the   *)
(* client does while it waits.                                              *)
(*                                                                          *)
(* It models the DEFERRED-CALL MACHINERY (`Thread::setPendingCall`,         *)
(* `Thread::dispatchOrQueuePlaneCall`, `Thread::doWork`'s                  *)
(* `_deferPendingCallLocked` and `doneWorking` drain, `pushCallReply`),     *)
(* not the wire format. Four switches select the variants:                  *)
(*                                                                          *)
(*  ArmWithoutOwner                                                         *)
(*    FALSE: the guarded protocol - a Call the thread cannot run is QUEUED  *)
(*           under the thread's lock with a completion owner.               *)
(*    TRUE : `dispatchOrQueuePlaneCall(call, queueIfBusy=false)` -          *)
(*           `Call::callFromMessage` -> `Thread::setPendingCall` ARMS the   *)
(*           Call and the dispatch returns false WITHOUT disarming it,      *)
(*           while the plane records that it left the request PENDING for   *)
(*           a later pass.                                                  *)
(*  GenGuard : `page->request_seq == seq` before the completion stores.     *)
(*  ParkedCompletesExternally                                               *)
(*    TRUE : the parked (blocking) Call's completion is an EXTERNAL event    *)
(*           (the guest's message arrives, a timeout expires, a signal is    *)
(*           delivered) that requires nothing from any deferred Call.        *)
(*    FALSE: the parked Call completes only once the thread's deferred queue *)
(*           is empty - the "internal circular wait" hypothesis.             *)
(***************************************************************************)

EXTENDS Naturals, Sequences, TLC

CONSTANTS Threads, Clients, ArmWithoutOwner, GenGuard,
          ParkedCompletesExternally, ClientReclaimsOnTimeout, ArmAfterQueue

ASSUME ArmWithoutOwner \in BOOLEAN /\ GenGuard \in BOOLEAN
ASSUME ParkedCompletesExternally \in BOOLEAN
ASSUME ClientReclaimsOnTimeout \in BOOLEAN
ASSUME ArmAfterQueue \in BOOLEAN
ASSUME Threads # {} /\ Clients # {}

(* two attempts of ONE logical operation: attempt 1 is the request a plane
   dispatch can leave armed on a parked thread, attempt 2 is the caller's
   bounded republish of the same operation *)
(* the blocking call a thread of the process is ALREADY inside - the state
   the evidence log records as `active_call=38 suspended=1` *)
ParkReq == 1
(* the two attempts of ONE logical plane operation *)
Req1 == 2
Req2 == 3
RqSeq == <<Req1, Req2>>
Requests == {ParkReq, Req1, Req2}

NoReq == 0

(* REAL-PATH INSTANTIATION (regression `tests/run-plane-deferred-arm-contract.sh`):
   ParkReq = semaphore_timedwait   (callnum 62; the fixture's main thread)
   Req1    = console_open          (callnum 16; the fixture's first open)
   Req2    = console_open retry    (the same operation, re-published after the
                                    plane's bounded wait reclaims the slot)
   ArmWithoutOwner = TRUE is the production shape of
   `dispatchOrQueuePlaneCall(call, false)`; the fixed server is FALSE.      *)

(*--algorithm dserver

variables
  owner      = [r \in Requests |-> "idle"],
  waiting    = [c \in Clients |-> NoReq],
  applied    = [r \in Requests |-> 0],
  (* terminal dispositions DELIVERED to the caller: the reply datagram, the
     lane reply slot, or the management page's `reply_state = DONE` *)
  published  = [r \in Requests |-> FALSE],
  (* completions DISCARDED because the page slot had moved on *)
  staleStore = [r \in Requests |-> 0],
  (* UNGUARDED completions that landed in a slot a LATER request owns *)
  foreignWrite = 0,
  armed      = [t \in Threads |-> NoReq],
  active     = [t \in Threads |-> NoReq],
  parked     = [t \in Threads |-> FALSE],
  deferred   = [t \in Threads |-> <<>>],
  (* the plane's `region.deferredSeq` memo *)
  memo       = [t \in Threads |-> NoReq],
  (* `Call::PlaneCompletion.armed()` *)
  pcArmed    = [r \in Requests |-> FALSE],
  (* TRUE for a Call whose completion owner has NOT been armed yet, i.e. the
     arm-after-queue window: the Call is reachable from the deferred queue
     before `setPlaneCompletion` ran. The FIXED protocol never sets it. *)
  pendingArm = [r \in Requests |-> FALSE],
  slot       = NoReq,
  slotDone   = FALSE,
  attempts   = [c \in Clients |-> 0];

define
  Superseded(r) ==
      \E c \in Clients : \E i \in 1..Len(RqSeq) :
          RqSeq[i] = r /\ i < Len(RqSeq) /\ attempts[c] > i

  (* Inv2: no completion may be stored into a slot a LATER request owns *)
  Inv2NoWrongGeneration == foreignWrite = 0

  (* Inv3: an accepted request whose handler ran must receive a terminal
     disposition - never a silent finish - unless its caller has already
     superseded it with a newer attempt *)
  Inv3NoSilentLoss == \A r \in Requests :
      (owner[r] = "done" /\ ~Superseded(r)) => published[r]

  (* Inv4: a mutating operation is applied at most once across the client's
     two attempts of it *)
  Inv4NoDuplicateApply ==
      \A i, j \in 1..Len(RqSeq) :
          (i # j) => ~(applied[RqSeq[i]] > 0 /\ applied[RqSeq[j]] > 0)

  (* Inv6: an internal circular wait. A parked thread carrying queued work
     whose only completion channel IS the parked call is the cycle the
     deferred mechanism must not be able to create; it is benign only under
     the external-event assumption that the parked call completes on its
     own. *)
  Inv6NoInternalCycle ==
      \A t \in Threads :
          ~(parked[t] /\ Len(deferred[t]) > 0 /\ ~ParkedCompletesExternally)

  (* Inv7: THE ATOMICITY REQUIREMENT for the arm-after-queue race. A Call that
     is reachable from the deferred queue must already carry its completion
     owner; a queued Call still waiting for `setPlaneCompletion` is the
     window in which a worker can run it and drop its answer. *)
  Inv7QueuedCallHasOwner ==
      \A r \in Requests :
          ~(owner[r] = "deferred" /\ pendingArm[r])

  (* Inv5: a DONE slot the caller is waiting on must belong to that caller's
     published request *)
  Inv5NoForeignReply == \A c \in Clients :
      (waiting[c] # NoReq /\ slot = waiting[c] /\ slotDone) =>
          published[waiting[c]]
end define;

begin
MainLoop: while (TRUE) do

  either
    (* A thread of the process is ALREADY parked inside a blocking call: a
       guest thread blocked in a mach_msg receive / semaphore wait / psynch
       wait. This is the state the audit's evidence records as
       `active_call=38 suspended=1`, and the blocking call is itself an
       ordinary accepted request. *)
    AExternalPark:
    with t \in Threads do
      if (active[t] = NoReq /\ ~parked[t] /\ owner[ParkReq] = "idle") then
        owner[ParkReq] := "running";
        active[t] := ParkReq;
        applied[ParkReq] := applied[ParkReq] + 1;
        pcArmed[ParkReq] := TRUE;
        parked[t] := TRUE;
      end if;
    end with;

  or
    AAcceptIdle:
    with t \in Threads do
      with r \in Requests do
        if (owner[r] = "transit" /\ armed[t] = NoReq
              /\ active[t] = NoReq /\ ~parked[t]) then
          armed[t] := r;
          owner[r] := "armed";
          pcArmed[r] := TRUE;
        end if;
      end with;
    end with;

  or
    AAcceptBusy:
    with t \in Threads do
      with r \in Requests do
        if (owner[r] = "transit"
              /\ (parked[t] \/ active[t] # NoReq \/ armed[t] # NoReq)) then
          if (ArmWithoutOwner) then
            if (armed[t] = NoReq) then
              armed[t] := r;
              owner[r] := "armed";
              memo[t] := r;
              pcArmed[r] := FALSE;
            end if;
          else
            deferred[t] := Append(deferred[t], r);
            owner[r] := "deferred";
            (* ArmAfterQueue: the production ordering -- the Call is
               reachable from the queue BEFORE the pass arms its completion.
               The fixed protocol arms here, before publication. *)
            pcArmed[r] := ~ArmAfterQueue;
            pendingArm[r] := ArmAfterQueue;
          end if;
        end if;
      end with;
    end with;

  or
    AContinuationCompletes:
    with t \in Threads do
      if (parked[t]
            /\ (ParkedCompletesExternally \/ Len(deferred[t]) = 0)) then
        parked[t] := FALSE;
        if (active[t] # NoReq) then
          (* the parked call's disposition goes over ITS OWN transport (the
             datagram / lane it arrived on), not the management page, so it
             must not touch the page slot *)
          if (pcArmed[active[t]]) then
            published[active[t]] := TRUE;
          end if;
          owner[active[t]] := "done";
          active[t] := NoReq;
        end if;
      end if;
    end with;

  or
    ADoWorkDefers:
    with t \in Threads do
      if (~parked[t] /\ active[t] = NoReq /\ armed[t] # NoReq
            /\ owner[armed[t]] = "armed" /\ memo[t] # NoReq) then
        deferred[t] := Append(deferred[t], armed[t]);
        owner[armed[t]] := "deferred";
        armed[t] := NoReq;
      end if;
    end with;

  or
    ADrainDeferred:
    with t \in Threads do
      if (~parked[t] /\ active[t] = NoReq /\ armed[t] = NoReq
            /\ Len(deferred[t]) > 0) then
        armed[t] := Head(deferred[t]);
        owner[Head(deferred[t])] := "armed";
        deferred[t] := Tail(deferred[t]);
      end if;
    end with;

  or
    ARunCall:
    with t \in Threads do
      if (armed[t] # NoReq /\ active[t] = NoReq /\ ~parked[t]) then
        active[t] := armed[t];
        owner[armed[t]] := "running";
        applied[armed[t]] := applied[armed[t]] + 1;
        armed[t] := NoReq;
      end if;
    end with;

  or
    AParkCall:
    with t \in Threads do
      if (active[t] # NoReq /\ ~parked[t]) then
        parked[t] := TRUE;
      end if;
    end with;

  or
    AFinishCall:
    with t \in Threads do
      if (active[t] # NoReq /\ ~parked[t]) then
        with r = active[t] do
          owner[r] := "done";
          active[t] := NoReq;
          if (memo[t] = r) then memo[t] := NoReq end if;
          if (pcArmed[r]) then
            if (GenGuard /\ slot # NoReq /\ slot # r) then
              staleStore[r] := staleStore[r] + 1;
            else
              if (~GenGuard /\ slot # NoReq /\ slot # r) then
                foreignWrite := foreignWrite + 1;
              end if;
              slot := r;
              slotDone := TRUE;
              published[r] := TRUE;
            end if;
          end if;
        end with;
      end if;
    end with;

  or
    (* the pass arms the completion of a Call it already queued
       (`setPlaneCompletion` after `dispatchOrQueuePlaneCall` returned) *)
    AArmCompletion:
    with r \in Requests do
      if (pendingArm[r]) then
        pendingArm[r] := FALSE;
        pcArmed[r] := TRUE;
      end if;
    end with;

  or
    APublish:
    with c \in Clients do
      if (attempts[c] = 0) then
        with r = RqSeq[attempts[c] + 1] do
          if (owner[r] = "idle") then
            owner[r] := "transit";
            waiting[c] := r;
            slot := r;
            slotDone := FALSE;
            attempts[c] := attempts[c] + 1;
          end if;
        end with;
      end if;
    end with;

  or
    (* the caller's bounded republish: enabled only for a request the server
       finished WITHOUT any disposition (the silent finish). A request still
       in flight is not republished: the server accepts every published
       request (it polls every pass), so a republish while an attempt is
       outstanding would be a protocol violation by the caller, not by the
       server. *)
    ARepublish:
    with c \in Clients do
      if (attempts[c] < Len(RqSeq) /\ attempts[c] > 0 /\ waiting[c] # NoReq
            /\ ~published[waiting[c]]
            /\ (ClientReclaimsOnTimeout
                  \/ owner[waiting[c]] = "done")) then
        with r = RqSeq[attempts[c] + 1] do
          if (owner[r] = "idle") then
            owner[r] := "transit";
            waiting[c] := r;
            slot := r;
            slotDone := FALSE;
            attempts[c] := attempts[c] + 1;
          end if;
        end with;
      end if;
    end with;

  or
    AReadReply:
    with c \in Clients do
      if (waiting[c] # NoReq /\ slot = waiting[c] /\ slotDone) then
        waiting[c] := NoReq;
      end if;
    end with;
  end either;

end while;
end algorithm; *)
\* BEGIN TRANSLATION (chksum(pcal) = "d27287e6" /\ chksum(tla) = "83356058")
VARIABLES pc, owner, waiting, applied, published, staleStore, foreignWrite, 
          armed, active, parked, deferred, memo, pcArmed, pendingArm, slot, 
          slotDone, attempts

(* define statement *)
Superseded(r) ==
    \E c \in Clients : \E i \in 1..Len(RqSeq) :
        RqSeq[i] = r /\ i < Len(RqSeq) /\ attempts[c] > i


Inv2NoWrongGeneration == foreignWrite = 0




Inv3NoSilentLoss == \A r \in Requests :
    (owner[r] = "done" /\ ~Superseded(r)) => published[r]



Inv4NoDuplicateApply ==
    \A i, j \in 1..Len(RqSeq) :
        (i # j) => ~(applied[RqSeq[i]] > 0 /\ applied[RqSeq[j]] > 0)






Inv6NoInternalCycle ==
    \A t \in Threads :
        ~(parked[t] /\ Len(deferred[t]) > 0 /\ ~ParkedCompletesExternally)





Inv7QueuedCallHasOwner ==
    \A r \in Requests :
        ~(owner[r] = "deferred" /\ pendingArm[r])



Inv5NoForeignReply == \A c \in Clients :
    (waiting[c] # NoReq /\ slot = waiting[c] /\ slotDone) =>
        published[waiting[c]]


vars == << pc, owner, waiting, applied, published, staleStore, foreignWrite, 
           armed, active, parked, deferred, memo, pcArmed, pendingArm, slot, 
           slotDone, attempts >>

Init == (* Global variables *)
        /\ owner = [r \in Requests |-> "idle"]
        /\ waiting = [c \in Clients |-> NoReq]
        /\ applied = [r \in Requests |-> 0]
        /\ published = [r \in Requests |-> FALSE]
        /\ staleStore = [r \in Requests |-> 0]
        /\ foreignWrite = 0
        /\ armed = [t \in Threads |-> NoReq]
        /\ active = [t \in Threads |-> NoReq]
        /\ parked = [t \in Threads |-> FALSE]
        /\ deferred = [t \in Threads |-> <<>>]
        /\ memo = [t \in Threads |-> NoReq]
        /\ pcArmed = [r \in Requests |-> FALSE]
        /\ pendingArm = [r \in Requests |-> FALSE]
        /\ slot = NoReq
        /\ slotDone = FALSE
        /\ attempts = [c \in Clients |-> 0]
        /\ pc = "MainLoop"

MainLoop == /\ pc = "MainLoop"
            /\ \/ /\ pc' = "AExternalPark"
               \/ /\ pc' = "AAcceptIdle"
               \/ /\ pc' = "AAcceptBusy"
               \/ /\ pc' = "AContinuationCompletes"
               \/ /\ pc' = "ADoWorkDefers"
               \/ /\ pc' = "ADrainDeferred"
               \/ /\ pc' = "ARunCall"
               \/ /\ pc' = "AParkCall"
               \/ /\ pc' = "AFinishCall"
               \/ /\ pc' = "AArmCompletion"
               \/ /\ pc' = "APublish"
               \/ /\ pc' = "ARepublish"
               \/ /\ pc' = "AReadReply"
            /\ UNCHANGED << owner, waiting, applied, published, staleStore, 
                            foreignWrite, armed, active, parked, deferred, 
                            memo, pcArmed, pendingArm, slot, slotDone, 
                            attempts >>

AExternalPark == /\ pc = "AExternalPark"
                 /\ \E t \in Threads:
                      IF (active[t] = NoReq /\ ~parked[t] /\ owner[ParkReq] = "idle")
                         THEN /\ owner' = [owner EXCEPT ![ParkReq] = "running"]
                              /\ active' = [active EXCEPT ![t] = ParkReq]
                              /\ applied' = [applied EXCEPT ![ParkReq] = applied[ParkReq] + 1]
                              /\ pcArmed' = [pcArmed EXCEPT ![ParkReq] = TRUE]
                              /\ parked' = [parked EXCEPT ![t] = TRUE]
                         ELSE /\ TRUE
                              /\ UNCHANGED << owner, applied, active, parked, 
                                              pcArmed >>
                 /\ pc' = "MainLoop"
                 /\ UNCHANGED << waiting, published, staleStore, foreignWrite, 
                                 armed, deferred, memo, pendingArm, slot, 
                                 slotDone, attempts >>

AAcceptIdle == /\ pc = "AAcceptIdle"
               /\ \E t \in Threads:
                    \E r \in Requests:
                      IF (owner[r] = "transit" /\ armed[t] = NoReq
                            /\ active[t] = NoReq /\ ~parked[t])
                         THEN /\ armed' = [armed EXCEPT ![t] = r]
                              /\ owner' = [owner EXCEPT ![r] = "armed"]
                              /\ pcArmed' = [pcArmed EXCEPT ![r] = TRUE]
                         ELSE /\ TRUE
                              /\ UNCHANGED << owner, armed, pcArmed >>
               /\ pc' = "MainLoop"
               /\ UNCHANGED << waiting, applied, published, staleStore, 
                               foreignWrite, active, parked, deferred, memo, 
                               pendingArm, slot, slotDone, attempts >>

AAcceptBusy == /\ pc = "AAcceptBusy"
               /\ \E t \in Threads:
                    \E r \in Requests:
                      IF (owner[r] = "transit"
                            /\ (parked[t] \/ active[t] # NoReq \/ armed[t] # NoReq))
                         THEN /\ IF (ArmWithoutOwner)
                                    THEN /\ IF (armed[t] = NoReq)
                                               THEN /\ armed' = [armed EXCEPT ![t] = r]
                                                    /\ owner' = [owner EXCEPT ![r] = "armed"]
                                                    /\ memo' = [memo EXCEPT ![t] = r]
                                                    /\ pcArmed' = [pcArmed EXCEPT ![r] = FALSE]
                                               ELSE /\ TRUE
                                                    /\ UNCHANGED << owner, 
                                                                    armed, 
                                                                    memo, 
                                                                    pcArmed >>
                                         /\ UNCHANGED << deferred, pendingArm >>
                                    ELSE /\ deferred' = [deferred EXCEPT ![t] = Append(deferred[t], r)]
                                         /\ owner' = [owner EXCEPT ![r] = "deferred"]
                                         /\ pcArmed' = [pcArmed EXCEPT ![r] = ~ArmAfterQueue]
                                         /\ pendingArm' = [pendingArm EXCEPT ![r] = ArmAfterQueue]
                                         /\ UNCHANGED << armed, memo >>
                         ELSE /\ TRUE
                              /\ UNCHANGED << owner, armed, deferred, memo, 
                                              pcArmed, pendingArm >>
               /\ pc' = "MainLoop"
               /\ UNCHANGED << waiting, applied, published, staleStore, 
                               foreignWrite, active, parked, slot, slotDone, 
                               attempts >>

AContinuationCompletes == /\ pc = "AContinuationCompletes"
                          /\ \E t \in Threads:
                               IF (parked[t]
                                     /\ (ParkedCompletesExternally \/ Len(deferred[t]) = 0))
                                  THEN /\ parked' = [parked EXCEPT ![t] = FALSE]
                                       /\ IF (active[t] # NoReq)
                                             THEN /\ IF (pcArmed[active[t]])
                                                        THEN /\ published' = [published EXCEPT ![active[t]] = TRUE]
                                                        ELSE /\ TRUE
                                                             /\ UNCHANGED published
                                                  /\ owner' = [owner EXCEPT ![active[t]] = "done"]
                                                  /\ active' = [active EXCEPT ![t] = NoReq]
                                             ELSE /\ TRUE
                                                  /\ UNCHANGED << owner, 
                                                                  published, 
                                                                  active >>
                                  ELSE /\ TRUE
                                       /\ UNCHANGED << owner, published, 
                                                       active, parked >>
                          /\ pc' = "MainLoop"
                          /\ UNCHANGED << waiting, applied, staleStore, 
                                          foreignWrite, armed, deferred, memo, 
                                          pcArmed, pendingArm, slot, slotDone, 
                                          attempts >>

ADoWorkDefers == /\ pc = "ADoWorkDefers"
                 /\ \E t \in Threads:
                      IF (~parked[t] /\ active[t] = NoReq /\ armed[t] # NoReq
                            /\ owner[armed[t]] = "armed" /\ memo[t] # NoReq)
                         THEN /\ deferred' = [deferred EXCEPT ![t] = Append(deferred[t], armed[t])]
                              /\ owner' = [owner EXCEPT ![armed[t]] = "deferred"]
                              /\ armed' = [armed EXCEPT ![t] = NoReq]
                         ELSE /\ TRUE
                              /\ UNCHANGED << owner, armed, deferred >>
                 /\ pc' = "MainLoop"
                 /\ UNCHANGED << waiting, applied, published, staleStore, 
                                 foreignWrite, active, parked, memo, pcArmed, 
                                 pendingArm, slot, slotDone, attempts >>

ADrainDeferred == /\ pc = "ADrainDeferred"
                  /\ \E t \in Threads:
                       IF (~parked[t] /\ active[t] = NoReq /\ armed[t] = NoReq
                             /\ Len(deferred[t]) > 0)
                          THEN /\ armed' = [armed EXCEPT ![t] = Head(deferred[t])]
                               /\ owner' = [owner EXCEPT ![Head(deferred[t])] = "armed"]
                               /\ deferred' = [deferred EXCEPT ![t] = Tail(deferred[t])]
                          ELSE /\ TRUE
                               /\ UNCHANGED << owner, armed, deferred >>
                  /\ pc' = "MainLoop"
                  /\ UNCHANGED << waiting, applied, published, staleStore, 
                                  foreignWrite, active, parked, memo, pcArmed, 
                                  pendingArm, slot, slotDone, attempts >>

ARunCall == /\ pc = "ARunCall"
            /\ \E t \in Threads:
                 IF (armed[t] # NoReq /\ active[t] = NoReq /\ ~parked[t])
                    THEN /\ active' = [active EXCEPT ![t] = armed[t]]
                         /\ owner' = [owner EXCEPT ![armed[t]] = "running"]
                         /\ applied' = [applied EXCEPT ![armed[t]] = applied[armed[t]] + 1]
                         /\ armed' = [armed EXCEPT ![t] = NoReq]
                    ELSE /\ TRUE
                         /\ UNCHANGED << owner, applied, armed, active >>
            /\ pc' = "MainLoop"
            /\ UNCHANGED << waiting, published, staleStore, foreignWrite, 
                            parked, deferred, memo, pcArmed, pendingArm, slot, 
                            slotDone, attempts >>

AParkCall == /\ pc = "AParkCall"
             /\ \E t \in Threads:
                  IF (active[t] # NoReq /\ ~parked[t])
                     THEN /\ parked' = [parked EXCEPT ![t] = TRUE]
                     ELSE /\ TRUE
                          /\ UNCHANGED parked
             /\ pc' = "MainLoop"
             /\ UNCHANGED << owner, waiting, applied, published, staleStore, 
                             foreignWrite, armed, active, deferred, memo, 
                             pcArmed, pendingArm, slot, slotDone, attempts >>

AFinishCall == /\ pc = "AFinishCall"
               /\ \E t \in Threads:
                    IF (active[t] # NoReq /\ ~parked[t])
                       THEN /\ LET r == active[t] IN
                                 /\ owner' = [owner EXCEPT ![r] = "done"]
                                 /\ active' = [active EXCEPT ![t] = NoReq]
                                 /\ IF (memo[t] = r)
                                       THEN /\ memo' = [memo EXCEPT ![t] = NoReq]
                                       ELSE /\ TRUE
                                            /\ memo' = memo
                                 /\ IF (pcArmed[r])
                                       THEN /\ IF (GenGuard /\ slot # NoReq /\ slot # r)
                                                  THEN /\ staleStore' = [staleStore EXCEPT ![r] = staleStore[r] + 1]
                                                       /\ UNCHANGED << published, 
                                                                       foreignWrite, 
                                                                       slot, 
                                                                       slotDone >>
                                                  ELSE /\ IF (~GenGuard /\ slot # NoReq /\ slot # r)
                                                             THEN /\ foreignWrite' = foreignWrite + 1
                                                             ELSE /\ TRUE
                                                                  /\ UNCHANGED foreignWrite
                                                       /\ slot' = r
                                                       /\ slotDone' = TRUE
                                                       /\ published' = [published EXCEPT ![r] = TRUE]
                                                       /\ UNCHANGED staleStore
                                       ELSE /\ TRUE
                                            /\ UNCHANGED << published, 
                                                            staleStore, 
                                                            foreignWrite, slot, 
                                                            slotDone >>
                       ELSE /\ TRUE
                            /\ UNCHANGED << owner, published, staleStore, 
                                            foreignWrite, active, memo, slot, 
                                            slotDone >>
               /\ pc' = "MainLoop"
               /\ UNCHANGED << waiting, applied, armed, parked, deferred, 
                               pcArmed, pendingArm, attempts >>

AArmCompletion == /\ pc = "AArmCompletion"
                  /\ \E r \in Requests:
                       IF (pendingArm[r])
                          THEN /\ pendingArm' = [pendingArm EXCEPT ![r] = FALSE]
                               /\ pcArmed' = [pcArmed EXCEPT ![r] = TRUE]
                          ELSE /\ TRUE
                               /\ UNCHANGED << pcArmed, pendingArm >>
                  /\ pc' = "MainLoop"
                  /\ UNCHANGED << owner, waiting, applied, published, 
                                  staleStore, foreignWrite, armed, active, 
                                  parked, deferred, memo, slot, slotDone, 
                                  attempts >>

APublish == /\ pc = "APublish"
            /\ \E c \in Clients:
                 IF (attempts[c] = 0)
                    THEN /\ LET r == RqSeq[attempts[c] + 1] IN
                              IF (owner[r] = "idle")
                                 THEN /\ owner' = [owner EXCEPT ![r] = "transit"]
                                      /\ waiting' = [waiting EXCEPT ![c] = r]
                                      /\ slot' = r
                                      /\ slotDone' = FALSE
                                      /\ attempts' = [attempts EXCEPT ![c] = attempts[c] + 1]
                                 ELSE /\ TRUE
                                      /\ UNCHANGED << owner, waiting, slot, 
                                                      slotDone, attempts >>
                    ELSE /\ TRUE
                         /\ UNCHANGED << owner, waiting, slot, slotDone, 
                                         attempts >>
            /\ pc' = "MainLoop"
            /\ UNCHANGED << applied, published, staleStore, foreignWrite, 
                            armed, active, parked, deferred, memo, pcArmed, 
                            pendingArm >>

ARepublish == /\ pc = "ARepublish"
              /\ \E c \in Clients:
                   IF (attempts[c] < Len(RqSeq) /\ attempts[c] > 0 /\ waiting[c] # NoReq
                         /\ ~published[waiting[c]]
                         /\ (ClientReclaimsOnTimeout
                               \/ owner[waiting[c]] = "done"))
                      THEN /\ LET r == RqSeq[attempts[c] + 1] IN
                                IF (owner[r] = "idle")
                                   THEN /\ owner' = [owner EXCEPT ![r] = "transit"]
                                        /\ waiting' = [waiting EXCEPT ![c] = r]
                                        /\ slot' = r
                                        /\ slotDone' = FALSE
                                        /\ attempts' = [attempts EXCEPT ![c] = attempts[c] + 1]
                                   ELSE /\ TRUE
                                        /\ UNCHANGED << owner, waiting, slot, 
                                                        slotDone, attempts >>
                      ELSE /\ TRUE
                           /\ UNCHANGED << owner, waiting, slot, slotDone, 
                                           attempts >>
              /\ pc' = "MainLoop"
              /\ UNCHANGED << applied, published, staleStore, foreignWrite, 
                              armed, active, parked, deferred, memo, pcArmed, 
                              pendingArm >>

AReadReply == /\ pc = "AReadReply"
              /\ \E c \in Clients:
                   IF (waiting[c] # NoReq /\ slot = waiting[c] /\ slotDone)
                      THEN /\ waiting' = [waiting EXCEPT ![c] = NoReq]
                      ELSE /\ TRUE
                           /\ UNCHANGED waiting
              /\ pc' = "MainLoop"
              /\ UNCHANGED << owner, applied, published, staleStore, 
                              foreignWrite, armed, active, parked, deferred, 
                              memo, pcArmed, pendingArm, slot, slotDone, 
                              attempts >>

(* Allow infinite stuttering to prevent deadlock on termination. *)
Terminating == pc = "Done" /\ UNCHANGED vars

Next == MainLoop \/ AExternalPark \/ AAcceptIdle \/ AAcceptBusy
           \/ AContinuationCompletes \/ ADoWorkDefers \/ ADrainDeferred
           \/ ARunCall \/ AParkCall \/ AFinishCall \/ AArmCompletion \/ APublish
           \/ ARepublish \/ AReadReply
           \/ Terminating

Spec == Init /\ [][Next]_vars

Termination == <>(pc = "Done")

\* END TRANSLATION 

=====

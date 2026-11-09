---------------------------- MODULE Supervisor ----------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS PreserveGroup, ProgressExit, ExpandVfork, WaitForStop, CheckBinding, Writer, Birth
VARIABLES pc, stop, pending, alive, newborn, exit2, held, vfork,
          image, requested, copied, memory, snapshot, mutated, released
vars == <<pc, stop, pending, alive, newborn, exit2, held, vfork,
          image, requested, copied, memory, snapshot, mutated, released>>
Tasks == {1,2,3}
(* 1/2 share a TGID; 3 is an owned different TGID sharing mm or files.
   A files-domain writer can mutate a mapped backing object. kernel is an
   already-submitted asynchronous writer, not a runnable tracee. *)
(* Even granting a full owned-tree stop, submitted kernel writes remain. *)
Scope == IF Writer = "kernel" \/ (ExpandVfork /\ vfork) THEN Tasks ELSE {1,2}
Stopped(t) == stop[t] \in {"trace", "group"}
Init == /\ pc = "freeze"
        /\ stop = [t \in Tasks |-> IF t = 2 THEN "trace"
                                  ELSE IF t = 1 /\ ~Birth THEN "listen" ELSE "run"]
        /\ pending = [t \in Tasks |-> FALSE]
        /\ alive = Tasks /\ newborn = Birth /\ exit2 = TRUE
        /\ held = ~Birth /\ vfork = (Writer = "vfork")
        /\ image = 1 /\ requested = 1 /\ copied = FALSE
        /\ memory = 0 /\ snapshot = 0 /\ mutated = FALSE /\ released = FALSE
InterruptBegin(t) ==
    /\ pc = "freeze" /\ t \in Scope \cap alive /\ t # 2
    /\ ~Stopped(t) /\ ~pending[t] /\ ~(t = 1 /\ vfork)
    /\ ~(t = 1 /\ newborn)
    /\ pending' = [pending EXCEPT ![t] = TRUE]
    /\ UNCHANGED <<pc, stop, alive, newborn, exit2, held, vfork, image,
                    requested, copied, memory, snapshot, mutated, released>>
InterruptResult(t) ==
    /\ pc = "freeze" /\ pending[t]
    (* de_thread cannot report exec until the sibling EXIT stop progresses. *)
    /\ t # 1 \/ 2 \notin alive
    /\ stop' = [stop EXCEPT ![t] = IF t = 1 /\ held /\ PreserveGroup
                                    THEN "group" ELSE "trace"]
    /\ pending' = [pending EXCEPT ![t] = FALSE]
    /\ UNCHANGED <<pc, alive, newborn, exit2, held, vfork, image,
                    requested, copied, memory, snapshot, mutated, released>>
NewbornResult ==
    /\ pc = "freeze" /\ newborn
    /\ stop' = [stop EXCEPT ![1] = "trace"] /\ newborn' = FALSE
    /\ UNCHANGED <<pc, pending, alive, exit2, held, vfork, image,
                    requested, copied, memory, snapshot, mutated, released>>
ResumeExit ==
    /\ pc = "freeze" /\ 2 \in alive /\ Stopped(2) /\ ProgressExit
    /\ stop' = [stop EXCEPT ![2] = "run"]
    /\ UNCHANGED <<pc, pending, alive, newborn, exit2, held, vfork, image,
                    requested, copied, memory, snapshot, mutated, released>>
ReapExit ==
    /\ 2 \in alive /\ stop[2] = "run"
    /\ alive' = alive \ {2}
    /\ UNCHANGED <<pc, stop, pending, newborn, exit2, held, vfork, image,
                    requested, copied, memory, snapshot, mutated, released>>
Ready == \A t \in Scope \cap alive :
             IF t = 2 THEN ~Stopped(t)
             ELSE Stopped(t) \/ (t = 1 /\ vfork)
BeginCopy ==
    /\ pc = "freeze" /\ (Ready \/ ~WaitForStop)
    /\ pc' = "copy" /\ snapshot' = memory
    /\ UNCHANGED <<stop, pending, alive, newborn, exit2, held, vfork, image,
                    requested, copied, memory, mutated, released>>
(* process_vm_* begin/result are separate from external mutation. The owner
   cannot process a new wait status between them, but external writers can run. *)
ExternalWrite ==
    /\ pc = "copy" /\ ~mutated
    /\ Writer = "kernel" \/
       (Writer \in {"mm", "files", "vfork"} /\ 3 \in alive /\ ~Stopped(3))
    /\ memory' = 1 /\ mutated' = TRUE
    /\ UNCHANGED <<pc, stop, pending, alive, newborn, exit2, held, vfork, image,
                    requested, copied, snapshot, released>>
CopyResult ==
    /\ pc = "copy" /\ (~CheckBinding \/ image = requested) /\ Stopped(1) /\ ~newborn
    /\ pc' = "done" /\ copied' = TRUE
    /\ UNCHANGED <<stop, pending, alive, newborn, exit2, held, vfork, image,
                    requested, memory, snapshot, mutated, released>>
RejectCopy ==
    /\ pc = "copy" /\ ~((~CheckBinding \/ image = requested) /\ Stopped(1) /\ ~newborn)
    /\ pc' = "done"
    /\ UNCHANGED <<stop, pending, alive, newborn, exit2, held, vfork, image,
                    requested, copied, memory, snapshot, mutated, released>>
ExecResult ==
    /\ pc = "freeze" /\ image = 1 /\ ~newborn /\ ~held /\ ~vfork
    /\ 2 \notin alive /\ Stopped(1)
    /\ image' = 2
    /\ UNCHANGED <<pc, stop, pending, alive, newborn, exit2, held, vfork,
                    requested, copied, memory, snapshot, mutated, released>>
Release ==
    /\ pc = "done" /\ ~released
    /\ stop' = [t \in Tasks |-> IF stop[t] = "group" THEN "listen" ELSE "run"]
    /\ released' = TRUE
    /\ UNCHANGED <<pc, pending, alive, newborn, exit2, held, vfork, image,
                    requested, copied, memory, snapshot, mutated>>
Next == (\E t \in Tasks : InterruptBegin(t) \/ InterruptResult(t))
        \/ NewbornResult \/ ResumeExit \/ ReapExit \/ BeginCopy
        \/ ExternalWrite \/ CopyResult \/ RejectCopy \/ Release \/ ExecResult
Spec == Init /\ [][Next]_vars
InvLimitedFreeze == pc = "copy" => Ready
InvGroupPreserved == held /\ released => stop[1] = "listen"
InvBirth == copied => ~newborn
InvCopyBinding == copied => image = requested
InvProtectedCopy == copied => memory = snapshot
InvVforkScope == pc = "copy" /\ vfork => Stopped(3)
(* A bounded enabledness check, NOT an unconditional temporal progress claim.
   Event/result actions abstract eventual kernel delivery under fairness. *)
InvExitProgress == pc = "freeze" => ENABLED Next
=============================================================================

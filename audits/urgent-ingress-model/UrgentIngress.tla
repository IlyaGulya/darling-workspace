-------------------------- MODULE UrgentIngress --------------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC
CONSTANT Unsafe
Requests == 1..5
Enter(r) == r \in {1,2}
Exit(r) == r \in {4,5}
VARIABLES phase, admitted, logical, actual, tombstone, executor, live,
          suspended, returned, reply, claimed, published, epoch, ownerEpoch,
          alive, recheck, badContext, badPublication, falseDone
vars == <<phase, admitted, logical, actual, tombstone, executor, live,
          suspended, returned, reply, claimed, published, epoch, ownerEpoch,
          alive, recheck, badContext, badPublication, falseDone>>
Init == /\ phase = [r \in Requests |-> "pending"]
        /\ admitted = <<>> /\ logical = <<>> /\ actual = <<>>
        /\ tombstone = FALSE /\ executor = 0 /\ live = {}
        /\ suspended = {} /\ returned = {} /\ reply = {}
        /\ claimed = {} /\ published = [r \in Requests |-> 0]
        /\ epoch = 1 /\ ownerEpoch = [r \in Requests |-> 0]
        /\ alive = TRUE /\ recheck = FALSE
        /\ badContext = FALSE /\ badPublication = FALSE /\ falseDone = FALSE
Admit(r) == /\ phase[r] = "pending" /\ alive
            /\ \A q \in 1..(r-1): phase[q] # "pending"
            /\ phase' = [phase EXCEPT ![r] = "accepted"]
            /\ admitted' = Append(admitted,r)
            /\ ownerEpoch' = [ownerEpoch EXCEPT ![r] = epoch]
            /\ UNCHANGED <<logical,actual,tombstone,executor,live,suspended,returned,reply,claimed,published,epoch,alive,recheck,badContext,badPublication,falseDone>>
Ready(r) == /\ ~tombstone /\ executor = 0
            /\ (IF r = 1 THEN logical = <<>> ELSE actual = logical /\ Len(actual) > 0)
            /\ (IF Enter(r) \/ Len(actual) = 0 THEN TRUE ELSE actual[Len(actual)] \notin live)
Prepare(r) == /\ phase[r] = "accepted"
              /\ \A q \in 1..(r-1): phase[q] \notin {"pending","accepted","prepared"}
              /\ (Ready(r) \/ Unsafe = "context")
              /\ phase' = [phase EXCEPT ![r] = "prepared"]
              /\ tombstone' = Exit(r)
              /\ badContext' = (badContext \/ tombstone \/ executor # 0
                    \/ (IF r = 1 THEN logical # <<>> ELSE actual # logical \/ Len(actual) = 0)
                    \/ (IF Enter(r) \/ Len(actual) = 0 THEN FALSE ELSE actual[Len(actual)] \in live))
              /\ UNCHANGED <<admitted,logical,actual,executor,live,suspended,returned,reply,claimed,published,epoch,ownerEpoch,alive,recheck,badPublication,falseDone>>
Wait(r) == /\ phase[r] = "accepted" /\ ~Ready(r) /\ ~recheck
           /\ recheck' = TRUE
           /\ UNCHANGED <<phase,admitted,logical,actual,tombstone,executor,live,suspended,returned,reply,claimed,published,epoch,ownerEpoch,alive,badContext,badPublication,falseDone>>
Arm(r) == /\ phase[r] = "prepared" /\ alive
          /\ phase' = [phase EXCEPT ![r] = "armed"]
          /\ logical' = IF Enter(r) THEN Append(logical,r) ELSE logical
          /\ UNCHANGED <<admitted,actual,tombstone,executor,live,suspended,returned,reply,claimed,published,epoch,ownerEpoch,alive,recheck,badContext,badPublication,falseDone>>
Execute(r) == /\ phase[r] = "armed" /\ executor = 0 /\ alive
              /\ phase' = [phase EXCEPT ![r] = "executing"] /\ executor' = r
              /\ live' = live \cup {r}
              /\ actual' = IF Enter(r) THEN Append(actual,r) ELSE actual
              /\ UNCHANGED <<admitted,logical,tombstone,suspended,returned,reply,claimed,published,epoch,ownerEpoch,alive,recheck,badContext,badPublication,falseDone>>
Reply(r) == /\ executor = r /\ r \notin reply
            /\ reply' = reply \cup {r}
            /\ UNCHANGED <<phase,admitted,logical,actual,tombstone,executor,live,suspended,returned,claimed,published,epoch,ownerEpoch,alive,recheck,badContext,badPublication,falseDone>>
\* One bounded saved outer executor suffices to distinguish nested return from
\* owner return. Suspension never removes executor ownership or a ready reply.
Suspend == /\ executor = 1 /\ phase[1] = "executing" /\ actual = <<1>>
           /\ executor' = 0 /\ suspended' = {1}
           /\ phase' = [phase EXCEPT ![1] = "suspended"]
           /\ UNCHANGED <<admitted,logical,actual,tombstone,live,returned,reply,claimed,published,epoch,ownerEpoch,alive,recheck,badContext,badPublication,falseDone>>
Resume == /\ executor = 0 /\ suspended = {1} /\ actual = <<1>>
          /\ ~tombstone /\ alive
          /\ \A q \in Requests: phase[q] \notin {"prepared","armed"}
          /\ executor' = 1 /\ suspended' = {}
          /\ phase' = [phase EXCEPT ![1] = "executing"]
          /\ UNCHANGED <<admitted,logical,actual,tombstone,live,returned,reply,claimed,published,epoch,ownerEpoch,alive,recheck,badContext,badPublication,falseDone>>
Return(r) == /\ executor = r
             /\ executor' = 0 /\ phase' = [phase EXCEPT ![r] = "returned"]
             /\ live' = live \ {r} /\ returned' = returned \cup {r}
             /\ logical' = IF Exit(r) /\ Len(logical) > 0 THEN SubSeq(logical,1,Len(logical)-1) ELSE logical
             /\ actual' = IF Exit(r) /\ Len(actual) > 0 THEN SubSeq(actual,1,Len(actual)-1) ELSE actual
             /\ tombstone' = IF Exit(r) THEN FALSE ELSE tombstone
             /\ recheck' = FALSE
             /\ UNCHANGED <<admitted,suspended,reply,claimed,published,epoch,ownerEpoch,alive,badContext,badPublication,falseDone>>
Claim(r) == /\ r \notin claimed
            /\ ((phase[r] = "returned" /\ r \notin live)
                \/ (Unsafe = "nestedClaim" /\ r \in suspended /\ r \in reply
                    /\ \E nested \in returned: nested # r))
            /\ claimed' = claimed \cup {r}
            /\ phase' = [phase EXCEPT ![r] = IF r \in reply THEN "claimed" ELSE "abandoned"]
            /\ admitted' = SelectSeq(admitted, LAMBDA q: q # r)
            /\ UNCHANGED <<logical,actual,tombstone,executor,live,suspended,returned,reply,published,epoch,ownerEpoch,alive,recheck,badContext,badPublication,falseDone>>
Publish(r) == /\ phase[r] \in {"claimed","abandoned"}
              /\ (phase[r] = "claimed" \/ Unsafe = "falseDone")
              /\ ((ownerEpoch[r] = epoch /\ alive) \/ Unsafe = "stale")
              /\ phase' = [phase EXCEPT ![r] = "published"]
              /\ published' = [published EXCEPT ![r] = @ + 1]
              /\ badPublication' = (badPublication \/ ownerEpoch[r] # epoch \/ ~alive)
              /\ falseDone' = (falseDone \/ r \notin reply)
              /\ UNCHANGED <<admitted,logical,actual,tombstone,executor,live,suspended,returned,reply,claimed,epoch,ownerEpoch,alive,recheck,badContext>>
Death == /\ alive /\ alive' = FALSE /\ executor' = 0
         /\ live' = {} /\ suspended' = {}
         /\ UNCHANGED <<phase,admitted,logical,actual,tombstone,returned,reply,claimed,published,epoch,ownerEpoch,recheck,badContext,badPublication,falseDone>>
Replace == /\ ~alive /\ epoch = 1 /\ epoch' = 2 /\ alive' = TRUE
           /\ UNCHANGED <<phase,admitted,logical,actual,tombstone,executor,live,suspended,returned,reply,claimed,published,ownerEpoch,recheck,badContext,badPublication,falseDone>>
Next == (\E r \in Requests: Admit(r) \/ Prepare(r) \/ Wait(r) \/ Arm(r) \/ Execute(r) \/ Reply(r) \/ Return(r) \/ Claim(r) \/ Publish(r)) \/ Suspend \/ Resume \/ Death \/ Replace
Spec == Init /\ [][Next]_vars
ContextOwnership == ~badContext
CurrentPublication == ~badPublication
NoFalseDone == ~falseDone
AtMostOnce == \A r \in Requests: published[r] <= 1
NoLiveExecutorClaim == claimed \cap live = {}
ExecutorOwnership == /\ suspended \subseteq live
                     /\ (executor # 0 => executor \in live \ suspended)
                     /\ live = suspended \cup (IF executor = 0 THEN {} ELSE {executor})
=============================================================================

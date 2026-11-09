--------------------------- MODULE ExecReplacement ---------------------------
EXTENDS Naturals, TLC
CONSTANT Fault
VARIABLES phase, death, deathEvents, authority, allocated, current, oldReleased,
          teardown, releases, finished, effects
vars == <<phase, death, deathEvents, authority, allocated, current, oldReleased,
          teardown, releases, finished, effects>>
Init == /\ phase = "idle" /\ death = FALSE /\ deathEvents = 0
        /\ authority = TRUE /\ allocated = FALSE /\ current = "old"
        /\ oldReleased = FALSE /\ teardown = "none" /\ releases = 0
        /\ finished = FALSE /\ effects = "none"
Begin == /\ phase = "idle" /\ ~death /\ ~finished
         /\ phase' = "clearing" /\ effects' = "clear"
         /\ UNCHANGED <<death, deathEvents, authority, allocated, current,
                        oldReleased, teardown, releases, finished>>
ClearResult == /\ phase = "clearing" /\ effects = "clear"
               /\ phase' = "allocating" /\ effects' = "allocate"
               /\ UNCHANGED <<death, deathEvents, authority, allocated, current,
                              oldReleased, teardown, releases, finished>>
AllocateResult == /\ effects = "allocate"
                  /\ phase' = IF phase = "allocating" THEN "allocated" ELSE phase
                  /\ allocated' = TRUE /\ effects' = "none"
                  /\ UNCHANGED <<death, deathEvents, authority, current,
                                 oldReleased, teardown, releases, finished>>
Publish == /\ (phase = "allocated" \/ (Fault = "publish-early" /\ phase = "allocating"))
           /\ phase' = "published" /\ current' = "new"
           /\ authority' = (~death \/ Fault = "publish-dead")
           /\ UNCHANGED <<death, deathEvents, allocated, oldReleased, teardown,
                          releases, finished, effects>>
ReleaseBegin == /\ (phase = "published" \/ (Fault = "release-early" /\ phase = "allocated"))
                /\ phase' = "releasing" /\ effects' = "release"
                /\ UNCHANGED <<death, deathEvents, authority, allocated, current,
                               oldReleased, teardown, releases, finished>>
ReleaseResult == /\ phase = "releasing" /\ effects = "release"
                 /\ phase' = "released" /\ oldReleased' = TRUE /\ effects' = "none"
                 /\ UNCHANGED <<death, deathEvents, authority, allocated, current,
                                teardown, releases, finished>>
Finish == /\ (phase = "released" \/ (Fault = "finish-early" /\ phase = "allocating"))
          /\ phase' = "idle" /\ finished' = TRUE
          /\ UNCHANGED <<death, deathEvents, authority, allocated, current,
                         oldReleased, teardown, releases, effects>>
Death == /\ deathEvents < 2
         /\ death' = TRUE /\ deathEvents' = deathEvents + 1 /\ authority' = FALSE
         /\ UNCHANGED <<phase, allocated, current, oldReleased, teardown,
                        releases, finished, effects>>
TeardownBegin == /\ death /\ phase = "idle"
                 /\ (teardown = "none" \/ (Fault = "double-teardown" /\ teardown = "done" /\ releases < 2))
                 /\ teardown' = "pending"
                 /\ UNCHANGED <<phase, death, deathEvents, authority, allocated,
                                current, oldReleased, releases, finished, effects>>
TeardownResult == /\ teardown = "pending"
                  /\ teardown' = "done" /\ releases' = releases + 1
                  /\ UNCHANGED <<phase, death, deathEvents, authority, allocated,
                                 current, oldReleased, finished, effects>>
Next == Begin \/ ClearResult \/ AllocateResult \/ Publish \/ ReleaseBegin \/
        ReleaseResult \/ Finish \/ Death \/ TeardownBegin \/ TeardownResult
Spec == Init /\ [][Next]_vars
InvAuthority == death => ~authority
InvCurrentThread == oldReleased => current = "new"
InvFinish == finished => (oldReleased /\ current = "new" /\ effects = "none")
InvExclusive == teardown # "none" => (phase = "idle" /\ effects = "none")
InvExactlyOnce == releases <= 1
InvPublication == current = "new" => allocated
=============================================================================

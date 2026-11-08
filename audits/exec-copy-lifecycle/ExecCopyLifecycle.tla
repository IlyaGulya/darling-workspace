------------------------- MODULE ExecCopyLifecycle -------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS UnsafeStaleCompletion, UnsafeLateWrite, UnsafeReplay
VARIABLES alive, attempt, generation, pending, ready, consumed, completions,
          bound, request, requestGeneration, writes, stale, late, replay
vars == <<alive, attempt, generation, pending, ready, consumed, completions,
          bound, request, requestGeneration, writes, stale, late, replay>>
Init == /\ alive = TRUE /\ attempt = 0 /\ generation = 0
        /\ pending = 0 /\ ready = {} /\ consumed = {}
        /\ completions = [a \in 1..2 |-> 0]
        /\ bound = FALSE /\ request = "absent" /\ requestGeneration = 0
        /\ writes = 0 /\ stale = FALSE /\ late = FALSE /\ replay = FALSE
Begin == /\ alive /\ pending = 0 /\ attempt < 2
         /\ attempt' = attempt + 1 /\ pending' = attempt + 1
         /\ UNCHANGED <<alive, generation, ready, consumed, completions, bound,
                         request, requestGeneration, writes, stale, late, replay>>
Ready(a) == /\ a = pending /\ a # 0 /\ ready' = ready \cup {a}
            /\ UNCHANGED <<alive, attempt, generation, pending, consumed, completions,
                            bound, request, requestGeneration, writes, stale, late, replay>>
Fail == /\ alive /\ pending # 0 /\ pending \notin ready
        /\ consumed' = consumed \cup {pending} /\ pending' = 0
        /\ UNCHANGED <<alive, attempt, generation, ready, completions, bound,
                        request, requestGeneration, writes, stale, late, replay>>
Complete(a) == /\ alive /\ a \in ready /\ generation < 2
               /\ (UnsafeStaleCompletion \/ (a = pending /\ a \notin consumed))
               /\ stale' = (stale \/ a # pending \/ a \in consumed)
               /\ completions' = [completions EXCEPT ![a] = @ + 1]
               /\ consumed' = consumed \cup {a} /\ pending' = 0
               /\ generation' = generation + 1 /\ bound' = FALSE
               /\ UNCHANGED <<alive, attempt, ready, request, requestGeneration,
                               writes, late, replay>>
Bind == /\ alive /\ ~bound /\ bound' = TRUE
        /\ UNCHANGED <<alive, attempt, generation, pending, ready, consumed,
                        completions, request, requestGeneration, writes, stale, late, replay>>
Submit == /\ alive /\ bound /\ request = "absent"
          /\ request' = "pending" /\ requestGeneration' = generation
          /\ UNCHANGED <<alive, attempt, generation, pending, ready, consumed,
                          completions, bound, writes, stale, late, replay>>
Timeout == /\ request = "pending" /\ request' = "timedout"
           /\ UNCHANGED <<alive, attempt, generation, pending, ready, consumed,
                           completions, bound, requestGeneration, writes, stale, late, replay>>
Write == /\ writes < 2
         /\ (request = "pending" \/ (UnsafeReplay /\ request \in {"timedout", "done"}))
         /\ (UnsafeLateWrite \/ (alive /\ bound /\ requestGeneration = generation))
         /\ late' = (late \/ ~alive \/ ~bound \/ requestGeneration # generation)
         /\ replay' = (replay \/ request # "pending")
         /\ request' = "done" /\ writes' = writes + 1
         /\ UNCHANGED <<alive, attempt, generation, pending, ready, consumed,
                         completions, bound, requestGeneration, stale>>
Death == /\ alive /\ alive' = FALSE /\ bound' = FALSE /\ pending' = 0
         /\ UNCHANGED <<attempt, generation, ready, consumed, completions,
                         request, requestGeneration, writes, stale, late, replay>>
Next == Begin \/ Fail \/ Bind \/ Submit \/ Timeout \/ Write \/ Death
        \/ (\E a \in 1..2 : Ready(a) \/ Complete(a))
Spec == Init /\ [][Next]_vars
TypeOK == /\ alive \in BOOLEAN /\ bound \in BOOLEAN
          /\ attempt \in 0..2 /\ generation \in 0..2 /\ pending \in 0..2
          /\ ready \subseteq 1..2 /\ consumed \subseteq 1..2
          /\ completions \in [1..2 -> 0..2]
          /\ request \in {"absent", "pending", "timedout", "done"}
          /\ requestGeneration \in 0..2 /\ writes \in 0..2
          /\ stale \in BOOLEAN /\ late \in BOOLEAN /\ replay \in BOOLEAN
NoStaleCompletion == ~stale
NoLateWrite == ~late
AtMostOnce == /\ writes <= 1 /\ \A a \in 1..2 : completions[a] <= 1
NoTimeoutReplay == ~replay
=============================================================================

# RPC / Ring / process-control-plane / courier audit — model artifacts

Small TLA+/PlusCal model of the **accepted-request disposition state machine** of
`darlingserver`, used to answer the four model-checking questions of the audit
(`audits/darlingserver-rpc-plane-audit-2026-10-19.md`, section 5).

It is deliberately **not** a model of the wire format. It keeps only the facts
that decide those questions: who owns an accepted request, when it runs, when
(and whether) its terminal disposition is published, and what the caller does
while it waits. Every action is annotated with the production code it abstracts.

## Files

| file | purpose |
|---|---|
| `DServerPlane.pcal.tla` | PlusCal source (edit this one) |
| `DServerPlane.tla` | translated TLA+ module (what TLC checks) |
| `run-all.sh` | runs the configuration × invariant matrix, prints verdicts |
| `model-check-results.txt` | the recorded output of `run-all.sh` |
| `counterexamples/*.txt` | TLC state traces for each violation — the deterministic regression scenarios |
| `cfg_*.cfg` | generated per-run configurations (kept for reproducing a single cell) |

## Running it

```sh
TLA_JAR=/path/to/tla2tools.jar ./run-all.sh
# tools: java (>=11) + tla2tools.jar (TLC + pcal.trans)
java -cp $TLA_JAR pcal.trans DServerPlane.tla   # after editing the .pcal.tla
```

## The four switches (each names one production shape)

| switch | shape it abstracts | production anchor |
|---|---|---|
| `ArmWithoutOwner` | `Call::callFromMessage` → `Thread::setPendingCall` **arms** the Call, then `dispatchOrQueuePlaneCall(call, queueIfBusy=false)` returns `false` **without disarming it**, while the plane records "left PENDING for a later pass" | `src/call.cpp:555-557`, `src/thread.cpp:443-448`, `src/server.cpp:3773-3780`, `src/server.cpp:1846` |
| `GenGuard` | `page->request_seq == seq` before the completion stores | `src/server.cpp:3811-3818`, `src/thread.cpp:2609` |
| `ParkedCompletesExternally` | the parked (blocking) Call's completion is an **external** event (guest message, timeout, signal) that requires nothing from a deferred Call | `src/thread.cpp:944-1013` (`suspend`), duct-tape `thread_block` → `dtape_hooks->thread_suspend` |
| `ClientReclaimsOnTimeout` | the caller's bounded republish fires while its attempt is still outstanding (the guest's 2 s bounded republish / datagram fallback) | `src/startup/mldr/mldr.c:2046,2093-2097`, `dserver-ring.c:3569-3592`; `.../ext/for-libkqueue.c:20-60` |
| `ArmAfterQueue` | the pass queues the Call first and calls `setPlaneCompletion` afterwards | `src/server.cpp:2444-2448`, `src/thread.cpp:469,920-925` |

## Invariants

| invariant | question |
|---|---|
| `Inv2NoWrongGeneration` | no completion is stored into a page slot a **later** request owns |
| `Inv3NoSilentLoss` | an accepted request whose handler ran gets a terminal disposition (no silent finish) |
| `Inv4NoDuplicateApply` | a mutating operation is applied at most once across the caller's attempts |
| `Inv5NoForeignReply` | a DONE slot the caller waits on belongs to that caller's published request (weaker restatement of Inv2 in this abstraction; implied by it) |
| `Inv6NoInternalCycle` | a parked thread carrying queued work whose only completion channel **is** the parked call (the internal circular wait) |
| `Inv7QueuedCallHasOwner` | no Call is reachable from the deferred queue before its completion owner is armed (the arm-after-queue atomicity requirement) |

## Result matrix (recorded in `model-check-results.txt`)

| config | Arm | Gen | Ext | Reclaim | Inv2 | Inv3 | Inv4 | Inv5 | Inv6 |
|---|---|---|---|---|---|---|---|---|---|
| `guarded`        | F | T | T | F | OK | OK | OK | OK | OK |
| `unowned-arm`    | T | T | T | F | OK | **VIOLATED** | **VIOLATED** | OK | OK |
| `no-gen-guard`   | T | F | T | T | **VIOLATED** | **VIOLATED** | **VIOLATED** | OK | OK |
| `reclaim-race`   | F | T | T | T | OK | OK | **VIOLATED** | OK | OK |
| `internal-cycle` | F | T | F | F | OK | OK | OK | OK | **VIOLATED** |
| `real-console-defer` | T | T | T | T | OK | **VIOLATED** | **VIOLATED** | OK | OK |
| `arm-after-queue` | F | T | T | F | OK | **VIOLATED** | **VIOLATED** | OK | OK |

Readings:

* `guarded` is the safe protocol: with the completion armed **with** the Call and
  the generation guard in place, every request that runs is published exactly
  once, and no attempt is applied twice **as long as the caller does not
  republish an outstanding attempt**.
* `unowned-arm` is the production shape of the audit's slice-2 defect: the Call
  runs, its answer is captured and dropped (`thread.cpp:2602-2634` with
  `planeCompletion().armed() == false`), and the caller's republish applies the
  operation a second time.
* `no-gen-guard` shows what the `request_seq` guard prevents: a late completion
  overwriting a newer request's slot (wrong-generation completion).
* `reclaim-race` shows that the generation guard alone does **not** make
  duplicate application impossible: a caller that republishes its operation
  while the server is still executing the first attempt gets both applied. The
  guard protects the *page*, not the *operation*; only the caller's ownership
  discipline does.
* `internal-cycle` shows the exact assumption the deferred-call mechanism rests
  on: a parked thread whose queued Call can only be drained after the parked
  call completes is a circular wait **iff** the parked call's completion is not
  an external event.
* `real-console-defer` is the **real-path instantiation** used by the
  regression `tests/run-plane-deferred-arm-contract.sh`: ParkReq =
  `semaphore_timedwait`/`mach_msg` (the fixture's main thread), Req1/Req2 =
  `console_open` and its retry. It is the shape the captured server trace
  shows (`number=16`, `active_call=62|38`, `suspended=1`).
* `arm-after-queue` models the second window: the Call is queued first and
  `setPlaneCompletion` runs afterwards (`server.cpp:2444-2448`). `Inv7`
  states the minimal atomicity requirement -- *a Call reachable from the
  deferred queue must already carry its completion owner* -- and this config
  shows both the window (`Inv7`) and its consequences (`Inv3`, `Inv4`).

## Limits of this model

* Two threads, one client, three requests; it is a shape model, not a
  state-space exploration of the production parameter ranges.
* Deadlock/liveness is expressed through `Inv6` and the `ParkedCompletesExternally`
  switch rather than a full temporal proof under per-action fairness; TLC's
  liveness checking with a uniprocess algorithm cannot distinguish a genuine
  starvation from a scheduling choice, so the claim is stated as a safety
  invariant plus an explicitly stated fairness assumption.
* No refinement relation to the C++ is established. The model constrains the
  *protocol shape*; the audit's defect list is established by reading the
  production code, and the model only says which shapes are safe.

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
| `run-all.sh`, `run_models.py` | isolated matrix runner, explicit expected verdicts and per-cell artifacts |
| `model-check-results.txt` | historical output of the old runner; not fresh acceptance evidence |
| `counterexamples/*.txt` | historical bounded traces; reproduce with the current runner before relying on them |
| `cfg_*.cfg` | checked-in constant assignments; copied, never edited by the runner |

## Running it

```sh
TLA_JAR=/absolute/path/to/tla2tools.jar audits/rpc-plane-model/run-all.sh \
  --output-root "$DW_STATE_ROOT/model-runs"
python3 -B tests/west_test_contracts/tlc_runner_contract.py
```

Prerequisites: Python 3, Java compatible with the selected `tla2tools.jar`,
and that real jar supplied explicitly with `TLA_JAR` or `--jar`. There is no
`/tmp` jar or output fallback. Without `--output-root`, output lives under
`$DW_STATE_ROOT/model-runs` (canonical workspace `.west-state/model-runs` when
unset). Every invocation creates a fresh `tlc-*` directory, prints its path,
and preserves it even on failure. The runner neither cleans shared state nor
writes generated configurations, state databases or traces into the checkout.

Each cell preserves its copied TLA sources, selected `case.cfg`, exact argv
and working directory (`command.json`), full merged Java output (`tlc.log`),
TLC state/trace files and structured verdict/exit code (`result.json`).
The invocation records the jar path and SHA-256; `results.json` covers all
attempted cells. Setup errors are preserved in `error.txt`.

`OK` requires Java exit 0, TLC's completed-success message and final
`Finished in` marker, with no reported errors. `VIOLATED` requires TLC's
invariant-violation exit **12**, the exact requested invariant, a behavior
trace beginning at State 1 and the final completion marker; no unrelated TLC
errors are allowed. Everything else is **TOOL/CONFIG ERROR**, including missing
Java/jar, parser/configuration failures, crashes and truncated output. An
unexpected invariant outcome or any tool error fails the overall command.
The checked expected counterexamples describe this bounded model, not a second
copy of production transition policy. The historical table below is not used
as a success oracle by parsing old logs.

The same runner supports other bounded models without another runner:

```sh
audits/rpc-plane-model/run-all.sh --jar /absolute/path/to/tla2tools.jar \
  --model-dir audits/exec-copy-lifecycle --module ExecCopyLifecycle \
  --case safe.cfg:NoStaleCompletion:OK
```

Repeat `--case CONFIG:INVARIANT:OK|VIOLATED` for the complete lifecycle cases
listed in that model's documentation. Custom models require explicit cases;
each config should declare its invariant on a single `INVARIANT` line, which
is replaced only in the isolated copy. Multiple constant/specification
declarations remain TLC's responsibility to validate. Use the lifecycle
documentation's full case list for acceptance, not just the example above.

The focused host contract injects controlled process outcomes into the real
CLI orchestration, including error exits, crashes, incomplete output, unexpected
invariants and stale-log isolation. It does **not** execute TLC or validate a
TLA specification. Acceptance additionally requires the actual full RPC matrix
and lifecycle cases with the real jar and preserved artifact directories.

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

## Model / implementation bridge and boundaries

The RPC model's event boundaries are request publication/claim, ownership arm,
queue/dispatch, cancellation/timeout, descriptor arrival and completion.
Production descriptor ownership is exercised independently through
`ProcessControlLifecycle` in darlingserver: identity binds generation, sequence,
operation, page mapping/size/epoch and descriptor token/kind. The helper owns
the accepted FD, dispatch transfers it once, cancellation retires ownership,
and late or stale descriptor delivery must not revive it.

Concrete bounded scenario correspondence (not an executable refinement map):

| Model event sequence / property | Source-owned scenario in `tests/process_control_lifecycle_test.cpp` |
|---|---|
| claim → wait → matching FD; ownership before runnable work (`Inv7`) | `courierAdapter()`: register with production `FdCourierState::resolve`, then synchronously deliver the matching arrival into `ProcessControlLifecycle`; callback observes READY |
| claim → wait → cancel → late FD; no completion after terminal cancellation | Direct-helper `replay({"wait", "cancel", "fd", "wait", "dispatch"})`; `courierAdapter()` also cancels a transaction after page-epoch replacement and rejects the later delivered FD |
| claim → wait → stale-identity FD; generation isolation (`Inv2`, weaker `Inv5`) | `courierAdapter()` rejects wrong PID, kind and generation on both waiter and pending paths, and suppresses delivery after generation changes between matching and delivery; direct-helper cases mutate every identity field |
| claim → matching FD → dispatch → dispatch; no second resource transfer | Direct-helper replay and dispatch assertions consume the owned descriptor exactly once |
| claim → wait → failure → cancel | Direct-helper `replay({"wait", "fd", "failure", "fd", "dispatch"})` leaves no live descriptor owner |
| cancel registration → retire captures → replace registration → stale cancellation | `courierAdapter()` checks the old strong capture survives until the returned retirement object is destroyed, then expires; cancelling the stale handle cannot cancel the replacement |

These are deliberately limited shared boundaries: the RPC TLA model does not
represent actual FD numbers, descriptor closing, mapping lifetimes or callback
memory safety. In particular, `Inv4NoDuplicateApply` concerns replay of a
mutating operation across client attempts; a helper's once-only FD dispatch is
**not** proof of that stronger property. The lifecycle model separately covers
exec/copy-generation cancellation boundaries; see its documentation for its
own helper mapping and bounded assumptions.

The source-owned production helper is
`internal-include/darlingserver/process-control-lifecycle.hpp`; the registered
host executable is `dserver_process_control_lifecycle_tests`. Its trace mode
drives that same helper (not a test FSM), opens real `/dev/null` descriptors,
checks rejected descriptors are closed, and closes each transferred descriptor.
After building the registered target, preserve these action traces alongside
the TLC artifacts (`HELPER` names the built executable):

```sh
"$HELPER" --trace wait fd wait dispatch dispatch fd
"$HELPER" --trace wait cancel fd wait dispatch
"$HELPER" --trace wait stale-fd fd dispatch
"$HELPER" --trace wait fd failure fd dispatch
```

Output records `event:accepted:state` at every boundary; helper states are
CLAIMED=0, WAIT_FD=1, READY=2, DISPATCHED=3 and CANCELLED=4. Compare the
cancel/stale/once-only expectations in the table, not raw integer equality
with a TLC state number. These `--trace` commands exercise direct helper calls,
not the courier adapter. Run `"$HELPER"` without arguments (CTest name
`dserver_process_control_lifecycle`) for `courierAdapter()`, identity mutation,
descriptor-destruction and direct-helper scenarios. The adapter uses the real
production `FdCourierState` and `ProcessControlLifecycle` boundary, but does
**not** instantiate `Server`, a socket or the server drain loop. Its synchronous
delivery is an explicit call to the production arrival object's `deliver()`;
it is not queued-inline server integration or proof of resolver scheduling.

Run the production helper's source-owned tests as well as TLC. The model and
tests can expose mismatched event ordering, but neither establishes a refinement
relation, trusted supervisor boundary, full server security, native-memory
protection or end-to-end Cryptex/Xcode correctness. Historical production line
anchors above describe the audited defect shapes, not proof that current
production code has those defects.

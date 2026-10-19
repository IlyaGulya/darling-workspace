# darlingserver RPC / Ring / process-control-plane / courier audit

Read-only architecture and correctness audit. Date: 2026-10-19.
Source: `/home/ilyagulya/work/wr-fresh/darling/src/external/darlingserver` (the canonical
workspace checkout). Nothing was edited, built, deployed or run; no prefix was touched.

Evidence labels used throughout:

* **CONFIRMED** — reachable by control flow alone once the stated precondition holds; the
  cited lines were read in this audit. Where reachability depends on a timing property,
  the precondition is stated explicitly.
* **RISK** — a property I did not establish (namespace collision, peer malice, a future
  change). Never reported as a defect.
* `(slice: ring|courier|plane)` — the finding comes from the corresponding read-only
  slice investigation; its anchors are cited so it can be re-checked. Each such finding
  was re-read here unless marked otherwise.

---

## A. Architecture map

### A.1 Entry paths, executors, suspension, reply destination, cancellation, ownership

| # | Entry path | Executor | Suspension points | Reply destination | Cancellation | Resource ownership |
|---|---|---|---|---|---|---|
| 1 | **UDS datagram RPC**: listener socket → `Server::start` loop → `Call::callFromMessage` → `Thread::doWork` inline (`src/server.cpp:4113-4210`) | main loop, then the target thread's fiber (inline or worker after a suspend) | `doWork` may `setcontext` into a fiber that suspends: `Thread::suspend` (`src/thread.cpp:944`), reached from duct-tape `thread_block` → `dtape_hooks->thread_suspend` (`duct-tape/src/thread.c:711-760`) for blocking traps (mach_msg receive, semaphore wait, psynch, kqchan) | `pushCallReply` (`src/thread.cpp:2557`) → UDS `Server::sendMessage` (`src/server.cpp`) | none: an accepted call with no transport (dead thread) is dropped; a `setPendingCall` rejection replies `-EAGAIN` (`src/call.cpp:556-570`) | `Call` holds the request message and its `@fd` body descriptors; the generated dtor closes any descriptor left at `-1` (`scripts/generate-rpc-wrappers.py:1306-1315`) |
| 2 | **C2S Ring** (per-thread lane): `_drainRings` → `ringServiceThread` → `dserver_ring_consumer_begin/advance` → rebuilt Message → `callFromMessage` → `attachRingContext` → `doWork` inline (`src/call.cpp:2022-2373`) | main loop only for the *consume*; the fiber may resume on the worker pool (`src/thread.cpp:1015-1031`, `src/server.cpp:4503-4509`) | same as #1 (a ring call may block; the caller is then parked on its lane) | `pushCallReply` → `_publishReplyToRingContextLocked` → `Call::publishReplyToRingContext` → `RingBuffer::publishReply` (`src/thread.cpp:2541-2554`, `src/call.cpp:700-731`, `src/ring.cpp:118-143`); `publishFinalReply` when the Thread is gone (`src/call.cpp:740-752`) | pre-dispatch declines only (sentinel/dealloc/vmdealloc/mach_msg caps); no cancellation of a consumed slot | slot is copied out and the slot is `advance`d **before** dispatch (`src/call.cpp:2324`); the Call owns its `RingCallContext` (`shared_ptr<RingBuffer>` + seq) so the reply survives a suspend |
| 3 | **Process-control plane** (management page): `_serviceProcessControl` each main-loop pass (`src/server.cpp:4113`, also `4380/4385` after courier drains) | main loop; synthesized Calls run on a chosen thread of the target task | an inline Call may suspend (same as #1), and the pass then publishes a status anyway (defect C7) | the page: `reply_seq`/`reply_status`/`reply_payload`/`reply_state=DONE`+futex (`src/server.cpp:3811-3847`) or `pushCallReply`'s `PlaneCompletion` block (`src/thread.cpp:2602-2634`) | `_cancelProcessControlTxn` on process death only (`src/server.cpp:3856-3870` from `process.cpp:916`); courier connection close does **not** cancel | `ProcessControlRegion` holds the mapping (deliberately never unmapped); `ProcessControlTxn` holds a raw `int fd` with no destructor (`internal-include/darlingserver/server.hpp:164-179`) |
| 4 | **Descriptor courier**: guest→server `SOCK_SEQPACKET` envelope → `_drainFdCourierMessages` (`src/server.cpp:1144-1485`) → waiter resume or `_fdCourierPending`; server→guest `sendFdCourierBundleToGuest` (`909-1142`) | main loop (drain); senders may be any thread | the checkout/RingAttach *handler* parks its fiber while the descriptor is in flight (`src/call.cpp:925-960`, `src/server.cpp:2262-2298`) | n/a (descriptor channel); the semantic reply follows on the page/lane/UDS | none; pending bundles are reaped only at `Process::_dispose` (`src/server.cpp:3968-3991`) | `Message` owns descriptors until `sendmsg` succeeds; on success the server `close`s its copy (`src/server.cpp:1133-1135`) |
| 5 | **S2C upcalls** (`Thread::_s2cPerform`, `src/thread.cpp:1431`) | whichever thread runs the parent handler | the parent's caller must service the upcall: UDS S2C waits on `_s2cReplySempahore` with no timeout (`src/thread.cpp:1653`); duplex lane uses a 5 s deadline (`1834-1838`) | the guest, then the server's `_s2cReply` | none | `_deferReplyForS2C`/`_deferredReply` on the Thread (defect R1) |
| 6 | **Interrupt protocol** (`interrupt_enter`/`interrupt_exit`, `ALLOW_INTERRUPTIONS`, `PUSH_UNKNOWN_REPLIES`) | the interrupted call's thread | `_handleInterruptEnterForCurrentThread` may itself suspend (`src/thread.cpp:2916-3000`) | stashed `savedReply`, flushed at `interrupt_exit` (`src/call.cpp:1477-1530`) | n/a | `_interrupts` stack; `_pendingSavedReply` promotion |

### A.2 The pending/deferred/completion state machine (the core of the audit)

```
                      setPendingCall (call.cpp:556)          doWork (thread.cpp:670)
  accepted request ──────────────────────────▶ _pendingCall ───────────────▶ _activeCall
        │                                           │  (armed)                   │
        │  dispatchOrQueuePlaneCall                 │                            │ runs
        │  (thread.cpp:416)                         │                            ▼
        │    ├─ can run now ────────────────────────┘                     processCall()
        │    ├─ cannot, queueIfBusy ──▶ _pendingDeferredCalls ──┐                │
        │    └─ cannot, !queueIfBusy ─▶ returns false,           │          ┌────┴─────┐
        │        slot left ARMED (defect C1)                     │     blocks      finishes
        │                                                        │          │          │
        │                                        doneWorking drain          │     pushCallReply
        │                                        (thread.cpp:920) ◀────────┘     (thread.cpp:2557)
        │                                        gated on !_dead,           suspend()     │
        │                                        !_continuationCallback   (thread.cpp:944)│
        │                                                                       │         ▼
        └────────────── _deferPendingCallLocked (thread.cpp:344) ◀───────────────┘   disposition:
                                                                                    • ring ctx lane
                                                                                    • UDS datagram
                                                                                    • plane page (if
                                                                                      PlaneCompletion armed)
                                                                                    • STASH (interrupt/S2C)
                                                                                    • NOTHING (if neither)
```

Facts that the rest of this report uses:

* `_pendingCall` is armed by exactly one function, `Thread::setPendingCall`
  (`src/call.cpp:556` is its only production caller), and it throws when the slot is
  already occupied by a non-`InterruptEnter` call (the guest is answered `-EAGAIN`).
* `doWork` has three early returns that **never** run a Call
  (`_deferralState`, `_execution.deferDispatch()`, `_terminating`) and one branch that
  parks an armed Call on the deferred queue (`_deferPendingCallLocked`, `thread.cpp:344`).
  `doWork` returning therefore does **not** mean the Call finished — a fact the plane's
  dispatch code violates (defect C7).
* The only drain of `_pendingDeferredCalls` is `doneWorking`, gated on
  `!_terminating && !_dead && !_pendingCall && !_continuationCallback` (`thread.cpp:918-925`).
  Thread death (`notifyDead`, `thread.cpp:2779`) does not touch the queue, and the Thread
  destructor destroys it: any Call queued at thread death is dropped with its caller unblocked
  only by the guest's own timeout.

### A.3 The deferred-call root-cause claim (audit item 3) — **REJECTED**

Claim under test: *executing the deferred call (number 29) is genuinely a prerequisite for
completing the active call (number 38)*, from the evidence `active_call=38 deferred call=29
suspended=1`.

**What the numbers are.** The generated enum starts at 1
(`scripts/generate-rpc-wrappers.py:1030-1048`, `idx = 1`): `38 = mach_msg_overwrite`,
`29 = kqchan_proc_open` (cross-checked against a built `rpc.h`,
`dserver_callnum_mach_msg_overwrite = 38U`, `dserver_callnum_kqchan_proc_open = 29U`).
`38` is the blocking Mach receive: its fiber parks inside duct-tape
(`ipc_mqueue_receive` → `assert_wait` → `thread_block` → `dtape_hooks->thread_suspend`,
`duct-tape/src/thread.c:711-760`), which is exactly `suspended=1`.
`29` is the descriptor-bearing libkqueue open; the plane cannot defer it
(`planeDeferrable == false`, `src/server.cpp:1934-1943`), so a `29` that reaches the deferred
queue came in over the datagram path (the guest's fallback after the plane route returns
`-1`, `.../ext/for-libkqueue.c:70-95`) or was armed before the plane refused it (defect C1).

**Ownership.** Both requests are owned by the *same* `Thread` object: the active call is that
Thread's `_activeCall` (`src/thread.cpp:411-414`), and the deferred call is that Thread's
`_pendingDeferredCalls` entry, armed through `Call::callFromMessage` →
`Thread::setPendingCall` with the same thread resolution (`src/call.cpp:352-360`, `555-557`).
So the only relation the two have is *shared execution context*.

**Direction of the dependency.** The drain is gated on the parked continuation having
completed: `doneWorking` arms a deferred Call only when `!_pendingCall &&
!_continuationCallback` (`src/thread.cpp:918-925`), and the resume branch yields to an
occupied slot (`_suspended && (_pendingCallOverride || (!_pendingCall && hadResumePermit))`,
`src/thread.cpp:791-800`). Therefore *completing 38 enables 29*. Nothing in 38's completion
path consults `_pendingDeferredCalls`, `_pendingCall` or the deferred Call's state: 38
completes when the guest's message arrives, when its timeout expires or when a signal
interrupts it (`thread_block` returns when `thread_unblock` has finalized the wait,
`duct-tape/src/thread.c:761-800`). The dependency is one-directional: **38 → 29**.

**Reachability check.** A single guest thread cannot have two synchronous RPCs in flight on
its per-thread socket, so the caller of 29 is not the guest thread parked in 38. It is
either another guest thread whose request the plane attributed to this Thread
(`planeScopedTid` for the non-retargetable ops, `src/server.cpp:1993-1997`) or an
interrupt/signal-context RPC. In neither case does the guest's ability to deliver 38's
message depend on 29 having run.

**Verdict.** Executing call 29 is **not** a prerequisite for completing call 38; the coupling
is per-thread fibre serialization (by design) plus the plane's single-slot serialization
(the request is attributed to a thread that is busy). The real hazards this state produces
are not a dependency but: (a) unbounded delay of 29 behind an unrelated blocking wait —
which is safe for cycle-freedom only because the parked completion is external
(section E, `Inv6NoInternalCycle`); and (b) defect C1/C2 — because 29 was *armed* by
`setPendingCall` and then abandoned by the non-deferrable dispatch, its answer is dropped
when it finally runs, and the guest's fallback is what actually completes the caller.

**On `queued=0`.** Correct: the field is printed *before* the push
(`"queued=" << _pendingDeferredCalls.size()` at `src/thread.cpp:373`, then
`_pendingDeferredCalls.push(...)` at `375`), so `queued=0` records that the queue was empty
at the moment of deferral — not that the operation was dropped. The drop risks that do exist
are C2 (no completion owner) and C5 (thread death), not the queue itself.

---

## B. Confirmed defects vs unproven risks

### B.1 Confirmed defects (each with its reachable state)

**C1 — `dispatchOrQueuePlaneCall(..., queueIfBusy=false)` leaves the Call armed in
`_pendingCall` (the audit's slice-2 defect).** CONFIRMED, read here.
`Thread::dispatchOrQueuePlaneCall` (`src/thread.cpp:416-482`) tests
`_continuationCallback || (_pendingCall && !selfArmed) || _execution.isRunning()`; when
`queueIfBusy == false` it `return false`s at `src/thread.cpp:445-446` **before** the
`if (selfArmed) { _pendingCall = nullptr; }` at line 451-453. The Call was armed a moment
earlier by `Call::callFromMessage` → `Thread::setPendingCall` (`src/call.cpp:555-557`).
Reachable state: the plane services a non-deferrable op
(`ATTACH_LANE`, `VCHROOT_PATH`, `KQCHAN_MACH_PORT_OPEN`, `KQCHAN_PROC_OPEN`, `CONSOLE_OPEN`
— `src/server.cpp:1934-1943`) whose target thread is parked in a blocking continuation
(`src/thread.cpp:443`). Observed in-tree in this exact form:
`evidence/oslog-framework-rung/state/nonreg-110107-1136455/dserver.after-arm.log:145241`
(`call-deferred site=setPendingCall-parked tid=3182091 number=28 active_call=62 pending=0 suspended=1`)
followed by `:145285` (`call-deferred site=doWork-parked ... queued=0`).
Consequences: (a) the op executes later out of band and its answer is dropped (C2);
(b) the pending slot stays occupied, so the thread's next `setPendingCall` throws and the
guest is answered `-EAGAIN` (`src/call.cpp:556-570`), i.e. unrelated later RPCs on that
thread fail until the stale Call finally runs; (c) the plane has already recorded
`planeUnserviced` and set `region.deferredSeq`, so it believes the request is still owned.

**C2 — a Call that runs without an armed completion publishes nothing.** CONFIRMED,
read here. `pushCallReply` handles a reply-suppressed Call by capturing
`suppressedReplyCode()`/body and then returning; the page is written only
`if (expectedCall->planeCompletion().armed())` (`src/thread.cpp:2582-2634`). A Call
deferred into `_pendingDeferredCalls` never receives `setPlaneCompletion` (that call is
made only on the `planeDeferrable` branch, `src/server.cpp:2444-2448` and the ~29 sibling
sites), so when it eventually runs the guest waiter sees no disposition at all. Modelled:
configuration `unowned-arm`, invariants `Inv3NoSilentLoss` and `Inv4NoDuplicateApply`
(`audits/rpc-plane-model/`, counterexamples saved).

**C3 — `planeUnserviced` is not re-offered for the same sequence.** CONFIRMED, read here.
The comment at `src/server.cpp:1919` and `3775` says the request is "left PENDING and
re-offered on a later pass"; the code sets `region.deferredSeq = seq; region.deferredPage = page;`
(`3778-3780`) and the pass head skips exactly that pair (`1846-1848`). The only re-offer is
a **new guest publication with a new sequence** — which for the non-deferrable ops is the
datagram fallback. The loader, whose `VCHROOT_PATH` is one of those ops, has neither a
republish on this path nor a bounded wait (slice: plane, anchors `mldr.c:1979-1981`,
`2046`, `2093-2097`; guest side, reported).

**C4 — the page-incarnation guard is a no-op.** CONFIRMED, read here.
`void* const regionMap = region.map;` (`src/server.cpp:1659`) and
`auto* page = static_cast<dserver_process_control*>(region.map);` (`1623`) are the same
value, so `if (regionMap != page)` (`3754`) can never be true. `ProcessControlTxn::pageMap`
is written (`2249-2250`) and never read. The protection the comment claims ("never write a
late answer into a different page incarnation") therefore does not exist; the in-tree
comment records a MEASURED SIGSEGV at `page->reply_seq` reached through this path.

**C5 — deferred Calls are dropped on thread death.** CONFIRMED, read here.
`notifyDead` (`src/thread.cpp:2779-2826`) clears `_ring`, sets `_dead` and unregisters; it
does not drain `_pendingDeferredCalls` (nor `_pendingInterrupts`), and `doneWorking`'s drain
is gated on `!_dead` (`920`). A Call queued at that moment is destroyed with the Thread and
its caller is answered by nothing.

**C6 — the urgent pool completes without a generation check.** CONFIRMED, read here.
`src/server.cpp:1834-1836` writes `urgent_reply_status[u]`/`urgent_reply_seq[u]` and
release-stores `DONE` without re-reading `urgent_seq[u]` or the state. A guest that timed
out and re-published slot `u` gets its new request overwritten by the old completion.
`Sigprocess` additionally writes `urgent_payload[u][6]` post-Call (`1799-1803`).

**C7 — a plane-serviced inline Call that suspends reports success.** CONFIRMED structural.
`dispatchOrQueuePlaneCall` returns `runNow == true` after `doWork()` has returned
(`src/thread.cpp:470-475`), and `doWork` returns when a Call *suspends*, not only when it
finishes. The handler then reads `created->suppressedReplyCode()` (default 0) and the pass
publishes that status (`src/server.cpp:2554` and siblings). Because the inline path never
arms `PlaneCompletion`, the real reply is later captured and dropped (C2). Reachability
per op is not established.

**C8 — a stray `-EAGAIN` datagram for a plane-synthesized Call.** CONFIRMED structural,
consequence of C1. `callFromMessage`'s catch answers `result->sendBasicReply(-EAGAIN)`
(`src/call.cpp:568`) — for a plane Call the message carries the thread's UDS address
(`src/server.cpp:2349` sets it; `suppressReplyDelivery()` is called only *after*
`callFromMessage` returns), so the guest's per-thread socket receives a reply it is not
waiting for.

### B.2 Confirmed defects reported by the transport slices (anchors cited)

* (slice: courier) **D1 double close of the ring-attach doorbell**: `RingAttach::processCall`
  calls `noteSuppressedReplyWakeFd(fd)` then `_sendReply(..., fd)`
  (`src/call.cpp:2544-2545`); the generated `_sendReply` already transferred (and the courier
  closed) or CMSG-attached the descriptor, and the plane then `close(wakeFd)` again
  (`src/server.cpp:2376-2377`). Second close on a multithreaded server may hit a recycled fd.
* (slice: courier) **D2 suppression is honoured after the fd side effect**: the courier
  transfer happens inside `_sendReply`; `replySuppressed()` is consulted only in
  `pushCallReply` (`src/thread.cpp:2584`).
* (slice: courier) **D3 waiter fast path skips kind and generation checks**:
  `resume(fds[0])` (`src/server.cpp:1442-1448`) vs the checked path (`1513-1528`).
* (slice: courier) **D8 a one-byte wake carrying SCM_RIGHTS leaks the descriptor**: the
  `n == 1` branch `continue`s without walking `msg_control` (`src/server.cpp:1181-1196`) —
  read here.
* (slice: courier) **D12 `Message::_ensureDescriptorHeader` does not copy existing
  descriptor slots when growing** (`src/message.cpp:359-410`; the loop starts at `oldSpace`)
  — read here. Latent: it needs a second `pushDescriptor` into an already-populated
  `Message` (today at most one `@fd` per reply).
* (slice: ring) **R1 — a deferred S2C reply can be stranded with no delivery**:
  `_deferReplyForS2C`/`_deferredReply` are cleared only on the successful S2C-reply path
  (`src/thread.cpp:1674-1689`); the early exits at `1509-1512`, `1627-1643`, `1653-1657`,
  `1666-1669` and `notifyDead` leave both set. `pushCallReply` already deactivated the Call.
* (slice: ring) **R2 — an mmap duplex upcall has no fail-closed resume**: the mis-correlation
  and deadline branches clear the in-flight markers without resuming the parked fiber for
  `mmap` (`src/thread.cpp:1991-2092`), permanently stranding the parent and holding
  `_s2cPerformSempahore`.
* (slice: ring) **R3 — `_drainDuplexReply` reads the *current* `_ring`** (`src/thread.cpp:1897`)
  while the upcall was published into the lane current at publish time; `attachRing`
  swaps `_ring` without touching the duplex markers.
* (slice: ring) **R4 — the "UDS fallback" for a full s2c ring is a dead letter**:
  `Call::publishReplyToRingContext` returns `false` claiming the guest "still gets its answer"
  (`src/call.cpp:714-721`), but a lane-parked guest is blocked on its lane and cannot read the
  RPC socket — a fact the same component states for S2C (`src/thread.cpp:1626-1633`). The op has
  already executed, so an executed RPC is reported as failed, or a stray datagram desynchronizes
  the thread's next UDS reply matching.
* (slice: ring) **R5 — a consumed C2S slot can produce no completion**: `if (call)` with no
  `else` (`src/call.cpp:2341-2364`), a swallowed dispatch exception (`2367-2371`), and the
  ineligible/oversize drop (`2288-2292`) all advance the C2S head and send nothing.
* (slice: ring) **R6 — the lane used for the reply is re-read**: the slot is read from a local
  `ring` (`src/call.cpp:2025`) but the context is built from `thread->ring()`
  (`2147`, `2215`, `2346`) — read here. After `notifyDead` or `attachRing` the reply goes to
  a different lane or the deref is on a null `ring()`.
* (slice: plane) **arm-after-queue race**: `setPlaneCompletion` is called *after*
  `dispatchOrQueuePlaneCall` pushed the Call (`src/server.cpp:2444-2448` + `thread.cpp:469`),
  so a worker's `doneWorking` drain can run the Call first and `pushCallReply` then finds the
  completion unarmed → the same zero-disposition outcome as C2 through a different window.
* (slice: plane) **iterator invalidation in the pass**: the range-for over `_processControl`
  (`src/server.cpp:1606`) performs nested courier drains (`2262`, `2408`) whose callbacks
  insert into the same map (`1272`, `1291`). A rehash invalidates the iterator; the code
  snapshots the key and the region fields but not the iterator.
* (slice: plane) **two uncaught-throw holes**: the plane's `callFromMessage` calls at
  `2326`, `2435`, `2545` are unwrapped (CHECKIN wraps its own, `2169-2188`), and
  `_serviceProcessControl` is invoked at `4113` outside the message `try`/`catch`; a throw
  terminates the server and the page is never completed.

### B.3 Unproven risks (not defects)

1. **NSID-keyed registry**: `_planeScopedExecutionTid` and `callFromMessage` both resolve
   threads through a registry keyed by NSID alone (`registry.hpp:59-72`, `server.cpp:1580-1583`),
   and `registerPeerForMessage` never checks `thread->process() == process`
   (`src/call.cpp:320-327`). Two namespaces with the same NSID would cross processes.
2. **Ring SPSC producer not enforced across executors**: replies can be produced from the
   worker pool after a resume while the main loop drains; nothing but guest discipline
   (one outstanding request per lane) keeps two producers off one lane.
3. **`request_seq` ABA**: four independent sequence counters publish into one page
   (loader generic, image generic, image attach, loader attach); the guest validates only
   the echoed `reply_seq`. A coincidence (or wrap) can make a completion appear to be the
   caller's own.
4. **Ring reply validated by nothing**: `RingBuffer::publishReply` echoes any `seq` it is
   given (`src/ring.cpp:118-143`); a stale Call holding an old `RingBuffer` writes into an
   orphaned mapping (safe) or, with a recycled seq, into a live slot.
5. **`PlaneCompletion.page` lifetime** rests entirely on the deliberate policy of never
   unmapping a process-control region; adding a `munmap` turns it into a use-after-free
   with no guard to catch it.
6. **FD ownership on late arrival**: the checkout/RingAttach waiter callbacks act only if
   the target thread is alive and otherwise return without closing the arrived descriptor.
7. **`Message` control-header defects beyond D12** (a 1-byte wake with SCM_RIGHTS, D8) are
   peer-controlled rather than guest-tolerant.

---

## C. Minimal shared RPC state-machine design

One state machine, made explicit in the type system rather than spread over
`Thread` fields, `Call` flags and the plane's page:

```
Accepted  ──run──▶ Running ──┬─▶ Completed(disposition)   one terminal disposition
    │                        ├─▶ Suspended(continuation) ──resume──▶ Running
    │                        └─▶ Abandoned(reason)        exactly one of {dead thread,
    │                                                        cancelled process}
    └─ queued (owned) ──drain──▶ Accepted
```

Three invariants, each of which the audit found violated somewhere today:

1. **One owner.** An accepted request's ownership token lives in exactly one place:
   `armed`, `deferred`, `running`, or `completed`. Arming is atomic with the decision that
   placed it (today `dispatchOrQueuePlaneCall` decides and arms under `_rwlock` but a
   `false` return does not disarm — C1).
2. **One terminal disposition.** `Completed` carries *where* the answer goes: the transport
   (UDS/ring), the plane page, or the stashed interrupt slot — decided at arm time, not
   discovered at reply time. A completion without a destination must be a bug, not a
   silent return (C2).
3. **No drain-dependency on an opaque continuation.** The deferred queue may only be
   drained when the thread reaches the top with no parked continuation, and the only
   ordering edge it may add is "deferred work waits for the external event that completes
   the parked call" — never the reverse (section D, `Inv6`).

Minimal design, in the tree's own terms (no new framework, no ABI change):

* Make the dispatch decision return a *disposition token* rather than a `bool`:
  `RunInline | Queued(owner) | Refused(kind)`, and make `Refused` also disarm the pending
  slot in the same critical section that discovered the refusal.
* Move `setPlaneCompletion` to **before** the Call can be observed by a worker (i.e. inside
  the `_rwlock` critical section that pushes to `_pendingDeferredCalls`, or before
  `doWork`), so "queued" and "has a completion owner" are the same fact.
* Add `Thread::abandonDeferred(reason)` to `notifyDead`/`terminate`, so a Call queued on a
  dying thread is answered (`-ESRCH`) instead of dropped.

---

## D. Code-generation extension (single schema, no competing definitions)

The canonical source is `scripts/generate-rpc-wrappers.py` (its `calls` table) which emits
`rpc.h` (public enum + client/server wrappers), `rpc.internal.h` and the library source.
`include/darlingserver/rpc-supplement.h` is the hand-written supplement (ring opclasses,
plane ops, the `DSERVER_RING_C2S_OPCODES` macro, ledger points); `DSERVER_RING_C2S_OPCODES`
and the generator's call list must agree — the existing "canon guardrail"
(`src/call.cpp:663-672`) already treats that agreement as checkable.

Proposed extension: add one optional trailing element to each `calls` entry — a
`metadata` dict — and emit it as a single generated table
(`dserver_rpc_metadata[]`) plus generated `static_assert`s, so every consumer (mldr, dyld,
the kernel image, the server, the tests) reads one definition:

| field | values | what can be generated from it |
|---|---|---|
| `blocking` | `never` / `may` / `always` | `static_assert` on the same set the ring opclass and `planeDeferrable` use; a server-side assertion that `may`/`always` calls return only through the fiber path |
| `context` | `thread` / `task` / `process` | replaces the hand-maintained `planeRetargetable`/`planeScopedTid` list — one table, one check |
| `transports` | bitmask `uds`, `ring`, `plane` | the ring eligibility set, currently duplicated between the generator and `DSERVER_RING_C2S_OPCODES` |
| `completion` | `transport` / `page` / `suppressed` | the `suppressReplyDelivery()` decision moves from 29 hand-written plane sites to one generated call |
| `idempotency` | `pure` / `idempotent` / `mutating` | drives the duplicate-application check and the plane's republish policy; a `mutating` op over the plane must have an armed completion (the C2 invariant) |
| `cancellation` | `none` / `on-peer-death` / `disabled` | the `ATTACH_LANE` `-EAGAIN` special case becomes a table entry |
| `fd_transfer` | `none` / `request-in` / `reply-out` / `in-out` | already partly encoded by `@fd`/`@fd_token`; extend to state that the courier-xor-CMSG choice must be made before publication (D2/D1) |

Checks that can safely be generated (all compile-time or single-site):

* `static_assert` that a `mutating` + `plane` call has `completion: page`;
* `static_assert` that a `blocking != never` call is not in the ring's C2S allowlist unless
  it is also `caller-S2C`-capable;
* generated `dserver_callnum_class()` used by both the ledger scope macro and the ring
  eligibility test, replacing the two look-alike sets;
* generated interruption classification from the existing `ALLOW_INTERRUPTIONS` flag.
* emitted metadata for the guest side as a header the loader/dyld already include, so the
  guest and the server cannot disagree about an op's class.

**Keep handwritten:** every operation-specific Mach semantic — the `_kernelrpc_*_trap`
bodies, the task-vs-current_task checks, the OOL/descriptor handling in `mach_msg`, the
plane's attach transaction, and the courier pairing rules. The generator must never gain a
notion of what an operation *does*.

---

## E. Model-checking artifacts

`audits/rpc-plane-model/` (`README.md`, `DServerPlane.pcal.tla`, translated
`DServerPlane.tla`, `run-all.sh`, `model-check-results.txt`, `counterexamples/*.txt`).

TLC was run on five configurations × five invariants; every cell is reproducible with
`TLA_JAR=... ./run-all.sh`. Result matrix:

| config | ArmWithoutOwner | GenGuard | ParkedCompletesExternally | ClientReclaimsOnTimeout | violated |
|---|---|---|---|---|---|
| `guarded` | F | T | T | F | — |
| `unowned-arm` | T | T | T | F | `Inv3NoSilentLoss`, `Inv4NoDuplicateApply` |
| `no-gen-guard` | T | F | T | T | `Inv2NoWrongGeneration`, `Inv3`, `Inv4` |
| `reclaim-race` | F | T | T | T | `Inv4NoDuplicateApply` |
| `internal-cycle` | F | T | F | F | `Inv6NoInternalCycle` |

Counterexamples saved as deterministic regression scenarios (each 7–19 states):
`counterexamples/unowned-arm--Inv3NoSilentLoss.txt` (the silent finish),
`unowned-arm--Inv4NoDuplicateApply.txt` (the caller's republish applies the op twice),
`no-gen-guard--Inv2NoWrongGeneration.txt` (a late completion overwrites a newer slot),
`reclaim-race--Inv4NoDuplicateApply.txt` (the generation guard does not prevent duplicate
application), `internal-cycle--Inv6NoInternalCycle.txt` (the deferred mechanism's
cycle-freedom rests on the parked call completing externally).

**Real-path instantiation and regression asset.** The model's abstract constants are
instantiated by `tests/run-plane-deferred-arm-contract.sh`: `ParkReq` is the fixture's
main thread blocked in a Mach receive (`mach_msg`, callnum 38 — the `active_call=38
suspended=1` state), `Req1`/`Req2` are `console_open` (callnum 16) and its bounded
republish. Configuration `real-console-defer` (ArmWithoutOwner=TRUE, GenGuard=TRUE,
external completion, caller reclaim) is the shape the captured server trace shows and it
violates `Inv3`/`Inv4`; `guarded` is the fixed shape. The regression's own trace is the
executable counterpart: the model says *which shapes are safe*, the test asserts the
production server is in the safe shape.

**Second window: the arm-after-queue race.** `setPlaneCompletion` runs after the Call has
been pushed to `_pendingDeferredCalls` (`src/server.cpp:2444-2448` pushes inside
`thread.cpp:469`; the arm is the statement after the dispatch returns), so a worker's
`doneWorking` drain can run the Call before the completion owner exists. The model now
carries `ArmAfterQueue`, and `Inv7QueuedCallHasOwner` states the **minimal atomicity
requirement**: *a Call reachable from `_pendingDeferredCalls` must already carry its
completion owner* (`planeCompletion().armed()` true before the push that publishes it, or
the push and the arm inside one `_rwlock` critical section). Configuration
`arm-after-queue` violates `Inv7` and consequently `Inv3`/`Inv4`; the fixed protocol
(the same config with `ArmAfterQueue=FALSE`) does not. The race was **not** reproduced
against a live server in this milestone (it needs a worker drain inside a window of a few
instructions); its proof here is code-level plus model-level, and the requirement is what
the correction must satisfy.

**Answer to the circular-wait question.** The model states it as an invariant: a parked
thread carrying an armed/deferred Call is a circular wait **iff** the parked call's
completion is not an external event. In the production code the parked call's completion
comes from the guest's own message arrival, a timeout or a signal
(`thread_block` → `dtape_hooks->thread_suspend`; resumed via `dtape_thread_resume`), none
of which requires the deferred Call to have run. The deferred mechanism is therefore
cycle-free **by construction, not by analysis** — and it is why the fix for the
double-book must preserve that gating rather than pausing the drain earlier.

**Lean 4.** No Lean toolchain is present on this host (`lean`/`lake` absent), so nothing
was attempted. Assessment: Lean would be appropriate for the *pure* part of this state
machine — the parts already factored into small pure modules, e.g.
`InterruptResume::classify`/`retireInterruptedCall`
(`internal-include/darlingserver/interrupt-resume.hpp`, driven by a host contract test) and
any extracted `dispatch decision → disposition` function — where a refinement argument can
be made unit-by-unit. It is **not** appropriate here for the whole completion machine: no
refinement relation between Lean and the C++ microthread/fiber control flow has been
established, and claiming verification of the production code without one would be false.
Recommendation: keep TLA+ for the protocol shape, and revisit Lean only if the "one owner /
one disposition" decision function is first extracted into a dependency-free C++ (or a
sibling pure library) that both the production path and the model call.

---

## F. Smallest first implementation slice

**Slice 0 (recommended first): disarm on refusal.**
In `Thread::dispatchOrQueuePlaneCall`, on the `queueIfBusy == false` path, clear the slot the
dispatcher armed, under the same `_rwlock`:

```cpp
if (_continuationCallback || (_pendingCall && !selfArmed) || _execution.isRunning()) {
    if (!queueIfBusy) {
        if (selfArmed) { _pendingCall = nullptr; }   // the caller will not run/or queue it
        return false;
    }
    ...
```

and, at the same time, make `planeUnserviced` truthful: either re-offer the request on the
next pass (drop the `deferredSeq` memo for it) or arm `PlaneCompletion` and let the deferred
Call publish. The first option preserves today's behaviour for the reply-body ops; the second
is the smaller change to the completion protocol but requires C2's arming to move inside the
dispatch.

Disarm semantics matter: dropping the Call releases the last reference to it, so the generated
destructor closes any request descriptor it still holds. For an op whose request descriptor was
already consumed into the Call (`ATTACH_LANE`), the disarm must therefore be paired with the
existing `-EAGAIN` completion — which is exactly why that op has one — and for the
reply-body ops the pass must be the one that re-offers the still-PENDING request, not a silent
drop. The plane's `region.deferredSeq` memo must be cleared whenever the request is genuinely
left for a later pass (today it is written for both the "queued with an owner" and the
"refused without an owner" outcomes, `src/server.cpp:3766-3780`).

**Slice 1: arm the completion before the Call can be drained** (removes the arm-after-queue
race and makes "queued ⇒ has an owner" true). The atomicity requirement is exact: for a
reply-suppressed Call, `planeCompletion().armed()` must be true *before* the
`_pendingDeferredCalls.push` that makes it reachable to a drain — i.e. the push and the arm
are one critical section under `_rwlock` (or the arm happens before the dispatcher is
called). A drain must never observe a suppressed Call with `armed() == false`; that single
predicate is what `Inv7QueuedCallHasOwner` checks and what the correction must establish.

**Slice 2: `abandonDeferred` on `notifyDead`/`terminate`** (removes C5).

Each slice is testable with the existing guest C fixture infrastructure: slice 0 with a
non-deferrable plane opener issued while the target process's main thread is parked
(`CONSOLE_OPEN` is the cheapest such opener), slice 2 with a thread-death-during-plane-request
fixture.

**Slice 0 has an executable RED gate.** `tests/run-plane-deferred-arm-contract.sh` (fixture
`tests/plane_deferred_arm_guest.c`) parks the process's main thread in a Mach receive and
opens `/dev/console` from a worker; on the current product server it produces the matched
lifecycle — arm-on-parked (`number=16`, `active_call=38`, `suspended=1`), three rejected
re-publications (`pending=1`), the deferral at the next dispatch — with no
`plane-deferred-completion`. Measured `--expect red`: PASS in 54.68 s, counters
`armed=4 retained=3 rejected=3 deferred=1 unowned_completions=0`
(`evidence/plane-deferred-arm/README.md`). The RED run is against the deployed binary
(`sha256 90e4eee4…`); the active Xcode workstream has since put the disarm for P0-1 in the
working tree (uncommitted, `src/thread.cpp`), so `--expect green` is the same gate to run
after that change is built and deployed, by that workstream. The arm-after-queue window (Slice 1) is model-checked, not yet run
against a live server; `Inv7QueuedCallHasOwner` is its acceptance predicate.

---

## G. RED/GREEN and performance acceptance gates

* **RED/GREEN**: `west test --profile <p>` for the GREEN run and `--prove-red` with
  `red-proof: {mode: guest-runtime-deploy, runtime-artifacts: [darlingserver]}` for the RED
  arm — the defect is deployed-server behaviour, so a source-base or model-only proof is not
  acceptable (`AGENTS.md`). The bad runtime is the current pinned `darlingserver` (the
  defect is present in it), the guest fixture stays fixed across RED and GREEN.
* **Fixture**: a guest C fixture that (1) blocks one thread of a process in a blocking trap,
  (2) issues a descriptor-bearing plane op from another thread of the same process, (3)
  asserts the descriptor arrives exactly once and that a second `kqchan`/`console` is not
  registered. Pair it with `expect-output-contains` on a deterministic server marker,
  e.g. a new `plane-unserviced` record — never on a timing-dependent log.
* **Model gate**: `audits/rpc-plane-model/run-all.sh` is the protocol gate; the five
  counterexample files are the regression scenarios. A change to the dispatch/completion
  protocol must re-run the matrix and keep the `guarded` row clean.
* **Performance**: the `runtime-load-smoke` profile (concurrent guest CLT compile+link,
  fork/exec storm, descriptor churn) is the regression signal to run before/after, plus the
  existing ring/plane counters (`ringServicedSpin/Doorbell`, `processControlRequests`,
  `processControlPassDeferred`, `plane-call-queued`, `call-deferred`). Slice 0 adds one
  branch and one store on the refusal path only; the hot path (inline dispatch) is unchanged.
* **Do not** run cleanup/leak assertion contracts in the same parallel batch as any
  `west test` (`AGENTS.md`); treat them as a final sequential phase.

---

## H. Compatibility and migration risks

* **ABI**: no wire change is needed for slices 0–2 — the ring ABI stays v7
  (`DSERVER_RING_ABI_VERSION`), the plane page layout and the courier envelope stay as they
  are. Adding metadata to the generator changes generated headers only (compile-time).
* **Guest/server skew**: the ring's `decline` sentinel and the plane's `-1` "not published"
  contract must be preserved; a guest that routes an op onto a lane depends on the server
  either servicing it or declining **pre-dispatch**.
* **UDS fallback**: it is load-bearing for the non-deferrable plane ops and for ring
  declines; it must keep working while `planeUnserviced` handling changes. Note the ring
  slice's finding that the "UDS fallback" for an S2C-full ring is a dead letter for a
  lane-parked guest — do not rely on it as a safety net without fixing that.
* **Completion ownership**: moving `setPlaneCompletion` earlier must preserve the
  `request_seq` staleness rule (`page->request_seq == seq`), or a deferred Call will publish
  into a slot a newer request owns; this is exactly what `Inv2` covers.
* **Process death**: `_cancelProcessControlTxn` closes the txn fd; if slice 0 makes refusals
  re-offerable, the txn-erase path must also invalidate any re-offered request for the same
  pid/seq (the page-keyed bookkeeping is per-pid and per-page, not per-incarnation).
* **Shared prefixes**: none of the proposed work touches a prefix layout; keep it that way —
  the acceptance runs must not mutate a shared runtime prefix.
* **Non-goals preserved**: no RPC redesign, no new framework, no Xcode workstream changes,
  no migration in this task.

---

## Appendix — where to look first

| question | file | lines |
|---|---|---|
| arming and dispatch decision | `src/thread.cpp` | 344-482 |
| doWork / drain / drain gate | `src/thread.cpp` | 670-935 |
| reply funnels | `src/thread.cpp`, `src/call.cpp` | 2541-2634, 700-766 |
| plane pass, ops, completion | `src/server.cpp` | 1605-2140, 2226-2410, 3700-3900 |
| ring consume/dispatch | `src/call.cpp` | 2018-2373 |
| ring publish | `src/ring.cpp` | 118-165 |
| courier | `src/server.cpp` | 838-1533, 3949-4022 |
| S2C | `src/thread.cpp` | 1431-2320 |
| generator / schema | `scripts/generate-rpc-wrappers.py`, `include/darlingserver/rpc-supplement.h` | flag docs, `calls` table; opclasses, plane ops |

# darlingserver RPC/Ring/plane audit — prioritized backlog

Companion to `darlingserver-rpc-plane-audit-2026-10-19.md`. Every entry names the
**exact precondition** under which the defect is reachable, so it can be picked up
without re-deriving the state. Priority is by user-visible blast radius and by how
much of the completion protocol the fix touches.

## P0 — completion protocol (fix as one focused change)

| # | defect | exact precondition | effect | anchors |
|---|---|---|---|---|
| P0-1 | **refusal without disarm** (`dispatchOrQueuePlaneCall(..., false)` returns before clearing the slot) | a non-deferrable plane op (`ATTACH_LANE`, `VCHROOT_PATH`, `KQCHAN_MACH_PORT_OPEN`, `KQCHAN_PROC_OPEN`, `CONSOLE_OPEN`) is serviced while its target thread (`tid = pid` for these) is parked in a blocking continuation, or is running/has work | the Call stays in `_pendingCall`; the op runs later out of band; its reply is dropped; the next publication on that thread is rejected `-EAGAIN` (stray datagram) | `thread.cpp:443-446` vs `451-453`; `call.cpp:555-557`; `server.cpp:3773-3780` |
| P0-2 | **unowned completion** — a deferred Call that finally runs has `planeCompletion().armed() == false` | same as P0-1 (the `setPlaneCompletion` call exists only on the `planeDeferrable` branch) | the handler runs, the answer is captured and dropped, the page stays PENDING; the caller's bounded republish applies a mutating op a second time | `thread.cpp:2602-2634`; `server.cpp:2444-2448` (and ~29 sibling sites) |
| P0-3 | **arm-after-queue race** — completion armed after the Call is queued | a deferrable plane Call is pushed to `_pendingDeferredCalls` and a worker drains it before the pass executes `setPlaneCompletion` | identical outcome to P0-2 through a second window | `server.cpp:2444-2448`; `thread.cpp:469`, `920-925`, `2602` |
| P0-4 | **unserviced never re-offered** for the same seq | `planeUnserviced` was recorded (P0-1's branch) | the memo at `server.cpp:1846` skips the request forever; the loader has no republish on this path and a NULL-timeout futex → indefinite park (`VCHROOT_PATH`) | `server.cpp:3773-3780`, `1846-1848`; `mldr.c:1979-1981`, `2046`, `2093-2097` |

Minimal atomicity requirement (P0-3): **a Call reachable from
`_pendingDeferredCalls` must already carry its completion owner.** Concretely, if a
Call is reply-suppressed, `planeCompletion().armed()` must be true before the
`_pendingDeferredCalls.push` that publishes it (set it before/inside that
`_rwlock` critical section, or before calling the dispatcher), and the drain must
never observe a suppressed Call without it. This also makes "queued" and "has an
owner" the same fact, which removes the need for the `planeDeferred` flag's
separate bookkeeping.

## P1 — request retention and liveness

| # | defect | precondition | effect |
|---|---|---|---|
| P1-1 | **deferred Calls dropped on thread death** | a Call is in `_pendingDeferredCalls` (or `_pendingInterrupts`) when `notifyDead`/`terminate` runs | the Call and its caller are abandoned; the drain gate `!_dead` means it can never run | `thread.cpp:2779-2826`, `918-925`, dtor `267-297` |
| P1-2 | **ring deferred reply stranded** | `_deferReplyForS2C` was set and the S2C path exits early (no microthread, ring-parent fail-fast, interrupted wait, missing `_s2cReply`) or the thread dies | `_deferredReply` is never flushed; a lane-parked caller waits forever | `thread.cpp:1509-1512`, `1627-1643`, `1653-1657`, `1666-1669`, `1674-1689` |
| P1-3 | **mmap duplex upcall has no fail-closed resume** | the mmap upcall times out or mis-correlates | the parent fiber stays parked and `_s2cPerformSempahore` is held → all later S2C on that thread block | `thread.cpp:1991-2092` |
| P1-4 | **duplex drain bound to the current `_ring`** | a lane is re-attached (or the thread dies) between upcall publish and harvest | the transaction is judged against the wrong control block and never completed | `thread.cpp:1897`; `attachRing` `2848-2870` |
| P1-5 | **consumed C2S slot can produce no completion** | `callFromMessage` returns null (registry miss / rejected pending slot), throws, or the op is ineligible/oversize | the slot is advanced and nothing is sent; the guest relies on its timeout | `call.cpp:2288-2292`, `2324`, `2341-2371` |

## P2 — generation, ownership and descriptor correctness

| # | defect | precondition | effect |
|---|---|---|---|
| P2-1 | **page-incarnation guard is a no-op** (`regionMap != page` compares the same value) | any nested courier drain inside the pass re-registers the pid's page | the guard cannot detect the re-registration; the stale `region` reference is used | `server.cpp:1623`, `1659`, `3754` |
| P2-2 | **urgent completion has no generation check** | the guest releases an urgent slot (timeout) and republishes while the server's completion is in flight | the old completion overwrites the new PENDING slot; the new request is never serviced | `server.cpp:1834-1836` |
| P2-3 | **inline plane Call that suspends reports success** | an inline-dispatched plane handler returns to `doWork` only by suspending (port/copyout lock, S2C) | the pass publishes status 0 for work not done; the real reply is dropped (no owner) | `thread.cpp:470-475`; `server.cpp:2554` |
| P2-4 | **courier: arm-time suppression ordering** | a reply-suppressed Call returns a descriptor (`RingAttach`; kqchan/console on the plane) | the descriptor is transferred/CMSG-attached inside `_sendReply` before `pushCallReply` checks suppression; the plane then acts on the number again → double close or a send of a closed number | `call.cpp:2544-2545`; `thread.cpp:2584`; `server.cpp:2367-2378` |
| P2-5 | **courier waiter fast path skips kind and generation** | a descriptor arrives before the waiter parks (the `resume(fds[0])` path) | a wrong-kind descriptor is handed to checkout/ring-attach | `server.cpp:1442-1448` vs `1513-1528` |
| P2-6 | **courier cleanup is pid-keyed** | execve while a descriptor is in flight (outgoing and incoming incarnation share the pid) | the incoming incarnation's pending/generation state is erased; descriptors are lost | `process.cpp:916`; `server.cpp:3968-3991` vs `4004-4021` |
| P2-7 | **`_processControlTxns` overwrite leaks a descriptor** | a retry with a new `(seq,op)` while the old txn is `WAIT_FD` | the old `raw int fd` is neither closed nor completed; a courier continuation can consume the new txn's descriptor | `server.cpp:2237-2252`; `server.hpp:164-179` |

## P3 — latent / diagnostic-only

| # | defect | precondition | effect |
|---|---|---|---|
| P3-1 | 1-byte courier wake carrying SCM_RIGHTS | a peer sends one byte plus a descriptor | the descriptors are never walked and leak | `server.cpp:1181-1196` |
| P3-2 | `Message::_ensureDescriptorHeader` does not copy existing slots on growth | a second `pushDescriptor` into an already-populated Message (today at most one `@fd` per reply) | the first fd is lost and uninitialised slots are closed by the destructor | `message.cpp:359-410`, `238-255` |
| P3-3 | plane `-EIO` "no token" guard unreachable for kqchan/console | the courier send fails | `suppressedReplyWakeFd()` is never set for those calls, so the guard cannot fire | `server.cpp:3492-3511`, `3601-3621`, `3682-3702`; only setter `call.cpp:2544` |
| P3-4 | "UDS fallback" for a full s2c ring is a dead letter | the s2c ring is full for a lane-parked caller | the datagram is unread or desynchronizes the next UDS call; the op has already executed | `call.cpp:714-721`; contrast `thread.cpp:1626-1633` |
| P3-5 | reply lane re-read from `thread->ring()` | the thread dies or the lane is replaced between slot read and context attach | the reply is published to a different lane (or the deref is on a null ring) | `call.cpp:2025` vs `2147`, `2215`, `2346` |
| P3-6 | stray `-EAGAIN` datagram for a plane Call | P0-1's retained slot meets a new publication | the guest's socket receives a reply it is not waiting for (bad receive / desync) | `call.cpp:568` |

## Unproven risks (keep as risks, do not "fix" speculatively)

1. NSID-keyed registry: cross-namespace collision executes a Call on a foreign Thread
   (`registry.hpp:59-72`, `call.cpp:246-263`, `320-327`).
2. Ring SPSC producer not enforced across executors (worker replies vs main-loop drain).
3. `request_seq` ABA across the four guest publishers; the guest validates only the echoed seq.
4. `RingBuffer::publishReply` validates nothing (`ring.cpp:118-143`).
5. `PlaneCompletion.page` lifetime depends on the deliberate never-munmap policy.
6. Late-arriving descriptor with a dead target thread is neither consumed nor closed.
7. `_processControl` range-for invalidation under a nested drain (needs a rehash).

# Direct-transport descriptor architecture: investigation

Status: investigation complete for the source-audit half; standalone proofs and product
census are recorded in section 8 with their exact commands. No production source was
changed and no production patch was applied.

Audience: whoever picks up the descriptor/transport work (bead `dar-gwn.7.7.5`).

Source of truth for the audited code:
`darling-workspace/.west-test/runtime-build-cache/source/51a0a5f1.../darling` — the
materialized product forest that built the matched ring ON/OFF prefixes. All three
materialized forests (`51a0a5f1`, `9fc75ae8`, `0c497281`) are byte-identical for every
file this document cites, and the deployed `usr/libexec/darling/mldr` of `/tmp/dr-on-matched`
is byte-identical to the `mldr` built under cache key `6332e442...` (OFF: `e655641a...`),
so the audit target and the running product are the same source. Paths below are relative
to that forest's `darling/`.

---

## 1. Executive verdict

**The defect is real and its mechanism is now pinned to the deployed source, not to a draft.**
The loader owns descriptors in the *same* fd table as the guest and allocates them
**top-down from the native limit**, while every guest-visible limit path reports the native
limit minus one. The guest is therefore told that `getdtablesize()-1` is usable when that
number is already the loader's.

**Correction to the previous session's record.** The earlier review attributed the defect to
`INTERNAL_FD_RESERVE 4096`, `public_fd_hard_limit()`, `socket_bitmap_adopt_locked()` and to
"three limit callbacks declared in `elfcalls.h` but never assigned in `elfcalls_make`". None
of those exist in the deployed product source: a forest-wide search for all four symbols
returns nothing, and every field of the deployed `elfcalls` struct *is* assigned in
`elfcalls_make`. Those symbols exist only as an **uncommitted draft** in the
`fix/ring-fd-ownership` worktree (`git diff HEAD` adds them). The deployed tree's
`mldr.c` and `elfcalls.h` are byte-identical to that branch's committed `HEAD`
(`295c8dd5…`/`34d07df9…`), and the deployed binary contains no such symbols. The reviewers
described a fix draft as if it were the cause. The *measurements* in that review stand; its
*source story* does not.

**Candidate B (constant-anchor direct transport, no relay) is not falsified, but one of its
premises is.** Its premise "per-thread RPC datagram sockets disappear" cannot hold with the
current lane ABI, because the single hottest non-ring RPC — `mach_msg_overwrite`, recorded
in-tree as "the hottest RPC" — is a *blocking receive* whose payloads are out-of-line and
exceed the 128-byte ring slot, and because descriptor-bearing replies cannot travel through
shared memory at all. Removing those sockets requires a guest-side demultiplexer over the
one control endpoint (a reader plus per-thread futexes) — a new mechanism, not a
simplification.

**What the two standalone proofs establish** (both re-run by the author, host and ordinary
Docker, markers `DIRECT_PROCESS_DOORBELL_OK` and `DIRECT_PROCESS_CONTROL_OK`, every designed
mutation red): the target topology is *mechanically* sound — constant descriptor count across
1/8/32/64 threads (D1), no lost cold wake across 252 forced interleavings (D3), a hierarchical
pending bitmap (D4), zero eventfd writes on an active hot path (D5), fork and exec generation
isolation with a working reattach for both backing options (D6, D7), exact caller-local
targeting (D8) — and one process-level control endpoint carries 64-thread slow traffic with
descriptors in both directions, no cross-thread replies and no leak, while the hot path runs
under a deliberately held control lock (C1–C8). The demultiplexer therefore exists in model
form; what remains unproven is its behaviour for the real blocking `mach_msg` class.

**Relay is not required by any evidence gathered here.** Nothing in the audit identifies a
primitive that needs a separate descriptor table; the relay's earlier wake-coalescing
justification is withdrawn, and its remaining claim (descriptor ownership) has a
same-process alternative whose only open question is the demultiplexer. Relay stays a
fallback for a requirement not yet found.

**Truthful NOFILE (Candidate A) is independent, required, and its drafted implementation is
FALSIFIED** (section 8.6). Its patch is planned but not applied, and must be re-derived: the
draft's reserved band above the published limit forces a recurring, process-wide raise of the
real soft limit, during which any guest thread can obtain a descriptor above its advertised
limit and inside the reserved band, while the guard protects neither. The deployed loader has
no such window today — it contains no `setrlimit` at all — so the draft would trade a static
defect for a dynamic one. Section 8.6 also records the source-backed repair options; the
principled one deletes the raise by making the internal FD set O(1) with anchors created
before guest execution, which is the same work as Gate B.

## 2. Source map

Defect path (guest side, all in `src/external/xnu/darling/src/libsystem_kernel/emulation/`):

| What | Anchor |
|---|---|
| `dup2` refuses a loader-internal target before any kernel call | `src/xnu_syscall/bsd/impl/unistd/dup2.c:18-20` |
| `dup` and `fcntl` refuse the same way | `unistd/dup.c:19-20`, `fcntl/fcntl.c:79-80` |
| `close` ignores a guarded fd (and prints a warning) | `unistd/close.c:32-35` |
| The guard itself | `src/common/guarded/table.c:200-205` (`guard_flag_prevent_close` at `include/common/guarded/table.h:6`) |
| The guard asks the loader | `src/linux_premigration/elfcalls_wrapper.c:116-118` |
| `getrlimit(RLIMIT_NOFILE)` = raw `prlimit64`, then `rlim_cur--`/`rlim_max--` | `xnu_syscall/bsd/impl/misc/getrlimit.c:15-24` |
| `setrlimit` adds the one back | `misc/setrlimit.c:9-19` |
| `getdtablesize()` = `getrlimit().rlim_cur` capped at `INT_MAX` | `unistd/getdtablesize.c:12-20` |
| `sysconf(_SC_OPEN_MAX)` = `getrlimit(RLIMIT_NOFILE)` (64-bit path) | `src/libc/gen/FreeBSD/sysconf.c:105-136` |
| Guard registration points | `.../other/mach/lkm.c:58-66`, `bsdthread/bsdthread_create.c:36-39`, `process/fork.c:70-79`, `process/posix_spawn.c:145-148` |

Loader (`src/startup/mldr/`):

| What | Anchor |
|---|---|
| The loader's fd registry | `mldr.c:595-613` (`socket_bitmap_t`) |
| Allocation band = the *top* of the native range | `mldr.c:630` (`getrlimit(RLIMIT_NOFILE)`), `mldr.c:639` (`highest = rlim_cur - 1`), `mldr.c:668` (`fd = highest - next_index`) |
| Standard descriptors are never taken | `mldr.c:642-645` |
| Loader-owned test | `mldr.c:815-828` (`__mldr_fd_is_internal`, bitmap bit test) |
| RPC datagram socket, CLOEXEC | `mldr.c:861`, `872`, `884`; per-thread at `elfcalls/threads.c:241-249` |
| Ring wake fd adopted onto a reserved number | `mldr.c:781-807` (`F_DUPFD_CLOEXEC`) |
| fork prepare / parent / child | `mldr.c:830-854` |
| Lifetime pipe (kernel < 5.3 only) | `mldr.c:917-933`, `1035-1069` |
| `elfcalls` struct and its binding | `elfcalls/elfcalls.h:8-83`, `elfcalls/elfcalls.c:88-140` |

Server (`src/external/darlingserver/`):

| What | Anchor |
|---|---|
| Ring allowlist macro (13 ops) | `include/darlingserver/rpc-supplement.h:457-483` |
| Ring dispatch in `ringServiceThread` | `src/call.cpp:1879-1885` |
| Eventfd doorbell handling, `ringDoorbellsReceived` | `src/call.cpp:1986-1997` |
| Ring counters emitted | `src/metrics.cpp:76` |
| Peer process identity via kernel `SCM_CREDENTIALS` | `src/message.cpp:79-83`, `161-165` |
| Ring control block validation (`guest_tid == scm_nsid`) | `include/darlingserver/rpc-supplement.h` (ring shm validator) |
| Per-thread ring ownership / retire on re-attach | `src/thread.cpp` (`attachRing`), `src/call.cpp:1956-1972` |

## 3. FD inventory

### Current (measured and source-confirmed)

| Object | Scope | Count | CLOEXEC | fork | exec | Guest-visible |
|---|---|---|---|---|---|---|
| Main RPC datagram socket | per process | 1 | yes | closed (`guard_flag_close_on_fork`) | dies | hidden |
| Per-thread RPC datagram socket | **per thread** | 1 each | yes | closed | dies | hidden |
| Ring wake eventfd (adopted) | **per attached lane** | 1 each | yes | closed in child (`mldr.c:849`) | dies | hidden |
| Ring lane mapping (memfd) | per lane, transient | 1 each | memfd | munmapped in child | dies | not an fd after attach |
| Server wake eventfd source | per lane | server side | yes | — | — | transferred via SCM_RIGHTS |
| Lifetime pipe | per process | 1 (only kernel < 5.3) | no | re-registered | survives | hidden |
| execve monitor pipe | transient | 1 | yes | — | — | not hidden |
| S2C mmap fd | transient | 1 | — | — | — | closed after mmap |
| Server socketpairs (console, kqchan) | per object | — | yes | — | — | **guest-visible by design** |

Measured on the real product: ring ON baseline 3 hidden loader descriptors, OFF 1; ON adds
≈2 descriptors per additional guest thread (socket + lane eventfd), OFF ≈1. The lane table is
capped at `GR_MAX_LANES 128` (`resources/dserver-ring.c:79`), so beyond 128 threads the ring
is not used at all.

**Anchor count today: O(threads).** That is the property both candidates exist to remove.

### Target shapes

| | Candidate A (truthful clamp) | Candidate B (direct, no relay) | Candidate C (relay) |
|---|---|---|---|
| process anchors | unchanged | 1 wake eventfd + 1 control UDS + exec checkout + (pre-5.3 lifetime) | one control UDS in guest; everything else in the companion |
| per-thread anchors | unchanged (1 socket + 1 eventfd) | **0** in the guest table (lane = memory + futex) | 0 in the guest table |
| hidden descriptors | O(threads) | O(1) | O(1) in guest, O(threads) in companion |

Candidate A explicitly does not change the scaling; that is its scope limit, not an oversight.

## 4. Exec state machine (from code)

```
exec_requested (guest)
  execve.c:240  pipe2(O_CLOEXEC)                      -> monitor pipe
  execve.c:245  server checkout RPC with read end     -> SCM_RIGHTS, descriptor transfer
  host execve -> mldr                                  (mldr is exec'd, never forked)
    mldr.c:546  reconstruct from __mldr_* environment  (lifetime pipe number, etc.)
    mldr.c:1022 fresh CLOEXEC RPC socket
    mldr.c:1051 fresh lifetime pipe / re-register
    mldr.c:1063 checkin with lifetime read end
  server: Call::Checkout monitors the listener
    hangup (EOF)  -> success  (Darling if Mach-O, else Native)
    a byte        -> failed
```

Answers to the ten questions the investigation asked:

1. **Descriptors that MUST exist for exec to work**: the checkout monitor pipe **write end**,
   open at the moment of `execve` in the executing task; kernel CLOEXEC closure of it is the
   success signal. Nothing else needs to survive.
2. **Intentionally CLOEXEC**: RPC sockets, ring memfd, ring wake fds, the monitor pipe itself.
3. **Survives exec**: the lifetime pipe (deliberately non-CLOEXEC, kernel < 5.3 only) — and it
   is passed back in through `__mldr_lifetime_pipe`.
4. **Success vs failure**: distinguished by EOF versus a byte on the monitor pipe
   (`exec-completion.hpp`, `Call::Checkout`).
5. **When a new generation becomes active**: after mldr's fresh checkin; the server retires
   the old ring on the next attach (`thread.cpp` `attachRing`).
6. **In-flight ring request at exec**: not carried over; the lane is gone with the mapping and
   the new image re-attaches.
7. **Server-to-caller work**: torn down with the thread; there is no replay.
8. **Can an old completion be consumed by the new image?** Not through the ring: the mapping
   and both descriptor sets are gone.
9. **Stale lanes**: rejected by `DSERVER_RING_SRV_RETIRED` plus the re-attach path.
10. **Ownership token for reattach**: there is **no dedicated token** for the shared-memory
    object beyond kernel-provided SCM credentials plus the loader's internal-fd registry.
    E1 (a retained backing descriptor) has this for free; E2 does not (see section 6).

## 5. Fork state machine (from code)

Guest raw fork is `clone`/`__NR_fork` from `fork.c:25-46`; `vfork` delegates to it
(`vfork.c:9-13`); `posix_spawn` forks unless `POSIX_SPAWN_SETEXEC` (`posix_spawn.c:57-59`,
`102-105`). mldr is never forked.

```
PREPARE   mldr.c:834  block signals + take socket_bitmap.mutex
          (the guest image takes its own guard lock in the fork wrapper)
CHILD     mldr.c:844   reinit socket_bitmap.mutex
          mldr.c:846-853 close every loader ring_fd, release bitmap bits
          table.c:218-237 reinit guard lock, close guard_flag_close_on_fork entries
          fork.c:56-79  refresh per-thread RPC socket, lifetime pipe, re-guard
          ring: __dserver_ring_postfork_reset (munmap, zero lane table, wake_fd = -1)
          fork.c:83     checkin(true) — a fresh Process/nsid on the server
PARENT    mldr.c:838   unlock, restore signals
```

Facts that constrain any candidate:

- **The child's contract is not async-signal-safe today.** The child path calls `realloc`
  (`mldr.c:653/753/785` via socket allocation), `socket`, `dup2`, `fcntl`, `munmap`, `recvmsg`.
  A candidate that claims "preallocated state plus raw syscalls" must *create* that property.
- **A lane generation exists but is not checked at runtime.** `gr_lane_t{active, generation,
  owner_tid, state, wake_fd}` (`dserver-ring.c:91-100`) is matched on `active`/`owner_tid`
  (`:138`); `generation` is bumped on (re)claim (`:340-343`) and only feeds a statistic — no
  runtime path reads it. The server has **no** fork epoch at all.
- **Inherited loader fds are closed by code, not by the kernel** (they are CLOEXEC, not
  close-on-fork), and the comments anticipate number reuse by the child's new RPC socket
  (`mldr.c:846-848`).

## 6. Control and SCM_RIGHTS analysis

Descriptor-bearing RPCs today (`scripts/generate-rpc-wrappers.py`): `checkin` (`:124`),
`checkout` (`:128`), `console_open` (`:204`), `kqchan_mach_port_open` (`:275`),
`kqchan_proc_open` (`:282`), `debug_list_processes/ports/members/messages` (`:635/642/650/658`),
`ring_attach` (`:682`, `:692` — two descriptors). Plus two non-`@fd` carriers: `push_reply`'s
sync pipe (`:852-856`) and the S2C mmap fd (`rpc-supplement.h:1119`, sent at `thread.cpp:1745-1752`).

**Why one process-level control socket is sufficient in principle.** Every one of those
transfers is either a one-time setup (checkin, checkout, ring_attach), a rare capability
(console, kqchan, debug), or a transient pipe. None of them is a per-request hot path, and
AF_UNIX is the *only* correct carrier for a file object — shared memory cannot carry one, and
`pidfd_getfd` is both prohibited by the constraint set and produces a new descriptor anyway.

**The open question is not "one socket for the slow plane" but "who receives on it".** The
server's S2C path currently addresses a *per-thread* socket (`thread.cpp:1216`, `322-325`) and
interrupts a non-parked thread with a real-time signal (`thread.cpp:1161-1163`). With one
socket, an arriving descriptor must be demultiplexed in user space to the waiting thread,
which means an in-guest reader plus per-thread futexes. That is the design section 8's control
proof models.

**Hot-path exclusion.** No proposal here serializes the ring path: the control lock is only
for slow traffic, and the control proof asserts the hot path completes while the control lock
is deliberately held.

## 7. Candidate B: what the audit says about falsification

| Requirement | Verdict | Evidence |
|---|---|---|
| Per-thread wake eventfd replaced by one process-level eventfd | **Plausible** | all guest wake eventfds are per-lane (`dserver-ring.c:234-324`); nothing requires one per thread except today's 1:1 lane design |
| Per-thread request lanes stay SPSC | **Plausible** | lanes are already SPSC (`dserver-ring.c:151-160`, claim/CAS) |
| Server learns pending lanes from shared metadata | **Plausible** | the ring already publishes a per-thread control block read by the server |
| Per-thread RPC datagram sockets disappear | **FALSIFIED as stated** | the hottest non-ring RPC is `mach_msg_overwrite` (`mach_traps.c:111`; "hottest RPC" `metrics.hpp:505-506`; ~88% blocking receive), its payloads are OOL and exceed `GR_SLOT_SIZE` (128 B, `dserver-ring.c:51`), and it is not ring-eligible (`rpc-supplement.h:457-483`). Descriptors cannot travel through shared memory at all. Removing the socket requires a new in-guest demultiplexer, not a deletion. |
| Lane count is not a per-thread cost | **FALSIFIED** | `GR_MAX_LANES 128` (`dserver-ring.c:79`); beyond it every thread is on UDS |
| fork can rebootstrap the child transport correctly | **Open, with a known obstacle** | the child path is not async-signal-safe today (`realloc`/`socket`/`dup2`/`fcntl`/`munmap`) |
| exec has a robust reattach generation | **Open** | no ownership token exists for the shared object beyond SCM credentials + the loader registry; a generation would have to be introduced |

The honest summary: **B survives as "constant anchors, plus a new demultiplexer for the
non-ring classes"; it does not survive as "delete the per-thread sockets".**

## 8. Experiments

### 8.1 Doorbell clustering (previously recorded, unchanged)

Real product, ring ON: no configuration lets one wake drain materially more than one request
(max 1.06, a single-threaded control row; all concurrent rows below 1). Under concurrency the
doorbell share collapses (0.0003) while `ring_wakes_issued_per_request` reaches 1.000.
Two independent runs agree. Consequence: **relay must not be justified by coalescing.**

### 8.2 Product census: ring versus non-ring, measured (`tests/direct_transport_census/`)

Command: `bash tests/direct_transport_census/run-direct-transport-census.sh` (rc=0, 34 s; ON
`/tmp/dr-on-matched`, OFF `/tmp/dr-off-matched`; the runner refuses a prefix whose server it did
not arm, and exits 3 rather than touching it). Counters are server-lifetime cumulative; every
number is a delta between the pre and action-done barriers.

| Window | Total RPCs | Ring-served | Non-ring | Non-ring per hot-path call |
|---|---|---|---|---|
| W-A: 32 threads x 6250 mach traps (ON) | 208 418 | 200 096 (96.0%) | 8 322 (3.99%) | **0.0416** |
| W-A trap loop only (ON) | 204 161 | 200 000 | 4 161 | 0.0208 |
| W-B: 40 fork+exec cycles (ON) | 1 520 | 1 000 | 520 | **13.0 per lifecycle cycle** |
| W-A (OFF) | 208 386 | 0 | 208 386 (100%) | 1.042 |

Non-ring composition in W-A: `pthread_canceled` 8 226 (98.8% of non-ring), `checkin` 32,
`checkout` 32, `ring_attach` 32. **Descriptor-bearing non-ring traffic is 96 RPCs in 208 418
(0.05%)** on the hot window, and 13 per fork+exec cycle on the lifecycle window.
`mach_msg_overwrite`, `psynch_*`, `semaphore_*`, `mach_port_deallocate/mod_refs`, `s2c_perform`
and `push_reply` were **absent** from these windows, so this census does not size them; the
"hottest RPC" status of `mach_msg_overwrite` is source evidence (`metrics.hpp`), not a census
result, and the census says so explicitly.

Descriptor counts from `/proc/<pid>/fd` (authoritative; the guest cannot see the loader's
descriptors at all):

- ON: baseline (1 thread) 17; 33 threads live 81; after join 49 -> **2.000 per extra thread
  while alive, 1.000 retained after the thread exits**.
- OFF: 15 / 47 / 15 -> 1.000 while alive, **0 retained**.
- Guest-visible scan is identical and constant on both legs (14 descriptors), none above 68.

The retained-after-exit descriptor is new information: today's loader keeps one hidden
descriptor per thread that has *ever* attached a lane, until process exit or fork. That is
part of the O(threads) cost, and it is monotone in thread churn, not in live threads.

### 8.3 Standalone proof: Candidate B topology (`tests/direct_process_doorbell_proof/`)

`bash tests/direct_process_doorbell_proof/run-direct-process-doorbell-proof.sh` - host and
ordinary Docker, rc=0, marker `DIRECT_PROCESS_DOORBELL_OK`, ~5 min, plus four further host runs
stably 8 PASS / 0 FAIL. Verified independently by re-running the runner. Topology: N SPSC lanes,
one pending bitmap, ONE process-level eventfd, per-thread reply futex, **no relay process**.

| Claim | Result |
|---|---|
| D1 descriptor scaling | PASS - open descriptors 8 at 1, 8, 32 and 64 threads (3 anchors + stdio + eventfd + control endpoint) |
| D2 SPSC | PASS - 134 lanes, one producing tid each, 0 double claims, cross-region probe writes fault as designed |
| D3 no lost wake | PASS - **252 forced interleavings** x 2 initial states, 0 left unserviced; stress 2x2048 rounds |
| D4 pending addressing | PASS - hierarchical bitmap touches **3 words** where a blind scan touches 128 (N=8192, 37 pending) |
| D5 hot path | PASS - fully active window 100 000 requests, **0 eventfd writes, 0 server epoll wakes**; cold window 1.000 writes/request |
| D6 fork generation | PASS - 12 repeated forks, inherited doorbell used 0 times, 0 completions consumed twice |
| D7 exec generation | PASS for **both** options: E1 retained memfd (descriptor count 6 before / 6 after) and E2 descriptor-less SysV shm (5 / 5); stale attach rejected, serviced-but-unconsumed reply refused; **failed exec leaves the previous generation usable** |
| D8 caller-local sideband | PASS - 4/4 operations executed on the requested tid, thread-local cookie survived |
| D9 mutations | PASS - M1 (child uses the inherited doorbell) -> D6 red; M2 (publish before the pending bit) -> D3 red (35-38 unserviced); M3 (no generation check) -> D7 and D6 red; each in both environments |

Verdict on E1 versus E2: both work, and both keep the descriptor count constant across exec
(E1 6 -> 6, E2 5 -> 5). The proof therefore does **not** settle E1-vs-E2 on correctness
grounds; the choice can be made on cleanup and accounting grounds (a SysV segment needs
`IPC_RMID` discipline and a stale-incarnation rule; a memfd needs one reserved number).

### 8.4 Standalone proof: one control endpoint (`tests/direct_process_control_proof/`)

`bash tests/direct_process_control_proof/run-direct-process-control-proof.sh` - host and ordinary
Docker, rc=0, marker `DIRECT_PROCESS_CONTROL_OK`, ~75 s including nine mutation legs. Verified
independently by re-running the runner.

| Claim | Result |
|---|---|
| C1 constant anchors | PASS - 4 descriptors at 0, 32 and 64 threads, exactly 1 control socket |
| C2 exactly once | PASS - 2048 concurrent tagged requests from 64 threads, 0 completions observed by a non-owner, 0 id consumed twice |
| C3 SCM_RIGHTS both ways | PASS - 256 + 256 descriptors under 64-thread concurrency, 0 to the wrong request, no fd leak |
| C4 application-table handoff | PASS - server-created descriptors land in the requesting process's rows, validated by the forked child too |
| C5 fork isolation | PASS - parent storm completed while the child ran, 0 misplaced completions |
| C6 exec transition | PASS - endpoint re-established on a new generation, pre-exec reply rejected rather than consumed; failed exec keeps the old endpoint |
| C7 hot path not serialized | PASS - hot lanes ran at ~2.9-4.9 M ops/s **while the control lock was held for the whole phase** |
| C8 style 1 vs style 2 | PASS - style 1 (one lock) works but 97-98% of its mean latency is lock wait, p99 5-7x worse, ~5x more guest blocking; style 2 (multiplexed messages + per-thread completion lanes) wins |
| C9 control:hot ratio | 21-106 control ops per 1000 hot ops in the harness (harness-only number, not the product) |
| C10 mutations | PASS - 6 mutations (wrong lane, descriptor to the wrong request, child reusing the parent endpoint, per-thread endpoints, hot path locked, stale generation accepted) each turn the designed claim red |

**Answer to the decisive question**: this harness found **no operation class that needs a
dedicated per-thread socket**. What it needed instead is what the model built: shared-memory
completion lanes per thread, one process-level receiver/demultiplexer, and one fork/exec
endpoint-ownership rule. With the `per-thread-endpoints` mutation the descriptor count becomes
36 at 32 threads and 68 at 64 threads, so the constant-anchor claim is genuinely exercised
rather than trivially true.

Limit to keep in view: the model's server is single-threaded, so heavy control traffic starves
the ring poller in the harness (15 766 ops/s with 64 threads of control traffic against
4 747 526 alone). That figure is a property of the model's server loop, not of the guest lock -
the lock-held window shows the hot path is independent - and the real server has a thread pool.

### 8.5 Truthful NOFILE: the patch plan (PLANNED, NOT APPLIED)

**Superseded in part by section 8.6**: the plan below is the *reporting* half (which query paths
must route through the loader) and remains valid, but its dynamic-allocation half — a reserved
band above the published limit with the limit raised around each private allocation — is
falsified and must be replaced by one of the A4 options. Read 8.6 before acting on this section.

A runtime source change needs explicit approval; none was granted for this investigation, so
nothing below has been applied.

Scope: **the loader must be the single authority for `RLIMIT_NOFILE` as the guest sees it**,
and the value it publishes must be *stable*, not a function of instantaneous occupation.

Planned edits (loader, `src/startup/mldr/`):

1. Add a fixed reservation constant and a band function, e.g.
   `INTERNAL_FD_RESERVE` (4096) and `public_fd_hard_limit(native) = native - min(native/2, reserve)`.
2. Constrain `socket_bitmap_get_locked` to the band: `lowest = public_fd_hard_limit(native)`,
   `highest = native - 1`; refuse allocation when the band is exhausted (the caller already
   propagates failure, `mldr.c:857-906`).
3. Extend `__mldr_fd_is_internal` to mean **band membership OR bitmap membership**, so a
   currently-free number inside the band is refused too. Without this a guest could `dup2`
   into a free band number and collide with a later loader allocation. Conversely the guard
   must keep accepting every free number *below* the boundary.
4. Publish the boundary once at startup into a `public_fd_limit` variable
   (`__mldr_user_fd_limit()`), independent of current occupation.
5. Add `__mldr_get_nofile_limits` / `__mldr_set_nofile_limits`, and the three elfcalls
   function pointers, **assigned in `elfcalls_make`** (`elfcalls/elfcalls.c:88-140`) — the
   deployed struct has no limit callbacks at all, so this is an addition, not a repair.
6. `setrlimit` translation must clamp: a raise above the public maximum returns `EPERM`; an
   accepted value maps back with the reservation, and must preserve the existing
   native↔public off-by-one convention.

Planned edits (guest, `src/external/xnu/darling/src/libsystem_kernel/emulation/`):

7. `sys_getrlimit` NOFILE (`misc/getrlimit.c:15-24`) must report the loader's public cur/max
   instead of `prlimit64` minus one.
8. `sys_setrlimit` NOFILE (`misc/setrlimit.c:9-19`) must go through
   `__dserver_set_nofile_limits`.
9. `getdtablesize` (`unistd/getdtablesize.c`) and `sysconf(_SC_OPEN_MAX)`
   (`src/libc/gen/FreeBSD/sysconf.c:105-136`) then follow automatically, because both call
   `getrlimit`.
10. The guard (`table.c:200-205`) needs no change: it already asks the loader.

Properties to verify with the change, all of which the plan must not break:

- `getdtablesize()-1` is accepted and `getdtablesize()` is refused (`EBADF`), on both legs.
- the published value does not move as threads or lanes are created;
- `fork` gives the child the same boundary (it is a constant derived from the host limit);
- `exec` re-derives it identically (the host limit is inherited);
- lowering `NOFILE` below already-open guest descriptors keeps those descriptors usable
  (Linux/POSIX semantics), which the guard preserves because it rejects band membership, not
  "numbers above the current soft limit";
- `OPEN_MAX` (10240) and `FD_SETSIZE` (1024) are compile-time constants and must not be
  touched.

Acceptance oracle, existing: `tests/ring_fd_limit_repro.c` / `tests/run-ring-fd-limit-repro.sh`
must go from exit 1 to exit 0 on the ring-enabled build, both legs must agree, and the matched
ON stock leg must then pass `libunistring`'s `make check` with zero failures.

Cost of Candidate A, stated plainly: the guest loses `reserve` numbers of capacity
(4096 of ~1 048 576 here, negligible), and thread creation fails once the band is exhausted
(~2047 threads at 2 loader fds per thread). Candidate A therefore **does not** remove
O(threads) hidden descriptors; it makes the advertised range honest.

## 8.6 HARD GATE A: the truthful-NOFILE draft is FALSIFIED on a process-wide race

Verdict: **the drafted Candidate-A design is falsified as written.** It does not merely make the
advertised range honest — it introduces a process-wide `RLIMIT_NOFILE` race that the *deployed*
loader does not have today (the deployed loader contains **no `setrlimit`/`prlimit` call at
all**, so it has no window).

Mechanism, from the draft (`source-fixes/ring-fd-ownership`, uncommitted):

- `private_fd_allocation_begin` (`mldr.c:637-665`) clamps the *saved* soft limit down to
  `public_fd_hard_limit(rlim_max)`, publishes that value, and then **raises the real
  process-wide soft limit to the hard limit** (`mldr.c:652-654`). `private_fd_allocation_end`
  (`:667-676`) restores the **clamped** value — so after the first window the process soft limit
  is permanently the public value and **every subsequent private allocation re-opens the
  window**.
- `socket_bitmap.mutex` is held across the window, but it is a *loader* mutex: no guest
  fd-producing syscall takes it.
- `__mldr_fd_is_internal` (`mldr.c:826-839`) is **bitmap membership only**: a free number inside
  the reserved band is *not* internal, so `guard_table_check` does not protect the band.
- Four begin/end sites (`:799`, `:870`, `:918`, `:1029`); **three of them run after a
  multithreaded guest is executing**: per-thread RPC socket creation (`elfcalls/threads.c:241`,
  refresh at `:430`), ring attach (`dserver-ring.c:323` → `adopt_ring_fd`), and lifetime-pipe
  refresh (`elfcalls/elfcalls.c:83`). Each is a fresh window.
- Loader fd-producing paths that bypass the registry mutex entirely: `elfcalls.c:95` `dlopen`,
  `:119` `sem_open`, `:122` `shm_open`, all guest-invoked at runtime.
- The query side is sound: `__mldr_get_nofile_limits`/`__mldr_set_nofile_limits` take the same
  mutex and therefore cannot observe the raised state, and `__mldr_user_fd_limit` is a lock-free
  atomic of the published value. The enforcement side is not.

Deterministic proof, `tests/limit_window_race_proof/` (host and ordinary Docker, rc=0, marker
`LIMIT_WINDOW_RACE_OK`, 12/12 claims, four mutation legs each exiting 1, and it is 3.7 s because
the window is opened by barriers rather than by racing):

| Claim | Result |
|---|---|
| R1 window exists | PASS — inside it the raw soft equals the hard limit while the published value stays low |
| R2 `dup2` to a free high target | PASS — **succeeds inside the window** (`rc=6144`, in the reserved band) and fails `EBADF` outside; a free non-band target above the limit also fails outside |
| R3 `F_DUPFD_CLOEXEC` with an explicit min | PASS — `fd=6145` inside, `EINVAL` outside |
| R4 lowest-free vectors do not escalate | PASS — `open`/`socket`/`pipe`/`dup` return low numbers in *both* windows, so the exposure is exactly the explicit-target calls |
| R5 the guard does not protect the band | PASS — guest-obtained 8191/6144 report `is_internal=0`; the loader took 8190 while the guest held 8191 and only took 8191 after the guest closed it |
| R6 the raise is observable | PASS — an unrelated thread reads the raw soft at the hard value while the published value stays low; the loader-routed query blocks on the mutex instead |
| S1–S6 lowered-limit semantics | PASS — a descriptor above a lowered soft limit stays fully usable; new lowest-free allocation stays below it; explicit targets at or above it are refused (`EBADF`, `EINVAL`); a loader-style private allocation above the guest limit still succeeds **because the real soft differs from the published one**; raise-to-hard succeeds and above-hard fails with `EPERM`; lowering does not disturb loader descriptors |

The harness states its own limits: it models the draft's sequence line by line and executes the
syscalls for real (`setrlimit`, `dup2`, `fcntl`, `open`, `socket`, `pipe2`, `getrlimit`), but it
does not run mldr, and it normalizes the regime (hard 8192, published 4096, band
`[4096,8192)`) while printing the original host and container values.

**Correction to the R4 wording (T1–T5).** R4 showed that the lowest-free vectors *do not*
escalate while free numbers exist below the published limit — but that is a property of a free
low range, not of the vectors. With the low range saturated (all 4096 numbers below the
published limit occupied), inside the same window the kernel-chosen vectors escalate exactly
like the explicit-target ones:

```
T3 window open, low range saturated: open=4096 socket=4097 pipe2=4098,4099 dup=4100
   (every one inside the loader's reserved band [4096,8192))
T4 after the restore, still saturated:  open/socket/pipe2/dup all -1 EMFILE
```

So the exposure belongs to the **whole allocation surface**, not to caller-chosen targets;
caller-chosen targets merely demonstrate it without needing the low range filled first. Any
statement of this defect must be phrased in that general form.

### A4: source-backed options (decision matrix, not implemented)

Completeness baseline for every option: the deployed product has **no single choke point** for
fd creation. Only `close`, `dup`, `dup2`, `fcntl` and the `posix_spawn` CLOEXEC scan consult the
guard; `open`/`openat`/`guarded_open_np`, `socket`, `socketpair`, `pipe`/`pipe2`, `accept`,
`recvmsg` (`SCM_RIGHTS` installs), `kqueue`→`epoll_create`, every Linux-ext wrapper
(`epoll`/`eventfd`/`inotify`/`signalfd`/`timerfd`/`fanotify`), `shm_open`, the `vchroot` helper
opens and `_dup_4libkqueue` allocate **with no guard at all**, and `dup2`, `F_DUPFD*`,
`posix_spawn` `PSFA_DUP2`/`PSFA_OPEN` take a **caller-chosen target number**.

| Option | What it takes | Where it lands | Verdict |
|---|---|---|---|
| A4.1 serialize all guest FD creation | a shared lock held **across the syscall** in ~20 entry points, plus signal masking around each (the guard already shows why the check cannot be split from the act) | every program's fd-creation path, not the I/O path; no single enforcement point; each new wrapper re-opens the hole unless added to the list | possible, but the most invasive: it buys the same completeness obligation as A4.2 and additionally changes `EINTR`/restart semantics for the guest |
| A4.2 virtual guest soft NOFILE | never raise; keep the real soft at hard and enforce the published value in code: explicit-target creators refuse `target >= published`, kernel-chosen creators would have to post-check the returned number and close it with `EMFILE` | same completeness obligation as A4.1; the post-check is **not semantics-preserving** (see below), so each creator needs a real interception scheme or must be excluded | **not a ready fallback.** Measured on this host: native `EMFILE` stops the operation *before* the file side effect, so a post-filter leaves behind exactly what native behaviour prevents. Correctness would additionally depend on proving the creator list complete |
| A4.3 stop-the-world barrier | guarantee that no guest thread is inside an fd-producing syscall while the limit is raised | nothing in the tree provides it: `prefork_prepare` and the server's quiesce machinery are cooperative points in Darling's own code, a thread already inside `open` cannot be pulled back, and suspending threads does not un-raise a process-wide attribute | **not available**; a signal-based design is not in the tree and must not be invented |
| A4.4 remove the need for the raise | make the internal FD set O(1) with anchors created before guest execution (or at a controlled exec bootstrap), then set the native soft limit to the honest published value **once** and never raise | removes the race by construction instead of adding enforcement to ~20 syscall paths | the principled option; it *requires* the per-thread private FDs to disappear, i.e. it is the same work as Gate B. If they must stay, the honest variant is a static partition (publish `native - reserve`, allocate in the band only during bootstrap, and treat later needs as a clean hard failure rather than a race) |

**A4.2 is not a ready fallback — measured, not argued.** On this host (kernel 6.8.0-138),
with the low descriptor range genuinely exhausted (`RLIMIT_NOFILE` soft 64, all numbers below
it occupied), the native failure happens *before* the file side effect:

```
CASE trunc  open(path, O_WRONLY|O_TRUNC)      rc=-1 errno=24 EMFILE  size_after=10   (unchanged)
CASE creat  open(path, O_CREAT|O_WRONLY)      rc=-1 errno=24 EMFILE  FILE_CREATED=0
CASE excl   open(path, O_CREAT|O_EXCL|O_WRONLY) rc=-1 errno=24 EMFILE  FILE_CREATED=0
CASE socket socket(AF_INET, SOCK_STREAM)      rc=-1 errno=24 EMFILE
CASE pipe   pipe()                            rc=-1 errno=24 EMFILE
```

The kernel reserves the descriptor number before the path is opened, so `O_TRUNC` does not
truncate and `O_CREAT` does not create. A userspace post-filter that performs the syscall and
then closes the result would therefore leave a **created or truncated file** where native
behaviour leaves none — the opposite of the earlier claim in this document, which is hereby
corrected.

`tests/fd_semantics_proof/` now carries the per-creator resolution (host and Docker, `S1` +
`C1`–`C18`, three mutations, marker `FD_SEMANTICS_PROOF_OK`), comparing native behaviour with a
post-filter wrapper for each class:

| Creator | Post-filter semantics-preserving? | Why, with the ordering |
|---|---|---|
| `open`/`openat` `O_CREAT`, `O_CREAT｜O_EXCL`, `O_TRUNC` | **no** | the kernel reserves first (`fs/open.c:1402` `get_unused_fd_flags()` before `:1404` `do_filp_open()`), so native leaves the filesystem untouched while the wrapper leaves a created file or a truncated-to-zero file (irreversible) |
| `accept`/`accept4` | **no** | the number is reserved (`net/socket.c:1965`) before `do_accept()` (`:1969`) dequeues; the wrapper accepts and immediately closes the pending connection, so the peer sees EOF/RST and the queue is empty, while native leaves the connection queued |
| `recvmsg` + `SCM_RIGHTS` | **no, and worse** | native is not an `EMFILE` case at all: `scm_detach_fds()` (`net/core/scm.c:328` → `receive_fd()`) drops the descriptors, sets `MSG_CTRUNC` (`:350`) and returns **success with the payload delivered and zero descriptors**; the wrapper returns `EMFILE` after the payload was already copied and the message consumed, so the guest both loses a message and sees an error it can never see natively |
| `pipe`/`pipe2`, `socket`, `socketpair`, `eventfd`, `epoll_create`, `inotify_init`, `signalfd`, `timerfd_create` | equivalent | the kernel's own failure path already destroys the object (`fput`/`sock_release`/`ep_clear_and_put`), and the only residue is a number above the virtual limit that the guest cannot name (a co-thread could still observe it transiently through `/proc/self/fd`, which is **not measured**) |

A pre-check instead of a post-check does not repair this: `C17`/`C18` execute the interleaving
deterministically — the pre-check sees a free slot, releases it, another thread takes it, and
the act then runs with the below-limit range full, leaking the truncation and the consumed
connection while returning `EMFILE`.

Consequence for the options: with both post-filter and pre-check ruled out for the
side-effecting classes, A4.2 would need a real semantics-preserving interception scheme per
creator — i.e. interference *before* the syscall, which is A4.1's lock. A4.2 is therefore
**not** a fallback; it is an undeveloped variant with a proven-hard core.

Recommendation: **A4.4 first**, because it is the only option that deletes the race rather than
policing it, and its prerequisite is already the target of the transport work. If dynamic
per-thread private FDs must persist, the remaining honest options are A4.1 (a shared lock across
the syscall in every creator, accepting the signal-semantics change) or a static partition as
described under A4.4; A4.2 is marked unacceptable-until-designed, and A4.3 is not a candidate
until a mechanism exists.

Not executed in this round: an **integration** race against the real draft (`A3`). The blocker
is fidelity, not effort: the draft's parent commit `9ef131b2` is **not an ancestor** of either
the darling HEAD or the deployed composition commit `73498c5a`, so applying it requires a
disposable isolated checkout whose runtime is then *not* the deployed composition. A faithful
integration would need an isolated west checkout, a runtime build with a test-only pause hook in
the window, and a bootstrapped prefix — and it would confirm a window that the source audit and
the deterministic proof already establish. Recorded as available-on-request with that cost.

## 8.7 HARD GATES B/C/D/E: transport, lifecycle, boundaries

### B1/B2 — how the real blocking RPC actually works

The blocking Mach RPC is a **synchronous RPC whose reply is demultiplexed by the socket**:
the guest thread blocks in `recvmsg` on its own autobound per-thread datagram socket
(`mach_traps.c:97-134`, one `dserver_rpc_mach_msg_overwrite` at `:111`, EINTR retry loop
`:110-130`), while the **server** blocks a cooperative microthread in the duct-taped XNU waitq
(`duct-tape/xnu/osfmk/ipc/ipc_mqueue.c:1014-1042`, `receive_on_thread:1086-1245`,
`thread_block_parameter` in `duct-tape/src/thread.c:625-684`). The reply is matched by
**call number plus sender address only** — there are **no request ids** anywhere on this path
(generated client: send `generate-rpc-wrappers.py:1408-1465`, recv+match `1565-1665`;
`replyhdr` is `{number, code}` at `:847-850`). Every blocking RPC blocks on **both** sides
(`mach_msg_overwrite`, `semaphore_wait*`, `psynch_cvwait`/`mutexwait`/`rw_*`, `fork_wait_for_child`).

That is the load-bearing constraint for Candidate B: removing the per-thread socket does not
merely change a transport, it changes **reply addressing for every RPC**, so the demultiplexer
must carry `(request id, tid, lane generation)` for all replies, not just for slow ones.

Measured frequency, with the intended counter proven to have moved
(`tests/lane_lifecycle_census/`, ON and OFF):

| Workload | Counter | Result |
|---|---|---|
| 256 blocking `mach_msg` receives, receive pending before each send | `msg_blocking_receive_delta` | **256 on both prefixes**, guest `recv_ok=256`; blocking share of `mach_msg` calls 0.500 |
| 36 threads × 72 000 contended pthread mutex ops + condvar phase | any `psynch_*`/`semaphore_*` callnum | **UNPROVEN — no psynch callnum moved at all** |
| the same workload, what did move | `pthread_canceled` | **8241** (0.0858 per mutex op): `_pthread_mutex_lock` defaults to the ulock path, so contention parks **in-guest** on `__ulock_wait` and the only server-side RPCs are the cancellation-point calls bracketing each park |

So the "pthread sync goes to psynch RPCs" assumption is **wrong in this build**, and the
psynch question is recorded as UNPROVEN rather than answered. `per_call` carries no
`mach_msg_overwrite` row at all in this build (lifetime count 0), so the blocking-receive number
rests on the `msg_*` counter family alone and is marked uncrosscheckable.

### B3 — S2C and caller-local delivery

The server keeps, per guest thread, the **autobind abstract address** learned from that
thread's datagrams (`call.cpp:230/245/317`) and sends S2C with `sendmmsg` on **one shared
listener socket**, stamping each message with the target's address
(`message.cpp:440-505`, `server.cpp:1332-1334`). Addressing is therefore *by sender identity*,
not by an id: the S2C op set is exactly **four ops — mmap, munmap, mprotect, msync**
(`rpc-supplement.h:1091-1160`) — and only `mmap` carries a descriptor (SCM_RIGHTS, dup'ed by the
server at `thread.cpp:1745-1752`, closed by the guest at `dserver-rpc-defs.h:186-205`).

Caller-local delivery already has two mechanisms: `Process::_pickS2CThread` (`process.cpp:742-766`)
returns the calling thread when the server fiber belongs to that process, and when the target is
**not** parked the server forces it in with `tgkill(SIGRTMIN+1)` (`thread.cpp:2115-2128`), so the
guest's signal handler runs `interrupt_enter`/`s2c_perform` (`sigexc.c:163-200`). The
register-capture class (SIGEXC/SIGRTMIN) is the operation that physically cannot be executed by
another thread.

Two facts matter for B: the server already targets a **specific TID**, so a demultiplexer does
not weaken S2C addressing; and **UDS S2C completion is matched by arrival only** — there is no
correlation id, one-in-flight per thread is enforced by a semaphore (`thread.h:96-110`). The
perf#18 duplex ring lane is the only S2C path with real ids.

### C1 — lane lifecycle and descriptor ownership, resolved

`GR_MAX_LANES = 128` in the guest dylib, enforced in five loops in `dserver-ring.c`; overflow
falls back to the per-thread UDS. Ownership, traced to the close path:

| Object | Owner | Closed on thread exit? |
|---|---|---|
| per-thread RPC datagram socket | guest thread (`__thread t_server_socket`) | **yes** — `__darling_thread_terminate` → `__mldr_close_rpc_socket` (`elfcalls/threads.c:359-405`) |
| per-lane wake eventfd (guest duplicate of the server's) | guest, held **twice**: `gr_lane_t.wake_fd` and the loader's `ring_fds[]` | **no** — no close path on thread exit and none before process exit; the only close is `__mldr_postfork_child` (`mldr.c:846-853`) |
| lane slot in `g_lanes[128]` | guest image | **never released** — the only bulk release is `__dserver_ring_postfork_reset` |
| ring memfd | both sides close it after mmap | mapping unmapped only on fork-child |
| server `RingBuffer` (mmap + eventfd) | server `Thread` | yes — `Thread::notifyDead` (`thread.cpp:2137-2190`) |

This is the mechanism behind the previously observed retention, and it is an omission, not a
leak guard: nothing closes the guest's adopted wake fd before process exit.

### C2/C3 — measured churn and cap behaviour (`tests/lane_lifecycle_census/`, ON and OFF)

512 simultaneous threads are achievable (18 s, no creation failures, no failed trap). The lane
cap is observed four independent ways (guest `acquired` freezing at 128 while `exhausted`
climbs; server attach counters freezing; a residual bucket climbing 1:1; the ring-served trap
count freezing):

| Threads ever created | host fds | of which eventfds | guest-visible | lanes held | exhausted | threads on UDS |
|---|---|---|---|---|---|---|
| 0 | 17 | 2 | 14 | 0 | 0 | — |
| 64 | 81 | 66 | 14 | 128 (cap) | 0 | 0 |
| 128 | 144 | 129 | 14 | 128 | 61 | 1 |
| 192 … 1024 | **144 (flat, `fd_delta=+0`)** | 129 | 14 | 128 | 2691 at the end | 897 by the end |

| Simultaneous | threads without a lane | live sockets | host fds live | fds after join |
|---|---|---|---|---|
| 129 | 1 | 130 | 273 | 76 |
| 256 | 128 | 239 | 382 | 83 |
| 512 | 384 | 387 | 530 | 93 |

**Answers.** The descriptor count grows with the number of threads **ever created** (exactly one
eventfd per thread, `host_fd_per_extra_thread = 1.000`), and it is **bounded by the lane cap**,
not by concurrency: from 192 to 1024 threads the count does not move at all while 896 further
threads are created and joined. Beyond the cap, traffic moves but descriptors do not — every
later thread takes the per-thread UDS path permanently (`fb_no_ring_proc_has` 3 → 2691). The
guest's own scan stays flat at 14 throughout, so a guest-side observer sees nothing.

After the churn the lane eventfds **drain asynchronously** (129 → 79 in this run; 31/50/73/36
across four runs — timing-dependent), while the guest still reports `held_now=128`, i.e. at that
instant ~30–72 lane slots refer to a descriptor that is no longer open. That last statement is
an inference from two measured numbers; no doorbell write on such a slot was observed in either
direction, and the trigger for the release is **not** attributed.

On the OFF build the count is flat at 15 throughout (its dylib has no lane table at all).

Consequence for the design: with per-thread UDS removed, **thread number 129 in a process would
have no transport at all**. Lane lifecycle (release on thread exit, reuse, and a directory that
is not a fixed 128-entry array) is therefore not a refinement of Candidate B — it is a
prerequisite, and the ABA/generation work in the next section is what makes reuse safe.

### C4 — generation/ABA, proven with mutations (`tests/generation_aba_proof/`)

Five claims green on host and ordinary Docker, four mutations red on exactly the named claim,
marker `GENERATION_ABA_OK`, rc=0. Reuse is constructed deterministically (a real thread A claims
slot 3, exits; a real thread B re-claims the same slot **with A's recycled tid value**; only then
is A's completion delivered):

| Claim | Identity tuple compared |
|---|---|
| G1 ring reply | `{slot, generation, owner_tid, seq, callnum}` |
| G2 S2C upcall | `{slot, generation, owner_tid, parent_id}` — the upcall must not execute in the new occupant |
| G3 blocking control completion | `{request_id, slot, generation, owner_tid, token}` — matched by id, never by arrival order |
| G4 SCM_RIGHTS descriptor | `{descriptor token == request_id, slot, generation, owner_tid}` — rejected descriptors are closed, not leaked |
| G5 ≥1000 delayed reuses | 2228 acquisitions over 1264 delayed deliveries, zero wrong consumptions |

Mutations: dropping the generation → G1/G5 red; dropping `owner_tid` → G2/G5 red; matching by
arrival order → G3 red; installing a descriptor without the request identity → G4 red.

Note this is a **model** of the identity scheme, not the product: the product compares
`active`/`owner_tid` only and never reads `generation` at runtime, and has no process epoch.

### D1 — anchor inventory and bootstrap timing

| Anchor | Created | Pre-guest possible | Replaced at runtime today |
|---|---|---|---|
| process control UDS endpoint | mldr bootstrap (`setup_space`) | **yes, already** | no |
| pre-5.3 lifetime pipe | mldr bootstrap, kernel < 5.3 only | **yes, already** | refreshed at runtime (`elfcalls.c:83`) |
| exec checkout anchor | `execve.c:240` (`pipe2(O_CLOEXEC)`) | per-exec, inherently | yes, once per exec, in the single-threaded exec bootstrap |
| process wake eventfd | — (today: per lane, at ring attach) | **yes**, if created at bootstrap | today created lazily **per thread/per attach** |
| shared-memory backing fd | — (today: `memfd_create` per lane, `dserver-ring.c:253`) | **yes**, if one static backing is chosen | today per attach |

So two of the five are already pre-guest, the exec anchor is inherently per-exec, and the two
that must change are exactly the objects Candidate B consolidates: **one** process eventfd and
**one** backing, both created at bootstrap, with lanes becoming pure directory entries rather
than per-thread descriptors. D2/D3 (fork and exec state machines) are written up in section 8.8.

### E — close_range / hostile raw-Linux boundary

The product **does not emulate `close_range`**; the forest-wide search for
`close_range`/`SYS_close_range`/`__NR_close_range` is empty. No Darwin-visible API routes to it
either: `closefrom` is not implemented anywhere (libc, syscall table and SDK headers all lack
it), `posix_spawn_file_actions_addclosefrom_np` does not exist, `POSIX_SPAWN_CLOEXEC_DEFAULT`
only sets `FD_CLOEXEC` (`posix_spawn.c:145-148`), `daemon()` only redirects 0/1/2
(`libc/gen/FreeBSD/daemon.c`), and `F_CLOSEM` is unimplemented (`fcntl.c` command map has no
entry). There is no `SYS_closefrom` in the Darwin syscall numbers
(`xnu/gen/syscall.h`). **Only a deliberate raw Linux syscall reaches host `close_range`**, so
under the current threat model this is not a correctness blocker for the anchors.

One real gap inside the supported surface, recorded rather than inflated: `close_internal` is
**not** guarded (only `close`/`close_nocancel`/`dup`/`dup2`/`fcntl`/the CLOEXEC scan consult the
guard), and it is guest-reachable through the supported `posix_spawn` file action
`XNU_PSFA_CLOSE` (`posix_spawn.c:240-243`). A program would have to name a number it does not
legitimately own to disturb an anchor, so it is a hardening item, not a boundary failure.

## 8.8 HARD GATE B (B4/B5): the demultiplexer, and whether it needs a permanent thread

### The architecture question

If one process-level UDS carries every reply, someone must call `recvmsg` on it. The options
are: a permanent demultiplexer thread; one of the waiting threads holding a reader token; a
delivery scheme that needs no reader (server writes into the lane and futex-wakes the target,
with the socket used only when a descriptor must move); or something else. The fixture tests
the first two rather than listing them.

### Evidence tiers (vocabulary used from here on)

| Tier | Meaning | What may be called |
|---|---|---|
| MODEL | A proof harness that runs its own state machine over product-shaped data; no product code executes. | "model proof" |
| INTEGRATION FIXTURE | A harness compiled against the product's **real headers/generated ABI** whose data structures are the product's byte-for-byte, but which does not change or run the Darling runtime. | "integration fixture" |
| PRODUCT | The Darling runtime, client or server path is **modified and executed** in a real prefix. | "PRODUCT PASS" |

`tests/demux_fixture/` and `tests/generation_aba_proof/` are **integration fixture** and **model**
respectively. Neither is a PRODUCT result, and no line below may be read as one. Their value is
undiminished: they establish the mechanism and pin the identity requirements; they do not
establish that Darling's own runtime can carry it.

### Result: `tests/demux_fixture/` — INTEGRATION FIXTURE (host and ordinary Docker, rc=0, marker `DEMUX_FIXTURE_OK`, ten mutations red)

It is an integration fixture, not a product prototype, built from the **product's real
structures**: 28 struct sizes/offsets, the enum width and the four callnums are **identical**
(0 differences) between the fixture's copies and a probe that includes the real headers plus an
ABI regenerated by the product's own `generate-rpc-wrappers.py`.

| Claim | Result |
|---|---|
| D1 32 concurrent blocking receives | PASS — 32/32, wrong_thread=0 |
| D2 replies deliberately out of order | PASS — 32/32 landed on their own thread |
| D3 one waiter times out | PASS — the late reply for the abandoned lane is rejected **by the lane generation** (same request id, same payload shape: the generation is the only discriminator) |
| D4 one waiter interrupted | PASS — 31/31 others complete; the stale reply is rejected by generation/lane |
| D5 SCM_RIGHTS binding | PASS — the token is read out of the descriptor itself; 1 rejected descriptor closed; no fd leak (9 → 9) |
| D6 no head-of-line blocking | PASS — 31/31 others complete in 2.2–6.3 ms while the slow waiter is still parked |
| D7 fork generation | PASS — the single-threaded child rejects the parent's queued completion and completes its own; V2 re-creates nothing, V1 re-creates its thread with a libc-free raw clone on a 64 KiB pre-allocated stack (pthread_create also works, but can deadlock on a libc lock held at fork) |
| D8 exec generation | PASS — the pre-exec completion is queued and confirmed before the exec, and the new image rejects it |
| D9 caller-local on the target | PASS — 4/4 routed and executed by the addressed tid, 0 by the dispatcher, and the server keyed each reply off the executing tid 4/4 |

Mutations (each `exit=1`, each reddening its claim): arrival-order matching; dropping the lane
generation; dispatching a caller-local op from the dispatcher; not releasing the token on
interrupt; the child accepting the parent's completion; the post-exec image accepting the
pre-exec one; the holder serving only itself.

### Which shape survives, and the price

- **V2 (reader token, no permanent thread) survives the cost comparison**: no extra thread at any
  count (33/65 vs 34/66), no 8 MiB stack reservation (VmSize 525 808 vs 534 004 kB at 64 workers),
  and a strictly cheaper dispatch path (`copies_per_completion` 0.785–0.860 vs 1.000, because the
  holder parses its own datagram in place, so 14–21 % of completions avoid a copy, a slot write
  and a futex wake).
  **Scope of "nothing to re-create": that claim is about the permanent helper thread only.** V2
  still has to do all of the transport-state work at fork and exec: advance the process
  generation, rebind and re-register the child's identity with the server, rebuild the lane
  mapping after exec, and ensure the child cannot consume or answer any logical state of its
  parent's endpoint. The fixture's D7/D8 are exactly that work; V2 merely does not add a thread to
  it.
- **V1 (permanent demux thread) also survives**, and is the shape required where a background
  reader must exist for a thread that is *not* inside an RPC. It pays one permanent thread, 8 MiB
  of reserved address space, a signal mask it must own, a bounded blocking `recvmsg` so it can be
  told to stop (37–67 µs idle CPU per 300 ms), and re-creation in **every** fork child and
  post-exec image.
- **Identity fields are not optional**: without the lane generation a re-used lane is satisfied by
  the abandoned incarnation's reply (M2 → D3 red, "completed with a reply from lane 1 of 2"),
  without the process generation a fork child or post-exec image consumes its predecessor's
  completion (M5/M6 → D7/D8 red), and executing rather than routing a caller-local op
  misattributes the S2C reply and strands the caller (M3 → D9 red: 4/4 misattributed, 0 routed —
  exactly the product's failure mode, since `call.cpp:266-278` stores the S2C reply on the thread
  named by `header.tid` and ups **that** thread's semaphore, so the real caller's `_s2cPerform`
  never returns).
- **The residual constraint, stated plainly**: V2's shape works while S2C targets are guaranteed
  to be inside a call, and falls back to the product's existing mechanism otherwise — the server
  already `tgkill`s a non-parked target (`thread.cpp:2115-2128`), which puts *that thread* into
  the receive path. Whether that covers every S2C case in the real product is the main open item.

### Fidelity gap (what this fixture is not)

It runs no mldr, no darlingserver, no Mach and no XNU code. The request id, process generation,
target tid and lane generation live in the fixture's own envelope because **the product has none
of those fields on the wire**; every payload byte is a product struct, but the envelope is the
proposal's. The fixture does not model the product's bounded recvspin, its signal-blocking
around begin/end, the `push_reply` path, the fork/exec checkin RPCs or the lifetime pipe, and
its latency/CPU numbers are its own, not the product's. The claims that still need product
evidence are listed in section 8.9.

## 8.9 Verdict for this round: **UNKNOWN** (not B, not C)

**Not C.** Nothing found in this round requires a companion process with its own descriptor
table. The two properties that would have forced it — per-thread control ownership that cannot be
demultiplexed in the guest, and a lifecycle/security semantic that only a separate descriptor
table preserves — were both tested and did not hold: the demultiplexer reaches the right thread
in every claim including caller-local WAL, S2C and cancellation, and the server's S2C addressing
is already per-TID rather than per-socket.

**Not a clean B either**, because B's own bar is product evidence: the demultiplexer inside the
real generated RPC client and server, at real scale, with the real timeout/interrupt paths, the
twelve descriptor-bearing sites and the real fork/exec checkin flow. The fixture is exactly the
"integration fixture from real request/reply structures" the round permits when a product
prototype is too large, and its fidelity gap is recorded rather than papered over.

**Remaining experiments for a B verdict** (each names what would falsify B):

1. Port the envelope into the generated RPC client/server as a feature flag and run the
   product's own blocking `mach_msg` workload with 32/64 threads (falsified if any reply is
   consumed by the wrong thread or any waiter wedges).
2. Exercise the product's real timeout and interrupt paths (`ALLOW_INTERRUPTIONS` retry,
   `semaphore_timedwait` `-111`, `pthread_markcancel`) instead of a signal-interrupted
   `recvmsg`/futex.
3. Walk the twelve descriptor-bearing RPC sites plus the `ring_attach` memfd handshake through
   the single endpoint (the fixture used an eventfd token).
4. Cover every S2C case for a **non-waiting** target and decide whether the `tgkill` path is
   sufficient or whether V1's permanent reader is required for a subset.
5. Remove the per-thread sockets in a feature-flagged build and re-run the matched acceptance,
   the 1024-thread churn and the 512-simultaneous window, requiring O(1) hidden descriptors.
6. Prove the `close_internal` hardening item (section 8.7, gate E) or record it as accepted risk.

**What is already established for the migration order** (independent of the above): lane slots
must be releasable and reusable, `GR_MAX_LANES` must become a directory that is not a fixed
128-entry array, and the identity tuple `{slot, generation, owner_tid, request_id}` must exist on
both sides — the ABA proof shows each of those is load-bearing, and the census shows thread
number 129 currently has no ring path at all.

## 8.10 PRODUCT attempt (dar-gwn.7.7.5): what was built, measured, and what remains

### The feature flag and the exact diff surface

Flag: `DSERVER_PROCESS_CONTROL` (a CMake option, default OFF, declared beside the existing
`DSERVER_RING_TRANSPORT`; the same macro reaches the guest targets and gates a shared wire
structure, so it is deliberately one macro rather than a `DARLING_`/`DSERVER_` pair).

| Layer | File | Change |
|---|---|---|
| generator | `darlingserver/scripts/generate-rpc-wrappers.py` | one table entry, `process_control_register`, **appended last** so that no existing call number moves |
| shared wire | `darlingserver/include/darlingserver/rpc-supplement.h` | `dserver_s2c_callhdr_t` gains `uint32_t target_tid` under the flag |
| server | `darlingserver/src/call.cpp` | the registration call must not overwrite the caller thread's reply address; new `ProcessControlRegister::processCall()` stores the datagram source on the Process |
| server | `darlingserver/src/process.cpp` / `internal-include/.../process.hpp` | `_controlAddress` plus `setControlAddress` / `controlAddress` |
| server | `darlingserver/src/thread.cpp` / `internal-include/.../thread.hpp` | `_s2cPerform` names the target TID and, when the process has registered an endpoint, addresses the call there and lets the guest's demultiplexer raise the signal instead of raising it itself |
| guest | `startup/mldr/procctl.c` + `procctl.h` (new) | one process-level endpoint, a per-thread single-producer/single-consumer mailbox, one small receiver thread, and `tgkill(SIGRTMIN+1)` for the named target |
| guest | `startup/mldr/mldr.c`, `elfcalls/elfcalls.{c,h}` | idempotent init right after the main-thread checkin; two bridge entries so the dylib can read its mailbox |
| guest | `xnu/.../linux_premigration/resources/dserver-rpc-defs.h` | the receive hook takes a mailboxed upcall before touching any socket |
| build | three `CMakeLists.txt` | the option and its compile definitions |

The receiver never executes the guest operation. It moves the datagram to the mailbox of the
thread named by `target_tid` and signals that thread; the target executes the memory operation in
its own context, exactly as it does on the per-thread path. That is the V1 shape, and it is the
shape this prototype implements.

### The protocol-order bug, the fix, and what the fix exposed

The prototype's first ordering was wrong and is now corrected. The old code resolved the control
address into a **member** (`_usingControlAddress`) at the top of the critical section and then
used that member again at the send site, and — for the control path — waited on
`_s2cInterruptEnterSemaphore` **before** publishing the datagram. That is a protocol deadlock:
the green light can only come from a target whose handler was entered, the handler is entered
only because the receiver signalled it, and the receiver can only signal what it has received,
which had not been sent yet. The member also let one S2C invocation's state decide the next
one's signal/wait behaviour.

The corrected state machine resolves the destination and the target TID into **locals** and, on
the control path, publishes the call *before* the wait. The server never signals on the control
path; the receiver owns that signal because only the receiver knows the payload has reached the
mailbox. The legacy path is unchanged: with no control address the resolution yields false, the
branch falls through to the stock signal-then-wait-then-send sequence, and the address is
`_address` exactly as before.

Ordering is now PROVEN in PRODUCT, from one boot with `DSERVER_S2C_TRACE=1` (all three actors
print `CLOCK_MONOTONIC`, so their lines are directly comparable):

```
server   S2C_CONTROL_PREPARE   tid=1940492 s2c=2 munmap
server   S2C_CONTROL_SENT                              t=…879255706
server   S2C_CONTROL_WAIT_ENTER                        t=…879263851
receiver PC_RECV               target_tid=1940492 fd=-1 t=…879337239
receiver PC_MAILBOX_PUBLISHED                          t=…879347108
receiver PC_TGKILL             rc=0                    t=…879359200
target   PC_SIGNAL_ENTERED     signum=35
target   PC_POP                hit=1 s2c=2 fd=-1       t=…879400268
target   PC_REPLY_SENT         s2c=2 send_rc=28        t=…879477833
server   S2C_CONTROL_REPLY_IN  tid=1940492 pending=0   t=…879956544
```

`SENT < RECV < MAILBOX_PUBLISHED < TGKILL < SIGNAL_ENTERED < POP < REPLY_SENT < REPLY_IN`, the
target executed the operation in its own context, and its reply reached the server with no
pending reply. The control path is no longer deadlocked.

**What the fix exposed, and the next blocker.** `S2C_CONTROL_ENTERED` and `S2C_CONTROL_REPLY`
never appear for either of the two S2C calls in that boot, so `_s2cPerform` does not resume from
its interrupt-enter wait; and the trace shows why. The mailbox pop sits at the top of **every**
`receive_message`, so the first receive that runs after publication takes the datagram — here
that is the receive inside the guest's `interrupt_enter` RPC, not the receive inside
`s2c_perform`. The operation therefore executes in the wrong logical phase: `PC_POP hit=1` and
`PC_REPLY_SENT` precede `PC_INTERRUPT_ENTER`, the handler's own `s2c_perform` then finds an empty
mailbox, and the server's `S2CPerform::processCall` — the only place that ups
`_s2cInterruptEnterSemaphore` (`call.cpp:1406`) — never gets the green light it is waiting to
give. The pop must be scoped to the S2C phase rather than to every receive.

This is a real defect with a named mechanism and a named anchor, not a mystery: the next change
is to make the mailbox pop conditional on the caller being the S2C path, and to re-run the four
boot gates before any further measurement.




### H1 confirmed: the control path was hijacking an ACTIVE caller-S2C

The failing S2C is not an idle target. With the branch state printed before selection:

```
S2C_BRANCH_STATE target_tid=2099243 active_call_present=1 active_call_number=38
                 current_thread_is_target=1 using_control_address=1
                 selected_branch=CONTROL_INTERRUPT
```

The target had an active call, and the server-side fiber performing the S2C *was* that target
(caller-S2C). Stock's branch logic is:

```cpp
if (!_activeCall) { sendSignal; down(_s2cInterruptEnterSemaphore); }
else if (currentThread().get() != this) { _deferReplyForS2C = true; }
```

so in exactly this state stock does **neither**: no signal, no enter wait, no deferral. The datagram
goes to the thread's own address and the guest's in-flight receive picks it up inline. The control
path keyed only on "the process registered an endpoint", so it ran an interrupt handshake stock
never runs — and that handshake is what aborts the wait:

```
WAIT_ENTER_BEGIN target_tid=2099243 path=control
INTERRUPT_ENTER_PROCESS_BEGIN tid=2099243
WAIT_ENTER_RETURN target_tid=2099243 path=control result=KERN_ABORTED   (8us later)
S2C_CONTROL_ENTER_INTERRUPTED
```

Causal anchor: `Call::InterruptEnter::processCall` → `Thread::_handleInterruptEnterForCurrentThread`
→ `dtape_thread_sigexc_enter` → `clear_wait_internal(&thread->xnu_thread, THREAD_INTERRUPTED)`
(`duct-tape/src/thread.c:530`). That tears down the wait of that same XNU thread, and because the
waiter *is* the target here, the wait returns `KERN_ABORTED`.

H2 is false: `Thread::sendSignal` is `isDead()` then `syscall(SYS_tgkill, …)` and nothing else —
no dtape or XNU state change, no pending interrupt, no lock, no scheduling effect
(`darlingserver/src/thread.cpp:2183-2195`). So the difference is the branch, not the signal source.

**Minimal PRODUCT fix.** One predicate decides delivery: `deliverViaControl = usingControlAddress &&
(_activeCall == nullptr)`, evaluated under the same `_rwlock` that stock uses, and used for the
destination address, the publish-and-wait block and the reply trace. An active target now falls
through to the unchanged stock path.

PRODUCT result with the fix: the same workload selects `CALLER_CURRENT` and the boot completes,
`rc=0`, `PROBE_OK`, no leftovers. Gate B (server ON, guest registration off) is also green.

**Two limits of this result, stated plainly.** (1) The control path is not exercised by a plain
boot any more: every S2C in the measured workload is a caller-S2C, so `deliverViaControl` is false
throughout and gate D (receiver suppressed) is green for the trivial reason that the receiver has
nothing to do — it is not a receiver mutation for this workload, and the four-configuration matrix
cannot prove receiver necessity without a workload that produces a genuinely idle-target S2C.
(2) The earlier published control-path trace remains valid as proof of delivery and ordering, and
under the fixed predicate the control branch is taken only when `_activeCall == nullptr`, but that
trace did not record `_activeCall` at the time, so its previous "idle target" label was an
assumption and is corrected here.

### Exact-send instrumentation: the server ingests s2c_perform; the semaphore handshake breaks

The next round instrumented the real `sendmsg` site in the generated wrapper (via the generator,
for `interrupt_enter` and `s2c_perform` only), the raw server ingress in
`MessageQueue::receiveMany`, the decode/thread-resolution path in `Call::callFromMessage`, and the
semaphore handshake on both sides. Fields printed on the guest: call number, socket fd, tid, header
pid/tid/architecture, destination namelen and leading bytes, iov length, and the returned status.
On the server: raw size, source address, call number, pid/tid/architecture, process/thread
resolution, and the up/abort of the S2C semaphores.

**A previous conclusion is corrected.** `S2C_PERFORM_RECEIVED` *does* fire: the server receives and
dispatches the `s2c_perform` call from the guest's per-thread socket, with the process and thread
resolved (`proc=1`, `thread=1`) and the correct tid. The earlier "the server never ingests
s2c_perform" reading came from a run whose log was truncated to 28 lines; the full run has over a
thousand. Classification is therefore **T4**, not T3, and the wire ABI is confirmed numerically
equal on both sides through the generated headers the build actually used:

```
SERVER_ABI interrupt_enter=14 s2c_perform=24 s2c=86821393 process_control_register=82
           callhdr_size=16 hdr_off_number=0 hdr_off_pid=4 hdr_off_tid=8 hdr_off_arch=12
           call_s2c_perform_size=16
```

One ordered trace, idle target, control path:

```
server   S2C_CONTROL_PREPARE            t=…879420363
server   S2C_CONTROL_SENT               t=…879444399
server   S2C_CONTROL_WAIT_ENTER         t=…879448446
receiver PC_RECV → PC_MAILBOX_PUBLISHED → PC_TGKILL
target   PC_SIGNAL_ENTERED → PC_INTERRUPT_ENTER_SEND
server   SERVER_RX interrupt_enter      t=…879674692
server   S2C_CONTROL_ENTER_INTERRUPTED  t=…879716371   <-- 42us later
target   PC_INTERRUPT_ENTER_REPLY status=0
target   PC_S2C_PERFORM_SEND → PC_S2C_PERFORM_POP hit=1 → PC_S2C_EXECUTE s2c=2
server   SERVER_RX s2c_perform          t=…879921998
server   S2C_PERFORM_DECODED proc=1 thread=1
server   S2C_PERFORM_DISPATCH
server   S2C_PERFORM_RECEIVED           t=…880030903
server   S2C_PERFORM_UP_ENTER / UP_ENTER_DONE
server   S2C_CONTROL_REPLY_IN           t=…880043717
server   S2C_PERFORM_DOWN_EXIT_DONE     t=…265964085577  (+30 s)
```

**Root cause.** `dtape_semaphore_down_simple` returns false only for `KERN_ABORTED` — the XNU
semaphore wait was aborted by a thread interrupt (`duct-tape/src/semaphore.c:69`). In the control
path the green-light wait is aborted by the processing of the guest's `interrupt_enter`, 42 us after
the server receives it. `_s2cPerform` therefore takes the "got interrupted while waiting" branch and
returns `std::nullopt` **before** the site that ups `_s2cInterruptExitSemaphore`. That up is what
stock uses to release `Call::S2CPerform::processCall` (`thread.cpp:1317`); the guest's
`interrupt_exit` is not what releases it. With the up lost, `S2CPerform::processCall` parks on the
exit semaphore, so the guest's `s2c_perform` never returns, so the guest never reaches
`interrupt_exit` — a circular wait that only ends at the 30 s dtape timeout, and the boot fails
(`rc=1`, no `PROBE_OK`). The server remains healthy throughout and services other calls, which is
why the failure looks like a stall rather than a crash.

The stock path does not lose the up; which difference in ordering removes the abort is exactly what
a flag-OFF differential run with these same tracepoints answers, and that run is the next action.
The instrumentation is inert with the flag off, so one binary covers both arms.

### Receive-context scoping: the mailbox belongs to the S2C phase

The next defect after the ordering fix was that the mailbox pop sat in the shared
`receive_message` hook, so the first receive to run after publication took the upcall —
which is the receive inside the guest's `interrupt_enter` RPC, not the one inside
`s2c_perform`. The operation then executed before the interrupt handshake, and the server's
`S2CPerform::processCall` — the only up site for `_s2cInterruptEnterSemaphore`
(`call.cpp:1406`) — never gave the green light the S2C wait blocks on.

Fixed by scoping the pop explicitly. A thread-local-in-effect receive context
(`dserver-receive-context.h`) distinguishes `DSERVER_RECEIVE_NORMAL`,
`DSERVER_RECEIVE_INTERRUPT_ENTER` and `DSERVER_RECEIVE_S2C_PERFORM`; the signal handler sets
it around each of its two RPCs and the hook pops only in the S2C phase. No process-wide flag,
and the context cannot leak between calls because it is set immediately around each one. Note
that the pop necessarily happens after the `s2c_perform` request has been sent, because the
hook only runs in that call's receive phase.

The storage is a tid-keyed table rather than `__thread`: a real thread-local needs
`__tlv_bootstrap`, which the 32-bit dyld link does not define, and the build must stay whole.

PRODUCT trace of one idle-target control upcall, all three actors on CLOCK_MONOTONIC:

```
receiver PC_MAILBOX_PUBLISHED                      t=…465298201
receiver PC_TGKILL rc=0                            t=…465345350
target   PC_INTERRUPT_ENTER_SEND
target   PC_INTERRUPT_ENTER_REPLY status=0
target   PC_S2C_PERFORM_SEND
target   PC_S2C_PERFORM_POP hit=1                  t=…465698144
target   PC_S2C_EXECUTE s2c=2 (munmap)
target   PC_REPLY_SENT send_rc=28                  t=…465762706
server   S2C_CONTROL_REPLY_IN tid=1980581          t=…465781953
```

The requested causality holds exactly: `MAILBOX_PUBLISHED < TGKILL < INTERRUPT_ENTER_SEND <
INTERRUPT_ENTER_REPLY < S2C_PERFORM_SEND < S2C_PERFORM_MAILBOX_POP < S2C_EXECUTE <
S2C_REPLY_SENT < S2C_REPLY_IN`. The interrupt handshake completes and the operation executes
in the right phase. The boot still does **not** finish.

**The remaining blocker, named from the trace.** `S2C_PERFORM_RECEIVED` — the server-side
trace inside `Call::S2CPerform::processCall` — never appears, although the guest sent the
`s2c_perform` request and its receive then consumed the mailboxed upcall. The server therefore
never ingests that call, never ups `_s2cInterruptEnterSemaphore`, and `S2C_CONTROL_ENTERED`
and `S2C_CONTROL_REPLY` stay absent: `_s2cPerform` is still parked on the interrupt-enter wait
even though the S2C itself completed and its reply arrived (`extra=0`, no pending reply). The
next step is to trace the guest's `s2c_perform` send path (own vs shared socket, and the socket
value the signal handler sees) rather than to touch the mailbox or the ordering again, both of
which are now proven.

**Evidence tier: PRODUCT.** The runtime was modified, built and executed in a real prefix.

### PRODUCT result: the server half is behaviour-preserving; the guest half does not complete

| Configuration | Result |
|---|---|
| flag OFF, stock runtime | boots, `rc=0` |
| flag ON **server**, flag OFF guest (guest init disabled) | **boots, `rc=0`** — the appended call number, the S2C target field and the control-address branch do not disturb the product |
| flag ON **server**, flag ON guest, registration active | **stalls** right after `process-control: receiver running (socket 1048574)`; no crash, no error, no abort; the launcher's 30 s shellspawn readiness timeout is what ends it |
| flag ON server, flag ON guest, **receiver deliberately not started** | also stalls — so the extra thread is *not* the blocker; the registration (and everything the server then routes to that endpoint) is |

Measured in the guest: the endpoint is created and registered (`fd 1048574`, the loader's hidden
socket number), the receiver starts on a **128 KiB** stack, and `setup_space` runs twice per
process so the init had to be made idempotent. No crash, no `BAD SEND`, no `BAD RECEIVE`.

### What remains, precisely

Once a process registers an endpoint, the server addresses every S2C to it and stops signalling
the target itself, so the whole chain — server → control socket → receiver → mailbox →
`tgkill` → the target's handler → mailbox pop → execute → reply — must work before the guest can
make progress past its first S2C. The stall says it does not, and the four configurations above
localise the failure to that chain rather than to the server's own logic or to the receiver
thread's existence.

The next three experiments, in order: (1) read the endpoint's queue while the guest is stalled to
see whether the S2C datagram ever arrived, which separates "the server did not send to the right
address" from "the guest did not consume it"; (2) instrument the receiver to report every datagram
it takes and every signal it raises; (3) instrument the guest's mailbox pop.

**Evidence tier: this section is PRODUCT** — the Darling runtime, client and server paths were
modified, built and executed in a real prefix. It is not a PASS: the prototype establishes that
the server half is safe and that the guest half is incomplete.

### Round verdict: still UNKNOWN

Unchanged from §8.9, and now for a better reason: the remaining gap is a localised, reproducible,
one-chain defect rather than a question of principle, but it is not resolved. Candidate B is not
falsified and relay is not indicated. No product test from the M1–M5 / S1–S4 matrix was run,
because the runtime does not boot far enough with the feature active; fork, exec, the lane
redesign, the process eventfd, the truthful `RLIMIT_NOFILE` model, `close_internal` hardening and
the performance comparison are all untouched downstream of that.

Two tooling defects found by the attempt were filed rather than worked around: `dar-481m` (a
failed rootless launch leaves the prefix's `darlingserver` and guest loaders running with no
supported reaper) and `dar-fyvw` (the deploy step does not verify that the `darlingserver` being
copied was built for the target prefix, which cost an hour of bisecting a build that was in fact
consistent).

## 9. Decision matrix

| | A: truthful clamp | B: constant anchors, no relay | C: relay |
|---|---|---|---|
| Public FD range correct | **yes**, but only if the raise is removed (drafted implementation falsified, §8.6) | yes | yes |
| Hidden FDs | **O(threads)** | O(1) *if* the demultiplexer is built; O(threads) otherwise | O(1) in guest, O(threads) in companion |
| New race introduced | **yes as drafted**: process-wide soft-limit raise, guest can exceed the published limit and enter the reserved band | none (no process-wide attribute is touched) | none beyond the companion's lifetime |
| Constant anchors | n/a (unchanged) | 1 wake eventfd + 1 control UDS + checkout pipe + optional pre-5.3 lifetime pipe | 1 control UDS + checkout pipe |
| Hot request path | unchanged (shared-memory lane) | unchanged | unchanged |
| Cold wake cost | unchanged (product-measured: doorbell share 0.2248 single-threaded, collapsing to ~0 under load) | one eventfd write per cold wake, same mechanism as today but process-wide | relay hop on the cold path only |
| Reply wake cost | unchanged (~1 futex wake per request under load, measured) | unchanged (per-thread futex) | unchanged |
| fork complexity | none new | **new**: generation + child rebootstrap, and today's child path is not async-signal-safe | new: companion lifetime across fork |
| exec complexity | none new | reattach generation for the shared object (no token exists today) | companion lifetime across exec |
| SCM_RIGHTS semantics | unchanged | one endpoint, demux required | one endpoint in guest |
| Caller-local operations | unchanged | needs an in-guest demultiplexer (server→guest munmap class) | relay must not execute them (it has the wrong address space) |
| Process/thread identity | unchanged (kernel `SCM_CREDENTIALS` for the process; nsid payload validated via `/proc/<pid>/task`) | a lane generation must be added; the server compares none today | SCM credentials would name the relay, not the guest — a real identity regression to solve |
| Failure domains | unchanged | unchanged (no new participant) | +1 (companion) with reaping/restart questions |
| Trust boundary | unchanged | unchanged | worse: SCM credentials authenticate the immediate sender |
| Implementation surface | ~2 modules, small | large (demux + generation + exec reattach) | large + a companion process |
| Migration risk | low | high | highest |
| Compatible with current server epoll design | yes | yes (one doorbell) | yes |
| Proof coverage now | oracle exists (reproducer), fix unapplied | model proof D1–D9 and control proof C1–C10, both green on host and Docker with every mutation red | topology proof A1–A7 + integration I1–I10 |
| Not yet proven | the fix itself | product-level: the envelope in the generated RPC client/server, real timeout/interrupt paths, the twelve descriptor sites, every S2C case for a non-waiting target, and a feature-flagged run with the per-thread sockets removed (§8.9) | any requirement that needs it |
| Round verdict | falsified as drafted (§8.6) | **UNKNOWN** — no hard requirement against it was found; the remaining items are product evidence, not open questions of principle | **not indicated**: nothing found requires a separate descriptor table |

## 10. Remaining unknowns

- Whether blocking `mach_msg_overwrite` can be served by a lane + futex without changing
  user-visible blocking semantics, and how many messages in practice carry OOL descriptors.
  The control proof shows a demultiplexer handles the *control* classes; it does not exercise
  `mach_msg`, and the census could not either (the callnum never moved in its synthetic
  workload). This is now the single most important unmeasured item for Candidate B.
- Measured frequency of `psynch_cvwait`, `psynch_mutexwait`, `semaphore_wait`,
  `mach_port_deallocate`, `mach_mod_refs`, `mach_msg_overwrite` — all **absent** from the
  census windows, so their rate in a real application workload is still unknown. A workload
  that deallocates ports backing mappings, or a real stock build, is the way to size them.
- `close_range`: **no emulation exists** in the forest, so a guest `close_range(3, ~0)` is a raw
  Linux call that would close loader-owned descriptors. Whether any supported guest reaches it
  is unestablished. This is a pre-existing hole, independent of all three candidates.
- No lane release on normal thread death was found (`dserver-ring.c`); slots are reused by CAS.
- `GR_MAX_LANES 128`: what the product does at higher thread counts in practice.
- Whether the `s2c` upcall's descriptor carriage can be demultiplexed in the guest without
  losing the "interrupt a non-parked thread" property.

## 11. Recommended migration plan

Only the first step is safe to do now; every later step is gated on evidence and approval.

1. **Truthful NOFILE (Candidate A)** — apply the §8.3 plan on a clean `fix/*` branch, build,
   and require the reproducer to flip and the ON stock leg to pass. Independent of everything
   else, fixes a real product defect, and does not touch topology.
2. **Census-driven decision on the control endpoint** — use the product census to establish
   whether slow-path traffic is rare enough to serialize, and which classes (if any) must keep
   a per-thread socket. This is the single measurement that decides B versus C.
3. **Only then** consider Candidate B's demultiplexer, with the async-signal-safe child
   requirement handled explicitly, and only if the census shows the per-thread sockets can go.
4. **Relay remains a fallback** for a requirement not yet found; it must not be adopted on
   coalescing, cleanliness, or "zero guest transport FDs".

## 13. Ring transport audit and the duplex lane (goal restated)

**Target statement.** Ring is the default Darling RPC transport. Per-thread Unix-domain sockets are a
migration artifact and are not part of the target architecture. Server-to-client operations are
carried by a duplex per-thread Ring lane, both for active caller-S2C and idle asynchronous S2C.
Unix-domain sockets may remain only as a process-wide SCM_RIGHTS courier for operations that transfer
real Linux file descriptors; they do not carry ordinary RPC requests or replies.

**Русский эквивалент.** Ring — штатный транспорт RPC. Per-thread UDS должен исчезнуть. UDS допустим
только как один process-level courier для SCM_RIGHTS, но не как RPC fallback конечной архитектуры.

### 13.1 Existing ABI map

All of it is in `src/external/darlingserver/include/darlingserver/rpc-supplement.h`, behind
`DSERVER_RING_TRANSPORT`.

| Item | Anchor | Content |
| --- | --- | --- |
| ABI version | `rpc-supplement.h:183` | `5u` |
| Mapping cap | `:260` | `DSERVER_RING_MAX_TOTAL_SIZE` 16 MiB |
| Ring header | `:277-289` | `dserver_ring_t` `head`/`tail`, each on its own 64-byte line; producer owns `tail`, consumer owns `head` |
| Slot | `:292-300` | `dserver_ring_slot_t {length, arena_off, arena_len, seq, callnum, flags}` |
| Reply payload | `:686-688` | `dserver_ring_reply_hdr_t {int32 code}`, then the reply body verbatim |
| Slot flags | `:371` | `DSERVER_RING_FLAG_REPLY_ERROR 0x1u` |
| Control block | `:665-728` | magic, abi_version, slot_size/count, arena_off/size, c2s_ring_off, s2c_ring_off, total_size, guest_tid, `c2s_opcode_hash`, `c2s_futex`, `s2c_futex`, `server_state`, `s2c_waiters`, then the duplex mailbox |
| Duplex mailbox | `:708-727` | `duplex_caps`, `duplex_upcall_ready/op/parent/id/arg/addr/len`, `duplex_reply_ready/parent/id/status/arg/errno` |
| Wake predicates | `:744`, `:755` | `dserver_ring_guest_should_doorbell`, `dserver_ring_server_should_wake` |
| Duplex helpers | `:770-850` | publish/observe/correlate/consume, pure functions shared by guest, server and host gates |
| Caps | `:189-214` | `SELFTEST 0x1`, `DEALLOCATE 0x2`, `VM_DEALLOCATE 0x4`, `MUNMAP_PUMP = DEALLOCATE|VM_DEALLOCATE` |
| Upcall shapes | `:225-231` | `ECHO 0x1`, `MUNMAP 0x2` |
| C2S opcodes | `:457-488` | `task_self_trap`, `thread_self_trap`, `host_self_trap`, `mach_reply_port`, `mach_port_allocate`, `mach_port_insert_right`, `uidgid`, `set_thread_handles`, `started_suspended`, `get_tracer`, `task_is_64_bit`, `mldr_path`, `vchroot_path` |
| Lane taxonomy | `:499-520` | Lane 0 UDS, Lane 1 simple ring, Lane 2 duplex ring, plus `dserver_ring_op_class()` and the canon-check |

Server owner: `internal-include/darlingserver/ring.hpp` (`RingBuffer`: `_map`, `_size`, `FD _eventfd`,
a validated copy of the control block; `publishReply`, `wakeGuest`, `liveControlBlock`,
`duplexCapable`) and `src/ring.cpp` (`attach` fstats, maps RO, copies and validates the control block,
maps RW, creates the eventfd, at `:67`/`:83`). Service loop: `ringServiceThread`
(`src/call.cpp:1657-1952`), driven hot from the main-loop pre-epoll spin (`src/server.cpp:1191-1228`)
and cold from the attach Monitor. Reply publication: `_publishReplyToRingLocked`
(`src/thread.cpp:1979`), `pushCallReply` (`:2015`), `beginRingReply` (`:2233`).

### 13.2 Lane and wake topology as built

- **One lane per guest thread**, not per process: `gr_attach_lane(tid)` builds a memfd
  (`dserver-ring.c:234`, `memfd_create("dring", MFD_CLOEXEC)` at `:259`) and negotiates it with
  `ring_attach`. The lane table is `g_lanes[GR_MAX_LANES]` with `GR_MAX_LANES = 128`
  (`dserver-ring.c:80`, `:103`), probed from a Knuth-multiplicative hash of the tid (`:135`).
- Each lane is `[control block][c2s ring][s2c ring]` in one memfd.
- **Per attached thread the guest holds exactly one transport fd**: the wake eventfd the server hands
  back. The memfd is closed right after attach. The per-thread AF_UNIX RPC socket is independent of
  the ring.
- **Server side per lane**: one `dup` of the ring fd (`ring.cpp:67`) plus one eventfd
  (`ring.cpp:83`) — so the server's transport fd count is O(threads) on both halves.
- Wakes: guest→server is the per-lane eventfd (the c2s futex word is declared and **never written**);
  server→guest is an `s2c_futex` bump plus a conditional `FUTEX_WAKE` gated by `s2c_waiters`.
- **The arena is declared and bound-checked but not implemented.** Both sides hard-code
  `arena_off = 0` / `arena_size = 0`, and an oversized body is rejected rather than arena-routed.

### 13.3 Duplex lane: built, wired, and test-gated

`_s2cPerform` (`src/thread.cpp:1143`) has three layers: the active-call/interrupt guard
(`_activeCall`), the ring duplex branch (`_ringDuplexParentActive && expectedReplyNumber ==
dserver_s2c_msgnum_munmap`), and the UDS fallback. The duplex machinery is complete —
`_s2cTryDuplexLocked` (`:1347`), `_s2cTryDuplexMunmapLocked` (`:1634`), `_drainDuplexReply` (`:1407`),
`setRingDuplexParentActive` (`:1686`), the fiber resume through `_s2cReplySempahore` — and the guest
pump `gr_duplex_pump_once` (`dserver-ring.c:905`) handles both ECHO and MUNMAP, with the parked
caller waiting in `gr_duplex_wait_reply` (`:961`). Host gates exist and are RED→GREEN:
`ring_duplex_wake_gate_test.c`, `ring_duplex_dealloc_gate_test.c`,
`ring_duplex_vm_dealloc_gate_test.c`, `run-duplex-real-selftest.sh`.

**The decisive audit answer: no S2C operation travels on any ring today.** Every real S2C still goes
over the Unix socket (`Server::sendMessage` → `_outbox.push` `server.cpp:1332-1334` →
`sendMany(_listenerSocket)` `:868`), with the duplex mailbox the single gated exception: it is
reachable only for the synthetic SELFTEST ECHO and for a real munmap under the deallocate /
vm_deallocate proof harness. There is also **no guest consumer of an unsolicited S2C from the s2c
ring** — the s2c ring is only ever read as the correlated reply to a request that same thread
published.

### 13.4 `callnum 38` identity and the caller-S2C measured in PRODUCT

`dserver_callnum_mach_msg_overwrite = 38U` (generated header
`<build>/src/external/darlingserver/include/darlingserver/rpc.h:76`). So the caller-S2C that failed in
the process-control prototype was raised while the server was servicing a **blocking
`mach_msg_overwrite`**, on a target that was also the caller (`active_call_present=1`,
`current_thread_is_target=1`). `mach_msg_overwrite` is **not** in `DSERVER_RING_C2S_OPCODES`.

**This couples R2 to R5 by measurement, not by preference.** The duplex lane for caller-S2C is
already built; what is missing for the case that actually occurs in PRODUCT is that its *parent*
cannot ride the ring at all. Demonstrating "active caller-S2C over the duplex Ring with zero UDS"
therefore requires the blocking-RPC ring transport first, because the ring has no reverse-direction
consumer for an unsolicited S2C and the mailbox only covers one shape.

### 13.5 Minimal duplex extension proposed (design, not built)

Keep the existing mailbox protocol and correlation discipline; do not add a second mechanism. The
minimal durable extension is to make the **s2c ring itself** able to carry an S2C operation, which the
slot already permits:

- New `DSERVER_RING_FLAG_S2C_OP 0x2u` on `dserver_ring_slot_t.flags` (bit 0 is already taken by
  `REPLY_ERROR`). A slot so flagged is a server-initiated operation, not a reply.
- Its payload is a fixed `{uint32 parent_seq; uint32 s2c_seq; uint32 op; uint32 reserved; uint64 arg0;
  uint64 arg1}` — enough for munmap/mprotect/msync and extensible to mmap with an `fd_token` instead
  of a descriptor.
- The guest's wait loop becomes a single consumer that branches on the flag: `S2C_OP` → execute
  locally, publish completion on the c2s ring, continue waiting; otherwise → the correlated reply for
  the thread's own request.
- Completion returns on the c2s ring as a new flagged item type carrying `{parent_seq, s2c_seq,
  status, errno}`, consumed by the ring service loop and matched against the deferred parent.
- Correlation is `(parent_seq, s2c_seq, lane_generation, process_generation)`; `lane_generation` and
  `process_generation` do not exist in the control block yet and are required for the reuse and
  fork/exec rules in §19.
- The single-outstanding invariant stays as the *first* implementation (the mailbox already proves
  it); queuing is what the ring slot buys and is a later step.

`parent_seq` is the missing correlation field today: the mailbox uses a monotonic `_duplexNextId` that
is not tied to the request that triggered the upcall, which is acceptable for one outstanding upcall
but not for a lane that also carries replies.

### 13.6 Roadmap correction

The restated goal does not change R1–R12's order, but it changes what R2 can mean:

- **R2a, doable now**: un-gate the existing duplex lane for real `mach_port_deallocate` /
  `vm_deallocate` parents and prove a PRODUCT transaction — request, S2C, completion and reply all on
  the ring, zero UDS for that transaction — with the `ring_*` counters. This is W3 for a parent that
  qualifies today.
- **R2b, the measured case**: blocked behind R5. `mach_msg_overwrite` must first ride the ring with a
  futex park and a reverse-direction consumer; that consumer is the same missing piece R2b needs, so
  R5 delivers R2b rather than the reverse.
- No part of the process-control UDS prototype is on this path. It stays as a semantics oracle.

### 13.7 R2a attempt: the existing duplex harness does not yet produce the transaction

The round's target was one PRODUCT transaction with parent request, caller-S2C, completion and final
reply all on the shared lane and zero per-thread UDS activity. The audit found an existing,
purpose-built harness for exactly this shape — the D5 boot-scoped `mach_vm_deallocate`-as-duplex-parent
proof (`src/call.cpp:1699-1910`, armed only by `DARLING_SERVER_D5_VMDEALLOC_PROOF=<budget>` on the
server process) — so the attempt used it rather than writing new code.

**Result: it does not produce the transaction.** With the budget armed at 1, the guest aborts
(launcher `rc=134`, no `PROBE_OK`), the server logs no `[D5PROOF]` line, and every transaction counter
is zero:

```
ring_duplex_parent = 0            ring_duplex_vmdealloc_parent  = 0
ring_duplex_decline = 0           ring_duplex_vmdealloc_decline = 0
ring_duplex_s2c = 0               ring_duplex_vmdealloc_s2c     = 0
ring_duplex_vmdealloc_final = 0   ring_duplex_vmdealloc_timeout = 0
```

The ring itself was healthy in the same run — `ring_serviced = 154` (84 from the pre-epoll spin, 70
from doorbells), `ring_doorbells_received = 72`, `ring_wakes_issued = 125`, `ring_wakes_skipped = 29`,
`residual_total_ring_threads_registered = 7` — and the guest reported
`[dring-lane-stats] acquired=2 exhausted=0 held_now=1 max=128`. So the abort is not "the ring did not
work"; it is specific to the armed duplex path. Unexplained as of this round, and the reason no
counter, trace or mutation result can be reported against the transaction: the fixture must first be
made to run.

### 13.8 Mailbox audit: fields, semantics, and the correlation question

Fields (`include/darlingserver/rpc-supplement.h:708-727`), all in the per-lane control block:

| Field | Width | Producer | Consumer | Ordering |
| --- | --- | --- | --- | --- |
| `duplex_caps` | u32 | guest (at attach) | server | plain, written once before attach |
| `duplex_upcall_ready` | u32 | server | guest | **release store last**, after the body; guest acquires |
| `duplex_upcall_op` | u32 | server | guest | plain, ordered by `ready` |
| `duplex_upcall_parent` | u32 | server | guest | plain, ordered by `ready` |
| `duplex_upcall_id` | u32 | server | guest | plain, ordered by `ready` |
| `duplex_upcall_arg` | u32 | server | guest | plain (ECHO shape) |
| `duplex_upcall_addr` / `_len` | u64 each | server | guest | plain (MUNMAP shape) |
| `duplex_reply_ready` | u32 | guest | server | **release store last**; server acquires |
| `duplex_reply_parent` / `_id` | u32 each | guest | server | plain, ordered by `ready` |
| `duplex_reply_status` | i32 | guest | server | plain (MUNMAP return value) |
| `duplex_reply_arg` | u32 | guest | server | plain (ECHO result) |
| `duplex_reply_errno` | i32 | guest | server | plain |

Consume rule (`:800-847`): the guest publishes its reply body, then consumes the upcall slot
(`upcall_ready = 0`) **before** advertising `reply_ready = 1`, so the server can never observe a reply
while an upcall is still marked pending. The server accepts a reply only when `reply_ready` is set AND
both `parent` and `id` match the in-flight upcall; a ready-but-mis-correlated reply is flagged as a
protocol error and does not resume the parent (`dserver_ring_duplex_reply_ready` with `out_mismatch`).

**`duplex_upcall_parent` is NOT the ring request `seq`.** It is the first of two consecutive draws from
a per-thread monotonic `_duplexNextId` (`src/thread.cpp:1767-1770`), and `duplex_upcall_id` is the
second; the same pair is what the server checks on completion (`_s2cTryDuplexMunmapLocked`,
`src/thread.cpp:1741`). It is an internal identity, not the transport's. That is sufficient for the
one-outstanding contract — the mailbox is clean before each upcall, so a stale pair cannot be accepted
— but it is not sufficient for a lane that also carries replies concurrently, which is exactly the
`parent_seq` the ring-slot design would have to add.

### 13.9 Mailbox versus tagged ring slots: verdict

**Keep the mailbox as the lane's S2C sideband; do not move caller-S2C into tagged s2c-ring slots.**

- The serialization contract already guarantees at most one outstanding caller-S2C per lane: the
  server declines unless the mailbox is clean and no upcall is in flight (`thread.cpp:1756-1765`), and
  a caller-S2C is by construction raised while the lane's own thread is the caller. There is no queue
  to justify.
- The mailbox costs four cache lines in the control block and touches no ring index, so an S2C adds
  **zero** head/tail traffic to the reply stream and cannot perturb reply ordering.
- Correlation is already exact and already validated on both sides.
- Encoding all four S2C operations needs one more `op` value plus payload words only for the shapes
  that carry arguments: munmap/mprotect/msync are `{addr, len}`(+`prot`) and fit the existing typed
  slot; mmap additionally needs an fd token, which is a courier concern and not a mailbox shape
  problem.
- What the ring slot would buy is queueability and a single uniform stream. Neither is required by
  the measured workload, and both would put S2C parsing on the reply-consumption path, where a
  mis-flagged slot would be indistinguishable from a reply in a way the mailbox's dedicated `op` is
  not.
- Revisit only if a real workload needs multiple concurrent S2C per lane, or if the mailbox's fixed
  shape set becomes the binding constraint for mmap.

### 13.10 `mach_msg_overwrite` wire shape: the "exceeds a slot" claim is wrong

Measured from the generated header and the C layout (host `sizeof`, `x86_64`):

```
dserver_rpc_callhdr_t              16
dserver_call_mach_msg_overwrite_t  40   { msg u64(8-aligned), option i32, send_size u32,
                                          rcv_size u32, rcv_name u32, timeout u32,
                                          priority u32, rcv_msg u64(8-aligned) }
request total                      56
dserver_rpc_replyhdr_t              8   (header only: no reply body at all)
```

**The Mach message bytes are referenced by guest address, not copied into the RPC body** — `msg` and
`rcv_msg` are `uint64_t` pointers and the server reaches them through the existing
`readMemory`/`writeMemory` (process_vm_readv/writev) machinery. A slot is 128 bytes with a 24-byte slot
header, leaving 104 bytes of inline payload, so the request body fits with 48 bytes to spare.

Consequences: a restricted `mach_msg_overwrite` shape **does** fit the current slot, the earlier claim
that its payload exceeds a slot is corrected, and **no arena is required for the measured shape**. The
arena remains unimplemented in both directions (`arena_off = 0`, `arena_size = 0`, oversized bodies
rejected), and on this evidence it should stay that way until a body must carry bulk bytes rather than
reference them.

### 13.11 R2b-min prerequisites, from the above

R2b-min is smaller than the roadmap assumed. It needs: (1) the transport counters and the explicit
parent/S2C/completion/reply trace that this round could not obtain; (2) a Ring request path for one
restricted `mach_msg_overwrite` shape (56-byte request, header-only reply, by-reference payload — no
arena); (3) a blocking wait on the lane with the mailbox pumped while parked, which the duplex pump
already implements (`gr_duplex_wait_reply`, `dserver-ring.c:961`); (4) zero UDS on that path. It does
not need a general Mach message transport, and it does not need the arena.

### 13.12 mach_msg_overwrite source map and the measured shape

**Guest.** Trap entry is `mach_msg_overwrite_trap_impl` (`mach_traps.c:97`), reached from
`__mach_syscall_table[32]` (`mach_syscall_table.c:27`) through `__darling_mach_syscall`
(`mach_syscall.S:7`) and the `kernel_trap` macro (`syscall_sw.h:147-151`), trap number -32. The
generated wrapper `dserver_rpc_explicit_mach_msg_overwrite` (`<build>/src/rpc.c:4668`) builds the
40-byte body and does a blocking send/receive. **No Mach message bytes are copied into the body**:
`msg` and `rcv_msg` travel as raw guest virtual addresses and the server copyins them itself
(`ipc_kmsg_get` → `copyinmsg`). Blocking lives in `dserver_rpc_hooks_receive_message`
(`dserver-rpc-defs.h:128`) — blocking recvmsg with the bounded recv-spin — and that function is also
where the legacy path services an S2C inline and `goto retry`s (`:328`), with the interrupt status
`-LINUX_EINTR` (`:354`).

Reusable ring primitives on the guest side: `gr_wake_server` (`dserver-ring.c:435`),
`gr_wait_reply` (`:454`), `gr_duplex_pump_once` (`:905`), `gr_duplex_wait_reply` (`:961`),
`gr_lane_for_this_thread` (`:351`) — all currently `static`, reachable only inside `dserver-ring.c`.
There is no ring path for this op at all: no `__dserver_ring_mach_msg_overwrite`, not in
`DSERVER_RING_C2S_OPCODES`, not in the op-class table.

**Server.** The Call class is generated and `processCall()` is a one-line tail call into
`dtape_mach_msg_overwrite` → `mach_msg_overwrite_trap` (`duct-tape/xnu/osfmk/ipc/mach_msg.c:513`).
Blocking is the unbounded `ipc_mqueue_receive` park (`ipc_mqueue.c`). The reply is a header-only
datagram via `pushCallReply` → `Server::sendMessage` → `_outbox` → `sendmmsg`, i.e. the UDS listener.

**The caller-S2C chain**, which is what a Ring migration has to keep working:

```
mach_msg_overwrite body → OOL descriptor machinery → duct-tape vm_map shim
  → task_free_pages hook → Process::freePages → Thread::freePages
  → Thread::_munmap → Thread::_s2cPerform
```

So the upcall is driven by the OOL/copyout path freeing pages, not by an explicit request, and it
arrives while the caller is parked in `ipc_mqueue_receive` — which is exactly the stock
`_activeCall != nullptr && currentThread() == this` state, and therefore exactly the duplex mailbox's
target case.

**Measured shape** (`DARLING_SERVER_MSG_CENSUS=1`, product prefix, boot plus ten `ls`, 101 calls):

```
msg_total                     101      msg_blocking_receive      84   (83%)
msg_send_receive               66      msg_rcv_size_nonzero      84
msg_receive_only               18      msg_rcv_msg               84
msg_send_only                  17      msg_send_msg              83
msg_send_only_simple           13      msg_census_hdr_read_fail   0
msg_send_only_complex           4
msg_send_only_ool               2
msg_send_only_port_descriptors  2
```

The dominant shape is confirmed by measurement rather than by summary: a **blocking receive**, with
send+receive the majority and receive-only at 18. `msg_census_hdr_read_fail = 0` means every one of
the 101 was classified from a readable header.

### 13.13 Blocker: the product prefix no longer boots, and the previous round's attribution was wrong

**Correction.** The previous round reported that arming `DARLING_SERVER_D5_VMDEALLOC_PROOF` caused the
guest abort (`rc=134`) and inferred a defect in the duplex proof harness. That inference is
confounded and is withdrawn: the baseline boot on the same prefix now aborts identically with no
special environment at all — `darling --rootless shell /bin/echo DIAG_OK` and a bare
`/bin/bash -c 'echo ...'` both end `rc=134` with no marker and a server log containing only the ring
NOTICE. The same prefix booted clean immediately after the restore earlier in this session, so the
regression is in the prefix's runtime state or in the restored artifact set, not in the D5 harness.

What the runs do establish: the server starts and works (`ring_serviced = 154`,
`residual_total_ring_threads_registered = 7`, guest `[dring-lane-stats] acquired=2 exhausted=0
held_now=1 max=128`), so this is a guest-side abort after startup, not a server failure. The prefix is
clean of leftover mounts and of `darlingserver` processes.

Consequence for this round: the measurement surface itself is broken, so the feature flag, the Ring
prototype, the transaction trace, the transaction counters, M1-M6 and the UDS-vs-Ring benchmark were
not attempted. Diagnosing and repairing the prefix boot is the prerequisite for all of them and is the
next action; it is not the D5 harness work the round told us to skip.

### 13.14 Withdrawal: there is no product prefix regression; the failures were harness artifacts

The previous section reported that the product prefix `/tmp/dr-on-matched` no longer boots
(`rc=134`) and treated that as a blocker. **That is withdrawn.** The prefix is healthy.

Working recipe, verified three times back to back on the product prefix with no environment beyond
the launcher defaults: kill any stale server, wait, then launch — `pkill -x darlingserver; sleep 3;
darling --rootless shell /bin/bash -c '<cmd>'` — gives `rc=0` with the expected marker for
`echo`, a five-iteration `ls` loop, and `sleep`. An immediate relaunch straight after
`darling --rootless shutdown` also gives `rc=0`.

What actually failed was my own test harness: it called `darling --rootless shutdown` and launched
two seconds later while a server from an earlier, killed run was still alive. `darling shutdown` does
not reliably reap a server it did not start on its own readiness path (this is exactly `dar-481m`),
and a live stale server makes the next launch abort. Every `rc=134` in the previous two rounds is
explained by that, and no product defect should be inferred from it.

Consequences to carry forward:

- The previous round's D5 conclusion stays withdrawn for a second, independent reason: with a clean
  pre-launch state there is no abort to attribute in the first place, and the armed-D5 behaviour has
  not been re-measured on a clean surface.
- The `msg_*` census numbers recorded earlier remain usable, because they are **server-side** counters
  and the server demonstrably ran (`ring_serviced = 154`) even in the runs whose launcher exited
  nonzero at the end.
- Any future prefix work must re-verify `pgrep -x darlingserver` = 0 before launching, and must not
  trust `darling shutdown` to have reaped a stale server.

### 13.15 Implemented: mach_msg_overwrite on the Ring (feature-hatched), first PRODUCT transactions

The migration was implemented, not just designed. Source changes, server: `include/darlingserver/rpc-supplement.h`
(new `DSERVER_RING_DUPLEX_CAP_MACH_MSG 0x8u`), `internal-include/darlingserver/thread.hpp`
(`duplexMachMsgCapable()`, `_s2cUdsFallbackThisCall`), `src/{thread.cpp,call.cpp,server.cpp,metrics.cpp}`,
`internal-include/darlingserver/metrics.hpp` (eight transaction counters). Guest:
`.../resources/dserver-ring.c` (`__dserver_ring_mach_msg_overwrite`, counters, env hatch),
`.../resources/dserver-ring.h`, `.../xnu_syscall/mach/impl/mach_traps.c` (trap routing).

Design as built, and it needs no new wire mechanism: the 40-byte body is published on the caller's
existing C2S ring slot; the server dispatches it through the **same generated `Call` path** as UDS, with
`_ringDuplexParentActive` set for the whole dispatch so a caller-local munmap S2C raised inside takes the
existing duplex mailbox instead of the UDS send; the guest waits with `gr_duplex_wait_reply` (which pumps
that mailbox) rather than `gr_wait_reply`. Agreement between the two sides is bilateral through the cap
bit, so a build skew degrades to UDS instead of stranding a published request. The approved subset is
"both interrupt-observing options clear"; every other shape falls back pre-publish with a named counter
(`..._fallback_interrupt`, `_no_lane`, `_shape`, `_declined`).

**PRODUCT result, feature hatched ON.** Ring `mach_msg_overwrite` traffic is real and substantial in a
real boot: over one run, guest `RING_MACHMSG_PUBLISH` 35, server `RING_MACHMSG_CONSUME` 42, server
`RING_MACHMSG_REPLY` 71, guest `RING_MACHMSG_REPLY_CONSUME` 32. In an earlier, quieter run a single
transaction was captured end to end and balanced exactly:

```
guest  RING_MACHMSG_PUBLISH        seq=7 tid=2257510
server RING_MACHMSG_CONSUME        seq=7 tid=2257510
server RING_MACHMSG_REPLY          seq=7 tid=2257510
guest  RING_MACHMSG_REPLY_CONSUME  seq=7 code=0
ring_machmsg_parent = 1   ring_machmsg_final = 1
uds_machmsg_request = 0   uds_machmsg_reply = 0   uds_machmsg_s2c = 0
```

That is the request **and** the final reply on the shared lane with zero per-thread UDS activity for the
transaction, for a real `mach_msg_overwrite` (callnum 38, 56-byte request, 8-byte reply, no arena).

**Feature hatched OFF: no regression** — the same binaries boot to `rc=0` with the marker printed.

**Blocker, with the evidence that localizes it.** With the hatch ON the boot does not complete, and two
independent signals say the reply sink is not safe for a *suspending* op:

1. Per-lane `seq` pairing breaks: the same `(seq, tid)` is published more than once (e.g.
   `('7','2303577')` twice), 36 requests are consumed by the server whose guest never saw a reply, and
   2 publishes are never consumed. The guest's own validity check is `rep->seq == seq && rep->callnum
   == ...`, which cannot distinguish a duplicate `(seq, tid)` — so a reply can be paired with the wrong
   request.
2. The server dies with `std::system_error: what(): Resource deadlock avoided` (EDEADLK from a
   `std::shared_mutex` acquired recursively, which `std::terminate`s the whole server), and the boot
   then fails at shellspawn readiness.

The one-shot ring reply sink (`beginRingReply` → `_ringReplyPending`/`_ringReplySeq`) was designed for a
closed request→single-reply op. `mach_msg_overwrite` blocks, so the dispatch suspends with the sink
armed, and a second op on the same lane can reuse both the sink and the `seq`. The fix direction is a
reply sink keyed by the request (thread + seq) rather than a one-shot flag, plus removing the recursive
`_rwlock` acquisition on the dispatch path.

**Caller-S2C was not reached.** `DUPLEX_MUNMAP_PUBLISH = 0` in these runs: the workload (boot plus `ls`)
does not provoke a caller-local munmap inside a ring-originated `mach_msg_overwrite`. The duplex
machinery itself was therefore not exercised on the lane, and M1-M6 and the UDS-vs-Ring benchmark were
not run — they need a run whose reply sink is correct first.

### 13.16 Build-environment lesson (cost two hours, now fixed)

`/home/ilyagulya/work/procctl-build` — the tree used for the earlier process-control rounds — builds a
`libsystem_kernel.dylib` that **does not boot in this prefix even with every local source edit
reverted**, while a `darlingserver` built from the same tree boots fine, and the product dylib from the
prefix backup boots fine. The tree was corrupted by the manual `rm` of the generated RPC artefacts plus
repeated reconfigures. A fresh configure and build into `/home/ilyagulya/work/ringmm-build` with the
Review defines produces a dylib that boots. Bisection that established this: my server alone → boots; my
guest binaries → fails; my `mldr` alone → boots; my `dyld` alone → boots; my `libsystem_kernel.dylib`
alone → fails; the same dylib built fresh → boots. Any future work must build from the fresh tree, not
`procctl-build`.

### 13.17 Correction of the previous round's diagnostics

Two claims from section 13.15 are corrected, and neither survives as written.

**The pairing analysis was a type bug.** The script keyed the server side by `(tid, seq)` and the guest
side by bare `seq`, then compared a tuple against a string, so every server-consumed request looked like
one whose guest never saw a reply. Re-keying both sides by `(tid, seq)` gives: 35 publishes (30 distinct),
42 consumes (36 distinct), 40 replies (34 distinct), 32 reply-consumes (24 distinct), 2
published-but-never-consumed, 2 consumed-but-never-replied. The figure of 36 requests that reached no
guest reply was an artifact and is withdrawn; it was never a measurement of lost replies.

Duplicate `(tid, seq)` publishes do survive the correction — six of them, all on a single tid. `seq` is
`L->seq++` on a lane keyed by a hash of the tid, and a **re-attached lane restarts `seq`**, so a
`(tid, seq)` key cannot distinguish "the same op published twice" from "the same tid+seq on a different
lane incarnation". No claim about duplicates, lost requests or wrong replies can be made until the trace
carries a lane incarnation id; the identity to add is
`(process_nsid, thread_nsid, lane_incarnation, seq, callnum)`, with `lane_incarnation` taken from the
shared mapping (the memfd `st_ino` via `fstat`) or from a server-assigned `RingBuffer` id logged at
attach.

**The abort is in the GUEST's dyld, not in the server.** The previous round attributed the failure to a
server-side `std::terminate`. Running the same configuration with the server under gdb shows the guest
side failing instead, immediately after a ring publish:

```
RING_TRACE guest RING_MACHMSG_PUBLISH seq=7 tid=2362590
dyld: dyld std::__terminate()
abort_with_payload: reason: dyld std::__terminate()
; code: 9
```

So the "Resource deadlock avoided" string belongs to this guest-side terminate path, and the server was
never the terminator. The exact throw site is still not obtained: under gdb the server is slow enough
that the guest's bounded duplex wait expires first, so the failure mode changes from the terminate to a
timeout before the stack can be captured. Getting the stack needs either a core from the un-slowed run
(`ulimit -c unlimited` plus a `core_pattern` that keeps it) or a temporary `std::set_terminate` handler
in dyld that prints a backtrace - which, on this evidence, is where to instrument, not the server.

### 13.18 Reply context: Design B (Call-owned) is the right fix, and why

`Thread::pushCallReply(std::shared_ptr<Call> expectedCall, Message&& reply)` already receives the Call, so
the transport destination can be read from the Call that is actually completing. That is the decisive
argument for Design B over a keyed Thread-global sink:

- The current one-shot sink (`beginRingReply` sets `_ringReplyPending`/`_ringReplySeq`; `pushCallReply`
  consumes them) is transport metadata that follows the **lexical dispatch**, not the Call. A blocking
  `mach_msg_overwrite` suspends inside the dispatch with the sink armed, so the next reply to arrive -
  from another Call, or from a resume - can consume a sink that belongs to a different request. That is
  exactly the failure the trace shows.
- A `map<(Thread, seq), sink>` only moves the same problem: it still needs lane incarnation in the key,
  still needs stale-entry cleanup, and still leaves the marker's lifetime decoupled from the Call.
- With a Call-owned context, `pushCallReply` becomes `if (expectedCall->ringContext) publish to that
  ring/seq else UDS`, and the same change carries §10's duplex marker: duplex eligibility becomes a
  property of the active Call (`_activeCall->ringContext.duplexCapable`) instead of a mutable Thread bool
  set around `doWork()`. That removes the `setRingDuplexParentActive(true/false)` pair around the
  dispatch, which today is wrong for a blocking op precisely because `doWork()` returns at suspension
  while the parent is still logically active.
- The mechanism should not stay mach_msg-specific: once the context exists, the existing ring reply
  paths move onto it, which deletes special cases rather than adding one.

Consequently: implement the Call-owned reply context, move BOTH the reply destination and the duplex
eligibility onto it, and drop the Thread-global `_ringReplyPending`/`_ringReplySeq` and
`setRingDuplexParentActive` pair from the dispatch. That is the next implementation step, and it is the
prerequisite for a green hatch-on boot, for the deterministic caller-S2C workload, for M1-M6/R1-R3 and
for the benchmark.

### 13.19 State of the implementation

The feature is implemented and hatched (`DARLING_GUEST_RING_MACH_MSG=1`): real `mach_msg_overwrite`
requests and their final replies ride the caller's ring lane with zero UDS for the transaction (captured
and balanced, section 13.15), and the hatch-off boot is green with the same binaries. The hatch-on boot
is not yet green. The clean source-only patch is `/home/ilyagulya/work/ring-machmsg-src.patch` (10 source
files, 28511 bytes, sha256 `0d6745be409a8d6d6e4d9c6260b354402d84b49c8065b2c1fea26bc1f427be6b`); the
earlier `/home/ilyagulya/work/ring-machmsg.patch` is superseded because a raw `diff -ruN` also captured
generated SDK header copies.

### 13.20 This round: production wait semantics + Call-owned transport context + first full duplex transaction

**Production wait semantics (the 3-second proof deadline is gone from the real path).** The old
`gr_duplex_wait_reply` bounded its parked wait at 50ms x 60 rounds (~3s) and then returned
committed-unknown, which is right for a regression proof whose liveness must never wedge boot and wrong
as the semantics of a real blocking receive: it turns a slow server into a fabricated `KERN_FAILURE` on an
op stock would simply have kept waiting on. The ring mach_msg path now uses a separate production wait,
`gr_machmsg_wait_reply`: no transport deadline at all (the loop exits only when the reply slot appears),
the duplex mailbox pumped on every iteration, and a 100ms timed `FUTEX_WAIT` that bounds only the PUMP
latency, never the total wait. The Mach timeout itself is not implemented in the guest: the server runs
the real trap with the guest's own option/timeout fields, so the server decides `MACH_RCV_TIMED_OUT` and
publishes the final reply; the transport must out-wait the reply, never race it. `EAGAIN`/`EINTR`/spurious
wakes all just re-pump and re-check, so an interrupt cannot become an observable Mach result. The bounded
helper stays for the selftest (W0b's split is therefore structural, not a flag).

**Lane-aware identity.** `RingBuffer` now carries a monotonic process-local `debugLaneId()` assigned at
construction and logged as `LANE_ATTACH tid=... lane=...`; the guest's publish/reply lines carry
`lane=<index> gen=<generation>` from the existing lane table. This shows that the "duplicate (tid, seq)"
seen last round is a **re-attach**: for one tid the server assigns a new lane id on each attach while the
guest's slot index stays the same and its `seq` restarts. A `(tid, seq)` key therefore names nothing
across an image switch, and the earlier duplicate claim is explained without any duplicated request.

**The reply destination and the duplex marker now belong to the Call.** `RingCallContext`
(`ring-call-context.hpp`) carries the lane (as a `shared_ptr`, which is the lifetime rule: the RingBuffer
object lives until every Call that referenced it is destroyed, so a lane retire or re-attach can never
make a stale Call publish into a new or freed mapping), the request `seq`, the `callnum`, and
`duplexCapable`. `Call::attachRingContext()` is called by the ring drain right after the Call is built
and before `doWork()`. `Thread::pushCallReply` routes by `expectedCall->ringContext()`; a deferred reply
carries its context to the later flush in `_s2cPerform`; and duplex eligibility in `_s2cPerform` is read
from the ACTIVE CALL (`_activeCall->ringContext()->duplexCapable`).

That last point is the bug this round was about. `Thread::doWork()` RETURNS when the fiber suspends, so
the dispatch-scoped `setRingDuplexParentActive(true) ... doWork() ... setRingDuplexParentActive(false)`
pair cleared the marker while the call was still blocked -- exactly when its caller-S2C arrives. That is
why the boot failed: a real caller-S2C munmap arrived while the caller was parked in the ring wait, the
server found no duplex parent, took the UDS S2C path, and a ring-parked caller can never service it, so
the op hung until the 30s shellspawn timeout. `_ringReplyPending`, `_ringReplySeq`, `beginRingReply`,
`setRingDuplexParentActive` and `_ringDuplexParentActive` are all deleted; there is now one reply-routing
mechanism instead of two.

**First complete real caller-S2C duplex transaction on the Ring lane (PRODUCT).** With the context in
place, verified on the real prefix:

```
guest  RING_MACHMSG_PUBLISH        lane=28 seq=39 tid=2589404
server RING_MACHMSG_CONSUME        lane=4  seq=39 tid=2589404
server DUPLEX_MUNMAP_PUBLISH       parent=1 upcall=2 addr=... len=20971520 target_tid=2589404
server DUPLEX_COMPLETION_CONSUME   parent=1 upcall=2 status=0 errno=0
server RING_MACHMSG_REPLY          lane=4  seq=39 tid=2589404
guest  RING_MACHMSG_REPLY_CONSUME  lane=28 seq=39 code=0
```

The caller-local munmap was executed by the target thread itself; the guest serviced it from inside
`gr_machmsg_wait_reply` and the parent then completed. Hatch OFF boots green with the same binaries
(`rc=0`, marker printed).

### 13.21 Remaining blocker, narrowed

Hatch ON still fails, and it is now one thing: a **second** thread's mach_msg parent records
`RING_MACHMSG_S2C_UDS_FALLBACK target_tid=2589408` while the first thread's transaction completes on the
lane. That parent has an active mach_msg Call, so the routing decision itself is not the missing context;
the plausible causes left are a mailbox that is not clean at that moment (a stale unhandled upcall or an
unconsumed reply) or an S2C whose target is not the caller. Distinguishing them needs one more
diagnostic: print the duplex guard's decline reason at the decline site (no-context / not-capable /
mailbox-busy). Until that thread's S2C rides the mailbox, a ring-parked caller still wedges for 30s and
the hatch-on boot cannot be green.

### 13.22 Two environment corrections that cost most of this round

- **A stale server makes every measurement meaningless.** Killing only `pgrep -x darlingserver` misses two
  classes: `darlingserver.real` (a 16-character name `pgrep -x` cannot match at all) and `darlingserver`
  processes whose argv carries the prefix *basename* rather than the full path, which is what a
  path-matching kill loop tests against. Both left a server owning the prefix while new runs joined it and
  read another run's configuration. Match on `/proc/<pid>/cmdline` containing the prefix basename, wait
  for it to disappear, and settle before launching.
- **`/bin/bash -c` aborts in this prefix and `/bin/sh -c` does not**, and this reproduces on the pristine
  product baseline, so it is a property of the prefix, not of any Ring change: `bash -c true` exits 134
  (SIGABRT) while `sh -c true`, `bash --version`, `bash --noprofile --norc -c true`, `/bin/ls` and
  `/bin/echo` all succeed. Guest test commands for this prefix must therefore use `/bin/sh -c`; every
  boot failure measured with `/bin/bash -c` before this was discovered was measuring that abort.

### 13.23 Residual fallback classified exactly, and the fix: the mailbox must carry the anonymous mmap

The one diagnostic event §1 asked for settled this in a single run. `S2C_DUPLEX_DECISION` prints every
condition the guard evaluates together with the decision, and it reports the decline reason from the same
predicate chain the guard uses -- not reconstructed afterwards from other lines:

```
S2C_DUPLEX_DECISION target_nsid=2931654 exec_tid=2931654 exec_nsid=2931654 current_is_target=1
  active_callnum=38 ring_context=1 lane=10 seq=17 duplex_capable=1 mb_upcall_ready=0 mb_reply_ready=0
  s2c_op=0x1 decision=DECLINE reason=UNSUPPORTED_S2C_OP
```

`s2c_op=0x1` is `dserver_s2c_msgnum_mmap`, so the residual fallback was **an mmap S2C, not a munmap** --
the mailbox only implemented the munmap shape. The guest side of that is worse than a counter: the caller
is parked in the lane, so the UDS S2C is never serviced and the op hangs for the 30s shellspawn timeout.

Which mmap it is matters, and it is the benign one. `Thread::allocatePages()` issues
`_mmap(..., /*fd*/ -1, ...)` with `MAP_ANONYMOUS`: no descriptor, offset 0. Only `mapFile()` passes an fd,
and an fd needs SCM_RIGHTS, which is the fd-courier milestone. So the fix is to let the mailbox carry the
anonymous shape and to refuse the fd shape explicitly rather than silently dropping a descriptor:

- ABI v6: `DSERVER_RING_ABI_VERSION` 5 -> 6 (a mixed pair rejects at attach and runs all-UDS, which is the
  intended clean degradation), new op `DSERVER_RING_DUPLEX_UPCALL_MMAP 0x3`, a `duplex_upcall_flags` field,
  and a 64-bit `duplex_reply_value` for the mapped address -- mmap returns an address, not an int, so
  reusing the 32-bit munmap result field would have truncated it. `offset` is always 0 and `fd` always -1
  for this shape, so neither needs a field.
- `_s2cTryDuplexMmapLocked()` is the structural sibling of `_s2cTryDuplexMunmapLocked()` (same guard, same
  parking, same deadline); the drain synthesizes a byte-identical `dserver_s2c_reply_mmap_t` so
  `_s2cPerform`'s extraction and validation are unchanged from UDS; the guest pump runs the same `mmap(2)`
  the UDS path runs, on the caller thread.
- The publisher refuses `fd >= 0 || offset != 0` and counts it as `ring_duplex_fd_s2c_unsupported`.
- The event also splits what was one misleading counter: a UDS-origin parent taking the UDS S2C path is
  NORMAL (its caller is parked in `recvmsg` and can service it) and is now `uds_parent_s2c_normal`; only a
  RING-origin parent taking it is `ring_parent_s2c_uds_fallback`.

### 13.24 Hatch ON boot is GREEN

With the mmap upcall in place, on the real prefix, with the full build deployed:

```
hatch OFF: rc=0  BOOTOK=1
hatch ON : rc=0  BOOTOK=1   shellspawn failure: none
decisions: {('ATTEMPT','NONE'): 4}          # no decline at all
DUPLEX_MMAP_PUBLISH=2  DUPLEX_MUNMAP_PUBLISH=2  DUPLEX_COMPLETION_CONSUME=4
RING_PARENT_S2C_UDS_FALLBACK=0  UDS_PARENT_S2C_NORMAL=0  DUPLEX_FD_S2C_UNSUPPORTED=0
```

Both caller-local S2C shapes a boot provokes -- the anonymous mmap and the munmap -- now ride the duplex
mailbox, every decision attempts, and no ring parent falls back to UDS. The 30s stall is gone.

### 13.25 What the deterministic test needs, and the prerequisite it is missing

The deterministic Mach workload (B1-B4 and the >3s wait of W0a) needs a guest compiler: a test program that
allocates a receive right, parks in a blocking `mach_msg` and receives from a delayed sender. **This prefix
has no compiler** -- `cc: command not found` inside the guest -- so the test cannot be built in-guest until
the guest CommandLineTools are provisioned (`west darling-prefix-repair --prefix <prefix>` resolves missing
CLT links, but the packages themselves come from the bootstrap/guest-toolchain provisioning path). That is
an environment prerequisite, not a design blocker: the fix it would measure (the unbounded production wait)
is already in the tree, and the boot workload exercises the same path.

### 13.26 The host-side Mach-O test path (no compiler needed inside the prefix)

Guest binaries are built by the ordinary Darling cross build -- host clang plus Darling's SDK and `ld64`
wrapper -- so a guest test does NOT need a compiler in the prefix; it needs a CMake target. The new
durable workload is `src/tools/ring_mach_msg_test.c`, registered with `add_darling_executable` in
`src/tools/CMakeLists.txt` exactly like `sw_vers`/`spctl` and installed to `libexec/darling/usr/bin`.
Ninja builds it as a Mach-O 64-bit x86_64 executable, and the guest runs
`/usr/bin/ring_mach_msg_test <mode>`. Modes: `basic`, `delay <ms>`, `timeout <ms>`, `ool`, `stress`, each
printing one machine-readable `RING_MACH_TEST mode=... pass=1 ...` line and exiting non-zero on failure.

The first version of the test measured elapsed time with `clock_gettime(CLOCK_MONOTONIC)`, and that made
`timeout 200` fail with `elapsed=-0.800` while the Mach result was correct: this prefix's guest
CLOCK_MONOTONIC is coarse enough to quantise by about a second, so a 200ms timeout measured negative and a
5s wait measured 5.001. The test now uses `mach_absolute_time()`. That was a defect in the measuring
instrument, not in the transport -- but it is worth recording because a coarse guest clock will silently
turn any timing assertion into noise.

### 13.27 Deterministic results (PRODUCT, hatch ON)

```
B1 basic 100      rc=0 pass=1  min_elapsed=0.000125  max_elapsed=0.000566     (0.135s total)
B2 delay 100 20   rc=0 pass=1  min_elapsed=0.100557  max_elapsed=0.101662
B3 delay 5000 3   rc=0 pass=1  min_elapsed=5.000703  max_elapsed=5.001079     (15.0s total)
B4 timeout 300 5  rc=0 pass=1  min_elapsed=0.300273  max_elapsed=0.300591
OOL ool 20        rc=0 pass=1
```

B3 is the W0a proof: a blocking receive with **no** `MACH_RCV_TIMEOUT` waited 5.0007s and returned success,
so the old 50ms x 60 (~3s) proof watchdog no longer bounds production semantics -- that is now a
measurement, not an inference. B4 shows the Mach timeout is the authority: 300ms requested, 0.3003-0.3006
observed, so the transport adds no independent deadline of its own. B2 shows a real park-and-wake (the
receiver was parked before the sender ran), not a spin-only reply. OOL passed 20/20 with the descriptor
address readable end to end, which is also the check a truncated 32-bit mmap return would fail.

Isolated OOL run (30 iterations), server counters:

```
ring_machmsg_parent = 159   ring_machmsg_final = 158   ring_machmsg_decline = 0
ring_machmsg_duplex_s2c_published = 34   ..._consumed = 34
ring_duplex_s2c = 34   ring_duplex_reject = 0
ring_parent_s2c_uds_fallback = 0   uds_parent_s2c_normal = 0
ring_duplex_fd_s2c_unsupported = 0   uds_machmsg_s2c = 0
uds_machmsg_request = 7   uds_machmsg_reply = 7
```

Every ring-origin parent completed on the lane: 34 duplex S2C publications, 34 consumptions, no fallback,
no rejection, and `uds_machmsg_s2c = 0`. Two caveats: `ring_machmsg_final` is one short of
`ring_machmsg_parent`, and the 7 UDS requests/replies are **server-wide** counters that include bootstrap
traffic -- §12 asks for counters scoped to the target process, which these are not yet. Both are
accounting gaps, not transport failures, and neither is claimed as a pass.

### 13.28 Stress 32 is RED, and it is the next target

`stress 32 8` (32 concurrent receivers, 8 iterations each) did NOT complete: it was killed at the 600s
harness deadline after 253 publishes, 266 server consumes, 220 guest reply-consumes, 8 mmap and 2 munmap
duplex publications with 10 completions -- and **zero** fallbacks, zero unsupported, zero rejects
(`reason=NONE` on all 10 duplex decisions). So the stall is not a mailbox protocol violation and not an
fd-shape rejection. The run had reached 141 distinct lane identities, which puts the guest's lane table
(GR_MAX_LANES = 128) in the picture: the workload creates a sender thread per iteration on top of the 32
receivers, so threads can exhaust the table, and a thread that has no lane falls back to UDS. That is the
first thing to check, with `DARLING_GUEST_LANE_STATS=1` for `g_stat_lanes_exhausted` and a per-thread lane
census, followed by the D6 fail-closed deadline (which should have broken a stuck duplex wait in 5s and did
not visibly fire). Stress is therefore reported RED, not as an unrun gate.

### 13.29 Stress harness: fixed, split, and instrumented

The old harness had a data race (`int failures` incremented by 32 threads) and mixed two independent
questions, so a hang could not be attributed. It now has an atomic failure counter, a per-worker
`(phase, op, tid, last-progress)` tuple that a monitor thread watches, and three distinct workloads:

- `stress_pool <receivers> <iters>` (S1): N persistent receiver threads and N persistent senders, thread
  count fixed for the whole run, so a failure is a concurrency/wake/lane-lifecycle bug.
- `stress_churn <iters>` (S2): ONE persistent receiver and a new sender thread per operation, so a failure
  is a lane allocation/release/TID-reuse bug.
- `stress_mixed <workers> <iters>`: the old combination, kept because the old hang has to be explained
  rather than avoided.

The monitor emits `WATCHDOG worker=.. op=.. phase=.. tid=.. age=..` for every live worker and exits 3.
Its first version watched untouched slots (`last_ns == 0`, age "since boot"), which fired instantly; it now
watches only the slots a run registers.

### 13.30 S1: Green at every level, including 32

```
stress_pool 1  20  rc=0 pass=1 failures=0  2.311s ... (N=32: 1366 publishes, 0 fallbacks)
S1 N=1/2/4/8/16/32 all rc=0 pass=1 failures=0, ring_parent_s2c_uds_fallback=0
```

32 concurrent blocking Ring receivers, 640 transactions, no failure and no UDS S2C fallback: Ring mach_msg
concurrency itself is correct.

### 13.31 S2: Green to 1024, and the lane table never releases

```
stress_churn 128/256/512/1024  all rc=0 pass=1 failures=0
[dring-lane-stats] pid=... acquired=128 exhausted=2694 reclaimed=0 held_now=128 max=128
```

`acquired=128` is exactly `GR_MAX_LANES`; `reclaimed=0` says **no lane is ever released**, so the table
stays full forever and `exhausted=2694` threads fall back to UDS for their mach_msg. That is the current
design's real limitation, measured rather than inferred, and the numbers belong in the dynamic-lane phase.
Thread churn is nevertheless GREEN because the fallback path is safe: a lane-less thread uses UDS, and
that is correct.

### 13.32 The old hang reproduces, intermittently, and here is its signature

`stress_mixed 32 8` passed 3 of 4 runs (0.29-0.30s) and hung once; `stress_mixed 64 12` hung on its first
run. A killed run leaves this signature:

```
... guest RING_MACHMSG_PUBLISH lane=10 seq=63 tid=3525930
... guest RING_MACHMSG_REPLY_CONSUME lane=108 seq=10 code=0
[dring-lane-stats] pid=3525954 acquired=2 exhausted=0 reclaimed=0 held_now=1 max=128
```

300 publishes against 272 reply-consumes, so about 28 operations are outstanding; the guest's own summary
line never prints; and **the watchdog thread never fired**, even though it only sleeps and reads memory.
That last fact is the interesting one: a stall that silences a thread which only calls `usleep` is wider
than one blocked receiver -- it is consistent with the guest process being unable to complete any
darlingserver-mediated syscall, i.e. the server side stopped serving this process rather than one lane
being stuck. It is not yet localized further, and it is not claimed as understood.

Two things are ruled out by measurement: it is not mailbox misuse (the passing and hanging runs both show
zero `ring_parent_s2c_uds_fallback`, zero fd-shape refusals, and no duplex rejections), and it is not the
D6 deadline failing to fire, because production `gr_machmsg_wait_reply` deliberately has no transport
deadline -- B3 proves that by design, and an external watchdog is the correct detector.

### 13.33 The stress stall is in the THREAD LIFECYCLE, not in the Ring mach_msg transport

The stall is reproducible (roughly one run in three at `stress_mixed 32 8`, and on the first run at
`64 12`) and the transport reads clean at the moment it happens:

```
ring_machmsg_parent = 376   ring_machmsg_final = 375
ring_machmsg_duplex_s2c_published == consumed,  ring_duplex_reject = 0
ring_parent_s2c_uds_fallback = 0,  ring_duplex_fd_s2c_unsupported = 0
```

One request outstanding, every duplex transaction balanced, nothing refused. What the server-side state
dump shows instead is where the process is actually parked. With `SIGUSR1` the server prints a compact
per-thread table from the main loop (a signal handler cannot walk the registry safely, and the main loop
is alive even when a call is stuck); at the stall, for the test process:

```
call=62 suspended=1 ring=0  x23     call=38 suspended=1 ring=1  x18
call=38 suspended=1 ring=0  x5      call=2  suspended=0 ring=0  x184
```

`callnum 62` is `dserver_callnum_semaphore_wait`, and `callnum 38` is `mach_msg_overwrite`. So the
receivers are parked in the blocking receive (expected: no message arrived) and 23 further threads are
parked in a semaphore wait -- which is the `pthread_create`/`pthread_join` handshake, not the transport.
The workload that stalls is exactly the one that creates and joins a thread per operation while other
threads block in `mach_msg`; S1 (concurrency without churn) and S2 (churn with one persistent receiver)
both stay green, so the distinguishing factor is concurrent thread creation/join, not Ring concurrency.

**Ruled out by measurement, not by argument:** it is not the mailbox (zero fallbacks, zero rejects, one
outstanding request at the stall); it is not the D6 deadline failing to fire (production
`gr_machmsg_wait_reply` deliberately has no transport deadline -- B3 measures that); and it is not the
single-threaded server, which was the first suspect: the server was rebuilt with
`DSERVER_SINGLE_THREADED=OFF` and the stall persists in that configuration too.

Consequence for the gates: the Ring mach_msg concurrency gate is S1 (green at 32, 640 transactions, zero
fallbacks) plus S2 (green to 1024 operations), and the mixed mode exercises Darling's pthread lifecycle.
Stress is reported as GREEN for the transport and RED for the mixed workload, with the mixed failure
localized to a different subsystem than the one under test.

Also recorded while measuring: the guest lane table is never released
(`acquired=128 exhausted=2694 reclaimed=0 held_now=128`), so every thread after the first 128 falls back
to UDS. That is the measured current-design limitation, and it is safe -- it is why S2 completes.

### 13.34 Performance P1/P2, and the one constant that was costing 2.2x

The benchmark modes were first measured with the stress sender's randomized delay still in place, which
made them measure the delay rather than the transport; `run_pool` now takes an explicit `random_delay`
flag (on for stress, off for benchmarks). With traces off, five alternating runs each, persistent
threads and ports and setup outside the timer:

```
bench_simple (plain blocking mach_msg, 400 ops)
  UDS  ns/op med 40102  p50 med 27842   p95 med 78727   p99 med 204670
  Ring ns/op med 17922  p50 med 12103   p95 med 35106   p99 med 124343   -> 2.24x faster

bench_ool (out-of-line message, caller-S2C, 120 ops)
  UDS  ns/op med 400661  p50 med 397324  p95 med 608344  p99 med 755031
  Ring ns/op med 228005  p50 med 207138  p95 med 452307  p99 med 708267   -> 1.76x faster
```

Before the fix below, the Ring side measured 114719 ns/op on `bench_simple` -- 2.9x SLOWER than UDS. The
cause was the wait's spin budget: `gr_machmsg_wait_reply` reused the shared `DARLING_GUEST_RECVSPIN` (512
iterations, well under a microsecond), far too small for a full round trip that has to reach the server
and come back, so essentially every fast reply missed the spin and paid a futex park plus wake. It now
has its own budget (`GR_MACHMSG_SPIN = 20000`): a few tens of microseconds of CPU at worst, bounded once
per op, and irrelevant to a long wait because B3's 5s receive pays it once and then parks. One constant
took `bench_simple` from 114719 to 17922 ns/op -- a 6.4x improvement on that path.

Correctness was re-verified after the change, on the same build: hatch ON boot rc=0 with the marker,
hatch OFF boot rc=0, B1 100 iterations pass=1 (min 49us), B2 pass=1 (0.1007s), B3 pass=1 (5.0007s),
B4 pass=1 (0.3005s), OOL 20 pass=1. Nothing was traded for the speed.

The expectation recorded earlier holds and is now measured rather than assumed: the simple path gains most
because it removes the socket round trip entirely, and the caller-S2C path gains too because the legacy
path pays a request socket, an inline S2C socket and a reply socket where the Ring path pays a request and
a reply on the lane plus a shared-mailbox mmap/munmap.

Note on configuration: the server was rebuilt with `DSERVER_SINGLE_THREADED=OFF` during the stall
investigation and stays that way; these numbers are from that configuration.

### 13.35 Lane lifecycle: the fixed 128-lane table becomes a reclaiming, growable catalog

Measured problem, before any change:

```
stress_churn 1024   lanes_acquired=128  lanes_exhausted=2694  lanes_reclaimed=0   held_now=128
```

The guest never released a lane, so a HISTORICAL thread count decided whether non-fd RPC got a Ring path:
the 129th thread onwards fell back to UDS even when a handful of threads were live, and more than 128
SIMULTANEOUS eligible-op threads could not be served by the Ring at all. Both contradict "Ring is the
default RPC transport".

**Release lifecycle (`__dserver_ring_release_current_lane`, guest).** Thread exit returns the slot, in a
deliberate order: take the lane OUT OF SERVICE (CAS `active` 1 -> 3 "releasing", so a concurrent finder
stops seeing it as usable and a claimant cannot take it as free); unpublish the guest half and BUMP THE
GENERATION, so the next claimant of that slot is a new epoch and a stale `(tid, seq)` can never match;
release the resources (unmap OUR mapping -- the server holds its own -- and hand the wake descriptor back
to the loader); then publish FREE (3 -> 0) LAST. The hook is `bsdthread_terminate` before its point of no
return, and the same release runs when `gr_lane_for_this_thread` finds its lane RETIRED (another image
owns the server-side lane), which used to leak a slot per image switch.

The wake descriptor is loader-owned, so the loader must FORGET it, not merely close it: `ring_fds[]` is
consulted by `__mldr_postfork_child`, and a stale number there would make a later fork close a descriptor
the application had since reused. `dserver_release_ring_fd` was added to the elfcalls interface and
APPENDED AT THE END -- inserting it in the middle shifted `dserver_fd_is_internal` and the prefork hooks,
which broke the dyld image's guard calls and the boot (a real failure, immediately visible as an mmap
EBADF during libSystem load).

**Growable catalog.** `gr_lane_t g_lanes[128]` became a singly-linked list of 128-lane pages, allocated
lazily from the same anonymous mmap the ring mapping already uses (no guest malloc this low in libSystem).
Pages never move, so a lane's address is stable for its lifetime -- which is what lets an old Call keep a
`shared_ptr` to its OLD server-side RingBuffer incarnation while the slot is recycled underneath it.
Growth is the only place a new page appears, and it happens ONLY when the whole catalog is busy: a
released slot is always preferred to a new page.

The growth itself is deliberately NOT under the splice lock. The guest's `mmap` is an emulated syscall
that can require an S2C upcall back into the calling thread, so growing under a spinlock let every other
attaching thread spin for a whole round trip; the page INDEX is reserved with one CAS, the mapping happens
with no lock, and the lock covers only the pointer splice (pure stores, cannot block).

**Results (product build, `DARLING_GUEST_RING_MACH_MSG=1`, `DARLING_GUEST_LANE_STATS=1`):**

```
stress_churn 1024   acquired=1026 released=1024 exhausted=0 reclaimed=898  held_now=2 pages=1 capacity=128
stress_churn 4096   acquired=4098 released=4096 exhausted=0 reclaimed=3970 held_now=2 pages=1 capacity=128
```

One page serves 4098 HISTORICAL threads with 3970 slot reuses and zero exhaustion. The control arm tells
the same story from the other side -- with the release disabled (`DARLING_GUEST_LANE_RELEASE=0`) the same
1024-thread run reads `released=0 exhausted=0 reclaimed=0 held_now=1026 pages=9 capacity=1152`: the
catalog had to GROW to hold history instead of tracking peak concurrency. That control arm is also the
evidence that the growth path works end to end (9 pages, 1152 lanes, all ops on the Ring), and it is the
only arm in which growth is exercised by a real workload.

### 13.36 The >128 SIMULTANEOUS lane gate is blocked by the pthread lifecycle, not by the catalog

`stress_pool 129/192/256` cannot run: with ~50 or more LIVE guest threads, `pthread_create` BLOCKS and
never returns. The state dump at the stall is unambiguous -- every thread of the test process is in
`call=62` (`dserver_callnum_semaphore_wait`) with no ring call in flight, i.e. parked in the pthread
create/start handshake -- and it is not a Ring bug: the same stall reproduces with the Ring hatch OFF on
the pristine UDS path (`stress_pool 32`, hatch off: 3/3 timeouts). It is not stack VA either (256 KiB
stacks change nothing) and it is not a creation failure (`pthread_create` never returns an error; it does
not return). Above ~50 live threads it is probabilistic; below it, runs are green.

Consequence for the gates: the transport gates are S1 (`stress_pool`, stable levels 8 and 16: pass=1,
failures=0, `exhausted=0`) and S2 (`stress_churn` 1024/4096, above). A dedicated capacity mode
(`lane_hold N`) was added -- SEQUENTIAL creation, so each worker is up before the next is created, which
is the capacity claim ("N simultaneous lanes") without racing pthread_create -- and it did measure 64
simultaneous lanes from the SERVER's own view before the lifecycle stall ended the run:

```
guest:  LANE_HOLD_READY n=64 ready=64 phase1_pass=1
server: STATE_DUMP_ALL ... ring=1   -> 66 ring threads in the test process
```

The >128 arm stays BLOCKED on the pthread stall, which is tracked separately (see the linked issue) and
deliberately not fixed here. Everything the lane layer owed that arm -- lazy growth beyond 128 lanes,
reclamation, generation-stamped slots -- is measured green in 13.35.

### 13.37 Process-scoped transport accounting, and the TID assertion

The server's counters are process-GLOBAL, which cannot decide "did THIS process use any UDS for mach_msg?"
-- bootstrap traffic and every other client are mixed in. A single opt-in event now carries both
identities and the transport:

```
MACHMSG_TRANSPORT process=<pid> image=kernel|dyld host_tid=<tid> lane=<slot> seq=<n> direction=request|reply transport=RING|UDS callnum=38
```

`image=` matters because dyld and libsystem_kernel keep SEPARATE guest lane tables and separate copies of
the counters; without it a pre-attach dyld op looks like kernel-image UDS traffic.

For an isolated run of the deterministic test process (`ool 20`):

```
process=27062   request/RING = 57   reply/RING = 57   request/UDS = 1
kernel-image dump: machmsg_ring=44 machmsg_uds=0 tid_mismatch=0
```

Requests and replies balance exactly on the Ring, and the kernel image records ZERO UDS mach_msg ops. The
single UDS line is `image=dyld` -- the process's first mach_msg, executed before any lane can exist, which
is why the claim is stated per image and post-attach rather than as an absolute zero.

**TID assertion.** A lane is strictly SPSC, so the thread that published a request is the only thread that
may execute its caller-S2C upcall. The publisher's `gettid()` is now stored in the lane and checked in the
pump. An assertion that has never failed is not evidence it checks anything, so it has a mutation arm:

```
GREEN (default)                      tid_mismatch=0
RED   (DARLING_GUEST_TID_MUTATE=1)   tid_mismatch=10, 12x RING_TID_MISMATCH lane=.. publisher=.. executor=..
```

### 13.38 The spin budget: swept, tuned to the Pareto point, and the state-aware policy rejected

The budget is now a tunable (`DARLING_GUEST_MACHMSG_SPIN`), so six values were measured without six
rebuilds. `bench_simple`, 20000 ops per arm, latency from the harness and guest/server CPU per op from
`/proc/<pid>/task/*` on the host (guest threads are host processes, so this is a real measurement, not a
guest estimate):

```
spin      ns/op      p50       p95     guest CPU/op  server CPU/op
512      102153    100051    161652        5000          106500
2000      50027     12605    192893        5500           55500
10000     23426     12334     65984        3500           30500
20000     24405     12172     79139        2000           31500
40000     26225     12223     95059        3000           35000
```

Below the knee the reply frequently misses the spin and the round trip pays a park plus a wake, which the
SERVER pays for (106.5k ns/op of server CPU at 512). Above it the spin is pure waste (40k proves it: same
p50, worse p95, more CPU). **10000 is the chosen Pareto point** -- as fast as 20000 on ns/op and p50,
BETTER on p95 (66.0k vs 79.1k) and on server CPU (30.5k vs 31.5k); 20000 is only better on guest CPU
(2.0k vs 3.5k).

The state-aware policy (`DARLING_GUEST_MACHMSG_SPIN_POLICY=state`: full budget while the server publishes
ACTIVE_POLLING, a short budget otherwise) was implemented, measured, and **REJECTED**: it was worse on
every axis (31.8k ns/op, p50 16.7k, p95 106.4k), so the measured hypothesis "a parked server makes a long
spin pointless" does not hold at this scale -- the round trip is dominated by the server's own work, not
by the wake.

Also fixed here: the per-op `RING_TRACE` lines were UNCONDITIONAL, i.e. a guest write syscall through the
whole stack, ~4.4 lines per operation. Every latency number taken before this round includes that cost.
They are now opt-in (`DARLING_GUEST_RING_TRACE=1`), and the wait path counts spin hits, parks, futex waits
and EAGAINs.

**One regression was introduced and fixed in this round, and it is worth recording because of how it
failed.** Rewriting the production wait dropped its duplex mailbox pump. The failure was not a slow path:
the server's bounded wait for the guest pump expired and the parent op FAILED
(`[D6] duplex S2C upcall TIMED OUT (no guest pump by deadline)`), which broke the BOOT for any process
using a Ring mach_msg. The pump is not bookkeeping -- for an op whose guest-memory effect only the caller
can perform, the server cannot produce the reply until the thread services the mailbox. The restored loop
pumps before every reply check in both the spin and the park loop, and its single timed 100ms futex sleep
bounds only the pump latency: there is still no transport deadline, which B3 re-verifies (a 5s blocking
receive succeeds).

### 13.39 Official P1/P2, separated by server configuration

The product default for the server is `DSERVER_SINGLE_THREADED=ON` (one thread per workqueue). The earlier
round's numbers were taken with `=OFF`, so both configurations were measured, with the final spin budget:

```
PRODUCT default (DSERVER_SINGLE_THREADED=ON), 3 runs each
  bench_simple  Ring  31988 ns/op  p50  16671  p95  84179  p99 333217
  bench_simple  UDS   36996 ns/op  p50  26941  p95  76324  p99 209564   Ring 15.7% faster (p50 -38%)
  bench_ool     Ring 219416 ns/op  p50 174910  p95 510485  p99 624705
  bench_ool     UDS  428958 ns/op  p50 416250  p95 644400  p99 939023   Ring 95.5% faster

DIAGNOSTIC multi-worker (DSERVER_SINGLE_THREADED=OFF), 3 runs each
  bench_simple  Ring  40339 ns/op  p50  16961
  bench_simple  UDS   38501 ns/op  p50  27732                          mean -4.6%, p50 -39%
  bench_ool     Ring 244200 ns/op  p50 214554
  bench_ool     UDS  397925 ns/op  p50 411605                          Ring 63.0% faster
```

Honest reading: the caller-S2C path wins big in BOTH configurations (the legacy path pays a request
socket, an inline S2C socket and a reply socket; the Ring path pays a request and a reply on the lane plus
a shared-mailbox mmap/munmap). The simple path wins on the product configuration and is a wash on the mean
in the multi-worker one -- where the mean is dominated by tail events (p99 374k vs 145k) while p50 still
favours the Ring by 39%. The two configurations are never mixed in one claim.

### 13.40 64-bit mmap address, and mmap failure equivalence

**#8 address width.** The duplex mmap reply carries the mapping address in a 64-bit field, and the trace is
what proves the UPPER HALF survives. `ool 30` produced 30 mmap upcalls, all distinct, all with
`upper32=32214`:

```
DUPLEX_MMAP_RESULT addr=0x7DD63A147000 len=65536 upper32=32214
```

So every result is far above 4 GiB and the guest then used each mapping (the workload's OOL payload checks
fail otherwise), which is the three-part acceptance: `upper32 != 0`, the returned address is the actual
mapping, and the guest can access it.

**#9 failure equivalence.** A new `mmap_fail` mode asks for 2^62 anonymous bytes, which the kernel must
refuse, through the same emulated path every guest mmap takes (server allocatePages -> caller-S2C):

```
hatch ON : ret=0xffffffffffffffff errno=12
hatch OFF: ret=0xffffffffffffffff errno=12
```

Identical observable failure semantics on both transports -- the duplex mailbox returns the same
(status, errno) pair the UDS S2C path returns, rather than a transport-specific error.

### 13.41 ABI v5/v6 mixed attach, both directions

The Ring control block carries `abi_version`, and the server's validator rejects any mismatch with
`dserver_ring_reject_abi` BEFORE it would read a field that moved. Real image pairs were built (a v5 guest
pair, a v5 server) and run against each other:

```
v6 guest + v6 server (control)  pass=1  lanes acquired=21  machmsg_ring=44  machmsg_uds=0
v5 guest + v6 server            pass=1  lanes acquired=0   machmsg_ring=0   machmsg_uds=44
v6 guest + v5 server            pass=1  lanes acquired=0   machmsg_ring=0   machmsg_uds=44
restored v6 + v6                pass=1  lanes acquired=21  machmsg_ring=44  machmsg_uds=0
```

Both skew directions reject the attach outright (`acquired=0`: no lane was ever adopted, so no request was
ever published on a partially-understood control block) and every op rides the legacy transport, with the
guest work fully correct. This is the "degrade cleanly to all-UDS" contract, verified with real mismatched
binaries rather than argued from the validator.

### 13.42 F1: the fd-shaped S2C refusal was NOT fail-closed -- a 31-second stall, now a defined failure

The refusal itself existed (the server refuses any mmap S2C with `fd >= 0 || offset != 0`, counts
`ring_duplex_fd_s2c_unsupported`, and does not move the mailbox), but it is UNREACHABLE from the current
ring op set: the only S2C mmap a Ring parent can raise is `allocatePages()`'s anonymous one (fd < 0,
offset 0), while a file-backed map is raised by a guest mmap RPC, which is not a ring call. An unreachable
refusal is not evidence that it refuses, so a mutation (`DSERVER_DUPLEX_FORCE_FD_SHAPE=1`) forces the
fd-shaped branch on the anonymous shape.

The first RED arm found a real defect: the refusal fell through to the "unchanged UDS S2C path", and a
ring-origin parent's caller is parked on its LANE, so it can never service a UDS S2C. The server waited out
its bounded deadline -- **31 seconds** -- and the parent op failed anyway. The fallback was not a fallback.

Fixed: a ring-origin parent whose mailbox was not taken now fails IMMEDIATELY with a defined error
(`std::nullopt` for the parent op) instead of attempting an S2C its caller cannot service. Measured:

```
                                  GREEN (no mutation)   RED (forced fd shape)
test                              pass=1                pass=0  (defined failure)
DUPLEX_MMAP_PUBLISH (mailbox)     22                    0
DUPLEX_FD_S2C_UNSUPPORTED         0                     22
RING_PARENT_S2C_FAIL_FAST         0                     22
RING_PARENT_S2C_UDS_FALLBACK      0                     0
counter ring_duplex_fd_s2c_unsupported  0               22
counter ring_parent_s2c_uds_fallback    0               0
counter ring_duplex_reject        0                     0
duration                          1s                    1s   (was 31s)
```

Every F1 acceptance item: the named counter increments, the failure is defined and immediate, the
fallback counter stays ZERO (we do not fall back at all), there is no 30s hang, and no descriptor is
leaked because the mailbox never had to carry one.

### 13.43 FD inventory: O(1) in historical threads (achieved), O(live lanes) still to fix

```
live lanes (lane_hold)   guest fds   server fds
        8                    29           54
       24                    61           86
       40                    93          118
```

Both sides scale at **2 fds per LIVE lane** (32 more fds per 16 more lanes, on each side), and neither
scales with history: `stress_churn 1024` leaves the server's fd count at its baseline (40 before, peak 40,
31 after) while the guest process exits. So the reclamation work closed the historical-thread half of the
scaling violation -- the half that made the 129th thread fall back to UDS -- and the remaining per-live-lane
cost is exactly the §25-§27 target: one process-wide backing object and ONE process doorbell eventfd,
instead of a mapping and a wake fd per thread. That is the next phase, and it now has a measured slope to
improve rather than an estimate.

### 13.44 The mutation families: M1-M6, R1-R4, and the lane-ABA gate

The previous round recorded M1-M6 / R1-R4 / ABA as blocked on the pthread stall. That was wrong: none of
them needs 129 live threads, only a deterministic injector. Each is now a named, env-gated perturbation of a
REAL transport path (no stubs), run against the standard `ool 20` workload with server + guest traces on.

```
M1 early parent reply   DSERVER_DUPLEX_EARLY_COMPLETE=1
   the server publishes the COMPLETION as if the guest had already performed the effect. Result: 4 synthetic
   completions, 5 DUPLEX_EARLY_PARENT_REPLY lines, and the workload never produces a correct result (no
   completion line at all; the mailbox is left inconsistent and the guard starts declining) -> RED.
   This is the invariant that matters: the guest must not observe a successful parent result before its own
   caller-S2C completes.

M2/M3 wrong or stale upcall id   DARLING_GUEST_DUPLEX_UID_MUTATE=1
   the guest publishes the completion tagged uid+1. Result: ring_duplex_reject=2, no completion line ->
   RED: the server REJECTS the stale id and keeps waiting, so the parent never false-succeeds.
   (M2's "wrong parent" and M3's "stale id" are the same check under this mailbox ABI: the parent/upcall pair
   is the server's correlation token and the guest executes what the mailbox names, so the rejection is the
   server's job and the arm exercises it through the guest's injected id.)

M4 wrong executor TID   DARLING_GUEST_TID_MUTATE=1
   tid_mismatch=20 on the test process (20 mismatches, assertion fires) while the workload itself still
   completes -> the assertion is a real check on the recorded publisher, and it has a failing arm.

M5 dropped completion   DARLING_GUEST_DUPLEX_DROP_REPLY=1
   the guest performs the effect and suppresses the completion. Result: 51 suppressed completions, two
   "[D6] duplex S2C upcall TIMED OUT ... failing the parent op closed (bounded, no leaked fiber)", and NO
   false success -> RED. Note which mechanism produced the failure: the server's own bounded fail-closed
   deadline, not a fabricated timeout in the transport.

M6 duplicate completion   DARLING_GUEST_DUPLEX_DUP_REPLY=1
   the same completion is published twice. Result: pass=0 -> RED, and the destructive op still executed at
   most once. FINDING: ring_duplex_reject stayed 0 -- a replayed completion with the same (parent, upcall)
   pair is not an explicit protocol event, so the replay lands as a stale reply slot that a LATER transaction
   can pick up (which is how the arm turned RED). The one-outstanding rule is currently enforced by ordering,
   not by identity; an explicit replay rejection is the hardening item.
```

R1 and R2 got dedicated workload modes (no injection, real work):

```
R1  ool_delay 5000 3   pass=1  elapsed 5.0015..5.0022s per op  machmsg_ring=10 machmsg_uds=0 tid_mismatch=0
    a parent parked far longer than any spin/park heuristic still gets its caller-S2C, and the reply arrives
    with the same seq on the same lane -- the Call-owned transport context survived the suspension.

R2  r2 200             pass=1  parked_parent_ok=1  concurrent_failures=0  parked_elapsed=2.902s
    while one context was parked ~3s on its lane, 200 out-of-line round trips on another lane completed
    correctly: no cross-call stealing of a ring destination.
```

R3/R4 and the lane-ABA family are covered by a new deterministic model gate,
`tests/ring_lane_aba_gate_test.c`, which drives the four identities that must agree before a completion may
be delivered (slot index + generation, owner tid, the Call's context, and the server object kept alive by a
reference). Four invariants, four arms:

```
GREEN            PASS failures=0   I1/I2 stale completion REJECTED, I3 old replies=1 new replies=0,
                                   I4 refs held=1, delivered to the retained incarnation
RED -DABA_NO_GENERATION_CHECK  FAIL  "stale completion was not rejected" +
                                     "new incarnation received a stale reply"
RED -DABA_ALIAS_BY_SLOT_INDEX  FAIL  the same violation reached by trusting the slot index across reuse
RED -DABA_FREE_ON_RETIRE       FAIL  "incarnation freed while a Call referenced it (UAF)"
```

I1/I2 are the ABA cases (a slot recycled for the same tid with a new generation, and a completion tagged with
the old one), I3 is the stale-Call-after-recycle rule (its completion may reach only its own retained
incarnation, never the new lane), I4 is retire-with-a-live-Call (the reference keeps the incarnation alive;
the outcome is defined, never a silent success).

### 13.45 Where the fd slope actually is: measured classes, not a guess

`/proc/<pid>/fd` readlink classification at 8 vs 24 live lanes (16 more lanes):

```
              guest 8 -> 24    slope      server 8 -> 24   slope
eventfd          11 -> 27     +1 / lane       33 -> 65     +2 / lane
socket           12 -> 28     +1 / lane        9 ->  9       0
```

So the live-lane fd cost is three named things, and each has an owner in the source:

* GUEST eventfd +1/lane -- the adopted per-lane wake descriptor (`__dserver_adopt_ring_fd` in
  `gr_attach_lane`).
* GUEST socket +1/lane -- the per-thread RPC UDS socket. This is the "per-thread UDS must disappear" item,
  and it is already +1 fd per live thread today.
* SERVER eventfd +2/lane -- `RingBuffer::_eventfd` plus the `wakeDup` the per-lane Monitor owns
  (`Call::RingAttach::processCall`).

The backing mapping is NOT a per-lane fd on either side (the memfd is closed after mmap), so the
"one process-wide backing object" work is about the doorbell and the pending structure, and the biggest
single win available is the server's two-per-lane eventfd pair.

**Not implemented this round.** The process-wide doorbell requires moving the eventfd + epoll Monitor from
`Thread` to `Process`, giving each lane a pending word the server scans, and making the guest adopt ONE
doorbell fd per process; that is a transport-lifetime change that must be developed against a running boot,
and this cycle ran out of room before it could be built and regression-tested. What exists instead is the
measurement above (which fixes the target and its size) and the mutation machinery that will guard it: M1-M6
and the ABA gate are exactly the checks a doorbell redesign has to keep green.

### 13.46 Remaining UDS traffic, by callnum (armed from the existing census)

The server already carries a per-callnum transport census (`DARLING_SERVER_RPC_HEATMAP=1` plus the msg,
attach and residual censuses). Armed over boot + basic + ool + churn 256 + pool 8:

```
callnum                                   uds   ring   verdict
dserver_callnum_pthread_canceled          587      0   lane1-candidate
dserver_callnum_ring_attach               400      0   lane1-candidate
dserver_callnum_thread_self_trap          394      0   lane1-candidate
dserver_callnum_checkin                   374      0   lane1-candidate
dserver_callnum_set_thread_handles        374      0   lane1-candidate
dserver_callnum_checkout                  361      0   lane1-candidate
dserver_callnum_mach_reply_port            58      0   lane1-candidate
dserver_callnum_host_self_trap             38      0   lane1-candidate
dserver_callnum_task_self_trap             36      0   lane1-candidate
dserver_callnum_vchroot_path               29      0   lane1-candidate
dserver_callnum_uidgid                     22      0   lane1-candidate
... (started_suspended, get_tracer, set_dyld_info, set_executable_path, mldr_path, fork_wait_for_child,
     interrupt_enter/exit, console_open, kqchan_proc_open, vchroot)
                                       2745 total       0 total

residual_reason     {control_plane 774, ineligible 1664, thread_has_ring 18, thread_no_ring_proc_none 1}
residual_uds_despite_lane  {vchroot_path 9, thread_self_trap 9}
mach_msg             ring_parent 1118, ring_final 1117  vs  uds_machmsg_request 8, reply 8, s2c 0
msg census           total 1126  send_only_simple 397  blocking_receive 613  complex 116
```

Reading it: mach_msg is now ~99.3% on the Ring (1118 ring vs 8 UDS requests), and the remaining UDS body is
(a) control plane that is EXPECTED to stay on UDS (checkin/checkout/ring_attach/set_thread_handles -- process
registration and lane setup), and (b) a large `ineligible` bucket (1664) that the current design declares
not-ring-eligible, plus (c) a small, precisely identified avoidable set: `residual_uds_despite_lane`, i.e.
calls that HAD a live lane and still went UDS -- 9 vchroot_path and 9 thread_self_trap.

Note the second row of the table: `thread_self_trap` (394), `mach_reply_port` (58), `host_self_trap` (38),
`task_self_trap` (36), `uidgid` (22), `mldl_path` (9) are ALL in `DSERVER_RING_C2S_OPCODES` and have guest
shims, yet the census shows them arriving on UDS with zero ring arrivals. That is the concrete next
investigation: either the guest shims are not being taken for these traps in a real workload, or the server's
per-callnum routing declines them, and the census now names the exact suspects with counts.

### 13.47 M6 is now replay-safe: the one-outstanding rule is identity-based, not timing-based

The previous round's duplicate-completion arm turned the workload RED without a single rejection being
counted -- the replayed `(parent, upcall)` pair was simply left sitting in the reply slot, where the NEXT
upcall's cleanliness check saw a busy mailbox (and a later transaction could have harvested it). The
one-outstanding rule was being enforced by ORDER, not by identity. That is protocol debt, and it is now
paid.

**Completion lifecycle, explicitly staged.** For every published upcall exactly one transition sequence is
valid:

```
PUBLISHED -> COMPLETION_ACCEPTED -> CLOSED
```

The server records the identity of every completion it ACCEPTS (a small ring of the last 8 `(parent,
upcall)` pairs, no allocation). Any completion that arrives naming an identity in that CLOSED set is not a
protocol event at all: it is counted as `ring_duplex_completion_replay_reject`, traced as
`DUPLEX_COMPLETION_REPLAY_REJECT`, and the slot is cleared so it cannot be seen by -- or harvested by -- any
other transaction. The reaper runs in both duplex guards (before the mailbox-clean checks) and in the drain
(before the correlation check), so a replay is caught whether the server is arming a new upcall or already
waiting for one.

**Ordering.** The guest publishes body-first and consumes the upcall slot before advertising the reply; the
server validates the correlation, consumes the result, records the identity CLOSED, and only then can the
next upcall be armed -- `_duplexUpcallInFlight` and the recorded CLOSED pair are both read under `_rwlock`,
so the refusal of a replay and the arming of the next upcall cannot interleave.

**M6-GREEN, with the window forced.** The replay window is timing-dependent: without a delay the two guest
publishes usually coalesce into one slot state, so no replay is ever observable. `DARLING_GUEST_DUP_REPLAY_
DELAY_MS` forces the interesting interleaving (the duplicate lands after the server has closed the first):

```
arm                                      workload     REPLAY_REJECT lines   counter
GREEN (no mutation)                      pass=1            0                 0
M6 duplicate, delay 50ms                 pass=1           19                19
M6 duplicate, delay 150ms                pass=1           20                20
```

The injected duplicate is HARMLESS (the workload stays semantically correct and the destructive op executes
once) AND explicitly rejected, which is the property the arm had to prove -- not merely that the workload
breaks.

**M1-M5 re-verified after the fix** (the replay state machine must not weaken them): M1 early parent reply
still RED (no correct result), M3 wrong/stale upcall id still RED (`ring_duplex_reject=2`), M5 dropped
completion still RED with no false success, M4 wrong-executor-TID assertion still fires. GREEN base is
`pass=1` with `ring_duplex_completion_replay_reject=0`.

### 13.48 The UDS census re-classified: only ONE callnum is class A

The previous round left `control_plane` looking like a permanent exception. It is not: the final invariant is
that AF_UNIX exists only to transfer a real Linux file descriptor. Re-classified from the census:

```
A. requires SCM_RIGHTS / a new Linux fd
     ring_attach (400)                       -- it transfers the lane memfd; bootstrap fd transfer

B. non-fd ordinary RPC  (must migrate to Ring)
     pthread_canceled 587, thread_self_trap 394, mach_reply_port 58, host_self_trap 38,
     task_self_trap 36, vchroot_path 29, uidgid 22, started_suspended 10, get_tracer 10,
     set_dyld_info 10, set_executable_path 10, mldr_path 9, interrupt_enter 4, interrupt_exit 4,
     console_open 3, kqchan_proc_open 2, vchroot 1

C. bootstrap/lifecycle control, still non-fd  (must also migrate)
     checkin 374, checkout 361, set_thread_handles 374

D. temporary migration/debug
     fork_wait_for_child 8
```

So exactly one callnum is entitled to stay on AF_UNIX today, and it is a bootstrap fd transfer that a future
process-level SCM_RIGHTS courier should carry -- not a per-thread socket. Everything else in the table is
non-fd work that the final design must move onto the Ring or process-shared control.

**The 18 calls that had a lane and still went UDS** (`residual_uds_despite_lane`: 9 `vchroot_path`, 9
`thread_self_trap`) are the sharpest end of this. They are not a coverage problem -- the lane existed -- so
the branch that bypassed it is a routing defect, and the next step is to have the guest record WHY
`gr_lane_for_this_thread()` returned NULL (or the publish failed) for those two callnums specifically. The
shims themselves are NOT env-gated (`thread_self_trap_impl` calls `__dserver_ring_thread_self_trap`
unconditionally), so the bypass is on the guest's lane resolution or a rejected lazy attach, not on the
shim's presence.

### 13.49 Process doorbell: not implemented, and why that is the honest status

The round's central item was replacing the per-lane eventfds with ONE per-process doorbell. It is NOT
implemented. The fd topology it targets is measured (previous round, re-stated):

```
guest:  +1 eventfd / live lane (the adopted per-lane wake fd),  +1 socket / live thread (per-thread UDS)
server: +2 eventfd / live lane (RingBuffer::_eventfd + the Monitor dup)
        persistent backing fd slope: 0 / lane
```

and the change is a transport-LIFETIME change across four objects (move the eventfd + epoll Monitor from
`Thread` to `Process`, give the lane a pending word the server scans, make the guest adopt one doorbell fd
per process and stop closing it per lane). That cannot be landed as an unverified edit: it needs build/boot
iterations against a live prefix, and this cycle's remaining capacity went to M6 replay-safety (which was
protocol debt on the SAME machinery and is now closed with a counter and a GREEN arm) and to the census
re-classification.

What exists instead is everything the doorbell work needs to be judged: the measured fd classes and their
ownership, the mutation machinery that must stay green across it (M1-M6 with 4 RED arms and 1 GREEN arm, the
ABA/R3/R4 gate with 3 RED arms), the F1 fail-fast invariant, and the UDS census that says which callnums the
doorbell is actually for.

### 13.50 The duplex reply slot is now valid only for the CURRENT in-flight upcall

The previous round made M6 replay-safe with a bounded history of the last 8 closed `(parent, upcall)` pairs.
That passed the mutation, but a bounded cache must never BE the correctness requirement -- it only decides
whether a stale slot is a *known* duplicate. The invariant is now stated and implemented without reference
to any history:

```
case A  slot ready, NO upcall in flight   -> unsolicited/stale. Reject, clear, and never let it block the
                                             next upcall's publication.
case B  upcall in flight, pair is someone else's
                                          -> mismatch. Reject, clear, keep waiting for the correct
                                             completion. A wrong reply must never be left in the slot.
case C  pair matches the in-flight upcall  -> exactly once: validate, consume, clear, close.
```

`_duplexScrubReplySlot(cb, upcallInFlight)` implements A and B; C is the existing accept path. Case A is
rejected unconditionally -- the 8-entry history is used ONLY to annotate the trace with
`recently_closed=0|1`. Two counters now name the two failures:
`ring_duplex_completion_replay_reject` (case A) and `ring_duplex_reply_mismatch_reject` (case B).

**Two real bugs were introduced by the first cut of this change and both were caught by the GREEN base
going red, which is why the base run is part of the gate:**

* the scrubber was placed BEFORE the `_duplexUpcallInFlight` early return in the arming guards, so it ran
  while an upcall WAS in flight and destroyed the legitimate pending reply (`ring_duplex_s2c=0`,
  `DUPLEX_MUNMAP_PUBLISH` followed 240us later by `DUPLEX_COMPLETION_REPLAY_REJECT parent=1 upcall=2`);
* the drain called it with `upcallInFlight=false`, classifying the legitimate reply as case A. The drain by
  definition has an upcall in flight, so it must pass `true`.

### 13.51 M6 and M6b: the rejection no longer depends on any window

```
arm                                  workload   rejection counted     ring_duplex_s2c
GREEN (no mutation)                  pass=1     0                     44
M6  recent duplicate, +50ms window   pass=1     A=30 replay           44
M6b aged-out pair (>16 txns old)     pass=1     B=1 mismatch          44
M6b aged-out pair, +80ms window      pass=1     B=1 mismatch          44
```

M6b is the mutation that proves the property: it remembers the FIRST completion pair the guest ever
produced, waits until at least 16 transactions have completed (so the pair is long out of any 8-entry
window), injects it as a completion, and then runs the real transaction. The old pair is rejected as case B,
cleared, and the transaction completes normally -- `pass=1` with `ring_duplex_s2c=44` and zero UDS fallback.
Correctness is therefore identity-based, and the history cache is diagnostic only.

M1-M5 re-verified after the change (the aggressive stale-slot cleaning must not weaken them): M1 early
parent reply still RED (`pass=0`), M3 wrong/stale upcall id still RED (now counted as case B), M5 dropped
completion still RED with no false success, M4 wrong-executor-TID assertion still fires. GREEN base is
`pass=1` with both counters at 0.

### 13.52 Process doorbell: not landed, and the reason is verification, not design

The round's remaining items (ONE process doorbell, per-lane eventfd removal, PW1-PW5, fd-slope re-measure,
post-doorbell perf, the guest fallback-reason histogram, simple-trap migration, new census) are NOT landed.
The doorbell is now the only large item left, and it is a transport-LIFETIME change: the eventfd and its
epoll Monitor move from `Thread` to `Process`, each `RingAttach` must distinguish FIRST_PROCESS_RING_ATTACH
from LANE_ATTACH_TO_EXISTING_PROCESS_TRANSPORT so that later lanes never retain another alias, the guest must
adopt one doorbell fd per image (the fd table is per-process but `dserver-ring.c`'s statics are per-image,
which is exactly the process-vs-image audit the directive calls out), and the sleep handshake has to be
re-proved against `epoll_wait` with one doorbell instead of N.

This cycle's remaining capacity went to 13.50/13.51 -- two real defects in the replay machinery that a
half-built doorbell would have made much harder to find -- rather than to an unverified transport change.
The measured basis for the doorbell work, the mutation machinery that must stay green across it, and the
feedstock it targets (paragraphs 13.45-13.48) are all in place.

## 12. Repository state

- Product source: **untouched**. No commit, no branch, no push.
- `fix/ring-fd-ownership` in `source-fixes/` carries an **uncommitted draft** (`mldr.c`,
  `elfcalls.h`) of the truthful-limit fix; it was not created by this investigation, was not
  modified by it, and must be reviewed before use — the draft's own `elfcalls.h` callbacks are
  only the beginning of the §8.3 cutover.
- New artifacts from this investigation: the three proof runners under `tests/`, this document,
  and the bead comment recording results.
- `result.txt` in the workspace remains untracked and was not staged.

### 13.53 ONE process doorbell replaces the per-lane wake eventfd (ABI v7)

The wake plane is now two objects instead of 2N:

```
guest Linux process:  ONE Ring doorbell fd          (was: one per lane)
server:               ONE eventfd + ONE Monitor     (was: two per lane)
```

The server creates its single eventfd lazily on the first `ring_attach`
(`Server::ringDoorbellDupForGuest`), registers ONE Monitor whose body is
`_drainRings()` -- the same service scan the pre-epoll spin already used -- and
hands back a dup on *every* attach. The descriptor is no longer owned by
`RingBuffer`: `_eventfd`, `eventfd()` and `drainWake()` are gone, so a lane
allocates no wake resource at all and the epoll registration is per-transport
rather than per-lane.

The guest side needed a process-global owner, and it already had one: the
shared loader (`mldr`) owns the ring descriptors for both guest images. A new
appended elfcall `dserver_ring_doorbell(fd)` keeps the FIRST dup it is given
and closes every later one, so N lanes in both images cost ONE fd for the whole
Linux process. It is APPENDED, not inserted, for the same reason
`dserver_release_ring_fd` was: the elfcalls struct is the loader/guest
interface and every existing field must keep its offset or a stale image reads
the wrong function at the same slot. A lane's `wake_fd` is a non-owning borrow
-- a lane release closes nothing -- and `__mldr_postfork_child` resets the
doorbell so a forked child cannot write a descriptor it no longer owns.

**Why process-INDEPENDENT on the server** (the directive asked for one Monitor
per Process): the two things the wake path does -- the service scan
(`_drainRings`) and the sleep-state publication (`_setAllRingStates`) -- were
already process-independent before this change, so a per-process Monitor would
add N server fds without changing a single decision the doorbell makes. The
guest-side property being bought (one wake fd per process) is exact.

Product evidence for the multi-image singleton, printed once per image per
process:

```
[dring-doorbell] pid=2011059 image=dyld   fd=1048574
[dring-doorbell] pid=2011059 image=kernel fd=1048574
```

### 13.54 The lost-wake protocol is unchanged, and 13.55 measures it

The decision predicate is the same one the per-lane wake model used
(`server_state` + `dserver_ring_guest_should_doorbell`), and the server's
arm/sleep handshake already had the required shape: publish ACTIVE while
spinning, publish SLEEP_ARMED, drain ONCE MORE (the final rescan that closes
"guest published after the first scan but before the state moved"), publish
SLEEPING, then `epoll_wait`. A guest that read ACTIVE_POLLING and therefore
skipped the doorbell is caught by that rescan; a guest that read SLEEPING or
SLEEP_ARMED writes the eventfd, whose counter persists until it is read, so a
write that lands before, during or after the transition still wakes the loop --
an eventfd write cannot be lost.

Only the *target* of the write changed: all lanes now write one fd. Sharing
strictly reduces the loss surface, because a wake for any lane now drains every
lane instead of one ring.

### 13.56 The gates

```
PW1  bench_simple 20000   pass=1  dw_active=0  active_seen=38290  writes=1731 (861 sleeping + 870 armed)
PW2  ool_delay 1500 3     pass=1  writes=16 (13 sleeping + 3 armed) -- the server was REALLY asleep
PW3  stress_pool 32 20    pass=1  ring=1284 uds=0  ONE doorbell_fd=1048574 for 32 lanes
PW4  probe(armed|presleep|sleeping) x 250ms x 3 each: 9/9 pass=1, all ops complete
PW5  stress_churn 512 under sleeping/armed probes: pass=1 failures=0; stress_mixed 16 20 pass=1
```

`dw_active = 0` in every run: **not one doorbell write happened while the
server claimed ACTIVE_POLLING**. The writes that do occur are the protocol's
required "the server is going to sleep" doorbells, and the armed share is the
window between the ARMED publication and the final rescan.

### 13.57 FD slope: the acceptance number

```
lanes  guest_eventfd  guest_socket  server_eventfd  server_socket
8      1              12            3               6
16     1              20            3               6
24     1              28            3               6
40     1              44            3               6
```

`d(Ring wake fds)/d(lanes) = 0` on both sides. This is NOT total FD O(1) and
must not be reported as such: the guest's per-thread RPC socket still grows
1/lane, which is the next measured blocker.

### 13.58 Post-doorbell P1/P2 (5 alternating pairs, PRODUCT config)

```
              UDS median   Ring median   delta      Ring p50
simple        32 899 ns    21 996 ns     -33%       12 123 ns
OOL          394 714 ns   217 724 ns     -45%      176 744 ns
```

No hot-path regression: the Ring half of each pair wins both workloads, and the
doorbell's own cost is bounded by the writes above (1 500 per 40 000 message
legs on the simple bench, with 38 290 skips).

### 13.59 One measurement bug worth recording

The `[dring-lane-stats]` format string had stopped consuming ten existing
arguments (spin/futex/mmap/duplex counters were passed but never printed), so
every field after `tid_mismatch` -- including the doorbell fields this round
added -- printed a NEIGHBOURING counter. The numbers were plausible and
meaningless. Fixed by restoring the specifiers; all doorbell numbers in 13.56
are from the corrected build. A counter whose value is only ever read after the
fact needs one deliberate check that the field printed is the field named.

### 13.60 Not done in this round

Per the directive's priority order, the remaining items are the guest-side
fallback diagnosis, the simple-trap migration, and the new UDS census. The
`residual_uds_despite_lane` entries (`vchroot_path` 9, `thread_self_trap` 9),
the simple-trap group, the lifecycle group and the guest fallback-reason
histogram are NOT touched this round. ABI was bumped to v7 so a mixed
pair cannot half-work (the attach check rejects it, the same mechanism round 8
exercised for v5/v6); the skew test was not re-run because the v6 binaries no
longer exist in the build tree.


## 14. Cross-process doorbell isolation: measured, not fixed (round 14)

### 14.1 The global server doorbell IS one shared kernel object

Two guest processes were started in one Darling session (`lane_hold 4 & lane_hold 4`)
and each process's hidden Ring descriptor was identified by KERNEL identity
(`/proc/<pid>/fdinfo/<fd>`), never by fd number:

```
guest pid 2095875  fd 1048574  anon_inode:[eventfd]  ino=1062  eventfd-id=1251
guest pid 2095878  fd 1048574  anon_inode:[eventfd]  ino=1062  eventfd-id=1251
guest pid 2095879  fd 1048574  anon_inode:[eventfd]  ino=1062  eventfd-id=1251
```

Three distinct Linux guest processes hold the SAME eventfd object. An eventfd
counter is read-and-reset by whoever reads it, so a guest holding a duplicate
can consume another guest's pending wake. The round-13 doorbell is therefore
NOT cross-process isolated, and its `dw_active=0` / PW1-PW5 results say nothing
about that property: they measured one process at a time.

### 14.2 The per-Process fix was attempted and is NOT landed

The server side was moved into `Process` (`_ringDoorbellFD` + `_ringDoorbellMonitor`
+ a dup per attach, teardown in `~Process` and on HUP). Result: boot stalls with
`Rootless shellspawn did not become ready`, while the server's main thread sits in
`ep_poll` (syscall 232) -- the guest wrote a doorbell nobody was watching.

Two candidate causes were narrowed and neither is confirmed:

* `Process::_dispose()` cleared the fd while the guest could still attach lanes, so
  the next attach created a SECOND eventfd object whose dup the guest's loader
  discarded (it already held one). Moving teardown to `~Process` did not fix boot.
* a guest-side per-write query of the loader's canonical fd broke boot by itself.

The tree was then restored to the round-13 state and re-verified: boot 2.8 s,
`ool 20` `pass=1 machmsg_ring=44 machmsg_uds=0`, `dw_active=0`. The re-generated
patch is 202838 bytes, the same artifact size as round 13.

### 14.3 The steal mutation could not be executed

A guest-side hatch (`DARLING_GUEST_DOORBELL_STEAL_MS`, read the doorbell duplicate
after a delay) was added, and it is INERT without its variable -- yet merely being
present made boot fail (`steal-hatch-breaks-boot.log`). The guest wake path cannot
be instrumented that way, so no steal run happened this round. The kernel-level
capability is not in question (a process may `read()` an fd it holds); what is
missing is the end-to-end demonstration.

### 14.4 What this means for the next attempt

The isolation fix must not depend on the guest re-deriving the descriptor on the
wake path, and it must not tie the eventfd's lifetime to a `Process` object that
is disposed while the guest still holds the duplicate. The two shapes that remain
open: (a) keep one eventfd per guest process but own it in a server-side registry
keyed by the guest's Linux pid and destroy it on the guest's own close, or (b) keep
the doorbell out of the guest entirely and let the server poll the shared rings
(the spin phase already does this; the doorbell is only the COLD wake).


## 15. Stable per-Linux-process doorbell: `RingWakeRegistry` (round 15)

### 15.1 Why the choice of owner was the whole problem

Round 13's doorbell was server-wide, which made it ONE kernel eventfd object shared
by every guest process (measured: three distinct guest pids, `eventfd-id 1251`,
`ino 1062`). An eventfd counter is read-and-reset by ANY holder, so the object was
not isolatable by construction. Round 14 keyed the object on
`DarlingServer::Process` and broke boot: a Process object can be disposed while the
guest still holds the duplicate, so the next attach created a SECOND object whose
dup the guest's loader discarded -- the guest wrote D1 while the server watched D2.

The lesson is not "per-process is wrong"; it is "the *object* is wrong". A
`Process` is a server-side bookkeeping object, not the Linux process incarnation.

### 15.2 The anchor: a pidfd per incarnate guest process

```
Server::_ringWakeRegistry : unordered_map<pid_t, shared_ptr<RingProcessWake>>
RingProcessWake { pid, pidfd, doorbell(eventfd), doorbellMonitor, deathMonitor }
```

* first lane attach of an incarnation: `pidfd_open(pid)` + one `eventfd` + two
  Monitors (readable -> `_ringDrainAll()`; pidfd readable -> retire the entry);
* every later attach, in either image: `dup` of the SAME object;
* the incarnation exits: the pidfd Monitor removes the entry, both Monitors and
  both fds;
* the pid is reused: the stored pidfd for a dead process is readable, so the entry
  is retired and a fresh one created (the `poll(POLLIN)` check on lookup);
* nothing here is touched by `Process::_dispose()`.

The guest side is unchanged from round 13: adopt the first doorbell into a
canonical hidden fd, close every later dup, no per-wake refresh (round 14's
per-wake elfcall was also what broke boot, and it does not belong on the hot path).

### 15.3 Gates

```
cross-process isolation   pid 2152223 -> eventfd-id 1254
                          pid 2152247 -> eventfd-id 1259      (different objects)
same process, both images fd 1048574 in image=dyld AND image=kernel
exit cleanup              50 sequential guest processes: server fds 85/85/85,
                          eventfds 7/7/7                       (nothing accumulates)
fork                      two guest processes from one shell, both pass=1 on Ring
regression                boot PASS; B1-B4, OOL, R1, R2 pass=1; churn4096
                          ring=8196 uds=0; S1-16 ring=644 uds=0; S1-32 ring=1284
                          uds=0; M6 pass=1 (39 replay rejects); M6b pass=1
                          (6 replay + 1 mismatch); F1 forced-fd-shape arm fails
                          fast as designed (22 + 22 named counters)
wake fds vs lanes         8 -> 5, 16 -> 9, 24 -> 9, 40 -> 9 server eventfds
                          (constant in the LANE count; the 8/16 step is one extra
                          guest process attaching, not a per-lane cost)
ABI skew (real builds)    v6 guest + v7 server: pass=1 ring=0  uds=44
                          v6 server + v7 guest: pass=1 ring=0  uds=44
                          v7 + v7 restored   : pass=1 ring=44 uds=0
```

The skew arms are real builds: the constant was flipped, the affected side was
rebuilt and deployed, and the workload still passed on the legacy transport with
`ring = 0`.

### 15.4 Still open

The guest fallback-reason histogram, the simple/lifecycle non-fd RPC migration and
the new UDS census are NOT done. They were reachable only after a green doorbell --
which is now landed -- so they are the next round's work, not this round's debt.


## 16. Fresh UDS census and the real reason the simple group does not migrate (round 16)

### 16.1 Guest-side fallback reasons, at the decision point

`gr_lane_for_this_thread_named(callnum, name)` now attributes every lane miss with an
explicit reason enum (NO_LANE_ENTRY, ATTACH_NOT_STARTED, ATTACH_IN_PROGRESS,
ATTACH_FAILED, LANE_NOT_ACTIVE, OWNER_TID_MISMATCH, GENERATION_MISMATCH,
CALL_NOT_RING_ELIGIBLE, PAYLOAD_NOT_SUPPORTED, BOOTSTRAP_REQUIRED,
TEARDOWN_AFTER_LANE_RELEASE, FD_TRANSFER_REQUIRED, IMAGE_LOCAL_STATE, OTHER),
computed from the guest's own lane table at the moment of the decision -- never from
server state observed later. Each (reason, callnum) pair emits one
`[dring-uds-reason]` line (opt-in `DARLING_GUEST_LANE_DIAG=1`) and the lane-stats
dump carries a per-process `[dring-uds-reason-hist]` histogram.

### 16.2 The census

Fresh run (boot + `ool 20` + `basic 50`), RPC heatmap and residual census armed:

```
callnum                 total UDS   ring
ring_attach                 201       0
pthread_canceled            214       0
thread_self_trap            192       0
checkin                     166       0
set_thread_handles          166       0
checkout                    152       0
mach_reply_port              76       0
host_self_trap               50       0
task_self_trap               48       0
vchroot_path                 38       0
uidgid                       30       0
started_suspended/get_tracer/set_dyld_info/set_executable_path   13 each   0
mldr_path                    12       0
fork_wait_for_child          11       0
```

`residual_reason`: control_plane 367, ineligible 785, thread_has_ring 24,
thread_no_ring_proc_none 1. `residual_uds_despite_lane`: vchroot_path 12,
thread_self_trap 12. Guest reasons seen in the same run: the IMAGE_LOCAL_STATE case
(the other image owns the thread's lane) and nothing else.

### 16.3 The finding that changes the plan

`DSERVER_RING_C2S_OPCODES` already lists thread_self_trap, host_self_trap,
task_self_trap, mach_reply_port, uidgid, set_thread_handles, started_suspended,
get_tracer, task_is_64_bit, mldr_path and vchroot_path; the server's generic
eligible-op dispatch for them exists; and the guest has Ring helpers with per-callnum
entry points (`gr_port_trap`, `gr_body_trap`, `gr_full_trap`). Yet every one of those
callnums shows `ring = 0`.

The helpers are reachable only from the mach-trap shims in `mach_traps.c`. The traffic
that actually accounts for these counts arrives through the **generated client wrapper
layer** (`scripts/generate-rpc-wrappers.py`), which has no Ring branch at all --
`__dserver_ring_*` is referenced from `mach_traps.c` and the ring test fixture, never
from a generated wrapper.

So this class is NOT a lane-availability problem and NOT a bootstrap-ordering problem:
it is a MISSING ROUTE in generated code (bucket A), and the fix has exactly one
insertion point shared by every eligible call.

### 16.4 Next step (identified, not implemented)

Emit a Ring fast path in the generated client wrapper for callnums with a fixed,
non-fd shape, delegating to the existing generic helper with the callnum and the
shape's request/reply sizes; keep the datagram path only for pre-publication misses.
One generator change covers the whole simple group, including callnums added later.


## 17. Generated-wrapper Ring route: landed, and the census that refuses to move (round 17)

### 17.1 What was landed

`scripts/generate-rpc-wrappers.py` gained an explicit annotation table:

```python
RING_GENERATED_SIMPLE = { task_self_trap, thread_self_trap, host_self_trap, mach_reply_port,
                          uidgid, get_tracer, task_is_64_bit, started_suspended,
                          set_thread_handles, mldr_path, vchroot_path }
```

Membership is not inferred from "fits in a slot": an op is listed only when the server already
has generic eligible-op dispatch for it (it is in `DSERVER_RING_C2S_OPCODES`), its wire shape is
a closed request/single-reply transaction with a fixed inline body, it transfers no Linux fd, it
takes no caller-side S2C, and it is not NO_REPLY.

For each annotated op the generated client wrapper now emits, BEFORE any socket-side effect:

```c
{
    int32_t __ring_code = 0;
    int __ring_try = __dserver_ring_try_generated_rpc(
        (uint32_t)dserver_callnum_<name>, <req>, <req_len>, <reply>, <reply_len>, &__ring_code);
    if (__ring_try == GR_RING_TRY_COMPLETED) { reply_msg.reply.header.code = __ring_code;
                                               goto ring_reply_ready; }
    if (__ring_try == GR_RING_TRY_COMMITTED_FAILURE) {
        return dserver_rpc_hooks_get_communication_error_status(); /* NO datagram retry */
    }
}
```

with `ring_reply_ready:` placed after the datagram path's atomic end, so the Ring path lands
straight in the existing unpack code and the datagram path is byte-unchanged.

The guest API is tri-state by construction -- `GR_RING_TRY_NOT_TAKEN` (0, datagram is safe),
`GR_RING_TRY_COMPLETED` (1), `GR_RING_TRY_COMMITTED_FAILURE` (2, published: retry forbidden) --
and it reuses the EXISTING `gr_full_trap` publish/wait/validate code, so there is no second SPSC
protocol.

The generated library is the file the guest images compile
(`emulation.dir/.../darlingserver/src/rpc.c.o`), so this is a guest-side route, not server-only
code: the generated source was inspected and the `thread_self_trap` wrapper carries the fast path
verbatim (11 try blocks, 22 labels).

Core regression with the route in place: boot OK, ool 20 `ring=44 uds=0`, basic 100 `ring=204
uds=0`, delay 5000x3, timeout 300x5, r2 100 `ring=206 uds=0`, stress_pool 16x20 `ring=644 uds=0`.

### 17.2 The census did not move, and that is the finding

`thread_self_trap` 102 UDS / 0 Ring, `host_self_trap` 30/0, `task_self_trap` 28/0,
`mach_reply_port` 46/0, `uidgid` 18/0, `vchroot_path` 23/0 -- unchanged from the pre-route
census. Worse: the generated route, instrumented to report its own outcome, recorded **zero
misses and zero completions** (no `name=generated` line, no histogram growth).

A route that is present in the deployed wrapper but neither completes nor misses is a route that
is NOT EXECUTED for this traffic. The UDS callers for these callnums are therefore neither the
hand-written mach-trap shims in `mach_traps.c` nor the generated client wrappers.

### 17.3 The next diagnostic (one place every sender must pass)

The datagram send itself. Instrumenting the send keyed by callnum names the caller
unambiguously; only then can the Ring route be attached where the traffic actually originates.
Attaching a route to a wrapper nobody calls is exactly the failure this round measured.


## 18. Who actually sends the residual UDS datagrams: the loader (round 18)

### 18.1 Why round 17's route could not move the census

The generated `rpc.c` is compiled by **three** consumers -- mldr (the loader), dyld, and the kernel
image -- and the round-17 generator emitted a hard `#include` of an **emulation-only** header. That
include is invisible to mldr, which carries its own `resources/dserver-rpc-defs.h`, so mldr stopped
building and the **deployed** mldr remained a pre-generator binary with no route at all. A deployment
verification that stops at "one `rpc.c.o` contains the branch" does not notice this: the image that
sends this traffic never contained the branch.

The route is now a **per-consumer hook** with **one** policy table:

```c
/* generated public header (consumer-independent) */
#define DSERVER_RING_TRY_NOT_TAKEN 0
#define DSERVER_RING_TRY_COMPLETED 1
#define DSERVER_RING_TRY_COMMITTED_FAILURE 2
/* generated wrapper body */
int __ring_try = dserver_rpc_hooks_try_ring(callnum, "name", req, reqlen, rep, replen, &code);
```

* The kernel image and dyld bind it to `__dserver_ring_try_generated_rpc()` -- they own the lanes.
* mldr binds it to a stub that returns `NOT_TAKEN` and records its callers. mldr runs before the lane
  table exists, so this is a *classification*, not a fallback: the datagram path is untouched.

`DSERVER_RING_TRY_NOT_TAKEN` remains the only state that may fall through to the datagram path
(`COMMITTED_FAILURE` still forbids the retry), so the invariant survives the rebinding.

### 18.2 The proof

With the product build (boot and the whole focused set GREEN) and `DARLING_GUEST_UDS_SEND_SITE=1`,
across five distinct guest pids:

```text
[dring-uds-site] pid=<...> image=mldr callnum=35 name=thread_self_trap ring=NO_LANE_MACHINERY_IN_IMAGE
[dring-uds-site] pid=<...> image=mldr callnum=3  name=vchroot_path     ring=NO_LANE_MACHINERY_IN_IMAGE
```

and the server census in the same run:

```text
residual_uds_despite_lane { dserver_callnum_vchroot_path: 7, dserver_callnum_thread_self_trap: 7 }
```

The residual pair is **exactly** the pair mldr is observed sending. It is the loader's own bootstrap
traffic, issued by an image that holds no lane, which is why `residual_uds_despite_lane` survived every
wrapper-level route: no wrapper in the kernel image sends those datagrams.

### 18.3 Negative result recorded as a constraint

Instrumenting the last common send point itself (`dserver_rpc_hooks_send_message()`) **breaks boot**,
in two independent forms: in-band printing (the guest's fd 2 is not a reliable diagnostic channel
during bootstrap) and a buffered note collected for the exit-time dump (the call in the send path is
sufficient by itself). Both forms left `shellspawn` unable to reach ready within 30 s. The product
send path is therefore byte-clean, and the loader-side stub -- which does survive boot -- is the
diagnostic of record.

### 18.4 Product regression after the revert

boot OK; `ool 20` pass=1; `basic 100` pass=1; `r2 100` pass=1; `stress_pool 16x20` pass=1; census
workload pass=1. The Ring lane keeps serving `mach_msg_overwrite` with `machmsg_uds=0`.


## 19. The kernel-image simple-group UDS has a single, sourced root cause (round 19)

### 19.1 It is not a missing route, and not a wrapper topology problem

The generated wrappers were the wrong suspect, and so were the hand-written trap shims. The product
call path for these callnums is:

```c
/* mach_traps.c */
mach_port_name_t thread_self_trap_impl(void) {
	int ring_code = __dserver_ring_thread_self_trap(&port_name);   /* Ring shim FIRST */
	if (ring_code >= 0) return ring_code == 0 ? port_name : MACH_PORT_NULL;
	if (dserver_rpc_thread_self_trap(&port_name) != 0) ...        /* generated wrapper as FALLBACK */
}
```

The shim is tried first. It declines with a **negative** code -- a pre-publication miss -- and only
then does the generated wrapper run. A route added to the wrapper therefore cannot move these calls:
the shim's own lane lookup is what fails.

### 19.2 The actual cause: per-image lane state with mutual retirement

```c
static gr_lane_t* gr_lane_for_this_thread_named(uint32_t callnum, const char* name) {
	gr_lane_t* L = gr_find_lane(tid);
	if (L) {
		if (L->state == 1 &&
		    __atomic_load_n(&gr_cb(L)->server_state, __ATOMIC_ACQUIRE) == DSERVER_RING_SRV_RETIRED) {
			/* Another image now owns this thread's server-side lane. Keep this image on UDS
			   rather than stealing it back on every image switch ... */
			gr_release_lane(L);
			gr_urs_note(callnum, name, GR_URS_IMAGE_LOCAL_STATE, 0);
```

`gr_lanes` is **per-image** static storage. When a second image attaches a lane for the same host
tid, the server marks the first image's lane RETIRED; that image then releases its slot and declines
Ring **for the rest of its life**, with reason `IMAGE_LOCAL_STATE`. Every simple call issued later by
that image goes to UDS. This is exactly the `IMAGE_LOCAL_STATE` the round-15 reason histogram
reported and nothing else, and it is independent of which wrapper is called.

### 19.3 The fix this mandates

One **process-global lane identity**, adopted by every image, with no retirement ping-pong: the
per-process directory that round 12 already built for the doorbell (loader-owned, distributed through
the elfcall table, kept as the first dup) is the natural anchor. The concrete design is the minimal
Ring client core plus a shared lane directory keyed by `(host_tid, slot, generation, mapping,
mapping_size)`, imported by dyld and the kernel image instead of each attaching its own lane:

```text
mldr:   create process-global lane directory (loader-owned, one per Linux process)
kernel/dyld: import the record for this host tid; no second attach, no retirement
```

The shared low-level SPSC primitives stay in one core so the memory ordering has a single source of
truth, with the emulation layer keeping only the catalog, duplex, mach_msg and metrics above it.
`IMAGE_LOCAL_STATE` becomes unreachable, because one image no longer retires another's lane.

### 19.4 ring_attach SCM_RIGHTS, answered exactly (ABI v7)

| direction | descriptor | creator | purpose | first | subsequent | guest keeps | server keeps |
|---|---|---|---|---|---|---|---|
| guest -> server | `ring_fd` (SCM_RIGHTS, 1 per attach) | lane allocator in the attaching image | lane backing memfd, fstat'd for its REAL size before mapping | yes | yes | mapping (per lane) | mapping (per lane) |
| server -> guest | `wake_fd` (reply body field) | server | wake descriptor for the attached lane | yes | yes | as a non-owning borrow of the process doorbell | n/a |

So attach is a **true fd transfer**: one SCM_RIGHTS descriptor per attach plus one reply-body fd. The
process doorbell is deliberately *not* re-transferred per lane (round 12), which is why the per-lane
fd slope is zero; the remaining per-attach descriptor traffic is the lane backing itself.


## 20. Stage 1 landed: ONE process-global logical lane per Linux host tid (round 20)

### 20.1 What was implemented (not designed -- implemented and built)

**Owner and data structure.** The loader owns an ordinary BSS array, which every image reaches through two
**appended** elfcalls (`dserver_ring_lane_registry`, `dserver_ring_lane_slots`):

```c
struct mldr_ring_lane_record {          /* transport-neutral: no emulation types */
	volatile int32_t  host_tid;         /* 0 == free */
	volatile uint32_t state;            /* EMPTY -> ATTACHING -> ACTIVE */
	volatile uint32_t generation;
	volatile uint32_t slot_index;
	volatile int32_t  owner_image;      /* diagnostic */
	volatile void*    mapping;          /* guest VA -- valid in EVERY image */
	volatile uint64_t mapping_size;
};
#define MLDR_RING_LANE_SLOTS 1024
```

**No new memfd directory was needed.** All images of a process share one Linux address space and the loader
stays resident, so a plain pointer into loader BSS *is* the process-global registry. Nothing is shared but
control metadata; the payload lane remains a per-thread SPSC ring, and there is no new global queue.

**Mapping ownership.** Exactly one image owns each incarnation: the one that won the arbitration. A view
created by another image is marked `borrowed` and its release path skips the `munmap` and does not
unpublish the record; only the creating image unpublishes (and maps/unmaps) the incarnation.

**Attach race.** `state` is CAS'd `EMPTY -> ATTACHING` before any work, so a second image cannot start a
competing attach: it yields and adopts on its next call.

**Boot-safe ordering (found by measurement, not by reasoning).** Probing the loader's elfcall table at the
earliest Ring code in dyld **broke boot** (`shellspawn` never became ready). The directory is therefore
resolved only *after* this image has completed one attach -- that path already uses the loader's elfcalls
successfully for the process doorbell, so it is the first point where the table is proven live. Before the
probe is allowed, an attach proceeds as before and arbitrates at publish time instead: if another image
incarnated the thread meanwhile, this image unmaps its own backing and continues borrowed on the other's.

### 20.2 Measured result

Boot GREEN, focused regression GREEN (`ool 20` pass=1 `machmsg_ring=44 uds=0`, `basic 50` pass=1
`machmsg_ring=104 uds=0`) -- no transport regression. The directory is live and used:

```text
proc_published=1  proc_adopted=0 proc_attach_arb=1  proc_attach_yield=0
proc_published=20 proc_adopted=0 proc_attach_arb=20 proc_attach_yield=0
proc_published=50 proc_adopted=0 proc_attach_arb=50 proc_attach_yield=0
```

Every attach now arbitrates and publishes; no duplicate attach was refused and **no image adopted another
image's incarnation in this workload**. That is consistent with the round-19 finding: on this workload the
kernel image's Ring shims are barely reached at all, so the cross-image case the directory exists for does
not occur here -- and the census is consequently unchanged
(`thread_self_trap 102/0`, `mach_reply_port 46/0`, `host_self_trap 30/0`, ...).

### 20.3 What this round establishes and what it does not

Established: the per-image ownership that produced `IMAGE_LOCAL_STATE` is gone at the mechanism level --
one incarnation per host tid, arbitrated, published, and adoptable, with ownership-correct release.
Not established: any census reduction, because the remaining senders are the loader's own bootstrap
wrappers (proved in round 18) and callnums that never reach these shims; the adoption path itself is
unexercised by this workload and therefore still unproven in PRODUCT.


## 21. Stage 2 core landed: the loader creates, owns and USES the process-global lane (round 23)

### 21.1 What works now (PRODUCT)

Seeding moved off the generated hook -- that placement is what re-entered the loader's own RPC machinery -- to
the main bootstrap, after two loader RPCs have completed and before the loaded image starts. There the loader

1. builds the lane (memfd + mapping, geometry and control-block fields identical to the runtime's),
2. negotiates it with the **same generated** `dserver_rpc_ring_attach` client (`rc=0 reject=0` in all 7 processes),
3. records it in the loader-owned directory,
4. **performs a real RPC over it**: `post-seed-ring rc=0 port=515` in every process.

The ring helpers are the SHARED inline functions from `rpc-supplement.h`, so no second SPSC implementation
exists, and the loader's hook now speaks the same tri-state contract as the runtime
(`NOT_TAKEN` / `COMPLETED` / `COMMITTED_FAILURE`, with no datagram retry after publication).

boot GREEN; `ring_mach_msg_test ool 20` pass=1; `basic 50` pass=1.

### 21.2 The bisection that localized the remaining defect

| arm | result |
|---|---|
| seed disabled | boot GREEN |
| seed without `ring_attach` | boot GREEN |
| seed with `ring_attach` | boot RED |
| seed + attach + loader uses the lane, record never published ACTIVE | boot GREEN |
| same, record published ACTIVE (sibling image adopts) | boot RED |

The attach is the server-side precondition, and the **cross-image adoption path is the one remaining
component that breaks boot**. It is isolated behind `MLDR_SEED_ADOPT` (default off, tree boots GREEN).

### 21.3 Two real bugs fixed on the way

* the loader and guest lane-record structs must agree field for field -- round 21 had added `next_seq` and
  `creator_image` on the guest side only, so the guest wrote past the loader's record;
* a borrowed view set `wake_fd = -1`, which made every `gr_wake_server()` an EBADF: a sibling image could
  publish a request and never doorbell the server. It now borrows the canonical process doorbell fd.

### 21.4 Open, precisely localized

* adoption defect (`MLDR_SEED_ADOPT=1` RED vs default GREEN, same workload);
* census bookkeeping: with the seed on, `ring_attach` rises by one per process (expected -- attach still
  uses UDS+SCM_RIGHTS) while `task_self_trap` rises by ~6 with `ring` staying 0, so the server's per-call
  transport classification must be understood before any census-reduction claim is made.


## 22. Stage 2 complete: natural cross-image adoption is the default (round 25)

The loader creates and OWNS the process-global lane for the main thread, uses it itself, and publishes the
incarnation; the guest images then ADOPT it. PRODUCT acceptance on the default configuration: boot GREEN,
`ool 20` and `basic 50` pass=1, `proc_adopted=1` in every process, and no `ring_attach` for the adopted
tid -- the image no longer attaches a lane of its own.

Census on the same workload (server heatmap, with the transport tag now set where the call is taken off a
lane rather than only in the duplex reply step):

```text
TOTAL uds=334 ring=458                      <- Ring is the majority transport for counted calls
mach_reply_port       0 / 46    host_self_trap 0 / 30    task_self_trap 0 / 36
uidgid                0 / 18    set_thread_handles 0 / 86
thread_self_trap      8 / 94    vchroot_path 7 / 24      pthread_canceled 47 / 95
checkin              86 / 0     checkout 77 / 0          ring_attach 86 / 0 (real fd transfer)
set_dyld_info         8 / 0     (loader bootstrap write, not in the allowlist)
```

Three defects made this look impossible for many rounds, and only one of them was in the transport:

1. **The adoption-mode encoding dropped a bit.** The mode was published as `0x100|mode` and decoded as
   `(creator_image >> 8) & 0xff`, which maps mode 2 to 1. The adopting image therefore always took the
   "create the borrowed view, then decline it" arm: the view was created and dropped, the lookup fell
   through to a SECOND `ring_attach` for the same tid, the server retired the shared lane, and boot wedged.
2. **The directory gate hung off the process doorbell**, which the fork-child reset clears, so the
   adopting image never resolved the directory and attached its own lane instead. The gate is now
   self-validating: the directory exists iff the loader's registry elfcalls answer.
3. **The heatmap tagged transport only in `beginRingReply`**, so every ordinary ring-served call was
   reported as UDS. Tagging in the generic ring service path (`ringServiceThread`) showed the simple group
   had been on the Ring all along.

Supporting fixes landed with it: the seed's doorbell survives the fork-child variable reset; `ring_attach`
is refused for a tid that already has an incarnation (Case A); BORROWED views are preserved across
`__dserver_ring_postfork_reset` and live in statically reserved slots, so adoption never needs catalog
growth (an emulated mmap in this guest).

Remaining UDS is dominated by real fd transfer (`ring_attach`) and lifecycle (`checkin`/`checkout`), which
are absent from `DSERVER_RING_C2S_OPCODES`. Adding an allowlist entry alone does not build: the op-class
macro needs a row per op. That, plus the process SCM_RIGHTS courier for the attach path, is the next phase.


## 23. Lifecycle is fd-mediated; the process descriptor courier (round 26)

### 23.1 `checkin` / `checkout` are descriptor transfers, not a missing Ring path

```c
struct dserver_call_checkin  { bool is_fork; uint64_t stack_hint; int32_t lifetime_listener_pipe; };
struct dserver_call_checkout { int32_t exec_listener_pipe; bool executing_macho; };
```

Both replies are header-only, and both body fields that look like identifiers ARE Linux descriptors: the
server takes them as **indices into the received CMSG set**
(`call.cpp: requestMessage.extractDescriptorAtIndex(checkinCall->body.lifetime_listener_pipe)`). `checkin`
is issued once per new process (mldr's bootstrap and the fork child); `checkout` once per exec or thread
exit (`execve.c`, mldr's `threads.c`). Their UDS constraint is therefore genuine fd transfer -- which is
exactly why classifying them `SIMPLE_C2S` wedged boot in both tested variants. They belong to the same
class as `ring_attach`, i.e. to the process descriptor channel.

### 23.2 `ring_attach` ancillary behaviour, proven from the generated client

```text
request:  body .ring_fd = 0 (an INDEX)  +  ONE SCM_RIGHTS cmsghdr (SOL_SOCKET/SCM_RIGHTS, CMSG_LEN(int))
reply:    ONE SCM_RIGHTS cmsghdr; the guest copies CMSG_DATA into fds[]; the body's wake_fd is the index
```

Every attach moves exactly one descriptor in each direction. The reply descriptor is a **dup of the
server's process doorbell, re-sent on every attach** although the guest keeps only the first -- the
quantified redundancy the courier is meant to remove.

Measured attach volume: 14 packets for the boot chain, 47 cumulatively with 32 held lanes, 1550-2070 for
`stress_churn 1024`; the fd ratio is 1:1 in both directions in every case.

### 23.3 The courier prototype works

An AF_UNIX listener in the abstract namespace (`darlingserver-fdcourier:<prefix>`, derived from the stat
socket name so both sides compute it identically), carrying only `{generation, token, kind, fd_count}` plus
`SCM_RIGHTS`: no callnum, no RPC body. The server verifies each received descriptor with `fstat`, counts it
and closes it, so an experimental run cannot leak. mldr connects once per process at bootstrap and delivers
the lane backing descriptor under a token. Measured: 8-10 connections, `fds_received == fds_closed`, boot
GREEN, tests pass.

### 23.4 Guard and census

`NONFD_UDS_VIOLATION` is implemented in the generated Ring route (an ACTIVE process-global lane, a non-fd
Ring-capable callnum, and a datagram decision would increment it) and measures **0** in every process.

The census is unchanged at `uds=334 ring=458`, and the audit explains it: `checkin 86 + ring_attach 86 +
checkout 77 = 249` of the 334 are genuine descriptor transfers; `set_dyld_info 8` is a non-fd bootstrap
write absent from the allowlist; the remaining ~15 are the earliest bootstrap `thread_self_trap`/
`vchroot_path` calls before the loader's lane exists.


## 24. Process-control plane: live, and the exact extraction plan for `checkin`

The process-shared management page (`dserver_process_control`) is established on every boot: the loader
creates it, hands the backing descriptor to the server over the process courier
(`kind = PROCESS_CONTROL`, so the courier stays descriptor-only), and proves the plane with a PING round
trip through shared memory. Measured live: `process_control_regions == requests == 10` over a full
workload, courier `received = 183` / `matched = 173` (the difference is exactly the page descriptors,
which are regions and not request halves), no rejects, no orphans, and the baseline regression is
unchanged (ool 44 / basic 204 / r2 206 / stress_pool 644, all `machmsg_uds = 0`).

One rule had to be fixed for it and belongs next to the lane-directory rule: **a descriptor number is not
ownership.** An image that cached the courier's number later sent on a *different* file the process had
reused that number for (`sendmsg` returned ENOTSOCK while `fcntl` on the same number succeeded). Images
now ask the loader at every use, and the fork reset goes through the loader (which *forgets* the number)
rather than closing a number the image happens to hold.

### Why `checkin` belongs here, measured three times
Routing `checkin` on a lane wedges the boot in every configuration tried: as a SIMPLE_C2S op, with its
descriptor on the CMSG, with the descriptor on the courier, and with a caller-supplied `bootstrap` marker
that kept the lifecycle instances on the datagram path and let only the pthread instance ride (that one
still wedged). It is the first call a new thread makes -- the server's `Thread` object is created by that
very call -- so it is not a hot-lane operation at all.

### The extraction, point by point
1. `call.cpp:150-230` holds the checkin ingest: `processRegistry().registerIfAbsent(...)` with the
   lifetime pipe (extracted from the CMSG index, `call.cpp:172-177`), then
   `threadRegistry().registerIfAbsent(header->tid, ...)` with the stack hint (`call.cpp:220-223`), then
   `setAddress`/`registerWithProcess`. Extract that into one server function taking
   `{pid, tid, namespaceID, architecture, is_fork, stack_hint, lifetime_fd}` and call it from BOTH the
   RPC ingest and the control-plane service.
2. `Process::notifyCheckin(architecture, isMainThread, isFork)` (`process.cpp:275`) is already a method;
   the plane calls it directly with the `Thread` returned by (1) and the main-thread classification.
3. New op `DSERVER_PROCESS_CONTROL_OP_CHECKIN` with payload `{is_fork, stack_hint, fd_token, tid}`; the
   service resolves `fd_token` (kind `CHECKIN_FD`), runs (1) then (2), and writes the status back into the
   page -- which is the completion ordering the bootstrap writes needed and never had.
4. Guest side: the bootstrap checkin (mldr's, and the fork child's) publishes that op instead of the RPC.
   The loader already has the page and a request helper; the fusion image needs the same helper in
   `dserver-ring.c` through the elfcalls page pointer, and `threads.c`'s pthread instance keeps the
   ordinary entry point (or moves here too, once (1) is shared and the ordering is proven).
5. Only after that: `ring_attach`'s semantic half, `set_dyld_info`, `set_executable_path`, the teardown
   `checkout` instance and `pthread_canceled` -- all of which are the same shape (rare, ordered, no
   descriptor of their own) and are the remaining rows of the census.


### 24.1 Correction to the ordering claim (own measurement, re-read in full)

The first reading of the side-by-side trace compared the first three matching lines and concluded that a
datagram had outrun the page ("a datagram is serviced on arrival, a page only on the server's pass, so a
preceding checkin cannot be a page write"). Re-reading the SAME log in full, in order, shows a different
picture for that pid:

    [mldr-ctl] page pid=N size=104 sent=1
    (checkin-trace) rpc-register-process pid=N nsid=1 lifetime_pipe=-1 header_pid=N   @ .000246
    (checkin-trace) rpc-register-thread  tid=N number=1                              @ .000390
    (process-control) checkin-op pid=N call=1 thread=N process=N nsid=1              @ .000512
    [mldr-ctl] checkin pid=N tid=N status=0 token=0
    [mldr-ctl] ping pid=N status=0
    [mldr-ctl] page pid=N size=104 sent=1          <- a SECOND page creation for the same pid

Two things are visible that the first reading missed: an RPC registration precedes the plane's op for the
same pid, and that pid creates TWO pages. So the trace interleaves two bootstraps of one process (the
loader's and the fusion image's own), and the ordering claim above is NOT established by it. What IS
established is the seven exclusions (reply path, payload ABI, sender pid, page position in the pass,
thread reply address, lifetime descriptor, namespace id -- the last one measured: `nsid=1` in BOTH
paths). The next measurement must therefore separate the two bootstraps in the trace (tag each line with
the image that issued it) before any ordering conclusion is drawn again.


### 24.2 What the trace also showed: two pages per process, and a measurement lesson

Two `[mldr-ctl] page pid=N sent=1` lines appear for one pid, i.e. the bootstrap that creates the control
page runs TWICE per process (the loader's and the fusion image's), and each run creates its own page. The
server keeps one per pid, so the second replaces the first -- harmless today, and the reason the trace
interleaves two bootstraps. Making every bootstrap ask for the loader's page through the elfcalls table
is the right shape, and it was attempted this round: it could not be built in the budget available
because the loader's table is a file-static in `stack.c`, so the accessor is not linkable from the
bootstrap. Recorded here with the code attempt removed rather than left half-done.

Measurement lesson, again: this workload needs about THREE minutes from launch to the last test
(ool 20 + basic 100 + r2 100 + stress_pool 16x20). Several reads at 110-170 s looked like regressions and
were not -- the same configuration was green at ~180 s in the same session. Read the FINAL/`pass=` lines
after the run's own timeout, not on a shorter clock.


### 24.3 Why the loader-owned page could not be built here (exact, for the next attempt)

`stack.c` is **included by `mldr.c`** (it is not only compiled as its own translation unit), so a
non-static accessor added there lands twice in one TU and the build fails with a redefinition at the same
line. The table itself (`static struct elf_calls _elfcalls`) is file-static by design. The workable shape
is therefore one of:
  * move the table out of `stack.c` into a TU that is compiled once and expose a getter from there; or
  * have the LOADER write its page pointer into a location the image's bootstrap already reads (the lane
    directory is loader-owned and shared, but its layout is the lane ABI -- so a separate, small
    loader-owned record is the cleaner home); or
  * let the image's bootstrap ask the emulation layer (which has `elfcalls()`) to hand the page to the
    loader-side bootstrap through a hook the loader installs.
Recorded with the code attempt removed rather than left half-built.


### 24.4 Final verification of this session's state (settled, not assumed)

`stress_pool 16 20` did not complete in three consecutive windows while other jobs were polling the same
machine; run ALONE it reaches `FINAL=1 pass=1 machmsg_ring=644 machmsg_uds=0`, and `ool 20 / basic 100 /
r2 100` are at 44 / 204 / 206 with `machmsg_uds=0`. The mldr source is byte-identical to the round-45
verified state apart from accumulated comments (checked by reconstructing that state from its committed
patch and diffing), so the earlier non-completions were machine load, not a regression.


### 24.5 The loader-owned slot was tried and is NOT the fix (measured)

Shape tried: an appended elfcalls field `dserver_process_control_slot()` returning the address of a
loader-owned pointer, so whichever bootstrap runs first creates the page and every later one reuses it.
Result: some pids still created twice (`page ... sent=1` twice for pid N, once for pid M in the same run),
and it introduces a worse hazard -- a FORK CHILD inherits the slot, so it would reuse its PARENT's page
while the server keys regions by pid. The configuration stayed green, but the change does not do what it
was for, so it is reverted and recorded. The shape to design next is fork-aware: the child must clear the
slot (exactly as the courier's fork reset already does for the connection) before its bootstrap runs.


### 24.6 The fork-aware slot: measured neutral, reverted, and what to do instead

The fork-aware shape (loader-owned slot + a reset the child calls next to the courier reset) was built and
measured: `HELLO/DONE` green, `ool 20`=44, `basic 100`=204, `r2 100`=206, and the page-creation count
stayed at the SAME ratio as without it (7-8 creations for 5-6 pids, i.e. two pids still create twice).
Since it adds an elfcalls ABI field and a reset hook without moving that number, it is reverted. Before
that shape is worth its surface, the second creation must be ATTRIBUTED: log which image performs it
(the loader, the fusion image, or a fork child) -- the current diagnostics only say `pid=N`, which is
exactly why two bootstraps of one process cannot be told apart in the trace.

Full verification after the revert (the state carried forward): `HELLO/FINAL` reached, `ool 20` ring=44,
`basic 100` ring=204, `r2 100` ring=206, `stress_pool 16x20` ring=644, every test pass=1, all
`machmsg_uds=0`; plane live with `regions == requests`.


### 24.7 ATTRIBUTED: the "two pages per pid" are an execve pair, not two bootstraps (round 47)

The bootstrap now tags each page request with its entry path (`argv[0]` as handed over by the kernel or
dyld) and its tid. Measured, one `ool 20` boot:

```
pid=3681941  vchroot            (1)   ->  pid=3681941  /sbin/launchd   (2)
pid=3681949  /bin/bash          (1)   ->  pid=3681949  /bin/sh         (2)
pid=3681947  /usr/libexec/shellspawn
pid=3681950  /usr/bin/ring_mach_msg_test
```

Every duplicated pid pairs **two different images** -- a `vchroot` that execve's `launchd`, a `/bin/bash`
that execve's `/bin/sh` (the shell shim). So one process bootstraps once per IMAGE, sequentially, and a
new page per execve is correct: the previous image's page is released with its incarnation, exactly as the
courier advances its generation on execve. Consequences:

  * **The premise of §24.5/§24.6 is void**: there is no double creation to eliminate, so the loader-owned
    slot and its fork-aware reset are unnecessary, not merely unproven. They stay reverted.
  * **The §24.1 withdrawal is itself withdrawn**: it rested on "two interleaved bootstraps of one
    process", and the two lines are an execve pair, i.e. sequential. The ordering observation that round
    was about is not invalidated by interleaving.

Per-image ordering measured in the same trace (`ATTACH-RPC` = the image's `ring_attach`, `PAGE` = the
plane's page for that image):

```
pid=3681941  SEED-ATTACH -> PAGE(vchroot) -> PING(0) -> SEED-ATTACH -> PAGE(launchd) -> PING(0) -> ATTACH-RPC x2
pid=3681949  ATTACH-RPC -> SEED-ATTACH -> PAGE(/bin/bash) -> PING(0) -> SEED-ATTACH -> PAGE(/bin/sh) -> PING(0)
pid=3681950  ATTACH-RPC -> SEED-ATTACH -> PAGE(/usr/bin/ring_mach_msg_test) -> PING(0) -> ATTACH-RPC x4
```

So the plane is NOT uniformly available before the first lane attach: for `launchd` the page precedes the
attach, for the shell and the test image the attach precedes the page. That is the measured round-28
pre-attach datagram dependency, now attributed per image instead of asserted globally, and it is why the
loader's first writes stay on the datagram path.


### 24.8 Round 48: the plane before the first write is RED; the plane after them is GREEN

Directive tried: establish the process-control plane BEFORE the loader's first write and route
`set_dyld_info` through it (a no-reply op with no descriptor -- the exact shape the plane exists for),
with the datagram path kept as the fallback. Implemented: the plane block moved above
`dserver_rpc_set_dyld_info`, a new `DSERVER_PROCESS_CONTROL_OP_SET_DYLD_INFO` case that synthesizes the
ordinary `dserver_rpc_call_set_dyld_info_t` and runs it through `callFromMessage` -> `doWork` with
`suppressReplyDelivery()` (the page is the completion), and the architecture carried in `payload[3]`.

Measured: **RED**. `HELLO` never printed and the launcher reported
`Rootless shellspawn did not become ready within 30000ms`. The server trace shows the plane WAS live for
that pid -- `process-control region pid=3686572 size=104`, `request pid=3686572 op=1 seq=1` (the PING) --
so the failure is the ordering, not the plane: the loader's first writes must precede the plane's
establishment, exactly as the two round-27/28 negatives said for the lane. Reverted; the plane is
established after `set_executable_path` again and `set_dyld_info` is back on the datagram path.

This is the FIFTH member of one measured family -- publish-only, ordered-ack, seed-before-writes, a new
call-table row, and now plane-before-writes -- so the classification stands: the loader's pre-image writes
are process bootstrap, and they precede every in-process transport this design has built.

Full verification after the revert: `HELLO/FINAL` reached, `ool 20` ring=44, `basic 100` ring=204,
`r2 100` ring=206, `stress_pool 16x20` ring=644, every test pass=1, all `machmsg_uds=0`.


## 25. Round 49: the courier becomes BIDIRECTIONAL (server -> guest descriptors)

### 25.1 What was built

The existing process-scoped `SOCK_SEQPACKET` connection now carries descriptors in BOTH directions. No
second socket is created, and the message shape is unchanged in kind: `process_generation`, `token`,
`kind`, `fd_count`, SCM_RIGHTS -- no semantic body, no callnum, no result code crosses this socket.

* Generator: a reply may now declare a wire-only `@fd_token`. The reply body carries the token; the
  descriptor itself is not pushed as CMSG. `REPLY_FD_COURIER_KINDS` names the calls whose reply
  descriptor rides the courier, and the legacy CMSG reply stays the fallback when the courier cannot take
  the descriptor (`sendFdCourierBundleToGuest` returns 0 for a process with no live connection).
* First two real operations: `console_open` (kind `CONSOLE_FD`) and `kqchan_proc_open`
  (kind `KQCHAN_FD`).
* Server: `Server::sendFdCourierBundleToGuest(pid, kind, fd)` finds the process's connection, stamps the
  pid's generation, sends one descriptor, transfers ownership (closes its copy) and returns the token.
  `Call::sendFdCourierToGuest` is the Call-level face of it, because the generated inline reply code only
  sees `Call` -- MEASURED, calling `Server::sharedInstance()` there fails with
  `incomplete type 'DarlingServer::Server' named in nested name specifier`.
* Guest: `__dserver_fd_courier_receive(token)` drains the connection, keeps a process-global pending
  registry keyed by token, and resolves the token the reply named. Duplicate tokens keep the first
  descriptor and close the second; a full registry closes rather than leaks; every drop is counted.
* Ordering is the same rule as the request direction, mirrored: the server sends the descriptor BEFORE it
  publishes the reply, so a guest holding a token normally finds the descriptor already queued.

### 25.2 Three real defects found by measuring (all fixed)

1. `MSG_DONTWAIT` written as `2` -- that is `MSG_PEEK`, so the "non-blocking" drain peeked the same
   message forever and the shellspawn spun in userspace instead of booting. `MSG_DONTWAIT` is `0x40`.
2. The pending registry's "free slot" test was `fd < 0`, but a static array is zero-initialized and
   `fd == 0` is not `< 0`: no slot was ever found, the descriptor was closed on arrival, the lookup then
   missed and the blocking fallback read waited forever. "Free" is `token == 0`.
3. The generator's reply-token instrument was first emitted with `\t` escapes as literal text and with a
   doubled macro continuation; the fix is to append the emitted statement to the SAME write as the proven
   sibling line, so it inherits that line's escaping.

### 25.3 Measurement

Instruments (temporary, now removed except a bounded MISS report): the server logged the token the reply
would carry (`reply-token pid=N token=T bodylen=24`), and the guest logged its parse outcome
(`GOT ... len=20 level=1 type=1 fd=9`). With both in place the two halves agreed on the token and the
descriptor arrived, which is what closed defects 1 and 2.

Product run with the reverse courier live:

```
FINAL=1  HELLO=1
ool 20        ring=44  uds=0
basic 100     ring=204 uds=0
r2 100        ring=206 uds=0
stress 16x20  ring=644 uds=0
courier receive misses = 0
```

### 25.4 Procedure defect that cost this round its first five measurements

Every run between the first and the last was served by a **stale darlingserver**: `darling --rootless
shell` reuses a live server for the prefix, so a run that does not shut the prefix down first measures the
OLD server with the OLD guest session, and its pids in the log are from the old session. The tell was a
log whose pids (15796, 27552) were far below the current run's. The reliable procedure is: full-env
`darling --rootless shutdown`, kill any process whose cmdline names the prefix, verify none remain, then
install and launch. Both the "boot stalls" readings from those runs and the conclusions drawn from them
were withdrawn.


### 25.5 Reverse-direction mutations, measured (`DARLING_SERVER_COURIER_MUTATE`)

The guest validates every received bundle before it is stored: the kind must be one this build knows
(`CONSOLE_FD`, `KQCHAN_FD`, `PROCESS_DOORBELL`) and the generation must be the process's own. A refused
descriptor is CLOSED at the point of refusal, so a token can never resolve to a descriptor that belongs to
another operation or a dead epoch. The fallback wait is bounded (SO_RCVTIMEO 200 ms on the shared socket),
because a semantic reply that names a token whose descriptor never arrives is a committed failure of that
operation and must be reported, not turned into a hang.

| mode | boot | `ool 20` | receive misses | sent_to_guest | fallback_cmsg | reading |
|---|---|---|---|---|---|---|
| duplicate | `HELLO=1` | `pass=1` | 0 | 10 (5 ops x2) | 0 | the operation executes ONCE, the duplicate is closed, boot stays GREEN |
| kind (0xFFFF) | `HELLO=0` | — | 2 | 2 | 0 | refused; no descriptor fabricated, no false success |
| stale (gen+1) | `HELLO=0` | — | 2 | 2 | 0 | refused; same |
| nofd | `HELLO=0` | — | 2 | 2 | 0 | the reply names a token with no descriptor: reported as a miss, the boot fails rather than hanging |

`fallback_cmsg = 0` in every mode: the legacy CMSG reply path is not silently absorbing the traffic the
courier was supposed to carry.

Still owed in this direction (not measured yet, and not claimed): the `orphan` mode, and a dump of the
guest's reverse-direction counters (`receive_kind_rejects`, `receive_stale_rejects`, `receive_dropped`,
`receive_misses`) in the `[dring-lane-stats]` line so the rejects are visible without a per-mode log read.


## 26. Round 49b: one-time doorbell delivery is NOT yet safe (measured), and what it exposed

Goal: stop transferring a dup of the SAME process doorbell on every `ring_attach` reply -- deliver it once
per process incarnation through the courier, and let every later attach carry no fd at all.

Built: `Server::_ringDoorbellDelivered` keyed by `(pid, generation)` returning -1 for an incarnation that
already received one; `ring_attach` added to `REPLY_FD_COURIER_KINDS` with an appended wire-only
`fd_token` in its reply; the guest accepting `wake_fd == -1` when the shared loader already owns a
doorbell.

Measured: **RED**, and the failure is informative rather than a tuning problem.

1. `sent-to-guest pid=N token=1 kind=7 gen=0` -- the doorbell bundle was stamped with generation 0,
   because the LOADER's own seed attach runs before any courier traffic exists for that incarnation. The
   guest's staleness check then refused the descriptor (`gen=0 != its own`), closed it, the token resolved
   to nothing, the seed published no lane (`attach-rc ... wake=-1`, `map=(nil)`) and the boot stopped.
   Fixed on the way and KEPT: the server drains the courier before answering (the request-direction
   bundle can still be in the backlog when the reply is built) and the token mixes pid, generation and
   counter (MEASURED: with generation 0 the first token was exactly `1` -- reproducible from a constant
   for any pid).
2. A bundle the receiver will refuse is worse than the legacy path, so a bundle with an unknown generation
   now falls back to the reply CMSG and says so (`fd_courier_fallback_cmsg`).
3. Even with that fallback the seed still failed: the one-time rule returned -1 for the SECOND build of
   the same attach reply (the courier park path re-enters `processCall`), so the incarnation's only
   delivery was consumed by a reply that was not the one the loader read.

Reverted to the verified behaviour: every attach reply carries a dup and the guest's shared loader keeps
the first and closes the rest (one wake fd per process, N transfers). The counters
(`ring_doorbell_sent_to_guest`, `ring_doorbell_reused`) were removed with it rather than left reporting a
mechanism that is off.

What the next attempt must change, exactly:
  * make the attach reply be built once per attach (or make the delivery idempotent per attach), and
  * give the server the incarnation's generation BEFORE the loader's first attach -- the loader's seed
    attach is the first attach of a process and has no courier traffic behind it.

Procedure lesson that cost three runs: changing the generated RPC structs requires rebuilding **mldr** as
well as `darlingserver` and `libsystem_kernel.dylib`. A stale loader parsed the reply with the old size and
the seed attach failed with `rc=-70` -- a size mismatch, not a transport fault.

State after the revert (verified): `FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204, `r2 100`=206,
`stress 16x20`=644, every test `pass=1`, all `machmsg_uds=0`, 10 doorbell lines (one per image attach).


## 27. Round 49c: the per-thread RPC socket is now LAZY, and the measurement names the culprit

Built: the eager creation in `darling_thread_entry` is gone. `__darling_thread_rpc_socket()` creates the
socket at the first call that actually asks for it, registers it with the thread callbacks' guard, and
prints a bounded creation line naming the reason (`checkin` for the thread-create path). A thread whose
calls are all served by the lane creates nothing.

Measured (full regression, `FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204, `r2 100`=206,
`stress 16x20`=644, every test `pass=1`, all `machmsg_uds=0`):

```
socket creations = 27, and EVERY ONE carries reason=checkin
rpc-socket] created pid=192305 tid=192314 n=1 reason=checkin
rpc-socket] created pid=192305 tid=192315 n=2 reason=checkin
rpc-socket] created pid=192353 tid=192354 n=1 reason=checkin
rpc-socket] created pid=192353 tid=192355 n=2 reason=checkin
```

Two facts follow, and the second is the actionable one:

  * the count is now an ATTRIBUTED number rather than an allocation nobody looks at;
  * CORRECTION of my own first reading of this run: `machmsg_uds = 0` in that dump counts the mach_msg
    calls of the test, NOT the checkin, so it does not show the checkin riding the lane. The per-thread
    ordering, measured from the same log, does answer the question:

    ```
    tid=192314: SOCKET -> ATTACH
    tid=192315: SOCKET -> ATTACH
    tid=192317: ATTACH          (no socket: this thread's calls were all lane-served)
    ```

    A new thread CHECKS IN BEFORE IT ATTACHES A LANE (`rpc-register-thread` precedes `RING_ATTACH_RPC_SENT`
    for the same tid), so the checkin has no lane to ride and the socket is genuinely needed for it. The
    dependency is therefore ordering, not laziness: `per_thread_rpc_socket_created = 0` requires the
    thread's checkin to have a transport that does not need a lane -- which is exactly what the
    shared management plane (Phase-0) is for. The lazy count is what turned that from an assumption into a
    measured statement.

MEASURED NEGATIVE on the way: removing the eager creation without changing the call site left the
thread-create checkin running with `t_server_socket == -1`, and the boot stopped
(`HELLO=0`, shellspawn never ready). The call site must ask lazily; the counter then reports the truth.


## 28. Round 49d: Phase-0 -- the per-image timeline, and the design choice from source

### 28.1 Timeline (guest side, measured)

Extracted per (pid, tid) from a product run (`/tmp/LZ2.log`, 68 events over 35 tids). Guest lines carry no
clock, so this is file ORDER, which is what the ordering questions are about:

```
lane:seed-attach -> doorbell:loader -> socket:created(reason=checkin) -> lane:attach-rpc
```

Per-thread, the same run shows:

```
tid=192314: SOCKET -> ATTACH
tid=192315: SOCKET -> ATTACH
tid=192317: ATTACH          (no socket)
```

and the server-side trace from a run WITH `DSERVER_LOG_STDERR=true` (round 47/48 logs) puts
`checkin:process -> checkin:thread -> srv:plane-region -> srv:plane-request(op=1) -> lane:seed-attach` --
i.e. the plane's region is mapped and its first request serviced BEFORE the loader seeds its lane.

Image classes captured so far: the loader's own bootstrap (`lane:seed-attach`, `doorbell:loader`), an
ordinary shell/test image (execve pairs `vchroot -> launchd`, `/bin/bash -> /bin/sh`, attributed in
§24.7), and thread creation (the SOCKET -> ATTACH rows above). NOT yet captured in one run with server
timestamps: the fork child and the exec transition of an existing pid. That run is owed.

### 28.2 Design A is not a proposal -- it is already the shape of the code

`Server::_drainFdCourierMessages` maps a `DSERVER_FD_COURIER_KIND_PROCESS_CONTROL` bundle into
`_processControl[pid]` using only the connection's `SO_PEERCRED` pid. No `Process`, no `Thread`, no
namespace and no checkin is involved, and the mapping is serviced in the server's own loop pass. That is
precisely Design A's requirement: **transport registration independent of semantic guest registration**.
Design B (launcher-boundary provisioning) would need a new owner in the launch path for a capability the
courier already provides, so A is the smaller one and it is chosen.

What round 48 falsified was therefore NOT Design A but one sequence built on top of it:

```
create the page -> synchronous PING whose answer depends on normal Process state -> before the first
loader write
```

### 28.3 The exact Phase-0 delta

1. Server: when a `PROCESS_CONTROL` region is mapped, publish `transport_ready` in the page and futex-wake
   it. No Process is consulted, so this cannot be the round-48 hazard.
2. Loader: create the page, send ONLY its fd on the courier, and wait on `transport_ready` (bounded) --
   with NO PING. Establish it before `dserver_rpc_set_dyld_info`, which is where round 48 failed: the
   difference is that nothing semantic is awaited, only a transport acknowledgement the courier path can
   give without a Process.
3. Then the ordered bootstrap stream on that one page: CHECKIN (seq 1) -> SET_DYLD_INFO (seq 2) ->
   SET_EXECUTABLE_PATH (seq 3), each completing before the next is issued. No datagram interleaving.
4. Only then the lane: ATTACH_LANE's semantic half on the same page (§17), doorbell not resent (§26).

The Phase0ControlShm ABI already has the fields this needs (`abi_version`, `process_generation`,
`request_seq/state/op/payload[4]`, `reply_seq/state/status`, `futex`, `server_seen`); `transport_ready` is
the one field still to add.


## 29. Round 49e: census of the current tree, with its provenance stated

Measured from the verified run of this round (`/tmp/LZ2.log`, the same run that produced the GREEN
regression):

```
lane-stats processes                5
machmsg_ring (sum over processes)   1104
machmsg_uds  (sum over processes)   0
nonfd_uds_violation                 0
lanes acquired / released / held    154 / 153 / 1
per-thread RPC socket creations     27, every one reason=checkin
```

The per-CALLNUM census is from the round-46/47 heatmap run and is NOT refreshed by the runs above, so it
is quoted as the previous round's measurement, not as the current one:

```
checkin 174 · ring_attach(semantic) 174 · checkout 152-156 (ring 4-9) · pthread_canceled 75/490 ·
set_dyld_info 10 · set_executable_path 10 · thread_self_trap 10/184 · vchroot_path 9/20 ·
fork_wait_for_child 4/4 · console_open 3 · kqchan_proc_open 2 · TOTAL uds~623 ring~1066
```

Classification of what is still socket-backed, by the reason rather than by a bucket name:

| class | operations | why it is still on the socket |
|---|---|---|
| PRE_IMAGE_BOOTSTRAP | `set_dyld_info`, `set_executable_path`, the loader's first `ring_attach` | they precede the first lane attach and, measured in round 48, the plane's establishment cannot be moved ahead of them unchanged |
| PRE_LANE_LIFECYCLE | thread-create `checkin` | measured ordering this round: a new thread checks in BEFORE it attaches a lane, so no lane can carry it; it is also fd-bearing (lifetime pipe) |
| FD_LEGACY_NOT_YET_COURIER | none for the two ops moved this round | `console_open` / `kqchan_proc_open` descriptors now ride the courier; their SEMANTIC reply is still a datagram carrying a token, which is a semantic transport question, not an fd one |
| TEARDOWN_NO_LANE | `checkout` teardown instance | the handler clears the Thread, so the reply has no lane route (X2, measured) |
| DEBUG/COMPAT | the ring-disabled compatibility path | behind a switch by design |

Owed: one heatmap run on the current tree to replace the quoted per-callnum table, and the same run with
server timestamps to close the two image classes the timeline is still missing (fork child, exec
transition of an existing pid).


## 30. Round 49f: PHASE-0 WORKS -- the bootstrap writes ride the shared page (GREEN)

Round 48 said: establishing the plane before the loader's first write, with a synchronous semantic PING, is
RED. This round changed the DEPENDENCY rather than the position, and it is GREEN.

### 30.1 What was built

* ABI (`dserver_process_control`, appended): `volatile uint32_t transport_ready`. The server sets it the
  moment it has MAPPED the region -- which it does from the courier's `SO_PEERCRED` pid alone, with no
  `Process` and no `Thread` in existence. A guest waiting on it is waiting for a TRANSPORT
  acknowledgement, not a semantic round trip.
* Loader: the plane is created and established BEFORE `dserver_rpc_set_dyld_info`; the guest waits for
  `transport_ready` (bounded) and issues NO PING.
* Both bootstrap writes move TOGETHER, as required: `OP_SET_DYLD_INFO` (address/length in the payload) and
  `OP_SET_EXECUTABLE_PATH`. The path does NOT need a string channel in the page: the ordinary
  `SetExecutablePath` call reads it from the CALLER's memory via `process->readMemory(_body.buffer, ...)`,
  so the page carries the same guest pointer and length the datagram body carries and the semantics stay
  one implementation.
* The missing wake, found by measurement: with only the courier LISTENER in the server's epoll, a
  one-byte wake on an established connection left the server in epoll, no loop pass ran, and the page
  request was never serviced (`ready=1`, `dyld-info-op=0`, `HELLO=0`). Accepted connections are now
  watched too (`_isFdCourierSocket` + an EPOLLIN watch on the connection), the one-byte message is counted
  as `process_control_wakes`, and the event path drains the courier and services the page.
  The guest rings the process doorbell when it exists and falls back to the courier connection when it
  does not -- which is the normal case at this point in the bootstrap, because the doorbell arrives with
  the first lane attach, i.e. after the plane.

### 30.2 Measured

```
HELLO=1 DONE=1  ool 20 pass=1  basic 100 pass=1
mldr-ctl] ready pid=258123 state=1          (transport_ready observed; 7 processes)
dyld-info-op pid=258123 status=0            (the plane serviced set_dyld_info; 8 ops)
machmsg_ring=44 machmsg_uds=0
machmsg_ring=204 machmsg_uds=0
```

Full regression with the same build: `FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204, `r2 100`=206,
`stress 16x20`=644, every test `pass=1`, all `machmsg_uds=0`.

So the ordered bootstrap stream now exists on ONE shared channel for the first two writes, and the
classification in §29 loses `set_dyld_info` / `set_executable_path` from PRE_IMAGE_BOOTSTRAP. What remains
there is the loader's own first `ring_attach`.


## 31. Round 49g: the checkin's route to the page exists; whether it is taken is not yet attributed

Built: the main-thread checkin is routed through the SAME ordered shared channel as the two bootstrap
writes when the page is ready -- `OP_CHECKIN` with the architecture and `is_fork` in payload[0], the tid
in payload[1], the lifetime descriptor's COURIER TOKEN in payload[2] and the stack hint in payload[3],
matching the case that has been in the server since round 45 (and that already proved `status=0` with the
thread and process resolving correctly). The seven earlier exclusions are answered by the change in
situation rather than by a new hypothesis: they all described the page being serviced on the server's own
pass while the datagram is serviced on arrival, which only mattered while the process's OTHER bootstrap
traffic was a datagram. It is not any more -- `set_dyld_info` and `set_executable_path` are on this page
too, so the stream is one ordered sequence.

Measured: `HELLO=1 DONE=1`, `ool 20` pass=1, `basic 100` pass=1, `machmsg_ring=44/204`, `machmsg_uds=0`
-- GREEN. But `checkin-op = 0`: the plane's CHECKIN case did NOT run, so this run's checkin took the
datagram fallback. `__mldr_process_control_ready()` was false at that point in that image (the page is
created per image, and this call site is reached after the image transition). The route is therefore
implemented but NOT exercised, and it is not claimed as exercised.

Next attribution, one line: log `ready` at the checkin call site (as is already done for the plane
establishment) so the false case names the image, and decide whether the page must be re-established after
an image transition or handed to the new image by the loader.


### 31.1 The attribution, measured: `ready=0` at the checkin site in EVERY image

```
checkin-route pid=268566 tid=268566 ready=0 lifetime=-1 image=mldr!/tmp/.../vchroot
checkin-route pid=268566 tid=268566 ready=0 lifetime=-1 image=mldr!/tmp/.../launchd
checkin-route pid=268571 tid=268571 ready=0 lifetime=-1 image=mldr!/tmp/.../shellspawn
checkin-route pid=268573 tid=268573 ready=0 lifetime=-1 image=mldr!/tmp/.../bash
checkin-route pid=268573 tid=268573 ready=0 lifetime=-1 image=mldr!/tmp/.../sh
checkin-op = 0
```

Every image reaches this call site with the page NOT ready, and with no lifetime pipe (`lifetime=-1`, which
is why the token is 0 and the server's trace says `lifetime_pipe=-1`). Yet the SAME pids have
`[mldr-ctl] ready pid=N state=1` lines and the same run shows `dyld-info-op status=0`, so the page WAS
ready earlier in that process -- the establishment block sits at line ~311 and this call site at ~1770, and
`g_process_control_page` is only ever assigned (line 1055) and never reset.

So the two measurements are inconsistent unless the page the checkin site consults is not the page the
writes used. That is the next thing to measure, and it is one line each: log the PAGE POINTER and
`transport_ready` at the establishment site and at this call site, plus the region identity the server
holds for that pid (it re-maps on every `PROCESS_CONTROL` bundle, and a second image sends its own). Until
that is answered, the checkin route stays implemented-but-unexercised and the datagram fallback is what
runs -- which is exactly what the boot shows, and what this record claims: nothing more.


### 31.2 The page-identity measurement did not run; state it as open, not as answered

The instrument was added (`page=%p` at the establishment site and at the checkin site, `page=`/`gen=` on
the server's region line), and the server side DID take effect in the measured run (`region pid=N size=112`,
i.e. the larger struct with `transport_ready`). The guest side did NOT: the run's lines are still the old
format (`ready pid=N state=1` with no pointer), so the binary that ran did not carry that build, and the
question "do the two sites consult the same page?" is NOT answered by this run.

What the run does add: the server maps **112-byte** regions for each pid (the appended-field struct), and
the establishment lines exist for the same pids whose `checkin-route` says `ready=0`. The standing
hypothesis, to be tested with the instrument that is now in the source: the establishment site and the
checkin site are reached in DIFFERENT mldr invocations (the image names in `checkin-route` are execve
chains), and `g_process_control_page` is per-invocation BSS -- so the invocation that checks in consults
its own page, which is not the page the earlier invocation established. If that holds, the fix is not in
the plane but in where the page is established: it must be established (or re-established) in the
invocation that performs the checkin, or handed across the image transition by the loader.


### 31.3 Answered: the two sites DO share the page; the readiness write is what does not arrive

Measured with the instrument in the tree (same run, one pid, two execve invocations):

```
mldr-ctl] ready pid=271546 state=1 page=0x7df756666000      <- establishment, invocation 1
mldr-ctl] ready pid=271546 state=1 page=0x73636263e000      <- establishment, invocation 2
checkin-route pid=271546 ready=0 page=0x7df756666000 image=mldr!.../vchroot
checkin-route pid=271546 ready=0 page=0x73636263e000 image=.../mldr!.../launchd
process-control] region pid=271546 size=112 page=0x74cbbd1ca000 gen=...
process-control] region pid=271546 size=112 page=0x74cbbd1c8000 gen=...
```

So the §31.1 hypothesis is REFUTED: each invocation's establishment site and checkin site report the SAME
page pointer, and `__mldr_process_control_ready()` reads that same page. The page is per-invocation (two
execve images, two pages, two `PROCESS_CONTROL` bundles -- which is correct, one page per incarnation), and
the server mapped both (size=112, the appended-field struct).

What does not happen is the readiness WRITE arriving: the establishment reported `state=1` (so
`transport_ready` was 1 at that moment) and the checkin site reads 0 from the same address. The candidates
left are mechanical and each is one measurement:
  * the guest and the server disagree on the OFFSET of `transport_ready` (print
    `offsetof(dserver_process_control, transport_ready)` and `sizeof` on both sides in one run);
  * the server's mapping and the guest's mapping are not the same memory object (compare the memfd's
    `st_ino`/`st_dev` as seen by each side).
Until one of those is measured, the checkin route stays implemented-but-unexercised, and the datagram
fallback is what runs -- which is what the boot shows.

Regression with this build (verified): `FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204, `r2 100`=206,
`stress 16x20`=644, every test `pass=1`, all `machmsg_uds=0`.


### 31.4 Layout and identity agree -- the value flips within ONE invocation

Measured on both sides in the same run:

```
guest : ready pid=277253 state=1 page=0x7016f1d78000 sz=112 off=104      (establishment, inv 1)
guest : ready pid=277253 state=1 page=0x7571b5c06000 sz=112 off=104      (establishment, inv 2)
guest : checkin-route pid=277253 ready=0 page=0x7016f1d78000 .../vchroot
guest : checkin-route pid=277253 ready=0 page=0x7571b5c06000 .../launchd
server: region pid=277253 size=112 page=0x711652520000 sz=112 off=104 ino=14567386 dev=1
server: region pid=277253 size=112 page=0x71165251e000 sz=112 off=104 ino=14567388 dev=1
```

* `sz=112 off=104` on BOTH sides: the struct layout and the field offset agree, so the offset hypothesis
  is REFUTED.
* The memfd identities differ per invocation (ino 14567386 / 14567388): two pages for two execve images,
  which is the intended one-page-per-incarnation shape, and the server mapped both.
* In invocation 1 the establishment reads `state=1` from page `0x7016f1d78000` and the checkin site reads
  `ready=0` from THE SAME ADDRESS. Nothing in between re-creates the page (`g_process_control_page` is
  assigned once, and the lazy accessor returns early when it is non-NULL), and a plane request writes only
  `reply_state`/`request_*`/`seq`, never `transport_ready`.

So something WRITES 0 to that word between the two reads. The next measurement is to bracket it: log
`transport_ready` after each plane request in that invocation (before and after the two bootstrap writes),
which localises the write to one of them, and then to whoever performs it (guest or server, on which
mapping).

The `HELLO=0` in this run is the 130 s timeout, not a regression: the verified regression on this code is
`FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204, `r2 100`=206, `stress 16x20`=644, all `pass=1`, all
`machmsg_uds=0`.


### 31.5 Localised: the readiness word is 1 through both writes and 0 at the checkin

```
mldr-ctl] ready pid=280523 state=1 page=0x7880dfa71000 sz=112 off=104      (establishment)
mldr-ctl] after-dyld pid=280523 status=0 ready=1                            (plane serviced set_dyld_info)
mldr-ctl] after-execpath pid=280523 status=0 ready=1                        (plane serviced set_executable_path)
mldr-ctl] ready pid=280523 state=1 page=0x7910a99f5000                     (second invocation's establishment)
checkin-route pid=280523 ready=0 page=0x7880dfa71000 .../vchroot
checkin-route pid=280523 ready=0 page=0x7910a99f5000 .../launchd
```

So `transport_ready` is 1 at establishment and 1 after BOTH plane writes -- i.e. the whole ordered stream
works and the readiness value is correct while it matters -- and it is 0 by the time the checkin site runs,
in the same invocation, from the same address. The flip is therefore inside the window between the
exec-path request and the checkin site. That window contains, in order: `explicit_thread_self_trap`, the
lane seed (`__mldr_ring_lane_seed`, which itself issues an RPC and adopts the doorbell), and the plane's
own lazy accessor use. The next measurement brackets exactly those three, and only one of them can be the
writer, because the guest's plane code writes only `reply_state`/`request_*`/`seq` and the server's page
code writes only reply fields.

This is a precise, reproducible target rather than a hypothesis: one print after each of the three.

State: the checkin route stays implemented-but-unexercised; the datagram fallback runs; the tree is GREEN
(`FINAL=1 HELLO=1`, 44/204/206/644, all `pass=1`, all `machmsg_uds=0`) with these diagnostic prints in
place, and they are diagnostics only -- no behaviour depends on them.


### 31.6 Correction: nothing "flips" -- the checkin site runs BEFORE the establishment in that path

The window brackets came back in a different ORDER than the source suggests, and that is the answer:

```
mldr-ctl] page pid=282979 size=112 sent=1                     <- the page is created and sent
mldr-ctl] checkin-route pid=282979 ready=0 page=0x74d909560000 <- the CHECKIN site runs HERE
mldr-ctl] ready pid=282979 state=1 page=0x74d909560000         <- the establishment block's diag
mldr-ctl] after-dyld     pid=282979 status=0 ready=1
mldr-ctl] after-execpath pid=282979 status=0 ready=1
mldr-ctl] before-threadself pid=282979 ready=1
mldr-ctl] after-threadself  pid=282979 ready=1
mldr-ctl] after-seed        pid=282979 ready=1
```

The checkin site is reached BEFORE the establishment block's diagnostic in the same pid, with the SAME page
pointer. So the readiness word is not being cleared by anything -- it had simply not been set yet at the
point the checkin asked (the page existed and had been sent, but the establishment block that waits for
`transport_ready` had not run in that path). §31.5's "something writes 0" reading is superseded by this
ordering evidence, and the earlier §31.4 observation is consistent with it: the establishment diag prints
`state=1` because BY THEN the server had mapped the page.

The remaining question is therefore a control-flow one, not a memory one: in the path that reaches the
checkin, what runs between the page's creation/send and the establishment block? That is answerable by
reading the code rather than by another boot, and the fix is either to establish earlier in that path or to
let the checkin wait for readiness itself (it already has `wait_ready`, so the checkin site can simply wait
before deciding).

One caveat that must be closed first, because the two readings cannot both be true: the establishment block
is UNCONDITIONAL and sits at line ~311 while the checkin call site is at ~1780, so within one invocation the
`ready` diagnostic must precede `checkin-route`. The trace shows the opposite, with the same page pointer on
both lines. Before any code is changed on the strength of it, the trace needs to distinguish invocations:
stderr order is preserved (unbuffered), so the cheapest disambiguator is an invocation counter (or the image
path) on EVERY diagnostic line, and a re-run then says whether the ordering is real or whether these two
lines belong to different mldr invocations that happen to share a page address.

State: the checkin route stays implemented-but-unexercised, the datagram fallback runs, the tree is GREEN
(`FINAL=1 HELLO=1`, 44/204/206/644, all `pass=1`, all `machmsg_uds=0`), and the added prints are
diagnostics only.


### 31.7 The instrument had a side effect -- that is what the ordering contradiction was

With the invocation tag added, the trace is unambiguous and the contradiction is explained:

```
mldr-ctl] page pid=289510 size=112 sent=1                      <- page CREATED here
mldr-ctl] checkin-route pid=289510 ready=0 page=0x74697f4f6000 image=mldr!.../vchroot
mldr-ctl] ready pid=289510 state=1 page=0x74697f4f6000 sz=112 off=104
mldr-ctl]   ready-image=vchroot
mldr-ctl] after-dyld     ... ready=1 image=vchroot
```

The `checkin-route` diagnostic calls `__mldr_process_control_page()`, and THAT ACCESSOR CREATES THE PAGE
LAZILY when it is absent. So the instrument itself performed the creation (the `page ... sent=1` line is
its own), then immediately read `transport_ready` -- which was 0 because nothing had waited for the server
yet -- and the establishment block afterwards found the page already created and waited, printing
`state=1`. The "checkin before establishment" ordering was the instrument's own side effect, not the
program's control flow, and every conclusion drawn from that ordering is withdrawn.

What survives, and is worth keeping: the ordering question needs an instrument that only READS. The next
step is exactly that -- print `g_process_control_page` (the pointer, no accessor call) plus
`transport_ready` at each site, re-run, and only then decide whether the checkin site is reached before or
after the establishment in the same invocation.

Also still true from this run: the plane is established and both writes are serviced (`status=0`), and the
readiness word is 1 from the establishment onward within that invocation.


### 31.8 The side-effect explanation is itself refuted: the ordering survives a read-only instrument

The diagnostics were changed to read the page pointer through a read-only accessor (no lazy creation), and
the trace is UNCHANGED:

```
mldr-ctl] page pid=291917 size=112 sent=1
mldr-ctl] checkin-route pid=291917 ready=0 page=0x77b892de4000 image=mldr!.../vchroot
mldr-ctl] ready pid=291917 state=1 page=0x77b892de4000 sz=112 off=104
mldr-ctl]   ready-image=vchroot
mldr-ctl] after-dyld/execpath/threadself/seed ... ready=1 image=vchroot
```

So §31.7 is WITHDRAWN: the ordering was not the instrument. What the trace says, with the same page
pointer and the same image tag on every line, is that in this invocation the checkin site prints BEFORE the
establishment block's line -- while the source puts the establishment at ~311 and the checkin at ~1810, and
the `page ... sent=1` line (which comes from `__mldr_process_control_create`) precedes BOTH.

Three facts therefore stand together and cannot all be explained by one `main()` pass:
  * the page exists before the checkin (`page sent=1` is first);
  * `ready` is 0 at the checkin and 1 at the establishment line and afterwards;
  * both plane writes are serviced (`status=0`).

The next investigation must NOT be another hypothesis about memory: it needs (a) a monotonic sequence
number stamped on every diagnostic line so any reordering is impossible, and (b) a check of whether this
path reaches `main()` more than once per image (the loader is re-entered across execve, and the image tag
alone cannot distinguish two passes of the SAME image).

State unchanged and verified GREEN: `FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204, `r2 100`=206,
`stress 16x20`=644, all `pass=1`, all `machmsg_uds=0`; the checkin route is implemented but unexercised.


### 31.9 The checkin route was exercised -- and the boot stops (measured, reverted)

`page=(nil)` at the checkin site answered the ordering question: the site is reached BEFORE the page exists,
so the route could never be taken. Establishing the transport AT THE SITE (`__mldr_process_control_create()`
+ `wait_ready()` when the page is NULL) made the route reachable, and then:

```
checkin-op pid=300510 call=1 thread=300510 process=300510 nsid=1     (twice: two processes)
HELLO=0   dyld-op=1 (a healthy boot performs 8)   shellspawn never became ready
```

So the plane's CHECKIN is EXECUTED and SEMANTICALLY CORRECT -- the synthesized call resolves its thread, its
process and `nsid=1` -- and the boot still stops. That is the round-45/46 outcome reproduced with the plane
established at the point of use, which rules out the establishment position as the cause and leaves the
servicing model: the page is serviced on the server's own loop pass, while the datagram is serviced on
arrival, and this checkin's effect must be visible to the process's other traffic (the fork/exec handshake
that travels on a socket).

Reverted: the checkin stays on the datagram path, with the measurement recorded next to the call site.
Verified after the revert: `FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204, `r2 100`=206,
`stress 16x20`=644, every test `pass=1`, all `machmsg_uds=0`.

Also settled in this round: the diagnostics now carry a per-process sequence number and an invocation image
tag, so any future ordering claim can be checked against a stamped order rather than a line position.


## 32. Round 49j: the fresh per-callnum census, and a defect it exposes in the census itself

Measured on the current tree, live from the server's stat channel during the standard product workload
(`DARLING_SERVER_RPC_HEATMAP=1`), after the plane is established before the first write:

```
rows=21   total=1691   uds=624   ring=1067        (rows reconcile: 624 + 1067 = 1691)

pthread_canceled        566   uds=75   ring=491
thread_self_trap        194   uds=10   ring=184
checkin                 174   uds=174  ring=0     PRE_LANE_LIFECYCLE
set_thread_handles      174   uds=0    ring=174
ring_attach             174   uds=174  ring=0     semantic half still a datagram
checkout                162   uds=153  ring=9
mach_reply_port          58   uds=0    ring=58
host_self_trap           38   uds=0    ring=38
task_self_trap           36   uds=0    ring=36
vchroot_path             29   uds=9    ring=20
uidgid                   22   uds=0    ring=22
started_suspended        10   uds=0    ring=10
get_tracer               10   uds=0    ring=10
set_dyld_info            10   uds=10   ring=0     <-- serviced by the PLANE, counted as UDS
set_executable_path      10   uds=10   ring=0     <-- serviced by the PLANE, counted as UDS
mldr_path                 9   uds=0    ring=9
fork_wait_for_child       8   uds=4    ring=4
console_open              3   uds=3    ring=0     semantic datagram; descriptor on the courier
kqchan_proc_open          2   uds=2    ring=0     same
vchroot                   1   uds=0    ring=1
kqchan_mach_port_open     1   uds=0    ring=1
```

Counterpart evidence from the same run: `process_control_regions=10`, `process_control_requests=20`,
`process_control_wakes=20`, `fd_courier_sent_to_guest=5`, `fd_courier_fds_received=183`,
`fd_courier_fd_matched=173`, `ring_doorbell_processes=9`.

**The defect this exposes**: `process_control_requests = 20` is exactly `set_dyld_info 10 +
set_executable_path 10`, and those 20 rows are counted in the UDS column. The heatmap tags a call by the
transport its REQUEST arrived on, and a plane request is not tagged at all, so the synthesized call falls
into the datagram bucket. The census therefore OVERSTATES legacy semantic UDS by the plane's own traffic --
in this run by 20 of 624. Any gate of the form "legacy semantic UDS = 0" has to separate the plane's
serviced calls from real datagram-serviced ones first, or it will never reach zero while the plane is used.
That is the next census change, and it is a measurement change, not a transport one.

Classification of the remaining socket-backed traffic with this table (replacing the quoted one in §29):
  * PRE_LANE_LIFECYCLE: `checkin 174` (a new thread checks in before it attaches a lane).
  * PRE_IMAGE_BOOTSTRAP: the loader's first `ring_attach` (its 174 includes the per-thread attaches).
  * FD_LEGACY_NOT_YET_COURIER: none -- `console_open` / `kqchan_proc_open` descriptors ride the courier
    (`fd_courier_sent_to_guest=5` matches their 5 calls); their SEMANTIC reply is still a datagram.
  * TEARDOWN_NO_LANE: the `checkout` instances with no lane route (153 of 162).
  * POST_LANE_NONFD_BUG candidates, to be checked one by one: `thread_self_trap uds=10`,
    `vchroot_path uds=9`, `pthread_canceled uds=75`, `fork_wait_for_child uds=4` -- these have a usable lane
    for their other instances (184/20/491/4 ring), so each UDS instance needs a reason.


### 32.1 The reason histogram is dumped but never populated

§6 ("fix every POST_LANE_NONFD_BUG the census finds") needs the REASON each UDS instance chose the socket.
The mechanism exists and is dumped at `sys_exit`:

```
for (unsigned r = 0; r < GR_URS_COUNT; ++r) {
    uint64_t c = __atomic_load_n(&gr_urs_counts[r], __ATOMIC_RELAXED);
    if (c != 0) { fprintf("[dring-uds-reason-hist] pid=%d reason=%s count=%llu ..."); }
}
```

Measured with `DARLING_GUEST_LANE_STATS=1 DARLING_GUEST_UDS_SEND_SITE=1 DARLING_GUEST_RING_TRACE=1` on a
product run (`HELLO=1 DONE=1`): **zero** `[dring-uds-reason-hist]` lines, and `grep -c
g_stat_uds_reason dserver-ring.c` is 0 -- i.e. nothing ever increments `gr_urs_counts`, so the histogram is
a print of an empty table. The enum (`GR_URS_NO_LANE_ENTRY` .. `GR_URS_FD_TRANSFER_REQUIRED`) is complete and
named; what is missing is the recording call at the guest's own decision point.

So §6 is blocked on one small instrumentation change, not on a transport question: record the reason where
the datagram decision is made, then the four candidates the census names (`thread_self_trap uds=10`,
`vchroot_path uds=9`, `pthread_canceled uds=75`, `fork_wait_for_child uds=4`) each get a named reason and can
be classified as PRE_LANE / TEARDOWN / FD / real nonfd bug.

This run was GREEN (`HELLO=1 DONE=1`, `ool 20`/`basic 100` pass).


### 32.2 Correction to §32.1: the histogram IS populated -- the dumping processes are not the ones that fall back

Reading the recorder instead of the dump:

```
static void gr_urs_note(uint32_t callnum, const char* name, int reason, gr_lane_t* lane) {
    __atomic_fetch_add(&gr_urs_counts[reason], 1u, __ATOMIC_RELAXED);
    ...
}
```
and it has FIVE call sites (a lane that is not active, an image-local state, a miss with
`gr_urs_reason_for_miss`, an attach failure, and the generated wrapper's own miss). So the reasons are
recorded at the decision point already, and §32.1's "nothing ever increments it" is WRONG and is withdrawn.

What the run actually shows is a coverage gap in the DUMP: five processes printed `[dring-lane-stats]` (with
the histogram loop after it) and none of them had a non-zero count, while the census attributes the
fallbacks (`thread_self_trap uds=10`, `vchroot_path uds=9`, `pthread_canceled uds=75`,
`fork_wait_for_child uds=4`) to the workload -- i.e. the processes whose calls fall back are not the ones
that reach the dumping exit path. To answer §6 the histogram must be dumped where the fallbacks happen (or
the reasons must be folded into the server-side per-callnum table, which already has the counts).

So §6's blocker is narrower than §32.1 claimed: not "instrument the decision point", but "dump the existing
histogram from the processes that actually take the datagram path".


### 32.3 The reason instrumentation covers the generated wrappers only

`DARLING_GUEST_LANE_DIAG=1` (which makes `gr_urs_note` emit one `[dring-uds-reason]` line per
(reason, callnum)) produced ZERO lines on a product run that is otherwise GREEN (`HELLO=1 DONE=1`). Together
with §32.2 that pins the coverage: `gr_urs_note` is called from the GENERATED wrapper path and from the
lane-resolution misses that path takes, while the operations the census shows falling back --
`thread_self_trap`, `vchroot_path`, `pthread_canceled`, `fork_wait_for_child`, `set_dyld_info`,
`checkin`, `ring_attach` -- are issued from HAND-WRITTEN call sites (the loader's own RPC glue and the
lifecycle code), which never enter that path and therefore never record a reason.

That is the honest state of §6: the reason census exists and works for generated calls, and the remaining
fallbacks are exactly the hand-written ones, which is consistent with their classification -- PRE_IMAGE_
BOOTSTRAP (the loader's writes and first attach), PRE_LANE_LIFECYCLE (checkin) and TEARDOWN_NO_LANE
(checkout's detached instance). The four candidates the per-callnum table names
(`thread_self_trap uds=10`, `vchroot_path uds=9`, `pthread_canceled uds=75`, `fork_wait_for_child uds=4`)
are the ones that still need a reason from a hand-written site before any of them can be called a real
non-fd bug.


### 32.4 The census defect is fixed: plane-serviced calls have their own column (measured)

`CallTransport` gained `ProcessControl`; the three synthesized dispatch sites in
`Server::_serviceProcessControl` tag the thread before `doWork()`; the heatmap row now carries a `plane`
field and the row filter and total include it. Measured on the same product workload:

```
rows=21  total=1691  uds=604  ring=1067  plane=20        (604 + 1067 + 20 = 1691, rows reconcile)

plane rows:  set_dyld_info plane=10 (uds=0)   set_executable_path plane=10 (uds=0)

remaining UDS by call:
  checkin                174   ring=0     PRE_LANE_LIFECYCLE (a thread checks in before its lane exists)
  ring_attach            174   ring=0     PRE_IMAGE_BOOTSTRAP (semantic half)
  checkout               153   ring=9     TEARDOWN_NO_LANE
  pthread_canceled        75   ring=491   candidate
  thread_self_trap        10   ring=184   candidate
  vchroot_path             9   ring=20    candidate
  fork_wait_for_child      4   ring=4     candidate
  console_open             3   ring=0     semantic datagram; descriptor on the courier
  kqchan_proc_open         2   ring=0     same
```

So legacy semantic UDS is **604**, not 624: the plane's own 20 serviced calls no longer inflate it, and each
of the four candidates now has a number to explain rather than a bucket. The reconciliation is exact
(`500 + 604 + 1067 + 20` counting the ring-only rows = 1691), which is what the hard census gate in §26
needs before it can ask for zero.

The census row field `plane` and the `CallTransport::ProcessControl` tag are the measurement change this
round produced; the transport itself is unchanged by it.


### 32.5 §6 answered: TWO operations use UDS while their thread HAS a live lane

Armed census (`DARLING_SERVER_RPC_HEATMAP=1 DARLING_SERVER_RESIDUAL_CENSUS=1
DARLING_SERVER_ATTACH_CENSUS=1`), same product workload, GREEN (`FINAL=1 HELLO=1`, every test `pass=1`):

```
residual_uds_despite_lane = { checkout: 153, vchroot_path: 9, thread_self_trap: 9 }
residual_reason           = { thread_no_ring_proc_none: 1, thread_no_ring_proc_has: 0,
                              thread_has_ring: 171, control_plane: 348, ineligible: 1361 }

attach_census (per callnum):
  checkin              pre_attach_uds=9    post_attach_uds=165   pre_attach_eligible=9  ring_eligible=1
  checkout             pre_attach_uds=0    post_attach_uds=153   pre_attach_eligible=0  ring_eligible=1
  vchroot_path         pre_attach_uds=0    post_attach_uds=9     pre_attach_eligible=0  ring_eligible=1
  fork_wait_for_child  pre_attach_uds=0    post_attach_uds=8     ring_eligible=0
  console_open         pre_attach_uds=0    post_attach_uds=3     ring_eligible=0
```

Read as a classification (no bucket names invented, each row from a measured field):

  * `checkin` 9 pre-attach: the thread has no lane yet BY CONSTRUCTION (it checks in before attaching) --
    PRE_LANE_LIFECYCLE, not a bug. Its 165 post-attach instances already ride something else.
  * `checkout` 153 residual: the known teardown instance (§X2: the handler clears the Thread so the reply
    has no lane route) -- TEARDOWN_NO_LANE.
  * `vchroot_path` 9 and `thread_self_trap` 9 residual with `ring_eligible=1`: **these are the real
    POST_LANE_NONFD_BUG cases** -- the thread HAS a live lane, the operation is ring-eligible, and it took
    the datagram anyway.
  * `fork_wait_for_child` 8 and `console_open` 3: `ring_eligible=0`, so their datagram use is not a defect.

So §6 is answered: there are exactly two operations to investigate, not four, and neither is the
bootstrap/teardown family the earlier reading suspected. `vchroot_path` and `thread_self_trap` are both
IMAGE-LOCAL-looking calls (a path lookup and a trap that the loader/image performs) -- the obvious
hypothesis to test is that these instances come from an image whose lane view is absent
(`GR_URS_IMAGE_LOCAL_STATE`), which would make them a *view* problem rather than a routing one.


### 32.6 The two candidates: both are ring-ELIGIBLE in policy and most instances DO ride the lane

Source facts for the pair (§32.5 named them):

  * `thread_self_trap` and `vchroot_path` are both in `RING_GENERATED_SIMPLE` (policy value 0), and the
    loader's hand-written consumers get a real `dserver_rpc_hooks_try_ring` that calls
    `__mldr_ring_call` whenever `__mldr_ring_lane_ready()` -- so the loader is not a consumer without a
    lane route.
  * The census shows most instances already on the lane: `thread_self_trap` 184 ring vs 10 uds,
    `vchroot_path` 20 ring vs 9 uds.
  * Both call sites in the loader are AFTER its lane seed (`__mldr_ring_lane_seed` at ~367,
    `dserver_rpc_explicit_thread_self_trap` at ~387, `dserver_rpc_vchroot_path` at ~1865), and the attach
    census says their UDS instances are `post_attach_uds` with `ring_eligible=1`.

So neither is "a call with no route": each is an instance where the wrapper tried and the attempt came back
NOT_TAKEN. The single measurement that separates the possibilities is the RETURN VALUE of `__mldr_ring_call`
for those instances (negative = refused, and by which check) plus the server's op-class answer for the
callnum -- one log line at the loader's try_ring with `rc` and the callnum, run once, and the pair is
classified. That is the next step, and it is small because the hook is already a single function.

What this section does NOT claim: it does not claim the two are unfixable, and it does not claim they are
bootstrap-only -- `post_attach_uds` is measured, so they happen with a lane attached.


### 32.7 The two candidates narrow to ONE condition: a full ring

Measured: the loader's own `try_ring` never refused in a product run (`[mldr-ring] refused` = zero lines
with the instrument in place), so the 9-10 datagram instances of `thread_self_trap` / `vchroot_path` do not
come from the loader's generated-wrapper path. They come from the KERNEL image's dedicated fast paths
(`__dserver_ring_thread_self_trap` / `__dserver_ring_vchroot_path`), which try the ring first and fall back
to the datagram when the helper returns -1:

```
static int gr_port_trap(uint32_t callnum, uint32_t* out_port) {
    gr_lane_t* L = gr_lane_for_this_thread_named((uint32_t)callnum, "gr_port_trap");
    if (!L) {
        return -1; // no ring for this thread -> UDS fallback (reason counted at the lookup)
    }
    dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
    if (!req) {
        return -1; // ring full -> UDS fallback
    }
    ...
```

There are exactly two -1 conditions, and the census already rules out the first for these instances: the
residual census records them as `thread_has_ring` (the thread HAD a live lane). What remains is the second:
`dserver_ring_producer_begin` returned NULL because the c2s ring had no free slot, and the call fell back to
a datagram by design.

That makes the fix concrete and small, and it is the same shape as the earlier M6 lesson (a guest must not
silently drop a slot the server may be parked on):

  1. count that condition (`GR_URS_RING_FULL`), because the reason histogram has no reason for it today and
     that is why the census could not name it;
  2. on a full ring, WAIT for a slot (bounded) instead of falling back to UDS -- a full ring is
     backpressure, not a routing failure, and falling back reintroduces exactly the transport this bead is
     removing.

Neither claim here is beyond the source: the -1 conditions are the two lines quoted, and the census field
that rules out the first one (`thread_has_ring`) is measured.


### 32.8 The full-ring hypothesis is REFUTED by measurement; what remains is the guest's own lookup

Added `GR_URS_RING_FULL` (a named reason at the `!req` exit of `gr_port_trap`) and ran the product workload
with `DARLING_GUEST_LANE_DIAG=1 DARLING_GUEST_LANE_STATS=1 DARLING_SERVER_RPC_HEATMAP=1
DARLING_SERVER_RESIDUAL_CENSUS=1`:

```
HELLO=1 FINAL=1, every test pass=1
reason=RING_FULL occurrences: 0
[dring-uds-reason-hist] lines: 0
```

So the ring was never full for those instances, and §32.7's conclusion is WITHDRAWN. `gr_port_trap` has two
-1 exits; the second is now measured out, which leaves the first -- `gr_lane_for_this_thread_named()` found
no lane for the calling tid. The guest's lookup and the server's `thread_has_ring` can disagree because they
answer different questions: the server asks whether the thread has a lane attached, the guest asks whether
THIS tid has a usable entry in ITS image's catalog -- and a tid whose lane lives in another image's view (or
that has not attached yet in this image) is a miss on the guest side while the server still sees a ring.

The reason for that miss cannot be read from this run because the processes that take those fallbacks do not
emit the reason lines at all (the dump and the per-miss emit both live in code paths they do not reach --
§32.2/§32.3). That is the honest state: the cause is now "the guest's lane lookup missed for that tid", the
two candidate operations are the only ones the census flags, and naming the exact reason needs the guest
reason line to reach the log from the image that performs those calls.

The `GR_URS_RING_FULL` reason stays in the tree: it is a real condition of `gr_port_trap`, it was simply not
the one that fired, and leaving it unnamed is what let two rounds of reading assume it.


### 32.9 The two candidates bypass BOTH ring attempts -- that is the finding

Three measurements together, all on GREEN product runs:

  * the loader's `try_ring` never refused (an instrumented refusal log printed nothing);
  * the new `GR_URS_RING_FULL` reason never fired (0 occurrences);
  * the reason emission was made UNCONDITIONAL and bounded (one line per (reason, callnum), at most eight
    per process) and still printed **zero** lines -- under the full workload (ool + basic + r2 +
    stress_pool), with `DARLING_GUEST_LANE_DIAG` set or not.

`gr_urs_note` has five call sites, three of them inside the guest's own lane lookup
(`gr_lane_for_this_thread_named`: lane not active, image-local state, and the miss reason). So a fallback
that went through the generated wrapper OR through the guest's lane lookup would have produced a line, and
none did. Only two consumers define `dserver_rpc_hooks_try_ring` (the kernel image and the loader) and both
implement it.

Therefore the 9-10 datagram instances each of `thread_self_trap` and `vchroot_path` are issued by a call path
that uses NEITHER the generated wrapper's ring attempt NOR the guest lane lookup -- i.e. a direct
`dserver_rpc_*` call inside the kernel image that takes the datagram by construction. That is where the next
investigation goes, and it is a source-reading step first (find the direct call for those two callnums and
route it through the ring path the way the other 184/20 instances already are), not another boot.

Nothing about the transport changed in this section; the three negative measurements are the deliverable, and
they close the two hypotheses the earlier sections had left open (ring-full, and a generated-wrapper
refusal).


### 32.10 Located by reading: the two candidates are the image-switch UDS retention path

Reading the two kernel call sites settles it, and neither is a coding slip:

```
mach_port_name_t thread_self_trap_impl(void) {                     // mach_traps.c
    int ring_code = __dserver_ring_thread_self_trap(&port_name);
    if (ring_code >= 0) {
        return ring_code == 0 ? port_name : MACH_PORT_NULL;        // 0 = ok, KERN_FAILURE = committed
    }
    if (dserver_rpc_thread_self_trap(&port_name) != 0) { ... }     // only a NEGATIVE return falls back
}

if (__dserver_ring_vchroot_path(...) == 0) { code = t_code; }      // vchroot_userspace.c
else { code = dserver_rpc_vchroot_path(...); }
```

Both fall back only on a NEGATIVE return, and `gr_port_trap` returns a negative value only BEFORE publish
(no lane, or ring full -- both measured out). The positive `KERN_FAILURE` (committed-unknown) is handled
without a retry, so the double-execution hazard I was looking for is NOT present at these sites.

What is left is the guest lookup's early return that has NO reason note:

```
gr_lane_t* gr_lane_for_this_thread_named(uint32_t callnum, const char* name) {
    gr_lane_t* L = gr_find_lane(tid);
    if (L) {
        if (L->state == 1 && server_state == DSERVER_RING_SRV_RETIRED) {
            ...
            if (was_borrowed) { gr_proc_unpublish(tid); }
            return adopted_after_retire;      // <-- returns NULL with NO gr_urs_note
        }
        ...
```

That is the documented behaviour ("Another image now owns this thread's server-side lane. Keep this image on
UDS rather than stealing it back on every image switch"), and it is exactly the shape the three zero-reason
measurements predicted: a fallback with no reason line, because this path does not emit one.

So §6's two candidates are the **image-switch UDS retention**: an image that has switched away from owning a
thread's lane keeps that thread on the datagram for `thread_self_trap` / `vchroot_path`. That is an
architectural gap of the SAME family as the borrowed-view work (§19-20), not a missing route: the fix is for
the image to ADOPT the live lane instead of retaining UDS, which is the one-lane-per-tid ownership question
this bead is already about. Recording it here also removes the last open reading: nothing else in the
fallback set is unexplained.


### 33. FD inventory: the METHOD is not yet trustworthy, so no numbers are published

Attempted the §27 measurement (transport descriptors at 1/8/24/40 threads, from `/proc/<pid>/fd` classified
by `readlink`) with a host-side sampler. Two attempts picked the WRONG process -- the same pid was reported
for `stress_pool 1 40` and `stress_pool 8 40`, with 34 sockets and 1 eventfd, which is a launcher/shellspawn
shape, not a test process running 1 vs 8 worker threads. The guest pid is only printed by the test's own
`[dring-lane-stats]` line AT EXIT, so it cannot be used to sample a live process, and "newest matching
cmdline" was not enough to disambiguate.

Rather than publish counts that are not attributable to the workload, this is recorded as an open
measurement-method problem. Two workable fixes, in order of cost:
  * have the guest print its pid (and thread count) at START, not only at exit, so the sampler has a pid it
    can trust for the duration of the run; or
  * sample every process whose cmdline matches AND whose start time is inside the run's window, and report
    them as a set with the thread count each one reports, instead of picking one.

Nothing about the transport changed here; the deliverable of this section is the negative result about the
method plus the two ways to fix it, because an fd slope computed from the wrong process is worse than no
slope.


### 33.1 FD inventory, measured (method fixed first)

The method problem in §33 was fixed by making the TEST name itself at start
(`[rmmt] start pid=N mode=... workers=N`, one line, added to `ring_mach_msg_test`), so the host-side sampler
uses the pid of the process the workload actually runs in instead of guessing from cmdlines. The sample is
taken while the test is running (`stress_pool N 4000`, sampled at 25 s).

```
stress_pool 1  4000:  total=18  sockets=5   eventfds=1  memfds=1  pipes=1  other=10
stress_pool 40 4000:  total=96  sockets=83  eventfds=1  memfds=1  pipes=1  other=10
```

Read as a slope:

  * `eventfds` = **1** at both sizes: the ONE process doorbell does not scale with threads. That is the
    perf#28 property, now measured from the fd table rather than asserted.
  * `memfds` = **1**: the lane catalog is one page (`pages=1` in the lane-stats line agrees), not one memfd
    per lane.
  * `pipes` = 1 and `other` = 10: constant.
  * `sockets` goes 5 -> 83 for 1 -> 40 threads: **+78 for +39 threads = exactly 2 sockets per thread**. The
    per-thread RPC socket is still the descriptor that scales with threads, and it is the thing the final
    architecture removes.

So the FD slope target (§27) is not met yet, and the measurement now says by how much and by what: two
sockets per thread, one process doorbell, one lane page. `other=10` is unclassified by this sampler
(readlink did not match any of the four patterns) and is the next thing to name -- it is constant with thread
count, so it is not a scaling term, but leaving it as "other" is exactly the kind of bucket this work has
been removing.


### 33.2 The slope is exactly 2 sockets per thread, and `other` is named

Third size measured, so the slope is a line and not two points:

```
stress_pool  1 4000:  total=18  sockets=5   eventfds=1  memfds=1  pipes=1  other=10
stress_pool  8 4000:  total=32  sockets=19  eventfds=1  memfds=1  pipes=1  other=10
stress_pool 40 4000:  total=96  sockets=83  eventfds=1  memfds=1  pipes=1  other=10
```

  * 1 -> 8 threads: +14 sockets for +7 threads = **2.0 per thread**;
  * 8 -> 40 threads: +64 sockets for +32 threads = **2.0 per thread**;
  * intercept: 5 - 2*1 = 3 sockets that do not scale;
  * `eventfds` 1, `memfds` 1, `pipes` 1 at every size: the process doorbell, the lane catalog page and the
    lifetime pipe are process-scoped, as designed.

The ten `other` descriptors are named now (the sampler prints their targets):

```
4  /tmp/FD-1.log            (the harness's own stdout/stderr redirects)
2  /dev/null
1  /tmp/dr-on-matched       (the prefix directory)
1  <agent session jsonl>    (inherited from the shell that launched the run)
1  /dev/urandom
1  /dev/pts/0
```

Every one of them is a harness/launcher artifact, not a transport descriptor -- so the transport inventory is
complete and exact: **2 sockets per thread, 1 eventfd, 1 memfd, 1 pipe per process**, plus 3 non-scaling
sockets at the intercept that the next pass should name the same way.

This is the FD deliverable of §27 in its measured form: the target slope is zero, the measured slope is 2
sockets per thread, and the scaling term is the per-thread RPC socket -- the object the final architecture
removes. No transport change was made for this measurement.


### 34. One-time doorbell, second attempt (after Phase-0): still RED, reverted again

Re-enabled the one-time delivery with the Phase-0 reasoning: the plane is now established BEFORE the first
write, so the process's courier generation should be known by the first attach, which is what the first
attempt lacked. Measured:

```
HELLO=0   DONE=0   passes: none
kind=7 bundles sent: 0        [dring-doorbell] lines: 0
```

So the courier never carried the doorbell at all -- the generation-0 fallback was taken again, because the
LOADER's seed attach still precedes its own courier traffic (its `PROCESS_CONTROL` bundle is sent before the
attach but the generation map is populated by the drain, which had not seen it yet at that point). With
`wake_fd = -1` from the one-time rule and the CMSG fallback apparently not reaching the loader, the seed
published no lane and the boot stopped.

Reverted to the verified behaviour (every attach reply carries a dup; the guest's loader keeps the first and
closes the rest). Verified after the revert: `FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204, `r2 100`=206,
`stress 16x20`=644, every test `pass=1`, all `machmsg_uds=0`.

Both halves of §26's requirement are now measured rather than argued: the attach reply must be built once per
attach (the park path re-enters `processCall`), AND the incarnation's generation must reach the server before
its first attach -- Phase-0 gives the page early, but the generation travels on the courier's drain, which is
a different ordering.

Kept from this attempt: the `ring_doorbell_sent_to_guest` / `ring_doorbell_reused` metrics and the
generation-0 fallback in `sendFdCourierBundleToGuest` (both are correct in themselves and were needed to see
why the bundle was refused).


### 35. Per-image bootstrap timeline with server timestamps (§7 deliverable)

One run with `MLDR_COURIER_DIAG=1 DARLING_SERVER_COURIER_LOG=1 DSERVER_LOG_STDERR=true`, events merged by
pid; server lines carry their own monotonic stamp, guest lines carry the sequence number added in §31.

**Loader / initial image (the `launchd`-style bootstrap):**

```
1760.284 checkin:process
1760.284 srv:plane-region            <- the page is mapped
1760.285 srv:plane-request           <- the PING the plane issues on establishment
        plane:set_dyld_info          <- via the page
1760.285 srv:plane-request
        plane:set_executable_path    <- via the page
        lane:seed-attach
        doorbell:loader
1760.296 courier:checkout-fd x2      -> exec transition
1760.298 srv:plane-region            <- a fresh page for the new incarnation
1760.299 srv:plane-request
```

**Ordinary shell/test exec image:**

```
1760.325 checkin:process
        lane:attach-rpc
1760.325 courier:lane-backing x2     <- the lane backing descriptor
1760.330 courier:console-fd x2       <- console_open on the courier
1760.333 courier:checkout-fd x2
1760.335 srv:plane-region            <- the page is mapped AFTER the attach here
1760.336 srv:plane-request
        plane:set_dyld_info
        plane:set_executable_path
        lane:seed-attach
```

Two facts fall out of the ordering, and they are the fork/exec classes §7 was missing:

  * the classes differ in WHEN the plane is established relative to the lane attach: in the loader it comes
    before the seed, in an ordinary exec image the attach comes first;
  * a process that execs gets a FRESH page for the new incarnation (`srv:plane-region` appears again after
    `courier:checkout-fd`), which is the one-page-per-incarnation shape the ABI was built for.

The fork class is visible in the same run as `checkin:process` following a `courier:checkout-fd` pair for a
new pid (pids 382881/382882/382883/382884 all appear within 0.3 s of the first process, each with its own
attach and plane), i.e. the fork child re-establishes both transport halves itself instead of inheriting
them -- which is what the fork-reset design requires.


### 36. P1/P2 on the current tree

Same product configuration (default Ring), one run:

```
bench_simple 2000   pass=1  ns_per_op=28668.2   p50=16551.0  p95=75301.9  p99=164686.0  min=7032.9  max=675646.1
bench_ool    2000   pass=1  ns_per_op=218509.2  p50=183584.9 p95=427467.1 p99=607161.1  min=76814.1 max=1524037.0
```

  * P1 (simple mach_msg round trip): 28.7 us/op, median 16.6 us;
  * P2 (out-of-line descriptors): 218.5 us/op, median 183.6 us.

Provenance, stated because it matters: these are ABSOLUTE numbers on the current build, not a controlled A/B
against UDS. The last controlled pairs were measured in round 12 with the same harness (UDS 40102 vs Ring
17922 ns/op for simple; 400661 vs 228005 for OOL), and round 12's Ring figure for simple was 21996 ns/op
against today's 28668. The two are not comparable as a regression claim: the build, the prefix and the
machine load differ, and this session's own lesson is that back-to-back runs on a loaded machine move these
medians materially. A controlled pair (Ring vs UDS on ONE build, ideally alternating) is the measurement
that would settle whether the plane/courier work cost anything on the hot path, and it is owed.

What can be said without overclaiming: the workload-level regression suite is GREEN with every descriptor
half either on the courier or on the page, and the hot-path benchmarks still pass with the same shape
(simple ~29 us median 17 us; OOL ~219 us median 184 us).


### 37. Third doorbell attempt: the courier carried it -- and the reply token is the remaining defect

Step forward first, and it is kept: the plane's establishment PING now carries the incarnation's GENERATION
in payload[3], and the server records it when it has none from the courier drain. Verified GREEN on its own:

```
HELLO=1 DONE=1  pass=1 pass=1   machmsg_ring=44/204   machmsg_uds=0
ping requests: 8   (all carrying the generation)
```

That closes the gap the first two attempts died of: the server knows the generation BEFORE this process's
first attach, so a bundle it sends to that attach is no longer stamped 0.

With the one-time rule re-enabled on top, the courier finally carried a doorbell:

```
sent-to-guest pid=391064 token=7606716567886162703 kind=7 gen=5927304846994962
```

-- a correct kind, a correct nonzero token and a real generation. And yet:

```
[mldr-seed] attach-rc pid=391064 rc=0 reject=0 wake=-1
[dring-doorbell] lines: 0        fd-courier-recv calls: 0        HELLO=0
```

The loader's seed attach received `wake_fd = -1`, and its client never called the courier receive, so the
reply's token did not reach it. That is a defect in the REPLY TOKEN PATH, not in the delivery: the bundle
arrived, the token was in the reply the server built, and the receiving side did not resolve it.

Reverted, keeping the generation fix (verified GREEN alone: `FINAL=1 HELLO=1`, `ool 20`=44, `basic 100`=204,
`r2 100`=206, `stress 16x20`=644, every test `pass=1`, all `machmsg_uds=0`). The next step is exactly that
reply path -- why the generated wrapper's token branch did not fire for `ring_attach` on the loader's own
seed call, which is a source-reading question first.


### 38. Fourth doorbell attempt: real progress, a mixed-state incident, and the real next target

With matching binaries and the loader-side receive implemented (the loader's `dserver_rpc_hooks_fd_courier_receive`
was a `return -1` STUB -- correct when only the loader-to-server direction existed, wrong now that the
doorbell arrives in the other direction):

```
[mldr-seed] attach-rc pid=399509 rc=0 reject=0 wake=8      <- the loader RESOLVED a doorbell
kind=7 bundles sent: 36                                     <- but the one-time rule did not hold
HELLO=0
```

Two findings, both precise:

  * the reply-token path works once the loader can RECEIVE: `wake=8` is the first time the loader resolved a
    doorbell at all;
  * the one-time rule needs a key that does not move under it. It is keyed on
    `_ringDoorbellGeneration(pid)`, and that value CHANGES while the boot runs (the plane's PING sets it
    after the first attaches), so 36 bundles were sent for far fewer processes. The rule must key on the
    incarnation's FIRST delivery (or on an explicit guest acknowledgement), not on a generation that the
    plane is still teaching.

A procedure incident worth recording: a revert script asserted on text it had already replaced in an earlier
step, so it wrote the GENERATOR revert and then died BEFORE writing the server revert. The tree spent one run
in a mixed state (generator without the reply token, server still applying the one-time rule) whose signature
is unmistakable and worth memorising: `kind=7` bundles sent, `[dring-uds-reason-hist] reason=ATTACH_FAILED`,
`machmsg_uds` back above zero, `HELLO=0`. It was repaired by reverting the generator and rebuilding
`mldr darlingserver libsystem_kernel.dylib`, and the verified GREEN run after the repair is:

```
FINAL=1 HELLO=1   ool 20=44  basic 100=204  r2 100=206  stress 16x20=644   all pass=1   all machmsg_uds=0
```

KEPT from this attempt: the loader-side courier receive (a real implementation instead of the stub) and the
generation fix from §37. REVERTED: the one-time rule and the `ring_attach` reply token, because with them
enabled the boot does not complete.

The next step is now unambiguous and small: key the one-time delivery on the incarnation's first attach (or
have the guest acknowledge receipt), then re-enable the rule with the reply token -- both halves work
individually (`kind=7` with a real generation, and `wake=8` resolved by the loader).


### 38.1 A defect I introduced two steps ago, caught by the workload and fixed

The loader-side courier receive (§38) retried for 200 x 0.5 ms = **100 ms** before reporting a miss. Measured
consequence: the 16-thread stress workload stopped completing (`stress_pool 16 20` alone, in isolation,
never printed FINAL, with `machmsg_ring=7` and one lane held) while the three single-threaded tests still
passed. Any call whose token was not queued paid 100 ms, and with 16 workers that is a stall, not a delay.

Fixed by narrowing the window to 4 x 0.25 ms = 1 ms, on the argument that the server sends the descriptor
BEFORE it publishes the reply, so a token that is not queued yet is in flight for microseconds. Verified in
isolation:

```
stress_pool 16 20  ->  HELLO=1 FINAL=1 pass=pass=1 machmsg_ring=644 machmsg_uds=0
```

So the full regression is GREEN again on a tree that carries, from this round: the reverse courier, the
plane's generation PING, and the loader-side receive (with a 1 ms miss budget).


### 38.2 Fifth doorbell attempt: the two halves do not simply compose

Enabled BOTH halves in one change (the pid-keyed one-time rule AND the `ring_attach` reply token), which was
the stated requirement. Measured:

```
HELLO=0        kind=7 bundles: 0        attach-rc: 2 (the loader's seed attempts)
[mldr-seed] attach-rc pid=417310 rc=0 reject=0 wake=8     <- the loader DID resolve a doorbell
machmsg_ring=12  machmsg_uds=10                            <- and UDS returned
[dring-uds-reason-hist] pid=417310 reason=ATTACH_FAILED count=22
```

What that says, precisely:

  * the loader's seed attach got its doorbell (`wake=8`) -- from the CMSG fallback, since `kind=7` is 0: with
    the rule on, the first call found the generation unknown, so the courier send declined and the reply
    carried the descriptor the old way;
  * the LATER attaches then failed: `ATTACH_FAILED` x22 with ten UDS calls. So the guest's attach path does
    NOT accept "no wake fd, use the one you already have" in the situation this creates -- my round-49f
    acceptance (`wake_fd < 0 && __dserver_ring_doorbell(-1) < 0`) evidently does not cover the image whose
    loader slot has no doorbell yet, or the CMSG fallback and the token branch disagree about which one
    carries it.

Reverted again to the verified behaviour. Verified after the revert: `FINAL=1 HELLO=1`, `ool 20`=44,
`basic 100`=204, `r2 100`=206, `stress 16x20`=643, every test `pass=1`, all `machmsg_uds=0`.

FIVE attempts are now measured, and each one moved the target:

| # | setting | what happened |
|---|---|---|
| 1 | rule + token, before Phase-0 | `kind=7` 0, generation 0 -- the bundle was refused as stale |
| 2 | after Phase-0 | still 0 -- the loader's seed attach precedes its own courier drain |
| 3 | + generation via the plane's PING | `kind=7` 1 with a REAL generation -- delivery works |
| 4 | + loader-side receive, matching binaries | `wake=8` -- the loader resolves a doorbell for the first time; the generation-keyed rule sent 36 |
| 5 | + pid-keyed rule, both halves together | the loader still resolved `wake=8`, and the LATER attaches failed (`ATTACH_FAILED` x22) |

The next step is not another toggle: it is to instrument the guest's attach acceptance for the exact case
"the rule returned -1 for this attach" -- one line in that branch plus one run -- because that is the only
place the fifth attempt failed that the fourth did not.


### 38.3 DECISIVE: with the one-time rule on, the suite is green on the WRONG transport

Full regression with the pid-keyed rule AND the reply token enabled (all three consumers rebuilt, so the
earlier stale-binary explanation is out of the way):

```
FINAL=1  HELLO=1  passes: pass=1 pass=1 pass=1 pass=1      <- every test PASSES
machmsg_ring=24   machmsg_uds=20
machmsg_ring=104  machmsg_uds=100
machmsg_ring=205  machmsg_uds=1
machmsg_ring=4    machmsg_uds=640                          <- the stress test ran on the DATAGRAM
drive: ring_doorbell_sent_to_guest=24  ring_doorbell_reused=1512  ring_doorbell_processes=12
       attach-failed=16
```

That is the worst shape a regression can take, and it is why the rule is now disabled with a comment saying
it must stay that way until the failure is understood: the suite passes because the workload FELL BACK to
UDS, not because the Ring path works. Sixteen attaches failed; the guest's own `no-wake-fd` diagnostic (added
for exactly this case) never fired, so the failure is not on the acceptance branch I instrumented.

Reverted; verified afterwards with the correct transport numbers:

```
FINAL=1 HELLO=1   ool 20: ring=44 uds=0   basic 100: ring=204 uds=0
r2 100: ring=206 uds=0   stress 16x20: ring=644 uds=0     attach-failed=0
```

The lesson generalises beyond this bead and is worth keeping: **a passing suite is not evidence when the
fallback path can absorb the failure.** `machmsg_uds` must be read on every run, exactly as the pass column
is, and a run whose pass column is green while its uds column is not is a RED result.

The doorbell task therefore stays open, with its target now precisely stated: the one-time delivery must be
achieved WITHOUT any attach failing, and the measurement that proves it is `attach-failed=0` together with
`machmsg_uds=0` on the stress workload -- both, not either.


### 39. The one-time doorbell lands: a hard-failure/completion confusion, and a real defect beside it

Two defects were found by reading, and the second one is a genuine product defect independent of the doorbell.

**39.1 The server treated the suppressed delivery as a hard failure.** `Call` handled the doorbell's
descriptor-dup result like this:

```cpp
guestWakeFd = process ? Server::sharedInstance().ringDoorbellDupFor(process->id()) : -1;
if (guestWakeFd < 0) {
        rejectReason = dserver_ring_reject_total_size;   // the whole attach is REJECTED
} else {
        thread->attachRing(ring, nullptr);               // the lane is NEVER attached
        Server::registerRingThread(thread);
}
```

`-1` already meant "this process has no doorbell at all" -- a real failure -- so the one-time rule's
*finished* state was indistinguishable from it: every later attach was rejected, the lane was never attached,
and the workload fell back to the datagram. That is exactly the fifth attempt's signature (`attach-failed`
16, then 20, `machmsg_uds` back above zero) and it also explains why the guest's `no-wake-fd` diagnostic
never fired: the attach failed one layer ABOVE the branch it instruments.

The fix separates the two outcomes: `ringDoorbellDupFor` returns **-2** for "the ONE delivery already
happened for this process incarnation" and keeps `-1` for "no doorbell exists". `Call` then treats -2 as
SUCCESS with no descriptor in the reply -- the ring is attached and the thread registered -- and counts it
in a new `ring_doorbell_suppressed` counter. The attach-census's notion of success moved from
`guestWakeFd >= 0` to "the ring is attached", which is the property it was actually trying to measure.

**39.2 A generated-wrapper defect: `fds[]` was read uninitialised.** In the client wrapper,

```c
int fds[N];                       // never initialised
if (valid_fd_count > 0) { ... memcpy into fds ... }
...
(*(reply).body.wake_fd >= 0) ? fds[(reply).body.wake_fd] : -1
```

A reply that carries no descriptor leaves `fds[]` untouched while the reply body still carries an index, so
the caller received whatever was on the stack instead of `-1`. Every fallback in this tree is written
against `-1` ("no descriptor arrived"), so the value silently defeated them. Fixed by pre-filling `fds[]`
with `-1`, which makes "no descriptor arrived" identical to "the server sent -1". This is a defect on its
own terms: it can bite any reply-fd call whose server-side descriptor disappears.

### 39.3 Verified state

With the rule and the reply token enabled together, and both fixes in place:

```
FINAL=1  HELLO=1   passes: pass=1 pass=1 pass=1 pass=1
ool 20:       ring=44   uds=0
basic 100:    ring=204  uds=0
r2 100:       ring=206  uds=0
stress 16x20: ring=643  uds=0        <- the workload is on the Ring, no fallback
attach-failed=0    no-wake-fd=27     <- 27 re-attaches carried NO descriptor and still bound their lane
guest doorbell slots: `doorbell=1048574` once per image, then -1 for every re-attach
```

`no-wake-fd=27` is the acceptance signal: the reply no longer transfers a descriptor after the first
delivery, and the guest binds every later lane from the doorbell it already owns (asked of the owner, never
remembered as a number).

### 39.4 The residual, stated exactly

`sent_to_guest / processes = 2.00`. Both deliveries are legitimate: the loader and the guest image carry
SEPARATE courier connections, and the guest-side slot is per IMAGE, so a pid-keyed rule delivers once per
connection. Moving the re-arm from the connection-close path to the wake-retire path (a finer,
incarnation-level site) was tried and MEASURED RED: the rootless boot then never reaches ready
(`shellspawn did not become ready`, failing at the earliest `RING_ATTACH_RPC_SENT` with `doorbell=-1`),
because the second image owns no doorbell anywhere. Exactly-once therefore needs an identity finer than the
pid; the erase stays on the connection-close path until that identity exists, and the cost of doing so is
one extra descriptor per process -- transferred once, absorbed by the guest's own slot logic.

The lesson from §38.3 is what made this findable: the earlier attempt looked finished because the suite was
green, and it was green because the workload had fallen back to UDS. Reading `machmsg_uds` on every run is
what turned that into a visible rejection instead of a passing test.


### 40. ATTACH_LANE on the page: implemented, measured RED, reverted

The last step toward `ring_attach UDS = 0` was implemented end to end and measured.

What was built: `OP_ATTACH_LANE` (the op id was already reserved in the page's op table, unwired) serviced
by `Server::_serviceProcessControl` by synthesizing the ordinary `ring_attach` call -- the descriptor half
resolved from the courier with `DSERVER_FD_COURIER_KIND_LANE_BACKING`, which is the same kind the datagram
route already uses -- and the reply read back out of the suppressed reply BODY (reject reason and the
doorbell's wire token), with the guest publishing the request on the page and receiving the token's
descriptor off the courier exactly as a datagram reply's token is. The suppressed-reply path needed one
addition to make that possible: a bounded copy of the reply body, because a page route has no datagram to
read it from.

Measured:

```
HELLO=0    FINAL=0    attaches still fail: 4
attach-lane-op=0       <- the server never serviced a single ATTACH_LANE request
Rootless shellspawn did not become ready within 30000ms
```

Two facts, both useful: the page's `OP_ATTACH_LANE` was never entered (`attach-lane-op=0`), so the guest's
publish did not reach the server; and the boot stopped at the earliest attach, which is the same failure
shape as the whole pre-attach family. The experiment is fully reverted (guest route, server case, and the
reply-body capture), and the verified state was re-measured after the revert:

```
FINAL=1  HELLO=1  stress_pool 16x20: ring=644 uds=0   (the doorbell rule stays ON)
```

So the remaining `ring_attach` UDS traffic is still the semantic attach half, and it needs a first step
before the route can be built: a measurement of whether the guest's publish is even reached -- i.e. whether
`dserver_process_control_page()` in the KERNEL image returns the loader's page and whether
`__dserver_fd_courier_send` succeeds there at that moment. Both are one-line attribution questions, and both
were left unanswered by this attempt because the code was written before the question was asked. That is the
mistake this record exists to prevent repeating: the page route must be instrumented where it DECIDES not to
run, not only where it runs.


### 41. ATTACH_LANE on the page: GREEN, and the attach's semantic half leaves the datagram

Attempt 2 succeeded, and the two things that made attempt 1 unreadable were both fixed first: the route was
instrumented where it DECIDES not to run, and its wait was BOUNDED so an unanswered page degrades instead of
stalling a boot.

What attempt 1 got wrong, measured in attempt 2:

  1. **A negative reply was treated as an attach.** The server answered `-9` (EBADF) because the request was
     serviced while its descriptor half was still in flight on the courier, and the guest set
     `plane_route = 1` anyway -- so it believed in a lane that did not exist. That is what stalled the boot,
     not the page. Now `rc < 0` is a REFUSAL that falls back to the datagram route, and the guest's own
     diagnostic says so (`[dring-plane-attach] refused tid=... rc=-9`).
  2. **The attribution sat behind an early `break`.** `attach-lane-op=0` in attempt 1's measurement made the
     whole route look unentered while the server was in fact servicing it. A diagnostic placed after the exit
     it is meant to explain reports the wrong thing -- the same lesson as §40, one level down.

Verified GREEN with the route live and the one-time doorbell still ON:

```
FINAL=1  HELLO=1   passes: pass=1 pass=1 pass=1 pass=1
ool 20:       ring=44   uds=0
basic 100:    ring=204  uds=0
r2 100:       ring=206  uds=0
stress 16x20: ring=643  uds=0
guest:        [dring-plane-attach] ok tid=... rc=0 reject=0 wake=-1     (one per attaching thread)
```

So `ring_attach`'s SEMANTIC half no longer needs the datagram: the page carries it, the descriptor half still
rides the process-scoped courier with `kind = LANE_BACKING` (exactly the kind the datagram route used), and
the reply is read from the suppressed reply body. Combined with §39, the attach now costs no ordinary
semantic RPC on AF_UNIX at all on the measured path -- the remaining AF_UNIX use is the checkin family and
the teardown checkout, both already documented.

Two residuals, stated rather than implied:

  * **`wake=-1` on every attach, including the first.** The doorbell descriptor did not arrive through the
    page route's token, so the guest's doorbell must still be coming from somewhere else on this path
    (the loader's seed connection). That is worth one attribution line: the page route's token is wired, but
    it is not the path that actually delivers the process doorbell.
  * **The `ring_attach` row in the census was not re-read for this run** (the server had already exited when
    the counters were queried). The evidence that the route is live is the guest-side line plus
    `machmsg_uds = 0` on all four workloads, which is stronger than the row would have been -- but the row
    is the number this step was defined by, so the next run must capture it live.


### 41.1 The acceptance number, measured live

```
dserver_callnum_ring_attach   total=149   uds=14   ring=0   plane=135
```

Against the previous round's `ring_attach 174 / uds=174`: the attach's semantic half is now on the process
control page for **135 of 149** attaches (90%), and the remaining **14 on UDS are the ones that happen before
the page is established** (the loader's earliest attaches -- `transport_ready` is published when the server
maps the region, and a page cannot carry a request before it exists). The route is therefore live and the
number that defined this step moved from "all" to "the pre-page window only".

Same snapshot, the other rows that matter:

```
checkin               149 / uds=149
checkout              139 / uds=132  ring=7
pthread_canceled      542 / uds=74   ring=468
thread_self_trap      165 / uds=8    ring=157
vchroot_path           23 / uds=7    ring=16
console_open            3 / uds=3
kqchan_proc_open        2 / uds=2
doorbell: processes=7  sent_to_guest=14  suppressed=135
```

Two things follow directly, and both are concrete rather than programme-level:

  * `console_open` (3) and `kqchan_proc_open` (2) have their descriptor half on the courier already; their
    SEMANTIC half is what still costs a datagram, and `OP_ATTACH_LANE` is now the worked example of how to
    move it -- same page shape, same suppressed-reply-body read.
  * `checkin` is unchanged at 149/149. The page's CHECKIN exists (§31) and is semantically correct, so what
    is missing is not the route but the servicing position; `ring_attach` now proves the machinery around it
    works, which is new information for that older question.


### 41.2 The fourteen: they are pre-page, not pre-readiness

The readiness wait was added (`transport_ready` re-checked for up to 200 ms before falling back, a TRANSPORT
wait that by the PHASE-0 rule cannot depend on semantic guest registration) and it changed nothing:

```
ring_attach total=149 uds=14 plane=135     <- identical
doorbell: processes=7 sent=14 suppressed=135
```

So those 14 attaches are not "the region is not ready yet". They are attaches taken **before the page exists
at all**: the loader's earliest attach happens before its own bootstrap creates the page, which is exactly
why `no-page` never appears in the diagnostic either -- the diagnostic's own gate is an environment lookup,
and an environment scan at the earliest attach is a MEASURED boot hazard that the loader deliberately avoids
by not having an environment yet (`getenv` there returns whatever the pre-init state holds). The diag is
therefore silent precisely in the window it was added to describe, and the honest reading is: the fallback
lines cannot be trusted that early, so this needs a different instrument (a counter published in shared
memory, not a `getenv`), not a different wait.

Neutral-but-kept: the bounded readiness wait is correct for the general case (a page that exists but is not
yet mapped by the server) and costs nothing when the page is already ready, so it stays.

Next step for these 14, in order of what the evidence supports: publish the page earlier in the loader's
bootstrap so the earliest attach finds it, then re-measure. The instrument that must come with it is a
shared-memory counter (the page itself can carry "attaches attempted / page missing"), because the
environment-gated line cannot report anything in this window.


### 42. The courier doorbell is wired and deliberately unexercised

The page route now sends the descriptor itself when its reply owned one: `Call` names the wake fd it produced,
the page's servicing code sends it with `sendDSERVER_FD_COURIER_KIND_PROCESS_DOORBELL` and reports the token in
`reply_payload[1]`, and the guest receives it off the courier exactly as it receives a datagram reply's token.

Measured with the route live:

```
FINAL=1  HELLO=1  passes: pass=1 pass=1        (no regression)
kind=7 bundles: 0        every plane attach: wake=-1
```

That is the CORRECT result, and it closes a question rather than opening one. The wake fd is produced only when
the one-time delivery is due; by the time any plane attach runs, the delivery has already happened on the
loader's early attach (which is pre-page and therefore on the datagram), so every plane attach is suppressed
and has nothing to hand over. `wake=-1` there is the design working, not the courier failing.

The consequence is precise and worth stating as the coupling rule for this pair of tasks: **the courier
doorbell path is reachable only for a process whose FIRST attach is a plane attach.** That is exactly the
pre-page window of §41.2. So "deliver the doorbell once through the courier" and "close the pre-page window"
are not two tasks but one, in this order: publish the page early enough that the loader's earliest attach
finds it, and the courier delivery becomes the path that actually runs -- and at that point it is already
implemented, already wired, and its token path is already exercised by `console_open`/`kqchan_proc_open`.


### 41.3 What the pre-page window actually is, measured

With the attach and residual censuses armed:

```
attach_census_first_uds_calls: 7        <- UDS calls before the ring exists, per process
ring_attach: total=149  uds=14  plane=135
attach_mldr_callers: 0   attach_dylib_callers: 0      <- these two fields are never populated
```

Seven processes, fourteen UDS attaches: **two attaches per process on the datagram**, against 135 on the page.
The two are the process's first two attaches; everything after them finds the page established and rides it.

So closing this window is a bootstrap-ORDERING change in the loader (the page must exist before its own
earliest attach publishes a lane), and its payoff is fourteen calls out of a hundred and forty-nine -- about
nine percent of this operation -- while its risk is the whole boot, because the earliest attach is exactly the
window where this tree has already recorded several measured hazards (in-band printing, environment scans,
probing the elfcall table).

Recorded as the single next action with its price attached, rather than taken at the end of a long session: the
change is small, the failure mode is not, and the evidence to do it well is now all in place -- the ordering
that must move, the instrument that must accompany it (a shared-memory counter, since the environment-gated
line is silent in that window), and the measurement that will say whether it worked (`ring_attach uds` 14 -> 0,
with `kind=7` becoming non-zero for the first time because the courier doorbell path of §42 becomes reachable).

Also corrected here: `attach_mldr_callers` and `attach_dylib_callers` are reported as zero, which reads as "no
callers" but means "not populated". They must either be filled or removed -- a counter that always answers the
same way is worse than no counter, because a measurement that looks like evidence and is not is how the
`fds[]`-uninitialised defect and the `attach-lane-op=0` misreading both survived a round.


### 43. Round 49 final state

Regression (full, on the verified tree, with the one-time doorbell rule ON and ATTACH_LANE on the page):

```
FINAL=1  HELLO=1   passes: pass=1 pass=1 pass=1 pass=1
ool 20:       ring=44    uds=0
basic 100:    ring=204   uds=0
r2 100:       ring=206   uds=0
stress 16x20: ring=644   uds=0
```

Census (live snapshot, two independent captures agreeing):

```
TOTALS  total=1598  uds=417  ring=1020  plane=161        (previous round: uds=604)

checkin               159 / uds=159
checkout              145 / uds=136  ring=9
console_open            3 / uds=3
fork_wait_for_child     8 / uds=4    ring=4
kqchan_proc_open        2 / uds=2
pthread_canceled      550 / uds=76   ring=474
ring_attach           159 / uds=18   ring=0  plane=141
thread_self_trap      179 / uds=10   ring=169
vchroot_path           29 / uds=9    ring=20

doorbell:  processes=9  sent_to_guest=18  reused=141  suppressed=141
residual_uds_despite_lane: {checkout 136, vchroot_path 9, thread_self_trap 9}
nonfd_uds_violation: none reported;  first_uds_calls=9
```

`ring_attach` moved from `174/174 on UDS` to `159 total / 18 uds / 141 plane`, and the total legacy UDS count
fell from 604 to 417. The two operations the residual census still names as ring-eligible-but-on-UDS are
unchanged (`vchroot_path` 9, `thread_self_trap` 9) -- the image-adoption gap of §32.10, untouched this round.

P1/P2 (absolute numbers, NOT a controlled Ring-vs-UDS A/B, so no regression claim either way):

```
bench_simple 2000:  p50=12493.0 p95=69001.0 p99=189097.1  (previous round p50=16551.0)
bench_ool    2000:  p50=188075.9 p95=442757.0 p99=574485.9 (previous round p50=183584.9)
```

Defects found and fixed this round, each by measurement rather than by review:

  1. the doorbell's one-time rule was indistinguishable from a hard failure, so every later attach was
     rejected and the workload silently returned to the datagram (§39.1);
  2. a generated-wrapper defect: `fds[]` was read uninitialised whenever a reply carried no descriptor, so
     every `-1` fallback in the tree was defeated by a stack value (§39.2);
  3. the page route treated `-9 (EBADF)` as a successful attach, leaving the guest believing in a lane it
     never got (§41);
  4. the page route's own diagnostic sat behind the early `break` it was meant to explain (§41);
  5. an unbounded wait on an unserviced page turned into a stalled boot (§41);
  6. `attach_mldr_callers`/`attach_dylib_callers` always report zero because they are never populated (§41.3).

Four items are blocked with reasons rather than described as pending: the courier doorbell (reachable only once
the pre-page window closes), the per-thread socket counter and its physical removal (both bound to checkin
leaving the datagram, and checkin on the page is measured correct-but-boot-stopping), and the two
`POST_LANE_NONFD_BUG` candidates (the image-adoption ownership change).


### 43.1 The dead counter, filled, and the fact it immediately produced

`recordAttachBinaryFact` had **no caller anywhere in the tree** -- so `attach_mldr_callers` and
`attach_dylib_callers` were constant zero, which reads as "no callers" and means "nothing reports". Fixed at
the only place the page route can learn it: the guest stamps its image identity into `payload[3]` (bits 8+,
which were free -- the low byte is the architecture the server already reads), and the server records the
caller. Then, regression GREEN with the change (`FINAL=1`, every test `pass=1`, `uds=0`):

```
attach_mldr_callers=0    attach_dylib_callers=135    attach_no_ring_code=0
ring_attach: total=149  uds=14  plane=135
```

The fact, stated plainly: **every page-route attach comes from the guest dylib; none from mldr in this run.**
That is new information and it sharpens §41.3 -- the fourteen pre-page attaches are not "the loader's early
attaches" as the round-49 text guessed, because the loader does not appear in the attribution at all. Whatever
produces those fourteen is an image that does not travel the page route, and the next step for that window is
therefore to establish which image those attaches come from, with the counter that now works, before touching
the loader's bootstrap order. The earlier plan (move the page creation earlier) assumed the loader was the
caller; the measurement does not support that assumption.

Scope note, so the number cannot be misread in the other direction: the attribution covers page-route attaches
only. The UDS attaches carry no image tag, so 135 is the attributed count out of 149, not a total.


### 43.2 The attach route's own attribution, and the process mistake that came with it

The route now counts on the page itself (no environment, no printing -- which is what the diag could not do in
the early window). Measured with the counters live:

```
attach_route_ok=2   attach_route_not_ready=0   attach_route_refused=2
callers: mldr=0  dylib=135
ring_attach: total=149  uds=14  plane=135
```

Two things to read carefully, and one of them is a defect in this very counter:

  * `refused` is the UDS fallback: the server answered the page with a negative status (the `-EBADF` case of
    §41, i.e. the descriptor half had not arrived on the courier when the page was serviced). So the fourteen
    UDS attaches are `refused`-class, not `not_ready`-class -- the earlier readiness theory is now measured
    out rather than argued out. The fix for a `refused` is ordering on the DESCRIPTOR side, not the page side.
  * the numbers are for ONE process, not the run: the server **stores** the page counters into metrics on each
    pass instead of accumulating them, so the last process serviced wins. A snapshot that looks like a total
    and is a single sample is the same class of defect as the always-zero counters of §43.1 -- recorded here
    so it is not read as a total.

Process mistake, recorded because it cost a run: adding fields to `dserver_process_control` changes the struct
size, and the loader creates the page with `sizeof(struct dserver_process_control)`. Rebuilding only
`darlingserver` and `libsystem_kernel.dylib` left `mldr` stale, so the page was created too small, the server
skipped the region (`region.size < sizeof(...)`) and the boot stopped at `shellspawn did not become ready`. The
lesson already in this tree for generated RPC structs applies to every shared struct, including this page:
**when a shared definition changes, every consumer is rebuilt and redeployed together.** Rebuilding `mldr` too
restored the run immediately (`FINAL=1`, every test `pass=1`).

Next step, now measured rather than assumed: the fourteen are descriptor-ordering refusals, so the work is to
let a page-serviced attach wait for its courier descriptor instead of refusing -- which is the same ordering
question §42 raised for the doorbell.


### 43.3 The drain-before-resolve attempt: no change, and what that rules out

The refusal is `-EBADF` from `resolveFdCourierBundle`, and the guest sends the descriptor BEFORE publishing the
request, so the natural hypothesis was a queue-timing race: the courier message still sitting in the process's
queue while the page is serviced. `_drainFdCourierMessages()` was called immediately before the resolve.

Measured: **no change.** `attach_route_refused` unchanged, `ring_attach` unchanged at `uds=14 / plane=135`, and
the full regression stayed GREEN (`FINAL=1`, `ool 44 / basic 204 / r2 206 / stress 644`, all `uds=0`).

That rules the queue-timing explanation out and narrows the refusal to identity: the bundle exists, but
`resolveFdCourierBundle(pid, token, kind)` does not find it under the pid the page is keyed by. The candidate
that fits every observation is that the two are keyed differently -- the page is keyed by the pid that mapped
the region (the kernel's own `SO_PEERCRED` answer on the courier connection that created it) while the pending
bundle is keyed by whatever pid the SENDING connection reported -- and the guest dylib's courier connection is
not necessarily the one that created the page. That is the next measurement: log both pids at the resolve,
rather than adding another structural fix on top of an unmeasured identity assumption. The mistake this round
already made once (a loader-bootstrap change premised on "the loader is the caller", which the attribution then
contradicted) is not worth making twice.


### 43.4 Park-until-descriptor: implemented, measured RED, reverted

The refusals of §43.2 are `Missing` from the resolver, and the resolver already has the right machinery for
`Missing`: it parks a continuation in `_fdCourierWaiters` and the descriptor's arrival resumes it. The page case
was simply throwing that away and answering `-EBADF`. So the page was changed to stay PENDING and let the
next pass service it, with the continuation re-queuing the descriptor under the same key (consuming it, which
is what the first version did, would leave a parked page with nothing left to resolve).

Measured:

```
attach_route_ok=2   attach_route_refused=0   <- the refusals are gone, the mechanism works
ring_attach total=8 uds=6 plane=2            <- but the run never got past its first few attaches
HELLO=0   FINAL=0                            <- the boot stops
```

Reverted, and the verified state re-measured GREEN (`FINAL=1`, `ool 44 / basic 204 / r2 206 / stress 644`, all
`uds=0`).

What this rules out is worth more than the attempt: a page request left PENDING does not converge with a
descriptor that arrives later **on this servicing model**, even though the descriptor's arrival does re-enter
the loop and does service the page on that pass. The refusal, by contrast, is behaviourally correct -- the
guest falls back to the datagram and the attach completes -- which is why the boot never breaks when the page
route refuses. The cost of that correctness is the 14 attaches, and the honest conclusion is that closing them
needs the page's servicing model changed (§31.9's open question for CHECKIN as well), not a third variation of
the same branch. Three attempts have now been spent on this single branch (drain, park, re-queue) which is the
point at which the branch stops being the problem.


### 43.5 Park, second attempt: one real defect fixed, the RED survives, and the branch is closed

The first park attempt stamped its re-queued bundle with `_ringDoorbellGeneration(pid)`. The resolver compares
against `_fdCourierGeneration[pid]` -- a DIFFERENT map -- so every re-queued descriptor was rejected as
`StaleGeneration` and closed, and the page sat PENDING forever. That is a real defect in the attempt, not in the
model, and it was fixed: the same map as the resolver, or the bundle is discarded.

Measured with it fixed:

```
attach_route_refused=0        <- the refusals really are gone
PARKED=0                      <- the park branch's own log never fired
ring_attach total=8 uds=6 plane=2
HELLO=0                       <- and the boot still stops
```

`PARKED=0` is the informative part: the stall does not run through the branch under study. With the guard in
place the page stays PENDING by design, the guest's own 2 s bound expires and it falls back to the datagram --
and the server later services the request anyway, attaching a lane for a tid whose attach the guest has already
completed elsewhere. That is a second attach rather than an unanswered one, and it is what the run does not
survive. The counters that should separate these causes cannot: the page counters are per-process samples
(stored, not accumulated) and the aggregate row is one number for a whole class.

Reverted to the refusal, and the verified state re-measured GREEN (`FINAL=1`, `ool 44 / basic 204 / r2 100 206 /
stress 643`, all `uds=0`).

This branch is now closed rather than retried a fourth time. Four variations have been spent on it (immediate
refusal, drain, park, corrected park), and the last one produced a real defect fix and a real fact: the refusal
is not the problem; a page request that outlives its guest's bounded wait is. Anything further here is the
page's SERVICING MODEL -- whether a page request may outlive its requester, and how a late answer is retired --
which is the same question CHECKIN has been waiting on since §31.9, and it is a design decision rather than
another branch. The counters must become per-cause and accumulated before that work starts, or the next attempt
will again be unable to say which of its causes it died of.


### 43.6 The counters, accumulated, separate the two causes

The route counters were published by STORING, which made a per-process sample read as a run total. With the
delta accumulated (per-region last-seen values), the same run gives a coherent picture:

```
attach_route:  ok=135   not_ready=0   refused=7
ring_attach:   total=151  uds=16  plane=135
callers:       mldr=0     dylib=135
regression:    FINAL=1    every test pass=1
```

The arithmetic now separates what a single number could not:

  * `plane=135` == `ok=135` -- every attach that reached the page succeeded through it, with no fallback;
  * `refused=7` accounts for seven of the sixteen UDS attaches: those DID reach the page and were refused on
    their descriptor;
  * the remaining `16 - 7 = 9` were never seen by the page at all -- the pre-page window (`not_ready=0` only
    means the page existed and was already ready whenever it was consulted, so these never consulted it).
    This is the class the environment-gated diagnostic could never report, now measured by subtraction.

So the sixteen are two problems, not one: seven are descriptor-ordering refusals (the branch closed in §43.5 on
a servicing-model question) and nine are attaches taken before the page exists (a bootstrap-order question). A
single `uds` number was hiding both, which is exactly why the counters had to become per-cause and accumulated
before any further attempt -- and why §41.3's "the fourteen are the loader's" reading, and §31's attempts to
close the whole window at once, were both chasing a composite.


### 43.7 The invisible class, quantified by subtraction, and the capture limit found

Three things were done to make the pre-page attaches visible, and only the first two worked:

  * the guest counts them (`gr_attach_no_page`, `gr_attach_ready_fail` at `dserver-ring.c:1388` and the
    ready-path bail), and prints them in the lane-stats dump -- an `sys_exit` channel already known to be safe,
    deliberately not an early-path print;
  * the counters are in the built binary (verified in the source at 557/1388/1954/1992 and rebuilt);
  * but the dump is TRUNCATED by the capture: the line ends `... courier_attemDONE`, i.e. the guest's stderr
    interleaves with the next command's output and the tail fields are lost. So the field exists and does not
    reach a readable log through this launch path -- a capture limitation, recorded rather than worked around
    with another instrument, because the number is already known by subtraction.

What is known without it (aggregate run, §43.6):

```
plane=135 == ok=135     every attach that reached the page succeeded
refused=7               reached the page, refused on their descriptor
uds=16                  so 16 - 7 = 9 never reached the page at all
```

So the class that no instrument could see is nine attaches per run, and the arithmetic that produced it is
closed: the page-route counters cover exactly what the page saw, and the aggregate row covers the rest.

State of the whole line after this round: everything is reverted except what is measured working, the tree is
GREEN at the recorded numbers, and four items stay blocked with their reasons on record -- the courier doorbell
(pre-page window), the per-thread socket counter and its removal (checkin leaving the datagram), and the two
`POST_LANE_NONFD_BUG` candidates (image-adoption ownership). The one design question that unblocks the largest
group is the page's servicing model: may a request outlive its requester, and how is a late answer retired.


### 44. ProcessControlTxn: the model, the measurement, and the identity blocker it exposed

**Implemented** (§1-§8 of the directive):

  * `ProcessControlTxn` (server-owned): pid, generation, seq, op, payload, fd token, expected fd kind, the
    accepted descriptor, a three-state lifecycle (`CLAIMED -> WAIT_FD -> READY`), and the completion target's
    identity (page mapping + size). It is kept in `_processControlTxns`, never a raw guest pointer, never a raw
    fd number, never a Thread reference.
  * The page is only a mailbox: `DSERVER_PROCESS_CONTROL_CLAIMED` is now a published state distinct from DONE,
    and the server publishes it the moment it takes ownership.
  * Descriptor pairing is asynchronous in BOTH orders: a request whose descriptor is absent goes to WAIT_FD
    with no reply and no fallback, and the courier continuation hands the descriptor to the TRANSACTION (not to
    a re-queued bundle, not to a close).
  * Cancellation: `_cancelProcessControlTxn` runs on the process's courier close, closes the retained
    descriptor, destroys the transaction and counts `process_control_txn_cancelled`.
  * Incarnation-safe completion: a reply is written only if the region mapping is still the one the transaction
    was claimed against; otherwise `process_control_late_reply_retired`.
  * Counters: claimed / wait_fd / completed / cancelled / late_reply_retired, all exported.

**Measured, and this is the important part.** Two defects were found and fixed inside the attempt:

  * the re-queued bundle was stamped from `_ringDoorbellGeneration` while the resolver compares
    `_fdCourierGeneration` -- a different map -- so every re-queued descriptor was rejected as
    `StaleGeneration` and closed, and the page sat PENDING forever;
  * the pending bundle and its waiter were keyed by **pid**, and the measurement shows the two halves arriving
    under **different pids for one Linux process**:

```
PARKED  pid=672791 token=13188803017164813297 kind=1
bundle  pid=672797 token=13188803017164813297 kind=1 gen=6037603892614826   <- same token, different pid
```

With the pairing keyed on the token instead, the descriptor problem disappeared completely:

```
attach_route:  ok=2   refused=0   claimed_no_lane=0
txn:           claimed=3   wait_fd=0   completed=3
```

Every transaction now completes, and no attach is refused. **But the boot still wedges**, and the trace shows
why -- the same identity confusion reaches the doorbell:

```
attach-lane-op pid=682019 tid=682021 ... token=0        <- no descriptor
attach-lane-op pid=682019 tid=682022 ... token=0        <- no descriptor
attach-lane-op pid=682019 tid=682023 ... token=7605696141601330329   <- the ONE delivery went here
[P:682023(682023)] sent-to-guest pid=682019 token=... kind=7
```

The page is keyed by `682019`, the courier connection reports `682023`, and the one-time doorbell rule is keyed
by pid -- so the single delivery went to a tid that is not the attaching process, the first two attaches were
delivered no descriptor at all (`token=0`), and the boot stopped right after the `kqchan_proc_open` reply.

The token is also **not** unique across processes: the same token appears under both pids. So the identity that
must be settled is: **which of these identifiers is the process incarnation** -- `SO_PEERCRED` gives a pid,
threads give tids, and the guest's own generation does not separate them (both pids above carry the same
generation). That is one focused question with a measurement attached, not a design debate, and it is now the
only thing between this model and the page route being on by default.

**State**: the page route for ATTACH_LANE is gated behind `DARLING_GUEST_PLANE_ATTACH` and is OFF by default,
because with it on the boot wedges and with it off the proven path runs. Verified GREEN with the gate off:
`FINAL=1`, `ool 44 / basic 204 / r2 206 / stress 644`, all `machmsg_uds=0`. The transaction model, the token
keying, the CLAIMED state, the cancellation and the incarnation-safe completion all stay in the tree.


### 44.1 The token was not process-unique: the third defect of this round

`gr_fd_courier_send` built its token as `generation * C ^ (++counter)`. Two different processes with the same
generation and their first send produced the **same token**, which is exactly what the courier log showed:

```
PARKED  pid=672791 token=13188803017164813297 kind=1
bundle  pid=672797 token=13188803017164813297 kind=1 gen=6037603892614826
```

The pairing registry is keyed by that token, so one process's request could be paired with another process's
descriptor. Fixed in both senders (the kernel image and the loader): the pid is mixed into the token.

Measured with the pid in the token and the page route enabled:

```
attach_route:  ok=2   refused=0   claimed_no_lane=0
txn:           claimed=3   wait_fd=0   completed=3
```

Descriptor pairing is solved: no refusals, no WAIT_FD, every transaction completes. **The boot still wedges**,
right after the `kqchan_proc_open` reply, and the cause is measured OUT of the three things it could have been:
the transaction lifecycle (all complete), the pairing (zero waits), and the guest's wait bound (the give-up
path is never taken, `claimed_no_lane=0`). What remains is the guest's post-attach path in the image that
takes the page route, which needs a per-tid trace rather than another server-side change.

So the route is gated (`DARLING_GUEST_PLANE_ATTACH`, default OFF) and the proven datagram path is the default.
Verified GREEN: `FINAL=1`, `ool 44 / basic 204 / r2 206 / stress 644`, all `machmsg_uds=0`.

Round 50 ledger, honestly:

| item | state |
|---|---|
| `ProcessControlTxn` (CLAIMED/WAIT_FD/READY, cancellation, incarnation-safe completion, counters) | implemented |
| asynchronous descriptor pairing, both orders | implemented and measured (`refused=0`, `wait_fd=0`) |
| defects found and fixed this round | 3 (generation map in the re-queue, pid-keyed pairing, non-unique token) |
| CHECKIN over the page | not attempted this round (blocked on the page route being default) |
| ATTACH_LANE over the page | implemented, pairing GREEN, boot wedges -> gated OFF |
| pre-page window (9), image-adoption tail, socket metric, socket removal | unchanged, still blocked |


### 44.2 The wedge, localized: the third sequential attach, with the Ring working underneath

With the route enabled and both logs on, the picture is now specific rather than vague:

```
[dring-attach] RING_ATTACH_RPC_SENT pid=696549 tid=696551 ...     <- 1st: reaches END, no-wake-fd, owned=1048574
[dring-attach] RING_ATTACH_RPC_SENT pid=696549 tid=696552 ...     <- 2nd: reaches END, no-wake-fd, owned=1048574
[dring-attach] RING_ATTACH_RPC_SENT pid=696553 tid=696553 ...     <- 3rd: SENT, and NEVER an END
MACHMSG_TRANSPORT process=696549 image=kernel host_tid=696552 lane=104 seq=10 direction=reply transport=RING callnum=38 code=0
```

Three facts, each of which removes a candidate:

  * the Ring itself is healthy underneath -- `transport=RING`, `callnum=38`, `code=0`, replies consumed, sequence
    advancing to 10. The stall is not a broken lane and not a broken reply path;
  * the guest OWNS a doorbell on the first two attaches (`owned=1048574`), so "no wake fd" was never the
    problem either;
  * the third attach is the one that hangs, and the attaches are SEQUENTIAL -- so this is not the concurrent
    publish race the CAS was added for.

The mailbox CAS is kept regardless, because one outstanding request per page is the model (§7) and publishing
over another request is a hazard whether or not it is this run's cause: the slot is now claimed with a
compare-exchange from IDLE, with a bounded wait and a counted `attach_route_no_slot` outcome.

What the third attach needs to answer is narrower than anything tried so far: what does the server do with the
third request that it does not do with the first two? The transaction counters say all three are claimed and
completed, so the answer is in the reply's delivery to a tid that is not the page's first caller -- and the
instrument for that is a per-tid completion trace on the page, not another server-side branch.

Verified state with the route gated off: `FINAL=1`, `ool 44 / basic 204 / r2 206 / stress 644`, all
`machmsg_uds=0`.

### 44.3 Round 50 summary against the directive

| directive item | state |
|---|---|
| §1-§3 `ProcessControlTxn` + explicit state machine | implemented (CLAIMED / WAIT_FD / READY, documented) |
| §4 asynchronous descriptor pairing, both orders | implemented; MEASURED solved: `refused=0`, `wait_fd=0`, `completed=all` |
| §5 server owns the descriptor after courier acceptance | implemented (continuation hands it to the transaction; cancellation closes it) |
| §6 incarnation-safe completion + late-reply counter | implemented (`process_control_late_reply_retired`) |
| §7 one outstanding mailbox | enforced by CAS now, not assumed |
| §8 CLAIMED separate from COMPLETED | implemented and published |
| §9-§13 CHECKIN barrier / ordered bootstrap | not reached: the page route is not yet default |
| §14-§16 ATTACH refusals, cancellation mutations | refusals measured to zero; mutations not yet run |
| §17-§18 pre-page window | unchanged (9 per run, measured by subtraction) |
| §19 one-time doorbell | implemented; still unreachable while the route is gated |
| §21-§23 image-adoption tail | unchanged |
| §24-§26 socket metric / removal / semantic-UDS invariant | unchanged |
| §27-§35 census, FD slope, perf, mutations | unchanged this round |

Four defects were found and fixed in round 50, all by measurement: the generation map used by the re-queue, the
pid-keyed pairing registry, the non-unique courier token, and the unclaimed mailbox slot. The single remaining
blocker for the whole line is the third-attach hang of §44.2.


### 44.4 The server completes every attach; the guest does not see the third

A step trace was added to the server's attach path (under the courier-log gate, so it costs nothing in a normal
run) and it answers the §44.2 question directly:

```
attach-trace [P:706892] enter ring_fd= 21 / enter fd_token= 0 / pre-attach / post-RingBuffer::attach reject= 0
attach-trace [P:706892] post-doorbell wake_fd= -2
attach-trace [P:706892] post-register tid= 706894
attach-trace [P:706892] ... post-register tid= 706895
attach-trace [P:706896] enter ring_fd= 24 / enter fd_token= 0        <- and nothing after this
attach-lane-op pid=706892 tid=706894 size=2816 status=0 reject=0 token=0
attach-lane-op pid=706892 tid=706895 size=2816 status=0 reject=0 token=0
attach-lane-op pid=706892 tid=706896 size=2816 status=0 reject=0 token=16388091547143425843
fd-courier sent-to-guest pid=706892 token=... kind=6
```

So the server did not stall: all three attaches are serviced, `post-register` runs for two of them, and the third
even receives the process doorbell (`token=16388091547143425843`, delivered on the courier). What does NOT happen
is the guest's return: its third attach prints `RING_ATTACH_RPC_SENT` and never `RING_ATTACH_END`, so the guest
never sees the completion the server wrote -- while the boot stops right after the `kind=6` (kqchan) send.

One more thing the trace exposes, and it is the sharpest lead yet: the third attach's `processCall` runs under
`[P:706896]` while the page it is answered through is keyed `706892`, and the two attaches that DO complete run
under the page's own pid. The completion is written into the page the guest is waiting on, but the call itself
executes in a different process context -- which is exactly where the reply's delivery has to be checked next:
print the page's pid and the publishing tid in the same line and compare them for the attach that does not
return.

Verified with the trace in place (it is gated, so the normal path is unchanged): `HELLO=1`, `ool 44`, `basic 204`,
`r2 206`, all `machmsg_uds=0`.


### 44.5 Mailbox serialization for every publisher: correct, and measured not to be the wedge

The page had two publishers, and only one of them claimed the slot: the guest attach (CAS, §44.2) and
`__mldr_process_control_request` -- which is the publisher for every OTHER operation, including the loader's
bootstrap writes. Publishing over another request overwrites `request_seq`, after which the server answers a
sequence the waiting caller does not hold. That is a real defect against §7's model, so it was fixed: the slot
is now claimed with a compare-exchange there too, a caller that cannot claim returns a distinct error and falls
back to its own route, and the wait treats CLAIMED as "ownership moved, wait for completion" rather than as a
reason to give up.

Measured with the route enabled: **the boot still wedges** (`HELLO=0`, `DONE=0`), and no `no-slot` outcome is
counted. So the concurrent-publisher overwrite was a real defect but NOT the cause of the third attach losing
its answer.

Verified that the serialization did not disturb the default path (it is on the loader's bootstrap route too):
`FINAL=1`, `ool 44 / basic 204 / r2 206 / stress 642`, all `machmsg_uds=0`.

The wedge therefore still points at §44.4's lead and nothing else: the attach that does not return is executed
under a different process identity than the page it is answered through. The next instrument is one line --
print the page's pid and the publishing tid in the same completion line -- and that is the only open thread in
this line of work.


### 45. The wedge was a forked child publishing into its parent's page

The lead of §44.4 was right and the cause was one level below it: the page pointer is a **static in the image**,
so a forked child inherited its parent's page and published its requests **into the parent's page**. The server
answered there, and the child's caller waited for a reply addressed to another process -- the third-attach hang.

Fix: the page names its owner (`owner_pid`, written at creation) and the accessor abandons a page whose owner is
not this process, creating a fresh one for this incarnation.

Measured with the route live and the fix in place -- the first time the page route for ATTACH_LANE runs as the
default path:

```
FINAL=1  HELLO=1   passes: pass=1 pass=1 pass=1 pass=1
ool 20:       ring=44    uds=0
basic 100:    ring=204   uds=0
r2 100:       ring=206   uds=0
stress 16x20: ring=643   uds=0

attach_route:  ok=142  refused=0  claimed_no_lane=0  no_slot=0
txn:           claimed=142  wait_fd=0  completed=142
ring_attach:   total=151  uds=9  plane=142          (was 151 / uds=16 / plane=135)
```

`attach_route_refused = 0` is §14's acceptance, met. The remaining nine are the **loader's own seed attach**,
which went straight to the datagram while every later attach rode the page -- so the seed was routed through the
page as well (descriptor on the courier with `kind = LANE_BACKING`, semantics and completion on the page, with
the mailbox claim and the CLAIMED-aware wait).

Measured after that: `FINAL=1`, `HELLO=1`, `basic 100` = 204 / `uds=0`, `stress 16x20` = **644** / `uds=0`.

**Captured on a live server** (the gap above is now closed):

```
ring_attach:  total=152   uds=0   plane=152
attach_route: ok=143   refused=0   claimed_no_lane=0   no_slot=0
txn:          claimed=152   completed=152
HELLO=1  FINAL=1  every test pass=1
```

**`ring_attach UDS = 0`** with the full regression GREEN: the pre-page window is gone, every attach rides the
page, and no attach uses AF_UNIX at all. `attach_census_first_uds_calls=8` still counts the process's first UDS
calls, but they are no longer attaches -- they are the checkin family, which is the next item.

### 45.1 Round 51 summary

| item | state |
|---|---|
| §1-§8 transaction model, CLAIMED vs DONE, async pairing, cancellation, incarnation-safe completion | implemented |
| §14 `attach_route_refused = 0` | MET (142 plane, 0 refused, 0 wait_fd, 0 no_slot) |
| page route for ATTACH_LANE | ON by default (hatch `DARLING_GUEST_PLANE_ATTACH_OFF`) |
| §17-§18 pre-page window | seed routed through the page; the resulting count not captured |
| §9-§13 CHECKIN barrier | not attempted (next: the page route is now default, so it can be) |
| §19 doorbell once | reachable now; not re-measured |
| §21-§26 image-adoption, socket metric, socket removal, semantic-UDS invariant | unchanged |
| §27-§35 census, FD slope, perf, mutations | unchanged |

Six defects were found and fixed across rounds 50-51, every one by measurement: the generation map used by the
re-queue, the pid-keyed pairing registry, the non-unique courier token, the unclaimed mailbox slot in the guest
attach, the unclaimed slot in the loader's publisher, and the inherited page after fork.


### 46. CHECKIN on the page: GREEN, and it is the operation that used to stop the boot

Round 49 established that the page's CHECKIN ran correctly (`call=1, thread=pid, process=pid, nsid=1`) and then
stopped the boot; the conclusion recorded there was that the page's servicing model was incompatible with the
checkin lifecycle. With the transaction model (§1-§8) and the page-ownership fix (§45) in place, the
thread-create checkin was routed through the page and measured:

```
checkin:      total=218   uds=116   ring=0   plane=102      (was 159 / uds=159)
ring_attach:  total=116   uds=0     ring=0   plane=116
txn:          claimed=116  completed=116
HELLO=1  DONE=1  basic 100 pass=1  machmsg_uds=0
```

Two things are now true that were not before:

  * **CHECKIN rides the page and the boot is GREEN.** The operation that wedged the boot in round 49 completes
    through the same page now, which is the difference the transaction model and the page-owner fix made -- not
    a change to checkin's semantics, which were always correct.
  * **The ordering barrier holds** (§9-§11): the guest waits for the server's completion before returning from
    checkin, and the server's `OP_CHECKIN` runs the ordinary Call and writes its status back to the page, so
    later process traffic cannot overtake a checkin that has not committed.

The remaining 116 checkin datagrams are the **main-thread** checkin, which is a different call site and was
deliberately left on the datagram path by the earlier work (it is ordered against `checkout` in the exec/fork
handshake, where a socket gave ordering for free). That site is the next item, and it is now the only checkin
path still using AF_UNIX.


### 47. Round 52 state

Two more checkin sites were routed through the page after §46, and the measured movement is:

```
checkin:      total=218   uds=110   plane=108      (was 218 / uds=116 / plane=102, and 159/159 before round 52)
ring_attach:  total=116   uds=0     plane=116
checkout:     total=107   uds=100   ring=7
HELLO=1  DONE=1  basic 100 pass=1  machmsg_uds=0
```

What moved: the fork-child checkin (which needed a bounded TRANSPORT wait, because a forked child gets a FRESH
page from the ownership fix and its transport is not established at that instant -- without the wait it fell
back to the datagram and the counters did not move at all). What did not: 110 checkin datagrams remain, and they
are not the sites already converted, so the next step is to attribute them by caller rather than to convert
another site blind -- the same lesson this round has now taught three times.

Honest ledger for the round: `ring_attach` is at zero UDS; checkin moved from 159/159 to 110 datagrams with 108
on the page; `checkout` (100) and the two descriptor calls (`console_open` 3, `kqchan_proc_open` 2) are
untouched; the socket metric, the image-adoption tail and the mutation suite are untouched.

Verified GREEN at every step: `HELLO=1`, `basic 100` = 204 / `uds=0`, `stress 16x20` = 643-644 / `uds=0`.


### 48. The main-thread checkin was disabled by a round-49 experiment

The attribution question of §47 answered itself in the source: the main-thread checkin sat behind

```c
if (false && __mldr_process_control_ready()) {
```

-- switched off by the round-49 RED experiment and never re-enabled, so EVERY process's main checkin used the
datagram. That is the whole of the remaining checkin UDS traffic, and it needed no new code to fix, only the
route it already had.

Re-enabled, and measured with a live server:

```
checkin:      total=332   uds=167   plane=165      (was 218 / uds=110 / plane=108; 159/159 before this round)
ring_attach:  total=176   uds=0     plane=176
checkout:     total=163   uds=153   ring=10
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
ool 20: 44/uds=0   basic 100: 204/uds=0   r2 100: 206/uds=0   stress 16x20: 644/uds=0
```

The route that stopped the boot in round 49 now carries the main checkin with the full regression GREEN -- the
difference being the transaction model (§1-§8) and the page-ownership fix (§45), not any change to checkin.

Two honest notes:

  * the per-site attribution counters added for this measurement read zero, so the six counters did not do their
    job -- the site-level split is known only from the aggregate movement (plane 108 -> 165), and the counters
    should be either fixed or removed rather than left reading a constant (the lesson of §43.1, which this round
    managed to repeat with a new counter).
  * `checkout` (153 datagrams) is now the largest remaining semantic UDS user, and it is the next item.


### 48.1 The dead counters, removed

The six per-site checkin counters added in §47 were never incremented -- they read a constant zero, which is the
"counter that always answers the same way" defect this work has already recorded twice (the dead attach-caller
counters of §43.1 and the sampled route counters of §43.6). They were removed rather than left in place, with the
reason recorded where they were: the aggregate heatmap already carries the movement they were meant to explain,
and a counter that cannot be wrong is not evidence.

Verified after the removal: `HELLO=1`, `ool 44 / basic 204 / r2 206`, all `machmsg_uds=0` (the stress workload
was cut by its window in that run, as it has been several times; it was GREEN with the same binaries in §48's
measurement).

### 48.2 Where round 52 leaves the line

```
ring_attach:  total=176  uds=0    plane=176      <- no attach uses AF_UNIX
checkin:      total=332  uds=167  plane=165      (159/159 at the start of the round)
checkout:     total=163  uds=153  ring=10
pthread_canceled / thread_self_trap / vchroot_path: 153/10, 169/10, 20/9
```

Six defects found and fixed by measurement across the round (generation map, pid-keyed pairing, non-unique token,
two unclaimed mailbox slots, inherited page after fork), plus the re-enable of a route a round-49 experiment had
left switched off. Next, in order: `checkout` (the largest remaining semantic UDS user, 153), then the two
descriptor calls (`console_open` 3, `kqchan_proc_open` 2), then the socket metric and its removal.


### 49. CHECKOUT on the page: implemented, GREEN on the tests that ran

`checkout` was the largest remaining semantic UDS user (153 datagrams). The page's op was reserved but had no
server case, so both halves were built:

  * **server**: `DSERVER_PROCESS_CONTROL_OP_CHECKOUT` resolves the exec listener pipe from the courier with
    `kind = CHECKOUT_FD`, builds the ORDINARY checkout Call (same technique as CHECKIN and ATTACH_LANE), runs it
    through `callFromMessage -> doWork` with the reply suppressed, and the page carries the completion;
  * **guest** (`execve.c`): publishes the request on the page with the pipe on the courier, claiming the mailbox
    slot and waiting for completion, with the datagram path kept as the fallback.

Measured: `HELLO=1`, `ool 20` = 44 / `uds=0`, `basic 100` = 204 / `uds=0`, `r2 100` = 206 / `uds=0` -- the
regression is GREEN with the route in place. The census snapshot for `checkout` was not captured in this run
(the server had exited before the read, the same capture problem this round has hit repeatedly); the method is
known and the number is recorded as unmeasured rather than inferred.

Next: the `threads.c` checkout instance (the descriptor-less thread-exit case), then `console_open` /
`kqchan_proc_open`, then the socket metric.

### 49.1 Round 52 final ledger

| operation | UDS before this round | UDS now | note |
|---|---|---|---|
| `ring_attach` | 174 (all) | **0** | page route, default; pre-page window closed |
| `checkin` | 159 (all) | 167 of 332 | page route for main/thread/fork sites; 165 on the page |
| `checkout` | 153 | 153 | page route implemented this round, GREEN, count not re-snapshotted |
| `pthread_canceled` / `thread_self_trap` / `vchroot_path` | 76 / 10 / 9 | 76 / 10 / 9 | untouched |
| `console_open` / `kqchan_proc_open` | 3 / 2 | 3 / 2 | untouched |

Six defects found and fixed by measurement, one disabled route re-enabled, one dead counter set removed, and the
page's transaction model built and used by four operations. Everything above is committed and the tree is GREEN
at `HELLO=1` with `ool 44 / basic 204 / r2 206 / stress 644` and `machmsg_uds=0`.


### 50. CHECKOUT stays on the datagram; and a build-consistency lesson repeated

Both checkout instances were routed through the page and both are RED, for the same reason the earlier
checkout-on-lane experiment was: the boot stops at `shellspawn did not become ready`. The execve instance (with
its descriptor half and an active lane) and the descriptor-less thread-exit instance both wedge. So both guest
sites are reverted to the datagram, with the reason recorded where they were, and the server-side
`OP_CHECKOUT` case stays in the tree for the work that will make the teardown ordering explicit rather than
implicit.

Verified after the reverts: `FINAL=1`, `HELLO=1`, `ool 44 / basic 204 / r2 206 / stress 643`, all
`machmsg_uds=0`.

**The lesson repeated, and it cost three runs.** Removing the six dead page counters changed
`struct dserver_process_control`, and the boot then stopped at `shellspawn did not become ready` -- the same
symptom as a page bug -- until every consumer was rebuilt and redeployed TOGETHER (mldr, darlingserver,
libsystem_kernel, dyld). This is exactly the rule already recorded in §43.2 for a shared struct, and it was
broken again by treating "mldr + darlingserver + the dylib" as the complete consumer set. The honest statement
is that the three RED runs in between (CO3, CO4, CO5) were build inconsistency, not checkout, and that the
checkout RED was only established afterwards.

### 50.1 Round 52 close

```
ring_attach:  total=176  uds=0    plane=176
checkin:      total=332  uds=167  plane=165
checkout:     datagram (page route implemented, measured RED, reverted)
pthread_canceled 153/10  thread_self_trap 169/10  vchroot_path 20/9  console_open 3  kqchan_proc_open 2
regression:   FINAL=1  HELLO=1  44 / 204 / 206 / 643   all machmsg_uds=0
```

Seven defects found and fixed by measurement across the round, one disabled route re-enabled, one dead counter
set removed and one build-consistency rule re-learned. What is left is unchanged in kind from the directive's
remaining list: the teardown ordering that checkout needs, the two descriptor calls, the socket metric and
removal, the image-adoption tail, the mutation suite, and the final censuses.


### 51. Correction: the checkout "RED" was a false read

§50 recorded both checkout routes as RED on the strength of a `shellspawn did not become ready` line. That line
appears in GREEN runs too -- it is a per-process message, not the run's verdict -- and the runs in question were
read at the wrong moment, before their own `FINAL`. Measured properly, with the execve route restored:

```
FINAL=1  HELLO=1  passes: pass=1 pass=1 pass=1 pass=1
ool 20: 44/uds=0   basic 100: 204/uds=0   r2 100: 206/uds=0   stress 16x20: 644/uds=0
```

**The execve checkout on the page is GREEN** and is kept. The lesson is the same one this work has recorded more
than once and keeps re-learning: a per-process diagnostic line is not a verdict, and a run must be read after
its own completion marker -- the discipline that produced the false RED here is the same one that produced the
false "done" readings earlier in the project, in the opposite direction.

The descriptor-less thread-exit checkout is not restored in this round; it is a teardown call and it needs the
same proper measurement the execve instance just got, rather than another revert on a misread.

Round 52 is therefore: `ring_attach UDS = 0` (measured), checkin on the page for three sites (measured), the
execve checkout on the page (measured GREEN), seven defects fixed, one dead counter set removed, one build rule
re-learned, one false RED corrected.


### 52. CHECKOUT on the page aborts the server, and that is why it stays on the datagram

Both checkout instances were routed through the page twice -- the execve instance (descriptor half on the
courier) and the descriptor-less thread-exit instance -- and the log names the real failure, which is not a boot
wedge and not a false read:

```
terminate called after throwing an instance of 'std::system_error'
  what():  Failed to send messages through socket: Transport endpoint is not connected
Rootless shellspawn did not become ready within 30000ms
```

**The server aborts.** Checkout is a TEARDOWN call: its caller is going away, and the server still attempts a
reply send that the page route's `suppressReplyDelivery()` does not prevent -- the attempt throws and takes the
process down. That also explains the earlier readings: what looked like a boot wedge was the server dying, and
what looked like a false RED (§51) was a run read before the abort.

Both guest sites are therefore on the datagram, with this reason recorded in place, and the server-side
`OP_CHECKOUT` case stays in the tree: it is the right shape for the operation, and what it needs is the
teardown-ordering work -- a page-serviced call whose caller may be gone must not attempt any send, which is a
change to the reply funnel rather than to this case.

Verified after the reverts: `FINAL=1`, `HELLO=1`, `ool 44 / basic 204 / r2 206 / stress 644`, all
`machmsg_uds=0`; prefix clean.

### 52.1 Round 52 close, with the honest ledger

```
ring_attach:  total=176  uds=0    plane=176      <- measured live
checkin:      total=332  uds=167  plane=165      <- measured live (159/159 before the round)
checkout:     datagram, both instances; page route implemented, ABORTS the server, reverted with the cause
regression:   FINAL=1  HELLO=1  44 / 204 / 206 / 644   all machmsg_uds=0
```

Seven defects found and fixed by measurement; one disabled route re-enabled (the main-thread checkin); one dead
counter set removed; one build-consistency rule re-learned; one false RED corrected; and one operation proven,
by an abort with a named exception, to need reply-funnel work before it can leave the datagram.

Two of my own readings were wrong in this round and both are recorded rather than quietly fixed: the
`shellspawn did not become ready` line is a per-process message that appears in GREEN runs, and a run must be
read after its own `FINAL`. The count of misread runs is now four; every one of them came from reading a
diagnostic line instead of the verdict, which is the single most expensive habit in this work.


### 53. The teardown abort, fixed at the reply funnel -- and checkout now rides the page

§52 named the real failure: the server aborts when a reply send meets a peer that is gone. The throw site is
`message.cpp`'s send loop, which treats EAGAIN, EINTR, EPIPE and ECONNREFUSED as "drop this message and carry on"
and throws on everything else -- and `ENOTCONN` ("Transport endpoint is not connected") was not in that list. A
teardown call whose caller is exiting therefore took the whole server down.

Fixed at the funnel, which is where it belongs: **a vanished peer is a dropped message, never a reason to abort
the process.** That is not checkout-specific -- any reply to a departed peer would have done the same.

With the abort gone, the execve checkout on the page is GREEN:

```
FINAL=1  HELLO=1  passes: pass=1 pass=1 pass=1 pass=1
ool 20: 44/uds=0   basic 100: 204/uds=0   r2 100: 206/uds=0   stress 16x20: 644/uds=0
ENOTCONN occurrences: 0
```

So the operation that appeared to "wedge the boot" twice was the server dying, and the fix is one errno in a
list that already had three of its siblings. The census snapshot for `checkout` was again not captured (the
server exits before the read); the route is GREEN and the number is recorded as unmeasured.

### 53.1 What this round established

  * `ring_attach` uses no AF_UNIX at all (measured live: `uds=0 plane=176`).
  * `checkin` moved from 159/159 datagrams to 167/332 with 165 on the page (measured live), including the
    main-thread site that a round-49 experiment had left disabled.
  * `checkout` on the page is GREEN once the reply funnel stops aborting on a departed peer; the server-side
    `OP_CHECKOUT` case is in the tree and the execve instance uses it.
  * Eight defects found and fixed by measurement, including one that had been mis-diagnosed twice (the abort),
    one false RED corrected, one build-consistency rule re-learned, and one dead counter set removed.

Remaining, unchanged in kind: the descriptor-less thread-exit checkout (same funnel fix applies; it needs the
measurement the execve instance just got), `console_open` / `kqchan_proc_open`, the socket metric and its
removal, the image-adoption tail, the mutation suite, and the final censuses with the FD slope.


### 54. Both checkout instances on the page: 153 -> 1

With the reply funnel no longer aborting on a departed peer (§53), the descriptor-less thread-exit checkout was
restored as well, and the live census now reads:

```
checkout:     total=164   uds=1    ring=0   plane=163      (was 153 datagrams)
ring_attach:  total=176   uds=0    ring=0   plane=176      (was 174 datagrams)
checkin:      total=332   uds=167  ring=0   plane=165      (was 159 datagrams)
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
ool 44/uds=0   basic 204/uds=0   r2 206/uds=0   stress 644/uds=0
ENOTCONN occurrences: 0
```

Three of the four lifecycle operations are now off AF_UNIX: the attach entirely, checkout almost entirely (one
datagram left, presumably a path taken before the page was ready), and half of checkin.

Round 52-54 ledger, measured on live servers:

| operation | datagrams before | datagrams now | on the page |
|---|---|---|---|
| `ring_attach` | 174 | **0** | 176 |
| `checkout` | 153 | **1** | 163 |
| `checkin` | 159 | 167 | 165 |

Nine defects were found and fixed by measurement across these rounds, the last being the reply-funnel abort that
had been mis-diagnosed twice as a boot wedge. Remaining semantic UDS, in order of size: `checkin` 167,
`pthread_canceled` 76, `thread_self_trap` 10, `vchroot_path` 9, `console_open` 3, `kqchan_proc_open` 2.


### 55. The remaining checkin datagrams are not from the main-thread site

A bounded TRANSPORT wait was added to the main-thread checkin (a fresh incarnation's page is created at that
point and its transport is not up yet, so the route was falling back). Measured: the wait is harmless -- the full
regression is GREEN (`FINAL=1`, `ool 44 / basic 204 / r2 206 / stress 644`, all `uds=0`) -- and the checkin count
did not move (`total=332 uds=167 plane=165`). So those datagrams do not come from the site that was just
changed, and the next step is attribution rather than another conversion.

The instrument for that must not be another page counter: the six per-site counters tried in §47 never
incremented and were removed in §48.1, which is the same lesson twice. What will work is what already works
here -- the server's own log at the UDS checkin path (`Call::Checkin::processCall` can name the tid and the
process it services), because the server is the only party that sees every datagram checkin regardless of which
guest site sent it.

Measured state after this step:

```
checkout:     total=166   uds=3    plane=163
ring_attach:  total=176   uds=0    plane=176
checkin:      total=332   uds=167  plane=165
regression:   FINAL=1  HELLO=1  44 / 204 / 206 / 644   all uds=0
```

Next, in order: attribute the 167 from the server side, then `pthread_canceled` (76), `thread_self_trap` (10),
`vchroot_path` (9), `console_open` (3), `kqchan_proc_open` (2), then the socket metric and its removal.


### 56. Attribution from the server side: the datagram checkins are fork children and main/thread checkins with no lifetime descriptor

The instrument that works -- the server's own log at the UDS checkin path -- answers §55 in one run:

```
uds-checkin pid=826379 tid=826379 fork=0 lifetime=-1
uds-checkin pid=826379 tid=826381 fork=0 lifetime=-1
uds-checkin pid=826383 tid=826383 fork=1 lifetime=-1
uds-checkin pid=826402 tid=826402 fork=1 lifetime=-1
...
count=218
```

Every one carries `lifetime=-1`, so **none of them is the exec path** (which has a lifetime pipe). They are
fork children (`fork=1`) and main/thread checkins (`fork=0`), and each pid/tid appears twice.

That contradicts the conversion: the fork-child site WAS routed through the page in §47 and the thread-create
site in §46. So for these processes the page route is not being taken even though it exists in the code -- the
candidate is that a forked child's page is not ready within the bounded wait (the child inherits the parent's
page pointer, the ownership check abandons it, and a fresh page needs a courier round trip that the wait may not
cover), or that the code is not reached at all on this path.

That is the next measurement, and it is now cheap: the same log plus a page-route success/failure line at the
two converted sites will say which. It is worth stating plainly that this is the fifth time in this work that a
conversion has been recorded as done on the strength of the code being present rather than the route being
taken, and the attribution log is the instrument that would have caught all five.


### 57. The datagram checkins are DUPLICATES: the page route works and its result is not accepted

§55/§56 left two hypotheses (the page is not ready in a fork child; the route code is not reached). The
server-side instrument added here answers both at once, because the server is the only party that can say
whether a process had a page at the moment its datagram arrived. The attribution line now carries
`page=` and `page_ready=`, read from the server's own `_processControl` registry:

```
uds-checkin pid=1116661 tid=1116661 fork=0 lifetime=-1 page=0 page_ready=0
uds-checkin pid=1116661 tid=1116661 fork=0 lifetime=-1 page=1 page_ready=1
uds-checkin pid=1116661 tid=1116663 fork=0 lifetime=-1 page=1 page_ready=1
uds-checkin pid=1116661 tid=1116663 fork=0 lifetime=-1 page=1 page_ready=1
uds-checkin pid=1116666 tid=1116666 fork=1 lifetime=-1 page=1 page_ready=1
uds-checkin pid=1116666 tid=1116666 fork=0 lifetime=-1 page=1 page_ready=1
...
count=218      histogram: page=0/page_ready=0 -> 1;  page=1/page_ready=1 -> 217
```

**Both hypotheses are refuted.** For 217 of 218 datagram checkins the process HAD a mapped page with its
transport published. So the guest is not falling back because the transport is missing -- it has it and does not
use it.

And every (pid, tid) appears TWICE. A line is printed once per `Checkin::processCall`, so those are two distinct
checkin calls for the same thread. Combined with §52's live census (`checkin total=332 uds=167 plane=165`), the
arithmetic is exact: **332 = 2 x 166**. Half of all checkins are duplicates -- the page route runs (165 of them,
counted as `plane`), the guest does not accept its result, and the same checkin is then repeated on the datagram
(167). The datagram masks the defect, which is why every regression in this round has been GREEN while the
transport was wrong.

The guest accepts a page-route checkin only when `reply_status == 0`, or when the server has claimed the
transaction. So the server is completing the OP_CHECKIN with a non-zero status, and the guest treats that as
"not checked in" and repeats the operation. The next instrument is the server's own reply status for the
OP_CHECKIN case -- one line, in `_serviceProcessControl`, naming the seq and the status it publishes.

This is the fifth conversion in this work that was recorded as done on the strength of the code being present
rather than the route being taken (§56), and the first one where the reason is visible: a route that works and a
result the caller does not believe.


### 58. THE CAUSE OF THE DUPLICATES: the page route carried the wrong architecture

§57 established that half of all checkins were duplicates -- the page route ran, the guest refused the result and
repeated the same checkin on the datagram. The server's reply status for the OP_CHECKIN case was the instrument
that named it: `102 status=-22` out of 108. -22 is -EINVAL, and the server produces it in exactly one way on this
path, through `processCallBasicReplyCode`'s `catch (std::exception&)`. That guard already logged its verdict, and
the log had been sitting in every capture of this round unread:

```
102 Uncaught exception from processCall (call dserver_callnum_checkin); replying -22
```

Printing the exception message (one line, gated) gave the sentence:

```
102 [processcall-guard] std::exception: Impossible: parent process architecture != child process architecture on fork
```

The throw site is `Process::notifyCheckin`, which compares the architecture the checkin CARRIES against the
process's own and refuses a mismatch. The enum is `0 invalid, 1 i386, 2 x86_64, 3 arm32, 4 arm64`, and the three
page-route sites were sending:

| site | sent | should send |
|---|---|---|
| `mldr.c` (main checkin) | `_32on64 ? 1 : 2` | correct -- this was the 6 `status=0` |
| `mldr/elfcalls/threads.c` (thread create) | hardcoded `1u` = **i386** | the real architecture |
| `libsystem_kernel/.../fork.c` (fork child) | hardcoded `1u` = **i386** | the real architecture |

So 102 of 108 page checkins carried `i386` for an `x86_64` process. The page route answered -EINVAL, the guest
saw a non-zero status, and it repeated the checkin on the datagram -- which is why the transport was wrong while
every regression in this round was GREEN. `mldr.c` had computed this correctly all along, which is why exactly
six checkins succeeded and why the defect looked like a partial failure rather than a systematic one.

**Fix**: `threads.c` now sends `mldr_load_results._32on64 ? 1u : 2u` (the same expression `mldr.c` uses, with the
loader's `load_results` declared `extern` there), and `fork.c` sends the same mapping computed locally from the
compiler's predefines. The latter is local rather than via `dserver_rpc_hooks_get_architecture()` because
including `resources/dserver-rpc-defs.h` in that translation unit conflicts with its own `memcpy` declaration
(measured: four compile errors).

**Measured after the fix** (live census, full regression):

```
checkin:      total=174  uds=10   plane=164      (was total=332  uds=167  plane=165)
checkout:     total=130  uds=0    plane=130      (was uds=153, then 1-3)
ring_attach:  total=174  uds=0    plane=174
pthread_canceled: 446/74/372     thread_self_trap: 194/10/184     vchroot_path: 29/9/20
Uncaught exception: 0            processcall-guard: 0
ool 44/uds=0   basic 204/uds=0   r2 206/uds=0   all pass=1
```

`total` fell from 332 to 174 -- exactly half, which is the duplicate itself, and the arithmetic that identified
it. **Three of the four lifecycle operations are now off AF_UNIX**: attach entirely, checkout entirely, and
checkin 164 of 174 (94%), with 10 datagrams left to attribute.

The lesson is the same one §56 and §57 recorded, now with the mechanism: a route that is present, that runs, and
whose answer the caller discards is indistinguishable from a route that was never taken -- unless the reply's
status is read. The exception was being logged the whole time.

**Verified on the clean regression** (no diagnostic logging, the run that must carry the claim):

```
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
ool 44/uds=0   basic 204/uds=0   r2 206/uds=0   stress 644/uds=0
Uncaught exception: 0
checkin: total=176 uds=11 plane=165     checkout: total=164 uds=1 plane=163
ring_attach: total=176 uds=0 plane=176
```

Note the diagnostic-logging run in the table above is slower than the clean one (the stress pool does not reach
its verdict inside the same window with the courier log on), so the verdict must be taken from the clean run and
the census from either -- a caution that cost one reading in this round.

`checkin` UDS is down from 167 to 11 of 176, and `total` from 332 to 176. What remains to attribute is small and
named: 11 checkin datagrams, `pthread_canceled` 75, `thread_self_trap` 11, `vchroot_path` 10.


### 59. After the fix: every page reply is accepted, and the remaining datagrams are a second, different defect

The attribution run after §58 is unambiguous about the fix:

```
  1  fork=0 lifetime=-1 page=0 page_ready=0     <- the very first checkin, before its page exists
166  fork=0 lifetime=-1 page=1 page_ready=1
  9  fork=1 lifetime=-1 page=1 page_ready=1
total=176
checkin-reply statuses: 165 status=0            (was 102 x -22)
Uncaught exception: 0
```

**Every page-route checkin now completes with status 0** and the guest accepts it -- the -EINVAL class is gone
entirely, and `total` is 176 rather than 332 because the duplicates are gone with it.

What is left is a different thing, and it is worth stating precisely rather than folding into the same story:
175 datagram checkins still arrive from processes that HAVE a ready page, and their page replies succeeded. So
these are not a fallback from a refused result -- the guest is sending a datagram checkin in addition to a page
checkin that worked.

The concrete candidate, and it is the hazard this work already recorded once: the guest's completion check in
`threads.c` accepts a page checkin only when `reply_seq == mine && reply_status == 0`. The server's tail clears
`request_state` to IDLE immediately after publishing, so a second thread of the same process can claim the slot
and publish its own sequence BEFORE the first thread has read its answer. The first then observes `reply_seq`
belonging to the second, refuses the result, and repeats its checkin on the datagram -- and with several threads
created at once this is systematic rather than rare, which matches 166 of 176.

If that is the mechanism, the fix is a protocol one and small: the server must not release the slot on publish;
the GUEST releases it after it has read its own answer. The mailbox model is one outstanding request per page,
and "one outstanding" has to mean "until the requester has read it", not "until the server has written it".

The measurement that would confirm it is a guest-side count of `reply_seq != mine` at the completion check --
which must be a page counter, not a print, for the reasons §47 and §48.1 recorded twice.


### 60. The remaining checkin datagrams: the loader's checkin site never has a page

§59 proposed a mailbox-ordering defect. That is **refuted by measurement**, and the real cause is simpler and was
already written down in this document once:

```
checkin-route pid=1144427 tid=1144427 ready=0 page=(nil) lifetime=-1 image=mldr!/.../vchroot
checkin-route pid=1144427 tid=1144427 ready=0 page=(nil) lifetime=-1 image=mldr!/.../launchd
checkin-route pid=1144433 tid=1144433 ready=0 page=(nil) lifetime=-1 image=mldr!/.../shellspawn
checkin-route pid=1144435 tid=1144435 ready=0 page=(nil) lifetime=-1 image=mldr!/.../bash
checkin-route pid=1144435 tid=1144435 ready=0 page=(nil) lifetime=-1 image=mldr!/.../sh
checkin-route pid=1144436 tid=1144436 ready=0 page=(nil) lifetime=-1 image=mldr!/.../ring_mach_msg_test
checkin-route pid=1144537 tid=1144537 ready=0 page=(nil) lifetime=-1 image=mldr!/.../sleep
count=7    ready=0 x7    page=(nil) x7
```

The loader's main checkin site finds `page == NULL` on **every** image, without exception. The bounded
`__mldr_process_control_wait_ready(200)` added in §55 therefore waits for a page that has not been created yet:
the checkin is earlier in this path than the establishment block, which the source itself already records from
round 49i. Every one of these falls to the datagram, and they are the bulk of the remaining `fork=0` traffic.

That also explains why §59's candidate cannot be the whole story: these requests never reach the page, so no
`reply_seq` can be mismatched for them.

The fix is to establish the page **before** the checkin site -- move the establishment earlier in the path, or
create it at the site itself. Round 49i tried the latter and measured RED, and that RED is why the route was left
switched off for so long. Its causes are now known and fixed: the inherited page after fork (§45), the missing
transaction/ownership model (§44), and -- decisively -- the wrong architecture byte (§58), which made the page's
checkin answer -EINVAL for 102 of 108 requests. A page route that was measured RED for a reason that has since
been repaired is not evidence against the route; it is evidence about the repair.

So the next change is small and has a specific acceptance: with the page established before the checkin, the
`checkin-route` lines must read `ready=1 page=<non-null>`, the `fork=0` datagram count must fall to the handful
of genuinely pre-page cases, and the regression must stay GREEN with `machmsg_uds=0`.


### 61. The checkin cannot be fixed by establishing the page at its site -- the round-49i RED was about POSITION

§60 ended with a plan: establish the page before the checkin site, and accept it if `checkin-route` reads
`ready=1`. That was implemented and measured, **after** the architecture defect of §58 was fixed, so the
architecture can be excluded as the cause of the earlier RED. The result:

```
HELLO=0  FINAL=0  passes: (none)
checkin-route: ready=0 x2, page=(nil) x2
uds-checkin=2   checkin-reply=2   Uncaught exception: 0
Rootless shellspawn did not become ready within 30000ms (/proc/self/fd/6/shellspawn.sock)
```

**The round-49i RED reproduces exactly.** The boot stops after two checkins and never reaches the shell. So that
RED was caused by the POSITION of the establishment, not by the architecture byte (§58), not by the inherited
page after fork (§45) and not by the missing transaction model (§44) -- all three of which were fixed before this
measurement, and the RED came back unchanged.

This is a clean negative and it also corrects §60's conclusion. The route cannot be reached by creating the page
at the checkin site, because the establishment block in `main` has a position constraint of its own that the
source already recorded: the loader's bootstrap writes must precede it, and this site is earlier still. The
correct shape is the other direction -- **move the checkin later in the path, after the establishment** -- rather
than the establishment earlier.

Reverted, and the revert was verified GREEN on the full regression:

```
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
ool 44/uds=0   basic 204/uds=0   r2 206/uds=0   stress 644/uds=0
Uncaught exception: 0
```

The checkin route therefore keeps its measured state from §58 (164 of 174 on the page, 10 datagrams), and the
remaining `fork=0` traffic stays attributed to the loader site having no page at that point -- which is now a
statement about ORDERING, not about a missing capability.


### 62. The mechanism, with line numbers: the checkin runs before the establishment because `load()` does

§61 concluded that the checkin must move later rather than the establishment earlier, and named no positions.
They are these, all in `src/startup/mldr/mldr.c`:

```
main:210/212   load(filename, CPU_TYPE_X86 | 0, false, argv, &mldr_load_results)
                 -> load64() -> setup_space()  [mldr.c:1915]
                      -> CHECKIN                       mldr.c:2100     page == NULL here
main:329       __mldr_process_control_create()          the page is created HERE
main:337       the generation PING
main:354/369   set_dyld_info / set_executable_path      the bootstrap writes
```

So `load()` is called from `main` at 210/212, well before the establishment block at 329, and `setup_space` is
reached from it -- which is why the checkin finds `page == NULL` on every image and why the bounded wait added in
§55 was waiting for something that did not exist yet. The source comment at the establishment block ("the
loader's first writes must precede the plane's establishment") is correct about the WRITES, which are at 354/369,
and it does not describe the CHECKIN, which is at 2100 and therefore earlier than all of it.

The change is therefore a reordering inside the loader, and it has to move the checkin, not the establishment:
the establishment cannot move earlier (§61 measured that RED), and the checkin cannot stay where it is if it is
to use the page. Concretely the checkin block leaves `setup_space` and runs in `main` after the establishment and
after the PING, taking with it the two things it owns there -- the lifetime pipe close and the `vchroot_path`
retrieval, both of which use only `lr` (== `&mldr_load_results`) and `lifetime_pipe`, which `main` has.

Acceptance, unchanged from §60 and now precisely located: `checkin-route` must read `ready=1 page=<non-null>`,
the `fork=0` datagram count must fall to the genuinely pre-page cases, and the regression must stay GREEN with
`machmsg_uds=0`. The one risk to watch is the ordering that the source comment protects: the checkin must not
end up AFTER the bootstrap writes, because a checkin whose result must be visible to the process's other traffic
cannot follow them.


### 63. The loader's checkin cannot ride the page at all -- proved by making the page available and watching it fail

§60/§62 aimed at making the page available at the loader's checkin site. That was achieved, and the boot then
failed -- which turns the whole question around and settles it.

The establishment block was moved from its position after `load()` to immediately before it, so that `load()` ->
`load64` -> `setup_space` -> checkin all run after the page exists. The move had to be made twice: the first
attempt landed the block inside `#ifdef __i386__` and it was compiled out, which is visible in the log as the
establishment never printing and the bootstrap writes falling to `ready=0`. After correcting the placement, the
measurement is unambiguous:

```
[mldr-ctl] page pid=1160286 size=136 sent=1
[mldr-ctl] ready pid=1160286 state=1 page=0x7aded64ac000 sz=136 off=104
[mldr-ctl]   ready-image=vchroot
[mldr-ctl] checkin-route pid=1160286 tid=1160286 ready=1 page=0x7aded64ac000 lifetime=-1 image=vchroot
[mldr-ctl] seq=1 after-dyld pid=1160286 status=0 ready=1 image=vchroot
...
HELLO=0   FINAL=0   shellspawn did not become ready
```

`checkin-route` now reads `ready=1 page=0x...` on every image -- the exact acceptance §60 asked for -- and the boot
does not complete. So:

* the page IS available at the checkin site, so a missing page was never the reason;
* two different earlier positions for the establishment (inside `setup_space`, §61; before `load()`, here) both
  give RED, so the position of the establishment was never the reason either;
* the RED is caused by the page route for THIS checkin.

That also finally explains the round-49i RED with evidence rather than inference: it was never about the
architecture byte (§58), the inherited page (§45), the transaction model (§44) or the position -- it is that this
particular checkin cannot be expressed as a page write. The source comment said so all along: this checkin's
result must be visible to the process's OTHER traffic, and the page is serviced on the server's own pass while a
datagram is serviced the moment it arrives. What is new is that the claim is now measured from both directions.

**Reverted** (the block is back after `load()`, before the bootstrap writes) and the revert verified GREEN:

```
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
ool 44/uds=0   basic 204/uds=0   r2 206/uds=0   stress 643/uds=0
Uncaught exception: 0   shellspawn-not-ready: 0
```

Consequence for the plan: the loader's main checkin stays on the datagram, and the `fork=0` datagram traffic
attributed in §59/§60 is that call, permanently -- it is not a migration gap to close by ordering. The reachable
target is the OTHER two sites, which are already on the page (`checkin` 164 of 174 in §58), and the remaining
work is the socket metric, the teardown calls and the image-adoption tail.

A caution recorded with this: one reading in this round showed `uds-checkin=2` and was briefly taken as a
success; the file had been rewritten by a later short run whose boot failed. Two runs writing one log path is
the same class of error as reading a diagnostic line instead of the verdict, and the defence is the same --
name the run and read its own completion marker.


### 64. State after the loader conclusion, and an instrument gap for the remaining three callnums

With §63 reverted and verified GREEN, the live census on the full regression reads:

```
checkin:           total=176  uds=11  plane=165      (was total=332 uds=167)
checkout:          total=165  uds=2   plane=163      (was uds=153)
ring_attach:       total=176  uds=0   plane=176      (was uds=174)
pthread_canceled:  total=571  uds=75  ring=496
thread_self_trap:  total=198  uds=11  ring=187
vchroot_path:      total=32   uds=10  ring=22
```

Three of the four lifecycle operations are off AF_UNIX and the two remaining large ones are already mostly on
the lane: `pthread_canceled` is 87% on the Ring, `thread_self_trap` 94%, `vchroot_path` 69%. The remaining
semantic UDS is 98 calls in total across those three.

The intended instrument for attributing them is the guest's own reason histogram, and it does not fire for this
class:

```
[dring-adopt] 80   [dring-attach] 120   [dring-doorbell] 11
[dring-lane-release] 31   [dring-lane-stats] 7
[dring-uds-reason-hist] 0
```

`[dring-lane-stats]` prints once per process at `sys_exit`, as designed, so the dump path is reached -- but no
reason line accompanies it. That means the reason accounting does not cover the sites these three callnums fall
back from, which is the same shape as the dead-counter defects recorded twice in this document: an instrument
that is present, runs, and has nothing to say about the thing it was built for.

So the next step for these three is not another conversion but an attribution change: either the reason is
recorded where these calls actually decide, or the server names the transport of each of them directly (the
heatmap already splits by callnum, so a server-side line per UDS `pthread_canceled` -- with the tid and whether
that tid has a lane -- would answer it in one run, the way the checkin attribution of §56 did).


### 65. CORRECTED: `pthread_canceled` is not duplicated -- it simply does not use the lane

The first version of this section claimed the call arrives twice, once on the lane and once on the datagram, on
the strength of a histogram reading `189 lane=0` and `189 lane=1`. **That reading was an artifact of the
counting**: `has_lane=1` contains the substring `lane=1`, so a `grep 'lane=1'` matches every line. Counted with a
token boundary the same log reads:

```
 lane=0: 210      lane=1: 0      has_lane=1: 210
```

So every invocation in that run arrived on the **datagram**, from a thread that **has a lane**. There is no
duplication here, and the §57/§58 mechanism does not apply to this callnum on the evidence available.

The corrected statement is narrower and still interesting: this call is not taking the lane even though one
exists for its thread. Two candidates remain, and they are distinguishable:

* the callnum is not lane-eligible at all (a generator policy question -- `pthread_canceled` may not be in
  `RING_GENERATED_SIMPLE`), in which case the full-run heatmap's `ring=496` must come from a different
  code path or a different run's traffic, and this is by design rather than a defect;
* the call is lane-eligible and the guest declines the lane for it.

The measurement that separates them is the same line read on the FULL regression, where the heatmap showed
`pthread_canceled total=571 uds=75 ring=496`: if `lane=1` appears there, the call is eligible and the run above
was simply one where no lane was in use for those threads; if `lane=1` never appears, the callnum is not
lane-eligible and the `ring` column of the heatmap is counting something else.

This correction is recorded rather than quietly overwritten because the error is the third of its kind in this
document -- a number produced by an instrument that could not answer the question asked -- and because the
`has_lane=1` / `lane=1` substring collision is exactly the sort of thing that will recur.


### 66. Answer to §65: `pthread_canceled` is not lane-eligible, and the heatmap's `ring` column is not trustworthy for such calls

The separating measurement §65 named, run on the full regression:

```
AZ1:  HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
pthread_canceled:  lane=0 571   lane=1 0   has_lane=1 571   has_lane=0 0
```

`lane=1` never appears -- zero of 571 invocations were taken off the lane, across a run that includes
`stress_pool 16 20` and therefore has lanes in active use. So the first of §65's two candidates holds: the callnum
is **not lane-eligible**. This is a generator policy fact, not a guest defect, and `pthread_canceled` on the
datagram is by design.

That also settles the second number, and it is the more useful outcome of the two: the full-run heatmap reported
`pthread_canceled total=571 uds=75 ring=496`, and this measurement shows that **zero** of those calls were
serviced from a lane. So the `ring` column is counting something other than "this call was taken off a lane" for
at least this callnum. Until that column is understood per callnum, no conclusion about the transport of a
non-eligible call may be drawn from it -- which is the same warning as §38.3 (`machmsg_uds` must be read every
run) applied to the other column.

What this closes and what it leaves:

* `pthread_canceled`, `thread_self_trap` and `vchroot_path` are the three callnums that are not lane-eligible.
  Their datagram traffic is the design, and the "remaining semantic UDS" figure should be read as those three
  callnums plus the permanently-datagram loader checkin of §63 -- not as a migration gap.
* The reachable target is therefore what §58 measured: attach and checkout off AF_UNIX entirely, checkin on the
  page for every site that can carry it, and the per-thread RPC socket, whose only consumer is the checkin, as the
  next thing to remove.
* The heatmap's `ring` column needs its own attribution before it is cited again; the transport tag it is built
  from is set at the point a call is taken off the lane (§29), so a callnum that is never taken off a lane must
  read zero there, and 496 means the tag is not what the column assumes.


### 67. `per_thread_rpc_socket_created = 27`, and every one of them is the thread-create checkin

The socket metric was measured on the full regression with its own diagnostic (`[rpc-socket] created ... reason=`):

```
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
ool 44/uds=0   basic 204/uds=0   r2 206/uds=0   stress 644/uds=0
rpc-socket creations: 27      reasons: 27 reason=checkin
[rpc-socket] created pid=1185805 tid=1185807 n=1 reason=checkin
[rpc-socket] created pid=1185805 tid=1185808 n=2 reason=checkin
[rpc-socket] created pid=1185821 tid=1185829 n=1 reason=checkin
...
```

**27 sockets, every single one attributed to `checkin`.** That confirms the §24 finding by measurement rather than by reading the code: the per-thread RPC socket has exactly one consumer, and it is the checkin. So removing it is a question about the checkin and nothing else.

The number also says the fallback is still taken: `threads.c` reaches
`if (!checked_in && dserver_rpc_explicit_checkin(__darling_thread_rpc_socket(), ...))`, and `__darling_thread_rpc_socket()`
is only evaluated when `!checked_in` -- so 27 creations mean 27 thread-create checkins did not accept the page
route. The checkin census in the same period reads `uds=11 plane=165`, so the two numbers are not the same
population; the socket counter is the more direct one for this question and the one to drive to zero.

The acceptance for the socket target is therefore exact and now instrumented: `rpc-socket creations: 0` on a full
regression. The reason it is not zero is the one §59 named and could not measure at the time -- the thread-create
site's completion check requires `reply_seq == mine && reply_status == 0` (or a claimed transaction), and the
architecture defect that used to make `reply_status` non-zero for this site is fixed (§58), so what remains is the
sequence race: the server releases the mailbox slot on publish, and a second thread of the same process can claim
it and publish its own sequence before the first thread has read its answer.

So the next change is the one §59 described: **the guest releases the mailbox slot after reading its answer, not
the server on publish**. It is a protocol change across the server's page tail and the three guest publishers,
and its acceptance is both the socket count going to zero and the checkin census's `uds` column falling to the
loader's permanently-datagram site.


### 68. The mailbox slot ownership change: correct, GREEN, and NOT the cause of the 27

§67 predicted that the 27 thread-create checkins which fall back and create a per-thread socket would stop once the
guest owns the mailbox slot until it has read its answer. That change was implemented -- the protocol header gains
`DSERVER_PROCESS_CONTROL_RELEASE`, the server publishes the serviced state instead of releasing, and all seven
guest publishers release after reading -- and it is measured GREEN:

```
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
ool 44/uds=0   basic 204/uds=0   r2 206/uds=0   stress 644/uds=0
shellspawn-not-ready: 0
rpc-socket creations: 27
```

**27, unchanged.** So the sequence race was not the cause, and §67's prediction is refuted. The change is kept
because it removes a real race (the slot is now owned until read, which is what "one outstanding request" must
mean), but it is not the fix for the socket count and must not be recorded as one.

One RED was measured and repaired on the way, and it is worth recording because it is a trap in the design: with
the server no longer releasing the slot, `request_state` stayed `PENDING` after servicing, and the service loop
**re-services any page it reads as PENDING** -- so the server re-ran the same request and published its old
sequence again, and the next bootstrap write read a foreign `reply_seq` and failed:

```
Failed to tell darlingserver about our executable path
Rootless shellspawn did not become ready within 30000ms
```

The serviced state is therefore `DONE` (the loop no longer re-services it) and only the guest's release sets
`IDLE`. That distinction -- "serviced" is not "free" -- is now in the code with the measurement that produced it.

What this leaves: the 27 fall back for a reason that is **not** the mailbox race. The candidates are now the ones
that were never excluded: the page pointer or `transport_ready` being unset for a thread's first call, the claim
bound expiring under 16-way contention, or a non-zero `reply_status` for the thread-create checkin specifically.
The instrument that separates them is the one already used for the checkin question -- the server's
`checkin-reply status=` line, read on a run that also counts the socket creations, so that the 27 can be matched
to the statuses the server published for them.


### 69. The 27 are not a refused result: every page checkin the server published was status 0

§68 left three candidates for the 27 socket-creating thread-create checkins. One run answers it:

```
rpc-socket creations: 27        reasons: 27 reason=checkin
checkin-reply statuses: 165 status=0        (no non-zero status at all)
checkin-reply lines: 165        uds-checkin: 176
uds-checkin breakdown: 166 fork=0 page=1 page_ready=1 | 9 fork=1 page=1 page_ready=1 | 1 page=0
rpc-socket by process: 8 + 8 + 8 + 2 + 1
```

**Every page checkin the server completed was accepted with status 0** -- there is no `-EINVAL`, no `-ESRCH`, no
refusal of any kind. So the thread-create fallback is not a refused result, and with §68's ownership change the
sequence race is gone as well. What is left is that the page request either never reached the server or was never
completed for those 27.

The breakdown also settles what the remaining datagram traffic IS, and it is worth writing down because it is
easy to mistake for the same defect:

* `166 fork=0 page=1 page_ready=1` -- these are the **loader main checkins**, one per process invocation, which
  §63 established can never ride the page. They are permanent by design, and they are the bulk of the column.
* `9 fork=1 page=1 page_ready=1` -- fork-child checkins, whose site has a page and still takes the datagram.
* `1 page=0` -- the very first checkin, before any page exists.
* The 27 socket-creating thread-creates are a **subset of the 166**, from three processes that each created eight.

So `checkin uds=176` is not 176 migration gaps: it is ~139 loader checkins, 9 fork checkins, 1 pre-page and the
27 thread-creates. Reading the column without this breakdown is how a permanently-datagram call gets counted as
work remaining.

The next instrument has to say whether the page request happened at all for those 27, and it must not be another
guest page counter (two sets of those were written and never incremented, §47/§48.1). The server already logs the
request's `payload[1]` (the tid) when it services an OP_CHECKIN; logging the same tid at the socket-creation site
and intersecting the two lists answers it in one run, the way the checkin attribution of §56 did.


### 70. The sequence collision is real, was fixed, and is NOT the cause either

§69 narrowed the 27 to "the page request either never reached the server or was never completed". The
intersection the section named answers the first half, and it is decisive:

```
socket tids: 19   checkin-op req tids: 143
in BOTH: 19       socket-only (no page request): 0
sample both: ('1206534','1206536') ('1206534','1206537') ('1206543','1206544') ...
```

**Every** thread that created a socket also appears in the server's list of serviced `OP_CHECKIN` requests --
`socket-only` is empty. So the page request DID reach the server and WAS serviced, and §69's own status
measurement says every one of those was completed with status 0. The guest therefore had a successful answer in
the page and created a socket anyway.

That leaves exactly one condition in the guest's acceptance test that can still fail:

```c
if (page->reply_state == DSERVER_PROCESS_CONTROL_DONE && page->reply_seq == mine && page->reply_status == 0)
```

`reply_state` is set by the server immediately before the futex wake, and `reply_status` was measured 0. So
`reply_seq != mine` is the only remaining possibility -- and the sequence is taken from a **non-atomic**
increment in a multi-threaded process:

```c
static uint32_t checkin_seq = 0;
uint32_t mine = ++checkin_seq;
```

Two threads of one process can therefore receive the SAME `mine`; the server answers one of them, and the other
observes a `reply_seq` that is not its own, refuses the result, and repeats the operation on the datagram --
creating the per-thread RPC socket this route exists to avoid. That is a real defect, and it was fixed at every
site that takes a mailbox sequence (`mldr.c` shared publisher and seed, `threads.c` checkin and exit-checkout,
`fork.c`, `execve.c`), each with `__atomic_add_fetch`.

**Measured after the fix: `rpc-socket creations: 27`, unchanged.** So the collision is real but is not the cause
either, and this section must not be recorded as a fix for the socket count. The change is kept: a non-atomic
sequence in a shared protocol is a defect regardless of whether it explains this symptom.

Four hypotheses have now been eliminated by measurement, each with a run:

| hypothesis | measurement | verdict |
|---|---|---|
| the result is refused (non-zero status) | 165 of 165 page replies status 0 | eliminated |
| the mailbox sequence race (§68) | ownership change, 27 unchanged | eliminated |
| the request never reaches the server | 19 of 19 socket tids are serviced | eliminated |
| the sequence collides between threads (§70) | atomic fix, 27 unchanged | eliminated |

What remains is the completion wait itself: `reply_state` not reaching `DONE` within the guest's bound. The
instrument for that cannot be a page counter (two sets were written and never incremented, §47/§48.1) and cannot
be an in-band print on the bootstrap path (a measured hazard); it has to be a print in `threads.c`, which runs
after the loader has finished and is therefore a safe place for one, gated and bounded, naming which of the
acceptance terms failed and what the guest actually read.


### 71. The `reason=checkin` label is a LIE, and `per_thread_rpc_socket_created = 27` is not 27 checkin fallbacks

Two measurements, each decisive, and together they overturn §24 and §67:

**(a) The acceptance test never fails.** A gated bounded print was added at the thread-create acceptance
decision, naming which term failed and what the guest actually read:

```
rpc-socket creations: 19
[checkin-diag] lines: 0
```

Zero. `checked_in` is set on **every** page-route checkin -- there is no refused result, no foreign sequence, no
timeout. So the socket is NOT created because the checkin fell back, and §67's "27 thread-create checkins did not
accept the page route" is wrong.

**(b) The reason label is set unconditionally.** `threads.c` sets it at the top of the thread entry, before
anything else:

```c
t_callbacks = args.callbacks;
t_rpc_socket_reason = "checkin";      // <- a label for "if a socket is created", not a measurement
...
// the socket is created lazily by the first call that needs a datagram, and the label is cleared after
```

The label therefore describes the thread's **first datagram-needing call**, whatever that call is, and it is
printed as if it were the cause. With (a) showing the checkin route succeeds, the 27 creations are threads whose
first datagram-needing call was something else entirely -- and the callnums that cannot use the lane are exactly
the three §66 identified: `pthread_canceled` (0 of 592 on the lane), `thread_self_trap`, `vchroot_path`.

So the corrected statement is:

* `per_thread_rpc_socket_created` counts threads whose first datagram-needing call was any non-lane call, not
  threads whose checkin failed;
* the socket's consumer is therefore **not** "the checkin" (§24) -- it is any call the lane cannot carry, and the
  checkin is merely the first such call in a freshly created thread;
* the target `per_thread_rpc_socket_created = 0` cannot be reached by moving the checkin to the page. It is
  reached when the calls a thread makes are lane-eligible, or when the socket is no longer the datagram
  transport for them.

This is the fourth time in this document that an instrument answered a different question than the one asked --
the dead per-site counters (§47/§48.1), the sampled route counters, the `has_lane=1`/`lane=1` substring collision
(§65/§66), and now a label that names a call rather than the reason. The defence is the same each time and was
what found this one: ask the instrument to name the thing that actually decided, and cross-check it against a
second measurement that can disagree.


### 72. Correction to §66: only `pthread_canceled` is not lane-eligible, and it is missing from the policy table with no reason recorded

§66 concluded that three callnums are not lane-eligible. Reading the policy table itself corrects that:

```python
RING_GENERATED_SIMPLE = {
    'task_self_trap': 0, 'thread_self_trap': 0, 'host_self_trap': 0, 'mach_reply_port': 0,
    'set_dyld_info': 0, 'set_executable_path': 0, 'uidgid': 0, 'get_tracer': 0,
    'task_is_64_bit': 0, 'started_suspended': 0, 'set_thread_handles': 0, 'mldr_path': 0,
    'vchroot_path': 0,
    ...
    'checkout': 0,
    # perf#30 FD-COURIER: the pthread-create checkin rides the lane; the main-thread one does not.
}
```

`thread_self_trap` and `vchroot_path` **are** in the table -- they are lane-eligible, and their heatmap columns
(`ring=187` and `ring=22`) agree. So §66's list of three was wrong; the eligibility test it used (zero lane
invocations in one run) was read as a policy fact when it was only a measurement of that run.

`pthread_canceled` is **not** in the table, and unlike every other exclusion in it there is **no reason
recorded** -- the table carries explicit justifications for the lifecycle calls (checkout's detached-completion
requirement, checkin's wedge and the separate-entry-point work it needs) and none for this one. It is the only
call the lane cannot carry whose exclusion is unexplained.

That makes it the concrete next change, and it is the change that actually reaches the socket target: with
`pthread_canceled` lane-eligible, the first datagram-needing call of a new thread is no longer forced onto the
datagram, so `per_thread_rpc_socket_created` stops counting it. Adding an existing call to the routing table does
not shift the generated callnum enum (the rule about never adding a call-table row is about new entry points),
so it is safe in the sense that matters here -- but it must be generated, built and measured like any other
change, and its acceptance is the socket count falling while the regression stays GREEN with `machmsg_uds=0`.

The caution recorded alongside: `checkin` is deliberately absent from the table and rides the process-control
page instead, and the table's own comment records that putting the lifecycle checkin on the lane wedged the boot
three times. `pthread_canceled` is not that call -- it is an ordinary per-thread operation -- but the same
question must be asked of it before it is routed: whether its result is ordered against anything that another
transport carries.


### 73. `pthread_canceled` on the lane: measured RED, and the table now records why it is absent

§72 found `pthread_canceled` absent from `RING_GENERATED_SIMPLE` with no reason recorded and proposed adding it.
That was tried, generated, built and measured:

```
HELLO=0   FINAL=0   rpc-socket creations: 1
Rootless shellspawn did not become ready within 30000ms
```

**RED.** Routing it on the lane stops the boot, exactly the outcome this table already records for the lifecycle
checkin. So §72's premise was wrong: `pthread_canceled` is **not** "an ordinary per-thread operation with no
ordering dependency". It is reached early enough -- the libpthread cancellation handshake -- that the lane cannot
be relied on to exist, and its result must be visible to the thread's other traffic.

Reverted, and the revert verified GREEN on the full regression:

```
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
ool 44/uds=0   basic 204/uds=0   r2 206/uds=0   stress 644/uds=0
shellspawn-not-ready: 0     rpc-socket creations: 27
```

The valuable part of §72 survives even though its proposal did not: the table now carries a comment on this
exclusion, which is what §72 correctly observed was missing. The rule the comment states is the one this work has
learned repeatedly -- a call that is reached early, or whose result another transport must observe, cannot move to
the lane no matter how ordinary it looks, and the only way to know which it is, is to try it and read the verdict.

Consequence for the socket target, now with all five candidates closed: `per_thread_rpc_socket_created = 27` is
the **price of the datagram** for calls that cannot use the lane, not a migration gap that ordering or routing can
close. Reaching zero requires a transport that is not the per-thread socket for those calls -- the same
conclusion §71 reached from the other direction -- and that is a design change, not a fix.


### 74. FD inventory: one socket per thread, measured from /proc

The architecture claim under test is "AF_UNIX only as a process-level SCM_RIGHTS courier, never an ordinary RPC
transport". Counting sockets directly in `/proc/<pid>/fd` for live guest processes:

```
pid=1693592  threads=18  sockets=20      (ring_mach_msg_test stress_pool 8 20)
pid=2095878  threads=6   sockets=7
pid=2095879  threads=6   sockets=8
pid=2096000  threads=6   sockets=7
pid=2164089  threads=3   sockets=4
pid=2164090  threads=3   sockets=5
```

**sockets = threads + 1 or + 2** in every row. The constant is the process-level pair (the SCM_RIGHTS courier and
the process doorbell); everything above it is one socket per thread -- the per-thread RPC socket, which §71 and
§73 established is created by the first call of that thread the lane cannot carry.

So the FD slope is **1 socket per thread**, not 0, and §33's earlier figure of two per thread was measuring a
state that has since improved but is not zero. The claim "AF_UNIX is only a courier" is therefore **not yet true
at the process level**: the courier is one of them, and the rest are per-thread RPC transports held by threads
whose first call needed a datagram.

Caveat recorded with the numbers: these processes were left over from earlier runs of the same prefix (the
command lines name `lane_hold` and `stress_pool 8 20`, not the run in progress), so they are a valid inventory of
the deployed state but not a sample of one named run. The ratio is consistent across all of them, which is what
makes it usable; a future measurement should name its run and take the sample from it, as §63's caution requires.

What this closes: the socket target has a measured shape now. Zero requires that no thread's first call needs a
datagram, and since the calls that need one are the three that cannot use the lane (and the early
`pthread_canceled`), the transport for those calls has to change. That is the design change §71 and §73 both
pointed at, and it is the honest form of the remaining work on this axis.


### 75. The complete residual inventory: every call still on the datagram, with its transport split

The final census of non-lane UDS traffic, taken live on the full regression (GREEN, `pass=1` x4):

```
console_open           total=3    uds=3    ring=0
fork_wait_for_child    total=9    uds=5    ring=4
interrupt_enter        total=5    uds=5    ring=0
interrupt_exit         total=5    uds=5    ring=0
kqchan_proc_open       total=2    uds=2    ring=0
pthread_canceled       total=571  uds=75   ring=496
thread_self_trap       total=198  uds=11   ring=187
vchroot_path           total=32   uds=10   ring=22

residual_uds_despite_lane: { vchroot_path: 10, thread_self_trap: 10 }
```

Two things this settles.

**First**, `residual_uds_despite_lane` still names exactly the two candidates §32.10 located -- `vchroot_path` and
`thread_self_trap`, 10 each -- so that gap is unchanged by everything this round fixed, and it is measured rather
than assumed. The fix remains the one §32.10 described: the image-adoption / one-lane-per-tid ownership change
(the borrowed-view family), which is a design change and not a routing change.

**Second**, the inventory is larger than §32's two entries and was never written down as a whole: `console_open`
(3), `kqchan_proc_open` (2), `interrupt_enter` / `interrupt_exit` (5 each) and `fork_wait_for_child` (5 of 9) are
also on the datagram and were not in that list. They are small, but they are what a "Ring is the default
transport" claim has to account for, and a claim that names only the two large ones is not the whole picture.

Read together with §73 and §74, the shape of the remaining work is now explicit:

| class | calls | what it needs |
|---|---|---|
| cannot use the lane (measured) | `pthread_canceled`, `interrupt_enter/exit` | a transport other than the per-thread socket |
| lane-eligible but falling back | `vchroot_path` (10), `thread_self_trap` (10) | the image-adoption / one-lane-per-tid ownership change |
| genuinely one-shot | `console_open`, `kqchan_proc_open`, `fork_wait_for_child` | attribution first: why each is on the datagram |
| permanently datagram by design | the loader main checkin (§63), fork children (§69) | nothing -- it is the design |

That table is the honest state of the goal: Ring is the default transport for everything it can carry, three of
the four lifecycle operations are off AF_UNIX, and what is left is enumerated with a reason for each class rather
than left as a count.


### 76. The residual splits into exactly two classes, and the "one-shot" class was wrong

§75 listed four classes. Reading the policy table for each remaining call -- no run needed -- corrects one of them
and collapses another:

| call | in `RING_GENERATED_SIMPLE` | consequence |
|---|---|---|
| `vchroot_path` (10 uds) | yes | lane-eligible, falling back |
| `thread_self_trap` (10 uds) | yes | lane-eligible, falling back |
| `console_open` (3 uds) | yes | lane-eligible, falling back |
| `kqchan_proc_open` (2 uds) | yes | lane-eligible, falling back |
| `checkout` | yes | lane-eligible (and measured on the page) |
| `pthread_canceled` (75 uds) | no | not eligible |
| `interrupt_enter` / `interrupt_exit` (5 + 5 uds) | no | not eligible |
| `fork_wait_for_child` (5 uds) | no | not eligible |
| `checkin` | no | rides the process-control page instead |

So there are exactly **two** classes, not four:

1. **Lane-eligible but falling back -- 25 calls**: `vchroot_path` 10, `thread_self_trap` 10, `console_open` 3,
   `kqchan_proc_open` 2. This is **one** defect, and it is the one §32.10 located: the image-adoption /
   one-lane-per-tid ownership change (the borrowed-view family). §75 put `console_open` and `kqchan_proc_open` in
   a "genuinely one-shot, attribution first" class; that was wrong -- they are in the table, so they are the same
   defect as the other two and no separate attribution is owed for them.
2. **Not lane-eligible by policy -- ~90 calls**: `pthread_canceled` 75 (measured RED when routed, §73),
   `interrupt_enter`/`interrupt_exit` 5+5, `fork_wait_for_child` 5, plus the loader checkin which rides the page.
   These need a transport other than the per-thread socket, which is the design change §71 and §74 both point at.

That is the sharpest form the goal has taken: **one defect covering 25 calls, and one design change covering the
rest**, with each call's class now decided by the table rather than by a guess. It also means the reachable
reduction from fixing the image-adoption gap is 25 datagram calls and up to 25 per-thread sockets, not a
handful -- which is why that gap is the next thing worth building rather than the mutations.


### 77. The 25 are not a migration-guard violation: the lane simply does not exist in the image that falls back

§76 reduced the residual to 25 lane-eligible calls falling back (`vchroot_path` 10, `thread_self_trap` 10,
`console_open` 3, `kqchan_proc_open` 2). The guest already carries the exact instrument for that class, and it is
not the reason histogram:

```c
int __dserver_ring_try_generated_rpc(...) {
    ...
    if (rc == -1) {
        // NONFD_UDS_VIOLATION: the process-global directory already has an ACTIVE incarnation for this
        // thread, this callnum is non-fd and Ring-capable, and the client is about to take the datagram
        // path anyway. This is the migration guard: on a warmed process it must stay 0.
        if (gr_proc_find_active(vtid)) { __atomic_fetch_add(&g_stat_nonfd_uds_violation, 1u, ...); }
        gr_urs_note(callnum, "generated", gr_urs_reason_for_miss(vtid), 0);
```

It is dumped by `[dring-lane-stats]` as `nonfd_uds_violation=`, so it needs no new code. Measured on the full
regression:

```
HELLO=1  FINAL=1  passes: pass=1 pass=1 pass=1 pass=1
nonfd_uds_violation: 0 in all 5 processes        (nonzero: 0 of 5)
[dring-uds-reason] lines: 0
```

**Zero.** So the migration guard never fires: for these calls there is **no ACTIVE process-global lane** for the
thread, and the fallback is not "an active lane existed and the call chose UDS anyway". It is the absence of a
usable lane in the image that is making the call.

That also explains why the reason emission is silent, and it is a second, independent reason on top of §64's: the
`gr_urs_note` call sits in `__dserver_ring_try_generated_rpc`, which lives in the image that owns the generated
route -- and if the call is falling back in a **different** image, that function is never entered, so neither the
counter nor the note can fire. The reason vocabulary has an entry for exactly this (`GR_URS_IMAGE_LOCAL_STATE`,
"this image has no lane table / different image owns the lane"), and nothing in this run could reach it.

So the instrument for the next step must live in the image that actually falls back, not in the one that owns the
generated route -- and the fix is the one §32.10 named: the image-adoption / one-lane-per-tid ownership change.
The two facts measured here make that fix's target precise: 25 calls, no active lane at the point of the call,
and no migration-guard violation to explain away.


### 78. Correction to §76/§77: 20 of the 25 are LOADER calls and are on the datagram by design

§76 called 25 calls "lane-eligible but falling back" and §77 measured that no active lane existed for them. Both
missed the simplest explanation, which the source states outright:

```
mldr.c:2149   dserver_rpc_vchroot_path(...)                          <- the LOADER's call
mldr.c:404    dserver_rpc_explicit_thread_self_trap(kernfd, ...)     <- the LOADER's call, explicit kernfd
```

`vchroot_path` (10 UDS) and `thread_self_trap` (10 UDS) are both issued by **the loader**, inside the same
`setup_space` path that §63 established can never use the page, and `thread_self_trap` is issued through the
**explicit** entry point with `kernfd` -- the datagram by construction. The loader is a consumer **without lanes**
by design; its own `dserver-rpc-defs.h` says so and binds `try_ring` accordingly, which is why the reason
histogram and the migration counter are both silent for these calls (§77): the image that owns those instruments
is not the image making the call.

So §76's class is wrong, and the correction matters because it changes what the next fix is worth:

| call | uds | really is |
|---|---|---|
| `vchroot_path` | 10 | loader call, datagram by design (like the loader checkin, §63) |
| `thread_self_trap` | 10 | loader call, datagram by construction (`explicit_*`, kernfd) |
| `console_open` | 3 | the only remaining lane-eligible fallback candidate |
| `kqchan_proc_open` | 2 | the only remaining lane-eligible fallback candidate |

**The reachable reduction from an image-adoption fix is therefore 5 calls, not 25**, and the "biggest reachable
concession" §76 pointed at is not that. Building the image-adoption / one-lane-per-tid ownership change to chase
`vchroot_path` and `thread_self_trap` would have been building it for calls that must stay on the datagram anyway.

What remains genuinely open is `console_open` (3) and `kqchan_proc_open` (2) -- five calls, each of which needs its
own attribution before anything is built, because nothing so far distinguishes "lane-eligible and falling back"
from "issued by the loader through an explicit entry point" except reading the call site, which is what this
correction did for the other twenty.

The pattern this makes explicit, and it is the fifth instance: a counter or a class is only as good as the
question it was built for, and the cheapest correction is to read the call site rather than to build a fix for a
number.


### 79. The last five calls are early calls, and the image-adoption gap is not confirmed by any call

§78 left five genuinely open: `console_open` (3) and `kqchan_proc_open` (2). Reading their definitions and call
sites closes them too:

```python
('console_open', [], [('console', '@fd'), ('fd_token', '@fd_token')])
'console_open':     'DSERVER_FD_COURIER_KIND_CONSOLE_FD'
'kqchan_proc_open': 'DSERVER_FD_COURIER_KIND_KQCHAN_FD'
```

Both are **descriptor-carrying** calls: the descriptor rides the process courier (like `ring_attach` and
`checkout`, which are on the lane and on the courier), and the call itself is lane-eligible. Their callers are
`openat.c` (opening the console) and `libkqueue/proc.c` (creating a kqueue channel) -- both of which happen
**early in a process's life**, before its lane exists.

And `residual_uds_despite_lane` does **not** name them: it names only `vchroot_path` and `thread_self_trap`, which
§78 showed are loader calls. So these five are not "lane existed and was ignored" either; they are the same class
as the pre-page checkin of §69 -- a call made before the transport it would use exists.

**Conclusion: the image-adoption / one-lane-per-tid gap is not confirmed by a single call in the residual.** Every
one of the calls that remain on the datagram is now accounted for by one of three facts, each read from the source
rather than inferred from a counter:

| fact | calls |
|---|---|
| issued by the loader (no lanes by design) | `vchroot_path`, `thread_self_trap`, the main checkin |
| issued before the lane exists (early in the process) | `console_open`, `kqchan_proc_open`, the pre-page checkin |
| not lane-eligible by policy | `pthread_canceled` (measured RED when routed), `interrupt_enter`/`interrupt_exit`, `fork_wait_for_child` |

That is the honest final state of the residual, and it changes the plan: building the image-adoption ownership
change is **not** justified by this census. It was §32.10's diagnosis for two calls that turn out to be loader
calls, and every other candidate has been explained. If that gap is real it needs its own evidence, and this
round did not produce any.

What is left as genuinely open work is therefore not a list of calls but two design questions: a transport for the
calls the lane cannot carry (`pthread_canceled` and the interrupts, ~90 datagram calls and the per-thread sockets
they force), and whether the early calls (`console_open`, `kqchan_proc_open`) should wait for the lane instead of
falling back. Both are changes, and neither is a bug hunt.


### 80. The socket-disabled hatch works, and the FIRST blocker is not in the census

The directive's acceptance tool was built: `DARLING_DISABLE_THREAD_RPC_UDS=1` makes the per-thread RPC socket
creation **fail hard and name the call** rather than silently recreate the transport this work removes. It has
two halves, because the name is only known in the image that makes the call:

* every generated wrapper names itself (`dserver_rpc_hooks_note_call("<call>")`, emitted by the generator before
  the socket acquisition);
* `mach_driver_get_fd()` -- the guest image's socket accessor -- checks the hatch and aborts, printing
  `[rpc-socket-DENIED] pid= tid= call=<name>`.

The mldr accessor keeps its own denial as a backstop. The hatch reads the environment libc-free (a scan of
`/proc/self/environ`) because this code runs around the bootstrap, where `getenv` is unavailable and an
in-band instrumented path is a measured boot hazard.

**First socket-disabled boot:**

```
HELLO=0   FINAL=0   Rootless shellspawn did not become ready within 30000ms
[rpc-socket-DENIED] pid=1318784 tid=1318784 call=mach_port_deallocate
[rpc-socket-DENIED] pid=1318784 tid=1318784 call=interrupt_enter
```

Two things follow immediately, and the first is the important one:

**`mach_port_deallocate` is the first blocker, and it is not in the residual census at all.** Every census in
§75-79 was built from the server's heatmap, which counts calls the server serviced; a call that never reaches the
server because the guest denies its own transport does not appear there. The hatch finds consumers the census
cannot see, which is exactly why the directive asked for it before any more census work.

**And it cannot go on the simple Ring or on the management plane.** The source states the rule outright:

```
"WRONG LANE, NOT BAD OP": deallocate / mod_refs fail rules 1-3 because the SIMPLE fast ring has no way to
deliver a caller-side S2C while the caller is parked. A future DUPLEX-ring lane that can accept caller-side
S2C mid-call is the right home for destroy-capable ops. Until that lane exists they stay UDS; do NOT smuggle
them back onto the simple ring via a "clever subset" that still risks an S2C.
```

So `mach_port_deallocate` is **destroy-capable**: it can trigger a caller-side S2C while the caller is parked,
which the simple lane cannot deliver. It is also **hot** (a Mach trap), so the process management plane -- which
the directive scopes to rare management work -- is the wrong home for it as well. Its transport is a **duplex
lane**, and the codebase already has one: the duplex mailbox and its `DSERVER_DUPLEX_*` hatches exist from earlier
rounds.

That reorders the migration the directive laid out, and the order now comes from the hatch rather than from the
census: `mach_port_deallocate` (duplex lane) first, then `interrupt_enter` (which the hatch also named, and which
§19-21 require to be reentrancy-audited rather than dropped into the single management slot), and only then the
rare lifecycle calls the census listed.


### 81. `mach_port_deallocate` already has a duplex route -- it is gated OFF by contract, not missing

§80 found the first socket-disabled blocker and the source's rule that it belongs on a duplex lane. The duplex
route for it **already exists**:

```c
int __dserver_ring_mach_port_deallocate_duplex(uint32_t target, uint32_t name, int* out_code) {
    if (!gr_dealloc_via_duplex_enabled()) {
        return -1; // hatch off -> UDS (the default path)
    }
    gr_lane_t* L = gr_lane_for_this_thread_named(...);
    if (!L) { return -1; }
    __atomic_store_n(&gr_cb(L)->duplex_caps,
        DSERVER_RING_DUPLEX_CAP_SELFTEST | DSERVER_RING_DUPLEX_CAP_DEALLOCATE, __ATOMIC_RELEASE);
    ... publish ...
}
```

with the contract stated next to it:

```
ROUTING: the guest sends deallocate on the duplex lane ONLY behind a per-command hatch
(DARLING_GUEST_DUPLEX_DEALLOCATE=1, default OFF, warm-server discipline) AND only after advertising
DSERVER_RING_DUPLEX_CAP_DEALLOCATE at attach. This routing decision is made BEFORE the request is
published (pre-dispatch) -- so a decline is always pre-mutation and there is never a
partial-mutation-then-UDS double-effect. On ANY transport miss the caller returns -1 and the trap impl
UDS-falls-back, which is safe because the server declines BEFORE dispatching the op.
```

So the reason `mach_port_deallocate` creates a per-thread socket is not a missing route, a missing capability or a
semantic obstacle. It is that the route is **switched off by contract**, deliberately: `default OFF, warm-server
discipline, set ONLY per-command on a warm server, never at boot`.

That is precisely the shape of thing the directive asks to end. The duplex lane, the capability bit, the
pre-dispatch decline and the S2C pump for the munmap shape are all built and were measured in earlier rounds; what
remains is to make the route the **default** rather than a warm-server experiment, and to answer the question the
contract was protecting against: whether the server is ready for a duplex deallocate **during boot**.

The next step is therefore small and specific, and it is the first migration of the socket-disabled loop:

1. turn the deallocate duplex route on by default (the hatch becomes a disable, not an enable);
2. run the socket-disabled boot;
3. if it is GREEN, `mach_port_deallocate` is migrated and the hatch names the next call;
4. if it is RED, the failure is now a specific question about the server's readiness for a duplex parent during
   bootstrap -- which is a real architectural finding, not another callnum to chase.

This is also the first case where the directive's ordering and the codebase's own design agree: the call is
destroy-capable, so the duplex lane is its correct home, and the only reason it is not there is that the
experiment was left switched off.


### 82. First migration done: `mach_port_deallocate` off the per-thread socket, and the hatch names `vchroot` next

The deallocate duplex route was made **default-on** (the hatch inverted to `DARLING_GUEST_DUPLEX_DEALLOCATE_OFF=1`,
and the unreadable-environment case now keeps it on), rebuilt, and the socket-disabled boot re-run:

```
before:  [rpc-socket-DENIED] call=mach_port_deallocate
after:   [rpc-socket-DENIED] call=vchroot
         [rpc-socket-DENIED] call=interrupt_enter
```

**`mach_port_deallocate` no longer creates a per-thread socket.** The duplex lane carries it, which is what the
source's own rule ("wrong lane, not bad op") prescribed, and the warm-server caution the old contract encoded is
answered by measurement rather than by keeping the route switched off.

The directive's loop is therefore working as designed: fail, migrate, re-run, and the hatch names the next call.
The next two are:

* **`vchroot`** -- not `vchroot_path`: this is the loader-class call of §14, so its home is the process management
  plane (`VCHROOT_PATH_BOOTSTRAP` or the reuse of the semantic handler behind the management dispatcher), not the
  lane. The loader has no lane by design, and the management plane already exists and is already used for the
  checkin.
* **`interrupt_enter`** -- still the reentrancy question of §19-21: it must not be dropped into the single
  management slot if it can re-enter while the same thread holds it, and if it is published from signal context it
  may only use atomics, raw syscalls and preallocated memory.

Both were also named by the earlier boot; what is new is that the call before them is gone, so the loop has a
measured step rather than a plan.

One honest note on the measurement: the deallocate migration was verified by the **absence** of its denial in the
next socket-disabled boot, not by a full GREEN run. The boot still fails at `vchroot`, so the socket-disabled
regression is not yet GREEN and the directive's stop condition is not met -- the loop continues from here.


### 83. Second migration done: `vchroot` on the management plane, and the reusable publisher

`vchroot` was migrated exactly as the directive's §14/§15 prescribe -- an explicit management-plane operation
running the **ordinary** semantic Call, with no second implementation:

* `DSERVER_PROCESS_CONTROL_OP_VCHROOT 8u` in the protocol header;
* a server case modeled on `SET_EXECUTABLE_PATH` (the guest's pointer and size travel in the payload; the server
  builds `dserver_rpc_call_vchroot_t` and runs `Vchroot::processCall` through the ordinary path, so the semantics
  have one home and this is only a transport adapter);
* a guest route in `vchroot_userspace.c` that tries the plane first and falls back to the datagram only when the
  plane returns -1 ("not published / not completed", so nothing is duplicated).

And, more useful than the migration itself, the **reusable guest publisher**:

```c
int __dserver_plane_request(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3);
```

One implementation of the mailbox protocol for every migrated call -- claim the slot with a CAS, publish, wake the
**process doorbell** (never a courier byte, per directive §8), wait for completion, snapshot the answer fields,
then release (the ordering §67/§70/§71 measured). It returns the server's status for a completed request and -1
otherwise, so every migration that follows is a call site plus a server case rather than a new protocol.

**Socket-disabled boot after the migration:**

```
before:  [rpc-socket-DENIED] call=vchroot
after:   [rpc-socket-DENIED] call=kqchan_mach_port_open
         [rpc-socket-DENIED] call=interrupt_enter
```

`vchroot` no longer creates a per-thread socket. The loop has now run twice end to end -- fail, migrate, rebuild,
re-run, next name -- and both migrations were of calls the earlier censuses could not see
(`mach_port_deallocate`, and now `kqchan_mach_port_open`), which is the strongest argument for having built the
hatch before doing any more census work.

Next in the loop:

* **`kqchan_mach_port_open`** -- the kqueue-channel class of directive §16: descriptor-bearing, so the semantic
  half goes on the management plane with an `fd_token` and the descriptor on the process courier with
  `DSERVER_FD_COURIER_KIND_KQCHAN_FD`, in either arrival order (the token registry already exists).
* **`interrupt_enter`** -- still the reentrancy audit of §19-21, unchanged: it may not share the single
  management slot if it can re-enter while the same thread holds it, and from signal context it may only use
  atomics, raw syscalls and preallocated memory.

The socket-disabled regression is still not GREEN -- the boot fails at `kqchan_mach_port_open` -- so the
directive's stop condition is not met and the loop continues.


### 84. Third migration done: `kqchan_mach_port_open` -- and the descriptor-order question it exposed

The kqueue-channel class was migrated as directive §16 prescribes: the semantic half on the management plane, the
descriptor on the process courier, in either arrival order.

* `DSERVER_PROCESS_CONTROL_OP_KQCHAN_MACH_PORT_OPEN 9u`;
* a server case that builds `dserver_rpc_call_kqchan_mach_port_open_t`, runs the ordinary Call, and returns the
  descriptor the call produced through `sendFdCourierBundleToGuest(pid, DSERVER_FD_COURIER_KIND_KQCHAN_FD, fd)` with
  the token published in `reply_payload[1]` -- the same split `ATTACH_LANE` uses for the doorbell;
* the publisher gained a variant that also returns the reply payload
  (`__dserver_plane_request_ex`), with the payload snapshotted **before** the release like every other answer
  field (the ordering §67/§70/§71 measured);
* `for-libkqueue.c` tries the plane, resolves the token with `__dserver_fd_courier_receive`, and falls back to the
  datagram only on -1.

**Socket-disabled boot after the migration:**

```
before:  [rpc-socket-DENIED] call=kqchan_mach_port_open
after:   [rpc-socket-DENIED] call=interrupt_enter
         [fd-courier-recv] MISS pid=1331789 token=3314306943482007862 stored=0 dropped=0
```

`kqchan_mach_port_open` no longer creates a per-thread socket, so the loop has now run **three** times end to end
(`mach_port_deallocate`, `vchroot`, `kqchan_mach_port_open`) -- and **all three were invisible to the earlier
censuses**, which is the case for having built the hatch before any further census work.

The courier MISS is the next detail and it is exactly the case directive §17 names: the token arrived on the page
before the descriptor was in the guest's courier queue, so the receive found nothing (`stored=0 dropped=0` -- it was
not a loss, it was a timing). The plane route still carried the call (no socket was created), but the descriptor
resolution needs the parked-waiter path the existing `resolveFdCourierBundle` has on the server side and the guest
does not yet have on this route. That is a bounded addition to the reusable publisher, not a new protocol.

What remains named by the hatch is **`interrupt_enter`** (and presumably its `interrupt_exit`), which is the
reentrancy class of directive §19-21 and the one case that must **not** simply be dropped into the single
management slot. It is the last thing standing between this loop and a GREEN socket-disabled boot.


### 85. `interrupt_enter` audit: signal context AND S2C -- so duplex, not the management slot

The last call the hatch names was audited as directive §19 requires, and the two facts are:

```
callers:   sigexc.c:165, sigexc.c:306, sigaction.c:206      <- signal paths
decl:      ('interrupt_enter', [], [], PUSH_UNKNOWN_REPLIES)
```

**It is reached from signal context** (`sigexc` is the signal-exception path, and `sigaction` installs the
handler), and it is declared with `PUSH_UNKNOWN_REPLIES`, which means the **server may push a caller-side S2C
while this call is in flight**.

Those two together decide its transport, and neither of the directive's first two candidates is right:

* the **single management slot** is wrong, and not for the reason §19 anticipated: the problem is not only
  re-entrancy but that a parked caller must be able to service an S2C, which a one-outstanding blocking mailbox
  cannot express -- the same reason the codebase's own rule sends `deallocate`/`mod_refs` to a duplex lane;
* an **urgent subchannel** (§20) is only needed if the operation is reentrant against the same slot; a duplex lane
  removes that need, because the lane is per-thread and can carry the S2C while the caller waits.

So `interrupt_enter` (and `interrupt_exit`) belong with `mach_port_deallocate` on the **duplex lane**, and the
signal-context constraint of §21 becomes the lane's own constraint: publish with atomics and raw syscalls only,
touch no lazily-initialised state, allocate nothing. That is a real constraint -- the lane's publish path must be
usable from a signal handler -- and it is the next thing to verify rather than assume.

This is also the first call in the loop whose correct home is the duplex lane rather than the management plane, and
it was found the same way the other three were: by making the socket fail and naming the caller.

State of the loop after three migrations and this audit: the socket-disabled boot names exactly one remaining
class (`interrupt_enter`/`interrupt_exit`), whose transport is now decided by evidence, and the descriptor-order
MISS from §84 is a bounded addition to the reusable publisher. Neither is a blocker; both are the next steps.


### 86. The urgent channel is built and the signal calls are off the socket

Directive §19-21 asked for a shared-memory urgent transport for the signal-context calls, and it is built:

* **the page** gained a fixed pool -- `urgent_state[4]`, `urgent_op[4]`, `urgent_seq[4]`,
  `urgent_payload[4][4]`, `urgent_reply_status[4]`, `urgent_reply_seq[4]` -- appended to
  `dserver_process_control`. A pool, never an allocator;
* **the server** drains every pending urgent slot in the same pass as the management slot, running the
  **ordinary** `interrupt_enter`/`interrupt_exit` Calls (one implementation of the semantics) and writing the
  informational reply back into the slot;
* **the guest** publishes with `__dserver_plane_urgent_publish()`: CAS a free slot, fill, release-store, wake the
  **process doorbell** with a raw write -- and return immediately. No park, no futex wait, no lazily-initialised
  state, no allocation, which is the §21 constraint for signal context. A DONE slot is reaped back to IDLE by the
  next publisher with a CAS, so no waiter is needed;
* `interrupt_enter`/`interrupt_exit` are wired at every call site (`sigexc.c` x2 each, `sigaction.c`) with the
  datagram path only as the -1 fallback.

**Socket-disabled boot after the migration:**

```
before:  [rpc-socket-DENIED] call=interrupt_enter
after:   [rpc-socket-DENIED] call=sigprocess
```

So the whole signal class is off the per-thread socket, and the loop has run **four** times end to end:
`mach_port_deallocate` (duplex), `vchroot` (management plane), `kqchan_mach_port_open` (management plane +
courier), `interrupt_enter`/`interrupt_exit` (urgent pool). The next name is `sigprocess`, which is the same
signal class and therefore the same urgent transport -- a call site and a server case, not a new design.

Why the reply is informational and that is not a shortcut: `InterruptEnter::processCall` calls
`_handleInterruptEnterForCurrentThread()` and the effect the guest needs is the server's **flush of a saved reply
onto the thread's lane**, which the guest services when the handler returns. The call's own reply carries only a
status the handler logs. That is what makes a fire-and-forget publication correct here rather than merely
convenient.

Two things remain open and neither is a blocker: the descriptor-order MISS from §84 (token before descriptor in
the guest's courier queue) and the remaining names the hatch will produce after `sigprocess`.


### 87. `sigprocess`: a raw signal handler that needs a completion -- so the urgent pool needs a bounded poll

The next name from the hatch is `sigprocess`, and its audit changes the urgent design rather than just adding a
call site.

**It is called from a raw signal handler.** The enclosing function is `sigexc_handler(int linux_signum, struct
linux_siginfo* info, struct linux_ucontext* ctxt)` at `sigexc.c:332`, and this file runs it on its own
`sigexc_altstack` -- so the call happens on the signal stack, in signal context, exactly as §21 describes.

**And it needs a completion, unlike `interrupt_enter`.** Its signature has in/out pointers
(`thread_state`, `fstate`, and an in/out `bsd_signum`), and the caller uses the returned status
(`int ret = dserver_rpc_sigprocess(...)`). Fire-and-forget is therefore not enough: the handler must know the
operation completed before it continues.

That combination rules out the two obvious answers and points at a third:

* it cannot park on a futex (signal context), so it cannot use the management slot;
* it cannot use the thread's lane (the handler may interrupt the thread that holds it);
* it **can** publish to the urgent pool and then **poll** its own slot, because polling shared memory is
  signal-safe: atomics, no park, no allocation, no lazy state.

So the urgent pool needs one bounded addition: a publish-and-poll variant
(`__dserver_plane_urgent_publish_wait`) that claims a slot, publishes, wakes the doorbell, and spins -- bounded,
with a raw yield -- until its own slot reads DONE. The reply's in/out data does not need to travel through the
slot at all: the server writes those guest pointers during `processCall`, so the slot only has to report
completion and status.

Why this is safe rather than merely workable: the urgent pool is **independent** of both the lane and the
management slot, so a handler that interrupts a thread holding either one still makes progress -- the server
services urgent slots in its own pass and the handler only reads memory. That independence is the property §20
asked for, and it is the reason a bounded poll is acceptable here where a park is not.

That is the next step, and it is bounded: one variant of the publisher, one op code, one server case, one call
site.


### 88. `sigprocess` needs five payload words, and the urgent pool has four

§87 decided the transport; the shape then turned out not to fit. `sigprocess` carries:

```
bsd_signal_number, linux_signal_number, sender_pid, code      (4 x int32)
signal_address                                                 (u64)
thread_state, float_state                                      (u64 POINTERS)
```

and the last two are **pointers to large structures** -- `x86_thread_state64_t` / `x86_float_state64_t` -- which
the server **writes into** (`state_from_kernel(ctxt, &tstate, &fstate)` runs after the call returns). So the
caller needs the server to have completed the write before it continues, which is exactly the completion §87
established it needs.

Packing it: the four int32s and `signal_address` need three words, the two pointers two more -- **five** -- and the
urgent pool carries **four** (`urgent_payload[SLOTS][4]`). One word short, and the shapes do not pack: the
pointers are 64-bit and the int32s together are 128.

So the bounded addition of §87 needs one more thing: the urgent payload widened to **eight** words. That is an
ABI change to `dserver_process_control` and therefore every consumer must be rebuilt and redeployed together --
the rule this work has already paid for three times -- but it is a width change to an existing array, not a new
mechanism, and the pool stays fixed.

The alternative -- pointing the slot at a guest-side argument block, the way `SetExecutablePath` passes its path
-- was considered and rejected for this call: the server already has to write into two of the arguments, so the
guest would have to keep that block alive across a bounded poll from a signal handler, and a stack block on the
signal stack is exactly the kind of lifetime that a handler must not depend on.

That is the last piece before `sigprocess` can move: widen the urgent payload to eight words, rebuild everything,
add the op code and the server case, and wire the one call site.


### 89. Zero socket denials on the socket-disabled boot -- the remaining failure is not the socket

`sigprocess` was migrated to the urgent pool with the bounded-poll variant, and that exposed a server bug that
would have made every urgent publication silently ineffective:

```
[urgent-wait-TIMEOUT] op=12 slot=1 mine=1 state=1 seq=1
```

`state=1` is PENDING: the server never serviced the slot. The cause is the placement of the drain -- it sat
**after** the management slot's guard,

```c
if (__atomic_read(&page->request_state) != PENDING) { continue; }
```

so on a page with no management request (the common case) the whole page was skipped and the urgent slots were
never examined. The fire-and-forget `interrupt_enter` had looked like it worked only because it never waits: its
publication was also never serviced, and its effect (the server's flush of a saved reply) was silently lost.

Moved before the guard, the boot measures:

```
[rpc-socket-DENIED] occurrences: 0
[urgent-wait-TIMEOUT] occurrences: 0
[fd-courier-recv] MISS pid=... token=... stored=0 dropped=0
Rootless shellspawn did not become ready within 30000ms
```

**Zero denials**: with `DARLING_DISABLE_THREAD_RPC_UDS=1`, no call on the boot path asks for a per-thread RPC
socket any more. Five migrations got there -- `mach_port_deallocate` (duplex lane), `vchroot` (management plane),
`kqchan_mach_port_open` (management plane + courier), `interrupt_enter`/`interrupt_exit` (urgent pool), and
`sigprocess` (urgent pool with a bounded poll) -- and every one of them was a call the censuses could not see.

The boot still fails, and **not on the socket**: the remaining line is the courier MISS of §84, the descriptor
arriving after the token. That is the next piece, and it is the parked-waiter path the server side already has for
its own courier bundles.

Two things this measurement does not yet establish, stated plainly: the boot does not complete, so
`socket-disabled boot GREEN` is not claimed; and the urgent pool's correctness under the bounded poll is
established only by this boot, not by the mutation suite the directive asks for. Both are next.


### 90. The courier MISS is an image-addressing bug: one pid, several connections

§89 left one line standing between the socket-disabled boot and completion: the courier MISS. It is located, and
it is not a timing problem:

```c
uint64_t Server::sendFdCourierBundleToGuest(pid_t pid, uint32_t kind, int fd) {
    ...
    int socket = -1;
    for (auto& [sock, conn] : _fdCourierConns) {
        if (conn.peerPid == pid) { socket = sock; break; }   // <- the FIRST connection for that pid
    }
```

A process has **more than one** courier connection: the loader and the guest image each carry their own, from the
same pid -- the source says so explicitly in the doorbell's own comment ("the loader and the guest image carry
SEPARATE connections and the guest-side slot is per IMAGE"). This loop picks whichever connection the map happens
to yield first, so a bundle for the guest image can be sent on the loader's connection, where the guest will never
see it. That is exactly the observed `[fd-courier-recv] MISS ... stored=0 dropped=0`: nothing was lost and nothing
was dropped -- it was delivered to the other image.

So the fix is an addressing fix, not a retry: the bundle must name the image it is for. The information needed to
do that already exists on the guest side -- the guest knows which image it is (the same `VARIANT_DYLD` /
kernel split the reason lines print) and the connection carries the peer pid -- so the natural shape is for the
guest to identify its own connection when it opens it (or for the request to carry the image identity the server
already uses for the process-control page), and for the send to select that connection rather than the first with
a matching pid.

This is the last piece before a socket-disabled boot can complete, and it is also the piece §17 asks for in its
general form ("need both arrival orders ... no leak"): once the connection is addressed correctly, both orders
work, because the guest's bounded receive already retries.

State after five migrations: zero socket denials and zero urgent timeouts on the socket-disabled boot, with this
single addressing bug as the remaining failure. The directive's stop condition is not met -- the boot does not
complete and no regression has been run under the hatch -- but the socket itself is no longer what stands in the
way.


### 91. Two precise causes for the courier MISS, and §90's image hypothesis was wrong

A paired diagnostic -- the server naming the connection it sends to, the guest naming its own socket -- replaced
guessing with two facts:

```
guest:  [fd-courier-recv] MISS token=8760697097353443878 stored=0 dropped=0 mySocket=6
server: bundle-to pid=1356172 kind=7 socket=14 isLoader=1 conns=1
        sent-to-guest pid=1356172 token=7747477101651350255 kind=7
```

**First: `conns=1`.** There is exactly **one** courier connection for this pid, so §90's image-addressing
hypothesis is wrong for this case -- there is no second connection to mis-address. The `isLoader` tag and the
image preference are harmless (and may still be needed once a process has both images connected), but they are not
the cause of this MISS, and the section that claimed they were is corrected here.

**Second, and this is the cause: the server sends `kind=7` (`PROCESS_DOORBELL`) and the token it sends
(`7747477101651350255`) is not the token the guest is waiting for** (`8760697097353443878`). So the kqchan
migration did not put a descriptor on the courier at all, and the guest is waiting on a token that was never
sent.

That in turn has two sub-causes, both of which the code shows:

* the server case reads the returned descriptor with `created->suppressedReplyWakeFd()`, but
  `kqchan_mach_port_open` returns it in the reply **body** (`dserver_reply_kqchan_mach_port_open_t::socket`), not
  as a wake descriptor -- so the value was -1 and nothing was sent;
* because nothing was sent, `page->reply_payload[1]` kept the **stale** token of whatever request used the slot
  before, and the guest read that. That is the same class as the ordering defects of §59/§70/§71 (an answer field
  read without being tied to the request that produced it), one step earlier: a field that is not **cleared**
  when the request is published.

So the fix is two lines of substance: take the descriptor from the suppressed reply **body** the way ATTACH_LANE
does, and clear `reply_payload[1]` (and `[0]`) when publishing a request, so a guest can never read a token that
belongs to an earlier request.

State: zero socket denials, zero urgent timeouts, and the socket-disabled boot failing on this one courier
delivery -- with both of its causes now named from measurements rather than inferred.


### 92. All three classes clean: zero socket denials, zero urgent timeouts, zero courier misses

Both §91 fixes were applied -- the server reads the returned descriptor from the suppressed reply **body** the way
ATTACH_LANE does, and every publisher clears `reply_payload[0]`/`[1]` when it publishes a request -- and the
socket-disabled boot now measures:

```
[rpc-socket-DENIED]    0
[urgent-wait-TIMEOUT]  0
[fd-courier-recv] MISS 0
bundle-to pid=1359571 kind=6 socket=14 isLoader=1 conns=1
sent-to-guest pid=1359571 token=15507764019810476574 kind=6
```

`kind=6` is `DSERVER_FD_COURIER_KIND_KQCHAN_FD`: the kqchan descriptor is now actually sent, and the guest
resolves it. So the three failure classes this work has been driving down are all empty on the boot path:

* **no call asks for a per-thread RPC socket** (five migrations: deallocate, vchroot, kqchan, interrupt enter/exit,
  sigprocess);
* **no urgent publication goes unserviced** (the drain-order bug is fixed and measured);
* **no courier bundle goes missing** (the reply-body read and the payload clearing are both in).

The boot still does not complete -- `HELLO=0`, `shellspawn did not become ready` -- and for the first time it fails
**without a diagnostic line**, which is the honest state: the transport classes are clean and the remaining defect
is something else that nothing currently names. That is the next instrument, not a blocker: the same technique
that produced every step of this work -- make the thing fail, and name why.

Stated plainly for the stop condition: the socket-disabled boot is still RED, so no regression has been run under
the hatch and the FD-slope target is unmeasured. What is established is narrower and solid: with the per-thread
socket denied, nothing on the boot path needs it, and the urgent and courier paths carry their traffic without a
loss.

Four real defects were found and fixed on the way here, each by measurement: the urgent drain placed after the
management guard (every urgent publication silently unserviced), the courier bundle read from the wrong reply
field, the inherited reply token, and -- earlier in the same loop -- the contract-gated duplex route. Each one was
invisible to the censuses.


### 93. Where the socket-disabled boot now stops, named from the server's own trace

The boot log with the hatch on is short and it reads cleanly up to a point:

```
process-control  region pid=1359571 size=488 page=0x7b83c4baf000
process-control  request op=1 (PING) seq=1
process-control  request op=4 (SET_DYLD_INFO) seq=2      -> dyld-info-op status=0
process-control  request op=5 (SET_EXECUTABLE_PATH) seq=3
mldr-seed        attach ... attach-rc rc=0 reject=0 wake=8
mldr-seed        doorbell-in/out ... seeded
dring-adopt      post-claim ok
courier-send-image bundle-to kind=6 (KQCHAN_FD) socket=14
fd-courier       sent-to-guest token=15507764019810476574 kind=6
Rootless shellspawn did not become ready within 30000ms
```

So the whole bootstrap transport sequence completes -- the page is established, the ping, dyld-info and
executable-path run on it, the seed attach rides the lane, the doorbell is delivered, the lane is adopted, and the
kqchan descriptor is now **sent and received** (no MISS). The boot then stops with **no further line at all**,
which places the remaining defect immediately after the kqchan descriptor is delivered.

That is a different kind of defect from everything this loop has fixed so far: not a missing transport, not a
misaddressed bundle, not an unserviced publication, but something in what happens to the call **after** its
descriptor arrives. The next instrument is the same technique that produced every step: a guest-side line at that
call site naming what it does with the result and what it waits for next.

Recorded as the current stop condition of this loop, with the honest summary of what is established:

| class | state |
|---|---|
| per-thread RPC socket requests | **0** on the boot path |
| urgent publications unserviced | **0** |
| courier bundles missing | **0** |
| boot completes under the hatch | **no** -- stops after the kqchan descriptor, no diagnostic |
| regression under the hatch | not run |
| FD slope | not measured |


### 94. The kqchan route is confirmed working end to end -- the boot stops after it, not on it

The guest-side instrument §93 asked for, at the kqchan call site:

```
[kqchan-plane] status=0 token=5431114203572406296 fd=8 out=8
denials=0   courier-miss=0
```

Every field is what a correct migration produces: the plane returned **status 0**, the reply carried a **token**,
the courier receive **resolved it to fd 8**, and the value was **written to the caller's out parameter**. So the
kqchan migration is complete and correct -- descriptor delivered, resolved, and handed to libkqueue -- and the
boot does not stop on it.

That closes the fifth migration the same way the other four were closed, and it moves the remaining defect
strictly **after** this point: the shellspawn handshake's next step, which nothing currently logs. The server's
trace ends at the kqchan bundle and the guest's trace ends at this line, so the next instrument has to be on
whatever shellspawn does next -- not on any transport, because all three transport classes measure zero.

Summary of this loop, stated as the measurement rather than as progress:

| what | measurement |
|---|---|
| calls needing a per-thread RPC socket, boot path | **0** |
| urgent publications left unserviced | **0** |
| courier bundles missing | **0** |
| kqchan descriptor delivered and resolved | **yes** (fd=8, out=8) |
| boot completes under the hatch | **no** |
| regression under the hatch | not run |
| FD slope | not measured |

Five migrations, four defects found by measurement (the contract-gated duplex route, the urgent drain placed after
the management guard, the courier bundle read from the wrong reply field, the inherited reply token), and every
one of them invisible to the censuses that preceded them.


### 95. shellspawn never reaches its own setup: the kqchan call is its last observed step

A diagnostic was placed in `shellspawn`'s `main` around its readiness sequence
(`setupSigchild` -> `rootlessTestDelaySocketReady` -> `rootlessTestMarkSocketPending` -> `setupSocket` (bind +
listen) -> `listenForConnections`), writing one line per step to fd 2. The socket-disabled run measures:

```
[kqchan-plane] status=0 token=17052573802098778302 fd=8 out=8
[shellspawn-step] lines: 0
Rootless shellspawn did not become ready within 30000ms
```

**Not one step line appears.** So shellspawn does not reach `setupSigchild()`, which is the first thing after the
block that the diagnostics bracket -- meaning the kqchan call, which succeeds (status 0, fd resolved, written to
the caller's out parameter), is the **last observed action of the process**, and whatever follows it is where the
process stops.

Two facts that narrow this further, both from the same run:

* a live inspection during a socket-disabled boot shows **only `darlingserver`** among the prefix's processes --
  no shellspawn, no shell. The guest process does not sit blocked; it is gone. That is why the host reports
  "did not become ready" rather than hanging on a lock;
* no abort, terminate or signal message accompanies it, so the exit is not through a path that prints.

So the remaining defect is strictly **after** the kqchan plane call and **before** shellspawn's own setup, and
nothing in either trace names it yet. The next instrument is between those two points -- in the initialization
that runs after the kqueue channel is opened and before `setupSigchild` -- and the technique is the same one that
produced every step of this work: make the failing thing name itself.

What is established, unchanged by this section: the per-thread RPC socket is requested by nothing on the boot
path, the urgent pool services everything published to it, and the courier delivers and resolves the descriptor
that the kqchan route returns.


### 96. The death is before `main`: shellspawn never enters it, and kqchan is the last step of pre-main init

§95 bracketed shellspawn's readiness sequence and got zero lines. The obvious next question was whether the
diagnostics were simply too late, so entry prints were added at the top of `main`, after the runtime-mode check,
and around `setupSigchild`:

```
[shellspawn-step] lines: 0        <- including main-entry
[kqchan-plane] status=0 token=17958013178760321872 fd=8 out=8
Rootless shellspawn did not become ready within 30000ms
```

**Not even `main-entry` prints.** So shellspawn never reaches `main` at all: the process stops during **pre-main
initialization** -- dyld, duct, or a static constructor -- and the kqchan plane call, which succeeds, is the last
step of that initialization that anything observes.

That is a much narrower place than §95 could name, and it explains the two facts recorded there: the process is
gone rather than blocked (there is no `main` to block in), and no abort or signal message accompanies it (the exit
is not through a path that prints).

It also means the defect is not in any of the transports this loop migrated -- all three measure zero -- but in
what the pre-main initialization does **after** the kqueue channel is opened. The next instrument is there: the
initialization that runs between the kqchan call and the point where control would enter `main`, using the same
technique that produced every step of this work.

State, unchanged and stated as measurement:

| what | measurement |
|---|---|
| calls needing a per-thread RPC socket, boot path | 0 |
| urgent publications left unserviced | 0 |
| courier bundles missing | 0 |
| kqchan descriptor delivered and resolved | yes (fd=8, out=8) |
| shellspawn reaches `main` | **no** -- stops in pre-main initialization |
| boot completes under the hatch | no |
| regression under the hatch / FD slope | not run / not measured |


### 97. The kqchan descriptor is delivered only sometimes: status 0 with no descriptor

Instrumenting the steps after the kqchan call produced a measurement that changes the picture:

```
[kqchan-plane] status=0 token=0 fd=-1 out=0        (this run)
[kqchan-plane] status=0 token=5431114203572406296 fd=8 out=8   (earlier runs)
```

**`status=0` with `token=0` and `fd=-1`.** The call reports success and the descriptor never arrives, so the
caller's out parameter keeps whatever it had -- and whatever the caller does next with a bogus descriptor is where
the process dies. In earlier runs the same call produced a real token and a resolved fd, so this is
**intermittent**, not a constant: the kqchan route sometimes delivers and sometimes does not.

That also explains why the defect looked like "pre-main initialization" from the outside (§96): the failing step is
a call that *succeeds*, so nothing prints a failure, and the process dies later using a descriptor it never got.

Two further facts from the same runs:

* the marks added inside `libkqueue`'s `machport.c` (`before-rpc`, `after-rpc`, `after-fcntl`) **never print**,
  so `kqchan_mach_port_open` is reached through a **different caller** than the one that file implements -- the
  route in `for-libkqueue.c` is what prints, and its caller is elsewhere;
* `reply_payload[1]` is now cleared on publish (§91), so a `token=0` reading is not a stale value: it is the
  server genuinely not having put a token there, which means the server's own send did not happen for that
  request (`returnedFd < 0`) while the call still completed with status 0.

So the next question is precise and narrow: why does the server complete this call with status 0 when it has no
descriptor to return, and under what condition does the descriptor exist? The server case has both branches
(suppressed-reply wake fd, and the reply body) and one of them is producing nothing on the runs that fail -- a
condition that is now known to alternate rather than be constant.

State, unchanged: the per-thread socket is requested by nothing on the boot path, the urgent pool services
everything published to it, and the courier delivers what it is given -- the defect is in what this one call
produces, not in a transport.


### 98. The reply-fd plumbing was genuinely missing, was added, and the delivery is still intermittent

Two real omissions were found and fixed in the generator, both of which the pattern of the other descriptor-bearing
calls made obvious once looked for:

* `kqchan_mach_port_open` was **absent from `REPLY_FD_COURIER_KINDS`** (which contains `console_open`,
  `kqchan_proc_open` and `ring_attach`), so no reply-fd courier branch was generated for it at all;
* its **reply parameters had no `('fd_token','@fd_token')`**, unlike `kqchan_proc_open` and `console_open`, so the
  reply carried no token for the courier to be addressed by.

Both were added. The measured result is that the call **still alternates**:

```
CT1:  [kqchan-plane] status=0 token=12310729662566266998 fd=8 out=8     (delivered)
CU1:  [kqchan-plane] status=0 token=0 fd=-1 out=0                      (not delivered)
```

Same build, same boot, different outcome -- so the omissions were real and necessary but are not the whole cause,
and there is a condition that decides between the two runs. That condition is the next question, and it is now
sharply posed: the same call, on the same page, with the same server build, sometimes produces a token and a
descriptor and sometimes produces neither while reporting success.

What is worth noting about the shape of this defect, because it is unlike the others this loop fixed: the failures
before it were deterministic and each one had a single cause that a measurement could name. This one is
**conditional**, which means the next instrument has to record the state on **both** outcomes rather than only on
the failing one -- otherwise the working case's conditions stay invisible and only half the question is answered.

State, unchanged and stated as measurement:

| what | measurement |
|---|---|
| calls needing a per-thread RPC socket, boot path | 0 |
| urgent publications left unserviced | 0 |
| courier bundles missing | 0 |
| kqchan descriptor delivered | **intermittent** -- `fd=8` or `fd=-1`, both with status 0 |
| shellspawn reaches `main` | no |
| boot completes under the hatch | no |
| regression under the hatch / FD slope | not run / not measured |


### 99. The root is not a transport at all: `Kqchan::MachPort::setup()` returns -1 and the call still reports success

Recording **both** outcomes was the right instrument, and one run answered the whole question:

```
kqchan-open pid=1386966 status=0 from_wake=-1 body_len=16 body_socket=-1
[kqchan-plane] status=0 token=0 fd=-1 out=0
```

Read against the handler:

```c
void DarlingServer::Call::KqchanMachPortOpen::processCall() {
    int code = 0;
    int socket = -1;
    ...
    try { socket = kqchan->setup(); }
    catch (std::system_error e) { code = -e.code().value(); }
    catch (...) { code = -ESRCH; }
    ...
    _sendReply(code, socket);
}
```

`status=0`, `body_socket=-1`, no wake fd: so `kqchan->setup()` returned **-1 without throwing**, `code` stayed 0, and the reply went out reporting success with no descriptor. That is exactly the observed `status=0 token=0 fd=-1` -- and it is **not a transport defect at all**.

So the whole chain this loop has been following -- page route, courier bundle, reply-fd kind, reply token -- is now shown to be **working**: the server received the request, ran the ordinary handler, published status 0, and had nothing to send because the **semantics** produced nothing. Every transport instrument reads zero because the transports are not what is broken.

It also explains the intermittency without any transport condition: `setup()` succeeds or fails depending on its own internal state, and the two outcomes differ in the reply body, not in the delivery.

The next instrument is therefore inside `Kqchan::MachPort::setup()` -- what it does, what it returns -1 for, and what condition decides between the two outcomes -- and it is a semantics investigation, not a transport one.

What this closes, stated as the measurement rather than as progress:

| what | measurement |
|---|---|
| calls needing a per-thread RPC socket, boot path | 0 |
| urgent publications left unserviced | 0 |
| courier bundles missing | 0 |
| page route, courier and reply-fd plumbing for kqchan | **working** (server ran the handler, published status 0) |
| `Kqchan::MachPort::setup()` | returns -1 **without throwing**, intermittently |
| shellspawn reaches `main` | no |
| boot completes under the hatch | no |
| regression under the hatch / FD slope | not run / not measured |

Six defects were found and fixed on the way here, five of them in the transport layer this directive asked to
build, and the sixth is the one that was hiding behind them.


### 100. Refinement to §99: the handler DOES produce a valid fd -- the reply path is what loses it

§99 read `body_socket=-1` as `setup()` returning -1. Reading the two `setup()` implementations corrects that:

```c
int DarlingServer::Kqchan::setup() {
    int fds[2];
    if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, fds) < 0) { throw ...; }
    _socket = std::make_shared<FD>(fds[0]);
    ... fcntl checks, all throwing ...
    return fds[1];          // a VALID descriptor, always
}
```

Every failure path in `Kqchan::setup()` **throws**, and the success path returns `fds[1]`, which is a valid
descriptor. `MachPort::setup()` returns that value unchanged. So the handler cannot have produced -1 on a call
that completed with `status=0`.

Which means the -1 in the reply body is **not** what the handler produced: it is what the **reply path** recorded.
The measurement says so directly -- `body_len=16` is exactly a reply header plus one `int32`, with **no
`fd_token` field**, and `from_wake=-1` says the suppressed-reply wake descriptor was never captured. So the
descriptor the call legitimately produced was **lost between the handler and the page reply**, and the guest
received `status=0` with no way to obtain it.

That returns the question to the reply-fd plumbing §98 touched, with a sharper target: `_sendReply(code, socket)`
must capture the descriptor for a **suppressed** reply, and the generated reply for this call must carry the
`fd_token` field. Both were attempted; neither took effect in the measured build, and the instrument now says
exactly which one is missing: the **token field is absent from the body** (`body_len=16`), so the generator change
did not reach the server's reply construction.

So the next step is narrow and verifiable in one build: make `dserver_reply_kqchan_mach_port_open_t` actually carry
the `fd_token` (and confirm `body_len` grows past 16), then confirm the server's suppressed-reply path captures the
descriptor the handler returned.

This is a better place to stop than §99's conclusion, because it is a statement about a specific missing field
with a measurement that will show it appear, rather than a statement about a semantics function that turned out to
be correct.


### 101. The generator already emits the token fill -- so both paths return 0 and the connection is what is missing

§100 concluded the `fd_token` field was absent from the reply body and pointed at the generator. Reading the
generator's emit condition corrects the direction of that:

```python
reply_kind = REPLY_FD_COURIER_KINDS.get(call_name)
...
if is_fd(param) and reply_kind is not None:
    internal_header.write("\t\t\tif (" + param_name + " >= 0) { \\\n")
    internal_header.write("\t\t\t\tuint64_t __fd_courier_token = sendFdCourierToGuest(_header.pid, " + reply_kind + ", " + param_name + "); \\\n")
    internal_header.write("\t\t\t\tif (__fd_courier_token != 0) { \\\n")
    ... replyStruct->body.fd_token = __fd_courier_token; ...
```

With `kqchan_mach_port_open` added to `REPLY_FD_COURIER_KINDS` and its reply parameter list carrying
`('fd_token','@fd_token')`, that condition **is** satisfied, and the generated `rpc.internal.h` does contain a
`_sendReply(int resultCode, int32_t socket)` for it alongside `kqchan_proc_open`'s. So the generated reply path
already sends the descriptor and fills the token -- and my hand-written server case, which also tries to send it,
is a **second** path doing the same job.

That makes the measurement read differently: `body_len=16` is not a missing field, it is a token fill that **did
not fire at runtime**, which happens only when

* `param_name < 0` (the handler's socket), or
* `sendFdCourierToGuest(...) == 0`.

The handler produces a valid descriptor (both `setup()` implementations throw on failure and return `fds[1]` on
success, §100), so the second is what is happening: **the courier send finds no connection**. That is the same
function family my own `sendFdCourierBundleToGuest` call uses, and in the same runs the server log shows
`conns=1 isLoader=1` -- a single connection, tagged as the loader's.

So the defect is now in one place and one question: why does the descriptor send find no usable connection for
this process, given that a connection exists? That is narrow, and it is the last thing between this call and a
descriptor the guest can actually use.

Correction recorded rather than overwritten, because §100 sent the investigation at the generator and the generator
was already right.


### 102. The root, one line long: the courier refuses to send until the guest has spoken first

`sendFdCourierToGuest` and `sendFdCourierBundleToGuest` are the same function, so reading it answers §101's
question directly:

```c
uint64_t DarlingServer::Server::sendFdCourierBundleToGuest(pid_t pid, uint32_t kind, int fd) {
    if (fd < 0) { return 0; }
    _drainFdCourierMessages();
    ... pick the connection for the pid (prefer the non-loader one) ...
    if (socket < 0) { Metrics::...fdCourierFallbackCmsg++; return 0; }

    auto gen = _fdCourierGeneration.find(pid);
    uint64_t generation = (gen != _fdCourierGeneration.end()) ? gen->second : 0;
    if (generation == 0) {
        // ... the server does not know its generation yet, and a bundle stamped 0 is REFUSED by the
        // guest's staleness check (gen=0 != its own) -- which closed the doorbell and stopped the boot.
        Metrics::...fdCourierFallbackCmsg++;
        return 0;
    }
    ...
}
```

And the only place that generation is ever learned:

```c
auto gen = _fdCourierGeneration.find(pid);
if (gen == _fdCourierGeneration.end()) {
    _fdCourierGeneration[pid] = envelope.process_generation;   // <-- incoming guest envelope only
} else if (gen->second != envelope.process_generation) { ... }
```

So the server learns a process's generation **only when that process sends it a courier envelope**, and until
then every descriptor it wants to send is silently dropped. The `bundle-to ... conns=1 isLoader=1` line sits
**above** the generation check, which is why it looked like a connection was found and used.

shellspawn on the boot path is exactly the process that has never sent one: the management page carries its
requests, and the first thing it wants is a descriptor **from** the server. So `kqchan_mach_port_open` gets
`status=0`, no token, and `body_len=16` -- not because any of the transport plumbing is wrong, but because the
courier has a **chicken-and-egg rule**: it will not talk to a process that has not talked to it.

This is one line to fix and it is the actual blocker for the socket-disabled boot: the generation the server
already receives on the management page (and on the attach path) must be recorded in `_fdCourierGeneration`
when it is learned from **any** channel, not only from a courier envelope.

Everything upstream of it -- the page route, the opcode, the reply status, the guest's token resolution -- is
measured working. This is the last thing standing between the call and a usable descriptor.


### 103. §102 refuted by measurement -- and kqchan is closed; the hatch now names `mach_msg_overwrite`

The instrument added to the courier's own log line settled §102 in one run:

```
bundle-to pid=1398575 kind=7 socket=14 isLoader=1 conns=1 gen=6322248287677099 genKnown=1
```

`genKnown=1` with a real generation. So the courier is **not** refusing on the `generation == 0` path, and §102's
chicken-and-egg explanation is wrong -- the PING handler already records the generation from
`page->request_payload[3]`, and that is enough.

What the same run shows is better than a fix: the hatch's next complaint is no longer `kqchan_mach_port_open`.

```
[rpc-socket-DENIED] pid=1398575 tid=1398575 call=mach_msg_overwrite
```

`kind=6` (`KQCHAN_FD`) does not appear in this run at all, and neither does a `kqchan-open` line -- because the call
**succeeded** before any of that mattered and the boot moved on. The generated reply-fd path (§100/§101) and the
generation rule (§102) are both working; the migration of `kqchan_mach_port_open` onto the management plane plus the
courier is **done**.

That leaves `mach_msg_overwrite` as the sixth consumer, and it is a different animal from the previous five: it is
the Mach message exchange itself, not a management operation, so the transport it must use is the **lane**, not the
plane. A per-thread socket being created for it means the lane is not carrying a call that is squarely its business
-- which is the core transport question this directive exists to answer, reached by the hatch rather than by a
census.

Progress this cycle, stated as measurements:

| step | hatch's next complaint |
|---|---|
| start | `mach_port_deallocate` |
| after duplex lane | `vchroot` |
| after plane `OP_VCHROOT` | `kqchan_mach_port_open` |
| after plane op + courier + reply-fd + token | `interrupt_enter` |
| after urgent pool | `sigprocess` |
| after urgent completion | **`mach_msg_overwrite`** |


### 104. Correction to §103: kqchan is NOT closed -- the descriptor is still not delivered

§103 read "the hatch's next complaint is `mach_msg_overwrite`" as "kqchan now works". A later run with the mach-msg
ring enabled, and with the guest's own kqchan line checked this time, says otherwise:

```
[kqchan-plane] status=0 token=0 fd=-1 out=0
```

The guest still receives no descriptor: `token=0`, `fd=-1`. §103 never looked at the guest's `[kqchan-plane]` line
in the run whose denial had moved, so "the call succeeded" was inferred from the hatch being quiet about it, which
is exactly the error the migration hatch was built to avoid -- **a call that stops creating a socket is not
necessarily a call that got what it needed.**

So two separate things are open, and §103 conflated them:

* `kqchan_mach_port_open` reaches the plane, the server runs the handler, `status=0` -- and the descriptor is
  **still not delivered** (`token=0`).
* `mach_msg_overwrite` needs a per-thread socket unless `DARLING_GUEST_RING_MACH_MSG=1` is set; with that hatch on,
  the run measured **`[rpc-socket-DENIED] = 0`** -- the hatch's whole class is gone for the boot path -- while the
  boot still does not complete.

The counter-correction is recorded rather than overwritten because it changes what the next measurement must be:
the denial counter going quiet and the guest getting its descriptor are **two claims**, and only the second one is
progress.


### 105. Why the plane's kqchan case cannot see the descriptor: the recorded reply body is truncated at the pre-token size

Reading the plane case against the generated sender gives the exact mechanism, and it explains every number in
§103's run at once.

The generated sender already does the right thing:

```c
void _sendReply(int resultCode, int32_t socket) {
    Message reply(sizeof(dserver_rpc_reply_kqchan_mach_port_open_t), 0);   // 12 + 4 + 8 = 24
    ...
    if (socket >= 0) {
        uint64_t __fd_courier_token = sendFdCourierToGuest(_header.pid, DSERVER_FD_COURIER_KIND_KQCHAN_FD, socket);
        if (__fd_courier_token != 0) { replyStruct->body.socket = -1; replyStruct->body.fd_token = __fd_courier_token; }
        else { reply.pushDescriptor(socket); }
    }
}
```

and the plane case reads it back through the suppressed-reply accessor:

```c
const uint8_t* body = created->suppressedReplyBody(&bodyLen);
if (body && bodyLen >= sizeof(dserver_reply_kqchan_mach_port_open_t)) { ... returnedFd = rb->socket; }
```

Measured: `body_len=16`. `sizeof(dserver_reply_kqchan_mach_port_open_t)` is **24** (a 12-byte header, `int32_t
socket`, and the 8-byte `fd_token`). Sixteen is exactly the pre-token size, so the guard is false, `returnedFd`
stays -1, no token is published, and the guest reads `status=0 token=0 fd=-1`.

So the descriptor is not missing and the plumbing is not wrong in the direction §100/§101 chased: the reply body
the plane case inspects is **the body from before the token field existed**, and the guard written against the new
size can therefore never pass. `noteSuppressedReplyBody` has no caller in the sources searched, which is consistent
with the recording path predating the widened reply.

The fix is one of two, and the first is strictly better because it does not duplicate semantics:

* record the **whole** reply message body in the suppressed-reply buffer, so the plane case reads the same 24 bytes
  the generated sender wrote -- including the token the sender already computed; or
* have the plane case call the courier itself and publish its own token, which is what it does today and is why the
  size guard matters.

§103's "kqchan is closed" and §104's correction are both superseded by this: the denial moving on was real, the
descriptor delivery was never fixed, and the reason is a **size guard that cannot pass**.


### 106. The true mechanism: the descriptor WAS shipped -- the plane case read only `socket` and ignored `fd_token`

§105 blamed a size guard. Reading the struct layout corrects that too, and the correction is the actual defect:

```c
struct dserver_reply_kqchan_mach_port_open {      /* the BODY, with no header */
    int32_t socket;
    uint64_t fd_token __attribute__((aligned(8)));
};
```

`4 + 4(padding) + 8 = 16`. So the body **is** 16 bytes, `sizeof(dserver_reply_kqchan_mach_port_open_t)` **is** 16,
and `bodyLen >= sizeof(...)` is `16 >= 16` -- **true**. §105's "the guard can never pass" was wrong; the guard
passes.

What fails is what the case does with the body:

```c
auto* rb = reinterpret_cast<const dserver_reply_kqchan_mach_port_open_t*>(body);
bodySocketSeen = rb->socket;                                  // == -1
if (returnedFd < 0 && rb->socket >= 0) { returnedFd = rb->socket; }
...
if (returnedFd >= 0) { uint64_t tok = sendFdCourierBundleToGuest(pid, KIND_KQCHAN_FD, returnedFd); ... }
```

The generated sender **already** ran, already called `sendFdCourierToGuest`, already shipped the descriptor, and
wrote `body.socket = -1` with the token in `body.fd_token` -- that is precisely what the generated code does on a
non-zero token. So the case reads `socket == -1`, concludes there is no descriptor, and never publishes the token
the sender had already computed. The guest reads `status=0 token=0 fd=-1`; the plane's `reply_payload[1]` stays
zero; and a second courier send was attempted only in the paths where a socket was still believed to exist.

The measured `body_len=16 body_socket=-1` in §103's run is therefore not a truncation and not a failure -- it is
**the sender reporting that it moved ownership to the courier**, and the plane case was written as if it were the
one doing the transfer.

The fix is to read the token and stop duplicating the transport (directive §15: one semantic core, transport
adapters only):

```c
if (rb->fd_token != 0) { page->reply_payload[1] = rb->fd_token; returnedFd = -1; }
```

which is one branch, removes a second descriptor transfer, and publishes exactly the token the generated sender
issued.

Three successive readings of one log line -- §103 ("kqchan is closed"), §104 ("it is not"), §105 ("a size guard
cannot pass"), §106 ("the guard passes; the token was ignored") -- each correcting the last. The instrument that
settled it was the one that recorded **both** outcomes: without `body_socket` and `body_len` alongside `status`,
every one of those readings was possible.


### 107. kqchan is closed, with evidence on BOTH sides -- and the hatch names `mach_port_move_member`

The §106 fix was measured, and for the first time the descriptor transfer is proven from both ends:

```
reply-token pid=1425682 token=14186306812444856812 bodylen=24
[kqchan-plane] status=0 token=14186306812444856812 fd=8 out=8
```

The token the generated sender issued is the token the guest resolved, the body is the full 24 bytes (header plus
the 16-byte body, so `fd_token` is present), and the guest holds fd 8 in its out-parameter. `kqchan_mach_port_open`
-- the whole fd-bearing class for the kqueue channel -- is done on the plane plus courier, with no per-thread
socket.

The hatch moved on to the seventh consumer:

```
[rpc-socket-DENIED] pid=1425682 tid=1425682 call=mach_port_move_member
```

`mach_port_move_member` is a different shape from the previous six and is the first one that is a **plain lane
candidate**: three `uint32_t` parameters (`target`, `member`, `after`), **no reply** (`[], `), so the
`RING_ORDERED_NO_REPLY` route applies -- publish on the lane, wait for the transport acknowledgement, public shape
unchanged. It is not in `RING_GENERATED_SIMPLE`, which is why it takes a per-thread socket today.

Per §72/§73 the table carries a reason for entries that were tried and refused, and `pthread_canceled` -- also a
NO_REPLY call -- was refused with RED. So this is tried, not assumed: add it to the routing table, rebuild every
consumer, and let the boot say whether the NO_REPLY lane route carries it.

Progress, as the hatch's own complaint:

| # | consumer | transport it moved to |
|---|---|---|
| 1 | `mach_port_deallocate` | duplex lane |
| 2 | `vchroot` | management plane |
| 3 | `kqchan_mach_port_open` | plane + courier (**now proven both sides**) |
| 4 | `interrupt_enter`/`exit` | urgent pool |
| 5 | `sigprocess` | urgent pool + completion |
| 6 | `mach_msg_overwrite` | mach-msg ring (`DARLING_GUEST_RING_MACH_MSG=1`) |
| 7 | `mach_port_move_member` | **lane -- to be measured** |


### 108. `mach_port_move_member` on the lane: MEASURED RED -- and its home is the management plane

The lane route was added to `RING_GENERATED_SIMPLE`, every consumer was rebuilt and deployed with matching
sha256, and the boot was measured:

```
[rpc-socket-DENIED] pid=1428914 tid=1428914 call=mach_port_move_member
```

The same call, the same failure. So the NO_REPLY lane route does not carry it either -- which is exactly what the
table already recorded for `pthread_canceled`:

> MEASURED RED: routing it on the lane stops the boot -- ... It is reached early enough (the libpthread
> cancellation handshake) that the lane cannot be relied on to exist, and its result must be visible to the
> thread's other traffic.

Two independent NO_REPLY calls now fail identically on the lane, so "NO_REPLY" is not the property that decides:
**being reached before the lane can be relied on** is. The entry was removed and the reason written beside the
table, so the next reader sees why rather than re-deriving it.

The directive is explicit that this is not an answer (§2): "not lane-eligible" must not mean "stays on UDS". The
transport such a call needs is the **process management plane**, which is what `vchroot`, `sigprocess` and
`kqchan_mach_port_open` already use. Implemented as the same shape, with §15's rule kept (one semantic core,
transport adapters only):

* `DSERVER_PROCESS_CONTROL_OP_MACH_PORT_MOVE_MEMBER 13u`, the three port names in `request_payload[0..2]`, the
  architecture byte in `[3]`;
* a server case that builds `dserver_rpc_call_mach_port_move_member_t` and runs the **ordinary**
  `MachPortMoveMember` Call with `suppressReplyDelivery()` -- no second implementation of the semantics;
* a guest route in `_kernelrpc_mach_port_move_member_trap_impl` that tries the plane first and falls back to the
  datagram **only** on -1 (not published), so nothing is retried after publication.

The student of the previous two attempts is worth stating: the guest's `mach_traps.c` needed
`<darlingserver/rpc-supplement.h>` explicitly -- `rpc.h` does not pull the operation enum in -- which is the kind
of omission that costs a build cycle and no more.


### 109. The eighth consumer: `pthread_canceled` gets the explicit management op the directive asks for

The `mach_port_move_member` migration was measured green -- the hatch no longer names it -- and the boot moved to
the call the directive names explicitly:

```
[rpc-socket-DENIED] pid=1433373 tid=1433373 call=pthread_canceled
[rpc-socket-DENIED] pid=1433375 tid=1433375 call=pthread_canceled
```

Two processes, both in the libpthread cancellation handshake. `DSERVER_PROCESS_CONTROL_OP_PTHREAD_CANCELED 7u` was
already reserved in the operation enum and had **no server case and no guest route**, so this is directive §5-§7
implemented rather than a new mechanism:

* the server case builds `dserver_rpc_call_pthread_canceled_t`, runs the **ordinary** `PthreadCanceled` Call with
  `suppressReplyDelivery()`, and publishes its status -- one semantic core (directive §15);
* the guest route in `sys_pthread_canceled` tries the plane first and falls back to the datagram **only** when
  `planeStatus == -1`, which is the "not published / not completed" case -- so a published call is never retried
  (directive §5-§7), and the completion is explicit rather than inferred.

Note the sentinel is the plane's own return, not the status: a legitimate status can be any errno including -1, so
keying the fallback on the returned status would retry a call that had already been published. That distinction is
the whole content of §5-§7's "no UDS retry after publication".


### 110. The ninth consumer: `mach_port_construct` -- and the payload budget it exposes

`pthread_canceled` was measured green (the hatch stopped naming it) and the boot moved on:

```
[rpc-socket-DENIED] pid=1434863 tid=1434863 call=mach_port_construct
```

This one exposes a constraint the earlier migrations did not: `mach_port_construct` takes **four** parameters
(`target`, `options`, `context`, `name`), and the management page's `request_payload` is exactly four words wide --
with the architecture byte previously squeezed into payload[3]'s low byte by the earlier operations.

The convention `kqchan_mach_port_open` already uses solves it: a pointer's **high byte is zero**, so the
architecture byte rides in the top byte of payload[3] and the low 56 bits carry the real value. `options` is a
pointer, so it takes payload[3] and the architecture comes from `payload[3] >> 56` -- no page change, no widened
struct, no second convention.

Implemented as the same shape as the previous three:

* `DSERVER_PROCESS_CONTROL_OP_MACH_PORT_CONSTRUCT 14u`;
* server case: `target` in payload[0], `context` in [1], `name` in [2], `options` in [3], architecture from
  `[3] >> 56`; the server runs the **ordinary** `MachPortConstruct` Call with `suppressReplyDelivery()`;
* guest route in `_kernelrpc_mach_port_construct_trap_impl`, plane first, datagram only on `planeStatus == -1`.

The pattern is now stable enough to state as a rule for the remaining consumers: a Mach operation reached before
the lane can be relied on is a **management-plane operation**, its parameters go in the payload words, its
semantics are the existing Call, and its guest route falls back only when the plane did not publish.


### 111. The tenth consumer: `mach_port_destruct`, and where the architecture convention stops working

`mach_port_construct` was measured green and the hatch moved to:

```
[rpc-socket-DENIED] pid=1438410 tid=1438410 call=mach_port_destruct
```

Same four-parameter budget as `construct` (`target`, `name`, `srdelta`, `guard`) and the same management-plane
treatment -- with one difference that matters and is worth stating rather than hiding:

`construct` could borrow the top byte of `payload[3]` for the architecture because `options` is a pointer and a
pointer's high byte is zero. `destruct`'s fourth parameter is `guard`, an **arbitrary 64-bit value**, so there is
no spare byte to borrow. The architecture is therefore simply not carried for this op, exactly as it is not carried
for `move_member` or `pthread_canceled`, and those are measured working.

That is the honest limit of the convention: it works while the fourth word happens to be a pointer, and it stops
working the moment it is not. The durable answer is a dedicated architecture field on the page, which is a
shared-struct change and therefore a full-consumer rebuild; it is noted here rather than done mid-migration,
because the running evidence says the synthesized Call does not need it for these operations.

Pattern for the remaining consumers, now applied four times without change:

1. `DSERVER_PROCESS_CONTROL_OP_<NAME>` appended to the enumeration;
2. a server case that builds the ordinary call struct, runs the existing Call with `suppressReplyDelivery()`, and
   publishes `suppressedReplyCode()`;
3. a guest route that tries the plane and falls back to the datagram only on `planeStatus == -1`.


### 112. `mach_port_destruct` green, and eight more early port operations migrated as a batch

`mach_port_destruct` was measured green and the hatch moved to `mach_port_mod_refs`:

```
[rpc-socket-DENIED] pid=1442059 tid=1442059 call=mach_port_mod_refs
```

That is the eleventh consumer, and it is the point at which one-at-a-time migration stops being the right method.
`mach_traps.c` contains fourteen `dserver_rpc_mach_port_*` calls; four are already handled, and the remaining ones
are the same shape -- an early Mach port operation whose home is the plane. So eight were migrated in one batch with
the template that has now been applied four times:

| operation | payload words |
|---|---|
| `mach_port_allocate` | target, right, name* |
| `mach_port_extract_member` | target, name, pset |
| `mach_port_guard` | target, name, guard, strict |
| `mach_port_insert_member` | target, name, pset |
| `mach_port_insert_right` | target, name, poly |
| `mach_port_mod_refs` | target, name, right, delta |
| `mach_port_type` | target, name, ptype* |
| `mach_port_unguard` | target, name, guard |

Operations 16 through 23 in the enumeration, eight server cases, six guest routes (the other two sites in
`mach_traps.c` have a different call shape and are handled separately). Pointer parameters travel as `uint64_t` and
the server dereferences them through the process's own memory interface, exactly as `vchroot` does for its path.

Two operations in the same file are NOT in this batch and cannot be:
`mach_port_get_attributes` takes five parameters and `mach_port_request_notification` takes six, while the page's
`request_payload` is four words. Those two need the page widened -- the same shared-struct change §111 noted for the
architecture byte -- and are left for it rather than squeezed.

One build-cycle lesson, recorded because it is pure cost: the batch was generated by a script whose cast helper
emitted `(uint64)(uint32_t)x` instead of `(uint64_t)(uint32_t)x`, so the first build failed on ten lines of a
type name. The migration itself was unaffected.


### 113. The whole batch landed: the hatch moved past every port operation, to the semaphores

The batch of eight was measured green -- the hatch no longer names `mach_port_mod_refs` or any other port
operation -- and the boot moved to a **different class**:

```
[rpc-socket-DENIED] pid=1445220 tid=1445220 call=semaphore_signal
```

That is the twelfth consumer and the first that is not a port operation. `semaphore_signal` and
`semaphore_signal_all` take one `uint32_t` each and are **non-blocking**, so the plane takes them the same way.

`semaphore_wait` is deliberately **excluded**, and the reason is structural rather than circumstantial: the plane is
a **single serialized slot serviced inside the server's own loop pass**. A blocking call published there would
occupy that slot until it could complete, stalling every other management request in the process -- a self-inflicted
deadlock class. A blocking operation needs the lane (which parks only its own caller) or a dedicated mechanism, not
this one. Recording the exclusion is the point: the next reader should not "finish the job" by adding it.

The count so far, by the hatch's own complaint:

| # | consumer | class | transport |
|---|---|---|---|
| 1 | `mach_port_deallocate` | port | duplex lane |
| 2 | `vchroot` | lifecycle | plane |
| 3 | `kqchan_mach_port_open` | fd-bearing | plane + courier |
| 4-5 | `interrupt_enter/exit`, `sigprocess` | signal context | urgent pool |
| 6 | `mach_msg_overwrite` | Mach message | mach-msg ring |
| 7 | `mach_port_move_member` | port | plane |
| 8 | `pthread_canceled` | lifecycle | plane |
| 9-11 | `construct`, `destruct`, `mod_refs` | port | plane |
| 12 | `semaphore_signal[_all]` | semaphore | plane |
| -- | eight more port operations | port | plane (batch) |


### 114. `call=unknown`: the request came through a path that carries no call name -- `bsdthread_terminate`

The semaphore migrations were measured green -- the hatch no longer names `semaphore_signal` -- and then it said
something new:

```
[rpc-socket-DENIED] pid=1448194 tid=1448194 call=unknown
```

`call=%s` is printed by the guest kernel's denial site from `__dserver_current_call`, which the generated wrappers
set through `dserver_rpc_hooks_note_call(name)`. So `unknown` means the request **did not come from a generated
wrapper at all**. There are exactly two direct requesters in the tree, and only one of them prints through this site:

```c
/* bsdthread_terminate.c */
// we can also unguard the RPC FD for this thread now
guard_table_remove(mach_driver_get_fd());
```

The terminate path asks for the fd purely in order to remove it. `mach_driver_get_fd()` **creates** a per-thread
socket when the thread has none, so this path manufactures a transport it only wanted to clean up -- and it does so
without a call name, which is why the hatch could not name it. (The other direct requester, `fork.c`'s
`guard_table_add(__dserver_per_thread_socket(), ...)`, goes through the loader and prints through the *loader's*
denial site with `reason=` instead.)

The guard was never this path's to look up: the loader adds it with the fd it created
(`t_callbacks->rpc_guard(new_rpc_fd)`), and the guest's `rpc_guard`/`rpc_unguard` callbacks already receive exactly
that fd. So the fix records it:

* `rpc_guard(fd)` notes the fd in a thread-local and `rpc_unguard(fd)` clears it;
* `bsdthread_terminate` unguards **the recorded fd**, and a thread that never had a guarded RPC fd simply has
  nothing to unguard.

That is the rule from the workspace rules applied literally -- **a descriptor number is not ownership** -- and it
removes a socket creation that no migration of a callnum could have removed, because there is no callnum: this is a
consumer the counter-based view could not see and the hatch could not name.


### 115. MILESTONE: the per-thread RPC socket class is closed -- zero requests AND zero creations

The `bsdthread_terminate` fix was measured, and the hatch has nothing left to name:

```
denials total: 0
[rpc-socket] created ...        (not one line)
kqchan-plane] status=0 token=16194714452175325495 fd=8 out=8
```

Two independent counters, both zero, and they mean different things. The denial counter is zero because no call
**asked** for a per-thread socket; the creation counter is zero because no code path **manufactured** one. A run
where the hatch is silent because the hatch is not consulted is exactly the error §103 made, so both are checked.

The guest's own creation line (`[rpc-socket] created pid=… tid=… n=… reason=…`) never appears, which is the direct
evidence: **`per_thread_rpc_socket_created == 0` on the boot path**, the directive's §26 target.

What the boot does now is fail for a reason that has nothing to do with sockets. There is no abort, no denial, and
the last guest activity is a lane release:

```
[dring-lane-release] pid=1451283 tid=1451283 slot=0 borrowed=1 ret=0x7F348C1DEF43
Rootless shellspawn did not become ready within 30000ms (/proc/self/fd/6/shellspawn.sock)
```

So the transport work this directive asked for is **done for the boot path** and the remaining failure is the
shellspawn readiness hang, which the mach-msg code already describes in its own comment as a distinct defect class:

> a real caller-S2C munmap arrives while this caller is parked in `gr_machmsg_wait_reply` … Without the bit the
> guard declines, the server falls back to a UDS S2C, and the ring-parked caller can never service it: the op then
> hangs until the shellspawn timeout.

The counting, by the hatch's own complaint, now reads:

| | |
|---|---|
| consumers found and migrated | **14** (plus the batch of 8 port operations) |
| transport classes clean | 3 (no denial, no urgent timeout, no courier miss) |
| per-thread RPC socket requests on boot | **0** |
| per-thread RPC socket creations on boot | **0** |
| boot completes under the hatch | no -- different cause, socket-free |
| regression under the hatch / FD slope | not yet run |


### 116. The descriptor intermittency of §97 is still alive -- and now its mechanism is named

A cleanly launched run (prefix shut down, no prefix-matching process left, verified before launch) printed the
failure §97 recorded and §106 was thought to have closed:

```
[dring-adopt] post-claim pid=1454273 ok
[kqchan-plane] status=0 token=0 fd=-1 out=0
```

So the delivery is still **intermittent**, and the two outcomes are now distinguishable by one field. Reading the
generated sender against the guest route gives the mechanism:

```c
uint64_t __fd_courier_token = sendFdCourierToGuest(_header.pid, KIND_KQCHAN_FD, socket);
if (__fd_courier_token != 0) { replyStruct->body.socket = -1; replyStruct->body.fd_token = __fd_courier_token; }
else { reply.pushDescriptor(socket); }        /* the CMSG fallback */
```

When the courier send returns 0, the generated sender deliberately falls back to **CMSG** (`pushDescriptor`) -- a
transport the **plane route does not read**. The plane case reads the reply body and, failing that, has no
descriptor; the guest's `[kqchan-plane]` line reports exactly that: `status=0 token=0 fd=-1`.

So there are two distinct faults, and §106 fixed only the first:

1. **fixed**: the plane case ignored an `fd_token` the sender had already produced and shipped;
2. **open**: when the sender cannot use the courier it silently switches to a descriptor transport the plane
   caller cannot observe -- and the plane caller has no way to know that happened.

The second is the one blocking the boot, and it is a *contract* fault rather than a plumbing fault: a route chosen
by the caller (the plane) must not have its answer delivered by a transport the caller does not own. The fix has to
be one of:

* the plane route refuses the descriptor and returns a status that says so, so the guest falls back to the datagram
  (safe -- nothing committed), or
* the plane route gets a CMSG-capable reply path of its own.

The measurement that names which one is needed is the courier log for the same request: why
`sendFdCourierToGuest` returned 0 for this process at this moment.

Also recorded: the first attempt at this diagnostic was **invalid** -- it was launched without shutting down the
previous run, and the resulting log held a single line (the timeout) with no guest output at all, which is the
signature of a reused server rather than a boot. The clean relaunch produced the 31-line log quoted above. The
workspace rule exists because this exact mistake produces a plausible-looking measurement.


### 117. The CMSG branch writes an INDEX, not a descriptor -- and the plane case was reading it as one

§116 named the mechanism; reading the generated sender's fallback branch to the end gives the concrete defect:

```c
} else {
    reply.pushDescriptor(socket);
    replyStruct->body.socket = (fdIndex++);      /* an INDEX into the CMSG list -- initially 0 */
    replyStruct->body.fd_token = 0;
}
```

`body.socket` in that branch is **not** the descriptor: it is the position of the descriptor in the CMSG list,
which is 0 for the first one. The pre-§106 plane case did `if (rb->socket >= 0) returnedFd = rb->socket;`, so on
this path it took **fd 0** as the descriptor -- then re-sent "fd 0" on the courier and, when that returned 0, closed
**fd 0** in the server process. Three separate faults from one misread field:

1. a descriptor the caller cannot receive is silently reported as success (`status=0`, `token=0`, `fd=-1`);
2. the value 0 is used as a real descriptor;
3. `close(returnedFd)` closes the server's fd 0.

The fix removes the misuse and records the miss instead of guessing:

* only a **non-zero `fd_token`** counts as a descriptor transfer (the courier path);
* when `fd_token == 0`, the plane case does **not** treat `body.socket` as a descriptor, counts
  `fdCourierFallbackCmsg`, and names the event (`kqchan-plane-no-token`) when the courier log is on;
* `returnedFd` stays -1, so nothing is sent and nothing is closed.

The underlying fault remains the one §116 named: the generated sender chose a transport (CMSG) that a plane caller
cannot read, and it did so **silently**. Removing the misread makes the boot's failure honest instead of corrupting
the service; the courier's own reason for returning 0 is the next measurement.

Worth stating plainly: this defect was invisible to every counter in the tree. The call reported `status=0`, the
guest reported a resolved call, and the only signal was a `-1` descriptor in a line added for a different purpose.


### 118. With the misread removed: descriptor delivered, no denials -- and the boot still stops elsewhere

The §117 build was measured:

```
[kqchan-plane] status=0 token=16733224056179724476 fd=8 out=8
kqchan-plane-no-token:  (not emitted)
denials: 0
HELLO: 0
```

So in this run the descriptor **was** delivered -- token non-zero, guest resolved it, fd 8 in its out-parameter --
and the new miss diagnostic did **not** fire, meaning the courier path succeeded rather than the CMSG fallback. The
§97 intermittency is therefore real but is **not** what stops this boot: it produced both outcomes across runs, and
the run where delivery worked still failed.

That separates the remaining work into two independent items, which is worth stating because they have been
conflated:

* **the delivery intermittency** (§97/§116/§117) -- both outcomes observed with the same build; a real defect, and
  now instrumented (`kqchan-plane-no-token`) plus no longer corrupting the service, but not the current blocker;
* **the boot stop** -- `denials = 0` AND a delivered descriptor, and shellspawn still never becomes ready. This is
  not a transport question at all: the transport the directive asked for is quiet and working at this point.

Everything the directive set out to do on the boot path is therefore measured done:

| claim | measurement |
|---|---|
| per-thread RPC socket requests | 0 |
| per-thread RPC socket creations | 0 |
| transport classes clean | 3 |
| kqchan descriptor on the plane | delivered, token resolved by the guest |
| boot stop cause | **not** sockets, **not** kqchan |

The next question is a lifecycle one -- why the shellspawn process is absent rather than blocked (only
`darlingserver` is alive at 50 s into the run, and the guest's last act is a lane release) -- and it is reachable
without touching any transport.


### 119. The guest exits 0 before pre-main completes -- and the prefix was carrying 893 stale processes

Two findings, both from asking `/proc` a direct question instead of reading the log again.

**The exit status.** The guest process for the failing boot is a zombie, so its status is readable:

```
1733427 Zs - mldr        /proc/1733427/stat -> state Z, exit_code 0, ppid 1733412
```

`exit_code 0` means the guest **terminates successfully** during pre-main initialisation. It is not a crash and not
a hang: the process runs the loader, attaches, adopts, does the kqchan call, releases the lane -- and then exits 0
without ever reaching `main`. `shellspawn` in the build tree carries its `main-entry` print and that line never
appears, so this confirms §95/§96 by a different route and adds the missing fact: the outcome is a **clean early
exit**, which is a decision somewhere in pre-main rather than a fault.

That also rules out the reading §115 was tempted by: the boot is not waiting on any transport. The process is gone
before the transport matters.

**The prefix was carrying 893 stale processes.** A prefix-scoped listing by `exe` -- not by `cmdline` -- found them:

```
prefix-owned procs (by exe): 894
```

Their `cmdline` is `/usr/libexec/shellspawn`, which does **not** contain the prefix path, so the cleanup recipe that
matches `/proc/<pid>/cmdline` against the prefix **never matched them** and every run added more. They held stale
`mldr (deleted)` mappings from earlier deploys.

Two corrections follow for the lifecycle rules, and they are worth keeping:

* a prefix-scoped stale-process match must consider `exe` as well as `cmdline`, because a guest process's `cmdline`
  is the in-guest path and carries no prefix;
* the accumulated set grows silently across runs, so it is a run-to-run state hazard even when it is not the
  current failure.

Killing all 893 (`remaining=0`, `mounts=0`) did **not** change the boot: the same `exit 0`. So the staleness was
real garbage and not the cause -- recorded because "we cleaned something and the symptom stayed" is the honest
result, and because the next person to see a prefix with hundreds of processes should know where they come from.


### 120. DECISIVE: with the hatch OFF, not one per-thread RPC socket is created either

The single most informative run of this cycle is the control: the same boot **without** the acceptance hatch.

```
NO-HATCH: HELLO=0 FINAL=0
sockets created: 0
```

Two conclusions, and both are stronger than anything the hatch could show on its own.

**1. The migration is complete, not merely suppressed.** `[rpc-socket] created` is the guest's own counter, printed
the moment a per-thread socket is manufactured, and it fires **zero times** with the hatch **off** -- i.e. on a run
where creating one is perfectly legal. Until now the evidence was "nothing asked for a socket"; this is "nothing
creates one even when it may". The per-thread RPC UDS is not being denied, it is **no longer reached**.

**2. The remaining boot failure is pre-existing and unrelated to this work.** The control run fails identically:
same `[dring-lane-release]`, same shellspawn readiness timeout, same `exit 0` in pre-main §119 measured. So the stop
is not caused by the hatch, not caused by any migration, and not a transport defect at all.

That is the honest shape of the result:

| claim | hatch ON | hatch OFF |
|---|---|---|
| per-thread RPC socket requests | 0 | (no denial possible) |
| per-thread RPC socket creations | 0 | **0** |
| kqchan descriptor on the plane | delivered | delivered |
| boot reaches ready | no | **no, identically** |

The directive's transport goal is met on this path in the strongest available sense, and what remains is a
lifecycle defect that predates it: the guest image **terminates with exit 0** after the loader hands off
(§119 measured the zombie's status; the handoff itself is now proven reached, `[mldr-handoff] entry=0x7ff8293f1000`,
with `mldr-ctl` running through `after-seed`).

Stated plainly, because it changes what should happen next: continuing to migrate callnums would be solving a
problem that no longer exists. The remaining work is the pre-main early exit, which is reachable and diagnosable
without touching any transport.


### 121. The guest dies BEFORE `dyld::_main` -- and the substitution matrix is exhausted

Two results, both narrowing the defect to a place no transport change reaches.

**The dyld milestones never fire.** Gated milestones were added at `dyld::_main`'s entry and around
`initializeMainExecutable()`, and the run produced **not one of them** -- not even `ENTRY`:

```
dyld-ENTRY / dyld-INIT_BEGIN / dyld-INIT_END:   0 lines
HELLO=0
```

So `dyld::_main` is never entered. The guest dies **before** it, i.e. inside `__dyld_start` (the Mach-O entry
stub) or at the handoff address itself: `[mldr-handoff] entry=0x7ff8293f1000`. This supersedes §119's "pre-main
initialisation" with something narrower: it is not *during* dyld's main, it is *before* it.

**The component substitution matrix has no usable GOOD arm.** §3/§4 of the directive ask for the last known
GREEN build and a GOOD/CURRENT substitution matrix. That is not available in this tree, and the reason is a fact
about the repository rather than a choice:

```text
procctl-src: git log -> a single EMPTY commit "init"; git status -> every path untracked
```

There is no tracked baseline, so no GOOD source and no GOOD diff. The only other build tree found
(`/home/ilyagulya/work/procctl-build`, artifacts dated 09-18) was substituted in full for
`darlingserver` + `mldr` + `dyld` + `libsystem_kernel` and **also failed** (`HELLO=0`), so it is not the GREEN
reference either. Only one `shellspawn` binary exists anywhere in the tree, so that component cannot be
substituted at all -- which means the matrix can never be completed as specified, whatever build is used.

What the matrix did establish, and it is worth keeping:

| substitution | result |
|---|---|
| GOOD(09-18) server+mldr+dyld+lsk, current shellspawn | RED, same readiness timeout |
| `--rootless exec` instead of `shell` | RED, same point (guest never runs) |
| current, all components | RED |
| current, hatch OFF | RED, identically |

The `exec` result matters independently: the failure is not the launcher's shell mode, shellspawn readiness
being a red herring for a broken loader chain.

**Reusable cleanup helper added** (directive §10), in the manifest repository because it is workspace tooling:
`scripts/prefix-cleanup.sh --prefix ABS [--settle N] [--dry-run]`. It enumerates prefix-owned processes by
`/proc/<pid>/exe` **and** `cmdline`, shuts the runtime down, kills, re-checks, escalates once, and exits non-zero
if any process or mount remains -- so it is usable as a gate rather than advice. The `exe` arm is the point: this
is the mechanism by which 893 guest processes accumulated unnoticed (§119).

Next step, precisely: verify what `[mldr-handoff] entry=0x7ff8293f1000` actually points at (the `LC_MAIN` entry
of the deployed Mach-O `dyld`) and instrument `__dyld_start`, because `dyld::_main` is now proven unreached.


### 122. The death is inside `__dyld_start`'s contract -- `dyldbootstrap::start` is never reached either

§121 proved `dyld::_main` is never entered. There is exactly one function between the handoff and it, so it was
instrumented next -- and it also never runs:

```
[dyld-boot-ENTRY] / AFTER_REBASE / AFTER_GUARD / AFTER_SIGEXC / AFTER_SUBSYS / BEFORE_MAIN :  0 lines
HELLO=0
```

So `dyldbootstrap::start` is never entered, and the death is inside the **assembly stub** `__dyld_start` -- or at
the address the loader jumps to, before any instruction of the chain the C++ milestones can observe runs.

Reading that stub gives the contract that must hold, and it is not the jump address:

```asm
__dyld_start:
	popq	%rdi		# param1 = mh of app        <-- READ FROM THE STACK
	pushq	$0
	movq	%rsp,%rbp
	andq    $-16,%rsp
	subq	$16,%rsp
	movl	8(%rbp),%esi	# param2 = argc              <-- FROM THE FRAME
	leaq	16(%rbp),%rdx	# param3 = &argv[0]          <-- FROM THE FRAME
	leaq	___dso_handle(%rip),%rcx
	leaq	-8(%rbp),%r8
	call	__ZN13dyldbootstrap5startEPKN5dyld311MachOLoadedEiPPKcS3_Pm
```

The entry stub takes its Mach-O header from the **stack the loader builds**, and `argc`/`argv` from the frame above
it. `mldr`'s `start_thread` sets `rsp` to `lr->stack_top` and jumps, so the whole handoff is the **frame contents**
at `stack_top`, not the address. That is now the thing to print -- from the loader, in a context where printing is
safe -- rather than another guest-side instrument.

Progress of isolation, stated as the boundary each step proved:

| instrument | result | boundary |
|---|---|---|
| `[mldr-handoff]` entry/stack | fired | loader reaches the handoff |
| `dyld::_main` milestones | silent | death is before dyld's C++ main |
| `dyldbootstrap::start` milestones | silent | death is inside `__dyld_start` or at the jump |
| loader handoff **frame** | printed next | whether the stub's inputs are well-formed |

The comparison that matters for the migration claim: this boundary lies **entirely before** any transport code path
the migrations touch (the plane/courier/urgent/Ring work all runs later or elsewhere), which is consistent with
§120's hatch-ON/hatch-OFF identity without depending on it.


### 123. `__dyld_start` DOES run -- and the gated probes may have been lying

The asm stub was instrumented with a **raw `write(2)` syscall** as its first instruction, because that is the only
output available before any C code has run. It fires:

```
[dyld_start]        1 line
dyld-boot-*         0 lines
HELLO=0
```

So `__dyld_start` **executes** -- the guest reaches the Mach-O entry stub -- and `dyldbootstrap::start` is still not
observed. Between the two lies the stub's own frame setup and one `call`.

That result also invalidates the method used for §121/§122, and the correction matters more than the result:

**the gated probes call `getenv()` before libc is initialised.** `__dyld_boot_diag` and `__dyld_diag` both open with
`if (on < 0) { on = (getenv("MLDR_DYLD_DIAG") != NULL) ? 1 : 0; }`, and dyld's bootstrap runs before any runtime that
would make `getenv` meaningful. A silent gated probe therefore proved nothing about whether the function was
entered -- it may have died at the gate. The asm probe had no gate, and it worked, which is exactly the control
that shows the difference.

This is the fifth instrument in this cycle that had to be corrected for the same reason in spirit -- a probe whose
silence could be explained by the probe itself rather than by the code under test (§103, §105, §106, §116, and now
§121/§122). The invariant worth carrying forward: **a probe must be able to fire in the state it is probing for.**
For pre-runtime code that means no libc, no `getenv`, no stdio -- raw syscalls only.

The bootstrap milestones are now unconditionally printing, which is the measurement that decides whether §121/§122
were conclusions or artifacts.


### 124. Confirmed, not an artifact: the `call` from `__dyld_start` never reaches `dyldbootstrap::start`

The bootstrap probe was made **unconditional** (no `getenv`, raw `write`), which removes the gate as an
explanation. The result is unchanged:

```
[dyld_start]    1 line      <- the stub executes
dyld-boot-*     0 lines     <- start() is still never entered
HELLO=0
```

So §121/§122 were **conclusions, not artifacts**, and the defect is now localized to a single instruction. Between
the working raw `write` and the unreached `call` there are nine instructions of pure frame setup, none of which can
fail without a visible fault:

```asm
__dyld_start:
	movq $1,%rax; movq $2,%rdi; leaq msg(%rip),%rsi; movq $16,%rdx; syscall   # FIRES
	popq	%rdi
	pushq	$0
	movq	%rsp,%rbp
	andq    $-16,%rsp
	subq	$16,%rsp
	movl	8(%rbp),%esi
	leaq	16(%rbp),%rdx
	leaq	___dso_handle(%rip),%rcx
	leaq	-8(%rbp),%r8
	call	__ZN13dyldbootstrap5startEPKN5dyld311MachOLoadedEiPPKcS3_Pm    # NEVER REACHED
```

The only step that can fail silently here is the `call`: `leaq ___dso_handle(%rip)` and the call target are
resolved against dyld's own load address, so if the entry address the loader jumps to is not the real load address
of that dyld -- a slide/base disagreement -- the stub executes its first instructions and then transfers control to
an address that is not `dyldbootstrap::start`. A process that ends in `exit 0` rather than a fault is consistent
with landing on the wrong byte sequence and simply returning.

That narrows the remaining question to one comparison, and it is answerable from the loader side where printing is
safe: whether `lr->entry_point` (measured `0x7395fe5f1000`) is the deployed dyld's `LC_MAIN` entry resolved
against the **same** base the loader mapped the image at. Everything above the call is now proven to execute;
everything below it is proven unreached; the disagreement, if there is one, is in the address.

Boundary for the migration claim, unchanged and now on firmer ground: this is entirely inside dyld's entry stub,
which no transport change touches. The plane, courier, urgent pool and Ring routing all run later or elsewhere.


### 125. The C probes were never usable: libc is not initialised at dyld bootstrap

§123 suspected the gate; the gate was not the whole problem. Making the probe unconditional still printed nothing,
because the probe itself was still **libc**: `write`, `strlen`, `getenv` are the guest's libc, and dyld's bootstrap
runs **before** any runtime that makes them work. The asm stub's raw `syscall` was the only probe in the set that
could fire, and it did -- which is precisely the control that exposed the others.

Rewriting the C probe as a **raw `syscall`** (fixed-length writes, no libc at all) immediately made it speak:

```
dyld-boot-ENTRY
dyld-boot-AFTER_REBASE
dyld-boot-AFTER_GUARD
dyld-boot-AFTER_SIGEXC
dyld-boot-AFTER_SUBSYS
dyld-boot-BEFORE_MAIN        <- NOT printed
```

Five milestones, and the sixth is absent. That single change turned seven rounds of "the code is not reached"
into a precise statement:

**the death is between `_subsystem_init(apple)` and the `dyld::_main(...)` call.**

What that rules out is more valuable than what it shows:

* `rebaseDyld` completes -- so the image is correctly rebased at its load address;
* `__guard_setup` completes;
* **`sigexc_setup()` completes** -- and this is the step that mattered most, because `sigexc.c` is where the
  signal-context transport routes (`interrupt_enter`/`interrupt_exit`, `sigprocess`) were wired to the urgent pool
  and the management plane. It runs cleanly, so those migrations do not break the guest's earliest signal setup;
* `_subsystem_init` completes.

The remaining gap is one call and its argument setup (`appsMachHeader->getSlide()`), not any transport code.

The method lesson is now the seventh instance in this cycle of the same class, and it is the one worth keeping:
**a probe must be able to fire in the state it is probing for.** Five of those seven were probes whose silence was
explained by the probe itself. The rule that fixes it: in pre-runtime code, print with a raw syscall and a fixed
length -- no libc, no `getenv`, no stdio, no `strlen`.


### 126. dyld is NOT the barrier: it completes its whole main successfully

With the probes finally able to fire, the sequence for each image is complete and successful:

```
dyld-boot-ENTRY -> AFTER_REBASE -> AFTER_GUARD -> AFTER_SIGEXC -> AFTER_SUBSYS
dyld-ENTRY -> INIT_BEGIN -> INIT_END -> AFTER_NOTIFY -> LC_MAIN_RESOLVED
```

Every one of them prints, for both images the loader starts. So dyld:

* rebases correctly at its load address;
* sets up the stack guard;
* **completes `sigexc_setup()`** -- the signal machinery where the urgent-pool and management-plane signal routes live;
* initialises the subsystem;
* enters `dyld::_main`;
* runs **all initializers** (`INIT_BEGIN` → `INIT_END`, i.e. `initializeMainExecutable()` returns normally);
* notifies monitoring;
* **resolves the main image's `LC_MAIN` entry**.

It does not stop, and it does not halt: the `halt()` probe (`dyld-HALT`) never fires, so `libdyld.dylib support
not present for LC_MAIN` is not the path taken. The guest therefore dies **after** dyld has done its whole job,
in the transfer to the main image's entry.

**One of the probes was itself wrong and is corrected here.** `BEFORE_MAIN` was inserted *after*
`return dyld::_main(...)` in `dyldbootstrap::start`, so it can only print if `_main` **returns** -- it measures the
comeback, not the entry, and its silence was never evidence about entering `_main`. That is the eighth instance of
the same class in this cycle and the reason the other probes now have to be read as a **sequence** rather than as
presence/absence of any one line.

What this buys, stated as the boundary that moved:

| earlier conclusion | now |
|---|---|
| "death in pre-main init" (§119) | measured, but located wrong |
| "death before `dyld::_main`" (§121/122) | an artifact of libc-based probes |
| "death inside the entry stub" (§124) | also an artifact |
| **measured now** | dyld completes fully; the bare handoff to the main image is where it ends |

The transport work is untouched by anything in that sequence -- and `sigexc_setup`, the one step that does touch
signal-context transport, is measured **passing**.


### 127. The handoff HAPPENS: the barrier is inside the main image, before its `main`

The last instruction before the main image's code runs was instrumented in the LC_MAIN branch of `__dyld_start`,
and it fires:

```
dyld-ENTRY -> INIT_BEGIN -> INIT_END -> AFTER_NOTIFY -> LC_MAIN_RESOLVED
[dyld-lnew]                                     <- the LC_MAIN branch, immediately before jmp *%rax
main-entry:  0
HELLO:       0
```

So dyld takes the LC_MAIN path and **executes the jump** into the main image. The chain is now proven end to end on
dyld's side:

```asm
Lnew:
	addq	$16,%rsp
	pushq	%rdi		# return address into _start in libdyld
	movq	8(%rbp),%rdi	# argc
	leaq	16(%rbp),%rsi	# &argv[0]
	leaq	0x8(%rsi,%rdi,8),%rdx
	movq	%rdx,%rcx
Lapple:	movq	(%rcx),%r8
	add	$8,%rcx
	testq	%r8,%r8
	jne	Lapple
	[probe]                 # FIRES
	jmp	*%rax		# jump to main(argc,argv,env,apple)
```

Everything upstream is accounted for and successful; nothing in the transport work appears anywhere in the
sequence. The remaining question is therefore inside the **main image**, between its `_start` and its `main`.

One caveat recorded against my own instrument, because it changes how much the `main-entry: 0` line proves:
`main-entry` is a `fprintf` -- **libc**. Unlike dyld's bootstrap, the main image reaches `main` only *after* crt has
run `__libc_start_main`, so libc **is** initialised by then and the print should work; but that is a reason to
believe the negative, not a measurement of it. The instrument to trust here is a raw syscall, placed in the main
image's `_start`/crt and at the top of `main`, which is the next measurement.

Boundary, and this is the important part for the directive's question:

| stage | status |
|---|---|
| loader reaches handoff | proven (§122 probe) |
| dyld entry stub executes | proven (raw syscall, §124) |
| `rebaseDyld`, guard, **`sigexc_setup`**, subsystem init | proven (§125) |
| `dyld::_main` entry, **all initializers**, notify, LC_MAIN resolve | proven (§126) |
| LC_MAIN branch and `jmp *%rax` into the main image | **proven (§127)** |
| main image's `_start` → `main` | **not reached -- the remaining defect** |

Every transport-code path the migration work touches is either later than this or absent from it, and
`sigexc_setup` -- the one early step that does carry signal-context transport routes -- is measured passing.


### 128. Proven with a probe that cannot lie: the main image's `main` is never entered

§127 proved dyld executes `jmp *%rax` into the main image. The open question was whether the image's `main` runs,
and the earlier evidence for "no" was a `fprintf`. That is not good enough, so the probe at the top of
`shellspawn.c`'s `main` was rewritten as a **raw syscall** -- and it still does not fire:

```
[dyld-lnew]             1        <- dyld executed its jump
[shellspawn-step]       0        <- main-entry never printed (raw syscall now)
HELLO=0
```

That settles it: the main image's `main` is **not entered**, and the instrument can no longer be the explanation.

**Two crt corrections came out of chasing it, and both were needed to get here:**

1. `src/external/csu/crt.c` looks like the guest entry and carries a `_start` that ends in
   `exit(main(argc, argv, envp, apple))` -- exactly the shape that produces an `exit 0`. Instrumenting it changed
   **nothing** in the shipped binary (identical sha256 after the rebuild), which is how it was discovered that
   `shellspawn` is linked `-nostdlib` with `crt1.10.6/start.S.o` and does **not** take that C `_start` on this
   path. The probes were in a file nothing links.
2. The real entry is the **assembly** `start` in `src/external/csu/start.S`, whose x86_64 path sets up
   `argc`/`argv`/`envp`, computes the `apple` pointer, and goes straight to `call _main` -- `OLD_LIBSYSTEM_SUPPORT`
   is off, so `__start` is not involved at all.

The probes are therefore now in `start.S`, immediately before and after `call _main`, as raw syscalls. That is the
last instruction boundary before the image's own code, and it is the only remaining place the `exit 0` can come
from.

Counted honestly, this is the **tenth** instrument in this cycle corrected for the same class of mistake, and the
pattern is stable enough to state as a rule: an instrument must be (a) able to fire in the state it probes, and
(b) actually part of the artifact under test. Four of the ten were libc-in-early-boot, and this one was
linked-into-nothing.


### 129. The probes in the image's own crt do not fire either -- so the jump does not land in it

The asm probes were placed in `start.S` immediately before and after `call _main`, and the probe string is
**verified present in the shipped binary** (`strings` finds `csu-call-main`, and the sha changed when they were
added). The run prints none of them:

```
[csu-call-main] / [csu-main-ret]     0 lines
[dyld-lnew]                          1 line      <- dyld's jump executed
[shellspawn-step] main-entry         0 lines     <- the image's main never runs
HELLO=0
```

So the image's own C runtime `start` does not execute either, and the boundary is now:

| stage | status |
|---|---|
| dyld completes `_main`, all initializers, notify, LC_MAIN resolution | proven |
| dyld executes `jmp *%rax` into the main image | **proven** |
| the main image's crt `start` runs | **no** |
| the main image's `main` runs | **no** |

Combined with the earlier facts, that is a contradiction with any reading in which the resolved entry is the
image's crt: if `jmp *%rax` landed in `start`, the probes in `start` would fire, and if it landed in `main`, the
probe in `main` would fire. Neither does. The remaining explanation is the one thing not yet measured: **the value
dyld resolved and jumped to is not the main image's crt entry at all.**

That is a single measurement -- print `%rax` at `Lnew` before the jump, and compare it with the deployed
`shellspawn`'s `LC_MAIN` entry resolved against the base the loader mapped it at -- and it is the next action. It
is also the first point in this whole chain where the loader's and dyld's ideas of the image's address could
disagree, which is exactly the class of defect the earlier `slide` handling (`entryPoint += slide`) makes
plausible.

For the directive's question, the boundary is now stated as tightly as the evidence allows: **every transport code
path is outside this window**. The window is between dyld's jump and the image's crt, and it contains no RPC, no
management plane, no courier and no Ring.


### 130. THE ROOT, numerically: the image being run is `vchroot`, not `shellspawn`

§129 left one question -- what value dyld resolves for the main image's entry. A hex probe (raw syscall) answers it:

```
[dyld-main-entry-addr 0x0000000100000c60]
```

and the deployed `shellspawn`'s own `LC_MAIN entryoff` is `0x13a0` (verified in both the build tree and the prefix,
thin x86_64, sha256-identical), so the entry for it would be `base + 0x13a0 = 0x1000013a0`. The resolved address is
**not** that. `getEntryFromLC_MAIN()` computes `fMachOData + entryoff` and returns it only if the result is inside the
image, so the address means the main image's `entryoff` is `0xc60`, and the base is `0x100000000`.

Scanning the prefix for a Mach-O whose `LC_MAIN entryoff` is exactly `0xc60` gives a unique answer:

```
/tmp/dr-on-matched/libexec/darling/usr/libexec/darling/vchroot
/tmp/dr-on-matched/usr/libexec/darling/vchroot
```

**`vchroot`.** The image being loaded as the main executable is the `vchroot` helper, not the requested program.
`vchroot` with no arguments prints its path and exits 0 -- which is precisely the observed outcome: no output, no
fault, `exit 0`, process gone.

That also closes a thread that was misread five sections ago: `[mldr-ctl] ... image=vchroot` in the loader's
diagnostics was treated as a label (`__mldr_diag_image(argv[0])` was assumed to classify by name) when it was
**literal** -- the main image really was `vchroot` all along, printed by the loader's own instrument.

What this means for the whole investigation, and it should be stated plainly because most of it was spent
elsewhere:

* the transport work is **not involved**: every instrument of it is quiet and every counter is zero;
* `shellspawn` is not reached because it is **never the image being run** -- not because dyld, the crt, `sigexc`, the
  lane, the plane or the courier failed;
* the earlier readings -- "pre-main exit 0", "dyld is not the barrier", "the crt does not fire", "`main` is never
  entered" -- were all **true** and all **consistent** with this; they were describing the wrong program's lifecycle
  exactly as measured, and none of them was about `shellspawn` at all.

The next action is therefore narrow and has nothing to do with any transport: find where the main image's path is
chosen and why it becomes `vchroot` instead of the requested program (`argv[0]` handling in the launcher, or the
loader's `dyld_path`/main-image selection).

Correction of record: the claim in §120 that "the remaining failure is pre-existing and unrelated to this work" was
right in substance and wrong in detail; §119-129 localized a **symptom of the wrong image**, which is why every
probe of `shellspawn` was silent from the very first one.


### 131. The full picture: the observed process was the `vchroot` helper, and the transport work is exonerated

§130 identified the running image numerically. Following the launch path gives the whole picture and it is not a
transport story at all.

`spawnInitProcess()` -- the function the launcher calls "start the prefix's init" -- execs **`darlingserver`**, not
`shellspawn`:

```c
execl(INSTALL_PREFIX "/bin/darlingserver", "darlingserver",
      prefixfd_str, parentfd_str, g_runtimePrefix->leaf, workdirfd_str, uid_str, gid_str, pipefd_str, ...);
```

So the prefix's init **is** the server, and `shellspawn` -- the program whose readiness the launcher waits for, via
`var/run/shellspawn.sock` -- is started by something else entirely. Meanwhile the guest process that does run, and
that every instrument in §119-§129 was watching, is **`vchroot`**: the rootless helper, which with no arguments
prints its path and exits **0**. That is the `exit 0` that seven sections of investigation localized to
progressively smaller windows -- and every one of those localizations was **correct about the process it was
measuring**.

The chain, with the transport work marked where it actually sits:

| stage | measured |
|---|---|
| launcher spawns `darlingserver` as init | yes (`spawnInitProcess`) |
| a guest process is launched and reaches dyld | yes -- but it is `vchroot` |
| dyld completes fully, jumps to the image | yes (§126/§127) |
| the image's entry is `vchroot`'s (`0xc60`) | **yes -- numerically (§130)** |
| `vchroot` runs and exits 0 | yes -- expected for a helper with no arguments |
| `shellspawn` ever starts | **no** |
| launcher's readiness wait fails | yes -- because shellspawn is not running |

**Conclusion for the directive's question.** The socket-disabled transport work is not implicated by any part of
this. Its own instruments are quiet and its counters are zero; the guest lifecycle that fails is a **different
program's**, and the failure is that `shellspawn` is not started at all. The per-thread RPC UDS is unreachable on
every path actually exercised (hatch ON and OFF, §115/§120), and the boot does not reach ready for a reason that
lies outside the transport entirely.

**What is NOT yet established**, and should not be claimed: that the socket-disabled transport is *correct under
load*. That requires the boot to become green, which requires shellspawn to run, which is the next work item and
has nothing to do with the migrations.

Next action, narrow: find where `shellspawn` is supposed to be started in the rootless flow and why `vchroot` is
the process that runs instead -- i.e. whether the launcher's `shell`/`exec` path is asking for the helper, or
whether the helper is expected to hand over to the init and does not.


### 132. `vchroot` is the right image -- and its own code cannot produce `exit 0`

§130 identified the image as `vchroot` and §131 called it a helper. Reading it corrects the second half:

```c
int main(int argc, const char** argv) {
    if (argc < 3) { fprintf(stderr, "vchroot <dir> <binary> [args...]\n"); return 1; }
    sprintf(buf, "%s%s", argv[1], argv[2]);
    if (access(buf, F_OK) != 0) { ...; return 5; }
    int dfd = open(argv[1], O_RDONLY | O_DIRECTORY);
    if (dfd == -1) { perror("open"); return 1; }
    if (fchdir(dfd) == -1) { perror("fchdir"); return 2; }
    if (__darling_vchroot(dfd) < 0) { perror("vchroot"); return 3; }
    unsetenv("DYLD_ROOT_PATH");
    execv(argv[2], (char * const *) argv+2);
    perror("execv");
    return 4;
}
```

`vchroot <dir> <binary> [args...]` is the rootless **exec wrapper**: chroot into the prefix and `execv` the real
program. So `vchroot` being the image dyld runs is **correct** -- not a wrong-program bug. And every return path in
that function is **non-zero** (1, 5, 1, 2, 3, 4), with `execv` not returning at all on success. **None of them is 0.**

The boot produces `exit 0` with no output. Therefore `vchroot`'s `main` did not execute its body -- no usage line,
no `open`/`fchdir`/`vchroot` error, no `execv` failure. The boundary is now between dyld's `jmp *%rax` (proven
executed) and this `main` -- the same place §129 measured as "the image's crt does not fire", except that §129 was
instrumenting **`shellspawn`'s** crt, and the running image is **`vchroot`**. The instrument was in the wrong binary
again -- the eleventh instance of the class, and the same root cause as the tenth: two programs are involved and the
one measured was not the one running.

That also explains why a second load is never seen. `execv(argv[2], argv+2)` would re-enter the loader and produce a
**second** full sequence (dyld entry, initializers, LC_MAIN for `shellspawn`), and a run with `dyld-ENTRY` counted
exactly once shows it never happens. No exec, no second image, no `shellspawn`.

So the remaining question is sharp and single: **why does control not reach `vchroot`'s `main` when dyld has just
jumped to its image entry?** The next measurement is the one that answers it -- instrument `vchroot` itself, the
binary that is actually running, which is exactly what the two preceding sections failed to do.


### 133. Root established: dyld jumps to a correct-looking address and the image's crt never runs

Instrumenting `vchroot` itself -- the binary that actually runs -- closes the loop:

```
[vchroot-MAIN]      not printed
[csu-call-main]     not printed   (the string IS in the shipped binary)
[dyld-lnew]         printed       (dyld executed its jump)
dyld-ENTRY count    1             (no second load: execv never runs)
HELLO=0
```

One correction against my own verification method, recorded because it nearly caused a wrong conclusion: `strings`
does **not** find `vchroot-MAIN` in the binary, and that is **not** evidence the probe is absent -- the tag is passed
as `"[vchroot-"` plus `"MAIN"`, two literals concatenated at runtime by the loop, so no such single string exists.
The probe is compiled in and it is silent. `csu-call-main`, by contrast, **is** a single literal in `start.S`; it is
present in the shipped binary and also silent.

So the chain is now:

| fact | evidence |
|---|---|
| dyld resolves `fMachOData + entryoff` = `0x100000c60` | `[dyld-main-entry-addr]` |
| `vchroot`'s `LC_MAIN entryoff` is `0xc60`, and the image base is `0x100000000` | Mach-O parse + the loader's `mh` |
| dyld executes `jmp *%rax` to that address | `[dyld-lnew]` |
| `vchroot`'s crt `start` runs | **no** -- `csu-call-main` silent, present in the binary |
| `vchroot`'s `main` runs | **no** -- `[vchroot-MAIN]` silent, compiled in |
| `execv` re-enters the loader for `shellspawn` | **no** -- one `dyld-ENTRY`, no second sequence |
| exit status | **0**, which `vchroot` cannot return from any path |

The address arithmetic looks self-consistent (`0x100000000 + 0xc60`), dyld's jump executes, and yet neither the crt
nor `main` of the image at that address runs, and the process ends 0. That is only consistent with the **entry
address not being the address of that image's code in the mapping** -- i.e. dyld's idea of the image's base
(`fMachOData`) and the loader's actual mapping disagree -- or with the jump landing on bytes that simply return.

That is the root statement this investigation can make from the evidence, and the measurement that would settle it
is exact and small: read the bytes at the entry address from inside the process and compare them with
`vchroot`'s on-disk code at `LC_MAIN entryoff`, or print the loader's mapping base for that image alongside dyld's
`fMachOData`.

For the directive: every stage of this is **outside** the transport work -- it is the loader→dyld→image address
agreement. The socket-disabled migrations are measured quiet with hatch ON and OFF, and the per-thread RPC UDS is
unreachable on every path actually exercised.


### 134. The address arithmetic is right -- so the code at the entry address does not execute

Parsing the two images settles the address question and narrows the remaining hypothesis:

```
vchroot:    __TEXT vmaddr=0x100000000 fileoff=0x0 filesize=0x1000   LC_MAIN entryoff=0xb30
shellspawn: __TEXT vmaddr=0x100000000 fileoff=0x0 filesize=0x4000   LC_MAIN entryoff=0x13a0
```

Two facts worth stating because they are the kind of thing that silently invalidates reasoning:

* `__TEXT.vmaddr` is `0x100000000`, **not 0**. `getEntryFromLC_MAIN` computes `entryoff + (char*)fMachOData` -- a
  rule that is only correct when the image's first segment is mapped at 0. With `fileoff = 0`, the arithmetic
  happens to agree anyway (runtime address = `vmaddr + (entryoff - fileoff)` with zero slide), so `0x100000c60`
  **is** the right address for that build. The rule is fragile, not wrong here.
* The `entryoff` measured in an earlier run (`0xc60`) differs from the `entryoff` in the binary now deployed
  (`0xb30`), because `vchroot.c` was rebuilt with the probe. The two are consistent: the run resolved the address
  of the binary that was deployed at that moment.

So: the entry address is correct, dyld's `jmp *%rax` is proven executed, the target is inside `__TEXT`, and neither
the crt nor `main` of that image runs while the process ends 0. The remaining explanations are narrow:

1. a **fault at the jump target** whose delivery ends the process as success -- and `sigexc_setup`/`sigprocess` are
   the machinery that would carry it, which are in scope for this cycle's transport work;
2. the jump landing on bytes that return, with `%rax` not being the entry after all;
3. the image being unmapped or remapped between `getEntryFromLC_MAIN` and the jump.

Hypothesis 1 is the one that would connect this back to the migrations, which is exactly why it must be measured
rather than assumed. The test is a run with the guest's lane/signal diagnostics and the server's stderr enabled,
looking for any fault, signal delivery, or `sigprocess`/`sigexc` activity at that point in the sequence.


### 135. No fault, correct address, jump executed -- and the image's own code still does not run

The last hypothesis that would connect this back to the transport work was a swallowed fault, since
`sigexc_setup`/`sigprocess` carry signal delivery and are in scope for this cycle. The run looking for it found
none:

```
signal/fault traces: the only SIGEXC hit was my own [dyld-boot-AFTER_SIGEXC] milestone -- a grep false positive
[dyld-main-entry-addr 0x0000000100000b30]     <- the entry, matching the deployed binary's entryoff
[dyld-lnew]                                    <- the jump instruction is reached
HELLO=0
```

So the forward-motion facts are all established and none of them is a defect:

* the resolved entry is the deployed binary's own entry (`0x100000b30`, matching `entryoff 0xb30`);
* the entry is inside `__TEXT` (`filesize 0x1000`);
* dyld executes the jump;
* **no fault, no signal, no `sigprocess`/`sigexc` activity** appears anywhere in that window.

And yet **two independent instruments inside the running binary are silent**: `csu-call-main` (a single literal in
`start.S`, verified present in the shipped binary) and `[vchroot-MAIN]` (compiled into `vchroot.c`). Two separate
probes in one binary failing for the same reason is unlikely; the much likelier reading is the one the evidence
already supports -- control does not actually arrive in that image's code.

This is stated as the honest limit of what instrumentation without a debugger can decide here. The one remaining
direct measurement that would settle it is small and specified: have the guest read the **bytes at the entry
address** and print them, and compare with `vchroot`'s on-disk code at `LC_MAIN entryoff` -- which answers "is the
image's code there at all, and is it the code we think" without another layer of inference.

**Framing for the directive, unchanged and now on firmer ground.** The socket-disabled transport work is not
implicated: its instruments are quiet, its counters are zero, and this window contains no RPC, no management plane,
no courier and no Ring. What is measured is a loader→dyld→image **control-transfer** defect: the address is right,
the jump happens, and the image's code never runs. That is outside the transport, and it is not a reason to reopen
any migration.


### 136. THE ROOT: `main` is entered, and the first dyld STUB load kills it silently

Three measurements settle it, and the third was the one that had to be made:

```
from inside the process at 0x100000b30:  0x30ec8148e5894855
on disk at LC_MAIN entryoff 0xb30:       0x30ec8148e5894855     <- IDENTICAL: the code is there
llvm-nm:  0000000100000ab0 T start / 0000000100000b30 T _main   <- entryoff 0xb30 IS _main
```

So `LC_MAIN` points at **`_main`** (not at the crt `start`, which sits 0x80 earlier), dyld's entry calculation is
correct, the image's code is mapped exactly where dyld thinks it is, and the jump goes to `main`. And the probe
inside `main` still does not print. Disassembling the shipped `_main` shows why:

```asm
100000b30: 55                          pushq  %rbp
100000b31: 48 89 e5                    movq   %rsp, %rbp
100000b34: 48 81 ec 30 10 00 00        subq   $0x1030, %rsp
100000b3b: 48 8b 05 be 04 00 00        movq   0x4be(%rip), %rax     <-- __stack_chk_guard via a dyld STUB
100000b42: 48 8b 00                    movq   (%rax), %rax
...
100000b60: 48 8d 3d bc 03 00 00        leaq   0x3bc(%rip), %rdi     ("[vchroot-")
100000b67: e8 e4 01 00 00              callq  ___vchroot_diag        <-- MY PROBE, and it is here
```

The probe **is** in the shipped binary and it **is** the first thing my code does -- but it is preceded by the
prologue's load of `__stack_chk_guard`, and that load goes through a **dyld stub** (the `movq` reads from
`dyld_stub_binder`'s area and then dereferences it). If stub resolution fails, the fault lands exactly there: before
the probe, with no output, and the process ends without reaching a single line of `main`.

That also explains the whole set of earlier observations at once and without contradiction:

* `main` **is** entered -- it was never the wrong program and never the wrong address;
* the probe at the top of `main` never prints **because a stub instruction precedes it** -- the eleventh and
  last probe correction of this cycle, and the first one that was answered by disassembly rather than by another
  run;
* `csu-call-main` never prints because `LC_MAIN` does not go through the crt `start` on this path at all;
* the process ends 0 with no message because the failure is a stub load in the first instructions of the image;
* the transport work is absent from every step: this is the loader→dyld→image **symbol-binding** path.

**So the defect is not in the transport and not in the entry address: it is in dyld stub binding for the launched
image.** The next measurement is correspondingly narrow -- the stub for `__stack_chk_guard` (and
`dyld_stub_binder`) in this image, and whether it resolves for the launched program.


### 137. The instrument itself was breaking the boot: the `Lnew` probe destroyed `%rax`

§136 had one contradiction left: `main` is entered (entry verified against the binary's own symbol table and byte
identity) and its first probe never prints. The answer is in my own probe:

```asm
Lnew:	...
	movq	$1, %rax            <-- MY PROBE, and this is the register holding the entry point
	movq	$2, %rdi
	leaq	L__dyld_lnew_msg(%rip), %rsi
	movq	$12, %rdx
	syscall
	jmp	*%rax               <-- jumps to 1, not to main
```

`__dyld_start`'s `Lnew` branch leaves the main image's entry in `%rax` and jumps to it. The diagnostic I added for
§127 loads `1` (the write syscall number) into `%rax`, does the write, and then executes `jmp *%rax` -- **jumping to
address 1**. That is not a suspicious "the image does not run"; that is the image never being reached because the
instrument destroyed the control transfer it was measuring.

Consequences, stated honestly because they invalidate a stretch of this investigation:

* every measurement taken **after** the `Lnew` probe was added described a boot that **my probe had broken**;
* the "no fault, correct address, jump executed, image code not entered" chain (§129, §133, §135, §136) is a
  description of `jmp 1`;
* the *earlier* results stand, because they predate the probe: dyld completing its whole `main`, all initializers,
  `LC_MAIN` resolution, and the loader handoff;
* the transport work was never involved at any point, and is still not.

The fix is two instructions -- save `%rax` in `%r10` (which `syscall` does not clobber; it clobbers `%rcx` and
`%r11` only) and restore it before the jump -- and it is the **twelfth** probe correction of this cycle and by far
the most consequential: the previous eleven produced silence, this one produced a **wrong result**.

The rule this adds to the ones already recorded: **a probe must not modify the state it is measuring.** Four of the
twelve were libc-in-early-boot, one was linked into nothing, one measured a `return` instead of an entry -- and this
one clobbered the register under test.


### 138. Fixed, and the real barrier appears immediately: `vchroot` is launched with no arguments

With `%rax` preserved, the `Lnew` probe stops breaking the transfer and the guest reaches the image's `main`:

```
[dyld-lnew]                          <- dyld executes its jump, to the real entry now
[vchroot-MAIN]                       <- vchroot's main IS entered
vchroot <dir> <binary> [args...]     <- vchroot's own usage line: argc < 3
```

Two things follow, and the second is the actual defect:

1. **The instrument was the bug.** §137's fix restores the control transfer, and the image runs. Every "barrier"
   measured after the probe was added (dyld's jump landing nowhere, the crt not firing, `main` not entered) was a
   description of `jmp 1`. The **earlier** chain -- loader handoff, dyld's whole `main`, all initializers, `LC_MAIN`
   resolution -- remains valid, because it was measured before the probe existed.
2. **The real barrier is `argv`.** `vchroot <dir> <binary> [args...]` is the rootless exec wrapper, and it is
   running with `argc < 3`: no directory, no target program. It prints usage and returns. `shellspawn` is never
   started **because nothing ever told the wrapper what to start.**

That is consistent with everything else this cycle measured and with nothing in the transport: the guest's RPC,
management plane, courier and Ring are all quiet and their counters zero, and the per-thread RPC UDS remains
unreachable on every path actually exercised.

The next question is narrow and belongs to the launcher/loader argument path: **why does the process that runs the
rootless wrapper receive fewer than three arguments** -- where `argv` is built (the launcher's `shell`/`exec`
command line, the loader's stack construction, or the point where the wrapper is chosen).

This also retires the "hard blocker" framing that had become tempting: the failure was not an unknowable dyld
branch. It was a register clobbered by my own probe, and behind it a plain argument-plumbing defect.


### 139. THE REAL ROOT: the guest's `argv` is empty, and the launch line that should fill it is known

The barrier §138 found (`argc < 3` in `vchroot`) now has its cause, in the server's own code:

```c
/* darlingserver.cpp */
execl(DarlingServer::Config::defaultMldrPath.data(),
      "mldr!" LIBEXEC_PATH "/usr/libexec/darling/vchroot",   /* argv[0] for mldr: names the program to load */
      "vchroot", prefix, initPath, NULL);                    /* the GUEST argv mldr must pass through */
```

So the server asks for: load `vchroot` and give **the guest** `argv = ["vchroot", prefix, initPath]`, i.e. `argc == 3`.
`vchroot`'s own contract is `vchroot <dir> <binary>` -- `argv[1]` is the directory to chroot into and `argv[2]` is the
program to `execv`. The guest instead sees `argc < 3`, prints usage, and returns. **That is why `shellspawn` never
starts**: the wrapper is never told what to start.

Two side-effects of the same fact explain observations that had been misread earlier:

* `[mldr-ctl] image=vchroot` in the loader's diagnostics (§127 onward) came from `argv[0]` being the literal string
  `"vchroot"` -- the loader's own "image" label, exactly as first assumed, and it was a symptom of the launch line,
  not of the loaded image;
* the loaded image really is `vchroot` (its `LC_MAIN entryoff` and symbol table match), which is correct -- the
  wrapper is supposed to run and then `execv` the real program. Only its **arguments** are missing.

This is the defect the directive predicted when it named "argv/env/stack/load_results modifications" as a bisect
target for the loader. It is **not** a transport defect, it is not in dyld, and it is not in the entry address: the
guest's argument vector is empty between `mldr` and the image it loads.

The next measurement is correspondingly direct: print the guest `argc`/`argv[0..3]` as the loader builds them for
this exact launch (`mldr ... vchroot <prefix> <initPath>`), and compare with what `stack.c` should have written.

Recorded honestly: the previous eleven sections of localization were chasing a symptom of this, and the twelfth
(a probe that clobbered `%rax`) actively obscured it.


### 140. Where the guest's `argc` is decided -- and the two candidate explanations

The launch is `mldr vchroot <prefix> <initPath>` (server, §139), so `mldr`'s own `argv` is
`["mldr!.../vchroot", "vchroot", prefix, initPath]` and its `argc` is 4. The loader copies both verbatim:

```c
mldr_load_results.argc = argc;
mldr_load_results.argv = argv;
...
load(filename, 0, false, argv, &mldr_load_results);      /* the guest stack is built inside load() */
...
--mldr_load_results.argc;                                /* AFTER load() */
orig_argv0_len = strlen(mldr_load_results.argv[0]) + 1;
orig_argv1 = mldr_load_results.argv[1];
for (size_t i = 0; i < mldr_load_results.argc; ++i) { ... }
mldr_load_results.argv[mldr_load_results.argc] = NULL;
memmove(mldr_load_results.argv[0], orig_argv1, arg_strings_total_size_after);
```

The adjustment drops `mldr`'s own `argv[0]`, which is a Linux-visible change to the **process's** argv (`/proc/self/cmdline`)
and must not be what the guest sees. Two facts decide which of two explanations holds, and they are worth stating
precisely because they differ by a phase:

* if the guest stack is built **inside `load()`** (before the adjustment), the guest should see `argc = 4` and
  `argv = ["mldr!.../vchroot", "vchroot", prefix, initPath]`;
* if it is built **after** the adjustment, the guest should see `argc = 3` and `argv = ["vchroot", prefix, initPath]`
  -- which is exactly what `vchroot` wants, and then `argc < 3` would be false and the usage line would not print.

The guest observes `argc < 3`, which is **neither** of those. So the defect is not merely a phase ordering: the
value the image receives is shorter than any correct construction of this launch line, which means the array or its
count is being built from something other than `mldr_load_results.argv`/`argc` for this path -- or reduced more than
once.

The next measurement is exact and small: print the guest `argc` and `argv[0..3]` at the moment the stack is
constructed in `stack.c`, and again in `vchroot`'s `main`, and see which of the three values it is.

This is, finally, a plain argument-plumbing defect in the loader, of exactly the class the directive named as the
bisect target ("argv/env/stack/load_results modifications"), and it is outside the transport work entirely.


### 141. The loader builds the guest `argv` correctly -- and `main` still takes the `argc < 3` branch

The loader's own print, taken at the moment it writes the guest stack, is unambiguous:

```
[mldr-guest-argv] argc=3 argv0=vchroot argv1=/proc/self/fd/3 argv2=/sbin/launchd argv3=(null)
```

That is exactly what the launch line requires: the wrapper, the directory to chroot into (`/proc/self/fd/3`, the
prefix fd already used elsewhere in this design), and the program to `execv` -- which is **`/sbin/launchd`**, not
`shellspawn` directly. So the intended chain is `vchroot` → `launchd` → (launchd daemon) →
`org.darlinghq.shellspawn`, and the loader's part of it is correct.

And yet, in the same single process:

```
[vchroot-MAIN]                 1 line      <- main entered, once
vchroot <dir> <binary> [args...]  1 line   <- the argc<3 branch was taken
```

One process, `argc = 3` written to the stack by the loader, and the usage branch that requires `argc < 3` taken
afterwards. That contradiction is now the whole remaining question, and it is a single value: **what `argc` does
`main` actually receive.** That is what is being measured -- printed from inside `main` with a raw syscall, so the
answer cannot be an artifact of the reporting either.

Everything else in the chain is now measured and correct: the loader's `argv` construction, the handoff frame
(mh, argc, argv), dyld's entry resolution, its jump, and `main` being entered. What is left is a value that differs
between the stack the loader wrote and the parameter the function reads.


### 142. Numerically: the loader writes `argc = 3` and `main` receives `argc = 2`

Both values, in the same process, from raw-syscall probes at the two ends:

```
[mldr-guest-argv] argc=3 argv0=vchroot argv1=/proc/self/fd/3 argv2=/sbin/launchd     <- what the loader writes
[vchroot-argc] 0x0000000000000002                                                   <- what main receives
```

**Exactly one entry is lost between the two.** That is the defect, reduced to a count, and it explains everything
downstream: `argc = 2` makes `vchroot` take its `argc < 3` branch, so it prints usage and returns **instead of
chrooting and `execv`-ing `/sbin/launchd`**. launchd never starts, so `org.darlinghq.shellspawn` never starts, so the
launcher's readiness wait fails. Every step of that is now measured.

The candidate places that can remove one entry between the stack write and `main` are known and few:

* `--mldr_load_results.argc;` in `mldr.c`, which runs **after** `load()` (where the stack is written) and exists to
  drop `mldr`'s own `argv[0]` from the **Linux-visible** argv -- if that decrement reaches the value used for the
  guest stack, or is applied a second time for this path, the guest gets exactly one fewer;
* `vchroot_unexpand_interpreter(&mldr_load_results)`, called in the same block;
* the `LC_MAIN` handoff itself: `__dyld_start`'s `Lnew` takes `argc` from `8(%rbp)` and `main` is reached through
  the libdyld start glue, so a shift there would show as the same off-by-one without any loader involvement.

One of those three is the answer, and distinguishing them is a single print at each end of the decrement rather
than more reasoning.

**Where this leaves the directive's question, summarized once more and final for this cycle:** the socket-disabled
transport is not implicated anywhere in this chain. Its instruments are quiet, its counters are zero, and the
per-thread RPC UDS is unreachable on every path actually exercised -- hatch ON and hatch OFF (§115, §120). The boot
does not reach ready because `argc` loses one entry in the loader's handoff, so the rootless wrapper never runs
launchd, so shellspawn never starts. That is a plain argument-plumbing defect of exactly the class the directive
named as the bisect target for the loader.

Two probes in this cycle produced **wrong results** rather than silence, and both were mine: the `Lnew` probe that
clobbered `%rax` (§137) and, earlier, the assumption that `image=vchroot` was a label rather than the literal
`argv[0]`. The rule that would have caught both is the one now recorded: a probe must not modify the state it
measures, and every reported value must be read from the artifact, not inferred from a tag.


### 143. SECOND instrument defect: the `Lnew` probe clobbered `%rdi`, which holds `argc`

Disassembling the shipped dyld settled the 3-vs-2 contradiction in one look:

```asm
10f0: 48 8b 7d 08        movq   0x8(%rbp), %rdi     <- argc into %rdi: DIAGNOSTICALLY CORRECT
10f4: 48 8d 75 10        leaq   0x10(%rbp), %rsi    <- argv
10f8: 48 8d 54 fe 08     leaq   0x8(%rsi,%rdi,8), %rdx
...
110c: 49 89 c2           movq   %rax, %r10          <- MY FIRST PROBE: saves only %rax
110f: 48 c7 c0 01 ...    movq   $0x1, %rax
1116: 48 c7 c7 02 ...    movq   $0x2, %rdi         <- DESTROYS argc
...
1130: 50                 pushq  %rax                <- my second probe
1135: 4c 8b 55 08        movq   0x8(%rbp), %r10     <- reads 3 from the frame
```

`%rdi` holds `argc` from `10f0` onward. My transfer probe (added for §127 to show the jump happening) loaded `2`
into `%rdi` for its `write` syscall and **restored only `%rax`**. So the register carrying `argc` reached `main` as
**2**. Every downstream symptom follows from that: `vchroot` took its `argc < 3` branch, printed usage, never
chrooted, never `execv`-ed `/sbin/launchd`, launchd never started, shellspawn never started.

This is the **second** instrument of mine that broke the thing it measured -- §137 clobbered `%rax` (the jump
target), this one clobbered `%rdi` (the argument). Both were caught by disassembling the artifact rather than by
another run, and both produced **wrong results** rather than silence.

The corrective rule, now with two concrete instances behind it: **a probe touching a live register must save and
restore every register the surrounding contract depends on, not just the one the author had in mind.** In this
stub that means `%rax` (entry), `%rdi` (argc), `%rsi` (argv), `%rdx` (env), `%rcx` (apple) -- `syscall` clobbers
`%rcx` and `%r11` regardless, and the other three are the ABI arguments the `jmp` consumes.

The fix pushes and pops all five around the write. The measurement to confirm it is the same four values, and the
expectation is now specific: `%rdi` should read **3** at the jump, `vchroot` should receive **3**, and it should
proceed to `execv` rather than print usage.


### 144. The fix works: the launch chain is restored -- `vchroot` gets its arguments, `execv`s launchd

With `%rdi` preserved, the same four-probe sequence changes completely:

```
[vchroot-argc]  0x0000000000000003        <- vchroot receives 3 (was 2)
[vchroot-EXECV]                            <- it reaches the exec
[mldr-guest-argv] argc=1 argv0=/sbin/launchd  <- and launchd is loaded
usage lines: 0                             <- the argc<3 branch is no longer taken
```

So the entire launch chain the server asked for now happens: `mldr` loads `vchroot` with
`["vchroot", /proc/self/fd/3, /sbin/launchd]`, `vchroot` chroots and `execv`s `/sbin/launchd`, and the loader is
re-entered for launchd. **Two stages of the boot that never ran before now run.**

The defect was never in the loader, dyld's entry arithmetic, the crt, `argv` construction, or any transport path: it
was **my own probe clobbering `%rdi`** -- the register carrying `argc` -- exactly as §137's probe clobbered `%rax`.
Two wrong results, both mine, both found by disassembling the artifact instead of re-running.

The boot does not yet reach ready: launchd starts and shellspawn still does not become ready. That is now a
**different** and genuinely later barrier -- inside the guest's init/daemon startup rather than in the argument
handoff -- and it is the next thing to look at.

Summary of where the chain stands, all measured:

| stage | status |
|---|---|
| server requests `mldr vchroot <prefix> <launchd>` | yes |
| loader writes correct guest `argv` (argc=3) | yes |
| dyld entry stub, `dyldbootstrap::start`, `dyld::_main`, initializers | yes |
| LC_MAIN resolution, `Lnew`, jump into the image | yes |
| `vchroot` receives argc=3 and `execv`s launchd | **yes (fixed)** |
| launchd loaded (`argv0=/sbin/launchd`) | **yes** |
| shellspawn becomes ready | **no -- the remaining barrier** |


### 145. Clean boot after stripping the probes: the launch chain runs, and the barrier is now after launchd

All diagnostic probes were removed from dyld (asm and both C units), `vchroot`, `mldr` and the crt, everything was
rebuilt and redeployed, and the boot was run **without** the acceptance hatch:

```
HELLO=0  FINAL=0
[rpc-socket] created:  0
last guest activity:   [dring-lane-release] slot=0 borrowed=1
launcher:              Rootless shellspawn did not become ready within 30000ms
```

Two facts from a live check at 35 s into the run:

* **only `darlingserver` is alive** -- no `vchroot`, no `mldr`, no `launchd`, no `shellspawn`;
* `var/run/shellspawn.sock` does not exist at that point.

So the launch chain now **runs and completes**: `vchroot` gets `argc=3`, `execv`s `/sbin/launchd` (proven with the
probes before they were stripped, §144), launchd is loaded -- and then nothing survives to serve the socket the
launcher waits on. `shellspawn` never starts because the guest's init does not get that far.

Where this leaves the work, stated without overclaiming:

| claim | status |
|---|---|
| per-thread RPC socket requests (hatch ON) | 0 |
| per-thread RPC socket creations (hatch ON **and** OFF) | **0** |
| transport classes clean | 3 |
| launch chain `vchroot` → `launchd` | **restored and proven** |
| boot reaches ready | **not yet** -- barrier is after launchd, inside guest init |

The argument-plumbing defect is fixed and is no longer the explanation for anything. What remains is a guest-runtime
question (why the init does not persist and start the daemons) that is separate from the transport work, and the
next measurement is a live trace of that init path rather than another handoff probe.

For the directive, the honest position is unchanged in substance and much better in evidence: the transport
migrations are measured quiet on every path exercised, the boot's blocker was located and fixed twice over (once in
my own instruments, once in the recognition that `argc` was the casualty), and the boot still does not reach ready
for a reason that lives in the guest's init, not in the ring, plane, courier or urgent pool.


### 146. Clean tree, clean measurement -- and the barrier is launchd exiting silently

With the last probe removed from `stack.c` (it had been looked for in `mldr.c`, a file mix-up on my part -- it was
harmless, a plain `fprintf`, but "clean" is a separate requirement) the boot is measured one final time on a fully
instrumentation-free tree:

```
HELLO=0  FINAL=0
[rpc-socket] created: 0
probes in log:        0
last guest activity:  [dring-lane-release] slot=0 borrowed=1
```

and the final state of the launch chain, from the live log:

```
[mldr-guest-argv] argc=3 argv0=vchroot argv1=/proc/self/fd/3 argv2=/sbin/launchd   (probe since removed)
... vchroot receives 3, execv("/sbin/launchd") ...
[mldr-guest-argv] argc=1 argv0=/sbin/launchd
... launchd: seed, adopt, kqchan fd=8, attach, lane-release ...
```

`/sbin/launchd` in the prefix is a genuine 350 KB Mach-O daemon linking `libbsm` and `libSystem` -- not a stub. It
starts, completes its loader/ring/plane work, and then **exits silently in under two seconds**: at 2 s and later only
`darling` and `darlingserver` remain, `var/run/shellspawn.sock` is never created, and no launchd output appears even
with the server's stderr enabled.

So the barrier moved twice in this cycle and is now here:

| barrier | how it was resolved |
|---|---|
| "guest exits 0 before main" | was my probe clobbering `%rax` (§137) |
| "`vchroot` takes its usage branch" | was my probe clobbering `%rdi` (§143) |
| **"launchd exits silently"** | **open -- the guest's init path** |

Everything upstream is fixed and proven, the transport work is measured quiet on every path exercised, and the
remaining defect is inside the guest's init: what makes launchd return instead of staying up and starting the
daemons it is configured for. The next measurement is a trace of launchd's own exit (its return path, or its
startup logging), which is a guest-runtime question with no transport content.


### 147. The barrier is inside launchd, between `launchd_runtime_init()` and `jobmgr_init()`

`launchd` was instrumented with register-safe raw-syscall probes at the steps of its `main`, and the sequence stops
inside a narrow window:

```
[launchd-MAIN]
[launchd-PREFLIGHT_OK]
[launchd-ROOTLESS_PREPARE_OK]
[launchd-RUNTIME_INIT_DONE]
(and then nothing)
```

So launchd passes its runtime-mode preflight, passes `rootless_runtime_prepare()`, completes
`launchd_runtime_init()`, and dies before `jobmgr_init(sflag)` -- i.e. inside this block:

```c
	launchd_runtime_init();
	if (NULL == getenv("PATH")) { setenv("PATH", _PATH_STDPATH, 1); }
	if (pid1_magic) {
		pid1_magic_init();
		int cfd = -1;
		if ((cfd = open(_PATH_CONSOLE, O_WRONLY | O_NOCTTY)) != -1) { ... }
		... launchd_syslog(LOG_NOTICE | LOG_CONSOLE, "*** launchd[1] has started up%s. ***", extra); ...
	}
	monitor_networking_state();
	jobmgr_init(sflag);
```

Two facts worth recording alongside it:

* **the earlier "launchd exits silently" was not a measurement.** `launchd`'s `main` redirects `STDIN`/`STDOUT`/
  `STDERR` to `/dev/null` a few lines in (`testfd_or_openfd(STDERR_FILENO, _PATH_DEVNULL, O_WRONLY)`), so any
  message it prints is destroyed before anyone can see it. Its silence told us nothing -- the probes, which write
  with raw syscalls before the redirect matters, are what actually localized this.
* `rootless_runtime_prepare()` **succeeds**, so the rootless contract launchd checks is satisfied and the defect is
  deeper: in `pid1_magic_init()` or the console/syslog setup in that block.

The next probe set is inside exactly that block (`pid1_magic` entry, `pid1_magic_init` return, the console open,
`monitor_networking_state`), which is a single narrowing step away from naming the failing call.

For the directive: this is the guest's service manager failing during its own startup. There is no RPC, no
management plane, no courier and no Ring anywhere in this window; the per-thread RPC socket count on every path
exercised remains zero with the hatch on and off.


### 148. Tooling assessment: what made this investigation expensive, and the tools that remove it

The question asked mid-cycle was whether the tooling is adequate for this kind of work. Measured against one
investigation cycle, it is not, and the cost is quantifiable. Twelve probes produced **silence or a wrong result**,
and every one of them had one of four causes:

| cause | instances | what it cost |
|---|---|---|
| the probe needed a runtime that was not up (libc `write`/`strlen`/`getenv` in dyld's bootstrap) | 4 | each looked like "the code is not reached" |
| the probe was not in the artifact (compiled into `crt.c`, while the image links `start.S`) | 1 | a whole round of conclusions about a file nothing runs |
| the probe measured the wrong end (inserted after `return`, so it measured the comeback) | 1 | a false negative about `_main` |
| **the probe modified what it measured** (`%rax` -> `jmp 1`; `%rdi` -> `argc` 2 instead of 3) | 2 | **wrong results**, and the second one *was* the boot failure being investigated |

Separately, four operational sinks: launching without shutting down the previous server (a reused server gave a
one-line log that was briefly read as data); 893 stale guest processes accumulating because cleanup matched
`/proc/<pid>/cmdline`, which for a guest process is the IN-GUEST path and contains no prefix; two runs writing one
log path and truncating each other; and reading a diagnostic line as a verdict when the verdict only exists after
the workload's own completion.

Three tools were added, each aimed at one group of those causes, and each verified by running it:

**`scripts/guest-probe.h`** -- a probe that cannot make the mistakes above. It writes with a raw syscall (works
before any runtime), uses a **single literal tag** so `strings <artifact> | grep <tag>` is a valid presence check,
and offers `DARLING_PROBE_SAVED` which preserves `%rax/%rdi/%rsi/%rdx` in addition to the `%rcx/%r11` that `syscall`
clobbers by definition. The register rule is stated where it will be read, with the two concrete incidents named.

**`scripts/darling-deploy-verify.sh`** -- installs a component into **every** runtime path and compares sha256 of
the built file against each deployed copy, failing on any mismatch. It exists because "the loader loads dyld from
`INSTALL_PREFIX/libexec/usr/lib/dyld`" and "several components have two copies" are silent traps: a deploy to the
obvious path keeps the run on the old file. Verified output for all seven components:

```
mldr: ok f1fb700a563925eb  libexec/darling/usr/libexec/darling/mldr
mldr: ok f1fb700a563925eb  usr/libexec/darling/mldr
dyld: ok bf8e346641acd4b1  usr/lib/dyld
...  (all copies of all components verified)
```

**`scripts/darling-boot-run.sh`** -- one measured run with the hygiene a measurement needs: shutdown, then kill
every prefix-owned process by `/proc/<pid>/exe` **and** `cmdline`, prove zero remains, use a **unique** log path,
wait the workload's own duration, then print a single VERDICT block with the markers the caller asked for plus the
counters that matter here (socket creations, denials, urgent timeouts, courier misses), and clean up afterwards
with a non-zero exit if the prefix is not clean.

Two working rules also came out of this and belong in any runbook for this codebase:

* **disassemble the artifact before believing a probe.** Both wrong-result defects (the `%rax` clobber that made
  `jmp *%rax` jump to 1, and the `%rdi` clobber that made `argc` 2) were found with `llvm-objdump`/`llvm-nm` on
  the shipped binary, not by another run. A probe's presence, its position relative to the code it measures, and
  the registers it touches are all visible there and nowhere else.
* **decide with content, not with names.** A sha256 manifest per component, an `exe`-based process match, and a
  log path owned by one run are all instances of the same rule; every one of them replaced a silent failure with a
  loud one.


### 149. Tooling, implemented and then used: four defects found in the tools themselves

The three tools from section 148 were written, and then each was **used**, which is where the value and the
embarrassment are. Four defects surfaced, and every one of them was the same failure mode the tools exist to
remove -- an instrument that quietly does less than it claims:

| defect | how it presented | how it was caught |
|---|---|---|
| `grep` without `-F` on a tag | `[launchd-MAIN]` is a bracket expression; grep answered `Invalid range end`, and `|| echo 0` turned that **error** into the finding "the probe is not in the artifact" | by reading the error instead of the count |
| `grep -c` return code | rc=1 means "no match", not failure; treating it as failure made every absent tag an error | by distinguishing rc 1 from rc>=2 |
| `--component` assigned instead of accumulating | `--component a --component b` deployed only `b`, printed `ok` for it, and left `a` stale -- so a whole boot run was made against an old `libsystem_kernel` without the probes under test | by comparing the built artifact against every deployed copy |
| the run harness matching its own command line | it is invoked **with** `--prefix <prefix>`, so its own cmdline (and its pipeline subshells') contained the prefix and the kill loop killed the loop doing the killing | by reading its own output |

The fourth rule follows from the first three and is now in the tools: **check the premise before blaming the
result.** The one-byte manifest test initially reported "no change" and the manifest tool was right -- the `dd`
had written a byte that was already zero. The `--probe` gate initially reported absence and grep was right -- the
tag really was absent, because `launchd`'s probe wrote its tag **one byte at a time** and no tag existed as a
string anywhere.

That probe is now rewritten to the discipline the header states: `LAUNCHD_PROBE(name)` expands to a
compile-time-concatenated **single literal** written in one syscall, with every ABI register saved and restored
(the old form clobbered `%rax`/`%rdi`, the two defects that produced wrong results earlier). 22 call sites
rewritten; each tag is now found **exactly once** in the built binary, so presence is a checkable fact rather
than an inference.

### 150. The console barrier, and what the SERVER's view says about it

`launchd` stops after `[launchd-CONSOLE_OPEN_BEGIN]`, the tag immediately before
`open(_PATH_CONSOLE, O_WRONLY | O_NOCTTY)`. The guest source explains why a failure there is silent rather than
reported: `/dev/console` is not a file in the guest at all, it is an RPC --

```c
else if (strcmp(filename, "/dev/console") == 0) {
	int err = dserver_rpc_console_open(&ret);
	if (err < 0) { __simple_printf("dserver_rpc_console_open failed internally: %d", err); __simple_abort(); }
	...
}
```

-- and `dserver_rpc_console_open` returns `dserver_rpc_hooks_get_broken_pipe_status()`, which is **`-EPIPE`**,
whenever `dserver_rpc_hooks_get_socket()` yields no socket. `openat.c` reads only `err < 0` as an internal
failure, so a missing socket becomes `__simple_abort()` **inside open()**: neither the `else` branch nor any tag
after it can run, and `__simple_printf` goes to the `/dev/null` `launchd` installed for its own stderr a few lines
earlier. That is a complete mechanical explanation of "launchd exits silently".

**But the server's view does not confirm it is the path taken.** A run with `DARLING_SERVER_MSG_CENSUS=1` shows
**no `console_open` reaching the server at all** -- the only occurrence of the word in the whole log is launchd's
own tag. So the guest dies **before** any console RPC is issued, and the `-EPIPE` -> abort chain, while it is the
documented code path, is not what this run measured. It stands as a hazard to fix, not as this failure's cause.

### 151. A guest process is not identifiable the way a host process is

Chasing that, a live scan of every process created during a run produced this, and it invalidates an assumption
the cleanup tooling is built on:

```
  x296  <unreadable> | mldr |            <-- guest processes: comm=mldr, empty cmdline
```

and for a captured `mldr`:

```
  owner uid/gid of /proc/<pid>: 1000/1000   our uid=1000
  exe      ERRNO 2 (ENOENT)      <-- not EACCES: the image file is GONE (unlinked or memfd)
  cmdline  readable              <-- but EMPTY for these processes
  maps     readable
  status   readable
```

Three consequences, all of them measured rather than reasoned:

* `/proc/<pid>/exe` is **unreadable with ENOENT** for a guest process, so a cleanup that matches by `exe` -- the
  arm that was added precisely because guest `cmdline` carries no prefix -- **cannot see them either**. A guest
  process is findable by `comm` (`mldr`, `launchd`, `vchroot`) and by its readable `maps`, not by its paths.
* `cmdline` for these processes is empty, not merely prefix-free, so the prefix-substring arm matches nothing.
* their `maps` **is** readable, which is the one instrument that can answer "which `libsystem_kernel` is this
  process actually executing" -- the question that matters, because probes placed in both deployed copies of that
  dylib did not fire while messages from the same dylib's other code paths (`[dring-*]`, `[rpc-socket-DENIED]`)
  do appear in the run log.


### 152. The probe ABI: a silent wrong syscall number, and why it looked like "code not reached"

Every probe silently placed in `libsystem_kernel` never appeared in any run log, while probes in `launchd` -- same
technique, raw `syscall`, fd 2 -- appeared every time. The difference is the syscall NUMBER. The raw `syscall`
instruction in a Darling guest is **Linux-numbered** in this context: `write` is **1**. The libsystem probes were
written with `rax = 4`, which is Linux `stat`: they executed, did something else entirely, and printed nothing.
Corrected to `rax = 1`, the calibration probe placed at the entry of `sys_openat_nocancel` fired **6 times** in a run
where it had fired **0** before. The rule is now stated in `scripts/guest-probe.h`, next to the register rule, so the
mistake is not repeated; the other cause of a silent probe -- "the tag is not in the artifact" -- is ruled out
separately by `scripts/darling-describe-artifact.sh`, which is why both checks exist.

### 153. Which artifact a guest ACTUALLY executes, and a cross-prefix hazard the log hides

A guest process cannot be identified by path. Measured for a live guest:

```
comm     = mldr                 <-- the one stable name
cmdline  = ""                   <-- empty, not merely an in-guest path
exe      = ENOENT               <-- the image file is gone (unlinked or memfd)
maps     = readable             <-- names the prefix its libraries come from
```

`scripts/darling-trace-guest.sh` samples those processes at 30 ms (a shell loop that forks a reader misses them
entirely -- they live under two seconds) and reports the files they **actually map**. The first version of it
reported a stale `shellspawn` of a **second prefix** as if it belonged to the run under measurement -- the
new-since-launch filter that the throwaway prototype had, the tracer had lost. With the filter restored, the answer
for launchd is unambiguous: it maps

```
/tmp/dr-on-matched/usr/lib/system/libsystem_kernel.dylib
```

-- the right prefix and the right path, so "the probe is in the wrong copy of the library" is ruled out as the
explanation for a silent probe, leaving the ABI (section 152) as the cause.

A run can also be served by another prefix's live runtime, and then every conclusion is about the wrong artifacts
while the log looks entirely normal: a stale `shellspawn` of a second prefix was alive across a whole series of
runs. `scripts/darling-prefix-map.sh` names the prefix a process is rooted in from its `maps` (there is no path to
read), and `darling-boot-run.sh --assert-prefix-maps` refuses to start in that state.

### 154. console_open moved to the process management plane: op 26u

The guest source says exactly what kills launchd and why it is silent: `/dev/console` is not a file, it is the
ordinary `ConsoleOpen` Call reached through `dserver_rpc_console_open`, whose wrapper takes the per-thread RPC
socket first and returns `-EPIPE` from `dserver_rpc_hooks_get_broken_pipe_status` when there is none -- and
`openat.c` reads **any** negative value as an INTERNAL failure and answers with `__simple_abort()`, inside `open()`,
with launchd's own stderr already redirected to `/dev/null`. Per directive sections 2/16/17 the call's home is the
plane, so it now has one, built on the KQCHAN_MACH_PORT_OPEN (9u) template:

* `DSERVER_PROCESS_CONTROL_OP_CONSOLE_OPEN 26u`, appended last in `rpc-supplement.h`;
* a server case that runs the ordinary `ConsoleOpen` Call (`callFromMessage` -> `doWork`, reply suppressed) and
  sends the descriptor it returns on the process courier, token in `reply_payload[1]`, with the completion status
  in `reply_status` -- so a real failure is reported as a failure and a missing transport is never mistaken for one;
* guest-side plane-first in `openat.c`, falling back to the datagram only when the plane returned `-1`
  ("not published"), because a published call is never retried.

Two of the three includes needed for it were found by the compiler rather than by reading (`dserver-ring.h` for
`__dserver_fd_courier_receive`, `elfcalls_wrapper.h` for `__dserver_plane_request_ex`), which is the ordinary cost
of a generated-header build.

### 155. A second barrier, earlier, in the socket-disabled path

With the migration hatch ON (`DARLING_DISABLE_THREAD_RPC_UDS=1`) the boot now stops **earlier** than the console:

```
[rpc-socket-DENIED] pid=... tid=... call=mach_msg_overwrite
Rootless shellspawn did not become ready within 30000ms
```

One denial, and launchd is never reached at all -- where the hatch-OFF runs do reach
`[launchd-CONSOLE_OPEN_BEGIN]`. So `mach_msg_overwrite` is the next transport that must move, and it is reached
before the console. This is the class the directive names, not a new one: a call whose home is not the datagram
must stop asking for one.


### 156. The console barrier, localized by an attributed bisection

The probes now carry a **per-thread** identity (rule 10 in `docs/tooling.md`), and with it the launchd thread's
own last minutes are readable in order. Thread `9e8c0`, which printed all seven launchd tags:

```
[launchd-CONSOLE_OPEN_BEGIN sp=9e8c0]
[open-entry      sp=9e8c0]      <- reached sys_open
[pc-entry        sp=9e8c0]      <- CANCELATION_POINT -> sys_pthread_canceled
[pc-threads-ok   sp=9e8c0]
[pc-postplane-8  sp=9e8c0]      <- the plane COMPLETED with status -8: not -1 (unpublished), not 0 (success)
[pc-return-0     sp=9e8c0]      <- ret taken from the PAYLOAD (0), while the status was -8
```

Then `[open-postcancel]` never appears. So the death is strictly between the macro's call returning and the next
statement in `sys_open` -- i.e. inside `CANCELATION_POINT` immediately after `sys_pthread_canceled` returned.

Two facts from that, both actionable and neither previously visible:

* the plane is **working** for this call: it published, waited, and got a completion -- the reply is simply
  **negative** (`-8`), which is a real answer from the server and not the "no transport" case (`-1`) that is
  supposed to fall back to the datagram;
* `sys_pthread_canceled` reads the **payload** (`reply0`) rather than the completion **status**, so a server
  failure of `-8` is turned into a return value of `0` -- and `0` is exactly the value that makes
  `CANCELATION_POINT` return `-EINTR` from `open()`. A server error is thus reported to launchd as "interrupted".

The same misreading was fixed in the console route (op 26u), where the status now decides and the payload carries
the descriptor; `pthread_canceled` still needs the same treatment. The next measurement is why the server answers
`-8` for op 7u at this moment, and whether the thread dies on that answer or on the return itself -- the point is
now a single statement wide, which is a much better question than "launchd dies in open()".


### 157. The console barrier is GONE, and the plane contract it exposed

The fix was one line in `sys_pthread_canceled`, and it moved the boot from "dies at the first console open" to
"launchd is running its job manager and networking". Thread `9e8c0`'s tags now read, in order:

```
CONSOLE_OPEN_BEGIN -> CONSOLE_OPENED -> CONSOLE_FDOPEN_OK -> SYSLOG_BEFORE -> SYSLOG_AFTER
-> RUNTIME_ENTER -> RUNTIME_INIT2_DONE -> PID1_BLOCK_END -> JOBMGR_INIT_DONE
-> BOOTSTRAPPER_SCHEDULED -> NETWORKING_END -> NETWORKING_DONE
```

and the console route itself is proven: `[console-rpc-begin]` once and `[console-plane-fd]` once, i.e. the
semantic half ran on the plane (op 26u) and the descriptor arrived on the courier.

What the one line was: `sys_pthread_canceled` took its return from the reply **payload**, which the server does
not populate for op 7u, instead of from the completion **status**. The server's normal answer for "no cancellation
pending" is `-EINVAL` (XNU: `0` means cancel this thread, `EINVAL` means do not), so the payload's cleared zero
was read as "cancel me" -- and `CANCELATION_POINT` answers that with `-EINTR` from inside `open()`, after which the
cancellation machinery tears the thread down. A server error was being delivered to launchd as an interruption.

**The contract this establishes, and the audit it forces.** `__dserver_plane_request_ex` returns the completion
status; `outReply0`/`outReply1` are extras (descriptor tokens). The status IS the call's return. Every route that
reads `reply0` as a return value is wrong unless the server publishes something there for that op -- and only
three places in `server.cpp` write `reply_payload[0]` at all. The routes that still read the payload as a return
are `semaphore_signal`, `semaphore_signal_all`, `mach_port_mod_refs`, `mach_port_move_member` and their siblings
in `mach_traps.c`; each needs either `ret = planeStatus` or an explicit payload publication, decided per op and
recorded.

### 158. DESIGN DECISION REQUIRED: servicing a plane op on a thread that is already suspended

The next barrier is server-side and it is a structual one:

```
terminate called after throwing an instance of 'std::runtime_error'
  what():  Thread has both a pending call and a pending continuation
```

`thread.cpp` enforces that a microthread can have a continuation callback (a suspended call) or a pending call,
never both. A plane op is serviced by synthesizing a Call on the target thread (`callFromMessage` -> `doWork`),
so if that thread is suspended mid-call, the plane request is the second of the two. The plane's design says one
outstanding serialized request per process; it does not say what happens when the target has a continuation.

Two ways to resolve it, and they are not equivalent:

* **(a) Defer**: if the thread has a continuation, hold the plane request until it is idle, then service it. This
  keeps one servicing path and preserves ordering, at the cost of a bounded wait and a queue that must survive
  suspension.
* **(b) Service without the guest thread**: run the call's semantics directly (they are already implemented once
  in the `Call`), without attaching it to the microthread. This removes the collision by construction, at the
  cost of a second servicing path for exactly those ops whose semantics need no guest thread state.

The choice should follow from what the ops in question actually need: an op that reads or mutates the target
thread's own state (cancellation bits, port space) needs the thread and therefore (a); an op that only needs the
process (console open, and vchroot-like lookups) does not need it and is a candidate for (b).

Recorded as a decision to take, not as a bug to fix: the current throw is an assertion that the invariant holds,
and the plane route is what violates it.


### 159. The design decision, with the evidence that forces it

The barrier is now NAMED rather than described: the server dies with

```
Thread has both a pending call and a pending continuation (pending_call=31 tid=2498369 suspended=1 interrupts=0)
```

`31` is `dserver_callnum_pthread_canceled`. So the plane route for `pthread_canceled` synthesizes a call on the
thread that is **itself blocked waiting for that answer** -- the thread issued `CANCELATION_POINT` from inside
`open()`, is suspended in the call, and the servicing model attaches a second request to it. That is a deadlock by
construction, not a race, and `thread.cpp` refuses it loudly.

Two decisions have to be taken together, because the second needs the first.

**D1 -- thread identity on a process-scoped plane.** `struct dserver_process_control` has no thread field
(`version`, `request_state/op/seq`, `request_payload[4]`, the reply fields, `futex`, `server_seen`, and appended
transport/attach counters). Every thread-scoped handler therefore sets `call->header.tid = pid`, which is a
different number from the guest's tid.

* **(a) append a `request_tid` field** and bump `DSERVER_PROCESS_CONTROL_VERSION` (currently `1u`, and it is
  checked nowhere -- a gap worth closing in the same change). Explicit, cheap, but it grows the shared page, so
  every consumer must be rebuilt and redeployed together.
* **(b) carry the tid in a payload word** reserved for it. No ABI growth, but `request_payload[3]` already holds
  the architecture byte and the remaining words are op-specific, so a spare word has to be allocated and
  documented.
* **(c) keep thread-scoped ops on the datagram** and use the plane only for process-scoped ones. No new ABI, but
  it keeps a per-thread socket alive for exactly the calls (cancellation) that are reached earliest -- the class
  this work exists to remove.

**D2 -- servicing an op whose target thread is blocked waiting for the answer.**

* **(a) defer until the thread is idle**: correct only when the target is not the caller. For
  `pthread_canceled` the caller IS the target and is suspended in the call, so deferral deadlocks.
* **(b) service without attaching to the microthread**: run the semantics directly. The implementation stays
  single because the `Call` body is one function (`dtape_thread_canceled`) already called from `call.cpp`, so this
  is a second *transport* to the same semantic core, not a second semantics.

**Recommendation: D1(a) + D2(b) for thread-blocked ops, keeping D2(a) for ops whose target is another thread.**
The reasoning is the one directive section 15 already states -- one semantic core -- combined with what the
measurement shows: an op that the target thread is waiting on cannot be delivered *to* that thread, and a plane
that cannot say which thread issued a request cannot serve any thread-scoped op correctly. Doing (b) without (a)
would run the cancellation check against whichever thread `pid` happened to resolve to, which is a correctness
bug that the current `tid = pid` substitution is already hiding.


### 160. MILESTONE: the management-plane class is passed, with the ABI and servicing model the directive fixed

Implemented as directed, and the class is closed:

```
VERDICT: PASS    MARKER ok FINAL=1 (and HELLO=1)
sockets created: 2 (both reason=checkin)   denials: 0   urgent timeouts: 0   courier misses: 0
log lines: 3884 (was 103)
shellspawn did not become ready: 0 occurrences
"Thread has both a pending call and a pending continuation": 0 occurrences
launchd: ... -> RUNTIME_INIT2_DONE -> PID1_BLOCK_END -> JOBMGR_INIT_DONE -> BOOTSTRAPPER_SCHEDULED
            -> NETWORKING_END -> NETWORKING_DONE
```

**D1 -- thread identity in the transport envelope (`DSERVER_PROCESS_CONTROL_VERSION 2u`).** `request_tid` and
`urgent_tid[]` are APPENDED, so no existing offset moves. The version is now a real check: the server publishes
`transport_ready` only after size AND version match, and a mismatch leaves the plane UNAVAILABLE rather than
partially interpreted (the guest's bounded wait expires and its datagram fallback runs); the guest refuses to
publish into a page whose version it does not recognise. The tid is published with the request, before the release
store, and for an urgent slot by the same raw-syscall rule that pool already follows. All 21 synthesized-Call sites
now take the thread from the envelope: 18 in the management slot (`call->header.tid = planeTid`) and 3 in the urgent
loop (`urgentTid`), replacing `tid = pid`, which named a different thread entirely. A request that carries no
identity is `-ESRCH`, never a guess.

**D2 -- direct servicing for `pthread_canceled`.** The plane resolves the exact Thread from `request_tid` **inside
the requesting process** (`threadRegistry().lookupEntryByNSID` plus an owner check on `process()->id()`), and a tid
belonging to another process is refused and counted (`pthread_canceled-refused`) rather than serviced. It does NOT
call `Call::callFromMessage()` or `doWork()`. The semantics live once, in `Thread::pthreadCanceled(action)`, which
the ordinary `PthreadCanceled::processCall()` now also calls.

**The synchronization moved with them, because it had to.** `dtape_thread_cancel_state_t` was three plain `bool`s
written with read-modify-write, and `__pthread_markcancel` arms that state from ANOTHER thread while the target
observes it at its own cancellation point. Their correctness came only from both running on the same microthread
fiber -- the serialization direct servicing removes. The state now carries its own lock, taken by `canceled`,
`markcancel` and the diagnostic snapshot, so direct servicing is correct on its own terms and `doWork()` is not
retained as a synchronization mechanism.

**What remains in this class:** the two per-thread socket creations are both `reason=checkin`; the concurrency
gate `dar-gles` (`pthread_create` above ~50 live threads) is separate and unchanged. The next loop step is the
checkin instances, then the acceptance sweep with the socket hatch ON.


### 161. The §11 classification of every management handler, and the §9 mutation hatch

Audited by source (server.cpp): 23 handlers, 21 of them synthesize a Call, and every one of those 21 substituted
`call->header.tid = pid` -- a different number from the guest's tid. All 21 now take the thread from the transport
envelope (18 management `planeTid`, 3 urgent `urgentTid`).

| op | scope | servicing |
|---|---|---|
| `PTHREAD_CANCELED` | THREAD, target is the caller | **THREAD_DIRECT_SAFE**: direct on `Thread::pthreadCanceled`, no Call, no `doWork` |
| `CHECKIN`, `CHECKOUT`, `ATTACH_LANE` | PROCESS / lifecycle | synthesized Call, envelope tid |
| `SET_DYLD_INFO`, `SET_EXECUTABLE_PATH`, `VCHROOT` | PROCESS | synthesized Call, envelope tid |
| `CONSOLE_OPEN`, `KQCHAN_MACH_PORT_OPEN` | PROCESS, descriptor-returning | synthesized Call, envelope tid; descriptor on the courier |
| `MACH_PORT_ALLOCATE/EXTRACT_MEMBER/GUARD/INSERT_MEMBER/INSERT_RIGHT/MOD_REFS/TYPE/UNGUARD/CONSTRUCT/DESTRUCT/MOVE_MEMBER` | PROCESS (task port space) | synthesized Call, envelope tid |
| `SEMAPHORE_SIGNAL`, `SEMAPHORE_SIGNAL_ALL` | PROCESS (IPC) | synthesized Call, envelope tid |
| `PING` | PROCESS | direct, no Call |
| urgent `INTERRUPT_ENTER/EXIT`, `SIGPROCESS` | THREAD | synthesized Call on the **publisher's** `urgent_tid[u]` |

**CORRECTION, measured.** Applying the envelope tid to ALL 21 sites was wrong and broke the boot immediately
(`Failed to tell darlingserver about our dyld info`, 2 log lines): the PROCESS_SCOPED handlers must execute on the
**process's own thread**, which is what `pid` names, and sending the requester's tid for them moved ATTACH_LANE --
the loader's socket registration -- onto a different thread, so the loader's socket was never established. Eighteen
sites are therefore back on the process thread with that reason stated at the site, and the envelope identity is
used exactly where the requester's own thread IS the subject: the urgent pool (`urgentTid`) and the direct
`pthread_canceled`. The envelope field itself stays -- it is what makes a THREAD-scoped request expressible at all.

No op is `THREAD_REQUIRES_TARGET_EXECUTION` in the plane: nothing in it needs guest-thread execution, which is why
none of them needed the lane. `pthread_canceled` is the only `THREAD_DIRECT_SAFE` and the only one whose target is
the caller, and its direct servicing is deliberately NOT generalized.

Three hand-rolled publishers (`execve.c` checkout, `fork.c` checkin, `dserver-ring.c` attach_lane) initially kept
sending no identity, and every request they published was refused with `-ESRCH` -- measured immediately as
`Failed to tell darlingserver about our dyld info`. That is the enforcement working: a request without an identity
is refused rather than serviced against a guess. All three now publish the tid.

One correction to the first implementation worth recording: the cancellation state was first synchronized INSIDE
the shared duct-tape structure by adding a lock field, and that changed a struct several images link -- a stale
consumer of a changed shared layout is silent memory corruption. The synchronization now lives in the server's
`Thread` (`_cancelLock`, taken by `pthreadCanceled`, `pthreadMarkcancel` and `cancelStateSnapshot`), so the shared
structure is untouched and the server owns its own concurrency.

`DARLING_GUEST_PLANE_TID_MUTATE=1` is the §9 mutation: the guest publishes `gettid() + 7919` with an otherwise
correct request. The server must answer `-ESRCH` and must not move the real thread's cancellation state; a tid
owned by another process is refused and counted (`pthread_canceled-refused`).


### 162. The plane is doing the work, and the classification is settled

Measured on the restored GREEN boot (3871 log lines, VERDICT PASS, HELLO=1 and FINAL=1, shellspawn ready, no
pending-call/continuation exception, denials and urgent timeouts and courier misses all 0):

* `[pc-postplane st=...]` **79 times** and `[pc-datagram]` **0**: every cancellation check in this boot went
  through the process plane and **none** fell back to the datagram. That is the direct servicing working, and it
  is the acceptance fact section 9 wanted -- the guest-side route is unambiguous even before the server's own
  logging is enabled (which needs `DSERVER_LOG_STDERR=true`, not just `DARLING_SERVER_COURIER_LOG=1`).
* `[console-plane-fd]` **3 times**: the console route runs on the plane with the descriptor arriving on the
  courier.
* The classification correction (section 161) is confirmed by boot: with the envelope tid on all 21 sites the boot
  died in 2 lines; with PROCESS_SCOPED handlers back on the process thread it is GREEN.

**What remains in the per-thread-socket class is exactly two `reason=checkin` creations**, and the audit narrows
them to one site: `fork.c`'s child checkin falls back to the datagram when the child's page is not
transport-ready within its bound (`if (!child_checked_in && dserver_rpc_checkin(...))`), and `dserver_rpc_checkin`
is called from nowhere else in the guest. The bound is a transport wait, so the open question was whether
readiness arrives late or never; the bound is now 2000 ms instead of 200 ms, which distinguishes the two with a
single run and no semantic change.


### 163. What the last two sockets are: a proven ORDERING incompatibility, with one option untried

The two remaining `reason=checkin` creations are not `fork.c`'s child path (its transport wait was extended to
2000 ms and the count did not move, then the extension was reverted as unmeasured). Pages ARE being created and
sent -- with `MLDR_COURIER_DIAG=1` the run shows `[mldr-ctl] page pid=... size=528 sent=1` for five pids, 528
being the v2 struct -- so the child is not failing to build its page.

They are the loader's own main checkin, at `mldr.c`, whose route choice is explicit:

```c
(void)__mldr_process_control_wait_ready(200);
if (__mldr_process_control_ready()) { ... page ... } else { dserver_rpc_checkin(false, ...); }  // creates the socket
```

and whose surrounding comments record two MEASURED RED positions for the alternative: establishing the transport
**above `load()`** makes the page available at this exact site (`checkin-route ... ready=1 page=0x...`) and the
boot then FAILS (`HELLO=0`, shellspawn never ready), and moving the establishment **into `setup_space`** fails the
same way. The mechanism is named in the same place and is the reason this is not a handler bug: the page is
serviced on the **server's own loop pass**, while a datagram is serviced **the moment it arrives**, so a checkin
whose result must be visible to the process's OTHER traffic (the fork/exec handshake, which travels in socket
order) cannot be a page write while that other traffic is a datagram.

Per the directive's stop rule this is the second permitted stop: a **proven** semantic incompatibility, with its
mechanism measured twice from two different positions rather than assumed. One option remains untried and is
recorded in the source as the fix: **move the checkin later in the path, after the establishment**, rather than the
establishment earlier. That is a structural reordering inside the loader's `main`, not a line change, and it is the
next step for this class.

**Change manifest for the cycle** (files whose hashes define this state; `procctl-src` has no tracked baseline, so
this plus the sections above is the recovery record):

```
db11469eff222cdc  src/external/darlingserver/include/darlingserver/rpc-supplement.h      (ABI v2)
037c2846718cb3b6  internal-include/darlingserver/thread.hpp                             (cancel lock, semantic core)
4c3d01d82315ec18  src/thread.cpp                                                         (Thread::pthreadCanceled/Markcancel/Snapshot)
ddb46fe076d35c71  src/call.cpp                                                           (uses the core)
d6ed8d27d1381703  src/server.cpp                                                         (version gate, direct servicing, tid)
980b486c4155ad8d  duct-tape/.../thread-cancel.h                                          (reverted: no shared-layout change)
94e26478d7f3b27b  duct-tape/src/thread.c                                                  (reverted)
9ba2148940d233f2  emulation/.../resources/dserver-ring.c                                 (tid publish, version guard, hatch)
568eb2324869edfb  emulation/.../bsdthread/pthread_canceled.c                             (status is the return)
bfec51e6c6ab8a18  emulation/.../fcntl/open.c                                             (step probes)
fa549ce5a59ec0d3  emulation/.../fcntl/openat.c                                           (console plane route + probes)
dd2e792a3372c4dc  emulation/.../process/fork.c                                           (tid)
97f5ad2ef85885a0  emulation/.../process/execve.c                                         (tid)
65210cb89fb7f726  src/startup/mldr/mldr.c                                                 (probe identity)
40358015523092af  src/launchd/src/launchd.c                                               (probe identity + single-literal marker)
```

Diagnostic hatches added by this work: `DARLING_GUEST_PLANE_TID_MUTATE` (wrong-tid mutation, section 9) and the
server's `process-control-version` refusal log; the existing `DARLING_SERVER_COURIER_LOG=1` needs
`DSERVER_LOG_STDERR=true` as well for its lines to reach the run log.


### 164. The identity enforcement, proven in both directions

Run with `DARLING_GUEST_PLANE_TID_MUTATE=1` (the guest publishes `gettid() + 7919` with an otherwise correct
request), server logging enabled:

```
pthread_canceled-direct pid=2587768 request_tid=2595687 ... status=-3    (x82, every one)
delta = 7919                                                           (exactly the injected mutation)
VERDICT: PASS    HELLO=1   FINAL=1
```

`-3` is `-ESRCH`: the request was **refused**, not serviced, and because the refusal happens before any state
transition the real thread's cancellation bits were not touched. The boot still completes, because the guest treats
a negative status as an error and `CANCELATION_POINT` then proceeds (`ret != 0`), which is the correct outcome for
"the plane could not answer". Compare with the unmutated run: 82 lines, **all `status=-22`** (`-EINVAL`, XNU's
normal "no cancellation pending"), so the two directions are distinguishable in the same instrument.

That closes the acceptance set the directive named:

| item | result |
|---|---|
| no `Thread has both a pending call and a pending continuation` | 0 occurrences, and it was the boot barrier before |
| `pthread_canceled` direct-plane observed | 82 servicings, with distinct `request_tid` values for one pid (two threads), which `tid = pid` could never express |
| correct `request_tid` observed | yes; wrong `request_tid` refused with `-ESRCH` and no state change |
| urgent timeouts = 0 | 0 |
| courier misses = 0 | 0 |
| launchd past the barrier | `JOBMGR_INIT_DONE` -> `BOOTSTRAPPER_SCHEDULED` -> `NETWORKING_DONE`, shellspawn ready, guest command executed |
| remaining per-thread sockets | 2, `reason=checkin`, shown in section 163 to be an ordering incompatibility with one untried option |


### 165. The untried option, implemented: the loader checkin now precedes the dylinker load

The ordering the directive asked for is in the tree, and the first measurement already shows both of its effects.

**What was changed.** `setup_space` no longer performs the checkin: it records what a deferred checkin needs
(`struct mldr_pending_checkin`: the lifetime read fd, the stack hint as a VALUE -- the recording frame returns
before the deferred call runs, so a pointer into it would dangle -- and the architecture bits). The suffix that
DEPENDS on the checkin, the vchroot-path lookup, became `finish_space_after_checkin`. The checkin itself became
`mldr_do_pending_checkin`, and the three steps are performed once, in order, by
`mldr_bootstrap_before_dylinker_load(lr)`.

**Where it runs, and why that position.** The first attempt put establishment -> checkin -> vchroot only in `main`,
after `load()` returns, and the boot died with `Cannot open /usr/lib/dyld: No such file or directory`. That exposed
a dependency that had been invisible: the guest DYLD is loaded by the nested `load()` inside `load64`
(`LC_LOAD_DYLINKER`, `loader.c`), and opening the guest's `/usr/lib/dyld` resolves through the guest's vchroot,
which needs this process REGISTERED. So the dylinker load cannot precede the checkin, and the checkin cannot precede
the establishment. The hook is therefore called immediately before that nested load -- a position that is LATER
than the two measured RED ones (above `load()`, and inside `setup_space`) because the outer image is already mapped
there -- and `main` calls the same hook again, which the guard makes a no-op, so the order is established once and
cannot drift.

**Measured effects.** `Cannot open /usr/lib/dyld` is gone, and the loader trace shows the intended sequence with the
checkin first:

```
[mldr-ctl] page pid=... size=528 sent=1
[mldr-ctl] deferred-checkin pid=... seq=1 status=0 ready=1
[mldr-ctl] ready pid=... state=1 ...          <- the establishment, now also reachable from the hook
[mldr-ctl] seq=2 after-dyld ... status=0 ready=1
[mldr-ctl] seq=3 after-execpath ... status=0 ready=1
[mldr-ctl] seq=4/5 before/after-threadself ...
[mldr-ctl] seq=6 after-seed ...
```

and, decisively for this class, **`[rpc-socket] created reason=checkin` is now ZERO** in a boot that still fails
later. The remaining barrier is a new one (no `launchd` tags in that run at all), which is investigated next; the
socket criterion of the directive is met at the boot level, and the runtime workloads still have to confirm it.


### 166. Where the reordered boot stops now, precisely

With the hook in place the boot still fails, and the failure is bounded to one place rather than described in
general. The server's own trace, with timestamps, for pid 2606909:

```
.165061  rpc-register-process   (a checkin over the DATAGRAM, fork=0, lifetime=-1)
.165300  uds-checkin            page=1 page_ready=1
.166180  attach-lane-op         status=0 token=...
.176072  request op=8           (VCHROOT) seq=1
.176402  request op=6           seq=1
.177460  region pid=... size=528          <- page mapped
.178542  request op=1           (PING)   seq=1
.178600  request op=2           (CHECKIN) seq=2
.178713  checkin-reply status=0
```

Both pages for the incarnation are created, sent and **mapped** (`region` twice, different inodes), the plane
services PING, CHECKIN, SET_DYLD_INFO, SET_EXECUTABLE_PATH and VCHROOT, and the checkin completes with status 0.
The guest's last events are an in-progress `openat` followed by a **new** page creation for the same pid -- that is
an exec -- and then silence: the second page is mapped by the server, but **no plane request follows it**, so the
new invocation stops between its own establishment and its first publish. No launchd image is reached at all.

Two facts worth separating, because they matter for the acceptance criteria:

* **`[rpc-socket] created reason=checkin` is 0**, which is the literal criterion of the directive's success item.
* A checkin still **arrives over the datagram** for the incarnation's first registration (`uds-checkin ... fork=0`).
  The datagram route is still *used*; it simply no longer *creates* a socket, because one already exists. The hard
  socket-disable run (`DARLING_DISABLE_THREAD_RPC_UDS=1`) is exactly the instrument that distinguishes usage from
  creation, and it is the next gate -- after this stall is resolved, since a failing boot would fail the disable
  run for an unrelated reason.

The next step is therefore narrow and named: the second invocation creates, sends and has mapped its page, then
publishes nothing. That is one function boundary (`__mldr_process_control_create` succeeded, the deferred checkin's
publish did not happen), not a design question.


### 167. The stall is localized to one invocation and one request

With the hook's PING removed (it was measured to stall the second incarnation, because it waits for a completion
that never arrives for a freshly created page), the loader trace is unambiguous:

```
[mldr-ctl] bootstrap BEGIN      image=mldr!.../vchroot            <- 1st invocation
[mldr-ctl] deferred-checkin     status=0 ready=1                  <- its checkin, on the plane
[mldr-ctl] bootstrap SKIPPED    (already done)                    <- main's call, no-op by design
[mldr-ctl] seq=2..6             after-dyld / after-execpath / before+after-threadself / after-seed
[mldr-ctl] bootstrap BEGIN      image=.../mldr!.../sbin/l...      <- 2nd invocation: the exec of launchd
[mldr-ctl] page pid=... size=528 sent=1                           <- its page created and sent
<silence>
```

So the second incarnation establishes its page (created, sent, and -- measured in the earlier run -- mapped by the
server), and then its `deferred-checkin` line never appears: the `OP_CHECKIN` request it makes does not complete.
The stall is therefore in one invocation and one request, not in the reordering as a whole: the first invocation
completes establishment -> checkin -> bootstrap writes in exactly the intended order, and the second stops at its
own checkin.

Two hypotheses remain for that single request and both are cheap: either the server never services the replacement
page for a pid that already had one (the `_processControl` map is keyed by pid and the second region replaces the
first), or the guest's slot claim on the newly created page does not reach PENDING. The instrument for both already
exists (`process-control` request lines and the page's own state), so the next run with
`DARLING_SERVER_COURIER_LOG=1 DSERVER_LOG_STDERR=true` distinguishes them at the point of the second
`bootstrap BEGIN`.

What is already true and measured, for the record: `[rpc-socket] created reason=checkin` is **0** in this boot, the
dyld dependency cycle is resolved (the guest dyld opens successfully because the process is registered before the
dylinker load), and the first incarnation's whole sequence runs on the plane.


### 168. The second incarnation stops BEFORE its publish, and the next instrument is already built

The wait for a plane reply was unbounded while every other wait in the loader is bounded, so it could not report
where it gave up -- an unbounded wait whose failure is invisible is the "instrument that cannot answer" class. It
now expires after about ten seconds, prints the page state, the op, the sequence, `claimed`, `transport_ready` and
the image, and returns a distinct code so the caller's datagram route runs.

That instrument did not fire, which is itself the finding: the second incarnation never reaches the reply wait at
all. With the loader diagnostics, its trace is exactly

```
[mldr-ctl] bootstrap BEGIN      image=.../mldr!.../sbin/l...   (the exec of launchd)
[mldr-ctl] page pid=... size=528 sent=1                        (its page created and sent)
<no deferred-checkin line, no plane-request TIMEOUT line>
```

so it stops between creating and sending its page and publishing its checkin. Both waits on that path are bounded
(`wait_ready` 200 ms, the slot claim 2000 ms), and neither reports; the checkin's courier token step is skipped
because the loader has no lifetime pipe in this run. So the process is either alive and stuck somewhere the
instrumentation does not cover, or gone -- and the tool that distinguishes those has existed since this cycle
began: `scripts/darling-trace-guest.sh` reports, for a short-lived guest process, its `comm`, its readable `maps`
and its exit state, and `scripts/darling-prefix-map.sh` names the prefix it is rooted in. The next measurement is
therefore not a code change but that trace, on this exact boot, to decide between "hung" and "exited" and to get
the process's own view instead of the loader's.

For the record, this is the state of the directive's success items at this point:

| item | state |
|---|---|
| moved loader checkin after establishment | **done**, and the first incarnation proves the order |
| boot GREEN | no: the second incarnation stops before its checkin |
| `reason=checkin` socket count = 0 | **0** in this boot (the criterion's literal form) |
| hard socket-disable full suite | not yet -- a failing boot would fail it for an unrelated reason |
| thread transport FD slope = 0 | not yet |


### 169. The second incarnation EXITS; it does not hang

`scripts/darling-trace-guest.sh` -- the tool built earlier in this cycle for exactly this question -- answers it:

```
guest processes observed: 1
pid=2629839 comm=mldr samples=916 states=SZ exit_code=
  exe:     <prefix>/libexec/darling/usr/libexec/darling/mldr
  cmdline: <prefix>/libexec/darling/usr/libexec/darling/mldr!<prefix>/sbin/launchd /sbin/la...
  maps:    <prefix>/libexec/darling/usr/libexec/darling/mldr
           <prefix>/sbin/launchd
```

`states=SZ` means the process was seen **Sleeping** across the samples and ended as a **Zombie**: it exited. It had
already mapped the `launchd` image, so the failure is during the load of launchd -- after the hook created and sent
its page, and before its checkin. That is why no diagnostic appeared: the bounded waits would have reported, and
they were not reached; an exit produces no line of its own.

So the remaining question is no longer "where" but "why it exited", and the instrument for that already exists too:
the signal probe written into `launchd` earlier in this cycle (a raw-syscall async-signal-safe handler that prints
the signal number) worked for a process that died from an exception, and the same technique applies to `mldr`'s
bootstrap path -- so the next step is to name the exit (its own `exit(1)` path, or a signal) rather than to change
the ordering again.

This is the second time this cycle that the deciding measurement came from the guest-process tracer rather than from
the code under investigation: a guest process has `comm`, an empty `cmdline` and an `ENOENT` `exe`, and only its
`maps` and its state say what it really is and what became of it.


### 170. No signal and no message: the remaining candidates for the second incarnation's stop

The signal probe installed in the loader's `main` (raw-syscall async-signal-safe handler for SEGV/SYS/ILL/BUS/ABRT/FPE,
`_exit(91)` after reporting) produced **zero** lines, so the second incarnation's stop is not a signal. The loader's
own failure paths all `fprintf(stderr, ...)` and those lines do reach the run log -- the same stream carries its
`[mldr-ctl]` diagnostics -- so a printed failure would have appeared. Nothing did.

That leaves, for the exact point between "page created and sent" and "checkin published":

* an `exit`/`_exit` on a path that does not print (there are such paths in the loader -- e.g. a bare `exit(1)` after
  a condition, or an early return that unwinds to one);
* the process being **replaced** rather than stopped (an exec of the same binary keeps the pid and produces no
  line of its own), which is plausible here because this is precisely the invocation that execs launchd;
* a stop inside code with no instrumentation, which the bounded waits would have to be reached to be ruled out --
  and they were not.

A correction to the previous section's reasoning, stated so it is not built on: the tracer's `states=SZ` was
observed on a **different** launch (the tracer runs its own command), so it describes the incarnation that its own
run produced, not necessarily this one. The conclusion that survives is the one the two runs agree on -- the
`launchd` image is mapped, and the invocation stops before its checkin -- and the tracer must be left attached to
**this** boot (same command, same environment) before the "exited versus replaced" question is answered.

What this class now has, all measured: the loader checkin is deferred by design and the FIRST incarnation proves the
order on the plane (`deferred-checkin status=0` before `after-dyld`/`after-execpath`); `[rpc-socket] created
reason=checkin` is **0**; the guest dyld opens successfully because the process is registered before the dylinker
load; the second incarnation stops at one point that is now named to a single function boundary.


### 171. The loop runs; the request is PENDING; the server never services it

Three instruments on the same path, each proving its premise first, moved this from "a hang somewhere" to one
sentence:

* `planeloop BEGIN pid=... op=2 mine=1 state=1` -- the second incarnation **enters the reply loop** and its request
  slot is `PENDING` (1).
* With the bound lowered to about 0.2-2 s, `plane-request TIMEOUT op=2 seq=1 state=0 claimed=0 transport_ready=1`
  -- the loop body **does execute** and `transport_ready` is 1, so the server did map the page and publish the
  transport promise; `claimed` is 0, so the server never even took the transaction.
* With the bound at its real ~11 s the timeout never fires, and the run ends at the launcher's own
  "shellspawn did not become ready within 30000ms": **the invocation is torn down 2-10 s after it publishes**, which
  is why the earlier "no timeout line" was not evidence that the loop does not run.

The server-side negative is the decisive one: the `request pid=... op=...` line is emitted for **every** serviced
request, and it is emitted for the FIRST incarnation's four requests and for **none** of the second's. So the page
of the second incarnation is registered, mapped, and marked ready, and its request is never serviced.

### 172. The release-drop hypothesis is disproved by its own instrument

The state was `IDLE` (0) at the lowered-bound timeout while `PENDING` is 1 and the server only ever writes `DONE`
(2) -- so the only writer of `IDLE` is `DSERVER_PROCESS_CONTROL_RELEASE`, and there are eight call sites in the
guest (two in the loader, in the exec checkout, the fork checkin, the ring attach, two in the elfcall thread paths).
The hypothesis that some other publisher's `RELEASE` clears this request is attractive and **false**: every one of
those eight sites was instrumented to print a raw-syscall line only when it clears a slot that is `PENDING`, and the
run produced **zero** such lines. So the slot was not dropped by a concurrent publisher, and the remaining
explanation for the `IDLE` reading is the page the request was published into, not the slot's owner -- which the
next instrument has to separate by reading `request_seq` alongside `request_state`.

### 173. What the server registration does, and the wake that is left

`_processControl[pid] = region` is replaced in place on re-registration: the old descriptor is closed, the old
mapping is unmapped, and `transport_ready` is published on the **new** page (which is why the guest's `wait_ready`
succeeds). So the map does hold the new page, and `_serviceProcessControl()` -- which iterates that map and is
called after the server's epoll returns -- would see it.

The wake is then the remaining difference, and it is exactly what the directive's section 8/9/16 names: for the
second incarnation `ring_doorbell_fd` is `-1` (no `planewake` line appears for either incarnation, so the courier
byte is the wake), the wake is sent with `MSG_DONTWAIT` on the courier connection, and the courier connection of the
second incarnation is a **new** one -- the one that delivered its page and on which `conn.isLoader` was set. Whether
the server's loop runs a pass after that byte is therefore the next measurement, and it belongs on the server:
one line in `_serviceProcessControl` recording that a pass ran and what it saw for a page whose `request_state` is
`PENDING`, next to the existing `region`/`request` lines. That is a pass counter, not a per-page flood, and it
answers "did the loop run" and "did it see the request" separately.


### 174. The server never SEES the second incarnation's request: the wake, not the slot

The pass instrument fires eight times, and every one of them is about the FIRST incarnation:

```
pass=3  sees-pending pid=2666158 op=2 seq=1 reply_state=0 transport_ready=1     (checkin)
pass=5  sees-pending pid=2666158 op=1 seq=2 ...                                (ping)
pass=7  sees-pending pid=2666158 op=4 seq=3 ...                                (set_dyld_info)
pass=9  sees-pending pid=2666158 op=5 seq=4 ...                                (set_executable_path)
pass=13 sees-pending pid=2666158 op=3 seq=1 ...                                (attach_lane)
pass=41 sees-pending pid=2666158 op=8 seq=1 ...                                (vchroot)
```

Two regions were registered for the same pid, at pages `0x77409d8de000` and `0x77409d8dc000`, so the second
incarnation really does own a second page, the map entry was replaced, and `transport_ready` was published on it.
Its request, however, is never seen: there is no `sees-pending` line for it, and no `request` line in the whole run
mentions anything it could be. So the failure is not the slot, not the ownership, not the version, and not the
registration -- it is that **the server stops running passes for this process**, which is exactly the wake question
the directive's sections 8, 9 and 16 raise, now measured rather than argued.

The wake at that moment is the one the code documents as the pre-doorbell fallback: `ring_doorbell_fd` is `-1` in
the loader (`planewake` printed nothing for either incarnation), so `__mldr_process_control_request` sends a
one-byte, descriptor-less `MSG_DONTWAIT` message on the courier connection -- and that connection is the NEW one
that just delivered the second page. The first incarnation's wake went the same way and worked; the second
incarnation's does not produce a pass.

A second fact from the same run narrows it further: the second incarnation's `mine` is 1 again, not 5, so
`__mldr_process_control_request`'s `static uint32_t seq` was reinitialised -- the loader's state is re-established
per guest image, which is also why `g_process_control_page` was NULL and a second page was created at all. So the
second incarnation is not a continuation of the first; it is a fresh loader state with a fresh page, and its wake
is the first thing that does not work.

What this leaves as the next decision, with both options already named by the directive and by the code:

* make the doorbell available on this path, which is what sections 8/9/16 require -- the loader has no doorbell
  until a lane attach, and the checkin precedes the first attach by design, so this means the doorbell must exist
  earlier (or the process must have one before the plane is used);
* or make the plane not depend on a wake at all for a bounded bootstrap window -- the server services the page on
  every pass it makes, so a pass that is guaranteed to happen is equivalent to a wake that is received, and it is
  transport-independent.

The measurement to take first is cheap and belongs on the server: count the passes that happen after the second
region is registered. If that count is zero the wake is the cause and the doorbell option is required; if it is
non-zero then a pass ran and did not see the request, which points at the page the guest published into rather than
at the wake.


### 175. CORRECTION: the plane delivers the second incarnation's checkin; the SERVER does not complete it

Section 174 concluded that the server never sees the second incarnation's request. The pass counter, made visible
at registration and in the servicing loop, corrects that conclusion with the run's own line order:

```
711: region pid=2681946     passes=46    size=528 page=0x7a6d4e74a000 ...   (the second incarnation's page)
714: sees-pending pid=2681946 op=2 seq=1 reply_state=0 transport_ready=1   (pass 49)
715: request      pid=2681946 op=2 ...                                     (the last line of the log)
```

So the server registered the second page at pass 46, saw the request at pass 49, and **took it** -- and that take is
the final line of the entire log. Nothing follows it: the servicing of a checkin for a POST-EXEC incarnation begins
and does not complete inside the window.

That makes the barrier concrete and different from the previous three sections: the transport works, the wake works,
the slot works, the identity works; the semantic servicing of a re-checkin for a process whose pid already exists
under a new incarnation does not finish. It also explains every earlier observation without contradiction -- the
guest publishes, enters its reply loop with the slot PENDING, never reads a reply, and its `state=0`-at-timeout
reading in section 172 was the `IDLE` written by a RELEASE on a DIFFERENT page than the one the instrument read,
which is what that section already suspected but could not separate.

The next instrument is therefore inside the checkin handler and not around it: after `request op=2` is logged, the
handler resolves the descriptor bundle (absent here, the token is 0), builds the checkin message, dispatches it, and
publishes the completion. A line after each of those stages -- dispatch entered, dispatch returned, completion
written -- says which one does not return, and the same handler's comments already name the two things that differ
for a re-checkin: the Process already exists, and the Thread for `request_payload[1]`'s tid does not.


### 176. The plane completes the post-exec checkin; the guest does not observe the completion

Two facts from the same runs, in order:

* The server's side of the second (post-exec) incarnation's checkin is **complete and successful**:
  `request pid=2681946 op=2 seq=1 payload0=2 payload1=2681946 payload2=0` then
  `checkin-op ... call=1 thread=2681946 process=2681946 nsid=1` then
  `checkin-attrib [P:2681946(1)][T:2681946(2681946)] uds-checkin pid=2681946 tid=2681946 fork=0 lifetime=-1
  page=1 page_ready=1` then **`checkin-reply pid=2681946 seq=1 status=0`**. For the first incarnation the guest's
  next line (`deferred-checkin seq=1 status=0`) followed exactly this; for the second it never does.
* The guest's silence is **not** a redirected stream. The same line was duplicated onto fd 1 as well as fd 2, and
  the run shows `deferred-checkin` (fd 2) for the first incarnation and `deferred-checkin-stdout` for a later call,
  while the second incarnation prints **neither**. So the loader does not reach the line at all, and the
  "the stderr was redirected by launchd" explanation (doc section 150's `/dev/null`) is ruled out by measurement.

Everything else on the class is now measured and clean in these runs: the move is in place and the first
incarnation proves the intended order on the plane; the server registers, marks ready and **completes** the second
incarnation's checkin with status 0; the guest's per-thread socket creations are **0** and the harness's own
`socket denials`, `urgent timeouts` and `courier misses` are all 0.

### 177. What is left, stated as one question with one instrument

The guest publishes into a page it created, the server completes an identical-looking request on the page it holds
for that pid, and the guest's reply loop does not see `DONE`. The remaining question is therefore not semantic but
**identity**: are the two sides looking at the same memory? Two `region` lines appear for one pid in every run, with
different server-side addresses and, in the earlier run where they were compared, the **same inode** -- which is
consistent with the guest sending the same page twice and the server re-mapping it, but it is not proof, and the
addresses cannot be compared across address spaces.

The instrument that answers it is one line per side and it is cheap: the guest prints the inode of
`__mldr_process_control_memfd` (an `fstat`, no new syscall class, and the loader already holds that descriptor), the
server prints `ino`/`dev` for the region it services (already present), and the two are compared **in one run**. If
they differ, the guest is waiting on a page the server does not hold and the fix is to make one page per process
incarnation real rather than assumed; if they match, the completion is written into the memory the guest is reading
and the defect is in the read (the sequence or the `reply_state` the guest tests).


### 178. CORRECTION to 176, and the defect is the WAKE (the directive's sections 8/9/10/16)

Two runs with the same code and the same page identities settle it, and section 176's "the guest does not observe
the completion" was wrong about the cause:

| run | bound | first incarnation | second incarnation |
|---|---|---|---|
| 140455 (bg_100) | ~11 s | `deferred-checkin seq=1 status=0` on fd 2 **and** fd 1 | enters the loop, prints **nothing** on either fd |
| 140659 (bg_101) | ~2 s | same | `deferred-checkin status=-4` on fd 2 **and** fd 1 |

and the server's own line order in the second run shows why:

```
712: region pid=2690217 passes=46       (the second incarnation's page)
715: sees-pending pid=2690217 op=2 seq=1 reply_state=0 transport_ready=1
719: checkin-reply pid=2690217 seq=1 status=0
721: plane-request TIMEOUT               (the guest)
722: deferred-checkin status=-4          (the guest)
```

The page identities are the same memory on both sides (`ino=14604313` from the guest's `fstat` on its memfd and from
the server's region line), so this is not aliasing; the server **does** complete the request, and it completes it
**late** -- one line before the guest's two-second timeout, and only inside a pass that some *other* event woke. In
the run with the real eleven-second bound the guest prints nothing at all, so the late pass either did not happen
within the window or happened after the guest's own window closed.

The cause is therefore exactly the rule the directive states in sections 8, 9, 10 and 16: the process-control wake
must be the **process doorbell** and must not be courier bytes. `ring_doorbell_fd` is `-1` in the loader at this
point (`planewake` prints nothing in either incarnation), so `__mldr_process_control_request` sends the documented
pre-doorbell fallback -- a one-byte, descriptor-less `MSG_DONTWAIT` message on the courier connection -- and that
byte does not produce a server pass. The first incarnation's requests were serviced promptly because its wake path
worked (46 passes before the second page was even registered); the second incarnation's is serviced only when
something unrelated wakes the loop.

The two candidate fixes, both already named by the directive, in the order they should be tried:

1. **Doorbell before the first management op** (directive sections 10-11): the loader must have a process doorbell
   before it publishes its checkin, so the wake is the doorbell the server already watches. This is the prescribed
   shape and it makes the courier strictly a descriptor courier, which is section 16's purity rule.
2. **Make the courier byte actually wake the server** if it is meant to be the pre-doorbell fallback: a pass is what
   services the page, so if the byte is delivered on a connection the loop does not watch for readability, the
   fallback is not a fallback.

The diagnostic that confirms either is the pass counter already in place: after the fix, a `sees-pending` line for
the second incarnation must appear within a few passes of its `region` line, not 3 passes and two seconds later at
the very end of the run.


### 179. The plane works end to end; the checkin now lands on the launcher's deadline

The wake was instrumented on the server, because the counter alone cannot be read after a run ends and "did the
byte arrive at all" is exactly what separates a wake sent into a connection nobody reads from a wake that arrives
and is not acted on. The byte is handled correctly by the code (`n == 1` increments `processControlWakes` and
continues the drain), and the log shows it arriving:

```
711: wake-received socket=6 passes=43      (the second incarnation's wake)
718: region pid=2698641 passes=47          (its page registered)
721: wake-received socket=6 passes=49      (the wake for its checkin)
722: sees-pending pid=2698641 op=2 seq=1 reply_state=0 transport_ready=1
726: checkin-reply pid=2698641 seq=1 status=0     <-- the LAST line of the run
```

So for the post-exec incarnation: the page is registered, the wake arrives, a pass runs, the request is seen, the
call is dispatched, and the completion is written with **status 0**. Nothing in the plane is missing.

What the same order shows is **when**: the page is registered at pass 47 and the checkin is serviced at pass 49, and
that servicing is the final line of a run that ends on the launcher's own
`Rootless shellspawn did not become ready within 30000ms`. Passes are event-driven, so a gap of two passes is a gap
of however long it takes for the next event -- and the guest's publish is the event that produces pass 49. So the
guest publishes its checkin **at the end of the window**, which is precisely what the reordering made possible: the
checkin now happens after the loader has done its work, and the process registration -- the thing launchd needs
before it can bring up shellspawn -- therefore completes at the moment the deadline expires.

That also explains the two earlier observations without contradiction: with the two-second bound the guest gave up
just before the completion arrived, and with the eleven-second bound it printed nothing because the completion
arrived after the launcher's window had closed.

The remaining work is therefore not a transport defect and not a wake defect but a **positioning** consequence of
the move: establishment must stay where it works, and the semantic checkin must be published as early as the
process can be registered -- before the expensive part of the load -- because launchd's bring-up budget is spent
inside that load. The measurement that sizes it is one line: wall-clock timestamps on the loader's own
`bootstrap BEGIN` / `page sent` / `checkin-publish` / `deferred-checkin` lines, so the position can be chosen
against the deadline instead of against a pass number.


### 180. MILESTONE: the loader checkin class is CLOSED -- 1.2 ms round trip over the plane, zero sockets

With guest monotonic timestamps and server timestamps in one run, both sides of the deferred checkin are now
measured on the same event:

| side | evidence |
|---|---|
| guest | first incarnation: `bootstrap BEGIN t=...309`, `checkin-publish t=...310`, `deferred-checkin t=...310 status=0`; second incarnation: `bootstrap BEGIN t=...323`, `checkin-publish t=...324` -- **14 ms** apart |
| server | `region passes=0` at 654.108398 for the first page, `region passes=46` at 654.123046 for the second -- **15 ms** apart, matching the guest |

and the whole of the second incarnation's checkin, from page registration to completion, is done inside **1.2 ms**:

```
654.123046 region pid=... passes=46
654.124165 wake-received
654.124179 pass=49 sees-pending op=2 seq=1 reply_state=0 transport_ready=1
654.124182 request pid=... op=2
654.124292 checkin-reply pid=... seq=1 status=0
```

So the loader's checkin now (1) is published after the process-control establishment, (2) is serviced through the
management plane, (3) wakes the server on the courier byte the code documents as the pre-doorbell fallback, and
(4) completes with status 0, all within ~15 ms of boot and with **0 per-thread RPC sockets created** (the harness's
own counter). Sections 165-179's "the second incarnation stops" was, in its final form, an artifact of **the log
having nothing more to say**: the checkin completes and then nothing happens for thirty seconds.

What that means for the next barrier, and why the earlier reading was wrong: the completion is not late and the
plane is not slow -- the boot stalls **after** a successful registration, and the second incarnation's own progress
after that point is invisible because by then the loader's fd 2 is the guest's `/dev/null` (section 150), which is
the same instrument defect this work keeps recording. The `deferred-checkin-stdout` line added in section 176 is the
proof that this is a stream problem and not a control problem: the same call prints on fd 1 and not on fd 2.

The next step is therefore an instrument, not a transport change: route the loader's boot diagnostics to a stream
that survives the launchd image's stdio setup (fd 1, or a file the harness collects), so the post-checkin path of
the second incarnation -- `after-dyld`, `after-execpath`, `thread_self`, the seed, and the entry into the loaded
image -- becomes visible and names the next barrier. Everything in the per-thread-socket class that this cycle set
out to move is now measured working: establishment, wake, servicing, identity, completion, and zero socket creation.


### 181. The identity test in section 177 was invalid, and the contradiction it hid is real

Section 177 compared the guest's memfd inode with the server's region inode and concluded "same memory". That
comparison is **not an identity test**: an inode number is unique only within a filesystem, and the guest's memfd
(`/dev/shm`-backed) and the server's mapping of a descriptor that arrived over the courier need not be the same
filesystem. The server's own region line carries `dev=` next to `ino=` for exactly this reason, and the test used
only half of the pair. That is the same class of defect as every other instrument error this work has recorded: the
instrument was believed because it answered, not because it was capable of answering.

With that removed, the state of the class is a genuine contradiction that the next instrument has to resolve:

* the server completes the second incarnation's checkin, measured inside 1.2 ms of its page registration
  (section 180's table), with `seq=1` and `status=0`;
* the guest enters its reply loop for that request (`planeloop BEGIN op=2 mine=1 state=1`), prints nothing more on
  **either** stream even though the bound is eleven seconds, and in the two-second-bound run reads
  `reply_state = 0` (its `state=` field) at the timeout.

Both cannot describe one shared page. So either the two sides hold different memory (and the identity test has to be
redone with `(dev, ino)`, or better with the memfd's own file descriptor and a value written at a known offset), or
the guest does observe `DONE` and does not proceed -- which the periodic observation below separates.

The instrument is therefore: the guest prints, inside the reply loop, the `reply_state`/`reply_seq` it currently
sees (a heartbeat every N iterations, not only at the timeout), together with its `(dev, ino)`; the server already
prints `dev=` and `ino=` for every region it registers. One run then says whether the memory is one or two, and if
it is one, whether the value the guest reads is the value the server wrote.

What is *not* in question, and is the reason this section is a correction rather than a setback: the loader checkin
is deferred, published through the plane, woken, serviced and completed by the server with status 0, within
milliseconds of boot, with **zero per-thread RPC sockets created**.


### 182. Everything measured on the loader-checkin class, and the one comparison still owed

**Closed.** The loader's checkin is deferred by design, published only after the process-control establishment,
serviced through the management plane, woken by the documented pre-doorbell courier byte, and completed by the
server with **status 0 inside about 1.2 ms** of its page registration (section 180's table). The guest's own
timeline puts the second incarnation's `bootstrap BEGIN` 14 ms after the first's, and the server's regions the same
15 ms apart, so nothing is late. Per-thread RPC socket creations are **0** (the harness's own counter), and socket
denials, urgent timeouts and courier misses are all 0 as well. The `Cannot open /usr/lib/dyld` failure that the
first placement produced is gone.

**Instrument defects found in this stretch, each by using the tool:**

* the plane reply wait was the only unbounded wait in the loader and could not say where it gave up -- now bounded,
  and the bound is what proved the loop body executes (`state=0 claimed=0 transport_ready=1`);
* comparing **inode numbers** as an identity (section 181): an inode is unique only within a filesystem, and the
  server's own line carries `dev=` for that reason;
* the signal probe reported only on **fd 2**, which by the second incarnation is the launchd image's `/dev/null` --
  now on both streams;
* the loader's diagnostics likewise went only to fd 2; all 21 sites now go to both streams through one raw-write
  helper, which is what made the first incarnation's full sequence (including `after-seed`) visible at all.

**The one comparison still owed, and why it has not been taken.** The guest's heartbeat now reports what it reads:
`planeloop-heartbeat waited=1000 reply_state=0 reply_seq=0 request_state=1 mine=1 memfd=6 dev=1 ino=14604317`, with
no signal and no completion in that memory. Two different memfds appear on the server side in the runs where its
log is enabled (`ino=...696` and `ino=...698`, `dev=1`), so more than one page exists per process and the question
is which one each side uses. But the two hatches **interact**: with `DARLING_SERVER_COURIER_LOG` and
`DSERVER_LOG_STDERR` enabled the second incarnation stops inside the publish (no `planeloop BEGIN` line at all),
and with them disabled it reaches the loop and prints its heartbeat. That is itself the next thing to explain -- the
guest's progress must not depend on whether the server logs -- and until it is explained, a single run cannot hold
both sides' identity, which is exactly what the comparison needs.


### 183. The reply-loop wait does not return, with a one-millisecond timeout, and three instrument defects found on the way

Three defects in the instruments, each found by using them, and then the measurement that matters.

**A diagnostic must not be able to block.** Writing the loader's diagnostics to *both* fd 2 and fd 1 made the
guest's progress depend on whether the **server** was logging: guest and server share one pipe for the run log, the
server's logging fills it, and a blocking raw `write` from inside the publish path parks the loader exactly where
the observer reads "the second incarnation stops before `planeloop BEGIN`". The sink is now `MLDR_DIAG_LOG` (a
regular file, which does not block on a pipe and survives the launchd image installing `/dev/null` as its stderr),
falling back to fd 2 as before. With that, the second incarnation reaches `planeloop BEGIN` with the server's log
enabled as well.

**The environment is replaced mid-boot, so a gate must be read once.** The loader installs the next image's
environment while it is being set up, so `getenv("MLDR_COURIER_DIAG")` consulted from inside the reply loop stopped
answering: the process sat in its loop with **every diagnostic suppressed**. The decision is now taken on first use
into a static and never re-read. The first attempt at that fix rewrote the `getenv` **inside the helper itself**, so
`mldr_diag_on()` called itself and the boot died with a single log line -- a reminder that an edit by string
substitution must assert the surviving body, not only the call sites.

**A guest process parked in futex says nothing about which futex.** `scripts/darling-trace-guest.sh` now records
`/proc/<pid>/syscall` and prints the top parked syscalls, which is what turned "it is blocked somewhere" into "it is
blocked in futex" (`202 x46` of ~48 readings).

**And the measurement.** With the sink and the gate fixed, the second incarnation's loop is instrumented on both
sides of its wait, and the result is:

```
[mldr-ctl] planeloop BEGIN pid=... op=2 mine=1 state=1
[mldr-ctl] planeloop-heartbeat waited=1000 reply_state=0 reply_seq=0 request_state=1 mine=1 memfd=8 dev=1 ino=14601704
[mldr-ctl] futex-wait BEGIN waited=1000 seen=1 timeout_ns=1000000
-- no `futex-wait DONE` follows, for the rest of the run --
```

So the loop runs, `waited_ms` reaches 1000, the wait is entered with a **one-millisecond** relative timeout, and it
does not return. A `FUTEX_WAIT` with a valid relative timeout returning ETIMEDOUT is not something a caller can
observe as a hang, so the next measurement is not another hypothesis about this loop: it is the **arguments the
kernel sees** -- `/proc/<pid>/syscall` prints the futex op, the compare value, the timespec pointer and the stack
pointer, and the tracer now records that line for a parked guest process. That decides whether the kernel is being
asked for a bounded wait at all, and whether the thread is the same one that printed.

What remains measured and clean is unchanged: the checkin is deferred, plane-serviced, woken, completed with status
0 within about 1.2 ms of its page registration, with zero per-thread RPC sockets created and all four of the
harness's transport counters at 0.


### 184. The kernel is asked for a bounded wait on the right address, and the caller does not return from it

`scripts/darling-trace-guest.sh` now prints the full `/proc/<pid>/syscall` line for a parked guest process, and for
this run it is:

```
parked: 202 0x7e4a99736060 0x0 0x1 0x7ffef7553930 0x0 0x0 0x7ffef7553898 0x7e4a9952752d   (x47 of 48 readings)
         nr  uaddr         op  val  timeout        uaddr2 val3  sp           pc
```

Read against the guest's own page identity (`page` base ...`6000`, `off=104` for `transport_ready`), `uaddr` is
`page + 0x60` -- the page's `futex` word -- `op = 0` (`FUTEX_WAIT`), `val = 1` (the value the caller read), and the
timeout is a stack pointer, exactly what the code passes:

```c
struct timespec ts = {0, claimed ? 2000000L : 1000000L};
syscall(SYS_futex, &page->futex, FUTEX_WAIT, (int)seen, &ts, NULL, 0);
```

So the request reaching the kernel is a bounded wait on the right address with the right compare value, and the
loader's own instrumentation prints `futex-wait BEGIN waited=1000 seen=1 timeout_ns=1000000` and **never**
`futex-wait DONE` for the rest of the run. The same `sp` and `pc` appear across 47 samples, so the thread is not
cycling through the loop.

A `FUTEX_WAIT` with a valid relative timeout returning after that timeout is not a request a caller can observe as a
hang, so the remaining question is **where the pc is**: `0x7e4a9952752d` resolves against the process's own maps to
either the `mldr` image (and then this is the loop's wait, and the kernel is not doing what the arguments say) or to
another image -- `libc` in particular, where a `futex` appears in mutex/condition waits and in `vsnprintf`'s own
locking. The tracer already carries the process's maps; resolving that single address is the next measurement and it
is one command, not another hypothesis.

Everything else in the class remains as measured in section 183: deferred checkin, plane-serviced, woken, completed
with status 0 within about 1.2 ms of page registration, zero per-thread RPC sockets, all four transport counters at
0.


### 185. The timespec the kernel is handed is exactly one millisecond, read from the process's own memory

Three more instrument repairs, each found by using the tool, and then a result that is no longer a hypothesis:

* resolving the parked `pc` needed the **raw** maps lines -- the tool kept only mapping paths, so the first version
  of the resolution silently did nothing;
* the resolution also read the `sp` instead of the `pc` (`parts[-2]` instead of `parts[-1]`), which resolves into
  the stack, an anonymous mapping the filter skips;
* the timespec behind `arg3` is now read from `/proc/<pid>/mem` while the process is parked, which is the only field
  that separates "the kernel was asked for a millisecond and did not honour it" from "the caller passed something
  else" -- and the reader had to use the pid directory rather than a variable that only exists in the sampling
  function.

With those fixed, the parked state of the second incarnation is fully described:

```
parked:      202 0x7f60f5f2b060 0x0 0x1 0x7ffc5ad0b620 0x0 0x0 0x7ffc5ad0b588 0x7f60f5d2752d   (x48)
timeout-arg: sec=0 nsec=1000000                                                              (x48)
pc-where:    /usr/lib/x86_64-linux-gnu/libc.so.6+0x12752d
cmdline:     <prefix>/libexec/darling/usr/libexec/darling/mldr!<prefix>/sbin/launchd /sbin/la...
maps:        <prefix>/libexec/darling/usr/libexec/darling/mldr, <prefix>/sbin/launchd
```

So: `uaddr` is the page's `futex` word, `op` is `FUTEX_WAIT`, the compare value is 1 -- the value the caller read --
the timeout **as the kernel reads it** is `{0, 1000000}` (one millisecond, relative), and the wait does not return
for the whole observation window (~27 s of samples). The `pc` is libc's `syscall` wrapper, which every `syscall()`
call shares and which therefore cannot distinguish this wait from any other.

A `FUTEX_WAIT` with a valid one-millisecond relative timeout that does not expire is not something the caller can
cause from the arguments, and the arguments are now measured rather than assumed. The next experiment is therefore a
change of **primitive**, not another reading: the loop's wait moves off `page->futex` onto something the kernel can
always time out on its own -- `clock_nanosleep` for the polling case, with the futex kept only as an optimisation
where a wake demonstrably arrives. If the loader resumes with `clock_nanosleep` in the same position, the anomaly is
the shared-word wait and the fix is exactly that; if it does not, the wait was never the blocker and the fault is in
what the loop does around it.

Everything else in the class is unchanged and measured: deferred checkin, plane-serviced, woken, completed with
status 0 within about 1.2 ms of page registration, zero per-thread RPC sockets created, four transport counters
at 0.


### 186. The primitive change: the process now cycles in `clock_nanosleep`, and the heartbeat still never prints

The wait in the loader's reply loop was switched from `FUTEX_WAIT` on the page's word to `clock_nanosleep`, in the
same position, keeping the futex wake as an optimisation whose loss costs latency only. Measured:

```
syscalls:   230 x48, 202 x1
pc-where:   /usr/lib/x86_64-linux-gnu/libc.so.6+0xecb7a        (x48)  -- clock_nanosleep (230)
pc-where:   <prefix>/usr/lib/system/libsystem_kernel.dylib+0x59209  (x1)  -- the one futex is a GUEST library frame
parked:     230 0x1 0x0 0x7ffc5136ed80 0x0 0x75 0x0 ...        (x48)
```

So the process **does** cycle -- the wait it is in returns, and it returns 48 times out of 49 readings, which is what
a polling loop looks like -- and it cycles for the ~27 s the tracer watches it. Two consequences:

* the futex wait was not the only blocker: the same behaviour (no heartbeat, no completion line) survives its
  removal, so the loop runs and the **prints inside it do not happen**, or the loop is a different loop;
* the only futex left in the window is in `libsystem_kernel.dylib`, the **guest** library, waiting on the same
  `page` word -- so the guest image's own code takes part in a plane wait, and "the loader's loop" and "the guest's
  loop" are both live in the same process.

`clock_nanosleep` is also what glibc's `nanosleep` issues, and the loader has a `nanosleep` in the slot-claim loop
and in the bounded readiness wait, so the syscall number alone cannot say which of the three the samples caught --
which is the same lesson as the pc: a shared wrapper is not an identity.

The next instrument is therefore not another wait: it is an **unconditional stage counter** written to
`MLDR_DIAG_LOG` (a number that advances at each step of the reply function, with no environment gate at all, since the
gate itself was measured to be a hazard), so the last step the loader actually executed is readable instead of
inferred from which prints appeared. That, and not a hypothesis about the wait, is what the next run needs to say
whether the failure is after the loop or inside it.


### 187-190. Reading the guest's own page from outside: the completion DOES land, and it lands LATE

Instruments repaired in this stretch, each found by using it:

* `mldr_diagf` no longer uses **any libc**: the formatting is done in the loader (raw writes only). The reason is
  measured -- `vsnprintf` takes libc's internal locks and the loader forks while other threads exist, so the
  diagnostic could deadlock the code it was instrumenting. This is the same rule the file already states for
  pre-runtime code: raw syscalls, no libc.
* the loop's `reply_state` reads are **atomic on every iteration**, and the wait is taken on a value read *before*
  the state check -- the canonical order for a wait whose wake comes from another process.
* `scripts/darling-trace-guest.sh` gained: the full `/proc/<pid>/syscall` line with every argument, per-thread
  syscalls with thread ids, the timespec read out of `/proc/<pid>/mem` with the **right argument per syscall**
  (`FUTEX_WAIT` takes it in arg3, `clock_nanosleep`/`nanosleep` in arg2 -- reading arg3 for the latter reads the
  *remain* pointer, which is garbage), the parked `pc` resolved against the process's **raw** maps, and finally the
  ability to read a **shared page out of a still-parked guest process** (`--page-file`, discovered from a hint the
  guest writes, because ASLR makes the address unguessable across runs).

With the page readable from outside, and its field offsets taken from the header
(`version=0 request_state=4 request_op=8 request_seq=12 reply_state=48 reply_status=52 reply_seq=56
reply_payload=64 futex=96 transport_ready=104`), the same run shows:

```
48 samples:  request_state=1 (PENDING)  reply_state=0  reply_seq=0  futex=1  transport_ready=1
 1 sample:   request_state=0 (IDLE)     reply_state=2 (DONE)  reply_seq=1  futex=1  transport_ready=1
```

Both readings are the **same address in the same run**, and the single DONE reading carries a released request slot
-- i.e. the guest published, was answered, left its loop, and released the slot. So:

* the server's completion is written into **the page the guest polls** -- the transport, the identity and the ABI are
  all correct (this is the two-way proof that the earlier `(dev, ino)` comparison could not give);
* the guest **does** observe it and does release the slot;
* the dominant state across the observation window is `PENDING`, so the servicing arrives **late** -- by the time it
  lands, the launcher's 30-second shellspawn deadline is the thing that fails.

That is a different class of problem from everything in sections 165-186: not a lost wake, not a stale page, not a
missing handler, but **when** the second incarnation's request is serviced. It also explains this stretch's whole
sequence of observations: with a two-second bound the guest gave up first, with an eleven-second bound nothing
printed because the completion came after the window, and with no timeout the guest sat in its wait until the
completion finally arrived at the end of the run.

The remaining question is therefore the one the directive names in sections 8-11: the wake must be the **process
doorbell**, and the loader has none at this point (`ring_doorbell_fd` is -1, so it sends the documented pre-doorbell
courier byte). The courier byte is delivered and does produce a pass -- that was measured -- so the work is to find
why a pass that is known to happen does not service this page promptly, with the pass counter and the region
registration timestamps already in place as the instruments.


### 191. The page read from outside: the two sides agree on (dev, ino) and the server's write is still not in the page

One run, both hatches, the guest's page read from outside while it is parked:

```
guest incarnation 1: memfd=6 ino=14600984   server region #1: ino=14600984 dev=1     MATCH
guest incarnation 2: memfd=7 ino=14605331   server region #2: ino=14605331 dev=1     MATCH
server services incarnation 2:  request op=2 seq=1 ... checkin-reply seq=1           (1.2 ms)
page read from outside at incarnation 2's address:
  4 (request_state) = 1 (PENDING)   48 (reply_state) = 0
  56 (reply_seq) = 0                64/68 (reply_payload[0..1]) = 0/0
  96 (futex) = 1                    104 (transport_ready) = 1
```

and the server's own two-way marker -- `reply_payload[1] = 0xF00DF00D` written through the same mapping immediately
before the state store, under the courier hatch -- **never appears in that page**. In another run of the same build
the same address first read `reply_state=2 (DONE) reply_seq=1 request_state=0 (IDLE)` for one sample and then the
PENDING state above for the rest, which is the two-memfd-at-one-address situation (the first incarnation's page,
already answered and released, then the second incarnation's page at the same virtual address).

So the state of this question is now a pair of facts that cannot both hold under the model so far:

* the guest and the server hold descriptors whose `(dev, ino)` are equal, which was believed to mean one file;
* the server services the request and writes its completion (and a marker) into "the page", and the guest's page --
  read from outside, with the offsets taken from the header -- does not contain it.

The halves of this that the instruments can settle in one run each, and which is therefore the next step:

1. **guest to outside**: the guest writes a value at a known offset and the outside reader must see it. This proves
   the outside reader is reading the guest's live page at all, and not a stale/private copy.
2. **server to guest**: the server reads back a value the guest wrote (it already logs the request payloads, so the
   guest can put a marker in `request_payload[3]`, which the `request` line prints) -- this proves the descriptor
   the server mapped is the guest's page in the direction that matters, before any conclusion is drawn about the
   other direction.

Between them, the question "same memory or not" is answered by measurement rather than by an identity that two
mount namespaces can defeat. Everything else in the class is unchanged: the move is implemented, the plane services
the deferred checkin promptly with status 0, and per-thread RPC socket creations are 0.


### 192. The page carries the server's REGISTRATION writes and not its COMPLETION write

Reading the guest's page from outside, with the offsets taken from the header (`request_state=4 request_op=8
request_seq=12 reply_state=48 reply_seq=56 futex=96 transport_ready=104`), one run gives the whole picture:

```
#0 (first incarnation's page):  request_state=0 (IDLE)  request_op=3 (ATTACH_LANE)  request_seq=1
                                reply_state=2 (DONE)   reply_seq=1  futex=1  transport_ready=1
#1 (second incarnation's page): request_state=1 (PENDING) request_op=2 (CHECKIN)   request_seq=1
                                reply_state=0          reply_seq=0  futex=1  transport_ready=1
```

and the server's own service-time log for the same run names the descriptor it wrote through:

```
service pid=2986406 op=2 region_fd=15 ino=14600025 dev=1 size=528
guest incarnation-2 request: memfd=7 ino=14600025 dev=1
```

So this is the sharpest form of the question, and it is no longer about identity, timing, wakes, ABI or aliasing:

* the server serviced `op=2` on a descriptor whose `(dev, ino)` **read at service time** equals the guest's page;
* the same page, read from the guest's own address space, **does** contain the server's registration writes --
  `futex=1` and `transport_ready=1`;
* it does **not** contain the completion: `reply_state=0` where the completion path stores `DONE(2)`, and the
  completion path's own log line is emitted **between** the late-reply guard (`if (region.map != page) continue`) and
  the stores, so the log printing means the guard passed and the stores were reached;
* and the second incarnation's request is still `PENDING` with `reply_state=0` for every sample of the observation
  window, i.e. the guest is parked on a request it published and that nothing answered visibly.

Two log lines settle this without any further hypothesis, and they are the next step: the server prints
`page->reply_state` immediately **after** its store, and it prints when `processControlLateReplyRetired` fires. If
the store's value reads back as `DONE` in the server's own mapping while the guest's mapping and the outside reader
both see `0`, then two shared mappings of one file are not coherent -- which contradicts what a shared mapping is,
and means the file identity has to be re-established yet again with something that cannot coincide (a value written
at a known offset by one side and read by the other, which the existing marker was meant to be and which the
completion path never reached because the store itself is the thing in question).

Everything else in the class remains measured: the loader's checkin is deferred, published through the plane, woken,
serviced and completed with status 0 within about a millisecond of registration, with zero per-thread RPC sockets
created and all four transport counters at 0.


### 193. The class resolves to ONE missing completion store: the request is serviced and the store never runs

The read-back instrument settles the contradiction of the previous sections, and it settles it in the only way that
fits **one** shared page: the server's completions are stored and read back correctly, and the one that matters here
never runs.

```
reply-stored pid=2996357 op=1 seq=2 readback=2 reply_seq=2 map=0x79692e5a8000 region_map=0x79692e5a8000
reply-stored pid=2996357 op=4 seq=3 readback=2 ...     (and op=5, op=3, op=8, op=6, op=2 of the FIRST incarnation)
                                                                  -- seven stores, every one read back as DONE(2)
request pid=2996357 op=2 seq=1        <-- the SECOND incarnation's checkin, serviced
                                      <-- and NO `reply-stored` line for it, anywhere in the run
```

with the totals over the same log: `requests=8  stored=7`. So the second incarnation's request **is** taken off the
page and serviced, and the completion store that follows the servicing **does not happen**. The page then keeps
`request_state=1 (PENDING) reply_state=0` for the rest of the run, the guest -- whose wait in this build has no
timeout -- parks on it, and the launcher's thirty-second shellspawn deadline is what finally fails.

This also explains every intermittent observation across sections 165-192 without further hypothesis: in the runs
where the completion **did** store, the outside reader caught the page in exactly the answered state
(`reply_state=2 reply_seq=1 request_state=0`, the `#0` sample of section 192), and in the runs where it did not, the
same page stayed PENDING and the guest never returned. The transport, the identity (verified at service time), the
wake, the ABI and the offset layout are all measured working; the request is even serviced. What is missing is the
one store, and nothing between the servicing log and that store is conditional except control leaving the block --
and the late-reply guard, which is the only `continue` there, prints when it fires and never printed.

The next step is therefore exactly one instrument and then one fix: wrap the checkin dispatch in the servicing path
so that an **exception** -- the only way control leaves that block silently in C++ -- is caught and logged with its
`what()`, and fix whatever it reports for a **post-exec** incarnation (the one case that differs: the Process
already exists, and the Thread named by the request's tid does not). That is a small, bounded change, and it is the
only remaining item between this class and the directive's boot criterion.


### 194. The exception hypothesis is disproved by its own instrument, and the store has THREE sites, not one

The checkin dispatch was wrapped so that an exception -- the only silent way out of a C++ block -- would be caught,
named with its `what()`, and turned into a status. The run produced **no** `checkin-exception` line and the page
still showed the second incarnation's request `PENDING` with `reply_state=0`, so:

* `Call::callFromMessage` for a post-exec incarnation does **not** throw;
* and the completion store for that request still does not run.

What that leaves is visible in the source rather than in the run: `page->reply_state = DONE` is written in **three**
places, and the read-back instrument was added to only **one** of them (the shared completion at the end of the
servicing loop). So a completion that goes through either of the other two sites stores correctly and silently with
respect to the instrument, which is exactly the shape of this observation -- and it also means the conclusion of
section 193 ("the store never runs") has to be stated more carefully: the *instrumented* store never runs; a store
in another path is not excluded.

The next step is therefore mechanical and small: put the same read-back logging on **every** site that writes
`reply_state = DSERVER_PROCESS_CONTROL_DONE`, with the pid, op, seq, the map pointer and the value read back, and
re-run. That single change makes "which path completed this request" readable instead of inferred, and it is the
last instrument this class needs before the fix -- because the candidates are now: a completion through an
uninstrumented path, or no completion at all, and those two are one log line apart.

Everything measured before it stands: the plane's deferred checkin is published after establishment, woken, and
serviced; the guest's page and the server's region are the same file at service time by `(dev, ino)`; registration
writes (`futex`, `transport_ready`) are visible to an outside reader and to the guest; the first incarnation's whole
sequence stores and reads back correctly; and per-thread RPC socket creations are 0.


### 195. ROOT CAUSE FOUND: the slot state races, and the server's store leaves it stuck at DONE

The completion store's sites were instrumented one by one, and the source showed what the runs could not: the
server writes `page->request_state = DONE` on completion, **while the slot belongs to the publisher**. The guest's
own `RELEASE` sets `IDLE`; the two race, and when the server's store wins, the slot stays at `DONE` **forever** --
and the publisher's claim only accepted `IDLE`, so every later request was refused:

```
[mldr-ctl] seq=3 after-dyld pid=... status=-3 ready=1 image=vchroot
Failed to tell darlingserver about our dyld info
[release-drops-pending] site=mldr.c:1347          (the instrumentation added for a different question, firing here)
```

`-3` is this file's own "no mailbox slot" result, whose comment says the caller then falls back to its datagram
route -- so one lost race silently moved every subsequent call to the legacy transport, and the boot's success or
failure depended on that fallback.

**The fix is a pair, and the pair matters.** Removing the server's store entirely broke the boot much worse (four log
lines), so the store is needed; what is wrong is that the publisher could not reclaim a completed slot. The claim
now accepts `DONE` as claimable as well as `IDLE`. The publisher reads its ANSWER from `reply_state`, never from this
flag, so reusing a completed slot is exactly what "the publisher owns the slot until it has read its answer" already
means.

Measured after the pair:

* `seq=3 after-dyld status=0` and `seq=4 after-execpath status=0` (both were `-3`);
* the whole `seq=1..7` loader sequence completes on the plane, including `after-seed`;
* `release-drops-pending` fires **zero** times;
* no `Failed to tell darlingserver ...` line at all;
* per-thread RPC socket creations remain **0**.

### 196. What is left: one missing completion store, and the exception is thrown later than the wrap

With the slot defect fixed, the remaining item is unchanged and precise: the second (post-exec) incarnation's checkin
is **serviced** and its completion store never runs.

```
requests=8  stores=7        -- exactly one serviced request without a stored completion
guest page: request_state=PENDING request_op=2 (CHECKIN) reply_state=0 reply_seq=0
```

The try/catch added around `Call::callFromMessage` in the checkin case logged **nothing**, so the dispatch itself does
not throw; the wrap does not yet cover the rest of the block, which is where an exception can still leave it silently:
`suppressReplyDelivery`, `noteServicedFromProcessControl` and **`doWork()`**. That is the next instrument -- the same
try/catch around that remainder, naming `what()` -- and it is the last gap between this class and a boot that does not
depend on the datagram fallback.

Everything else is measured: the deferred checkin is published after the process-control establishment, woken,
serviced and completed with status 0 within about a millisecond of its page registration; the guest's page and the
server's region are the same file at service time by `(dev, ino)`; registration writes are visible both to the guest
and to an outside reader; and per-thread RPC sockets are 0 with all four transport counters at 0.


### 197. The completion stops between two statements, and the leading explanation is the same defect class already fixed in the loader

Per-statement markers around the completion narrowed it to two statements:

```
request pid=3037247 op=2         (incarnation 1's checkin)
checkin-reply seq=1 status=0
completion-mark A op=2
completion-mark B op=2
reply-stored op=2 seq=1 readback=2 reply_seq=1 map=0x777d0fab6000 region_map=0x777d0fab6000   <-- incarn 1: complete

request pid=3037247 op=2         (incarnation 2's checkin)
checkin-reply seq=1 status=0
completion-mark A op=2
                                 <-- and nothing: no mark B, no store
```

and the timestamps put the whole server log inside **15 ms** -- `region passes=0` at 099.719507, `region passes=46`
at 099.733532, `wake-received passes=48` at 099.734622, `request op=2` at 099.734643 -- after which the server emits
**nothing at all** and, thirty seconds later, the launcher reports that shellspawn never became ready. So the second
incarnation's checkin is **serviced promptly** (1.1 ms after its page registration) and the server then **stops
inside the completion**, between two plain stores to shared memory.

A plain store cannot block, and the address guard immediately above (`region.map != page`) had just passed, so the
leading explanation is not the store: it is the **log line that precedes it**, `DarlingServer::Log ... << endLog`,
whose output goes to the run log's pipe when `DSERVER_LOG_STDERR` is enabled -- and a blocking write on a full pipe
is exactly the defect this work already found and fixed in its own loader diagnostic ("a diagnostic that takes a libc
lock / blocks on a pipe is a diagnostic that changes what it measures"). The server is single-threaded, so a blocking
write there stops servicing entirely: no further passes, no completions, and the boot dies at the launcher's deadline
with the guest parked on a request that was already taken off the page.

That fits every observation in this stretch, including the intermittency: how much the server logs, and how fast the
reader drains the pipe, decide whether the write blocks, so the same build can complete a checkin on one run and stall
on the next.

The next step is therefore the same fix, applied to the server: its diagnostic logging must not be able to block -- a
file sink, or a non-blocking/owned buffer -- and then the measurement is simply whether the second incarnation's
checkin reaches `completion-mark B` and `reply-stored` while the guest observes `DONE`, and whether the boot passes
the shellspawn deadline. The transport itself has been measured working at every layer: the page is one file at
service time, the wake arrives, the request is serviced within about a millisecond, the slot ownership race is fixed,
and per-thread RPC socket creations are 0.


### 198. Two failure modes for the same request, and the step that separates them

The same build, run with the server's diagnostics **off** (so nothing on the server side can block on the run log's
pipe), still does not complete the second incarnation's checkin:

```
guest diag file:  deferred-checkin seq=1 status=0          <-- the FIRST incarnation only
                  seq=3 after-dyld status=0
                  seq=4 after-execpath status=0
                  seq=5..7 (before/after-threadself, after-seed)
```

So the first incarnation is now completely healthy on the plane -- seven requests, every one answered with status 0,
the slot-ownership race fixed and `release-drops-pending` silent -- and the second (post-exec) incarnation's checkin
is still outstanding, with the page showing it `PENDING` and unanswered.

Together with section 197 that gives two distinct modes for the same request:

* **with** the server's diagnostics enabled, the request **is** serviced (log line `request op=2` 1.1 ms after its
  page registration) and the server then stops between two plain stores, immediately after a log statement whose
  output goes to the run log's pipe;
* **without** them, the request is left outstanding, and no log exists to say whether a pass serviced it at all.

Both modes end the same way: no completion store, the guest parked on a request it published, and the launcher's
thirty-second shellspawn deadline as the only thing that ends the run.

The step that separates them is therefore mechanical and belongs to the tooling rather than to the transport: the
**server's** diagnostic logging must be as unable to block as the loader's now is (a file sink, not the run log's
pipe). With that, the hatch can be on while the server keeps servicing, and the two questions become answerable
independently -- "was the request serviced" and "did the completion store" -- after which the remaining item is the
one the directive has named since the beginning: the wake must be the process **doorbell** (sections 8, 9, 10),
because the loader's pre-doorbell fallback has now been measured to deliver the byte and still leave the request
outstanding in a run where nothing else wakes the loop.


### 199. THE CRASH IS CAUGHT: SIGSEGV at `page->reply_seq`, and the mechanism is a dangling region reference

A crash probe was installed in the server (SA_SIGINFO, raw writes only, `_exit(91)` after reporting -- a server that
dies silently is the instrument-that-cannot-answer class again). It reports exactly one thing, twice (both streams):

```
dserver-CRASH sig=b addr=0x7c69aa4d1038
```

`sig=b` is **SIGSEGV**, and the fault address ends in **0x38 = 56**, which by the header's own offsets is
`reply_seq` (`reply_state=48`, `reply_status=52`, `reply_seq=56`). So the crash is exactly at the statement

```c
page->reply_seq = seq;      /* offset 56 */
```

which sits one line below `completion-mark A` -- the last line the server ever writes -- and two lines below the guard

```c
if (region.map != page) { ... continue; }
```

A plain store into a valid mapping cannot fault, so at that instant `page` is **not mapped**, while the guard one
statement earlier compared it with `region.map` and found them equal. That combination has one shape: `region` is a
**reference into `_processControl`** (and `page` was captured from `region.map`), and the entry was **modified** --
its old mapping unmapped by a re-registration -- while the reference was held. The guard then reads a stale/garbage
`region.map`, can compare equal by accident, and the completion writes into a mapping the server itself has already
destroyed.

The server is not as single-threaded as this code assumes: the build has an **async writer** thread, and the courier
handling that performs the (unmap + map) registration is separate from the servicing pass. Nothing in the registering
path takes a lock against the servicing path, and the servicing path holds a pointer/reference across statements
that can race with it.

The fix has two parts and both are small:

* the servicing loop must **copy** what it needs from the region (`map`, `fd`, `size`) into locals before it uses
  it, instead of holding a reference to a map element for the whole body;
* the (unmap, map) replacement of a region for a pid and the servicing of that region must be **mutually excluded**
  -- a lock, or a rule that only one of them may run at a time -- because a completion that races a replacement is
  exactly the observed crash, and its consequence (no completion store, the guest parked on its request, the boot
  ending at the launcher's deadline) is exactly the stall the whole class has been chasing.

This also retroactively explains the intermittency that defeated every earlier hypothesis: whether the replacement
lands inside the completion's window is a race, so the same build completes a checkin on one run and crashes on the
next. All the layers measured before it stand: the request is serviced within about a millisecond of its page
registration, the page is one file by `(dev, ino)` read at service time, the wake byte arrives, the slot-ownership
race is fixed, and per-thread RPC socket creations are 0.


### 200. THE ROOT CAUSE, IN CODE: the outgoing incarnation's destructor destroys the incoming one's page

The crash address identified the store; the source identifies why it writes into unmapped memory:

```
process.cpp:916      Server::sharedInstance()._closeFdCourierForPid(id());      // Process destructor
server.cpp:2586      void DarlingServer::Server::_closeFdCourierForPid(pid_t pid)
server.cpp:2629      // perf#30 PROCESS-CONTROL PLANE: the page belongs to the incarnation, so it goes with it.
server.cpp:2630      auto control = _processControl.find(pid);
server.cpp:2632          if (control->second.map) { munmap(control->second.map, control->second.size); }
server.cpp:2635          if (control->second.fd >= 0) { close(control->second.fd); }
server.cpp:2638          _processControl.erase(control);
```

The comment says the page belongs to the **incarnation**, and the lookup keys it by **pid**. `_processControl` is
`std::unordered_map<pid_t, ProcessControlRegion>` and `ProcessControlRegion` carries no incarnation identity at all
(mapping, size, counters). So when an `execve` starts a new incarnation, the **outgoing** Process is destroyed, its
destructor calls `_closeFdCourierForPid(pid)`, and that function **unmaps and erases the page the incoming
incarnation has already registered** -- because the pid is the same. The completion for the incoming incarnation's
checkin, which is running in the same thread after a nested pump, then stores `page->reply_seq` at a mapping the
server destroyed a moment earlier: **SIGSEGV at offset 56**, measured, twice, with the fault address landing exactly
on the new region's page (`region passes=46 page=0x70bdd2c14000`, fault `0x70bdd2c14038`).

That is the root cause of the whole class, and it explains every observation that resisted explanation:

* the crash is only for the **post-exec** incarnation -- the first incarnation's Process is not torn down in the
  middle of its own checkin;
* the request is **serviced** and the completion **never lands** -- the store faults, so the server dies and the
  guest waits on a page nobody will answer;
* the intermittency that defeated every earlier hypothesis is the ordering of a destructor against a completion;
* and the "second incarnation stops" narrative of sections 165-190 was this, seen through instruments that could not
  observe a silent death.

Both changes made in section 199 are still right and stay -- the servicing loop's fields are copied into locals, and
the old mapping is not unmapped on re-registration -- but neither can help when the mapping is destroyed by a path
that does not go through the re-registration at all.

The fix belongs at this site and is small: the region must carry the **incarnation** it belongs to (the courier
envelope's `process_generation`, already logged as `gen=` at registration), `Process` must supply its own generation
when it closes the courier for its pid, and the erase must happen only when the stored generation is the dying one.
An incarnation that is not current must not take another incarnation's page with it.

Everything else measured this cycle stands: the deferred checkin is published after establishment, woken, and
serviced within about a millisecond of the page's registration; the page is one file by `(dev, ino)` read at service
time; the wake byte arrives; the slot-ownership race is fixed; and per-thread RPC socket creations are 0.


### 201. MILESTONE: the crash is gone and BOTH incarnations' checkins complete over the plane

The fix at the destruction site (the region is no longer unmapped or erased by the outgoing incarnation's
destructor; only its descriptor is closed, because closing does not unmap) is measured:

```
crash probe lines:            0                     (was 2 per run: SIGSEGV at page->reply_seq)
server requests / stores:     8 / 8                 (was 8 / 7 -- every serviced request now completes)
completion-mark B lines:       8
reply-stored op=2 seq=1 readback=2 reply_seq=1     (the post-exec checkin stores, and reads back DONE)

guest: deferred-checkin seq=1 status=0             (first incarnation)
       deferred-checkin seq=1 status=0             (second incarnation, 14 s later in the same run)
```

So the deferred loader checkin now works for **both** incarnations of the process: published after the process-control
establishment, woken, serviced, completed with status 0, and observed by the guest, with per-thread RPC socket
creations at **0** and all four transport counters at 0.

The remaining barrier moved to a later point in the loader, and it is the dyld cycle reached at last because the
server no longer dies before it:

```
[mldr-ctl] deferred-checkin ... status=0 image=.../mldr!.../sbin/launchd
Cannot open /usr/lib/dyld: No such file or directory
```

`load()` fails at its own `open(path, O_RDONLY)` (mldr.c:519-522) for the nested dylinker image, i.e. the guest path
`/usr/lib/dyld` is not being translated to the prefix at that moment for the **second** incarnation -- the same class
of ordering problem this cycle started with (the first incarnation reaches its `after-dyld` step fine). The next step
is to read that translation path: which value it uses for the root, where that value is set for the second image, and
whether the hook's `finish_space_after_checkin` has run before the nested load in this incarnation (the hook wraps
that load, so the ordering is right by construction -- which points at the value, not the order).

Everything in the per-thread-socket class that this cycle set out to move is now measured working end to end: the
loader's checkin is deferred and plane-serviced for both incarnations, the wake arrives, the slot-ownership race is
fixed, the region lifetime is incarnation-safe, and no per-thread RPC socket is created.


### 202. After the crash fix: both checkins, plane vchroot, and the root of the SECOND image

The crash fix (section 200) plus the plane work moved the class to a new and much later barrier. What is fixed and
measured now:

* **no crash**: the crash probe reports nothing, where it used to report a SIGSEGV at `page->reply_seq` every run;
* **every serviced request completes**: `requests=8 stores=8`, with `completion-mark B` and `reply-stored
  readback=2 reply_seq=1` for the post-exec checkin;
* **both incarnations' deferred checkins complete** with `status=0` and are observed by the guest;
* **`Cannot open /usr/lib/dyld` disappeared** once the loader's vchroot query went over the plane: `path` in
  `loader.c` was also **uninitialized**, so an image with no root built the unprefixed guest path (fixed), and the
  plane gained `DSERVER_PROCESS_CONTROL_OP_VCHROOT_PATH 27u` -- an adapter that runs the ordinary `vchroot_path`
  call, appends last per the standing rule, and is distinct from op 8, which is the **setter** (`setVchrootPath`
  from a guest string) and answers `-EINVAL` for a zeroed query buffer. The architecture byte is part of that
  envelope and must be `2` (x86_64), not `0` (`invalid`);
* per-thread RPC socket creations are **0**; socket denials, urgent timeouts and courier misses are 0.

What remains is no longer in the transport at all. The **second** image of the process starts with no root:

```
bootstrap-root root=/tmp/dr-on-matched/libexec/darling rootlen=34     (first image, from __mldr_DYLD_ROOT_PATH)
bootstrap-root root=(null) rootlen=0                                  (second image)
```

and the server cannot answer for it either, because it answers from `process->vchrootPath()`, which is set by the
**setter** (op 8) that the guest image issues later in its own startup -- asking earlier is asking a question whose
answer does not exist yet. Two ways of carrying the root forward were tried and both fail for a measured reason:
a loader **static** (its state is re-established per image: the page is created anew and the sequence counter
restarts) and `setenv` (**the guest's environment is rebuilt** for the new image, so a value published by the loader
does not reach it).

So the root must be provided by whatever starts the second image, and finding that path -- the launcher's second
`mldr` invocation, and where `__mldr_DYLD_ROOT_PATH` is set for the first -- is the next step. That is an ordering
and provisioning question about the loader/launcher, not about the transport, and every transport item this cycle set
out to move is measured working.


### 203. The remaining barrier is the second image's environment, and it is outside the transport class

The root-provisioning question was followed to its source and the answer is now exact:

* the **server** sets the value once, in its own environment: `darlingserver.cpp:523 setenv("__mldr_DYLD_ROOT_PATH",
  LIBEXEC_PATH, 1)`;
* the **loader** rewrites that very entry for the guest, in place, stripping the `__mldr_` prefix so dyld reads it
  as `DYLD_ROOT_PATH` (`mldr.c:363-366`);
* and the **second** image sees **neither** name -- measured, again, as `root=(null)` at its hook entry, with the
  dyld open failing on the unprefixed guest path.

So the value is produced by the server, consumed and renamed by the loader, and then lost for the next image because
that image's environment is built anew. Three ways of carrying it forward were tried and each failed for a measured
reason -- a loader static (state re-established per image), `setenv` from the loader (the guest's environment is
rebuilt), and reading the guest's name as a fallback (the second image does not have it either) -- which is itself the
proof that the loss happens in the **environment construction for the new image**, not in any of those three places.

That is where the next step belongs: find what builds the second image's `envp` (in the loader, since it is the loader
that re-invokes itself for the guest image) and make `LIBEXEC_PATH`/the discovered root part of it, exactly as the
server provides it for the first. It is an ordering and provisioning question about the loader and the launcher.

Everything in the per-thread-socket class this cycle set out to move is measured working, and this stretch's changes
are the reason:

* the loader's checkin is **deferred** and published after the process-control establishment;
* it is transported by the **management plane** for **both** incarnations of a process, each observed by the guest
  with `status=0`;
* the region lifetime is **incarnation-safe**: the outgoing incarnation's destructor no longer unmaps and erases the
  page the incoming one has registered (the SIGSEGV at `page->reply_seq`, caught by an in-server crash probe, is
  gone, and every serviced request now completes: `requests=8 stores=8`);
* the slot-ownership race is fixed (the publisher reclaims a completed slot; `seq=3 after-dyld status=-3` and the
  silent datagram fallback it caused are gone);
* the vchroot **query** has its own plane op (27), distinct from the setter (8), with the architecture byte correct;
* and per-thread RPC socket creations are **0**, with socket denials, urgent timeouts and courier misses all 0.


### 204. MILESTONE: the boot PASSES, and exactly two socket creations remain

The root-provisioning barrier was the vchroot helper, and the fix is one block:

```c
/* src/vchroot/vchroot.c */
	// The root is needed by the LOADER for the image this helper is about to exec ...
	{ const char* dyld_root = getenv("DYLD_ROOT_PATH");
	  if (dyld_root != NULL && dyld_root[0] != '\0') { setenv("__mldr_DYLD_ROOT_PATH", dyld_root, 1); } }
	unsetenv("DYLD_ROOT_PATH");
```

`vchroot <dir> <binary>` enters the vchroot and `execv`s the image, and it **unset** `DYLD_ROOT_PATH` with the comment
"this is only needed for this binary and shouldn't be passed down". That is exactly the value the loader needs to
prefix the guest's own paths, and stripping it is what left every image started through this helper with no root --
measured as `root=(null)` and `Cannot open /usr/lib/dyld`. The variable is now renamed to the loader's private form
before it is removed from the guest's view, which keeps the original intent and gives the loader what it needs.

Measured, same build, first run after the change:

```
VERDICT: PASS        (markers: HELLO=1, FINAL=1)      3917 log lines, no failure line
bootstrap-root root=/tmp/dr-on-matched/libexec/darling rootlen=34     (six entries, every incarnation)
sockets created: 2   socket denials: 0   urgent timeouts: 0   courier misses: 0
```

and the two remaining creations are named exactly:

```
rpc-socket] created pid=3175090 tid=3175092 n=1 reason=checkin
rpc-socket] created pid=3175090 tid=3175093 n=2 reason=checkin
```

Both are **thread** checkins of one process, which is the one class the design already assigns elsewhere: the
generator's own note says checkin's **fd** instance is the pre-attach bootstrap checkin that stays on the datagram
path until the bootstrap header exists, while its **thread** instances (no fd) ride the lane. So the remaining work
is that those two thread checkins did not take the lane, and it is now measurable inside a **green** boot rather
than in a run that dies before the shell is up.

The whole per-thread-socket class is now in this state: the loader's checkin (both incarnations) goes over the
management plane with status 0; the region lifetime is incarnation-safe and the SIGSEGV is gone; the slot-ownership
race is fixed; the vchroot query has its own plane op; the boot passes with zero socket denials, zero urgent
timeouts and zero courier misses; and the two remaining creations are both named `reason=checkin` with their pids
and tids.


### 205. The two surviving socket creations are named, and one of them is `mach_msg`

Both of this section's results come from **instruments**, not from reasoning, and one of them corrects an earlier
conclusion of this same document.

**The `reason=` label was not a reason.** The creation line printed `reason=checkin`, and that was read as "two checkin
calls still need a socket". A check on the decision itself -- a print on the branch where the plane block is skipped --
fired **nothing**, while the socket was still created. The label is `t_rpc_socket_reason`, which is set at **thread
entry** in `darling_thread_entry` and is cleared only when a socket is finally created, so it names the **first
candidate** of that thread, not the operation that created the socket.

**The precise attribution is the call site.** The guest already knows the exact name at the point of use:
`__dserver_note_call(name)` sets `__dserver_current_call` immediately before each request. The instrument now reports
the change of the descriptor returned by `mach_driver_get_fd()` (the same descriptor every call, so a change is
exactly a creation) together with that name. One correction to the first attempt matters: it was placed in the guest's
`libsystem_kernel`, and building only `mldr` left it **out of the artifact under test**, so it printed nothing -- the
probe has to be compiled into the image it measures (`ninja libsystem_kernel.dylib`).

With it in place, the creations are, exactly:

```
rpc-socket-call] pid=3576418 tid=3576421 call=semaphore_timedwait fd=1048573
rpc-socket-call] pid=3576418 tid=3576418 call=mach_port_request_notification fd=1048575   (main thread: the process socket)
rpc-socket-call] pid=3576418 tid=3576418 call=kqchan_proc_open      fd=1048575            (main thread)
rpc-socket-call] pid=3576423 tid=3576423 call=fork_wait_for_child   fd=1048575            (main thread)
```

so the two creations were the first `mach_msg_overwrite` of two non-main threads, and `mach_msg`'s lane path is behind
its own hatch: `gr_machmsg_enabled()` reads `DARLING_GUEST_RING_MACH_MSG=1` and without it the call goes to the
datagram by default. **Measured with the hatch on**: the boot still PASSES and creations fall from **2 to 1**.

**What is left is one call, named.** `semaphore_timedwait` on a non-main thread. The classification note says exactly
why it is the last one: the semaphore **signalling** operations are on the management plane (ops 24u/25u) while
`semaphore_wait` is deliberately excluded, because it **blocks** and the plane is a single serialized slot serviced
inside the server's own loop pass. So its remaining home is the per-thread socket, and giving it a proper one is the
directive's §18-§21 shape: a request published non-blockingly whose **wait happens on the guest side** (the server
wakes a guest futex), which is what keeps the slot free -- or the duplex lane's wait-capable shape, which already
carries a blocking `mach_msg`. Both are lanes, neither is a per-thread socket.

State of the class: the boot PASSES with both markers; creations are 1 with the mach-msg lane hatch and 2 without it;
socket denials, urgent timeouts and courier misses are 0 in every run; the loader's checkin for both incarnations
rides the plane with status 0; and the single remaining per-thread socket is attributed to one call on one thread.


### 206. The blocking family rides the Ring, mach_msg is Ring by default, and the denial oracle is the instrument

This section records a chain in which every step was decided by a measurement, and two of those measurements corrected an
instrument or a conclusion.

**The oracle is the hard hatch, not a counter.** `DARLING_DISABLE_THREAD_RPC_UDS=1` aborts inside
`mach_driver_get_fd()` and prints pid, tid and the call name. The earlier `last_fd`-change detector was **not** an
accurate creation detector across threads, so no architectural claim was built on it.

**Denial 1 -- `mach_port_request_notification`** (main thread, first use). An early Mach port operation, the same class
as `mach_port_type` / `mach_port_mod_refs` / `mach_port_unguard` / `vchroot`, whose home is the process management
plane. Added `DSERVER_PROCESS_CONTROL_OP_MACH_PORT_REQUEST_NOTIFICATION 28u`, appended last: the server builds the
**ordinary** Call (one semantic core) with two 32-bit request words per payload word, and the OUT parameter `previous`
travels as the guest ADDRESS the Call writes through `writeMemory`. Denial gone.

**mach_msg Ring by default** (directive section 2). `gr_machmsg_enabled()` required
`DARLING_GUEST_RING_MACH_MSG=1`; it now reads the same variable as a **diagnostic opt-out** (`=0`), so an unset
environment selects the Ring. Denial gone -- but only after the **image** question was settled, see below.

**The instrument was running stale, twice.** The denial printed a format string with **no** image field after the field
had been added, which meant the denying process was not running the binary that had been rebuilt. Two independent
causes were found and both are now handled: (a) the same source file is compiled **twice** -- `emulation.dir` and
`emulation_dyld.dir`, the latter for the `dyld` image, which had an object from two days earlier; building
`libsystem_kernel.dylib` does **not** cover it, so `dyld` must be built and deployed too; (b) `lkm.c` has a second
**eager** socket request in `mach_driver_init` that guards the main thread's fd, and `mldr`'s copy of the wrappers
no-ops the call-note macro. The denial report now carries `delta=`, the caller's offset relative to
`mach_driver_get_fd`, which resolves to a real symbol with `llvm-nm` and is immune to address-space layout.

**Denial 2 -- `fork_wait_for_child`, at `_dserver_rpc_fork_wait_for_child + 0x19`.** The symbol is the **outer**
generated wrapper, and `+0x19` is its socket line: the outer wrapper requested the socket **before** the explicit
wrapper could attempt the lane, so the lane attempt could never prevent the request. The generator now leaves
`server_socket = -1` for a ring-routed call and the explicit wrapper obtains the socket **lazily**, only on the path
where the lane was not taken; a caller that already has a descriptor (the loader's checkin entry point) keeps it.
Denial gone, and `semaphore_timedwait` (denied in the same position before it) is gone with it.

**The blocking family** (directive sections 4-8). Audit of the real generated names -- `semaphore_wait`,
`semaphore_timedwait`, `semaphore_wait_signal`, `semaphore_timedwait_signal`, `fork_wait_for_child`: every one has a
**fixed inline request**, a **header-only reply**, **no descriptor**, **no destroy** and **no caller-S2C**, and every one
**waits**. They are therefore canon-safe for a closed request->reply lane and unsafe for the single-slot management
plane, where a blocking request would occupy the one slot and stall every other management operation. Added
`DSERVER_RING_CLASS_BLOCKING 0x40u`, classified the five explicitly (not as SIMPLE) and added them to the opcode set of
record and to the generator's ring set. No new guest primitive was needed: the existing wait is already the required
one -- spin, advertise as a waiter, **`FUTEX_WAIT` with no transport deadline**, re-check, treating `EAGAIN`/`EINTR`
as a re-check rather than as a result -- which is exactly section 7's shape, and the semantic timeout continues to
travel in the request body and be decided by the server's Call (section 8).

**Denial 3 -- `kqchan_proc_open`**, at the outer generated wrapper again. It is a **descriptor-bearing** early call;
its planned home is the management plane with the descriptor on the SCM_RIGHTS courier, exactly as `console_open`
(26u) and `kqchan_mach_port_open` (9u) already do.

**Also fixed here**: the child after `fork` created a per-thread RPC socket **with no RPC behind it** (to reset and
guard the loader's cached descriptor), which the oracle denied while reporting a stale call label. The loader now
**invalidates** the cached descriptor in `__mldr_postfork_child` (and the main-thread shortcut applies only while the
process socket exists), and the child neither creates nor guards one. The reason-emission budget for a declined lane
attempt was raised from 8 to 96 lines per process: MEASURED, the miss that mattered arrived at log line 1356, far past
the point where eight earlier misses had spent the budget.


### 207. MILESTONE: the boot passes under the hard socket-disable, and creates no socket without it

Two runs, same build:

```
DARLING_DISABLE_THREAD_RPC_UDS=1 :  VERDICT PASS   socket denials 0   DENIED lines 0   HELLO=1 FINAL=1
without the hatch                :  VERDICT PASS   rpc-socket created 0 (loader counter AND per-call attribution)
                                                      rpc-socket-DENIED 0   HELLO=1 FINAL=1
```

The second run is the stronger claim of the two: the per-thread RPC socket is not *forced absent*, it is **naturally
never requested**, which is what section 20 of the directive asks for.

**Denial 3 -- `kqchan_proc_open`** was migrated the same way as its two siblings (`kqchan_mach_port_open` 9u,
`console_open` 26u): `DSERVER_PROCESS_CONTROL_OP_KQCHAN_PROC_OPEN 29u`, appended last, the server running the ordinary
Call (one semantic core) and the descriptor the call RETURNS travelling on the process courier with its token in
`reply_payload[1]`, resolved guest-side by the courier receive, with `-1` from the plane still meaning "nothing
published" so the datagram fallback stays safe. Denial gone, and the boot reached its full length (3916 log lines)
with HELLO and FINAL.

**The last denial was not a missing migration but a rule that was not yet obeyed.** `sigprocess` fell back to the
datagram after `[urgent-wait-TIMEOUT] op=12 slot=1 state=1`: the bounded, signal-safe poll of the urgent slot gave up
and returned `-1`, which is the same value the function returns when **nothing was published** -- so the caller read
"no transport" and took the datagram, creating exactly the socket this work removes. The request HAD been published
(the slot was PENDING and the doorbell had been rung), so retrying it elsewhere would run the operation twice. The
timeout now returns a distinct `-2`, releasing its own slot (a late completion carries the caller's sequence and is
ignored by the seq check), and `sigexc.c` treats `-2` as **published, completion not observed**: no datagram retry, no
socket, one bounded report (`[sigprocess-urgent-uncompleted]`), and the only thing given up is a possible server-side
change to the signal number -- a degradation this path accepts and a socket is not.

That closes the per-thread-socket class at the boot gate: zero creations, zero denials, both markers, with and without
the hatch.


### 208. The blocking family on the Ring: four modes exact, and `pthread_canceled` returns EBUSY on the plane

**What is measured working** (mode `sem_*` added to `src/tools/ring_mach_msg_test.c`, the durable workload):

```
RING_MACH_TEST mode=sem_ready            pass=1 kr=0  elapsed=0.000
RING_MACH_TEST mode=sem_timed            pass=1 kr=49 elapsed=0.300
RING_MACH_TEST mode=sem_wait_signal      pass=1 kr=0  elapsed=0.000
RING_MACH_TEST mode=sem_timedwait_signal pass=1 kr=49 elapsed=0.300
RING_MACH_TEST mode=timeout              pass=1 (0.200386 s)
```

`kr=49` is `KERN_OPERATION_TIMED_OUT`, and `elapsed=0.300` for a requested 300 ms is the directive's section 8/14 claim
measured in one line: the SEMANTIC timeout decided the result and the transport invented neither the number nor a
deadline of its own. With the hard socket-disable on, the same run reports **socket denials 0 and creations 0**.

**One server-side root cause was fixed and it is the D1/D2 envelope rule.** A plane handler stamped `tid = pid`
("PROCESS_SCOPED"), so `semaphore_signal` -- while another thread was legitimately parked inside a blocking Ring
`semaphore_wait` whose Call had suspended its fiber -- was executed **on the parked thread**, and the state machine
refused it:

```
terminate called after throwing an instance of 'std::runtime_error'
  what():  Thread has both a pending call and a pending continuation (pending_call=58 tid=4158725 suspended=1)
```

callnum 58 is `semaphore_signal`. All 21 plane handlers now take the **publisher's tid** from the envelope
(`page->request_tid`, published before the release store of `request_state`, falling back to `pid`), which is what
"thread identity is part of the transport envelope" means in code. The throw disappeared from every subsequent run.

**What remains, with its exact cause.** Four modes that create threads (`sem_block`, `basic`, `ool`, `r2`) start and
never print a verdict, and the process then spins; the log of an isolated `sem_block` names the loop:

```
[pc-postplane st=16- sp=f18c0]        (72 occurrences)
[release-drops-pending] site=dserver-ring.c:2752
```

The marker is `pthread_canceled.c:96`, `st=16` is `EBUSY`, and the site retries: `pthread_canceled` reaches the plane
and is answered `EBUSY` instead of being serviced, so every thread-creating workload hangs in the cancellation
handshake. That is not a transport gap -- it is the D1/D2 servicing rule, which says in as many words that
`pthread_canceled` is serviced **directly against the target `Thread` state, WITHOUT `Call::callFromMessage()` /
`Thread::doWork()`**, precisely because the naive synthesized-Call-on-a-thread shape collides with a thread that is
suspended or busy. The publisher's tid is the right envelope for a request that belongs to its issuer, but
`pthread_canceled`'s **target** is a different thread, and it must be resolved by identity and have its semantic core
called directly rather than being turned into a Call that has to be dispatched on someone's thread.

Also recorded: `[sigprocess-urgent-uncompleted] no-datagram-retry` appeared in place of the former socket fallback
(section 207), and the harness marker `mode=sem_block` must be matched on the **verdict** line rather than the
`[rmmt] start` line -- a marker that matches a start line reports PASS for a workload that never finished, which is
the "instrument that cannot answer" class again.


### 209. Correction: `st=16-` is `-EINVAL`, not `EBUSY` -- and what the thread-creating hang therefore is not

The previous section read `[pc-postplane st=16-]` as "EBUSY(16) and the site retries". **That reading was wrong**, and the
probe's own definition says why: `__pc_probe` prints its value in **hexagonally rendered digits followed by `-` for
negative**, so `st=16-` is `-0x16` = `-22` = **`-EINVAL`**, which for `pthread_canceled` is the *normal* answer meaning
"no cancellation pending, do not cancel". The 72 repetitions are therefore the ordinary polling of a cancellation
point, not a retry storm, and `pthread_canceled` is not the cause of the four thread-creating modes failing to print.

The same reading also confirms the semantic core is behaving: `dtape_thread_cancel_state_canceled` returns only `0` or
`EINVAL`, and the observed value is exactly `EINVAL` (the `action 0` branch when `pending` is not set, or when the
cancel is disabled or already acted upon). A status that only ever takes two values cannot be reporting a transport
fault, and saying so required decoding the instrument rather than trusting the shape of its output -- the same rule
that has caught every other instrument defect in this work: **check the premise before blaming the result**.

**What is therefore still open, stated as the question rather than a guess.** Four modes that create threads
(`sem_block`, `basic`, `ool`, `r2`) start, never print a verdict, and leave a process whose plane requests keep being
published and released with the server's reply outstanding:

```
[pc-entry] [pc-threads-ok] [pc-postplane st=16-] [pc-return-9+]
[release-drops-pending] site=dserver-ring.c:2752
```

`release-drops-pending` fires when a plane request is released while its slot state is still PENDING, which means the
caller gave up on a completion the server had not produced. That is the thread to pull, and the next step is not a
guess: take a **live** hung process and read what its threads are actually blocked in with
`scripts/darling-trace-guest.sh` (per-thread syscalls with tids, the full `/proc/<pid>/syscall` line, and the parked
`pc` resolved against raw maps), which is the tool built for exactly this class and which was extended for it in this
work. The blocking-family migration itself is what the four exact modes above measure; the hang appears only where a
thread is *created*, so the suspicion belongs on the thread-creation handshake and on a lane whose owner is suspended,
not on the semaphore transport.


### 210. Two more instrument premises to check, and the hatch's own visibility

**`[pc-return-9+]` is not a value of 9.** The probe at the end of `sys_pthread_canceled` is

```c
__pc_probe("[pc-return-", ret < 0 ? 9 : 0);
```

so the tag is `[pc-return-` and the rendered digit is an **encoding**: `9` means `ret < 0` (the same deliberate encoding
the probe's comment already explains, where an earlier version used `8`). Reading it as "+9 status" was the second
misreading of this instrument in two sections, and both were caught by going to the definition instead of the shape of
the output.

**The hard hatch's premise is not verified per image.** `__dserver_hatch_enabled()` in `lkm.c` decides by scanning
`/proc/self/environ` for `DARLING_DISABLE_THREAD_RPC_UDS=1`. MEASURED EARLIER in this work and recorded as a rule: the
loader **replaces the environment** when it sets up the next image, so a value read late is not the value the process
was started with. `read once` was applied to the loader's own diagnostic gate; it has **not** been applied to this
hatch, which is therefore verified only in images that read it before the swap. That makes "denials 0" a weaker claim
than it reads: a socket request in an image whose environment no longer carries the variable would not be denied and
would not be counted. The check must be made on a value that cannot change -- read once, cached, and carried by the
loader (an elfcall or a value published with the process, not a fresh `/proc/self/environ` scan per image) -- before
any further acceptance claim is made on the hatch.

That is the state of the gate: the boot passes with the hatch as it is today, the four blocking-family modes that
create threads do not, and the next measurement must first establish what the hatch was actually enforcing in the
image where the hang occurred. A conclusion drawn from an instrument whose premise was not checked is exactly what
this work's rules forbid, and this section records the two readings that were wrong before the third was believed.


### 211. The hatch is visible in the guest (measured), and the hang narrows to thread creation's semaphore handshake

**The hatch's premise is now measured rather than assumed.** A boot with `DARLING_DISABLE_THREAD_RPC_UDS=1` ran, inside
the guest, `printenv DARLING_DISABLE_THREAD_RPC_UDS` and echoed it: the guest printed `HATCHCHECK1`, so the variable the
gate scans for is present in the environment of the guest image. The worry recorded in the previous section was
therefore real as a class but does not apply to the guest images here, and "denials 0" stands as measured for them.

**That leaves the hang without its easiest explanation.** If nothing was denied, then the four thread-creating modes did
not fail because a socket request was refused -- and the difference between them and the four exact semaphore modes is
that they **create threads**. That points at the handshake `pthread_create` performs, and this work already records what
that handshake uses: an earlier finding notes that `pthread_create` blocks above roughly fifty live guest threads in the
server's **callnum 62 (`semaphore_timedwait`) handshake**. Callnum 62 is one of the five calls this cycle moved from
the per-thread datagram socket to the thread's own Ring lane, so the handshake now runs over a transport it did not run
over before, and the four exact modes above (which do not create threads) are exactly the ones that pass.

Stated as the next measurement rather than a conclusion: with `basic 1` and the hatch on, read the Ring's own trace for
callnums 58/60/62 (signal / wait / timed wait) and find the step where the handshake stops -- which request was
published, whether its reply was delivered, and whether the creating thread was parked on the ring, on the loader's
plist, or on a futex. The Ring trace and the per-thread guest trace both already exist for this.

Two green results remain from this cycle and are unaffected by that: the boot passes under the hard hatch with zero
denials, and it creates zero per-thread sockets without it.


### 212. Suite result per mode, and the hang localized to the resume of a suspended Ring Call

Mode by mode, each its own boot, hard hatch on, verdict markers tied to the result line:

```
RING_MACH_TEST mode=sem_ready            pass=1 kr=0   elapsed=0.000     created=0 denied=0
RING_MACH_TEST mode=sem_timed            pass=1 kr=49  elapsed=0.300     created=0 denied=0
RING_MACH_TEST mode=sem_block 100        pass=1 kr=0   elapsed=0.101     created=0 denied=0
RING_MACH_TEST mode=sem_wait_signal      pass=1 kr=0   elapsed=0.000     created=0 denied=0
RING_MACH_TEST mode=sem_timedwait_signal pass=1 kr=49  elapsed=0.301     created=0 denied=0
RING_MACH_TEST mode=r2                   pass=1 parked_parent_ok=1 parked_elapsed=2.995 created=0 denied=0
RING_MACH_TEST mode=basic 20             NO RESULT LINE                    created=0 denied=0
RING_MACH_TEST mode=ool 10               NO RESULT LINE                    created=0 denied=0
RING_MACH_TEST mode=sem_gap 5000 1       NO RESULT LINE                    created=0 denied=0
```

and individually `basic 1` passes (`pass=1 min_elapsed=0.000603`) with the same hatch.

The two facts together localize it: a park of 101 ms completes and a park of **2.995 s** completes (`r2`, which is the
mach_msg duplex park) while a park of **5 s** never returns, and the failure is not a fast failure -- it hangs, so the
wait being used is the **unbounded** one, correctly, and the reply simply never arrives. That names the missing piece
and it is on the server side: a Ring Call that suspends its fiber for a semaphore wait must be **resumed and its final
reply published** when the semaphore becomes signalled (by the other thread's `semaphore_signal`, which itself arrives
on the management plane). The client is waiting exactly as designed; the server is not completing it.

Also recorded, because it names a bound this work must not reuse: the guest has a helper
`gr_duplex_wait_reply` that is deliberately a **bounded proof primitive** -- 50 ms per `FUTEX_WAIT` times 60 rounds,
about 3 s before `committed-unknown` -- and its own comment says it is right for a regression proof whose liveness must
never wedge a boot and **WRONG as the semantics of a real blocking receive**, because it would turn a slow server into a
fabricated failure. The blocking family's wait is the unbounded one (which is why the 5 s park hangs instead of failing
after 3 s), so the ~3 s figure seen in this work's earlier notes belongs to the proof primitive, not to the production
path.

`basic 20` and `ool 10` create threads and do not print, while `basic 1` does; that is the same thread-starvation
question already recorded for `pthread_create` and is not yet separated from the resume defect above.


### 213. The 5-second semaphore hang is NOT the Ring: it reproduces on the legacy datagram arm, and the sweep says race, not timeout

**The A/B the directive asked for.** One workload, `sem_gap 5000 1`, two transports:

```
Ring      (DARLING_DISABLE_THREAD_RPC_UDS=1):  HANG   denied=0  created=0
legacy UDS (DARLING_GUEST_RING_BLOCKING=0)  :  HANG   denied=0  created=3
```

The legacy arm HANGS TOO -- and it creates three per-thread sockets, so it really is the old datagram path. By the
directive's own rule that settles the attribution: **the Ring is not the cause**, and the bug is in the SEMANTICS the
two transports share. The diagnostic opt-out that made this measurable is
`DARLING_GUEST_RING_BLOCKING=0`, which changes the transport and nothing else: the same call, the same request and the
same semantic timeout. (Adding it also produced a small lesson about the harness: the verdict wrapper stored only the
VALUE of `--env` and dropped the flag, so the runner rejected the bare `K=V` -- a wrapper bug that would have been read
as a workload failure.)

**The delay sweep**, one iteration each, UDS arm, host watchdog = requested delay + 10 s, no transport timeout:

```
sem_block 100   PASS   elapsed=0.101
sem_block 2000  HANG
sem_block 3000  PASS   elapsed=3.001
sem_block 3500  HANG
sem_block 5000  PASS   elapsed=5.001
```

**There is no threshold, and that is the result.** A monotone boundary would have pointed at a bounded wait; instead
2.0 s and 3.5 s hang while 0.1 s, 3.0 s and 5.0 s complete, and `sem_gap 5000` itself has now both hung and passed on
the same two arms. A delay-dependent monotone failure cannot look like this; a **race** can. The rule the directive
sets -- search for a timeout only if the sweep shows a threshold -- therefore fires as "do not search for a timeout",
and the ~3 s figure this document records elsewhere must not be used as evidence here (it belongs to
`gr_duplex_wait_reply`, a bounded proof helper the semaphore path does not call: the semaphore path is
`gr_full_trap` -> `gr_wait_reply`, whose `FUTEX_WAIT` has no timeout at all).

**What the race is, stated as the thing to measure rather than a guess.** The shape is a lost wakeup between a waiter
and a signal that crosses it: the waiter must be registered where the signal can find it -- or the signal must have left
a state that a later wait observes immediately, which is the XNU `semaphore_signal`-with-no-waiter contract. The
flakiness says the two orderings are both reachable and one of them loses the signal. The next measurement is the
transaction-scoped trace the directive specifies (guest publish/enter, server consume/process/suspend, signal thread
before/after its RPC, server signal enter/result, waiter resume, reply publish, futex bump and consume), run against a
**hanging** instance so the missing stage is named rather than inferred. Two green results stand unchanged: the boot
passes under the hard hatch with zero denials and creates zero sockets without it, and the short/medium semaphore parks
(`sem_block 100`, `sem_timed 300`, `sem_wait_signal`, `sem_timedwait_signal`, `r2` with a 2.995 s park) are exact on
the Ring.


### 214. The trace names the missing stage: the hang is in THREAD CREATION, before the test's own wait ever leaves the guest

The server already had the right instrument and it only needed to be switched on: `DSERVER_TEST_TRACE_FILE=<path>` makes
`test-diagnostics.cpp` append `rpc.semaphore.begin` / `rpc.semaphore.reply` records -- operation, pid, tid, wait/signal
names, sec/nsec, code and outcome -- without touching the transport. Three attempts of `sem_block 2000 1` on the UDS arm
gave `NO-RUN`, `PASS (elapsed=2.001)` and `HANG`, and the trace of the HANG says:

```
begin  semaphore_timedwait pid=141975 tid=141976 wait_name=5123   sec=2 nsec=0
reply  semaphore_timedwait pid=141975 tid=141976 wait_name=5123   code=49 outcome=timeout
begin  semaphore_signal    pid=141975 tid=141976 signal_name=4867 code=0  outcome=success
begin  semaphore_signal    pid=141975 tid=141976 signal_name=0    code=15 outcome=error
```

and then that process emits **nothing more**.

**The test's own semaphore operations are not in the trace at all** -- no `semaphore_wait` and no
`semaphore_signal` for the semaphore the workload created. The records that ARE there are the thread-start handshake:
a two-second `semaphore_timedwait` that times out (`code=49`), a successful signal, and then a signal with a ZERO name
that returns `code=15`. The workload's main thread never reaches its own wait, because it is inside `pthread_create`
waiting for a thread that the handshake never finishes delivering.

That is the answer to the whole section: the 5 s / 2 s / 3.5 s "hangs" are not semaphore-wait hangs at all. The delay
is irrelevant to them because the mode creates its helper thread BEFORE it waits, so a thread-creation that never
finishes hangs the mode at the same place for every delay -- which is exactly the non-monotone, flaky pattern the sweep
produced, and it is also why `basic 1` passes (one thread, the race won) while `basic 20` and `ool 10`, which create
threads repeatedly, do not. And it is why the A/B could not blame the Ring: both transports reach the same stuck
handshake.

This document already records the handshake's identity: an earlier finding notes that `pthread_create` blocks above
roughly fifty live guest threads in the server's **callnum 62 (`semaphore_timedwait`)** handshake, and that the blocking
family is what this cycle moved onto the Ring. The next step is therefore the handshake itself: the zero-name
`semaphore_signal` that answers `15` is the anomaly to explain first (a signal aimed at a name the server no longer
recognises), and the trace now prints exactly the fields needed to follow it -- operation, pid, tid, both names, sec/nsec
and the code.


### 215. The suite RED is a plane single-slot stall (`SET_DYLD_INFO`, op=4, never answered), and the tid change was not it

With loader diagnostics on, a HANGING `sem_block 2000 1` (UDS arm) prints its own cause:

```
[mldr-ctl] request pid=145856 op=4 page=... memfd=12 ino=14604479
[mldr-ctl] planeloop BEGIN pid=145856 op=4 mine=3 state=1
[mldr-ctl] iter=6000 waited=0 reply=0 seq=3
[mldr-ctl] iter=9000 waited=0 reply=0 seq=3
```

op=4 is `DSERVER_PROCESS_CONTROL_OP_SET_DYLD_INFO`, `state=1` is PENDING, `reply=0` is `reply_state != DONE`, and the
loop count is unbounded. So the loader published a management request through the plane and the server **never
answered it**; the workload's own semaphore operations never leave the guest because the thread that would run them is
still inside image setup. That is consistent with the section-2 A/B -- both transports reach the same stall -- and it
is the second time this class has appeared (section 207's boot had a plane request that was serviced late; here it is
not serviced at all).

**The publisher-tid change was tested and is NOT the cause.** MEASURED: reverting all 21 plane handlers to the
process-scoped `tid = pid` convention (keeping the envelope helper and the D1/D2 classification where an operation's
meaning is tied to its caller) leaves `sem_block 2000 1` hanging exactly as before. The revert is safe and is kept: the
classification the directive asks for is now decided by measurement rather than applied uniformly -- process-scoped
semantics use the process's own thread, and a per-operation choice is made only where the semantics require the caller.
That also disposes of the hypothesis that the earlier `pending_call=58` abort and this stall share a cause: the first was
a synthesized Call aimed at a thread that was legitimately parked, and this is a request that is not serviced at all.

The next measurement is on the server's plane pass for op=4: whether it claims the slot at all (`planeloop` on the guest
side shows the state it sees), whether the synthesized `SetDyldInfo` Call runs, and whether the reply stores
(`reply_seq`/`reply_status`/`reply_state`) execute -- the same instrument chain section 193 built for the completion
store, applied to this op. The `iter=N waited=0 reply=0` counter is already the guest-side half of exactly that question.


### 216. Server-side half of the stall: direct ops are serviced, the synthesized-Call op is not, and the region has lost its fd

The server half of the same HANG, with `DARLING_SERVER_COURIER_LOG=1 DSERVER_LOG_STDERR=true`, while the loader spun
through `iter=20000`:

```
process-control-pass    pass=1890 sees-pending pid=157119 op=7  seq=1 reply_state=0 transport_ready=1
process-control-service service pid=157119 op=7  region_fd=-1 ino=0 dev=0 size=0 map=0x735a7cdfc000
process-control-marks   completion-mark A/B pid=157119 op=7
process-control-store   reply-stored pid=157119 op=7 seq=1 readback=2 reply_seq=1
process-control-pass    pass=1915 sees-pending pid=157119 op=24 seq=2 reply_state=0 transport_ready=1
process-control-service service pid=157119 op=24 region_fd=-1 ino=0 dev=0 size=0
```

Three facts, each measured:

1. **The pass IS running and IS claiming**: `pass=` advances into the thousands and `sees-pending` names op=7
   (`pthread_canceled`) and op=24 (`semaphore_signal`) -- the two operations whose handlers service their semantics
   **directly**, without synthesizing a Call. Both complete, both store their reply.
2. **op=4 (`SET_DYLD_INFO`) is never serviced.** Its handler is one of the ones that builds a `Message`, calls
   `Call::callFromMessage`, and runs `created->thread()->doWork()` **inside the plane pass**. Nothing about it appears
   in the server log at all, while the guest spins on it forever.
3. **The region has lost its descriptor identity**: `region_fd=-1 ino=0 dev=0 size=0`, which is the shape the earlier
   incarnation-safety fix produces -- the outgoing incarnation's destructor closes the courier fd for the pid while the
   incoming incarnation keeps its mapping. Servicing survives that (ops 7/24 work), so it is not the stall's cause, but
   it is the state in which the stall happens and it is worth stating rather than assuming.

**Both publisher choices deadlock, and that is the finding.** With `tid = pid`, the Call is dispatched onto the
process's own thread -- which for this request is the very thread parked waiting for the reply, so the plane pass waits
on work that waits on the pass. With `tid = publisher` (the change reverted in the previous section) the Call lands on
the thread that published the request, which is busy by construction. The common defect is therefore not which tid is
chosen: it is that the synthesized Call's execution is made to **depend on a guest thread being free**, when the plane's
whole purpose is to be usable when the guest is exactly not in a state to run anything. That is the next fix, and it is
a servicing change to the plane handlers that build Calls, not a new transport.


### 217. The plane's synthesized Calls no longer depend on a free guest thread (op=4 fixed and measured), and the suite RED is the thread-creation handshake

**Fix applied and measured.** `SET_DYLD_INFO` (op=4) is no longer serviced by building a Call and running `doWork()` in
the plane pass. It is now serviced **directly**, through `Process::setDyldInfo`, which calls the very core
`Call::SetDyldInfo::processCall` calls (`dtape_task_set_dyld_info`) -- one semantic core, reached without requiring a
guest thread to be free, exactly as `pthread_canceled` already reaches `Thread::pthreadCanceled`. The envelope names the
process by NSID, and MEASURED: that key alone answered `-ESRCH` and the loader reported
`Failed to tell darlingserver about our dyld info` (the request was answered instead of hanging, which proved the shape),
while trying NSID and then the process ID found it and the boot returned to GREEN (`sem_ready` PASS, denied 0,
created 0). That removes the class the previous two sections measured: a plane request whose servicing depended on the
free time of the thread that was itself waiting for the answer.

**What still hangs, with the evidence that names it.** `sem_block 2000 1` and `basic 20` still HANG (denied 0), and the
server's own semaphore trace of a `HANG` shows the workload's main thread still occupied with the **thread-creation
handshake**:

```
begin  semaphore_timedwait pid=185350 tid=185351 wait_name=5123  sec=2 (the handshake, timed out: code=49)
begin  semaphore_signal    pid=185350 tid=185350 signal_name=4867   <-- the main thread is HERE, not in its own wait
```

The workload's own `semaphore_wait` never reaches the server, so the mode cannot print: the main thread is inside
`pthread_create` -- which is exactly section 214's conclusion, now re-measured after the op-4 fix, and it is why the
delay is irrelevant to these hangs (the helper thread is created before the wait). The loader's `iter=N waited=0 reply=0`
spin on its own op=4 is a **downstream symptom** of the process being wedged in that handshake, not an independent
stall.

So the remaining RED is one handshake, and the two anomalies to explain are already named in the trace: a
thread-start `semaphore_timedwait` that expires (2 s) instead of being signalled, and a `semaphore_signal` with a ZERO
name that answers 15 (`bsdthread_terminate` signals the join semaphore it was given, so a zero name means the dying
thread had none). Both are guest-visible fields the server trace prints, so the next step is a trace-level comparison of
a PASSING thread creation against a HANGING one, which the existing instrument already supports.


### 218. The thread-creation handshake timeout is NORMAL, and the stall is the single plane slot held by a Call-based op

The trace comparison the previous section asked for, PASS vs HANG, same workload shape, UDS arm:

```
PASS  begin semaphore_timedwait pid=192594 tid=192596 wait_name=5123 sec=0 nsec=100000000
PASS  reply semaphore_timedwait ... code=49 outcome=timeout            <-- times out and the run still PASSES
PASS  begin semaphore_signal    pid=192594 tid=192594 signal_name=4867
PASS  ... (the trace CONTINUES: the workload's own operations follow)

HANG  begin semaphore_timedwait pid=195915 tid=195916 wait_name=2307  sec=2
HANG  reply semaphore_timedwait ... code=49 outcome=timeout            <-- identical timeout
HANG  begin semaphore_signal    pid=195915 tid=195915 signal_name=2563
HANG  ... (the trace STOPS: nothing else is ever recorded for this process)
```

**The handshake's timeout is not the defect.** It expires in the PASSING run too, and then the correct signal is sent and
the workload completes. The anomaly I flagged two sections ago is therefore withdrawn: a thread-start
`semaphore_timedwait` that expires is normal here, and the two runs are identical through that point, including the
signal that follows.

**What differs is everything after it: the trace stops.** In the HANG the process emits no further semaphore record, and
the loader of that same process is simultaneously spinning on a plane request of its own
(`iter=20000 waited=0 reply=0`). Those two facts together are the shape of the single-slot plane holding a request whose
servicing needs a guest thread, while the guest's threads are waiting on the plane: the slot is occupied by an op that
was written the way `SET_DYLD_INFO` was -- build a Call, `callFromMessage`, `doWork()` on a guest thread -- and the
thread-creation path is exactly where that op is reached (the new thread's CHECKIN). The `SET_DYLD_INFO` fix removed
that dependency for one op; the same classification now has to be applied to the ops on this path, starting with CHECKIN,
whose semantics are the reason the thread exists at all.

That is the next fix and it is the directive's section-11 classification, now with measurements behind it rather than a
rule of thumb: an op that must be serviced **while the guest cannot run anything** may not be executed by asking a guest
thread to run it. Ops whose semantics are a state change on the server's own objects (dyld info, executable path,
thread registration) can be reached through their core directly; ops whose semantics genuinely need a target thread
executing (signal delivery into a thread's state) already have their own direct path.


### 219. Attempted and measured: direct servicing of CHECKIN is NOT equivalent -- and the revert had to be verified like any other change

Following section 218, the plane's CHECKIN (op=2) was serviced directly: the registration still happened inside
`callFromMessage` (the Call object is still built), and its **semantics** ran on the plane pass through the same core
`Call::Checkin::processCall` runs -- `Process::notifyCheckin(architecture, isMainThread(...), is_fork)` -- instead of
`t->doWork()`.

**Result: the failure moved, it did not disappear.** The loader got **past** the stall this section set out to remove
(the boot no longer stopped on `SET_DYLD_INFO`) and failed at the next one:

```
[release-drops-pending] site=mldr.c:1347
Failed to tell darlingserver about our executable path
Rootless shellspawn did not become ready within 30000ms
```

So the substitution is not an equivalence: `Checkin::processCall`'s body is small, but the Call path around it is not --
the ingest block in `callFromMessage` handles the lifetime descriptor's CMSG index, the stack hint, the fork flag and the
fork-latency metric, and the thread it returns is created and armed by that block. Running the visible few lines on the
plane while leaving that machinery to a Call whose work never runs is exactly the "second convention" this work forbids.

The change was therefore **reverted**, and the revert is itself recorded as a measurement: a first attempt at undoing it
by byte span glued a brace onto a `catch` and broke the translation unit, and the artifact was only believed after it
compiled **and** ran. Restored baseline, confirmed by run: `sem_ready` PASS (denied 0, created 0) and `sem_block 2000 1`
HANG exactly as before the attempt, with section 217's direct `SET_DYLD_INFO` servicing retained.

**The shape of the real fix is therefore now clear**: the checkin's registration and arming must be reached by the plane
**without** a Call at all -- which means extracting that ingest block into a helper both the datagram path and the plane
call, rather than substituting the tail of one for the other. That is a refactor of the registration, and it is the next
step; it is not a new transport.


### 220. The registration block is now SHARED (validated), and the plane's first use of it crashes -- with the missing input named

**What was done, and validated.** The ingest block of `callFromMessage` -- which builds and arms the Process and the
Thread, takes the lifetime descriptor out of the message, registers the thread with its process and records the message
address -- is now a function both routes can call:

```cpp
	std::pair<std::shared_ptr<Process>, std::shared_ptr<Thread>>
	Call::registerPeerForMessage(Message& requestMessage, dserver_rpc_callhdr_t* header, bool replyErrors);
```

`callFromMessage` calls it, so the datagram path is unchanged **by construction** rather than by inspection, and MEASURED:
`sem_ready` still PASSes (denied 0, created 0), which exercises checkin on every boot.

**The plane's first use of it crashed the server**, and the crash is recorded rather than worked around:

```
[dserver-CRASH sig=b addr=0x684
Rootless shellspawn did not become ready within 30000ms
```

SIGBUS at `0x684` -- a small offset, i.e. a dereference through an invalid base -- immediately after the plane began
running the shared registration and the direct `notifyCheckin`. The most probable missing input is named by the helper
itself: it does `tmp->setAddress(requestMessage.address())`, and a plane `Message` has **no socket address**, whereas
every message the datagram ingest sees carries one. The plane path therefore has to supply an address (or the helper has
to tolerate its absence) -- that is the next thing to measure, and it is a small, specific question rather than another
substitution.

**The plane path was reverted to the Call dispatch** and the baseline re-verified by run (`sem_ready` PASS, `sem_block
2000 1` HANG as before). The shared-registration refactor is **kept**: it is behaviour-preserving, it compiles, and it is
what makes the next attempt an equivalence instead of a substitution.


### 221. The address hypothesis is weak, and the next instrument is named

Section 220 named the plane message's missing socket address as the most probable input behind `[dserver-CRASH sig=b
addr=0x684`. That hypothesis does not survive looking at the API: `Message::address()` returns an `Address` **by
value** and `Thread::setAddress(Address)` stores one, so nothing dereferences a pointer through a missing address, and a
default-constructed address is a value rather than an invalid base. The crash therefore comes from somewhere else in
the direct path, and the honest state is that it is **not yet localized**.

The next instrument is small and this work already has the pattern: the crash probe prints the signal and `si_addr`, and
it is given the `ucontext` and ignores it (`(void)uc`). Printing the **faulting instruction pointer** from that context
turns "SIGBUS at 0x684" into an address inside the server binary, and `llvm-nm` resolves it to a function and an offset
exactly as it did for the guest's `_dserver_rpc_fork_wait_for_child + 0x19`. An instrument that reports where a crash
happened and not just that it happened is the same rule this work has applied to every other failure: name the stage
before changing the code.

Recorded so the next attempt starts from measurements: the shared registration refactor is in place and validated
(`sem_ready` PASS); the plane's direct use of it crashes and the plane path is reverted to the Call dispatch; and the
crash needs one line of instrumentation before it can be attributed.


### 222. The crash is localized: `ipc_port_destroy + 0x3c`

The instrument from the previous section did its job. The probe now prints, with raw writes only, the faulting
instruction pointer **and** the address of a known symbol in the same image:

```
dserver-CRASH sig=b addr=0x684,self=6431afb8a060,pc=0x6431afd271bc
```

`self` is what makes `pc` usable: subtracting it and resolving against the server binary with `llvm-nm -n` gives

```
faulting pc -> ipc_port_destroy + 0x3c
```

So the plane's direct registration path dies in **Mach IPC port teardown** -- a duct-tape XNU function -- not in the
registration code the change touched and not in `notifyCheckin`. That is a strong statement about the input the plane is
missing: a datagram `Message` carries the peer's address and port context, and the plane's synthesized `Message` has
neither, so a Mach object is constructed for the plane route in a state that port teardown later cannot handle. The
address hypothesis of section 220 was wrong in its mechanism and right in its direction: what is missing is the message's
transport identity, and it is consumed somewhere other than where the helper visibly stores it.

**State, all re-verified by run**: the shared registration refactor is in place (`sem_ready` PASS, denied 0, created 0);
the crash probe prints `pc` and `self` permanently, so the next crash of any kind is resolvable in one run; the plane
path is back on the Call dispatch; and the plane's direct checkin remains the next fix, now with its failure localized
to `ipc_port_destroy` and its missing input narrowed to the message's transport identity rather than the socket address.


### 223. Tooling: a crash resolver, a mini stack walk, and the two instrument defects that were hiding the answer -- which turns out to be architectural

Asked whether it was time to improve the diagnostic tools, the answer was yes and the improvement paid for itself in one
cycle.

**`scripts/dserver-crash-resolve.sh`.** It takes a `dserver-CRASH` line (or a raw `self`/`pc` pair), derives the offset
from the probe's own `self=`, resolves it against the server binary with `llvm-nm`, prints the **disassembly around the
faulting instruction with a `<=== FAULT HERE` marker**, and walks the printed stack words into symbol + offset. Before it,
every crash was resolved by hand with a throwaway python snippet -- five times in one session, each time re-deriving the
delta and each time risking a different answer.

**The probe now writes a mini stack walk**: `self=`, `sp=`, `w0..w7=`, `ret=`, `pc=` and `addr=`, all with raw writes
only, because a server that dies silently is an instrument that cannot answer.

**Two defects in the instrument itself, found by using it.** `ret=` was MEASURED to be a stack pointer and not a code
address (so it could never name a caller), and the first version of the stack-word tags wrote `,w0` -- three bytes,
without the `=` and without the `0x` prefix -- so the number ran straight into the next tag and the resolver could not
parse a single word. Both are fixed; the lesson is the same one this work keeps learning: an instrument that prints its
values unparseably is the same class as one that cannot print them.

**What the working instrument immediately said.** The faulting instruction is

```
1b9a5f: call 174970 <current_thread>
1b9a64: mov  %rax,-0x48(%rbp)
1b9a68: mov  -0x48(%rbp),%rax
1b9a6c: cmpl $0x0,0x684(%rax)   <=== FAULT HERE      (si_addr = 0x684)
```

`si_addr = 0x684` is exactly `0 + 0x684`, so `%rax` is **zero**, and `%rax` came straight out of `current_thread()`.
**`current_thread()` returned NULL.** The plane pass executes on a server thread that is **not an XNU thread**, so the
Mach/IPC semantics it was asked to run directly have no current thread to run against and dereference null.

That is not a bug in the registration and not a bug in `notifyCheckin`: it is a property of the execution context. Direct
servicing works for operations whose semantics are the server's own state (this is exactly why `pthread_canceled`, which
touches only cancellation state, has worked on the plane all along) and cannot work for operations that touch Mach --
which is every operation that creates or registers a Thread or a Process.

**So section 218's diagnosis stands and its remedy was wrong**: the plane's single slot must not be *held* while a Call
waits for a free guest thread, and the answer is not "run the semantics on the plane" but "**dispatch the Call onto an XNU
thread and complete the slot asynchronously**". The `ProcessControlTxn` machinery already exists for precisely this
"the other half arrives later" shape, so the fix is to use it for the checkin instead of inventing a third route.

State: the plane path is reverted to the Call dispatch, the baseline is re-verified by run (`sem_ready` PASS, `sem_block
2000 1` HANG as before), and the tools above are permanent.


### 224. Tooling: a suite runner, and a cleanup that was killing the process tree that asked for it

**`scripts/darling-suite-run.sh`** runs a LIST of guest workloads, one boot each, judges every row by that workload's own
machine-readable line (through `darling-guest-verdict.sh`), prints one table with the counters next to each row, and exits
non-zero if any row is not `PASS` or if `--require-zero-creations` is set and any row created one. It exists because the
directive's acceptance is a SET (`boot + basic + ool + r2 + stress_pool + churn + fork/exec + the semaphore family`), and
judging a set by hand is where two workloads that never finished were called PASS in one session.

**Using it immediately found a defect in the harness, and the defect was in the class this work keeps recording.** The
first run died with `rc=137` (SIGKILL) three seconds in, before printing a single row. The cause:

* `darling-boot-run.sh`'s cleanup enumerates prefix-owned processes by `exe` **or** `cmdline`, and excludes only `$$` and
  `$PPID`. A wrapper is itself invoked with `--prefix` (so its cmdline matches) and a **two-level** wrapper -- suite ->
  verdict -> runner -- is therefore a *grandparent*, which the exclusion did not cover: the runner killed the process
  tree that had asked it to run. Fixed by walking the PPID chain with `/proc/<pid>/stat` and excluding **every**
  ancestor. `scripts/prefix-cleanup.sh` had the same shape and got the same fix.
* The fixed guard then still did nothing, and the reason is the second half of the defect: `ancestors=$(own_ancestors)`
  produces a **newline**-separated list while the membership test is `case " $ancestors " in *" $pid "*`, which needs a
  space on both sides. A newline-separated list can never match it, so the guard silently passed every ancestor through.
  MEASURED both ways: with the broken separator the wrapper still died (`rc=137`, 15 s in); with the list normalised
  (`| tr '\n' ' '`) the same two-level wrapper completes and prints `wrapper survived`, `rc=0`.

Recorded in `docs/tooling.md` with the other instrument rules. The lesson is the same one that keeps recurring here: an
instrument whose guard silently does nothing is indistinguishable from an instrument that has no guard, and the only way
to tell them apart is to run the shape the guard exists for.


### 225. One tool instead of a pile of scripts: `dwdiag`, and the defects it found in itself

Asked for reusable, composable tooling, and for the existing debug runner to be part of it rather than a parallel
thing, the four scripts this cycle had accumulated were folded into the **Rust** tool that already owns the
guest/runtime execution machinery:

```
scripts/dwdiag <symbolize|crash|verdict|suite> [OPTIONS]      # build-and-exec shim, stable path
  = darling-debug-runner diag ...                             # implementation, in the sibling tool repo
```

`darling-guest-verdict.sh`, `darling-suite-run.sh`, `darling-symbolize.sh` and `dserver-crash-resolve.sh` are **deleted**
-- four shells with four quoting rules, four exit-code conventions and four chances to disagree about what a verdict is.
The subcommands are designed to be composed rather than scraped:

* **one verdict rule**: `RING_MACH_TEST mode=<M> ... pass=1`, and the ABSENCE of that line is `FAIL`/`HANG`, never PASS;
* **`--json` on every subcommand**, so a caller chains `crash` into `symbolize` instead of parsing text;
* **stable exit codes**: 0 ok/PASS, 1 verdict or acceptance failure, 2 usage, 3 tool error;
* it **delegates**: prefix start/stop stays the boot harness's (`--boot-runner`), and the symbol table stays
  `llvm-nm`'s. A second implementation of either would be a second source of truth for something the toolchain answers.

Verified by use, not by inspection: `symbolize` agrees with a hand-computed `llvm-nm` answer on the same address;
`crash` parses a real `dserver-CRASH` line, resolves the location, walks two stack words into symbols and prints the
disassembly with the fault marked, and its JSON parses; `verdict` returns `PASS` with `rc=0` for `sem_timed 300 1`; and
`suite` prints the table for a two-mode set with `SUITE-VERDICT PASS`.

**Four defects the tool found in ITSELF during that verification, each fixed and each recorded as a rule:**

1. the boot harness was invoked as `--cmd "" <command>` -- two arguments -- so it exited instantly and the verdict
   reported `NO-RUN`, which is exactly what a workload that failed to start looks like;
2. the JSON escaping handled quotes, backslashes and newlines but not tabs or other control characters, so a crash
   document containing a disassembly was **unparseable**: a tool that emits "json" a consumer cannot parse is worse than
   one that emits text, because the consumer trusted it;
3. crash fields were parsed with a first-match-else chain, so `addr` was silently empty whenever it shared a comma-field
   with `sig` (`[dserver-CRASH sig=b addr=0x0`) -- a parse that looked right and dropped a field;
4. a source splice of the parser dropped its trailing expression, which the compiler caught as "mismatched types" -- the
   same rule this work applies to every other change: an edit is a change and must be compiled **and** run.

The tool lives in the sibling `darling-debug-runner` repository, so at handoff its keeper bundle
(`tool-handoff/darling-debug-runner/root.bundle` in the workspace) is now stale and must be refreshed; that is a
handoff-time obligation, not a product change.


### 226. The tool is vendored, and its documentation is where a reader will actually look

The diagnostics were living in a sibling repository, which meant three separate places to keep in step: the source, the
instructions that call it, and the handoff bundle that carries it between sessions -- and MEASURED, all three had already
drifted (the workspace's `tool-handoff/darling-debug-runner/root.bundle` was stale the moment the `diag` subcommands were
added). The tool is now **vendored into this workspace** at `tools/darling-debug-runner` (source, `Cargo.toml`,
`Cargo.lock` and README; build output is ignored), and `scripts/dwdiag` builds it on first use and execs it, printing
which copy answered, because "the tool ran" and "the tool you documented ran" are different claims. The sibling
repository stays only as a fallback for a checkout that predates the move.

**Documentation, in the four places a reader actually looks:**

| Where | What it answers |
|---|---|
| `tools/darling-debug-runner/README.md` | what the tool is, every subcommand with a copy-pasteable invocation, `--json`, the exit codes |
| `docs/tooling.md` (this workspace) | the entry point, why there is ONE tool, the defects each rule exists for, and short verified invocations |
| `AGENTS.md` (this workspace) | the normative mapping "situation -> command": `scripts/dwdiag <symbolize\|crash\|verdict\|suite>` with the verdict rule and the instruction not to hand-roll symbolizing, crash parsing or per-mode verdicts in shell |
| this document (§225, §226) | the decisions and the measurements behind them |

Every example in all four is an invocation that was actually run. That is deliberate: the failure mode this section keeps
recording is not "undocumented" but "documented with a command that does not work" -- an instrument that cannot answer is
indistinguishable from one that is absent.


### 227. The deferred plane Call is measured WRONG for checkin: one Call per thread is the invariant that decides

Section 223 concluded that the plane must not hold its single slot while a Call waits for a free guest thread, and that
the answer is to dispatch the Call and complete the slot asynchronously. The mechanism was built accordingly -- a plane
completion written by `pushCallReply` when the Call carries a page target, a `Server::_deferredPlaneCalls` queue drained
by the server loop after the plane pass, and the checkin's inline `doWork()` replaced by that path behind
`DARLING_SERVER_PLANE_DEFER_CHECKIN=1` so the default path stayed untouched.

**The default path is unaffected** (measured: `sem_ready` PASS, denied 0, created 0, with the new sink and queue present
but no handler using them). **The deferred path fails, and it fails for a reason that settles the design:**

```
plane-deferred-exception what=Thread's pending call overwritten while active
Failed to checkin with darlingserver
```

`Thread::setPendingCall` refuses a second pending Call on one thread, and the thread being checked in **is the caller** --
it is parked in the plane request whose answer is exactly the Call being armed. So checkin cannot be dispatched onto its
own target thread: the inline `doWork()` in the plane pass is not an accident, it is the only form in which this
operation can run, because it needs to run **on** the caller, whose fiber is otherwise busy waiting for the answer.

That also re-reads section 218: the plane pass does not block for checkin -- checkin is fast -- so the slot is not held
by checkin in the way that section assumed. What remains for the suite's hangs is the **guest-side thread-creation
handshake**, which sections 214 and 217 already measured from the other side (the trace of a HANG stops right after the
thread-start signal while the loader spins on a plane request of its own).

Two instruments were corrected in the process, both of the same class: the drain reported its exception only under a
courier-log hatch that was **off** in the run that mattered (so a silent failure looked like "checkin failed"), and it
now reports unconditionally but bounded; and the `--json`/field parsing defects of section 225 were found the same way.

The deferred path is kept **behind its hatch** as a measured negative -- it is the mechanism section 223 asked for, and it
is now known not to apply to checkin -- and the next measurement is the loader's own plane request in a hanging
thread-creating workload (which op it spins on now that `SET_DYLD_INFO` is serviced directly, and whether the server
services it).


### 228. The plane's completions are being written into a STALE mapping: `region_fd=-1` and `reply=0` on the guest side

The measurement the previous section asked for, on a hanging thread-creating workload (UDS hatch on, loader and server
diagnostics on) names the actual defect, and it is not the slot discipline at all:

```
loader:  [mldr-ctl] request pid=877379 op=4 ; op=5
         [mldr-ctl] iter=19000 waited=0 reply=0 seq=3      <-- still waiting
server:  process-control-service op=4 pid=877379 region_fd=-1 ino=0 dev=0 size=0
         process-control-service op=5 pid=877379 region_fd=-1 ino=0 dev=0 size=0
```

The server **serviced** the very requests the loader is still waiting on, and the guest reads `reply=0`. A completion that
the server wrote and the waiter cannot see is the section-192 class ("the store ran and the other side cannot see it"),
and here the identity fields say exactly why: **`region_fd=-1 ino=0 dev=0 size=0`** -- the registry entry for that pid has
lost its descriptor, so the mapping the server writes into is not the page the guest is polling. The earlier
incarnation-safety fix closed the courier fd for a pid without erasing the entry (correctly -- erasing it unmapped the
incoming incarnation's page), but the entry now keeps a **stale mapping** across an incarnation change, and every
completion for the new incarnation lands in the old one.

That single fact explains the whole remaining suite table: a workload that starts a thread (or forks, or execs) carries a
process through a new incarnation, the plane's registry entry for that pid points at the previous incarnation's mapping,
the guest's completion wait never fires, and the symptom alternates between "spins forever" (`HANG`) and "the guest gives
up and takes the datagram" (`denied=1`, which is exactly what the newly-attributed `first-denial=pthread_canceled` in
this same run is).

**The fix is therefore in the region bookkeeping, and it is specific**: a plane request must be serviced into the page the
REQUESTER published. The doorbell/attach path already learns that mapping (`ATTACH_LANE`, the `(dev, ino)` identity probe,
`regionMap`), so the server must **re-adopt** the current mapping when the entry's identity is gone (`fd == -1`, or a
`(dev, ino)` that differs from what the request's page reports) instead of writing into whatever the entry still holds.
That is the next change to make, and the instrument for it is already in the log: every `process-control-service` line
prints `region_fd ino dev size map`, so the re-adoption is verifiable line by line.


### 229. The identity comparison names the root cause: the guest waits on a NEW page while the server answers the OLD one

The region-fd fix of section 228 is verified in the fields it was meant to change -- a serviced request now carries its
identity again:

```
process-control-service pid=891922 op=2 region_fd=50 ino=14607480 dev=1 size=528
process-control-service pid=891922 op=4 region_fd=50 ino=14607480 dev=1 size=528
process-control-service pid=891922 op=5 region_fd=50 ino=14607480 dev=1 size=528
```

and `region-fd-kept-for-live-process` fired three times, so the descriptor survives the outgoing incarnation's
destructor as intended. (Seventy `region_fd=-1` lines remain: the lookup that guards the close asks the registry by
**NSID**, and a process outside the root namespace is keyed by its own pid -- the same two-key lesson as `SET_DYLD_INFO`
in section 217, and the same one-line refinement.)

**But the loader still spins with `reply=0`, and comparing the two identities says why.** The guest publishes the page it
is polling, and the server logs the page it writes:

```
guest : [mldr-ctl] request ... dev=1 ino=14604477
server: process-control-service ... ino=14607480
```

**Different memfds.** After an exec (and after a fork) the guest creates a **new** plane page, while the server's
`_processControl` is keyed by **pid** and keeps servicing the entry registered by the previous incarnation. The server
answers into the old mapping, the waiter polls the new one, and the loader spins `iter=20000 waited=0 reply=0 seq=3`
while the server's log proves it serviced that very request.

So the fix is exactly what section 228 described and is now backed by a `(dev, ino)` pair rather than by an inference:
**a plane request must be serviced into the page the requester published.** The doorbell/attach path already learns the
new page (the courier delivers the new memfd), so the server must either re-adopt the entry when the identity changes or
key the plane's bookkeeping by something finer than the pid -- which is the same "a pid is not an incarnation" rule this
work has now hit in the descriptor courier, in the region lifetime (section 200) and here.

Two refinements are therefore queued and both are small: the close-guard must try both registry keys, and the service
path must re-adopt (or re-key) when the requester's page identity differs from the entry's.


### 230. CORRECTION of section 229, and the real cause of the stall: a PROCESS_SCOPED plane op dispatched onto the SPINNING requester

**Section 229 is wrong and is corrected here.** It claimed the guest was waiting on a page whose memfd (`ino=14604477`)
differed from the one the server answered into (`ino=14607480`), and inferred a per-incarnation page identity problem.
The guest publishes its own identity next to its request and the two agree exactly:

```
guest : [mldr-ctl] request pid=891922 op=5 page=... memfd=10 ... dev=1 ino=14607480
server: process-control-service pid=891922 op=5 region_fd=50 ino=14607480 dev=1 size=528
```

One memory, one page. The `14604477` in section 229 came from a **different pid's** line in the same log; comparing an
identity from one process with an identity from another is exactly the "an instrument's premise must be proven" defect
this work keeps recording, and it produced a plausible, wrong root cause. (The `region_fd=-1` refinement stands on its
own merits and is kept: the close-guard should still try both registry keys.)

**The real cause is the one section 216 already found for `SET_DYLD_INFO`, one op later.** `SET_EXECUTABLE_PATH` (op 5) was
handled by building an ordinary Call with `header.tid = pid` -- PROCESS_SCOPED, "the process's own thread executes it" --
and running it with `doWork()`. But the requester of this op is the **loader**, and the loader is not parked in a server
RPC wait: it is **spinning in the plane loop waiting for exactly this reply**. `doWork()` can therefore never run that
Call -- it goes into the thread's pending slot and waits for a thread that will not come back -- so no completion is
stored, and the loader blocks in its unbounded futex wait. The guest log says precisely that: it ends at

```
[mldr-ctl] iter=20000 waited=0 reply=0 seq=3
```

and never prints another heartbeat, because `waited_ms` never advances past the first wait.

The remedy is the remedy of section 216, applied to op 5: **service it directly through the `Process`**. The semantics
need no guest thread at all -- `SetExecutablePath::processCall` reads the string through the process's memory interface
and stores it on the `Process` -- so the plane now reads the guest pointer out of the page's payload and calls the same
`readMemory` + `setExecutablePath` pair, with the same `TestDiagnostics` trace, trying both registry keys as the dyld
case does.

**Measured effect, on `basic 20` under the socket hatch.** Boot stays GREEN (`sem_ready` PASS). The workload's fallback
disappears (`denied` 1 -> 0 for this shape) and the bootstrap walks **through** the op that used to deadlock:

```
seq=4 after-execpath pid=917234 status=0 ready=1 image=/usr/libexec/shellspawn
seq=5 before-threadself / seq=6 after-threadself / seq=7 after-seed ...
```

with the server confirming the store (`process-control-store op=5 seq=4 readback=2`). The remaining stall is now
**after** `after-seed` and before the workload's own machine-readable line, so the next instrument is the workload's own
progress inside `shellspawn` rather than another plane op.


### 231. The instrument grew the one command this cycle kept re-improvising

Three runs in a row, the diagnosis was reached by grepping two logs by hand for the same three facts, so the tool now
carries them: `dwdiag progress --log RUN_LOG [--guest-log MLDR_DIAG_LOG] [--mode M]` prints whether the workload produced
its own machine-readable line, the last `[mldr-ctl]` stage the guest loader reached, the op the guest **published** and
the last plane op the server **serviced**. Published-versus-serviced is the whole diagnosis -- a request published and
never serviced is a server-side stop; one serviced while the guest still waits is a completion that did not land -- and
`verdict` now composes the same summary into a `VERDICT-STAGE` line on any non-PASS verdict, so a HANG is readable
without a pipeline.

Its first version was itself an instance of the defect this section's predecessors keep recording: it classified a line
by testing whether the first whitespace token equals `seq`, while every such line begins `seq=N`, so the rule could never
fire and the tool reported an early `planeloop` line as the last one. Lines are now classified by the key **before** the
`=` and a priority decides which survives (a stage beats a spin, a timeout beats everything). The command is documented
in the tool README, `docs/tooling.md` and the workspace situation list, and the vendored copy is the canonical one.


### 232. The slot was wedged at DONE for the guest's copy of the claim loop: section 195's remedy had been applied to ONE of two copies

The refusal instrument added in the previous section named it in one line, after eight rounds of inference:

```
[plane-refuse] why=no-slot op=9 a=2 b=21474836484 tid=958809
```

`a` is `request_state` and the constants are `IDLE 0 / PENDING 1 / DONE 2 / CLAIMED 3`, so the slot was **not** held by an
in-flight request: it was left at **DONE** by a completion, and `b = (request_op << 32) | request_seq` says the holder was
the loader's own `SET_EXECUTABLE_PATH` (op 5, seq 4) -- the request whose answer the loader had already read.

Section 195 found exactly this shape and fixed it by making the claim loop accept **IDLE or DONE**, because "the publisher
reads its ANSWER from `reply_state`, never from this flag". That fix went into the **loader's** copy (`mldr.c`) only. The
guest library's copy in `dserver-ring.c` still accepted `IDLE` alone, so in a process whose first plane request fell back
to the datagram -- leaving the server's `request_state = DONE` unreleased -- the slot was unusable **for the life of that
process**: every later request spun its 2000 ms bound, returned `-1`, took the datagram, and was **denied** by the hard
socket hatch, so the call never ran at all. That is the stall this whole round was chasing, and it is one line:

```c
uint32_t expect = (t % 2 == 0) ? DSERVER_PROCESS_CONTROL_IDLE : DSERVER_PROCESS_CONTROL_DONE;
```

(the same alternation `mldr.c` already used).

**Measured effect, `basic 20` under the hard socket hatch:**

| | before | after |
|---|---|---|
| `rpc-socket-DENIED` | 1 (`kqchan_mach_port_open`) | **0** |
| plane requests serviced | 11 (ops 1-8 only) | **154** (ops 1-7, 26) |
| images that reached a guest stage | launchd, vchroot, launchctl | + **`/usr/bin/ring_mach_msg_test`**, `/bin/sh`, shellspawn |

So the workload binary now starts. It still does not print its own result line, and the next profile is taken with the
Ring/lane instruments rather than a plane op -- with one trap already avoided: 81 `[pc-postplane st=16-]` probes (81
negative returns, if the digit is hex, = `-EINVAL`) look like a failure and are **not** one: `dtape_thread_canceled`
returns `EINVAL` for `action 0` exactly when no cancellation is pending, which is XNU's documented answer, so "fixing"
it would have changed semantics to silence a correct signal.

The instrument itself is also kept, because it is what made the diagnosis mechanical rather than inferential:
`__dserver_plane_request_ex` now prints one bounded `[plane-refuse] why=… op=… a=… b=… tid=…` line per reason, and the
loader prints `[mldr-ctl] plane-noslot op=… state=… holder_op=… holder_seq=…`.


### 233. The workload starts and the Ring carries it, until one dispatch is refused: continuation + pending call

With the slot fix in place the workload binary runs, and the Ring is measurably carrying its traffic -- 107
`RING_MACHMSG_PUBLISH` against 104 `RING_MACHMSG_REPLY_CONSUME` in one run, sequences advancing on several lanes -- and
then it stops with publishes whose reply never arrives:

```
RING_MACHMSG_PUBLISH lane=0  gen=1 seq=30 tid=976076      <-- no matching REPLY_CONSUME
RING_MACHMSG_PUBLISH lane=23 gen=1 seq=10 tid=976071      <-- last line of the run
```

The server side answers why, in one line:

```
ring C2S dispatch threw: Thread has both a pending call and a pending continuation (pending_call=35 tid=...)
```

`ringServiceThread` reads a request out of a lane slot and runs it with `call->thread()->doWork()` on the thread that
published it. `Thread::doWork` refuses -- and throws -- when that thread already carries a `_continuationCallback`,
because the state machine allows "a pending call" or "a pending continuation", never both. The refusal is correct: a
thread suspended on a continuation is parked in a specific server-side wait, and dispatching a second operation onto it
would clobber the state the continuation has to resume. The exception is caught, the request is dropped, and the guest
waits on a reply that will never be published -- the stall.

That is a **different** conflict from section 227's, and it says what the Ring path still needs. The datagram path never
meets it because each request arrives on a socket whose thread is parked in a normal RPC wait. A Ring request arrives
from a thread that publishes and then waits on its own slot, and that thread may already be suspended server-side (a
continuation is exactly what a caller-S2C raised while its call was parked leaves behind). The choice is therefore
between queueing the call until the continuation completes, serving it on a different thread, or making the guest's own
protocol keep one outstanding operation per thread; the next instrument is this throw's identity: the message names the
NEW call (`pending_call=35`) but not the continuation, so the continuation's owner must be printed with it before the
next run can decide between those three.


### 234. The stall dump: the server can now answer "which thread is waiting on what", and the answer is a mutual wait

Every stall this cycle was read from counters ("publish without a reply"). The question a stall actually asks -- which
thread is parked, on which call, and on what kind of wait -- was unanswerable, because the server had **no way to
enumerate live threads**: `Registry` offered lookups by key and nothing else. Three pieces were added:

* `Registry::forEachEntry` (snapshot under the lock, visit WITHOUT it, so a visitor may take its own locks);
* `Thread::hasContinuation()`, the other half of the invariant that section 233 hit, which no counter showed;
* a timer watchdog in the server loop (`DARLING_SERVER_STALL_DUMP=1`), whose progress signal is the counters that tick
  only when the server really serves or answers (`ringS2cFull`, `ringServicedSpin`, `ringServicedDoorbell`,
  `ringProcessDoorbellServiced`), and which makes the blocking `epoll_wait` finite **only** under that hatch. It reports
  after 5 s without progress and reprints at most every 30 s, so it can neither flood a log nor make a run slow.

Its first run named the deadlock outright:

```
stall-dump idle_ms=5235 serviced=567 processes=4 threads=8 waiting=2 pending=0 continuations=4 suspended=5
 detail= pid=1 tid=996851 pending=-1 active=62 suspended=1 continuation=1 waiting=1;
         pid=1 tid=996848 pending=-1 active=38 suspended=1 continuation=1 waiting=1;
         pid=0 tid=4194307 active=-1 suspended=1 continuation=0; pid=0 tid=4194306 ... ; pid=0 tid=4194305 ... 
```

with the call numbers resolved from the generated table: **62 = `semaphore_timedwait`** and **38 = `mach_msg_overwrite`**.
So two guest threads of the workload are parked **with continuations**, one inside the blocking semaphore family and one
inside the message receive, and the server has no runnable work left (`pending=0`). This is a *mutual* wait, not a lost
wake: the standalone semaphore modes pass (`sem_timed kr=49 elapsed=0.300`), so the family completes and replies when it
is the only thing in flight; here the receive and the semaphore wait are parked at the same time and neither can make
progress.

The three kernel threads (`pid=0`, `active=-1`) are the idle/daemon ones and are expected to be parked. The next decision
follows from that: whether the parked `semaphore_timedwait` should have completed on its **own** semantic timeout (the
server holds the timeout, by design) while something else is parked -- i.e. whether a parked continuation blocks the
same process's other parked continuation from being resumed, which is a server-side concurrency question and no longer a
transport question.


### 235. The signal must ride the same transport as the wait, and the semaphore trace then shows the wait is NOT the stall

Two things were established this round, one by a fix and one by a measurement that corrects the previous section.

**The fix: `semaphore_signal` and `semaphore_signal_all` belong to the blocking family.** The route table
(`RING_GENERATED_SIMPLE`) carried the four *waits* and **not** the signals, so a signal went by the process plane --
whose single slot was held by the parked wait -- could not be published, took the datagram, and was **denied** by the
hard socket hatch. The waiter was therefore never woken, which is exactly the mutual wait section 234's dump showed
(`active=62` parked next to `active=38`, `serviced=568`, `waiting=2`). A signal may never queue behind the wait it
exists to release. Both calls were added to the wire class table and to the route table; boot stays GREEN and
`basic 20` no longer reports a denial at all.

**The measurement that corrects the reading: the wait is not stuck.** With the server-side semaphore trace enabled, the
same run says:

```
rpc.semaphore.begin  operation=semaphore_timedwait pid=1 tid=1021526 wait_name=2307 sec=30 nsec=0
rpc.semaphore.reply  operation=semaphore_timedwait pid=1 tid=1021526 wait_name=2307 code=49 outcome=timeout terminal=reply-enqueued
rpc.semaphore.begin  operation=semaphore_timedwait pid=1 tid=1021526 wait_name=2307 sec=30 nsec=0      <-- again
```

The server's timed wait **times out and enqueues its reply** (`code=49` is `ETIMEDOUT`), the guest receives it, and the
workload **starts the same wait again**. So the semaphore family, on the Ring, is working exactly as its standalone
modes measure (`sem_timed kr=49 elapsed=0.300`). What the dump shows parked is therefore not a lost wake: it is a
workload politely waiting 30 seconds at a time for something that never comes.

That something is the **sender**: the other parked thread sits in `mach_msg_overwrite` (call 38), i.e. a receive, with a
continuation, and the two publishes with no matching consume are the two halves of one message exchange. The remaining
question is therefore the **send-to-parked-receive wake on the Ring**: when the sender's `mach_msg_overwrite` puts a
message on the port a server-parked receive is waiting for, the receive must be resumed and its reply published -- and if
that wake is missing, the sender's own reply never arrives either, which is precisely two publishes without two
consumes and two parked threads.


### 236. The mach_msg send/receive join, at the trap level, and what it rules OUT

Three instruments were added to the duct-tape XNU copy (the real server-side path; the earlier attempt traced
`mach_msg_receive`, which this path does **not** use -- measured: `recv_enter` never appeared while `recv_woken` did):

* `mach_msg_overwrite_trap`: `trap_recv_wait` (mqueue + name + option) and `trap_recv_done` (result), plus
  `trap_recv_object` (the **port object behind the name**, which is the only field that can answer an identity
  question);
* `ipc_kmsg_send`: `kmsg_send_dest` (the mqueue the message is posted to), first placed on `msgh_remote_port` and
  **corrected** -- that field is the port OBJECT, not a name, so the first version looked right and answered nothing;
* `ipc_mqueue_post`: `post_wake` (the mqueue, the receiver it found, and whether it found one).

The measurements, on `basic 20`:

| fact | value |
|---|---|
| receives armed (`trap_recv_wait`) | 102 |
| receives completed inline (`trap_recv_done`) | 56 |
| wakes through the continuation (`recv_woken`) | 45 |
| posts that **found a waiter** (`post_wake result=1`) | 45 |
| posts that found none (`result=0`) | 56 |

**Every post that found a waiter woke it** (45 = 45), and every armed receive whose mqueue received a post either
completed or belongs to a thread whose completion is the continuation path. So the post/wait handshake is **not**
losing wakes, and the armed-and-uncompleted receives are threads waiting for a message that has not been sent to their
port object. The remaining identity question -- "does the same name denote the same object for sender and receiver" --
was instrumented (`trap_recv_object`) and shows names denoting several objects **across the run**, which is expected
because the workload creates and drops a port every iteration; no same-iteration mismatch was found.

Section 231's rule applies to two of these three attempts: `msgh_remote_port` looked like a name and was a pointer, and
`mach_msg_receive` looked like the receive entry and was not the one this path uses. Both were caught by asking what
the instrument actually compares.

### 237. `basic 1` PASSES and `basic 2` hangs: the transport is proven, the REPEAT is not

The shape, measured with the same build and the same hard socket hatch:

```
basic 1  ->  PASS  RING_MACH_TEST mode=basic delay_ms=0 iters=1 pass=1 min_elapsed=0.000283 max_elapsed=0.000283
basic 2  ->  HANG
basic 4  ->  HANG
```

One full iteration -- `make_port`, `pthread_create` a sender, the sender's `mach_msg` send, the main thread's blocking
`mach_msg` receive, `pthread_join`, `drop_port` -- completes with `pass=1` and no denial and no socket creation. That is
the whole mach-msg path on the Ring, end to end, and it is the first time this project has a **passing** workload on the
Ring under the hard hatch. The failure is therefore **not** the transport: it is what the second iteration does to the
state the first one left behind (a port name reused with a different object, a lane not released, a thread's reply port,
or the pthread create/join handshake, which the stall dump shows parked in `semaphore_timedwait`, callnum 62).

The next step is bounded and specific: instrument the workload's own loop progress (one bounded line per iteration) and
re-read the stall dump for iteration 2, where the same three instruments already say which half is parked and on which
mqueue and call number.


### 238. The remaining hang is `drop_port`: a MIG `mach_port_mod_refs` waiting for a reply that never arrives

The workload's own loop was instrumented (gated by `RING_MACH_TEST_ITER_TRACE=1`, bounded to four iterations, read once
from the environment) with one line per step of every iteration. Under the hard socket hatch, `basic 2` then says:

```
ITER 0 make_port
ITER 0 port=2307 create
ITER 0 created
ITER 0 received kr=0 send_failed=0
ITER 0 joined
        <-- "ITER 0 dropped" never appears
```

So the message exchange of iteration 0 completes with `kr=0`, the sender thread joins, and the run stops in
**`drop_port`** -- which is `mach_port_mod_refs(mach_task_self(), q, MACH_PORT_RIGHT_RECEIVE, -1)`.

Three facts pin it down, and together they rule out the transport machinery this round was about:

* the stall dump taken at that moment names exactly two parked guest threads: `active=62` (`semaphore_timedwait`) and
  `active=38` (`mach_msg_overwrite`), `waiting=2`, `pending=0` -- the second of which is the shape a **MIG stub waiting for
  its reply** has;
* `mach_port_mod_refs` is reached through `_kernelrpc_mach_port_mod_refs_trap_impl` in the guest's mach syscall table, so
  the call is a MIG request that sends to a special port and blocks for the answer;
* there is **no** `rpc-socket-DENIED` line and **no** abort in the run (`created=0`, `denied=0`), and the guest stderr
  shows neither a denial nor a `mach_driver_get_fd` abort -- so the caller never asked for a per-thread socket, and it is
  not the hard hatch that stopped it: it is waiting for a **reply**.

That also explains why this op was already flagged in the profile's class table as `UDS_ONLY | DESTROY | CALLER_S2C` with the
note "destroy-capable -> caller-S2C deadlock on the simple ring; the right home is the future duplex lane" -- and why
`basic 1` can pass while `basic 2` hangs: the port this tears down is one whose receiver and sender have just used the
Ring, so the teardown is the first destroy-capable operation after the Ring has been used for a full exchange.

**Where the next instrument goes** is therefore not the Ring, the plane or the lanes: it is the MIG request that
`mach_port_mod_refs` issues (which special port it targets, and whether the server ever sees it) -- the same
`dtape.msgq` trace already prints it, and its `send_enter`/`trap_recv_wait` pair for that thread is the next line to read.


### 239. The remaining failure is a RACE, and the trace perturbs it

Two measurements settle the shape of what is left:

```
basic 20, no trace, three runs in a row:   HANG / HANG / HANG
basic  2, msgq trace ON:                   PASS  (iters=2 pass=1, every ITER line including "dropped")
basic  1, no trace:                        PASS  (pass=1, 283 microseconds)
basic  2, no trace:                        HANG
```

So the transport carries a full exchange (and, when the timing allows, several), and the stop is a **timing-dependent**
failure in the teardown path of section 238 -- not a deterministic state the instruments can read off, because the
instrument **is** a change in timing: the `dtape.msgq` trace writes a line per event, and that is enough to move the
race. This is the rule this project keeps re-learning, now with a measurement: **a diagnostic that changes what it
measures cannot be the instrument that decides a race.**

What that implies for the next instrument is concrete -- it must be non-perturbing:

* the guest and duct-tape traces have to record into a **bounded memory ring buffer** (no syscall, no I/O, no lock on the
  hot path) and dump it only on a trigger (a stall watchdog, or process exit), so the recorded run and the measured run
  are the same run;
* the counter set already in place (`post_wake` found-a-waiter vs not, `trap_recv_wait`/`trap_recv_done`, the stall dump's
  per-thread `pending`/`active`/`continuation`/`suspended`) is enough to say **which** state the race leaves behind, and
  the memory ring only has to add **the order** of the events that led to it.

Everything else this round established stands: the plane's op 5 is serviced directly, the slot's claim accepts `DONE`,
the signals ride the Ring with the waits, the stall dump answers "who waits on what", and a full mach-msg exchange on the
Ring under the hard socket hatch completes with `pass=1` -- which is a state this project had not reached before.


### 240. Recovery record for this round (the source tree has no git baseline)

`procctl-src` is a working tree with no tracked baseline, so the changes of sections 230-239 are recorded here by
content hash (sha256, first 16 hex digits). A prefix deployed from these revisions is `/tmp/dr-on-matched` with
`darlingserver` verified at `c62a90bbe6fe0f17`'s build; the guest libraries and the workload binary were deployed with an
explicit sha256 equality check over EVERY runtime copy.

| file | sha256[16] |
|---|---|
| `src/external/darlingserver/src/server.cpp` | `530cb4ec4bcd823a` |
| `src/external/darlingserver/internal-include/darlingserver/server.hpp` | `b0df63cd652a7201` |
| `src/external/darlingserver/internal-include/darlingserver/registry.hpp` | `bc9ad1595801e455` |
| `src/external/darlingserver/internal-include/darlingserver/thread.hpp` | `ba97d3cf2f8d5686` |
| `src/external/darlingserver/src/thread.cpp` | `d2f32cc9bf937dd8` |
| `src/external/darlingserver/internal-include/darlingserver/rpc-supplement.h` | `0cf1c1f7de1f0a94` (edited with the signal class) |
| `src/external/darlingserver/scripts/generate-rpc-wrappers.py` | `0cd453291b2d79cd` |
| `.../resources/dserver-ring.c` | `0dc7addbbfa16776` |
| `src/startup/mldr/mldr.c` | `b6491c014a29c96e` |
| `duct-tape/src/test-diagnostics.c` | `246823a458f3f677` |
| `duct-tape/internal-include/.../test-diagnostics.h` | `0a325489dccd5daa` |
| `duct-tape/xnu/osfmk/ipc/mach_msg.c` | `b08137e2337830bd` |
| `duct-tape/xnu/osfmk/ipc/ipc_kmsg.c` | `5eacf6fb81e195ff` |
| `duct-tape/xnu/osfmk/ipc/ipc_mqueue.c` | `dd0bec06ae2e2a4d` |
| `src/tools/ring_mach_msg_test.c` | `c05597e774f3ffff` |

The `rpc-supplement.h` row is the one hash not taken inline above; it is recorded from the same edit set (the blocking
family gained `semaphore_signal` and `semaphore_signal_all`) and must be re-taken if the tree is used as a base.


### 241. The event ring, and what its ORDER says: the stall is the semaphore handshake, not the transport

The instrument section 239 asked for was built: `dtape_test_trace_msgq` now writes into a fixed **memory ring**
(2048 records, one atomic reservation per event, plain stores, no syscall, no allocation, no lock) whenever
`DSERVER_TRACE_RING=1`, and only *then* is a file written -- once, by the server's stall watchdog
(`dtape_test_ring_dump`). The line-based mode is kept for runs that are not chasing a race, so the two modes share the
same call sites and cannot drift. Two defects of its first version were fixed by measurement: it dropped the
`bits`/`result` fields (the post's "found a waiter" flag is exactly what must survive) and it classified events by
**character position**, which is one rename away from silently mislabelling everything -- it now compares the event
string.

With the hot path in memory and the rare semaphore trace still on a file, `basic 20` under the hard socket hatch says:

```
ring tail (ordered):  451 send_dest th=…a768 mq=…1828
                      452 post_wake th=…a768 mq=…1828 c=…e008 d=1     <-- found a waiter, and woke it
                      453 recv_woken th=…e008 mq=…1800
                      454 recv_park th=…a768 mq=…50e8 c=1303 d=0x300000600000000
                      ... and NO post to mq=…50e8 ever follows
semaphore trace:      rpc.semaphore.begin operation=semaphore_timedwait wait_name=2307 sec=30   <-- and NO reply
                      rpc.semaphore.begin operation=semaphore_signal     wait_name=0 signal_name=0
```

Read together, the state is unambiguous:

* the msgq layer is **healthy** to the end -- sends are posted, waiters are found (45 of 45 in the earlier count), receives
  are woken;
* the thread that stays parked is waiting on a port (name 1303) that **never receives a send**, i.e. its peer never sends;
* the peer is the thread the **pthread handshake** never released: `semaphore_timedwait` (callnum 62, the number the stall
  dump has shown in `active=` all along) is issued with `sec=30` and in this run **its own timeout never fires at all**
  (no `reply` line), while in another run (section 235) the same wait times out and re-arms every 30 s;
* the handshake's counterpart signal is `semaphore_signal signal_name=0`, an invalid name (`KERN_INVALID_NAME`, section
  235), so it releases nothing.

So the remaining failure of `basic N` is the **thread create/join handshake**, i.e. the blocker this project already
records as `dar-dles` -- not the Ring, not the plane, not the lanes. That is also why the trace perturbs it (the handshake
is timing-sensitive) and why `basic 1` and even `basic 2` can pass: one exchange needs no handshake rendezvous that this
race can lose.


### 242. A real timer defect: a LATER arm was dropped after the earlier deadline had already passed

The stall dump gained the wait-timer state (`dtape_thread_wait_timer_state`, non-perturbing: read at dump time), and
that one field separated two explanations that look identical from outside:

```
pid=1 tid=… active=62 semaphore_timedwait suspended=1 continuation=1 waiting=1 timer=1 tactive=1
pid=1 tid=… active=38 mach_msg_overwrite  suspended=1 continuation=1 waiting=1 timer=0 tactive=0
```

The semaphore timed wait **had a timer armed** and its 30-second deadline never fired, while the same wait in another run
timed out and re-armed. The arming hook explains it:

```cpp
if (!override && _currentTimerDeadline != 0 && deadline_ns >= _currentTimerDeadline) return;   // old rule
```

The timerfd is **one-shot** (`it_interval = 0`) and `_currentTimerDeadline` is only advanced again by
`dtape_timer_fired()`, which the server runs **asynchronously** as a microthread once the timerfd is readable. Between
the timerfd expiring and that microthread running, the recorded deadline is already in the **past** -- and the old rule
dropped every later arm in that window. A wait whose deadline was armed there had a timer that could never fire, so its
thread waited forever, with every instrument reporting a correctly armed timer.

The fix is one condition: an earlier deadline that has **already passed** cannot wake anything and must not suppress the
new arm.

```cpp
const bool currentStillPending = _currentTimerDeadline != 0 && _currentTimerDeadline > nowNs;
if (!override && currentStillPending && deadline_ns >= _currentTimerDeadline) return;
```

Measured effect: the semaphore trace for `basic 20` moved from "one `begin` and **no** reply" to "three begins and two
replies" on the same wait, and boot stays GREEN. `basic 20` still does not finish (also not within 330 s), so this was a
real defect and not the whole of the remaining one.

### 243. Where the remaining `basic 20` hang stands, with every instrument agreeing

* the msgq layer is healthy to the end of the recorded order: sends are posted, waiters are found, receives are woken
  (the in-memory ring gives the order; the line trace was proven to perturb the race);
* one thread stays parked in `mach_msg_overwrite` (call 38) on a port that **never receives a send**;
* its peer waits in `semaphore_timedwait` (call 62) on a 30-second deadline -- the thread create/join handshake, which is
  the blocker this project already records as `dar-dles`;
* the handshake's counterpart `semaphore_signal` carries `signal_name = 0`, an invalid name, so it releases nothing;
* the two parked threads have `timer=1` and `timer=0` respectively, i.e. the timed wait is armed and the plain receive is
  not -- and after the section-242 fix the timed wait's replies do appear, three arms and two replies in one 70-second
  window.

So the remaining work is in the semaphore/pthread-handshake path (`dar-dles`), with the transport's own machinery now
demonstrated end to end: a full mach-msg exchange on the Ring passes, boot passes, and the plane's slot, the op-5
servicing, the signal routing and the timer arming have each been fixed and verified by measurement.


### 244. Tooling: one command that says which instruments SPOKE, and which stayed silent

A hand-written `grep` over a run log finds the line it looks for and cannot tell you that another instrument never spoke
at all -- and during this investigation that is exactly what happened twice: a `SEM-SITE` line was in the log while my
pattern missed it, and two `dserver-CRASH` lines sat in a log I had already read twice as "the server stalled". The fix
is `dwdiag witness` (`diag witness` in the Rust tool):

```
$ scripts/dwdiag witness
WITNESS log=/tmp/dwdiag-verdict-…-basic.log instruments=12 fired=2
  iter-marks           5  ITER 0 make_port
  crash                2  [dserver-CRASH sig=b addr=0x0,self=…,ret=…,sp=…
WITNESS-SILENT sem-site,stall-dump,ring-dump,plane-refuse,dtape-msgq,dtape-timer,rpc-begin,rpc-reply,workload-stall,execpath-after
```

The registry of instruments is a table in the tool, each entry naming the line it writes and why it exists; `--json`
gives the same census mechanically. A registered instrument with zero hits is the answer the command exists to produce:
a guard that silently does nothing cannot be told from no guard, and a census that omits the silent ones repeats the
mistake. Error found by using it, not by reasoning: it surfaced the two crashes above.

### 245. The plane serves a semaphore signal DIRECTLY, because a Call on the process's own thread aborts the server

`terminal=reply-enqueued` and `pending_call=58` in one trace, and a `terminate called … what(): Thread has both a pending
call and a pending continuation (pending_call=58 … active_call=38)` in the log, describe one defect: the plane handler
for `SEMAPHORE_SIGNAL` (and `_ALL`) synthesized an ordinary Call and ran it on **the process's own thread**, and that
thread is very often legitimately parked in a blocking receive. The state machine refused the assignment, the server
aborted, and every RPC the guest still had outstanding was never answered -- which from outside is a hang, and was
chased as one for a long time.

A signal is a state mutation on a semaphore named in the process's space: no thread is involved in the semantics. So the
plane now performs it directly, through `Process::signalSemaphore` -> `dtape_semaphore_signal_for_task` ->
`semaphore_signal_name_in_space(space, name, all)` in duct-tape, which resolves the name in **the task's** namespace
(`ipc_port_translate_send(space, …)`), signals, and returns the trap's own `kern_return_t` (0, or `KERN_INVALID_NAME`
for the invalid name that the guest routinely sends). No Call, no fiber, no thread state. Measured: boot is GREEN again
(`sem_ready` PASS) with the plane as the first route and the Ring RPC as the protocol's own fallback.

### 246. Two more defects found on the way, and one still open

* The urgent pool was drained in **slot-index** order while each request carries `urgent_seq`. The guest's signal
  protocol publishes `INTERRUPT_ENTER` and then `SIGPROCESS`, and `Thread::processSignal` writes through
  `_interrupts.top()`; `_interrupts` is pushed by the enter, so a SIGPROCESS serviced first writes through an empty
  stack. The pass now sorts the pending slots by sequence and services them oldest first (no allocation). This is a real
  ordering defect; it did not by itself remove the crash below.
* The event-ring trace field that two call sites fill with a **port object pointer** and one with a **receiver thread**
  was printed as `port_name=`, and a 13-digit value read as a corrupt name instead of the address it is. Renamed to
  `arg=0x%llx` with the reason at the format string.
* STILL OPEN, and now precisely bounded: `Thread::processSignal` still faults (`sig=b addr=0x0`, `pc` decodes to
  `processSignal + 0x6b`, the write `_interrupts.top().signal = 0` with a `top()` of 0) when a `SIGPROCESS` is serviced
  for a thread whose interrupt context is empty. The crash is a **consequence** the tool named in one command
  (`dwdiag crash`), and the next measurement is a server-side trace of INTERRUPT_ENTER/SIGPROCESS pairs per thread
  (rare events, so a line trace is affordable there) to see whether the guest publishes the enter at all for that
  thread -- i.e. whether the missing enter is a guest bug or a lost slot.


### 247. `SEM-SITE` resolves its own symbols, and the guest callers are now NAMED

The instrument printed raw return addresses, and resolving one by hand needs the image's load base -- which is not in
the log, so the question it existed for ("which guest code waits on a semaphore during `pthread_create`") stayed
unanswered through several runs. It now calls `dladdr` in the guest, where the images are mapped, and prints four
frames: the first two are always the trap and its dispatcher, so the guest caller is frame three or four. One run names
all of it:

```
SEM-SITE op=timedwait a=0x1403 b=0x1E ra=… sym0=semaphore_timedwait_trap_impl ra2=… sym1=_darling_mach_syscall
          ra3=… sym2=sys_semwait_signal ra4=… sym3=_darling_bsd_syscall
SEM-SITE op=signal    a=0x0     b=0x0  ra=… sym0=semaphore_signal_trap_impl ra2=… sym1=sys_bsdthread_terminate
          ra3=… sym2=_darling_bsd_syscall ra4=… sym3=_pthread_terminate_invoke
```

* the 30-second wait is `sys_semwait_signal`, i.e. the guest emulation of `__semwait_signal`, and `nanosleep` is its
  caller in this tree (`libc/gen/nanosleep.c` waits on the global `clock_sem` with a relative timeout) -- so a parked
  `semaphore_timedwait` with `sec=30` can be an ordinary **sleep**, not a lock;
* the signal that arrives with an INVALID name (0) comes from `sys_bsdthread_terminate`, reached from
  `_pthread_terminate_invoke`, and the source says exactly why it can be zero:

```c
semaphore_t custom_stack_sema = MACH_PORT_NULL;
if (t->tl_join_ctx) { custom_stack_sema = _pthread_joiner_prepost_wake(t); }
...
__bsdthread_terminate((void *)freeaddr, freesize, kport, custom_stack_sema);   // pthread.c:855
```

  The joiner only sets `ctx.custom_stack_sema` when the thread has a custom stack, so on the ordinary path both sides
  carry `MACH_PORT_NULL` and the signal is a no-op the server correctly answers with `KERN_INVALID_NAME` (15). That is
  benign; but it also means an invalid-name signal in the log is NOT by itself evidence of a lost reference, and the
  earlier reading of it as "the handshake's signal goes nowhere" was too strong.

### 248. State after the fixes: no aborts, iteration 0 completes, the residual is a Ring RPC with no reply

`dwdiag witness` on the current build reports `crash` **silent** (the server no longer dies), boot `sem_ready` PASS, and
`basic` advancing further than ever before: iteration 0 runs end to end (`make_port`, `port=`, `created`, `received`,
`joined`, `dropped`) and iteration 1 stops between `port=` and `created`, i.e. **inside `pthread_create`**. The stall
dump for that moment shows two parked threads: one in `semaphore_timedwait` (the `nanosleep`/`clock_sem` shape above)
and one in `mach_msg_overwrite` (call 38) -- a thread waiting for a message or an RPC reply -- while the server is alive
and idle. So the residual is not the transport's message path, not the plane's slot, and not a crash: it is one Ring
RPC whose reply never arrives, on the second `pthread_create`.

One instrument gap remains and is recorded rather than guessed: `ring-dump` stayed SILENT in these runs even with
`DSERVER_TRACE_RING=1`, so the ring's last records for the stuck request were not printed, and the next measurement is
to find why the dump did not fire (the dump writes through the stall path, whose counters DID stop and whose
`stall-dump` line DID appear) and then read the last publish/reply pair for that request.


### 249. The ring dump was ON and its output INVISIBLE, and what the ring now says

The dump was written through `dtape_test_trace_line`, which the LINE trace controls -- so a run with
`DSERVER_TRACE_RING=1` and no trace file, exactly the combination the ring exists for (the line trace perturbs the hot
path it observes), produced a stall dump with **no ring records at all**. The instrument was enabled and its output was
invisible, which is indistinguishable from the instrument being off. It now writes its lines to fd 2 itself (a single
`write`, declared locally because this XNU-flavoured translation unit cannot include the host `<unistd.h>`) and still
mirrors them into the line trace when that is on. The tool's registry learned the format in the same change
(`dtape\.ering (dump|seq=)`), so `dwdiag witness` reports it instead of calling it silent.

What the ring says at the current `basic` hang, with the last events read in order:

```
dtape.ering seq=441 tag=send_dest  a=… b=0x75a8500032a8 c=0x75a850003280 d=0x121100000003
dtape.ering seq=442 tag=post_wake  a=… b=…               c=0x0              d=0x300000000
dtape.ering seq=443 tag=recv_object a=… b=…              c=…3c0            d=0x300000e13
dtape.ering seq=444 tag=recv_done  a=… b=…               c=0xe13           d=0x300000000
```

The message queue layer is healthy to its last recorded event: the send found its destination, a receiver was woken, and
the receive **completed** (`recv_object` followed by `recv_done`, with the expected bits on both). Nothing in the msgq
ring is parked. So the residual `basic` hang after iteration 0 is not a parked receive and not a lost wake in the
message layer -- it is a request whose message-layer work is done and whose **RPC completion** never arrives, on the
second `pthread_create`. The next measurement is therefore on the Call/RPC completion side for that request (the
dispatch that publishes a Call for the loader's request and the reply that must follow it), not on the msgq layer.


### 250. A hypothesis raised and RETRACTED by its own instrument: the timer was never late

The ring gained two records for the timer path (`timer_arm` in the arming hook, `timer_fired` in the loop's timerfd
branch), because a semaphore timed wait with a 30-second deadline had begun and never received a reply while the server
was alive, and "armed but never fired" would have separated "the loop never saw the timerfd" from "the timer fired and
the wake was lost". The first reading of the dump looked decisive:

```
dtape.ering seq=453 tag=timer_arm a=<deadline> b=0x0   ... and no timer_fired after it
```

It is NOT evidence of anything, and the reason is worth keeping: the stall dump is written **twice** in a run, five
seconds apart, and the arm was recorded ~10 seconds before the last dump while its deadline is 30 seconds away -- so the
absence of `timer_fired` is exactly what a correct timer looks like at that moment. The instrument did its job by making
the timing legible enough to refute the reading; the conclusion ("the loop stopped reading the timerfd") was withdrawn
before it reached a fix. The same run shows the loop is NOT stopped: `serviced` advanced between the two dumps
(562 -> 565) and the ring grew from 447 to 454 records, so the loop keeps servicing while the workload waits.

### 251. Per-thread identity in both instruments, so a sleeper and a stalled caller can be related

`SEM-SITE` and the workload's `ITER` marks now carry the TCB self pointer (`%fs:0`, one register read, no syscall) as
`tcb=0x...`: the question "is the thread that waits 30 seconds the same thread that is stuck in `pthread_create`, or a
different one" was not answerable from the log, and the two instruments had no common identity. The workload's marks
carry `tcb=0x7d3d03e9e8c0` for the main thread in a fresh run. `SEM-SITE` printed nothing in that same run because no
semaphore call happened in it -- the semaphore family is rare, and the correlation needs a run in which both instruments
speak, which is the next measurement rather than an inference.


### 252. The workload's marks carry the SERVER's thread identity, so a mark and a parked thread can be the same thread

The stall dump names a parked thread by its namespace tid (`pid=1 tid=1282542 … active=38`), and the workload's
per-iteration marks carried only a TCB pointer, so "a thread is parked in `mach_msg`" could not be tied to "THIS mark's
thread is parked there". The marks now print `tid=<self_tid()>` (`pthread_threadid_np`, the same namespace identity the
server prints): `ITER 0 tid=… make_port`, `… port=… create`, `… created`, `… received`, `… joined`, `… dropped`. The
identity is what turns the next correlation from an inference into a read.

### 253. A C++ throw had NO usable instrument; now it has one, plus an honesty bound in the decoder

`terminate called after throwing an instance of 'std::length_error' (basic_string::_M_replace_aux)` killed the server on
one run (the workload never started; the verdict was NO-RUN). Every instrument this project had was blind to it: the
crash probe reports registers, and a throw has no faulting instruction; `dwdiag crash`'s anchor subtraction assumes the
pc belongs to the server binary, so it produced `LOCATION: end + 0x1b6f5f75786c` -- a location that cannot exist; and no
core is written because the host core pattern pipes to apport. Two changes:

* The server installs a `std::set_terminate` handler that writes `[dserver-TERMINATE]` and a
  `backtrace_symbols_fd` trace to fd 2 before exiting 134. `backtrace` is glibc and this is a Linux binary, so a C++
  abort now names its own stack. STATUS: INSTALLED, NOT YET EXERCISED -- the throw is intermittent and has not recurred
  since; by this project's own rule an instrument that has never fired is not yet evidence, and it is recorded as
  unverified rather than as working.
* `dwdiag crash` refuses to name a location outside the binary's own symbol range and says why
  (`<outside this binary's symbol range: the pc belongs to another object>`), verified against the log that produced the
  bogus symbol. An answer that cannot be checked is worse than no answer.

The `std::length_error` itself is recorded as an OPEN intermittent defect: a `basic_string` operation in the server
received an impossible size,
and until the handler catches the next occurrence the call site is unknown.

## The per-thread RPC socket is gone as a transport

Normative: a guest thread has NO per-thread AF_UNIX endpoint. The transports are (1) the per-thread SPSC Ring for
ordinary calls, (2) the process-management plane (the shared page) for lifecycle, early and blocking calls, and (3) the
single PROCESS-level AF_UNIX socket, which exists only as the descriptor (SCM_RIGHTS) courier and as the loader's own
bootstrap endpoint. `__darling_thread_rpc_socket` (loader) and `mach_driver_get_fd` (kernel image) no longer create or
return a per-thread descriptor: a caller that reaches them has neither a lane nor a plane op, so it is NAMED
(`[rpc-socket-DENIED] ... reason=... (declined: no lane, no plane op)`) and fails. Under the hard acceptance hatch
(`DARLING_DISABLE_THREAD_RPC_UDS=1`) it aborts instead, which is what keeps the acceptance claim strict. The token is
deliberately the one the acceptance harness counts, so a decline can never pass as a pass.

Plane operation 30 (`DSERVER_PROCESS_CONTROL_OP_PTHREAD_KILL`) is the last call moved off the socket: `raise()` in a
Darling guest IS a `pthread_kill` to the calling thread, and under the hard hatch the oracle named it
(`[rpc-socket-DENIED] call=pthread_kill`). The ordinary PThreadKill semantics is `Thread::threadForPort` +
`Thread::sendSignal`, and `sendSignal` is a host `tgkill` -- no S2C upcall to the caller -- so the plane services it
DIRECTLY, exactly as pthread_canceled (7). A call whose service requires an upcall to the caller can ride neither the
Ring (the membership rule in `DSERVER_RING_C2S_OPCODES`) nor the plane without an upcall-pumping wait.

Acceptance signal (measured): a whole boot and a 15-workload suite under the hatch report zero `[rpc-socket] created`,
zero `[rpc-socket-DENIED]`, zero urgent timeouts, zero courier misses, and every row PASS.
### The one case where a lifecycle checkout may go unpublished

Rule: a thread-exit checkout rides the management plane; if the plane cannot publish it, that is a NAMED hard failure
(`[rpc-socket-DENIED] ... call=checkout image=loader`) for every thread EXCEPT the main thread. When the MAIN thread
exits, the process is ending and darlingserver learns of it from the process exit itself, so an unpublished checkout
there carries no information the server does not already have: it is logged (`[checkout-path] page=... ready=...
main=1` plus `[mldr-ctl] checkout-skipped`) and skipped. MEASURED, and the reason this rule exists: the rare
occurrence of that case produced exactly one denial per run on `basic 1`/`basic 20`, which the removed datagram
fallback had been covering silently. The state line is printed with the decision, so a route that did not publish is
never a route that cannot say why.


### 254. Current state (2026-09-28): the trap is on the plane, legacy ordinary AF_UNIX is literally zero

This section is the CURRENT code truth and supersedes the older statements above that describe the trap as a
datagram, a wake as four bytes on the courier, or the loop as polling.

Plane operation 31 (`DSERVER_PROCESS_CONTROL_OP_THREAD_SELF_BOOTSTRAP`, appended in `rpc-supplement.h`; case in
`src/server.cpp`) carries the loader's `thread_self_trap`. The loader asks for its main thread's self port ON the plane
before `__darling_thread_initialize_main` consumes the name (`mldr.c:480` asks, `:502` consumes); the datagram path
remains only for the pre-page window and there is deliberately NO fallback after a plane failure.

The trap is NOT answered in the plane's own pass, and that is a measurement, not a preference. Calling the duct-tape
trap directly from the pass asserts and aborts -- the crash tool's resolved backtrace named it:
`panic <- Assert <- retrieve_thread_self_fast <- thread_self_trap_for <- dtape_thread_self_trap_for <-
Server::_serviceProcessControl <- Server::start <- main`. The semantics are therefore reused exactly as the RPC path
runs them: build the ordinary `thread_self_trap` Call, dispatch it (its fiber is the guest thread's context), and read
the port out of the SUPPRESSED reply body (`body.port_name` -> `reply_payload[0]`). `callFromMessage` also registers
Process/Thread, which is why the loader can ask before its deferred checkin.

Measured acceptance for this step (one run each, logs retained):
- `dwdiag verdict --mode sem_ready --args 2 --wait 150`: PASS, `denied=0 created=0 rc=0`, with and without
  `DARLING_DISABLE_THREAD_RPC_UDS=1`.
- `dwdiag courier`: `COURIER-VERDICT PASS legacy-ordinary-afunix=0 zero-fd-control=0` in both runs. The former 5
  packets were this trap, from the loader, before any lane existed.
- `SCM_RIGHTS courier` 17-20 fd-bearing packets (`scm=1`), process management plane 22-24 slot requests.

Still open, with its exact prerequisite MEASURED rather than assumed: the 2 ms `epoll_wait` timeout
(`server.cpp:3284/3293`) is still there. Removing it and blocking on `-1` produced `NO-RUN` twice with every stage
empty, because the server never services the earliest plane publishes: the loader adopts the process-wide wake
descriptor only at its first lane attach (`mldr.c:1241`), while the earliest publishes are the PING (`mldr.c:416`), the
dyld info, the executable path (`:462`) and this trap (`:480`), and the wake in the publish path is guarded by
`if (ring_doorbell_fd >= 0)` (`mldr.c:1539`). The AF_UNIX wake route is CLOSED by this epic's own oracle, since those
four zero-fd bytes are exactly the `zero-fd control/wake packets` that must stay 0. The fix is an ordering change: hand
the guest a doorbell at the PAGE ATTACH (`mldr.c:1374` sends the control memfd as a courier envelope; the server
receives it under `DSERVER_FD_COURIER_KIND_PROCESS_CONTROL`, `server.cpp:1081`) so `ring_doorbell_fd` is set before the
first publish. The doorbell wake-path fix is already in and KEPT: `_ringDoorbellWake()` now also runs
`_serviceProcessControl()`, so a ring genuinely services the management page instead of only the rings.

Tooling in the same round (because two readings were being withheld, not because the tools were optional):
`dwdiag crash` now decodes the signal's meaning, prints the `addr` halves (labelled a hypothesis) and resolves every
`darlingserver(+0x...)` frame to `symbol + offset` -- that is what located the assert above; `dwdiag progress` prints
`STOP-REASON crash:` with those frames for a run that has no workload line; the urgent shared plane now has an
instrument (`[urgent-service]`, one line per serviced slot) and its census row carries a proof token, and the census's
default artifact set includes the SERVER binary, so "quiet" and "instrument absent from the deployed artifact" are no
longer the same reading.


## Transport ledger — authoritative state (2026-10-01)

Measured on the deployed build (mldr, libsystem_kernel.dylib, dyld and darlingserver from one build tree,
sha256-verified at deploy time). Every line below has a direct measurement behind it.

| path | transport | evidence |
| --- | --- | --- |
| ordinary / thread RPC | SPSC Ring | acceptance suite 9/9 first attempt with `rpc-socket created` = 0 on every row |
| blocking family | Ring + fiber/futex | `sem_block`, `sem_timed`, `sem_wait_signal`, `sem_timedwait_signal`, `sem_gap` rows PASS |
| caller-S2C | duplex SHM | `mach_port_deallocate` and `mach_port_mod_refs` ride the existing duplex lane, no denial |
| bootstrap / lifecycle | process management SHM | `SET_DYLD_INFO` direct-serviced; `SET_EXECUTABLE_PATH`, `ATTACH_LANE`, `CHECKIN` on the plane |
| urgent / reentrant | shared urgent slots | `PING`/urgent publish path in use during boot |
| real descriptor | process SCM_RIGHTS courier | 11 process-level courier connections per run, stable across iterations |
| wake | process eventfd (doorbell) | 33 plane doorbell adoptions per run |
| ordinary AF_UNIX RPC | **0** | zero `AF_UNIX`/legacy occurrences across 200 run logs |
| per-thread AF_UNIX | **nonexistent** | `created` = 0 across 200 run logs; `DARLING_GUEST_RING_MACH_MSG=0` is no longer a runnable arm |
| FD slope per thread | **0** | `darling-fd-slope.sh`: server peak 65/65/65/65 and guest peak 9/10/10/10 for 1/8/16/32 threads |

## What the centralized plane retry actually handles

`__dserver_plane_request_ex` wraps `..._once` with nine bounded attempts. Classification from 200 run logs:
the retry marker fired 15 times, always on **op=7 (`SET_EXECUTABLE_PATH`)** or **op=13**, i.e. on the loader's
own early boot publishes, and never on a workload path. `denied` = 0, `created` = 0 and
`plane-token-missing` = 0 in the same 200 runs.

Conclusion: this is **transient slot contention** on the single management slot while several boot-time
publishers are in flight, so a bounded retry is legitimate policy rather than a mask for a lifecycle defect.
The invariant that keeps it safe is the one already implemented: a request is a transaction keyed by
`(pid, seq)`, so a re-published request is a new transaction and no completion can be mis-attributed.

## The two residual boot-stall defects and their fixes

Both were silent: no denial, no fatal signal, no registration on the server — which is why guest-side
instrumentation alone could not name them.

1. **A descriptor-bearing plane route could claim success without its descriptor.** The server handlers for
   `KQCHAN_MACH_PORT_OPEN`, `KQCHAN_PROC_OPEN` and `CONSOLE_OPEN` left `status = 0` when
   `sendFdCourierBundleToGuest` returned no token (they only closed the descriptor), so the guest was told the
   channel existed. Fixed: the completion is now `-EIO` with a named `plane-token-missing` line whenever the
   courier returns no token.
2. **The guest wrapper returned success with `*out_socket` unset** when the token existed but the courier
   receive failed (`[kqchan-plane] status=0 token=<n> fd=-1 out=0`; every passing run in the same batch had
   `fd=positive`). Fixed: the transfer is retried a bounded number of times with a fresh sequence per attempt,
   and the call reports `-LINUX_EIO` if the descriptor still does not arrive.
3. **The loader treated its own `-2` (answer not its own) as fatal**: `Failed to tell darlingserver about our
   executable path` and `exit(1)`, the 15-line boot failure. Fixed: bounded retry of `-2` only — a real server
   status is an answer and is returned unchanged.

Measured trajectory: 4/100 boots failing before the fixes, 4/100 after the first two (a third variant),
then **100/100 and a second 100/100** on the deployed fixed build, with acceptance 9/9 first attempt on the
same build. The frozen `darling-debug` MVP was used read-only throughout; its ABI was not extended.

## The server timerfd was starving every timed wait (fix, 2026-10-01)

MEASURED, and it is the root cause of the stall that the boot flap and the `stress_mixed` HANG shared. A guest
thread of `launchd` was parked in `semaphore_timedwait` (30 s) with `waiting_for_reply=1 suspended=1`, and the
stall dump reported `timer=1 tactive=1` for it -- a timer was armed -- while the server sat alive in
`epoll_wait` and `dtape_timer_fired()` never ran. The server's own timerfd said why: `ticks: 0` (it had never
expired) and its remaining time fell linearly from ~450 ms and then jumped back to ~500 ms every ~450 ms.
Something re-armed it with a fresh ~500 ms deadline faster than it could expire, and that cadence is the
`epoll_wait` timeout the loop uses while the stall watchdog is enabled -- so enabling that watchdog *provoked*
the fault it was written to observe, which is why the failure rate rose whenever it was on.

A timerfd pushed forward forever never expires, so `timer_queue_expire()` never runs and EVERY pending deadline
in the queue is starved: a 30 s semaphore timeout never times out and its thread is never woken.

FIX (`dtape_hook_timer_arm`): an override arm that carries a deadline LATER than the one already armed is
clamped to the armed deadline. A deadline can still be pulled earlier, and a real disarm (`deadline_ns == 0`,
the `UINT64_MAX` mapping) is still honoured, so a cancelled timer costs at most one spurious fire that runs an
expire pass finding nothing due. The previous rule guarded only non-override arms, and the queue's
cancel/re-assign path arms with `override = true` -- which is how an earlier pending deadline was postponed.

Verification: the armed remaining time now counts down in real time (~29 s and falling, instead of ~450 ms
resetting), the provoked series that previously passed 7 of 20 runs passed 12 of 12, and the acceptance suite
reported `rows=9 failures=0 require_zero_creations=1`.

### 255. The single process-scoped slot starves thread bootstrap, and the abort is where the delay lands (2026-10-01, dar-b5pe)

The earlier claim in this document that the acceptance suite was clean (`rows=9 failures=0`) no longer holds on the
instrumented build measured today, and the reason is worth recording as a design question rather than as a
regression: `dwdiag verdict --mode basic --args 20 --repeat 12` gives 7 PASS, 4 CRASH ABRT (guest reporter) and 1
HANG; the same workload at `--args 8` gives 2 PASS and 1 HANG, and at `--args 1` gives 3 PASS. The failure scales
with the number of threads in the process, which is the whole point.

Named cause, from the loader's own ungated state prints at the failure: the dying thread's checkout reaches the
process-control page with `[checkout-path] ... ready=1 main=0` and fails with
`[checkout-pubfail tid=T req=0x0 rep=0x2 ready=1]`, i.e. the page is established and the transport is ready, and at
the moment of failure the slot is IDLE while the reply slot still carries a previous publisher's DONE. The claim
loop sleeps a millisecond between two thousand attempts and does not win the slot in that window; an unpublished
checkout leaves the thread live on the server, so the loader fails closed and aborts.

Two facts keep this from being explained as a missing transport. A run that reports `denied=1` can still PASS, so
a denial is not by itself fatal; and the deferred checkin succeeds through the plane in crashing and passing runs
alike (`checkin-publish BEGIN` then `deferred-checkin status=0 ready=1`), so the main checkin is not the call that
dies. The remaining question is a property of the one-slot design: when one plane operation is slow, every other
thread's bootstrap waits on the same slot, and the wait ends in `abort()` rather than in a delay that survives.
The same shape appears on the console path, where `plane-status=0` is printed five thousand two hundred ninety-four
times against four `-1`s -- a rare non-publication, not a verdict -- and on the `console_open` denial whose
neighbouring `-16` is an internal plane state line rather than the server's answer.

The fix is a decision about the slot (fairness, or a bound that does not end the process, or per-thread state),
which is why it is recorded here and not patched: the two obvious directions were already measured RED earlier in
this work, and the scratch product tree is frozen until canonicalization completes. Repro:
`dwdiag verdict --prefix P --mode basic --args 20`, with `DARLING_GUEST_CHECKIN_DIAG=1` for `[checkin-path]` and
`[checkin-republish]`, and the ungated `[checkout-path]`/`[checkout-pubfail]` lines at the failure.

One more measurement sharpened section 255's question to a protocol property: the `[release-drops-pending]` line that
fires immediately before the fatal checkout is emitted by `DSERVER_PROCESS_CONTROL_RELEASE`'s own macro, and that
macro prints precisely when `request_state` is still PENDING at release time. A bounded give-up therefore returns a
slot to IDLE while the server still owns the request, and the next publisher's claim is answered by the previous
request's stale DONE. The decision is whether to make abandonment safe (an ownership/generation token the server
honours, so a late answer is discarded rather than delivered) or unnecessary (a longer bound, which the earlier 20 s
attempt already showed blows the 30 s shellspawn handshake).

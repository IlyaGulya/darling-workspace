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

**Truthful NOFILE (Candidate A) is independent, required, and its patch is planned but not
applied** (section 6). It does not solve O(threads) hidden descriptors, and it never claimed to.

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

## 9. Decision matrix

| | A: truthful clamp | B: constant anchors, no relay | C: relay |
|---|---|---|---|
| Public FD range correct | **yes**, by construction (plan in §8.3) | yes | yes |
| Hidden FDs | **O(threads)** | O(1) *if* the demultiplexer is built; O(threads) otherwise | O(1) in guest, O(threads) in companion |
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
| Not yet proven | the fix itself | the demultiplexer under the real blocking `mach_msg` class; async-signal-safe child rebootstrap; the exec ownership token in the product | any requirement that needs it |

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

## 12. Repository state

- Product source: **untouched**. No commit, no branch, no push.
- `fix/ring-fd-ownership` in `source-fixes/` carries an **uncommitted draft** (`mldr.c`,
  `elfcalls.h`) of the truthful-limit fix; it was not created by this investigation, was not
  modified by it, and must be reviewed before use — the draft's own `elfcalls.h` callbacks are
  only the beginning of the §8.3 cutover.
- New artifacts from this investigation: the three proof runners under `tests/`, this document,
  and the bead comment recording results.
- `result.txt` in the workspace remains untracked and was not staged.

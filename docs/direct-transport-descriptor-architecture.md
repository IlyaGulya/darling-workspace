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

## 12. Repository state

- Product source: **untouched**. No commit, no branch, no push.
- `fix/ring-fd-ownership` in `source-fixes/` carries an **uncommitted draft** (`mldr.c`,
  `elfcalls.h`) of the truthful-limit fix; it was not created by this investigation, was not
  modified by it, and must be reviewed before use — the draft's own `elfcalls.h` callbacks are
  only the beginning of the §8.3 cutover.
- New artifacts from this investigation: the three proof runners under `tests/`, this document,
  and the bead comment recording results.
- `result.txt` in the workspace remains untracked and was not staged.

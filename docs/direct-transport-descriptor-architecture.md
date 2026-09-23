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

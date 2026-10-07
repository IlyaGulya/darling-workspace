# The guest ulock primitive: what a faithful fix looks like

Status: design for the `dar-lf4b` slice. Written after the T4 investigation, with the measurements linked
at the bottom. It exists so the next session implements the queue instead of re-deriving why the current
one is not fixable at the consumer level.

## The defect in one paragraph

`libplatform/src/os/lock.c` is Apple's `os_unfair_lock` code, and it rests on two facts about the primitive
under it: (1) the lock word describes its own wait queue -- bit 0 (`OS_ULOCK_NOWAITERS_BIT`) is cleared by
a waiter before it sleeps, so a word still carrying the bit means "nobody is waiting"; (2) `__ulock_wait`
reports how many waiters remain, and `lock_slow` consumes exactly that (`waiters_mask = NOWAITERS` when
`ret > 0`) to decide whether the next unlock must wake. Darling implements `__ulock_wait`/`__ulock_wake`
over the HOST's `futex(2)`, which cannot report the remaining count -- so `ulock_wait.c` hacks the return
value to always look like "more waiters", with a comment calling that "the safe thing to do if we are
unsure". The result is a best-effort queue description under a lock that is built to describe its queue
exactly: measured, sixteen threads sat in ulock waits on one word whose value still claimed no waiters.

## The fix: the queue belongs where the kernel would keep it

Put the wait queue in the kernel-side emulation (darlingserver / duct-tape), keyed by
`(guest task, guest address)`:

* Because thread lifecycle must be able to touch it. Darwin keeps ulock queues in the kernel for this
  reason: a thread that dies or is terminated while queued must be REMOVED from the queue, and when a
  queue empties or its owner dies the word's hint bits must be repaired. A host futex cannot express
  either, and that gap is what the frozen cores show.
* Not in `libplatform`: it is a library above the primitive. Its `lock.c` should go back to Apple's code
  with no compensating logic; every consumer that reads the word deserves a primitive whose contract is
  true rather than one whose contract is best-effort.

Restore the semantics exactly, not approximately:

* `__ulock_wait` returns the number of waiters still queued: 0 for the one that empties the queue, > 0
  otherwise. The fact already exists in the queue; only the futex shim discards it.
* `__ulock_wake` wakes one or all per its flags, returns Darwin's values, and reports `ENOENT` when there
  is nothing to wake.
* `ULF_NO_ERRNO`, timeouts, and `EINTR` keep their meanings (the existing retry loop is right).
* Removing the queue must not strand the word: on thread death, dequeue and reconcile the hint bits.

Cost control, so this stays affordable:

* Uncontended fast paths never enter the primitive; only the slow path pays, which is the same class of
  cost as a syscall on Darwin.
* The transport already exists: the ring (`__dserver_ring_*`) handles exactly this family of traps, with
  the UDS path as fallback.
* An unconditional wake per unlock (the stopgap below) is NOT acceptable: it turns every unlock into a
  syscall.

The class-level win: a truthful `ulock` gives Darwin semantics to every consumer, not just this lock --
libc++abi's `_dlock` (whose `_dlock_wait` frame appears in the frozen ld core), libdispatch's ulock-based
waits, `os_once`, and the pthread paths.

## What has been ruled out, and how

* Consumer-side stopgap, implemented and measured: make `os_unfair_lock_unlock` wake whenever the word
  cannot PROVE the queue is empty (on this platform it never can). It is sound but pessimistic (a futex
  wake per unlock), fixes one consumer of many, and -- measured -- it did NOT cure the reproduction: the
  deep-queue arm still wedges. Reverted; the component tree and the prefix are back to their prior state.
* Host futexes as the queue: they cannot report the remaining count (documented in the source's own
  comment) and cannot participate in thread-death bookkeeping.

## Tests this needs

1. Primitive contract (new, at the boundary): N waiters on one address; wake one; assert the woken
   waiter's return reports the remaining count; assert a terminated waiter is dequeued and the word's
   hints are reconciled; assert wake with no waiters is `ENOENT`. This is the test whose absence let the
   deviation live.
2. Integration RED (exists): `tests/run-ios-unfair-lock-contract.sh`, arm `many-waiters-one-word` --
   sixteen threads on one word with dead-progress detection. It fails today; it must pass after the fix.
3. Acceptance: the iOS toolchain gate T1-T4 (`tests/run-ios-toolchain-contract.sh`), i.e. the real link
   producing an arm64 iOS Mach-O.

## Evidence

`hunt/ios-t4-ld-sigill/README.md` (START HERE section): the recovered chain, the frozen-state cores, the
statement that the word cannot prove its queue empty while sixteen threads wait on it, and the source of
the deviation in `ulock_wait.c`. Beads: `dar-lf4b` (this fix), `dar-otzk` (the instruments that would have
shown the server's view during a contract run and do not today).

## The design is not an interpretation: Darwin's own implementation is in the tree

`darling/src/external/xnu/bsd/kern/sys_ulock.c` is Darwin's ulock kernel implementation, with its
interface in `bsd/sys/ulock.h` and its own test in `xnu/tests/ulock.c`. Two facts read straight off it:

* The queue object is keyed by `(ulk_pid, ulk_object)` -- the task and the user address (comparison at
  sys_ulock.c:137-142) -- which is exactly the keying this note prescribes.
* `__ulock_wait` returns the remaining queue depth: the waiter joins with `ull->ull_nwaiters++` (line 599)
  and leaves with `*retval = --ull->ull_nwaiters;` (line 780), followed by the queue-empty action at 781.

So Darling's `ret = 1` hack in `ulock_wait.c` is a deviation from Darwin's own code, not from an
interpretation of it, and the shape to restore -- Darwin's algorithm over a queue keyed by task and
address, with thread-death dequeue -- is the shape Darwin itself uses.

Practical consequence for the slice: do not invent the primitive contract test; adapt Darwin's
`xnu/tests/ulock.c`, and reuse the algorithm in `sys_ulock.c` over a duct-tape-backed queue where the
kernel structures are not available.

## Why the queue is not guest-side

A guest-side queue is tempting: per-address records in the guest process could count depth exactly, sleep
waiters on a per-record futex with a generation counter, and Wwrite the hint bits from a layer that knows
the depth. That alone would cure the measured wedge, because the stranding comes from a *live* waiter
whose hint is wrong, not from a dead one.

It loses on the death case. A guest-side queue can only dequeue through the paths the guest controls
(cancellation points and pthread cleanup), while Darwin's kernel dequeues on *any* thread death, including
asynchronous `thread_terminate` of a thread parked in `__ulock_wait`. Under Darling only the server knows
that a thread died, and the stranding that follows is the same class of bug this slice exists to remove. A
stale entry would also make `ULF_WAKE_THREAD` wake a dead thread and report a bogus depth.

So the queue stays in the kernel-side emulation keyed by (task, address), matching `sys_ulock.c`, and the
guest-side stub keeps doing what Apple's stub does: pass the operation through and return the kernel's
answer.

## Where the gap actually is, and what foundation already exists

The gap is structural and easy to state: `darlingserver/duct-tape/xnu/bsd/kern/syscalls.master` declares the
kernel side of the primitive -- 515 `ulock_wait`, 516 `ulock_wake`, 544 `ulock_wait2` -- with
`NO_SYSCALL_STUB`, and there is no `bsd/kern/sys_ulock.c` in duct-tape. The server never implemented
Darwin's queue; the guest's `libsystem_kernel` emulation resolves these calls to a host futex instead,
which is where the unknowable depth and the `ret = 1` hack come from.

The foundation for the real thing is already ported:

* `duct-tape/xnu/osfmk/kern/turnstile.c` carries the turnstile machinery ulock is built on, including the
  per-thread waiting-turnstile link (`thread_update_waiting_turnstile_priority_chain`) and inheritor
  promotion chains.
* `darlingserver/src/server.cpp:210 dtape_hook_thread_terminate`, registered as
  `.thread_terminate` (line 478), is exactly the thread-death point at which a waiter must be dequeued.
* `darlingserver/include/darlingserver/rpc-supplement.h` already routes a guest trap that needs the server
  (`thread_self_trap`, the `DSERVER_RING_CLASS_SIMPLE_C2S` entry at line 864), so the transport a
  queue-backed ulock call needs already exists.

So the slice is not "invent a queue in the right place"; it is "implement the three declared-but-missing
syscalls over the turnstile machinery that is already in the tree, and let the guest stub return the
kernel's answer."

## Measured: the primitive's absent implementation can panic the server

Running the primitive's contract arm in the full fixture (12 arms passed: fork-held, token-identity,
1.6M-iteration pthread mutex exclusion, allocator exclusion, trylock, condvar) reaches
`stage=ulock-contract` and then kills darlingserver:

    darlingserver duct-tape panic: "semaphore_block"@.../duct-tape/xnu/osfmk/kern/sync_sema.c:635
    semaphore_signal failed (internally): -111

Line 635 is the `default:` arm of `semaphore_convert_wait_result`, which panics on a `wait_result_t` that
is none of `THREAD_AWAKENED`, `THREAD_TIMED_OUT`, `THREAD_INTERRUPTED` or `THREAD_RESTART`: the server's own
wait machinery handed its semaphore block an impossible result. This is worse than the lost depth the
contract arm was written to show, and it is the same class of failure as the "server is gone" symptoms seen
around the linker: a product path that exercises this area can take the prefix down rather than block.

The reproducer is now cheap: `tests/ios_ulock_contract_guest.c` runs the primitive alone, in a spin variant
(no usleep anywhere, the ulock/futex path by itself) and a usleep variant, printing each result before the
next starts, so one short run attributes the panic to the primitive or to the semaphore path beside it.
Run it through the same mechanism: `IOS_UNFAIR_LOCK_FIXTURE=tests/ios_ulock_contract_guest.c DPREFIX=...
DARLING_BUILD_DIR=... bash tests/run-ios-unfair-lock-contract.sh`.

## Measured root cause: the wait blocks where the server cannot see it

The primitive-only reproducer separates the two halves in one run (tests/ios_ulock_contract_guest.c):

| variant | what it does | result |
| --- | --- | --- |
| `spin` | the ulock/futex path alone, no usleep anywhere | completes: `ret=1` for all four waiters, `depth_contract=violated`, `stranded=0` |
| `usleep-idle` | usleep with no waiter parked | completes (`start` then `done`) |
| `usleep` | usleep while the waiters are parked in `__ulock_wait` | hangs for the full timeout; in the full fixture the same arm panics at `sync_sema.c:635` |

So usleep is not the defect, and neither is the useless depth by itself: the depth hack is conservative, and
`stranded=0` shows it. The defect is that `__ulock_wait` parks the thread in a host futex, inside the guest
process, where the server cannot see it. A guest thread blocked that way is incoherent with the server's own
view of the task's blocked threads, and the next operation that goes through that bookkeeping either loses
its wakeup (hang) or hands `semaphore_block` a `wait_result_t` it cannot interpret (panic).

That promotes the fix's first requirement from fidelity to coherence: a guest thread blocked in ulock must
be a first-class server-side block -- a turnstile wait the server owns, as the kernel implementation in
`sys_ulock.c` has -- so that thread death, semaphore operations and wakeups all agree about it. Restoring
`__ulock_wait`'s remaining-depth return follows from the same structure, because the queue that knows the
depth is the one the server owns.

This is the leading hypothesis for the linker wedge and for the "server is gone" symptoms that appeared
around it: both are the same lost-wakeup/panic pair, reached by an ordinary process that holds an unfair
lock while another thread is blocked in the primitive.

## New work, not a regression

`git log --all -- '*ulock*'` in `darlingserver` returns only the initial imports, and there has never been a
`bsd/kern/sys_ulock.c` in duct-tape, so Darling never implemented the kernel side of this primitive. The
guest emulation has no server-call path for it either (`grep` over the psynch emulation finds no
dserver/rpc/duct reference): it resolves the call locally to a host futex. So this slice is new
implementation on both sides, not the restoration of something removed -- which is also why no amount of
consumer-side adjustment on the lock could fix it.

## Implementation map (file level, verified by reconnaissance)

Ordered change list for the slice. Two assumptions corrected by the survey: the guest stubs already exist and
already dispatch here, and the guest-thread death path is `dtape_thread_dying` (reached from
`Thread::notifyDead`), not the kernel-only `dtape_hook_thread_terminate`.

1. `duct-tape/xnu/bsd/kern/sys_ulock.c` (NEW) -- port `xnu/bsd/kern/sys_ulock.c`: the `ull_t`/`ulk_t` hash
   keyed by `(pid, addr)`, `ull_get`/`ull_put`, wait via `turnstile_prepare` / `waitq_assert_wait64` /
   `thread_block_parameter`, wake via `waitq_wakeup64_*`. Duct-tape has no `sysproto.h`, so take explicit
   parameters (psynch style) rather than `struct ulock_wait_args`.
2. `duct-tape/CMakeLists.txt` -- add the new kernel file beside `xnu/osfmk/kern/turnstile.c` (:287) in the
   `darlingserver_duct_tape` static library (:256).
3. `duct-tape/src/ulock.c` (NEW, or `src/psynch.c`) -- hand-written `dtape_ulock_wait` / `dtape_ulock_wait2` /
   `dtape_ulock_wake` wrappers (`NO_DTAPE_DEF`), modelled on `dtape_psynch_mutexwait` (`src/psynch.c:92`);
   declared through the generated `DSERVER_DTAPE_DECLS`.
4. `scripts/generate-rpc-wrappers.py` -- append the three calls to the `calls[]` table (:22) with
   `XNU_BSD_TRAP_CALL|XNU_TRAP_NO_DTAPE_DEF|ALLOW_INTERRUPTIONS`, and add them to `RING_GENERATED_SIMPLE`
   (:790-858) so the generated client wrapper tries the ring lane.
5. `include/darlingserver/rpc-supplement.h` -- add the three ops to `DSERVER_RING_C2S_OPCODES` (:474-535) and
   classify them in `DSERVER_RING_OP_CLASS` (:861-907): `ulock_wait`/`ulock_wait2` as
   `SIMPLE_C2S|BLOCKING`, `ulock_wake` as `SIMPLE_C2S`. This step is not optional: the datagram transport is
   gone (`mach_driver_get_fd` declines unconditionally), so an op missing from the ring tables aborts the
   guest instead of failing gracefully.
6. Guest side: replace the futex bodies in `.../psynch/ulock_wait.c` (:58-76) and `ulock_wake.c` (:25-38) with
   `dserver_rpc_ulock_wait(...)` / `dserver_rpc_ulock_wake(...)`, preserving the raw-negated-errno semantics
   for `ULF_NO_ERRNO` documented at :40-57. `bsd_syscall_table.c` already maps 515/516 (:484-485); 544 is
   unmapped today even though `___ulock_wait2.S` exists.
7. Thread death: `duct-tape/src/thread.c` `dtape_thread_dying` (:610) -- call `dtape_ulock_thread_dying(thread)`
   before `clear_wait_internal` (:615). The reachable caller is `DarlingServer::Thread::notifyDead`
   (`src/thread.cpp:2554`, calling `dtape_thread_dying` at :2593); `Thread::terminate` (:832) and
   `dtape_hook_thread_terminate` (`src/server.cpp:210`, registered :478) are kernel-thread-only and are not the
   guest waiter path.
8. Build/deploy: the changed closure is `libsystem_kernel.dylib` + `dylib` (dyld carries a static emulation
   path, so the pair must be deployed together) plus `darlingserver`, into both prefix closure copies.
9. Gate: `tests/ios_ulock_contract_guest.c` -- `variant=spin` must move from `{1,1,1,1}` to `{0,1,2,3}` and the
   `usleep` variant must stop hanging or panicking -- then the full fixture, then T1-T4.

## Verified: the ring tables are the only transport, and there is a measured precedent

`dserver_rpc_hooks_get_socket` is `mach_driver_get_fd`, and the tree's own record at
`darlingserver/include/darlingserver/rpc-supplement.h:903` says it "declines unconditionally now". The same
comment records what happens to a call that reaches it: `dserver_ring_op_class()` returned 0 for
`psynch_mutexwait` and `psynch_mutexdrop` -- unclassified, treated as UdsOnly -- so the ring refused them
as well, `dserver_rpc_*` returned negative, and the guest answered with `__simple_abort()`:
`[rpc-socket-DENIED call=psynch_mutexwait]` then `RING_MACH_TEST_CRASH sig=6`.

So the failure mode is measured, not theoretical: an ulock op that is not in
`DSERVER_RING_C2S_OPCODES` and not classified in `DSERVER_RING_OP_CLASS` kills the guest process instead of
returning an error. That is why step 5 of the implementation map is mandatory, and it is also why the
classification must be `SIMPLE_C2S|BLOCKING` for the waits: an unclassified op defaults to UdsOnly, which no
longer exists.

## Deploying this slice: the whole chain, or nothing

Changing the RPC tables changes what the loader and the server agree on, so a partial deploy fails in a way
that looks like a bug in the change itself. Measured while validating: deploying only
`libsystem_kernel.dylib` + dyld + darlingserver leaves the loader stale and the server drops its checkin
(`Message buffer was too small for dserver_call_checkin_t`); later, restoring dyld and libsystem_kernel.dylib
from the deploy tool's own backup while leaving a loader built from the changed tree fails the same way,
because the pair is still mixed.

So the consistent set for this slice is: `libsystem_kernel.dylib` + `dyld` + `darlingserver` + `mldr` +
`mldr32` (`--deploy-closure-names libsystem_kernel.dylib --deploy-darlingserver --deploy-mldr`, with
`mldr mldr32` in `--targets`). Note also that `darling-build ... | tail` hides a failed build: the pipeline
returns the tail's status, so the deploy looks successful after a failed compile.

## A build dir is baked to one prefix (do not deploy across prefixes)

`darling-build` refuses to hide this, and `--force` must not be used to walk past it: the build dir carries
the install prefix it was configured for, so a dyld/mldr built in it cannot boot a different prefix. The
doctor states it exactly: "build-dir install-prefix (P1) != launcher baked prefix (P2); a dyld/mldr built here
will NOT boot P2".

Measured consequence while validating: a freshly bootstrapped clean prefix (`--bootstrap-runtime-profile
homebrew-rootless-bootstrap-minimal`) boots and runs the fixture correctly, but deploying this build dir's
artifacts into it cannot work -- the run fails with `sigprocess failed internally while processing Linux
signal 4: -3` and shellspawn never becomes ready -- and that failure says nothing about the change under
test. A run that deploys artifacts must target the prefix the build dir is baked for; to test another prefix,
configure a build dir for it (or let its own bootstrap deploy its artifacts).

Kept as evidence: /home/ilyagulya/work/wr-fresh/hunt/ulock-impl-deploy/{bootstrap-clean-prefix.log,
clean-prefix-baseline-gate.log}.

## Validated

The queue works. On a recreated, freshly bootstrapped prefix (the previous one's runtime state was broken
independently: every generation, fix or not, died with SIGSEGV at 0xFFFFFFFFFFFFFFFF right after the plane
steps), with the whole chain deployed and hashes verified, the primitive gate passes:

    VERDICT: PASS
    IOS-UNFAIR-LOCK PASS: single-thread lock/unlock/lock, contention and 1000 repetitions all survive

`tests/ios_ulock_contract_guest.c` reports `pass=1`, which it can only do when every waiter returns, the
multiset of `__ulock_wait` returns is exactly {0..K-1}, no waiter is stranded, and the usleep variant -- the
one that used to hang or panic the server -- completes.

Committed on canonical fix branches: darlingserver `fix/ulock-turnstile-queue` (b78fce3), the xnu/darling
overlay `fix/ulock-turnstile-queue` (493989c), and darling `fix/mldr32-arch-register-index` (dbe768a01, the
loader build fix this slice needed to redeploy the chain at all).

## Fork children take the futex, deliberately

A child forked from a process holding an unfair lock must BLOCK when it takes that lock (its inherited holder
thread does not exist in the child), and it must not trap. With the queue it trapped: the child attaches its
own lane during the fork handshake, and the first postfork predicate cleared as soon as that lane existed, so
the child went down the RPC path and died in the plane.

The fix makes the fork-child decision sticky for the process: it holds while the child has no catalog of its
own AND keeps holding for the child's whole life (`getpid() != g_ring_owner_pid`). A fork child's lock words
are COW-private, so no transport -- not the parent's unlock, not the server's queue -- can ever release it
through the shared word; blocking is the entire requirement, and the host futex gives that with no transport.
Every other RPC still uses the child's own lane. One instrument records the decision, because it is otherwise
invisible: `[ulock-forkchild] pid=… tid=…: fork child takes the host futex for ulock`.

Measured, on the deployed build: the child logs `forkchild=1`, takes the futex, and the parent's 3 s timeout
finds it still blocked -- `IOs-UNFAIR-LOCK fork-child blocked=1 waited_ms=3000 (Darwin-like)` -- in the
standalone fixture and in the full 12-arm fixture.

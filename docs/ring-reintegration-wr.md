# WR — recovered Ring product candidate (West manifest checkpoint)

`WR` is the first reproducible Git/West representation of the recovered Ring
product. It supersedes the earlier "reconstruct W1–W5 from topic history" plan:
the accepted product source survives as a physical forest, so it is preserved
directly as ordinary per-component commits instead of being re-derived from
fragments.

Source authority (ADR 0001): this `darling-workspace` commit plus the resolved
West manifest. `west manifest --freeze` is derived evidence, never a second
source of truth. The old patch/profile/lock layer stays `FROZEN_LEGACY`.

## Recovery sources

- physical forest: `/home/ilyagulya/work/r1-repro` (pristine; 571 970 files, one
  empty `init` commit, everything untracked) — the materialized forest the
  accepted runtime was built from.
- its build: `/home/ilyagulya/work/r1-repro-build` (configured against the
  forest; `rootless_bootstrap` builds rc=0).
- secondary delta evidence:
  `/home/ilyagulya/work/darling-dev/evidence/r1-transport-sources-20260928T112410Z.tar.gz`
  (438 paths) + `.sha256`, and
  `darling-workspace/diagnostics/ring-loader-fixes-20260930/` (the later
  loader fixes).
- earlier reconstruction trees: `r1-clean-base`, `r1-clean-ref`,
  `r1-clean-build`.

The Sep-28 archive is a recovery **input**, not the final product: it predates
the Sep-30 loader fixes. `WR` preserves the **Sep-30 forest** (`r1-repro`), so
tree equality is the physical forest, not the archive.

## Component recovery bridge commits

Each bridge commit's tree equals the corresponding pristine `r1-repro`
component tree (verified by entry count: files + symlinks). Bridges are ordinary
commits whose parent is the revision the forest base was materialized from; no
historical authorship is claimed.

| component | branch | bridge commit | base parent |
| --- | --- | --- | --- |
| darling | `recovery/transport-bridge-darling` | `dfc302f60232a436624887d61745b58394ec9321` | `5f2d7401` |
| darlingserver | `recovery/transport-bridge-darlingserver` | `9e1d3d989caf5a918c91cb399e5a3efea947e729` | `89751e64` |
| xnu | `recovery/transport-bridge-xnu` | `3ef268818f870cff7a1fd315004220389b4b2bb9` | `5f26a4c2` |
| libmalloc | `recovery/transport-bridge-libmalloc` | `8c3d3863f2add948cbcb9667a8fb767969a2d688` | `a57991e2` |
| dyld | `recovery/transport-bridge-dyld` | `7f0fd6d9672b08bfcab9fe53453d565f5f6eb7c7` | `63f667cf` |
| installer | `recovery/transport-bridge-installer` | `bb68430c98dd295c6f3a91c4a4796be99cf92423` | `88764e61` |
| perl | `recovery/transport-bridge-perl` | `6c27f05a29fa6dc76fd14e485c4a76f100e6be3e` | `a65d68be` |
| libkqueue | `recovery/transport-bridge-libkqueue` | `660ebc3ae078a264503f1f54a9b76f65d46bfe2f` | `f673c801` |
| libressl-2.8.3 | `recovery/transport-bridge-libressl-2.8.3` | `45f14a83115947ba36a33572e29975a8dd811eb5` | `2a56b36b` |
| libpthread | `recovery/transport-bridge-libpthread` | `8fd9324b044470a6f659f02477785df2c89160eb` | `f07f265b` |

`libunwind` is pinned to `d5fbc8180ea2e06efa693ee03a768e532a98671f` (a build
prerequisite, not part of the recovery: without `-fno-jump-tables` on
`unwind_static` the dyld/system_loader link fails).

darling's bridge commit also aligns its submodule gitlinks with the manifest
pins (the nine recovered components above plus `libunwind`, `libplatform`,
`objc4`); every other gitlink is unchanged.

### Exclusions (recorded, not silent)

- `src/external/darlingserver.product` — a staging copy of `darlingserver`
  present in the forest, referenced by no `CMakeLists.txt`, cmake file, script,
  manifest or build directory. Excluded as a forest-local artifact.
- `darling/docs/darling-docs` — a West-managed nested project inside darling's
  worktree (its own manifest entry), not darling product source; excluded from
  darling's worktree status via `.git/info/exclude`.

## Checkpoint state

- workspace commit: `WR` = `795451d1` on `change/ring-reintegration-v1` (the
  manifest-pins checkpoint; later documentation-only commits do not change the
  pin set).
- resolved component SHAs: the table above (also `west list`).
- `west manifest --freeze` digest:
  `sha256 8dd3170bf3819cb8b30eca7514333d4029e6e3f653517c0fd6502f36a907730c`
  (all eleven pins resolve in the freeze; the freeze reads `manifest-rev`, so
  `west update` was run for the recovered projects first).
- `dirty = 0` for every recovered component; untracked product source = 0.
- `west manifest --validate` passes.

## Next (after this checkpoint)

1. Build `WR` fresh from a disposable checkout with the canonical configure/build.
2. Boot it through the **canonical harness** (the one that supplies
   `DPREFIX` + `DARLING_PREFIX` + rootless settings), not a hand-written
   environment: a manually approximated environment produced a fake
   `plane-refuse why=no-page` defect (see `docs/ring-reintegration-dag.md`).
3. If the `vchroot: execv: Bad file descriptor` seen in the manual smoke does
   **not** reproduce under the canonical harness, classify it as a
   manual-harness artifact and proceed to the Ring gates; if it does reproduce,
   resolve the actual `_execv` binding for the deployed `vchroot` Mach-O before
   adding further instrumentation.
4. Gates: focused Ring/RPC host contracts, then 9/9 first attempt
   (`ROW-RETRY 0`, `created=0`, `denied=0`), then ≥200 consecutive clean boots.
5. Provenance extension; NOFILE (`dar-dar6x4-perf-5dq.34`) only after Ring
   acceptance.

`r1-repro` is evidence only from this point: no further product edits there.

## Checkpoint identity

```text
workspace  change/ring-reintegration-v1 @ 3e5562f9  (WR checkpoint + token-identity fix)
xnu        f3e71f3997a898ea7962e43dfad32c500efae97e  fix/ring-courier-token-image-identity
darling    30fa003112412ae9f7b4d72302eef1df0c242732  fix/ring-courier-token-image-identity
```

## WR boot blocker resolved: cross-image fd-courier token collision

The canonical-harness smoke **did** reproduce `vchroot: execv: Bad file
descriptor` (resolving the open question in step 3 above), so it is a product
defect, not a manual-harness artifact.

Resolved boundary and measured chain (each step is a probe, not an inference):

```text
vchroot  execv("/sbin/launchd")
  -> sys_execve            entered (fname=/sbin/launchd)
  -> sys_open              ret=7   (openat resolved /home/.../prefix/sbin/launchd)
  -> sys_read              ret=256 (Mach-O magic)
  -> checkout              ret=-9  EBADF          <-- here
  -> server case CHECKOUT: resolveFdCourierBundle(...) != Resolved -> status = -EBADF
     [courier-store]        pid=242210 token=5189026761396706776 kind=1   (lane backing)
     [courier-kindmismatch] pid=242210 token=5189026761396706776 want=2 got=1
```

Root cause: the token is `generation*A ^ pid*B ^ per-image counter`. mldr and
libsystem_kernel are two copies of that formula inside **one** process: same
pid, same generation, counters that both start at the same value. The loader's
first lane-backing bundle (kind=1) and the guest's first execve checkout bundle
(kind=2) therefore carried the **same** token; the server keys a pending
descriptor by token, dropped the checkout envelope as a duplicate, and the
checkout then resolved as `KindMismatch` (want=2 got=1) -> `-EBADF` -> boot
stopped before shellspawn, with launchd never reaching its runtime.

Retired hypothesis (measured, not assumed): `vchroot` closing its root `dfd`
(`orig=4 valid=1`, `dfd=3`, `same=0`). Closing it was restored after the
experiment; the failure was identical with the fd left open.

Fix (normal commits, both `fix/ring-courier-token-image-identity`):
mix the per-image counter's own address into the token in both senders.

```text
xnu      f3e71f3997a898ea7962e43dfad32c500efae97e
darling  30fa003112412ae9f7b4d72302eef1df0c242732
```

Verified: with the diagnostics reverted, the canonical-harness rootless
bootstrap smoke reaches `launchd-RUNTIME_ENTER` with no
`execv: Bad file descriptor`.

## Next boundary (measured, not yet resolved)

Boot now advances to launchd PID 1 runtime and then a launchd-spawned child
(pid 365580 in that run) crashes after successfully attaching a ring lane:

```text
[srv-process-register #1 pid=365580 ...]
[dring-attach] RING_ATTACH_END pid=365580 ... rc=0
[sigexc-fatal sig=11 code=128 addr=0x0 pid=365580 tid=365580]
[dserver-CRASH sig=b addr=0x0 ... pc=0x75fa2b04a994 ...]
[sigexc-fatal sig=4 code=2 addr=0x7DEB9A6796D8 pid=365580 tid=365580]
Rootless shellspawn did not become ready within 60000ms
```

That crash is the next thing to diagnose; no instrumentation has been added for
it yet.

### Next-boundary measurements (plane attach)

The child that crashes does so right after a **successful** plane attach:

```text
[dring-plane-attach] ok tid=365580 rc=0 reject=0 wake=24
[sigexc-fatal sig=11 code=128 addr=0x0 pid=365580 tid=365580]
[dserver-CRASH sig=b addr=0x0 ... pc=0x75fa2b04a994 ...]
[sigexc-fatal sig=4 code=2 addr=0x7DEB9A6796D8 pid=365580 tid=365580]
```

Control run with `DARLING_GUEST_PLANE_ATTACH_OFF=1` (the guest-side hatch at
`dserver-ring.c:672`): **no `sigexc-fatal` appears at all**, but boot also stops
earlier -- the plane is refused (`[dring-plane-attach] no-page` ->
`[dring-attach-fail] plane-unanswered-no-uds`) and launchd reaches
`RUNTIME_INIT_DONE` but never `RUNTIME_ENTER`/`BOOTSTRAPPER_SCHEDULED`. That
run's tail shows the UDS fallback with `reason=ATTACH_FAILED lane_state=0`, so
the control is suggestive (the crash needs a live plane) but not conclusive:
the plane-off workload never executes the instruction that faults.

Inherited, non-usable state is visible in the same probe and is NOT the cause:
the child reports `b0_active=1 b0_owner=<parent pid> b0_borrowed=1`, i.e. it
inherited the parent's `g_borrowed[0]` record across fork; `gr_borrowed_lane_usable()`
requires `owner_tid == tid`, so the record is ignored.

Next step: instrument the plane-attach success path in the child (or capture the
guest fault with `dwdiag run --capture-gdb`), not the courier/token path.

### The child's death is a signal-delivery failure, not a ring transport failure

The launcher's captured stderr does not carry the whole story; the prefix's
`private/var/log/dserver.log` does. For the crashing child (pid 390425 in that
run) the server recorded:

```text
plane-doorbell-sent pid=390425 token=... via=envelope
attach-lane-op pid=390425 tid=390425 size=2816 status=0 reject=0 token=...
[sigprocess-no-interrupt] delivering a signal without an interrupt context tid=390425 nstid=390425
Uncaught exception from processCall (call dserver_callnum_sigprocess); replying -22
fd-courier-conn-closed fd=23 ... peerPid=390425 isLoader=1
[P:390370(1)]: timed out waiting 30 seconds for fork child checkin
```

So the ring attach and the lane it created were fine (`status=0`): the child's
fatal path is that it faulted (SIGSEGV, `addr=0x0`) while the thread had **no
interrupt context** (`thread.cpp:1134`), so the server could not deliver the
signal, `dserver_callnum_sigprocess` threw, the reply was `-22`, and the child
died. PID 1 then timed out waiting for that forked child's checkin.

Fault address resolution (same-run `/proc/<pid>/maps` union, sampled at 20 ms
for comm `mldr`/`launchd`/`vchroot`/`shellspawn`): the secondary SIGILL
`addr=0x708273EFE6D8` falls in

```text
mldr  prefix/usr/lib/system/libsystem_platform.dylib  708273ef9000-708273eff000
      -> offset 0x56d8 = _os_unfair_lock_recursive_abort + 0x4
```

i.e. the dying process reached libplatform's deliberate
"Trying to recursively lock an os_unfair_lock" trap, consistent with the signal
failing to be delivered and the process aborting rather than reporting.

Scratch harnesses used for this round live outside the product tree:
`/home/ilyagulya/work/ring-reint-state/wr_smoke_maps.py` (smoke + maps union)
and `wr_maps_watch.py` (standalone sampler). Nothing in the product tree was
changed for this measurement.

Next: find the *first* fault in the child (the SIGSEGV at 0x0 that has no
interrupt context) -- capture it with `dwdiag run --capture-gdb`, or instrument
the child's startup after `RING_ATTACH_END`. The signal-delivery refusal itself
(`sigprocess-no-interrupt`) is the second half of the same boundary.

### The `[dserver-CRASH]` line is the SERVER's own fault, and it is now located

`dserver_crash_probe` is installed by the server for SIGSEGV/SIGBUS/SIGILL/SIGABRT/SIGFPE
(`darlingserver.cpp:1184`, `dserver_install_crash_probe`). It prints the **server's** registers,
and its `self=` field is the address of `&dserver_crash_probe`, i.e. the server's own PIE slide.
So the earlier reads of that line as a guest report were wrong; it is the server crashing.

Measured run (`pc` and the same run's maps in one capture):

```text
[dserver-CRASH sig=b addr=0x0,self=0x625443cb76a0,ret=0x625443d68151,sp=0x78058bd9ad78,
               w0=0x625443d68151,w1=0x625464344380,w2=0x625464344370,pc=0x78058ba4a994]
```

Resolved:

```text
static &dserver_crash_probe        = 0x1a6a0        (nm on the built server)
slide                              = self - 0x1a6a0 = 0x625443c9d000
ret - slide                        = 0xcb151  ->  Thread::_handleInterruptEnterForCurrentThread + 0x6d1
pc                                 ->  /usr/lib/x86_64-linux-gnu/libc.so.6, mapping-relative 0x22994
```

The server instruction at that return site is `call setcontext@plt` with `rdi = this + 0x140`
(the context being restored), and `gdb ptype /o DarlingServer::Thread` confirms
`ucontext_t _resumeContext` is exactly at offset **0x140**. glibc's `setcontext` at `+0x34`
is `fldenv (%rcx)` after `mov 0xe0(%rdx),%rcx`, i.e. it dereferences
`uc->uc_mcontext.fpregs` --- and the fault is `addr=0x0`, so **that pointer is NULL**.

`_resumeContext` is declared without an initializer (`thread.hpp:99`) and is only filled by
`getcontext(&_resumeContext)` (thread.cpp:756, 868). A `setcontext(&_resumeContext)` on a Thread
whose context was never captured therefore restores garbage, and glibc faults on
`fpregs == NULL`. That matches the server-log line `[sigprocess-no-interrupt] delivering a
signal without an interrupt context` and the following uncaught
`dserver_callnum_sigprocess` exception (reply -22): the signal path resumes a thread for which
no interrupt/resume context was ever saved.

Full chain for this boundary:

```text
guest child faults in user mode (no emulated syscall in flight)
  -> server Thread::deliverSignal finds _interrupts empty  -> [sigprocess-no-interrupt]
  -> dserver_callnum_sigprocess throws, replies -22
  -> the server's own resume path does setcontext(&_resumeContext) with fpregs == NULL
  -> glibc setcontext+0x34 faults -> dserver_crash_probe -> [dserver-CRASH ...]
  -> the child dies, PID 1 times out waiting for the fork child checkin,
     shellspawn never becomes ready
```

Next: decide the semantic fix (guard the resume against a context that was never captured, or
capture it on the paths that can be resumed) --- measured, not guessed, and verified with the
same canonical-harness smoke.

### Which source site that is

`ret - slide = 0xcb151` sits immediately after `call setcontext@plt`, whose argument is
`this + 0x140` = `&_resumeContext`. In source, the only `setcontext(&_resumeContext)` inside the
microthread machinery is `thread.cpp:636`, guarded at `thread.cpp:607` by

```cpp
if (_suspended && (_pendingCallOverride || (!_pendingCall && hadResumePermit))) {
    ...
    if (_continuationCallback) {            // 616
        _resumeContext.uc_stack... = ...;   // 622-625
        makecontext(&_resumeContext, microthreadContinuation, 0);   // 626
    } else {
        assert(_stack.isValid());           // 629 (NDEBUG in RelWithDebInfo -> not a guard)
    }
    setcontext(&_resumeContext);            // 636
```

The context is only ever captured by `getcontext(&_resumeContext)` (thread.cpp:756, 868, each
paired with `_suspended = true`), while the continuation arm only *rewrites* `uc_stack`/`uc_link`
and calls `makecontext`. glibc requires the ucontext given to `makecontext` to come from
`getcontext`; `makecontext` does not populate `uc_mcontext.fpregs`, so a context that was never
captured keeps `fpregs == NULL` and `setcontext` faults exactly as measured. That makes the
continuation arm (or any resume on a Thread whose context was never captured) the defect, not
the guest and not the courier.

### Corrected site: `jumpToResume` through `_interrupts.top()`, not thread.cpp:636

A temporary probe at `thread.cpp:636` (`setcontext(&_resumeContext)`, the microthread resume
arm) printed `fpregs != 0` on every resume it saw, and no probe line preceded the crash, so the
crashing call is a **second** `setcontext(&_resumeContext)`: `Thread::jumpToResume()`
(`thread.cpp:2519`), inlined at its only caller

```cpp
2734: if (!self->_didSyscallReturnDuringInterrupt) {
2735:     if (localInterruptedContinuation) {
2736:         localInterruptedContinuation();
2737:     } else if (self->_interrupts.top().interruptedCall) {   // _interrupts NOT checked for empty
2738:         self->_handlingInterruptedCall = true;
2739:         self->_pendingCallOverride = true;
2740:         self->jumpToResume(self->_interrupts.top().savedStack.base, self->_interrupts.top().savedStack.size);
2741:     }
2742: } else if (self->_handlingInterruptedCall) {
...
2756:     if (self->_interrupts.top().savedStack.isValid()) {     // same unguarded top()
2761:     self->_interrupts.top().interruptedCall = nullptr;
```

The compiled branch matches the measurement exactly: `cmpq $0x0,0x108(%rax)` off
`_interrupts`' deque internals (`_interrupts` is at byte offset 3040, size 80), then two bool
stores, then `call setcontext` with `rdi = this + 0x140` = `&_resumeContext`. `_interrupts` was
measured **empty** on this path (`[sigprocess-no-interrupt]`, `thread.cpp:1134`), so `top()` is
undefined; the garbage `interruptedCall` read as non-null, the branch was taken, and
`_resumeContext` — only ever captured at `thread.cpp:756`/`868`, neither of which ran — had
`fpregs == NULL`. glibc `setcontext+0x34` (`fldenv (%rcx)` after `mov 0xe0(%rdx),%rcx`) then
faults at `addr=0x0` and the server dies in its own handler.

The function is unguarded in **three** more places (2756, 2761) and it is entered with an empty
interrupt stack, so the root question is one level up: **why is an interrupt-entry dispatch
executed with no interrupt context?** A local `_interrupts.empty()` guard at 2737 would stop the
server crash but suppress the symptom rather than fix the push path, so no product change was
made here. `thread.cpp` is back to its committed state (the probe was removed and the server
rebuilds clean).

### CORRECTION: `_interrupts` is NOT empty at the failing resume; `_resumeContext` was never captured

A temporary probe at both sides of the interrupt machinery settled the empty-stack guess (it was
wrong):

```text
[irq-probe] push  tid=404904 size=1                    (thread.cpp:571, the push happened)
[irq-probe] entry tid=404904 size=1 override=0 pending=0 interruptedCont=0   (thread.cpp:2734)
```

So on the failing thread the interrupt stack has one real entry at the entry point, and
`_interrupts.top()` at 2737 is *not* undefined. What is invalid is the context the resume
restores: `jumpToResume()` (`thread.cpp:2512`) is exactly

```cpp
setcontext(&_resumeContext);          // stack/stackSize are used only under DSERVER_ASAN
```

and `_resumeContext` is captured ONLY by `getcontext(&_resumeContext)` at `thread.cpp:756`/`868`,
each paired with `_suspended = true`. glibc's `setcontext` then faults on
`uc_mcontext.fpregs == NULL`, so on the failing path **the thread reached the interrupt resume
without ever having been suspended**.

Re-measured on the clean tree (4 consecutive canonical-harness smokes): all four failed with the
same record, `ret - slide = 0xcb151` (the same `setcontext` site inside
`_handleInterruptEnterForCurrentThread`), plus `[sigexc-fatal sig=11 …]` and
`Rootless shellspawn did not become ready within 60000ms`. One earlier run of the same tree did
reach `WEST_PREFIX_BOOTSTRAP_OK`, so the boot is *intermittent*, not uniformly failing.

Also measured (guest side, temporary probe printing `gregs.rip` at the fault): the guest fault
that starts the chain is inside the loader itself,
`mldr → __mldr_process_control_request` (mldr.c:1824 region), not in the ring code.

Open question for the next iteration: which path dispatches/handles an interrupt enter for a
thread that was never suspended (so `_resumeContext` was never captured). Instrument
`getcontext(&_resumeContext)` (756/868) and the `_suspended` transitions against the interrupt
entry on one thread id, rather than adding more guards.

## Interrupt/resume: the state machine, the contract, and the measured effect

### Derived transitions (from `thread.cpp`)

| # | State before InterruptEnter | How the continuation is represented | Legal resume | Result |
|---|---|---|---|---|
| A | interrupted call suspended in `suspend()` | `_resumeContext` captured; `_suspended` cleared by the dispatch that ran the interrupt | `jumpToResume()` -> `setcontext(&_resumeContext)` | call resumes at its suspension point; re-entry at `_syscallReturnHereDuringInterrupt`; cleanup frees the parked stack |
| B | interrupted call suspended *with* a continuation callback | `_interruptedContinuation` (the callback), no live fiber to resume | run `localInterruptedContinuation()` | callback runs; nothing is restored |
| C | interrupted call present but NEVER suspended (measured) | none: no captured context, no continuation | **no server-side resume** | interrupt bookkeeping only; the call stays owned by its own path |
| D | InterruptEnter queued in `_pendingInterrupts` before the call was dispatched (documented at `thread.cpp:304`) | none yet | same as C | as C |
| E | the interrupted call already ran to its syscall return | `_didSyscallReturnDuringInterrupt` | none | `_handlingInterruptedCall` is cleared; cleanup |
| F | stacked/repeated interrupts | as above | any of A/B/C as classified | must stay balanced |

The push site (`thread.cpp:570-578`) is what makes C reachable: it stores `_activeCall` into
`_interrupts.top().interruptedCall` **without checking that the call ever suspended**, and a Thread
created for an already-running post-exec image never runs `setupKernelThread()`/`suspend()` at all
(its `_resumeContext` is still the value-initialized one, `fpregs == NULL`, which is exactly what
glibc faulted on).

### Contract

`dserver_interrupt_resume_tests` (`src/external/darlingserver/tests/interrupt_resume_test.cpp`)
drives A-F through the shipped pure decision `InterruptResume::classify()`, asserts
"no restore of an uncaptured context" over all 16 state combinations, and carries an explicit
pre-fix model as its RED arm. Output:

```text
RED/GREEN arm: pre-fix restores an uncaptured context, shipped decision does not
interrupt/resume contract: OK
```

### Measured effect of the fix (one canonical-harness smoke)

```text
before: [dserver-CRASH ...] per failing run, shellspawn never ready
after : dserver-CRASH = 0 ; launchd reaches BOOTSTRAPPER_SCHEDULED and RUNTIME_ENTER
```

The guest child still dies on its own SIGSEGV (`[sigexc-fatal sig=11 ...]`, then the SIGILL) and
`Rootless shellspawn did not become ready within 60000ms` is still the verdict, so the boot is not
green. The plane also keeps answering `-9` (`[pc-return-9+]`), which is the next thing to attribute
before any reliability sample.

### Still open (recorded, not guessed)

`interruptedCall` ownership for case C: the interrupt cleanup (`thread.cpp:2756-2762`) still frees
the parked stack and clears `interruptedCall` unconditionally, while in case C the interrupted call
was never resumed and is therefore still owned by its own path. During the handler `_activeCall` is
the *InterruptEnter* call itself, which is why ownership cannot simply be copied back inside the
handler; the handoff has to be settled against the deactivation path
(`_deactivateCallLocked()` keys off `_interruptedForSignal`) before it is changed.

### Correction and the boundary that remains after the fix

`[sigprocess-no-interrupt]` is **documented-benign** and is not the failure: `Thread::processSignal()`
(`thread.cpp:1131-1148`) explains that an empty `_interrupts` for an arriving signal only means
there is no interrupt marker of this thread's to clear, and that "the signal is still delivered
below". It reports the anomaly once. Earlier readings of this line as the cause were wrong.

What actually fails, on the same run, is:

```text
[sigprocess-no-interrupt] delivering a signal without an interrupt context tid=429646 nstid=429646
Uncaught exception from processCall (call dserver_callnum_sigprocess); replying -22
fd-courier-conn-closed fd=23 ino=1553574 peerPid=429646 isLoader=1 remaining=1
[P:429590(1)]: timed out waiting 30 seconds for fork child checkin
Rootless shellspawn did not become ready within 60000ms
```

The `sigprocess` call is dispatched from an **urgent slot** (`server.cpp:1686-1709`,
`page->urgent_payload[u]`): it is created with `suppressReplyDelivery()`, run with `t->doWork()`,
and the reply code read back with `suppressedReplyCode()` -> `-22`. So the next boundary is the
exception thrown while servicing that urgent sigprocess request (which is why the child's fault
never reaches a handler and the fork child never checks in), not the resume decision that is now
fixed and covered.

### The urgent-plane sigprocess failure is `setPendingSignal()` throwing

A temporary probe on the worker's exception guard (which maps any `std::exception` to `-EINVAL`,
so the -22 never implied EINVAL) named the thrower in one run:

```text
[call-exc] call=12 std::exception what=Can't set pending signal with no active interrupts
```

`call 12` is `dserver_callnum_sigprocess`. The urgent-plane route reaches
`Thread::setPendingSignal()` (`thread.cpp:1131`) on a thread whose interrupt stack is empty, that
function throws `std::runtime_error`, the guard replies `-22`, the guest's sigexc handler sees the
failure and hits `__simple_abort()` (the `[sigexc-fatal sig=4]` SIGILL), the fork child never
checks in, and shellspawn never becomes ready.

The class already treats that state as legal in two other places: `processSignal()` documents that
an empty `_interrupts` only means there is no marker of this thread's to clear, and
`pendingSignal()` already answers 0 for an empty stack. `setPendingSignal()` now agrees: report the
anomaly once, return 0, never throw (both callers ignore the return value).

MEASURED after the change (one canonical-harness smoke): `replying -22` gone from the server log,
`[dserver-CRASH]` still 0. The guest child still dies on its own SIGSEGV, and the plane still ends
at `[pc-return-9+]` before the shellspawn timeout, so the next boundary is the guest's own fault.

### The remaining guest fault is an ALIGNMENT fault, not a null dereference

The guest-side probe now prints `gregs.rip`, and the same-run `/proc/<pid>/maps` union records each
mapping's **file offset** (the first attempt dropped it and resolved the wrong symbol twice).

```text
[fault sig=11 addr=0x0 sicode=128]
[fault-rip rip=0x5B3DF4E85CF9 rsp=0x7FFFFFDFE9A8 rbp=0x4]

comm=mldr  perms=r-xp  map_off=0x2000  file_off=0x6cf9
  -> prefix/libexec/darling/usr/libexec/darling/mldr + 0x6cf9
  -> __mldr_fd_courier_send_envelope + 0xb9  (mldr.c:1440)
     instruction: `movaps %xmm0,0x30(%rsp)`   (aligned 16-byte store)
```

`si_code = 128 = SI_KERNEL` with `si_addr = 0` is the signature of an alignment fault, not of a
null dereference, and the stack confirms it: the frame pushes 4 registers and subtracts 0x88, so a
correct SysV call would leave `rsp` 16-byte aligned at that store, while the fault has
`rsp = 0x…E9A8` (8 mod 16). The function was therefore called with a stack that was one 8-byte slot
off, so the compiler's assumed alignment did not hold.

Call chain (both callers are ordinary C calls in the loader):
`__mldr_fd_courier_send_token()` (`mldr.c:1479`) -> `send_envelope`, called from the lane-backing
attach (`mldr.c:2268`) and from the checkin token (`mldr.c:3001`).

This is the fault that still kills the child after both server-side fixes; the signal is now
delivered (no `-22`, no `[dserver-CRASH]`), the child aborts, and shellspawn never becomes ready.
Next: find which caller runs with a misaligned stack (probe `rsp & 0xf` at entry in the loader and
at its callers), rather than adding an alignment attribute blindly.

### The alignment-path probe did NOT reproduce; the guest fault site is not yet stable

A follow-up run with entry-parity probes in the loader (`__mldr_fd_courier_send_envelope` entry,
the lane-backing call site and the checkin-token call site) produced **no** `[mldr-align]` line at
all, while the same run still recorded one `fault sig=11`. So either the faulting path differs
between runs or the fault does not go through the courier send every time; the single-run
alignment attribution above is therefore a measured *instance*, not yet a stable signature.

The probes were reverted (both repos clean, loader rebuilt without them). The next step for this
boundary should be the sanctioned in-namespace capture (`scripts/dwdiag run --capture-gdb
--gdb-namespace`) rather than more log probes: the guest fault must be caught where it happens,
with its stack, instead of being reconstructed from per-run offsets.

### Working hypothesis for the remaining guest fault (to be tested, not assumed)

Evidence in one place:

```text
launchd-RUNTIME_ENTER -> pid 453389 registers (srv-process-register #1, callnum=14, image=2)
  -> ring attach succeeds (RING_ATTACH_END rc=0, plane-attach ok)
  -> [sigexc-fatal sig=11 code=128 addr=0x0 pid=453389]
  -> [sigexc-fatal sig=4 code=2 ...]      (the guest's abort)
  -> [wait4 pid=453389 raw=0x84]          (parent sees WIFSIGNALED, SIGILL)
  -> Rootless shellspawn did not become ready within 60000ms
```

So the crashing child is the one launchd spawns right after `RUNTIME_ENTER` — i.e. the process the
shellspawn wait is for. Its fault is `si_code = 128 (SI_KERNEL)` with `si_addr = 0`, and the one
instance resolved so far (`mldr + 0x6cf9` = `__mldr_fd_courier_send_envelope + 0xb9`) is an
**aligned** `movaps %xmm0,0x30(%rsp)`, whose frame arithmetic says the caller must have had `rsp`
one 8-byte slot off SysV alignment.

A misaligned stack that appears *inside an otherwise correct native frame* is what a signal
**resume** with a wrong `rsp` produces, and the guest signal machinery here is exactly the piece
the last two fixes touched: Darling installs its own handler (running on `sigexc_altstack`, 4096-byte
aligned, `sigexc.c:72/293`) and rewrites the BSD ucontext for the guest handler. The test is
therefore not another grep but a capture: take the fault in-namespace (`scripts/dwdiag run
--capture-gdb --gdb-namespace`, `--gdb-ex 'handle SIGSEGV stop print nopass'`) and read the
interrupted frame, then compare the delivered `uc_mcontext.rsp` with the frame the instruction
expects. Only if that shows an 8-byte loss is the resume path the defect; otherwise the alignment
instance was incidental and the hunt goes back to the child's own code.

## Remote reachability of the WR pins (2026-10-04)

Every project the WR manifest pins was tested against its **declared** remote: five upstream pins
are exact published tips (`libressl v2.2.9/v2.5.5/v2.6.5`, `neverbleed openssl111fix`,
`python_modules master`), `darling-docs 14c841ef` is an ancestor of `darlinghq/darling-docs master`,
and the local-only recovery pins were published create-only to the project forks:

| project | pin | published ref | verified |
|---|---|---|---|
| darling | 386d18270423617d3a9e94da7660f8adb05d1c26 | `darling-next/darling recovery/ring-wr` | yes |
| darling/src/external/xnu | f3e71f3997a898ea7962e43dfad32c500efae97e | `darling-next/darling-xnu recovery/ring-wr` | yes |
| darlingserver | 9e8b49725930b536c338b18aa173eb5c6f8cf121 | `darling-next/darlingserver recovery/ring-wr` | yes |
| dyld | 7f0fd6d9672b08bfcab9fe53453d565f5f6eb7c7 | `darling-next/darling-dyld recovery/ring-wr` | yes |
| installer | bb68430c98dd295c6f3a91c4a4796be99cf92423 | `darling-next/darling-installer recovery/ring-wr` | yes |
| libpthread | 8fd9324b044470a6f659f02477785df2c89160eb | `darling-next/darling-libpthread recovery/ring-wr` | yes |
| libressl-2.8.3 | 45f14a83115947ba36a33572e29975a8dd811eb5 | `darling-next/darling-libressl recovery/ring-wr` | yes |
| perl | 6c27f05a29fa6dc76fd14e485c4a76f100e6be3e | `darling-next/darling-perl recovery/ring-wr` | yes |
| libkqueue | 660ebc3ae078a264503f1f54a9b76f65d46bfe2f | `darling-next/darling-libkqueue recovery/ring-wr` | yes |
| libmalloc | 8c3d3863f2add948cbcb9667a8fb767969a2d688 | `darling-next/darling-libmalloc recovery/ring-wr` | yes |

The workspace checkpoint itself is published: `IlyaGulya/darling-workspace`
`change/ring-reintegration-v1` at `4e001066d3832fa312507a05ea5ee00df9e9b520`.

`darling` (the manifest repository's own remote) already hosts `recovery/ring-wr`; the workspace
remote hosts the checkpoint branch.

**Three declared remotes had to change** for a genuinely fresh `west update` to succeed at all:
`darlingserver`, `libkqueue` and `libmalloc` were declared against the **upstream** org
(`darlinghq/*`), while their reviewed WR pins exist only on the forks. Pushing upstream is out of
scope, so the manifest now declares `remote: darling-next` for those three, with the upstream
repository still recorded in each entry's `userdata.upstream-repository`. That is the minimum
change that makes the pinned revision obtainable from a declared remote.

### Contract extensions before the gates (2026-10-04)

`dserver_interrupt_resume_tests` now also decides and asserts:

```text
OWNERSHIP of the interrupted call at interrupt cleanup
    resumed                      -> Retire (its own syscall-return re-entry is the completion)
    not resumed, reply suppressed -> Retire (plane-serviced call: no waiter left)
    not resumed, live reply      -> Keep   (this path cannot complete it; NAMED once in the server
                                            as [interrupted-call-not-resumed] instead of being
                                            dropped silently)
PENDING SIGNAL with no interrupt marker
    -> never raises, reports whether it mutated anything, never consumes the signal
```

`Thread::setPendingSignal()` routes its decision through that same rule, so the contract exercises
the product path. MEASURED: no deactivation mismatch ("Upon deactivating the active call ...") in
any retained run, so no behavior changed for the states the implementation actually reaches; the
`Keep` case is instrumented, not silently handled.

## Fresh WR cutover proof (2026-10-04)

Performed in a genuinely empty directory (`/home/ilyagulya/work/wr-fresh`), with no reuse of the
long-lived workspace, its object databases, r1-repro or any old build tree:

```text
west init -m git@github.com:IlyaGulya/darling-workspace.git --mr change/ring-reintegration-v1
west update
  -> 163 / 163 projects at their exact manifest revisions (verified by HEAD == revision each)
west manifest checkpoint 6b1f32a1b489521088267ebb6af1887a78c821d4
component HEADs (fresh clones)
  darling                     8bc594dfd5654568623cd09deb3b2ce7dc4b452b   == pin
  darling/src/external/xnu    f3e71f3997a898ea7962e43dfad32c500efae97e   == pin
  darling/src/external/darlingserver 2186bb43c59e7dbdd174e26567d696f1e052dd73 == pin
  darling/src/external/dyld   7f0fd6d9672b08bfcab9fe53453d565f5f6eb7c7   == pin
dirty tracked = 0 (darling, xnu, darlingserver)
untracked = 0 except `darling/docs/` (a West project path materialized INSIDE the darling repo,
             which git therefore reports as untracked; not stray product source)
cmake configure OK (RelWithDebInfo, EUNION/RING/ROOTLESS_HOMEBREW/ROOTLESS_TOOLCHAIN,
             DSERVER_RING_TRANSPORT, DSERVER_SINGLE_THREADED, SKIP_DRIFT_GATE)
ninja rootless_bootstrap -> 4723/4723 OK
```

Canonical fresh prefix and smoke (`west test --prefix ... --bootstrap-runtime-profile
homebrew-rootless-bootstrap-minimal`, the receipt-writing path that runs the workspace doctor, the
deploy and a bounded guest smoke):

```text
prefix bootstrap guest stdout: WEST_PREFIX_BOOTSTRAP_OK
prefix bootstrap phase complete: guest login shell (0.5s)
prefix bootstrap phase complete: doctor (0.2s)
prefix bootstrap passed for /home/ilyagulya/work/wr-fresh/prefix
  deployment receipt: /home/ilyagulya/work/wr-fresh/prefix/.darling-deploy-receipt.json
```

Receipt verification (§7):

```text
manifest_commit 6b1f32a1b489521088267ebb6af1887a78c821d4   dirty=false
components: darling 8bc594dfd565, darlingserver 2186bb43c59e, xnu f3e71f3997a8,
            dyld 7f0fd6d9672b, libsystem 08df454b6eb0
101 artifact rows
mldr (2 deployed copies)              deployed == built, sha 9855118cfc3a
darlingserver                         deployed == built, sha 7c86447db2ea
libsystem_kernel.dylib (2 copies)     deployed == built, sha e5de7cad4721
deployed probe census: mldr 0, darlingserver 0, libsystem_kernel 0
```

(The hashes differ from the long-lived tree's `build/runtime` outputs because the bootstrap built
its own artifacts from its own materialized source forest. The receipt is the authority for
built == deployed, and it agrees.)

### Two conditions this proof ran under, and why

1. `GIT_LFS_SKIP_SMUDGE=1`. `darling/src/external/swift` pins `471514f4b498...`, whose checkout
   requires `libswiftAVFoundation.dylib` from `https://git-lfs.darlinghq.org/lubos/darling-swift`,
   for which no credentials exist here ("Git credentials ... not found"). Without the skip, the
   *runtime source forest* materializer fails on that smudge; with it, HEAD == the manifest pin and
   the payload stays an LFS pointer. The runtime closure does not consume it: the closure configured
   and linked `rootless_bootstrap` without it. `libunwind`, the other project that failed the first
   update pass, materialized normally and IS required by the closure
   (`src/CMakeLists.txt:186`).
2. `WEST_RUNTIME_BUILD_CACHE=off`. The canonical bootstrap's cache-REUSE path is broken in this
   framework state: with a warm `<manifest>/.west-test/runtime-build-cache`, it passes
   `root=reuse_plan.source_entry` (inside the cache store) together with the *evidence unit* session,
   and `test_runtime_evidence.py:72 record_worktrees()` then raises
   `ValueError: evidence worktree escapes its unit:
   <manifest>/.west-test/runtime-build-cache/source/<key>/darling`. Disabling reuse makes the forest
   land under `evidence.source_root` and the same run passes. That is a tooling defect on the reuse
   path, recorded here rather than worked around silently; the proof above is the
   build-from-sources arm.

## Focused host contracts on the fresh candidate (2026-10-04)

Run in the fresh workspace's own build tree:

```text
dserver_interrupt_resume_tests      rc=0  interrupt/resume contract: OK   (decision A-F, ownership,
                                          pending-signal; explicit pre-fix RED arm)
dserver_exec_completion_tests       rc=0  DSERVER_EXEC_COMPLETION_CONTRACT_OK
dserver_process_identity_tests      rc=0
dserver_runtime_mode_tests          rc=0  DSERVER_RUNTIME_MODE_CONTRACT_OK
tests/run-process-control-slot-ownership-contract.sh   rc=0
    SLOT-OWNERSHIP ok: current protocol fails 1 case(s); generation-safe algorithm is clean
tests/vchroot_fdless_rpc_invariant_contract.py         rc=0
    VCHROOT_FDLESS_RPC_INVARIANT_OK (descriptor-bearing calls: checkin, checkout, console_open,
    debug_*, kqchan_*)
```

Two RPC assets in the workspace (`tests/rpc_error_reply_contract.cpp`,
`tests/rpc_sleep_account_contract.c`) have no host driver in this tree (no runner or registry
reference), so they were not counted as executed contracts. The process-control model's
"current protocol fails 1 case(s)" is reported by the contract itself and is pre-existing; it is
not introduced by any change in this checkpoint.

## Acceptance suite 9/9 on the fresh candidate (2026-10-04)

Run against the fresh prefix with the fixture installed through the sanctioned asset path
(`dwdiag prefix --asset ...=private/var/tmp/ring_mach_msg_test`), with **row retries disabled**
(first attempt) and per-thread-socket creation forbidden:

```text
bash scripts/dwdiag suite --prefix /home/ilyagulya/work/wr-fresh/prefix --wait-base 60 \
  --require-zero-creations --retry-failed-rows false -- \
  'basic 20 :: sem_ready 2 :: sem_block 100 1 :: sem_timed 300 1 :: sem_wait_signal 4 ::
   sem_timedwait_signal 300 1 :: sem_gap 5000 1 :: threadnoop 3 :: fsview'
```

```text
RUN-ENV prefix=/home/ilyagulya/work/wr-fresh/prefix mldr=9855118cfc3a \
        libsystem_kernel.dylib=e5de7cad4721 dyld=a133ea854ce1        (per row, from the deploy)
basic 20                    PASS  0  0   RING_MACH_TEST mode=basic pass=1 iters=20
sem_ready 2                 PASS  0  0   RING_MACH_TEST mode=sem_ready pass=1
sem_block 100 1             PASS  0  0   RING_MACH_TEST mode=sem_block pass=1
sem_timed 300 1             PASS  0  0   RING_MACH_TEST mode=sem_timed pass=1
sem_wait_signal 4           PASS  0  0   RING_MACH_TEST mode=sem_wait_signal pass=1
sem_timedwait_signal 300 1  PASS  0  0   RING_MACH_TEST mode=sem_timedwait_signal pass=1
sem_gap 5000 1              PASS  0  0   RING_MACH_TEST mode=sem_gap pass=1
threadnoop 3                PASS  0  0   RING_MACH_TEST mode=threadnoop pass=1
fsview                      PASS  0  0   RING_MACH_TEST mode=fsview pass=1
SUITE rows=9 failures=0 require_zero_creations=1
SUITE-VERDICT PASS
```

(`PREFIX-PREREQ ok checked=5` for every row; the two counters printed per row are `denied` and
`created`, both 0.)

Row composition, declared because the historical suite is not spelled out verbatim anywhere in the
retained docs: the seven rows the transport ledger names as PASS evidence (`basic`, `sem_ready` and
the blocking family `sem_block`/`sem_timed`/`sem_wait_signal`/`sem_timedwait_signal`/`sem_gap`) plus
`threadnoop` (ordinary/thread RPC path) and `fsview` (vchroot path), both real modes of the same
fixture.

## Provenance: what identifies a tested product (2026-10-04, §10/§11/§17)

The receipt the deploy path writes under `<prefix>/.darling-deploy-receipt.json` is the provenance
record, and it now carries every fact §17 asks for, with `west manifest --freeze` left where it
belongs -- DERIVED evidence reproducible from the manifest commit, stored as neither text nor a
lockfile:

| field | source |
| --- | --- |
| `workspace.manifest_commit`, `workspace.dirty` | the manifest repository (the product revision) |
| `components[].revision`, `dirty`, `untracked` | each component worktree |
| `build_dir` | the build tree the artifacts came from |
| `artifacts[].source_sha256` | the BUILT artifact |
| `artifacts[].deployed_sha256` | every deployed copy |
| `runtime_verdict` | the workload runner, when one records it |

`dirty` and `untracked` are reported separately because MEASURED they are different facts: a
materialized West project path inside a component (`darling/docs/`) leaves untracked entries on an
otherwise pinned component, and a single boolean cannot say which of the two a gate saw.

`runtime_verdict` is written by `dwdiag suite --record-receipt <receipt>` (see `docs/tooling.md`);
the runner adds that one field and refuses a receipt that already carries a verdict, so a receipt
never describes a run that did not measure it.

The doctor's deployed-artifact check keeps the receipt comparison as the DEFAULT
(`--receipt-mode=current`) and the historical `deploy-baseline.md5` comparison as an explicit
regression mode (`--receipt-mode=historical`, or any explicit `--expect-*-md5`) -- a development
deployment is checked against the build it came from, not against a historical digest.

## Tested identity for the gate runs (2026-10-04)

The build/prefix tree used for the 9/9 suite and the boot gate is
`/home/ilyagulya/work/wr-fresh`, whose manifest repository sits at `6b1f32a1` with
`west.yml` sha256 `ad283b5cae176600262b180ebdef24cf61153e1903df5efbcb67b384e7081e7a` --
byte-identical to `west.yml` on this branch (`git show 6b1f32a1:west.yml` and
`git show HEAD:west.yml` agree), so the component pins the run used are this branch's pins.
The deployed bytes are pinned independently of any commit by the prefix receipt and by each
run's own `RUN-ENV prefix=… mldr=… libsystem_kernel.dylib=… dyld=…` line, which is what a
claim about a specific build has to name.

## Boot gate: 199/200 clean, one HANG inside the thread-create trap (2026-10-04)

`basic 20`, one prefix boot per run, `--guest-command /private/var/tmp/ring_mach_msg_test`,
`--wait 60`, no row retries, verdict judged per run:

```text
BOOT-GATE runs=200 pass=199 fail=1 crash=0 hang=1 bootfail=0 noverdict=0 created=0 denied=0
```

The failing run is `#014 verdict=HANG (watchdog) created=0 denied=0`; its log is
`/tmp/dwdiag-verdict-2513327-basic.log` (preserved at
`evidence/boot-gate-20261004-verdict-2513327-basic.log`). What the log actually shows:

```text
[rmmt] start pid=2514729 host_pid=2514729 host_tid=2514729 mode=basic arg=20
[pcreate alloc-enter] pid=2514729
[pcreate alloc-done]  pid=2514729
[pcreate add-enter]   pid=2514729
[add lock-enter] / [add lock-done] / [add unlock-done] / [add intro-enter] / [add intro-done]
[pcreate add-done]    pid=2514729
[pcreate trap-enter]  pid=2514729          <-- nothing follows it
waited 60s of at most 60s
```

`[pcreate trap-enter]` is `libpthread/src/pthread.c` immediately before `__bsdthread_create(...)`
(the guest thread-create trap). The SUPPORTED conclusion is exactly this: **`__bsdthread_create`
did not return before the watchdog**. Where inside it the transition was lost is NOT established
by this run: the failing run did not have `DARLING_GUEST_CREATE_TRACE` enabled, so the loader-entry
discriminator (`[bsc pre-loader ...]`, `[bsc post-loader ...]`) was off and the absence of loader
marks cannot separate "the trap never reached the loader" from "the loader never printed".
The workload produced no result line at all. No `rpc-socket-DENIED`, no
`dring-uds-reason`, no stall-dump body in that log, so the failure is SILENT on the guest side --
the same signature class as the Bead's separately classified `stress_mixed 20` residual (a plane
request released while still pending on a guest path).

This is a causal issue for acceptance (`0 HANG` is a gate criterion), not a measurement artifact:
the hunt is a repeated `basic 20` under `--freeze-on-fail` so the first reproduction leaves the
prefix and its processes alive for the existing read-only instruments (`dwdiag progress`, server
diag log slice, `darling-debug thread`, ring trace). No new probes.

## Reproducing the one HANG: hunt history and the exposure argument (2026-10-05)

The hang must be reproduced before it can be fixed, and the first attempts each failed for a *harness* reason
worth recording, because both were silent:

| hunt | configuration | runs | result | what it established |
| --- | --- | --- | --- | --- |
| gate | `basic 20`, `--wait 60`, hatches off | 200 | **1 HANG** (run 14) | the failure is real; the log ends at `[pcreate trap-enter]` |
| v2 | hatches on, `--freeze-on-fail` | 1 | none (blocked) | `--freeze-on-fail` leaves the prefix RUNNING after a PASS too, and its processes hold the caller's pipe open, so a command-substitution capture blocks on EOF forever |
| v3 | hatches on, file redirect, prefix cleaned between runs | 308 | 0 HANG | consistent with a ~1/200 event (22% chance of 308 clean) OR with the extra per-create writes shifting the timing |
| v4 | hatches OFF (original configuration), fixed harness | 12 | 0 HANG | stopped: 20 create cycles per run is the wrong exposure unit |
| v5 | `threadnoop 2000`, hatches off, fixed harness | running | - | ~100x the create-cycle exposure of one `basic 20` run |

The exposure argument is the reason v5 replaced v4, and it comes from the pinned tree, not from intuition:
`ring_mach_msg_test.c` documents the defect it was written for as *a race whose stalling iteration moves between
runs* ("3, then 16, 20, 24, 28, 32, 37 across batches"), and `threadnoop` is the mode that isolates it --
"create+join a thread that does NOTHING (thread lifecycle alone)". One `basic 20` run performs 20 create cycles
and hung at a thread-create trap, i.e. of order one stall per 4000 create cycles; `threadnoop 2000` performs
2000 create cycles in one boot and names the stalling iteration itself (`[noop <i> pre-create]`,
`[noop <i> post-create]`, plus the create/join durations), so the same boot cost buys ~100x the exposure and a
named iteration instead of an absence.

Both harness defects above are the same class this session keeps meeting: a silent wait that reads as a product
result. They are fixed in the hunt scripts (`/home/ilyagulya/work/wr-fresh/hunt/hunt3.sh`, `hunt5.sh`), and the
freeze-time capture (`capture.sh`) answers the thread questions from live `/proc` state rather than from log text.

### Correction: the last mark does NOT bound the stall to the create (2026-10-05)

The earlier statement in this document -- that the failing run shows the thread-create trap "never returning" -- is
stronger than the evidence supports, and the measurement that shows this is `threadnoop`.

With the iteration trace off (which is how the gate ran), the workload prints NOTHING between the create trap entry
and the next iteration's first mark: the receive, the join and the port teardown are MARK-FREE. So
`[pcreate trap-enter]` being the last line bounds the stall only to the interval

```text
create trap entry  ->  <anything>  ->  next iteration's [pcreate alloc-enter]
```

which contains the create's return, the message round trip, the join AND the receive-right destruction.

`threadnoop 2000` x 60 runs (120,000 create+join cycles, no ports and no messages, `--wait 60`, `--freeze-on-fail`,
prefix cleaned between runs) was **0 stalls**. At the gate's measured order of one stall per ~4000 cycles of
`basic 20`, 120,000 cycles would have reproduced it overwhelmingly if the bare lifecycle were sufficient. It is
therefore NOT: the ingredient is the port/message round trip or the port teardown, exactly as the fixture's own
comments already say about this family ("`stress_churn`/`basic` die at the receive right's destruction while
`timeout` passes, so the ingredient is the round trip"; and the `stress_mixed` residual was "a plane request
released while still pending on a guest path").

The next hunt therefore uses the round-trip mode at high exposure: `basic 2000` (2000 create cycles AND 2000
message round trips plus 2000 port teardowns per boot, ~100x one `basic 20` run), unhatched, `--freeze-on-fail`,
with the live-state capture ready. The phase that stalls is then named by the frozen state, and if the live state
is ambiguous the same hunt repeats with the workload's own `RING_MACH_TEST_ITER_TRACE=1` hatch, which prints the
per-iteration phase (`make_port`, `port=`, `created`, `received`, `joined`, `drop_enter`) -- evidence only, never a
verdict.

### Two instrument defects that made the first two readings of this HANG wrong (2026-10-05)

**1. The `[pcreate ...]` marks are budgeted at 4096 per process, so a log that stops mid-cycle is the budget.**
`libpthread/src/pthread.c`'s `__pcreate_mark` does `if (__atomic_fetch_add(&g_marks, 1, ...) >= 4096) return;`.
The marks are 10 per `basic` iteration (5 `[pcreate ...]` + 5 `[add ...]`), so `basic 2000` exhausts the budget at
about iteration 410 and the log's LAST LINE is then wherever the budget ran out -- not where the workload stalled.
I read "the stall is inside `__pthread_add_thread`" out of exactly that artifact before checking the budget, and the
code's own comment warns about the same class at an earlier bound of 48. Any reading of a long run's last mark must
first compare the mark count with 4096.

**2. The freeze snapshot was taken AFTER the harness's own kill, so it cannot show the server's stall-time state.**
The harness's launcher command carries `timeout 120`; the tool's watchdog fires at 60 s and returns, so a capture
taken in the next seconds is in time, while one taken minutes later (my first attempt, because the capture script
had a syntax error and never ran) sees only the aftermath: the workload still blocked, `darlingserver` already
`state=Z` (zombie), one `mldr` from the launcher still alive, the rest gone. What such a late capture still proves:

```text
workload (guest-declared host_pid 724048)
    Threads: 1                     <-- NO second host thread exists at the stall
    single thread: state=S  wchan=__skb_wait_for_more_packets   (a socket receive)
    fds: exactly ONE socket, at fd 1048575 -- the top of the fd table, i.e. a loader-reserved number
         socket:[1387560] = AF_UNIX SOCK_DGRAM, abstract name @bdc2f, scm_fds 0
```

So at the stall the workload has exactly one thread, and it is waiting for a datagram on a single per-process
abstract DGRAM socket. Whether the *create* or the *receive* is what stalled is NOT established by this snapshot
(the marks cannot say, for reason 1 above), and the server's own state is lost for reason 2 -- which is why the
next reproduction is captured within seconds by the hunt itself rather than by hand.

`__skb_wait_for_more_packets` is the AF_UNIX datagram receive wait, and `fd 1048575` is the loader's own reserved
fd (mldr reserves lane descriptors from the top of the fd table with `F_DUPFD_CLOEXEC`, mldr.c:1375), so the
descriptor is one of the process's own transport descriptors -- not an application fd.

## PROVENANCE DEFECT: the deployed runtime does not correspond to the source revision the evidence names (2026-10-05)

While preparing the decision table for the HANG I grepped the **deployed** loader for its own diagnostics and found
none of them. Measured, same tree, same hour:

| literal in `darling/src/startup/mldr/elfcalls/threads.c` | source | object `mldr.dir/elfcalls/threads.c.o` (16:22) | linked `build/runtime/src/startup/mldr/mldr` | **deployed `<prefix>/libexec/darling/usr/libexec/darling/mldr`** |
| --- | --- | --- | --- | --- |
| `checkin-republish` | 1 | 1 | 1 | **0** |
| `plane-wake` | 3 | 1 | 1 | **0** |
| `release-drops-pending` | 2 | 2 | 1 | **0** |
| `checkin-diag` | 1 | 1 | 1 | **0** |
| `mldr-dthread pre` | 1 | 1 | 1 | **0** |
| `dthread-mask` | 1 | 1 | 1 | **0** |
| `no-transport (declined)` | 1 | 1 | 1 | **0** |

None of those statements is inside a preprocessor region (the file's only `#if`s are lines 99, 614-675, all
unrelated), and the deployed binary is not stripped of string literals: it still contains `mldr is part of Darling`,
the DWARF path `../source/darling/src/startup/mldr/elfcalls/threads.c`, and 70 other `mldr` strings. So the code that
prints those lines is genuinely ABSENT from the deployed loader.

The deploy receipt names that loader's origin, and it is NOT the WR build tree:

```text
dest  : <prefix>/libexec/darling/usr/libexec/darling/mldr
source: /home/ilyagulya/work/wr-fresh/darling-workspace/.west-test/runtime-evidence/.inflight-hqrptnav/build/src/startup/mldr/mldr
source_sha256 : 9855118cfc3a7d7c        deployed_sha256: 9855118cfc3a7d7c     (self-consistent)
receipt components: darling revision 8bc594dfd5654568623cd09deb3b2ce7dc4b452b  (dirty: True)
receipt workspace:  manifest_commit 6b1f32a1b489521088267ebb6af1887a78c821d4
```

The receipt is internally consistent (deployed bytes == the built bytes it copied) and the WR source tree at
`8bc594dfd5` DOES contain the diagnostics, with the file clean in git. The materialization it copied from
(`.west-test/runtime-evidence/.inflight-*`) is deleted by design after the run, and its loader content disagrees
with that revision -- the signature of a **stale reuse**: the runtime was built from an older source identity and
deployed as if it were the current one. (The 2026-10-04 bootstrap notes already recorded that the canonical
bootstrap's cache-REUSE path was broken and needed `WEST_RUNTIME_BUILD_CACHE=off`; this is that defect's blast
radius: the deployed runtime.)

CONSEQUENCE, stated plainly: the 9/9 suite and the 199/200 boot gate were measured on a loader that is NOT the one
the pinned source builds. They remain valid measurements OF THOSE BYTES (the RUN-ENV hashes and receipt are
self-consistent), but they are not evidence about the pinned source, and the HANG being hunted may be a defect of
the older loader that the newer source has already changed. The corrective order is therefore:

1. rebuild the runtime from the current pinned source with `WEST_RUNTIME_BUILD_CACHE=off`;
2. assert the deployed content corresponds to the source by the same literal check used above (a cheap, direct
   correspondence test, independent of any cache key);
3. re-run the focused contracts, 9/9 and the full 200-boot gate on those artifacts, recording the workspace SHA and
   the RUN-ENV identities.

### Refinement: the receipt cannot detect the mismatch, and the forest that built it is gone (2026-10-05)

Two more measured facts about the defect above, both of which say the receipt is not the authority it reads as:

1. **The receipt mixes two identities.** `build_receipt()` composes `workspace` and `components` from the CALLING
   workspace (`component_identity(topdir, ...)`), while `artifacts[]` records whatever `(source, destination)` pairs
   the deploy handed it. When those artifacts came from a separately materialized forest, the receipt asserts the
   workspace's revisions over bytes that forest did not produce -- which is exactly the observed case: the receipt
   names `darling 8bc594dfd5` (whose `threads.c` contains the diagnostics, verified with `git show`, worktree clean,
   sha256 `e2516fcf5f5c…`) while the deployed loader contains none of them. `deployed_sha256 == source_sha256` proves
   only that the copy is faithful, not that the SOURCE is the pinned revision.
2. **The forest it built from no longer exists.** The receipt's source path is
   `.west-test/runtime-evidence/.inflight-hqrptnav/build/src/startup/mldr/mldr`, an inflight unit discarded after the
   run; every evidence unit surviving in that workspace is `status: failed`
   (`runtime-evidence-20261004T162547Z`, `…162807Z`, `…163056Z`, `runtime-evidence-20261005T075651Z`), and the
   materialized forests they still hold DO contain the diagnostics (`darling` at `8bc594dfd`, markers present). So
   the mismatch cannot be reconstructed from the surviving evidence, and no unit in that workspace records a
   successful bootstrap.

Both facts are why the correction below uses a **direct correspondence test** (does the deployed artifact contain a
literal that only the pinned source compiles in?) instead of trusting the receipt, and why the from-source arm now
runs with the two documented conditions together -- `GIT_LFS_SKIP_SMUDGE=1` *and* `WEST_RUNTIME_BUILD_CACHE=off`:
the first omission failed the materializer on `src/external/swift`'s LFS objects
(`fatal: libswiftAVFoundation.dylib: smudge filter lfs failed`), which is the same wall the 2026-10-04 notes hit.

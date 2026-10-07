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

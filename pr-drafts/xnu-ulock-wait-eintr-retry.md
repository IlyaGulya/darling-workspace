# libsystem_kernel: retry ulock_wait on EINTR instead of aborting os_unfair_lock

**Bead:** dar-gwn.6
**Branch:** `fix/ulock-wait-eintr-retry` (base `preupstream/main`)
**Module:** `darling/src/external/xnu`
**Status:** blocked (local-only; full from-source `brew install` not yet green — a separate signal mis-delivery, dar-gwn.1.5, still ends the stress repro)

## Problem

Under a fork/exec signal storm (e.g. Homebrew's concurrent-ruby download
workers spawning short-lived children), a Ruby process aborts with:

```
[BUG] Illegal instruction at 0x…
```

The faulting thread is in `os_unfair_lock`'s contended slow path — reached from
ordinary allocator work such as `free_tiny` taking the malloc magazine lock
(`free_tiny+0x85` is the return address right after
`os_unfair_lock_lock_with_options`). The actual illegal instruction is a `ud2`
inside `__os_unfair_lock_lock_slow`, reached via
`_os_unfair_lock_corruption_abort`.

## Mechanism

`__os_unfair_lock_lock_slow` calls
`__ulock_wait(UL_UNFAIR_LOCK | ULF_NO_ERRNO, …)` and then does:

```c
if (ret < 0) {
    switch (-ret) {
    case EINTR:
    case EFAULT:      continue;                       // retry
    case EOWNERDEAD:  _os_unfair_lock_corruption_abort(current); break;
    default:          __LIBPLATFORM_INTERNAL_CRASH__(-ret, "ulock_wait failure");
    }
}
```

so it requires `__ulock_wait` to return a **raw negated errno**.

But Darling's generic BSD-syscall stub (`___ulock_wait` →
`__darling_bsd_syscall`) treats any return in the errno range `[-4095, -1]` as a
failed syscall and routes it through `cerror_nocancel`, which returns `-1`.
When `futex(FUTEX_WAIT)` is woken early by a signal, `sys_ulock_wait` returned
`-EINTR` (= `-4`), the stub turned that into `-1`, and the caller's
`switch (-ret)` fell into the `default` → `__LIBPLATFORM_INTERNAL_CRASH__` →
`ud2` → `SIGILL`.

The signal storm is essential to the trigger: it is what makes
`futex(FUTEX_WAIT)` return `-EINTR` while a thread is contending the malloc
magazine lock. (The pre-existing `ret &= ~0x800` in `sys_ulock_wait` made this
worse, not better: applied to an already-negative value it mangled `-EINTR(-4)`
into `-2052`, also fatal. There is no `0x800` errno tag in this ABI, and there
is no way to smuggle a small `-errno` back through the generic `cerror` stub.)

Captured live with an env-gated trace in `sigexc_handler` (the delivered signal
was a genuine `SIGILL`, `si_code = ILL_ILLOPN`, `si_addr == rip`), then
symbolized against the guest's `/proc` maps to pin the fault at
`__os_unfair_lock_lock_slow+0x13f` (the `ud2`).

## Fix

Retry `futex(FUTEX_WAIT)` internally on `EINTR` for untimed
`UL_COMPARE_AND_WAIT` / `UL_UNFAIR_LOCK` waits in `sys_ulock_wait`, instead of
returning `EINTR`. This is semantically identical to what `os_unfair_lock`
would do on `EINTR` (re-read the lock word and re-wait) and matches macOS's
internal ulock restart across signals, while never handing a spurious errno to
the corruption-abort path. The bogus `0x800` mask is also dropped from
`ulock_wake`.

Timed waits are unaffected (they still surface `ETIMEDOUT` etc.), and the
errno-mode (non-`ULF_NO_ERRNO`) callers are unchanged for the success path.

## Verification

Deterministic RED→GREEN with a fork/exec-storm Ruby repro
(`sleep_stress2.rb`: a main thread doing short timed sleeps while worker threads
spawn short-lived children), run with the exact portable-ruby Homebrew uses:

- **Baseline:** aborts with `[BUG] Illegal instruction` in
  `__os_unfair_lock_lock_slow` (the `ud2`) around round ~200.
- **Fixed:** 0 `Illegal instruction`, 0 `ulock_wait failure` across a
  `RUNS=8 NWORK=10 ROUNDS=600` storm, running well past the baseline failure
  point.

## Scope / status

- Scoped to `sys_ulock_wait` / `sys_ulock_wake` in the libsystem_kernel BSD
  syscall emulation.
- `publication-status: blocked` until full from-source `brew install` is green
  end-to-end. With this SIGILL removed, the same storm still terminates via a
  **separate** defect — a reaped-child `SIGCHLD` mis-delivered as a fatal signal
  with `si_code = SI_USER` (observed as `SIGFPE`, default action) — tracked
  under dar-gwn.1.5.

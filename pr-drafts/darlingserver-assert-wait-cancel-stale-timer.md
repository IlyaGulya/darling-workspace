# darlingserver: cancel a stale wait_timer when entering a new wait

**Bead:** dar-gwn.1.9
**Branch:** `fix/assert-wait-cancel-stale-timer` (base `89751e6` → `preupstream/main`)
**Status:** blocked (local-only; complements `fix/cancel-stale-wait-timer`, full from-source brew end-to-end still pending)

## Problem

`brew install` under Darling intermittently aborts with a Ruby fatal during the
download phase:

```
/usr/local/Homebrew/Library/Homebrew/download_queue.rb:184: [BUG] pthread_cond_wait: Operation timed out (ETIMEDOUT)
```

`download_queue.rb:184` is `sleep 0.05` in the DownloadQueue poll loop. Ruby's
`sleep` blocks the thread on an **untimed** internal `pthread_cond_wait`
(`rb_native_cond_wait`), and MRI treats any return value other than `0`/`EINTR`
from that untimed wait as a fatal `[BUG]` via `rb_bug_errno("pthread_cond_wait",
…)`. So a spurious `ETIMEDOUT` on a wait that was entered **with no timeout** is
what kills the process. The trigger is the concurrent-ruby download thread pool:
a fork/exec storm of short-lived `curl` children that woke timed waits early.

## Mechanism

duct-tape arms `thread->wait_timer` for any wait with a non-zero deadline
(`waitq_assert_wait64_locked`) and, on expiry, fires
`thread_timer_expire() → clear_wait_internal(THREAD_TIMED_OUT)`. XNU keeps a
stale timer from ever firing on the *wrong* wait by cancelling `wait_timer` in
`thread_unblock()` on every wakeup — that is the existing `fix/cancel-stale-wait-timer`
(dar-gwn.2) commit.

But in duct-tape's cooperative model some wakeup/continuation paths complete a
**timed** wait without routing through `thread_unblock()`. The observed case: a
timed psynch `cvwait` (Ruby's timed `sleep` backing) is woken `THREAD_AWAKENED`,
and its continuation (`psynch_cvcontinue`) returns to userspace and the thread
re-blocks in a **new, untimed** wait before the previous wait's still-armed
timer is serviced. The leftover timer then fires `thread_timer_expire()` against
the thread's *current* (untimed) wait, delivering `THREAD_TIMED_OUT` →
`_wait_result_to_errno` → `ETIMEDOUT` → Ruby `[BUG]`.

This was confirmed with env-gated server instrumentation (`DSERVER_TMRTRACE`):
under the fork/exec-storm repro, timers armed for wait *seq N* (a timed
`cvwait`, `block_hint=PThreadCondVar`) were seen firing on the same thread at
wait *seq N+1* whose recorded deadline was `0` (untimed) — i.e. a stale timer
landing on an untimed wait — and each such event coincided with the
`download_queue.rb:184` abort.

## Fix

Cancel any `wait_timer` still armed when a thread reaches
`waitq_assert_wait64_locked()`, before (re)arming for the new wait. A thread can
only be in one wait at a time, so a live timer there is necessarily a leftover
from a prior wait and can only do harm. This restores XNU's invariant that a
thread entering `assert_wait` has no live `wait_timer`, and follows the same
accounting protocol as `thread_unblock()`/`dtape_thread_destroy()`: only
decrement `wait_timer_active` when `timer_call_cancel()` actually dequeued the
call; if it was already in flight, clearing `wait_timer_is_set` makes the
pending `thread_timer_expire()` a no-op.

This **complements** (does not replace) the `thread_unblock()` cancel: that
handles the common wakeup path; this closes the residual leak it cannot reach.

## Verification

Deterministic RED→GREEN with a fork/exec-storm Ruby repro
(`dar-gwn18-etimedout-repro/sleep_stress2.rb`: a main thread doing many short
timed `sleep`s while worker threads spawn short-lived children), run with the
exact portable-ruby Homebrew uses:

- **Baseline (sigexc-flood fix present, this fix absent):** aborts at
  `download_queue.rb:184` with `[BUG] … ETIMEDOUT` within the first run/round;
  `DSERVER_TMRTRACE` shows the stale untimed-wait timeout firing.
- **Fixed:** 0 stale-timer hits across tens of thousands of timer expiries and
  no abort across repeated runs.

## Scope / status

- Scoped to a single defensive cancel in the wait-arming path; no behavioral
  change for waits that are cancelled normally (the timer is already gone, so the
  guard is a no-op).
- `publication-status: blocked` until full from-source `brew install` is green
  end-to-end. This removes the dar-gwn.1.8/.1.9 download abort; remaining brew
  blockers are tracked separately.

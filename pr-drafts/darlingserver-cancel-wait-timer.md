# PR draft - darlingserver: cancel wait_timer in thread_unblock

- **beads:** dar-q95.15
- **repo:** darlinghq/darlingserver
- **current branch:** `fix/homebrew-psynch-ruby-hang`
- **current commit:** `32af4d56`
- **clean PR branch:** not created yet
- **files:** `duct-tape/src/thread.c`

## Title
duct-tape: cancel wait_timer when unblocking a thread

## Body
`waitq_assert_wait64_locked()` arms `thread->wait_timer` for timed waits, but
duct-tape only cancelled that timer during thread destruction. If the wait was
woken early by a normal wakeup or signal, the stale timer remained armed and
could later fire while the thread was blocked in an unrelated wait.

That stale expiry flows through `thread_timer_expire()` and
`clear_wait_internal(THREAD_TIMED_OUT)`, producing a spurious timeout for the
thread's current wait. For psynch waits this can clear/prepost the wrong
condition state and strand a later wakeup.

Match XNU's `thread_unblock()` behavior by cancelling an armed wait timer when
the thread is actually unblocked. The timer-expiry path is safe because it
clears `wait_timer_is_set` before calling through to `thread_unblock()`.

## Tests

Validated during the Homebrew/Ruby timed-wait investigation:

- targeted Ruby timed-wait stress stopped losing deadline wakeups,
- Homebrew fetch/build paths advanced past the earlier timer-related hangs,
- later failures were isolated to separate psynch/raw-fork issues.

## Cleanup Before PR

Create a clean branch off darlingserver `origin/main` and make sure no `[PH]`
diagnostic logging from the investigation branch is included in the diff.

# PR draft — darlingserver: fix microthread resume-before-suspend lost wakeup

- **beads:** dar-q95.8 (root cause: dar-gwn.1.4)
- **repo:** darlinghq/darlingserver
- **branch:** `fix/microthread-resume-race` (off `origin/main` `89751e6`)
- **files:** `internal-include/darlingserver/thread.hpp`, `src/thread.cpp`

## Title
thread: fix microthread resume-before-suspend lost wakeup (coalesced resume permit)

## Body
A generic lost-wakeup race between a duct-tape blocking wait and DarlingServer's
`Thread::suspend()` / `resume()`: when a wake (`resume()`) arrived *before* or
*during* the microthread's physical suspension, it was dropped and the thread
was stranded forever.

Concrete interleaving: a `kernelAsync` worker increments its available count,
`semaphore_wait` marks the XNU thread `TH_WAIT`, and `thread_block_parameter()`
observes `waiting==true`. Before it calls `thread_suspend()` / C++
`Thread::suspend()`, a producer signals the semaphore. `thread_unblock()` sets
`THREAD_AWAKENED` and calls `resume()`, but `resume()` sees C++
`_suspended==false` and returns without scheduling. The signal has already
consumed the semaphore token; the worker then calls `suspend()`, sets
`_suspended=true`, and sleeps forever.

### Fix
A coalesced `_resumePermit`:

- `resume()` records one permit while the thread is `_running` or `_suspended`
  (coalescing repeated wakes), and only schedules when already suspended and
  not running.
- `suspend()` consumes a pending permit *before* sleeping and again *after*
  `getcontext()` captures the resume context (covering the early and
  in-progress windows), returning without sleeping if one is present.
- `doWork()` replays a permit that arrives after `suspend()`'s final check by
  rescheduling the microthread — but only once `_running` is false, so two
  workers can never run the same microthread concurrently.

`_running`, `_suspended`, and `_resumePermit` are documented as orthogonal
lifecycle dimensions rather than collapsed into one enum.

This supersedes the earlier dedicated-per-timer-expiry microthread workaround
(see dar-gwn.1.3 / commit `8ff03f0`), which was net-reverted: timer delivery on
the shared `Thread::kernelAsync` path is reliable once this race is fixed.

## Validation
- Deterministic 20 ms amplified wake-before-suspend window: baseline timeout
  3/3 → fixed 10/10.
- Ordinary Ruby timed-wait stress: 50/50, edge-case smoke 20/20.
- Heavy 80-thread / 40 000-wait run: completes, peak RSS ~27.6 MiB, host
  threads stay at 2.
- Reproducers in `tools/repro-darlingserver-resume-race/` (white-box patch
  amplifier) and a portable black-box single-binary RED/GREEN client
  (`timer-wait-stress.c`, dar-77o).

# PR draft — darlingserver: fix lost fork-child checkin under a signal-aborted wait

- **beads:** dar-gwn.6.5
- **repo:** darlinghq/darlingserver
- **branch:** `fix/fork-checkin-sticky-flag` (off integration tip; source-commit `eb7e93b`)
- **files:** `src/process.cpp`, `internal-include/darlingserver/process.hpp`
- **related:** `fix/fork-checkin-bound` (dar-q95.13) — that patch bounds the wait to 30 s so a lost checkin surfaces as a logged timeout instead of a permanent hang; this patch fixes *why* the checkin is lost.

## Title

process: fix lost fork-child checkin under signal-aborted wait

## Body

A forked child checks in with darlingserver by raising its parent's
fork-wait semaphore (`Process::notifyCheckin`); the parent blocks for it in
`Process::waitForChildAfterFork`.

That wait is forcibly aborted whenever a signal is delivered to the parent's
server microthread — `dtape_thread_sigexc_enter` does
`clear_wait_internal(THREAD_INTERRUPTED)` and clears `TH_UNINT|TH_WAIT`, so the
wait is aborted even if it was requested uninterruptible. In practice a
`SIGCHLD` raised on the parent by a *sibling* that just died while the parent
is forking the next child triggers exactly this.

If the child's checkin hands its semaphore wakeup off to the parent
(`SEMAPHORE_THREAD_HANDOFF`) at the same instant the wait aborts, the wakeup is
discarded and the XNU semaphore count is left stuck at `-1`. A later fork's
wait then starves forever, producing a spurious 30 s `fork child checkin`
timeout, a permanent fork hang, and a pile of unreaped zombie children. This is
readily hit by a `SIGCHLD`-heavy parallel fork storm such as `make -j`
(observed installing Homebrew's libunistring under Darling).

Fix: back the fragile counting-semaphore handoff with a sticky per-process
flag. `notifyCheckin` sets `_forkChildCheckedIn` before raising the semaphore;
`waitForChildAfterFork` checks it on entry (fast path), after an interrupt, and
at timeout. The semaphore remains the fast wakeup path, but even if its wakeup
is lost to a signal-forced abort the sticky flag is still set, so the guest's
existing re-issue of `fork_wait_for_child` (after `-EINTR`) observes the checkin
and returns immediately. The handler never re-blocks the microthread itself
(that is unsafe in the duct-tape); the guest's per-RPC retry is the re-wait
vehicle. The flag is reset when consumed and when the process is replaced via
`exec`.

## Validation

- Deterministic SIGCHLD-driven fork-storm repro (parent installs a non-restarting
  SIGCHLD handler, then fork-storms children that exit staggered): RED (30 s
  fork-checkin timeout on the first round) before the fix → GREEN (400/400 rounds,
  thousands of SIGCHLDs delivered, zero timeouts, no panic) after.
- A plain fork-storm (no SIGCHLD pressure) stays GREEN — no regression of the
  normal path.
- `brew reinstall wget` under Darling: libunistring's `make` phase no longer
  stalls (0 fork-checkin timeouts; the build advances past where it previously
  wedged).

## Note

This is a minimal, correct fix at the fork-checkin layer. The deeper issue — that
a signal-aborted XNU semaphore wait can lose a handed-off `semaphore_signal` and
leave the count stuck at `-1` — is a duct-tape semaphore/sigexc interaction that
may warrant a broader fix; flagged for review before upstreaming.

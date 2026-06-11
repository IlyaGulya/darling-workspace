# PR draft — darlingserver: bound fork-child checkin wait

- **beads:** dar-q95.13
- **repo:** darlinghq/darlingserver
- **branch:** `fix/fork-checkin-bound` (off `origin/main` `89751e6`)
- **files:** `duct-tape/include/darlingserver/duct-tape.h`,
  `duct-tape/include/darlingserver/duct-tape/types.h`,
  `duct-tape/src/semaphore.c`,
  `internal-include/darlingserver/process.hpp`,
  `src/call.cpp`, `src/process.cpp`
- **origin commits:** `a8cfc49` (bound), `e1764b3` (logging)

## Title
process: bound fork-child checkin wait with a timeout

## Body
`Process::waitForChildAfterFork()` blocked forever on the fork-wait semaphore
via `dtape_semaphore_down_simple()`. A fork child that never checked in (died
early, failed to re-exec, etc.) wedged the parent's microthread permanently.

Add a timed semaphore wait and bound the checkin to 30 s:

- new `dtape_semaphore_down_timeout()` + `dtape_semaphore_wait_result_timed_out`
  (wrapping `semaphore_timedwait`);
- `waitForChildAfterFork()` now returns `bool` and waits at most 30 s;
- `Call::ForkWaitForChild` replies `-ETIMEDOUT` on timeout instead of hanging;
- fork-child checkin lifecycle logging (parent/child association, checkin
  observed, timeout).

Predates the psynch investigation — originated in the SwiftPM fork-hang work.

## Note
Independent of the cvwait and resume-race PRs (disjoint files); orderable freely.
This is a robustness bound for a broken fork-child lifecycle, not a complete
root-cause fix for why a child might fail to check in. It prevents permanent
server wedging while preserving the original failure as an `-ETIMEDOUT` result
that can be diagnosed separately.

# PR draft - libpthread: decode negative psynch wait returns under Darling

- **beads:** dar-q95.16
- **repo:** darlinghq/darling-libpthread
- **current branch:** `fix/homebrew-psynch-ruby-hang`
- **current commit:** `3de6464`
- **clean PR branch:** not created yet
- **files:**
  `src/pthread_cond.c`,
  `src/pthread_mutex.c`,
  `src/pthread_rwlock.c`

## Title
pthread: decode negative BSD-error returns from emulated psynch waits

## Body
Darling's emulated BSD psynch wait syscalls can return their BSD error value
directly as a negative return register value. This differs from the real macOS
libpthread expectation, where syscall failure is reported as `-1` plus `errno`.

The upstream checks only handled `(uint32_t)-1`, so an interrupted or timed-out
psynch wait could be misread as a successful update/acquisition:

- condvars skipped the `EINTR` / `ETIMEDOUT` / prepost recovery path,
- mutexes skipped the interrupted-wait retry path,
- rwlocks treated negative wait returns as successful update values.

Decode any negative return as `-(int32_t)return_value`, while preserving the
existing `-1 + errno` path. This lets libpthread consume Darling's interrupted
psynch wakeups correctly instead of stranding condvar/mutex/rwlock handoffs.

## Tests

Validated during the Homebrew psynch investigation:

- `brew fetch wget` loops advanced past the previous psynch deadlock,
- `brew install jq` advanced past `Fetching downloads`,
- later Homebrew failures were isolated to distinct timer/raw-fork issues.

## Cleanup Before PR

Create a clean branch off darling-libpthread upstream. Consider whether the
more fundamental fix should instead normalize the emulated libsystem_kernel
syscall ABI to macOS-style `-1 + errno`; if that is feasible, this libpthread
change becomes either unnecessary or a compatibility backstop.

Tracked as `dar-q95.23`: resolve the psynch ABI layering before opening this
PR. This is the most suspicious patch-vs-root-fix item because it teaches
libpthread about a Darling-specific negative-return convention. If that
convention is not the intended ABI boundary, move the normalization below
libpthread and keep this change only if it is still needed as a defensive
backstop.

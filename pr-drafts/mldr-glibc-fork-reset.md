# PR draft - mldr: reset glibc loader locks in raw-fork child

- **beads:** dar-q95.14
- **repo:** darlinghq/darling
- **branch:** `fix/mldr-glibc-fork-reset`
- **base:** top-level Darling upstream branch
- **commit:** `d90fe8f2a949bcf51f5d4a5aed6ce3fd3a7ef98f`
- **files:**
  `src/startup/mldr/glibc_fork_reset.{c,h}`,
  `src/startup/mldr/mldr.c`,
  `src/startup/mldr/CMakeLists.txt`,
  `src/startup/mldr/elfcalls/elfcalls.{c,h}`,
  `tests/regression/run-glibc-fork-lock-reset.sh`,
  `tests/regression/glibc_fork_lock_reset.c`,
  `tests/regression/glibc_fork_lock_reset_tls.c`

## Title
mldr: reset glibc loader locks in raw-fork child

## Body
Darling's fork path uses raw `__NR_fork`, so it bypasses glibc's normal
`fork()` child-side loader reset. The earlier fix reset
`GL(_dl_stack_cache_lock)`, which prevents `pthread_create()` from deadlocking
when the parent forked while another thread was mutating glibc's stack cache.

That was still incomplete: glibc's own `fork()` child path also resets
`GL(dl_load_lock)` and `GL(dl_load_tls_lock)` unconditionally. If the parent
forks while another thread is in `dlopen()`/`dlclose()` or dynamic TLS setup,
the child can inherit those recursive loader locks as permanently owned by a
thread that no longer exists.

Move the glibc reset code into `glibc_fork_reset.{c,h}` and have mldr:

- detect the stack-cache region in `_rtld_global` without copy relocations,
- detect the three consecutive free recursive rtld mutexes for
  `_dl_load_lock`, `_dl_load_write_lock`, and `_dl_load_tls_lock`,
- reset `_dl_load_lock` and `_dl_load_tls_lock` in `__mldr_postfork_child()`,
- keep the existing stack-cache lock reset and inherited-cache drop.

The recursive mutex reset uses glibc's `pthread_mutex_t` initializer image and
stores the `__lock` word last. This keeps the code valid for both `mldr` and
`mldr32`.

## Tests

- `tests/regression/run-glibc-fork-lock-reset.sh`
  - compiles the production `src/startup/mldr/glibc_fork_reset.c`
  - verifies all three cases deadlock without the reset:
    stack-cache/pthread, load-lock/dlopen, load-tls-lock/dlopen
  - verifies all three cases pass with the reset
- `ninja mldr mldr32`
- `ninja src/startup/mldr/install`
- Homebrew e2e smoke:
  `brew install --build-from-source lz4` passed 3/3 iterations with
  `ALL-PASS` against the rebuilt/installed mldr.

## Notes

This is a child-side runtime-state restoration fix, not a one-off lock
workaround. Darling reaches the child through raw `__NR_fork`, so it does not
run glibc's `fork()` child path. The PR should say that mldr is responsible for
explicitly emulating any critical host-runtime child resets skipped by that raw
fork path; these glibc loader locks are the concrete instances covered here.

A longer 10-iteration Homebrew run completed two source-build iterations and
then hit a separate Ruby `pthread_cond_wait: ETIMEDOUT` abort in
`download_queue.rb:184`; it did not reproduce the old `mldr` `futex_wait_queue`
hang. The successful 3-iteration run is the clean e2e signal for this PR.

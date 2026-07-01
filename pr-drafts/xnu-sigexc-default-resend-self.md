# PR draft - xnu: re-raise fatal default signal to self thread, not the process group

- **beads:** dar-gwn.6.4, dar-gwn.1.5
- **repo:** darlinghq/darling-xnu
- **current commit:** `362aafc`
- **clean PR branch:** fix/sigexc-default-resend-self
- **files:** signal/sigexc.c

## Title
signal: re-raise an emulated fatal default signal to the calling thread, not the process group

## Body
When a guest process receives a signal whose application disposition is the
default action (`SIG_DFL`) and that action terminates the process,
`sigexc_handler()` emulates the effect by setting the handler to `SIG_DFL` and
re-raising the signal. It did so with:

```c
// Resend signal to self
LINUX_SYSCALL(__NR_kill, 0, linux_signum);
```

`kill(0, sig)` does **not** mean "send to self" — a `pid` of `0` targets *every
process in the caller's process group*. So when a process took a fatal default
signal (e.g. `SIGSEGV` from a genuine crash), the re-raise broadcast that signal
to its parent and sibling processes that share the process group, terminating
them as collateral damage with a spurious `SI_USER` signal.

This is observable with any crashing child under a shared shell. A concrete case
is gnulib's `CHECK_PRINTF_SAFE` probe (`configure`'s "checking whether printf
survives out-of-memory conditions"): the conftest is *designed* to crash, and on
macOS it terminates alone — `configure` records the "no" answer and proceeds. On
Darling the conftest's `SIGSEGV` was broadcast via `kill(0)` to its parent
`bash`/`configure`/`ruby`, killing the whole build ("terminated by uncaught
signal SEGV").

macOS delivers a defaulted fatal exception to the **faulting thread** of the
**faulting process** only (`ux_handler` → `psignal`/`threadsignal`). Re-raise to
this thread with `tgkill(getpid(), gettid(), sig)`, which is the `raise()`
equivalent and affects only this process.

## Tests

- A portable POSIX oracle (`fork` a child that crashes by `SIGSEGV`, parent in
  the same process group watches for a collateral hit) returns the same verdict
  on real macOS 26.5.1, native Linux, and patched Darling: **child dies alone,
  parent survives**. Unpatched Darling kills the parent (FAIL).
- Server-side `sigprocess` trace over a live `libunistring` `configure`
  fork-storm: **0** cross-process `SI_USER` signal deliveries (sender process ≠
  receiver process) with the patch, versus the pre-patch parent-directed
  `SIGSEGV` broadcasts.
- `brew reinstall wget`: `libunistring` `configure` runs past the printf-OOM
  probe (where it previously died) and through to the configure tail; no
  "terminated by uncaught signal SEGV".

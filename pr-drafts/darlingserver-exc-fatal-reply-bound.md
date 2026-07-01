# darlingserver: bound the reply wait for fatal in-process exception delivery

**Bead:** dar-gwn.1.10
**Branch:** `fix/exc-fatal-reply-bound` (base `89751e6` → `preupstream/main`)
**Status:** blocked (local-only; the timeout is a defensive bound, and live brew-green is pending — see Scope notes)

## Problem

A guest hangs forever when a thread takes a fatal `EXC_BAD_ACCESS` whose
in-process `EXCEPTION_DEFAULT` handler consumes the exception but never resumes
the faulting thread.

The concrete trigger is gnulib's `CHECK_PRINTF_SAFE` probe ("checking whether
printf survives out-of-memory conditions"), run by `libunistring`'s
`./configure` (and others). It installs a task-level `EXC_MASK_BAD_ACCESS`
handler via `task_set_exception_ports(..., EXCEPTION_DEFAULT, ...)` with a
dedicated handler thread blocked in `mach_msg(MACH_RCV_MSG)`, then deliberately
provokes a NULL dereference (a `setrlimit`-starved `printf("%.5000000f", 1.0)`
whose gdtoa `Balloc` gets `malloc` == NULL and derefs it). The crash is
*expected* — on macOS the handler catches it, calls `exit()`, and the process
terminates so `configure` records the probe "no" and proceeds. Apple's own Libc
gdtoa has the same unguarded deref; this is by design.

### Mechanism

On Darling the handler instead **deadlocks**:

- The faulting microthread runs `exception_triage_thread` →
  `exception_deliver` (EXCEPTION_DEFAULT) → `mach_exception_raise`, a
  synchronous MIG call that blocks waiting for the handler's reply, **while the
  faulting guest thread still holds a libsystem_c process-global lock it
  acquired during the float-formatting path**.
- The handler thread receives the exception and runs `exit()`, whose
  `__cxa_finalize`/stdio-cleanup path needs that same libsystem_c lock →
  `psynch_mutexwait`, owner = the faulting thread.

Both guest threads park in `recvmsg`, darlingserver goes idle, and neither RPC
ever replies — the process wedges forever and `configure` hangs.

On real macOS this is harmless because the handler's `exit()` reaches the
`_exit` syscall and `task_terminate_internal` force-aborts every other thread
(`clear_wait(thread, THREAD_INTERRUPTED)`) and tears down the address space, so
the faulting thread's held lock becomes irrelevant. darlingserver has no such
teardown: `task_terminate` is a stub, and the handler cannot even reach `_exit`
because it blocks on the held lock first.

## Fix

Bound the synchronous reply wait, but only for the fatal `EXC_BAD_ACCESS`
delivery:

1. Add `dtape_thread.fatal_exception_delivery`, set across
   `exception_triage_thread` in `dtape_thread_process_signal` **only** when the
   Mach exception is `EXC_BAD_ACCESS`.
2. In `kernel_mach_msg_rpc`, when the current microthread has that flag set, arm
   the reply `ipc_mqueue_receive` with `MACH_RCV_TIMEOUT` and a bound
   (`DTAPE_FATAL_EXC_REPLY_TIMEOUT_MS`, default 3000 ms). On `MACH_RCV_TIMED_OUT`
   it releases the reply port and returns that code.

The timeout makes `mach_exception_raise` return `MACH_RCV_TIMED_OUT`, so
`exception_deliver` fails and `exception_triage_thread` falls through to the
next handler level — the host `ux_handler`, which posts the default fatal signal
(SIGSEGV). The faulting thread's `sigprocess` RPC then returns, the guest takes
the SIGSEGV default action, and the **whole guest process terminates** — which
matches macOS's "handler didn't handle it → default action → terminate" and
releases the deadlock. The server reaps the dead guest via its pidfd.

### Why scoped to EXC_BAD_ACCESS only

`EXC_BAD_INSTRUCTION`, `EXC_ARITHMETIC`, and `EXC_BREAKPOINT` have legitimate
catch-and-resume handlers in real workloads (debuggers; the brew/Ruby download
path raises and recovers from SIGILL). Bounding those would risk turning a
recoverable, slowly-handled exception into a forced termination. `EXC_BAD_ACCESS`
delivered EXCEPTION_DEFAULT and left unresumed is the proven, unambiguous
deadlock case.

## Verification

- **Deterministic repro** (`dar-gwn110-repro/oom_printf_nocrash.c` +
  `run-repro6.sh`): a faithful model of the gnulib `nocrash_init` probe (the
  same task-level `EXC_MASK_BAD_ACCESS` handler thread + `setrlimit` +
  `printf("%.5000000f", 1.0)`).
  - **RED** (baseline darlingserver): all repro threads park in
    `__skb_wait_for_more_packets` with darlingserver idle (`ep_poll`) — wedged
    ≥8 s, flagged WEDGED.
  - **GREEN** (fixed): ~3 s after the fault the repro process *terminates* (no
    wedge); the guest session ends. Deterministic across repeated runs.
  - Server-side trace confirmed the exact sequence: task-handler raise times
    out at 3 s (`MACH_RCV_TIMED_OUT`) → triage falls through to `ux_handler`
    (success) → faulting `sigprocess` returns → guest default-terminates.

## Scope notes

- The 3 s bound is a defensive bound, not a protocol-correct handshake; it only
  ever fires when a handler consumed a fatal `EXC_BAD_ACCESS` and will never
  resume the faulting thread. A correct handler resumes in microseconds.
- Live brew-green at the `libunistring ./configure` OOM probe is the intended
  end-to-end gate; until that is demonstrated on a full run, this stays
  local-only.

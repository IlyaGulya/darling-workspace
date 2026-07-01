# PR draft - xnu: don't enable DEBUG_SIGEXC by default (RPC log flood)

- **beads:** dar-gwn.1.9
- **repo:** darlinghq/darling-xnu
- **current commit:** `7268517`
- **clean PR branch:** fix/sigexc-debug-flood
- **files:** signal/sigexc.c

## Title
signal: don't enable DEBUG_SIGEXC by default (sigexc RPC log flood)

## Body
`sigexc.c` hardcoded `#define DEBUG_SIGEXC`, which routes every `kern_printf()`
in the signal-delivery and thread state save/restore paths through
`__simple_kprintf()` — a synchronous RPC round-trip to darlingserver.

The sigexc handler and its helpers emit dozens of these lines per signal
(`dump_gregs()` alone loops 23 registers; the float/thread state save and
restore add more), so any signal- or suspend-heavy workload turns into a flood
of thousands of blocking server round-trips. Under a fork/exec storm (for
example `brew install`, whose download workers spawn and reap many short-lived
`curl` children) this collapses guest throughput: the guest spends its time
logging register dumps instead of making progress, so downloads stall and
exited children pile up as unreaped zombies.

Remove the unconditional `#define` so the debug path is compiled out by default
(the existing `#ifdef`/`#else` already provides the no-op form), and route the
per-S2C `"sigrt_handler S2C"` line through the same gate. The once-per-process
`darling_sigexc_self()` boot marker is left as-is.

`DEBUG_SIGEXC` can still be enabled via the build when actively debugging the
signal/exception machinery.

## Tests

- `brew install wget` under Darling reaches the dependency-build stage instead
  of stalling in the download phase with a backlog of unreaped `curl` zombies.
- Boot + general guest workloads unaffected (debug path was diagnostic-only).

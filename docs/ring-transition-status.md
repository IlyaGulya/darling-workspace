# Ring transition status (reconciled)

This is a coordination record, not product source. It corrects the impression left by older
migration docs and stale open Beads. Source authority remains the component Git commits plus the
`darling-workspace` manifest commit.

## Verdict

**PRODUCT/RUNTIME RING TRANSITION: COMPLETE.**

**LEGACY CLEANUP / PERF BACKLOG: open** (see below). These items do NOT mean the Ring transition is
pending.

## What "complete" means (accepted architecture)

```text
ordinary/thread RPC:            Ring
blocking thread RPC:            Ring + fiber/futex
caller-S2C:                     duplex SHM
bootstrap/lifecycle:            process management SHM
urgent/reentrant:               shared urgent slots
real FD transfer:               process SCM_RIGHTS courier
wake:                           process eventfd
ordinary AF_UNIX RPC:           0 (thread path)
per-thread AF_UNIX endpoint:    none
```

A process SCM_RIGHTS courier socket is not evidence that ordinary UDS RPC remains.

## Evidence

1. Git ancestry (proven, not inferred from branch names). In this workspace
   (`change/dtape-explicit-context`):

   ```text
   manifest:  5136c25f -> ... -> 44b580f0 -> a4dc166c (Ring acceptance checkpoint)
              -> ... -> e9ab7aac (accepted NOFILE base) -> ... -> HEAD
   ```
   `git merge-base --is-ancestor a4dc166c HEAD` = YES; `e9ab7aac` is also an ancestor of HEAD; the
   Ring acceptance checkpoint `a4dc166c` is in the current product's history.

   Component lineage (all YES):
   ```text
   darlingserver: 2186bb43 (Ring checkpoint) -> c338f90a (NOFILE) -> cd897eb (current)
   darling:       9f6fd3373 (Ring checkpoint mldr fix) -> 060fc61c (current)
   xnu:           f3e71f39 unchanged across a4dc166c, e9ab7aac and HEAD
   ```

2. The current accepted build is Ring-ON: `build/dtape-ts/CMakeCache.txt` has
   `DARLING_RING_TRANSPORT=ON` and `DSERVER_RING_TRANSPORT=ON`.

3. The accepted transport implementation is present and is the active one:
   - darlingserver `src/ring.cpp` (SPSC C2S ring, `s2cRing`, duplex mailbox, `duplexCapable`);
   - xnu `dserver-ring.c` (guest-side lane, duplex completion protocol, S2C mmap/munmap upcalls,
     eventfd wake, fd-courier adoption);
   - `src/call.cpp` routes `mach_port_deallocate` / `mach_port_mod_refs` over the duplex lane when
     the guest is duplex-capable.
   - Op-class table: 22/35 `SIMPLE_C2S`, 11/35 `SIMPLE_C2S|BLOCKING`; the only 2 non-simple entries
     are the destroy/caller-S2C ops handled by the duplex lane.

4. Acceptance ledger (`docs/ring-reintegration-wr.md`, last sections, 2026-10-05): on the pinned
   checkpoint (`a4dc166c`), the 9-mode suite is 9/9 (retry-failed-rows false, require-zero-creations)
   and the boot gate is 200/200 clean, `denied=0 created=0` (no per-thread socket creations). The
   earlier "Ring-ON boot hangs" flap was later shown to be **test-infrastructure isolation**
   (per-prefix, not per-run), tracked as `dar-agent-infra-hardening-twz1.24`, not a Ring defect.

## Tracker reconciliation (stale open Beads)

| Bead | classification |
| --- | --- |
| `dar-jj6s` (Ring-ON boot hangs / Ring-OFF does not compile) | SUPERSEDED BY LATER ACCEPTANCE (boot: 9/9 + 200/200; flap = test infra) + LEGACY/OFF-PATH CLEANUP (OFF compile) |
| `dar-b5pe` (plane slot transiently held) | ACTUALLY COMPLETE BUT TRACKER STALE (its own last comment records acceptance complete on `a4dc166c`) |
| `dar-1il.3` (design-only duplex spec) | SUPERSEDED BY LATER ACCEPTANCE (duplex mechanism present) |
| `dar-1il.3.1` (duplex prototype) | SUPERSEDED BY LATER ACCEPTANCE (children `dar-1il.3.1.1`/`.3.2.1`/`.3.2.2` closed; mechanism in product) |
| `dar-1il.3.2` (broad migration of destroy/caller-S2C ops) | PERFORMANCE/BACKLOG ONLY (the two destroy ops are already routed via duplex; further coverage is optional) |
| `dar-1il` (P5 migrate hot ops onto ring) | PERFORMANCE/BACKLOG ONLY (33/35 ops already Ring-classified) |
| `dar-agent-infra-hardening-twz1.25` (West-native Ring bootstrap, retire `manifest-ring-*`) | PARTIAL: bootstrap DONE (direct entrypoint + `west darling-bootstrap` is canonical); retiring the transitional `manifest-ring-*` profiles is LEGACY CLEANUP |
| `dar-gwn.7.7`, `.7.7.1`, `.7.7.2` (matched Ring OFF/ON parity) | LEGACY/OFF-PATH CLEANUP (OFF is not the accepted product) |
| `dar-4cp9` (stress; selfdrop names the duplex-lane gap) | PERFORMANCE/BACKLOG ONLY (measured under `DARLING_DISABLE_THREAD_RPC_UDS=1`) |
| `dar-dles` (pthread_create blocks above ~50 live threads) | REAL REMAINING PRODUCT BUG, explicitly NOT a Ring bug (reproduces with the Ring hatch OFF) |

## LEGACY CLEANUP / PERF BACKLOG

```text
1. DSERVER_RING_TRANSPORT=OFF does not compile (metrics.cpp uses the ring op-class table
   unconditionally). Legacy/fallback only; Ring-ON is the canonical product decision.
2. Retire the transitional manifest-ring-* runtime profiles (baseline/on/off) after parity.
3. Matched Ring OFF/ON acceptance for the legacy comparison line (gwn.7.7*).
4. Boot-time profile (~11-16 s/run; per-process loader/dyld/libSystem startup, not the Ring
   transport) -- dar-dar6x4-perf-5dq.35.
5. NOFILE truthfulness -- dar-dar6x4-perf-5dq.34.
6. Shared-memory SPSC ring transport for the hot-path RPC -- dar-dar6x4-perf-5dq.30.
7. Optional further duplex coverage / stress backlog -- dar-1il*, dar-4cp9.
```

## Do not re-open

Future sessions must not read a stale open Ring migration Bead as "the Ring transition is pending".
The product contains the accepted Ring transport; the remaining items above are cleanup, legacy
fallback and performance.

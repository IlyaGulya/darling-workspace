# Ring loader/attribution fixes, 2026-09-30

Durable, versioned copy of the product changes that produced the deployed artifacts
(`mldr=52b03b0ac229`, `libsystem_kernel.dylib=ab8a45074848`, `dyld=be43e3e77534`,
`darlingserver=339583b74931`) and the gates they met (acceptance 9/9 first attempt,
100/100 `basic 20`, zero legacy AF_UNIX RPC).

## Why it is a bundle and not a branch

The sources were edited in `/home/ilyagulya/work/r1-repro`, a **materialized forest**: one
`init` commit with everything untracked, so nothing pinned those edits. The canonical
checkout (`darling-gwn-resume/darling`, branch `fix/recovered-gwn-runtime`) holds a
different stack -- every changed file differs from the forest's pre-edit content by sha256 --
so the delta cannot be applied to it directly. The profile the forest came from is one of
`patches/perf` (`perf18-server-ring`), `patches/ring-comparison` or `patches/homebrew`
(the loader's plane patches live in `patches/homebrew/darling/`); pinning that exactly needs
a materialization, which is why this bundle records hashes instead of assuming a base.

## Contents

* `patches/*.patch` -- unified diffs, pre-edit snapshot -> shipped file, one per changed file.
* `MANIFEST.txt` -- sha256 of each shipped file, the patch line count, and the sha256 of the
  pre-edit snapshot each patch applies to. Those snapshots are the authority for "what this
  stage changed" when the forest is unavailable.
* `src_*` -- verbatim copies of the shipped files.

## Changes carried here

1. `rpc-supplement.h` -- the psynch family classified on the ring (waits as
   `SIMPLE_C2S|BLOCKING`, the rest `SIMPLE_C2S`); before this, `sys_psynch_*` returned negative
   on the removed transport and the guest called `__simple_abort`.
2. `mldr.c`, `threads.c` -- checkout and checkin reply attribution (a published request is
   delivered even when its reply is overtaken: `[checkout-reply-unseen]`,
   `[checkin-reply-unseen]`), the atomic process-slot protocol (three `reply_state` stores and
   one wait-loop read that were non-atomic), and a bounded retry of the idempotent
   main-thread-port read.
3. `call.cpp`, `server.cpp` -- server-side unconditional records used as instruments
   (`srv-kthread-create`, `srv-process-register`, `srv-checkin`, `srv-lane-attach`).

Instrumentation is part of this bundle because it is part of the deployed artifact; the
attribution and classification changes are the product fixes.

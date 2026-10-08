---
name: darling-diagnostics
description: >-
  Measure Darling guest/runtime behaviour with the workspace's diagnostic tools
  instead of ad-hoc shell. Use when instrumenting guest code, deploying runtime
  artifacts, comparing a built artifact against the copies a prefix actually
  loads, tracing short-lived guest processes, or deciding whether a run's output
  is a verdict at all. Prevent the silent-measurement failures these tools exist
  to remove: probes that never fire, deploys that never take effect, runs served
  by another prefix, and diagnostic lines read as verdicts.
---

# Darling diagnostics: measure with the tools, and know what a run proves

This skill is operational guidance, not a snapshot of the workspace. Re-check the
current scripts, their `--help`, and the repository rules before using them. If
they disagree, fix this skill rather than following stale commands.

Canonical sources live in the selected manifest repository:

* tools: `scripts/` (see the index in `docs/tooling.md`);
* durable reasoning and the incidents behind each rule:
  `docs/direct-transport-descriptor-architecture.md`.

## The tools and the question each one answers

| tool | question it answers |
|---|---|
| `scripts/darling-boot-run.sh` | one measured run: clean start, unique log, the workload's own duration, markers plus counters, cleanup, one `VERDICT` line |
| `scripts/darling-deploy-verify.sh` | are ALL runtime copies of a component the file I just built (sha256 per copy) |
| `scripts/darling-artifact-manifest.sh` | did any artifact change since a recorded baseline (works without git history), and does a probe tag exist in the deployed copies |
| `scripts/darling-describe-artifact.sh` | where a Mach-O starts (`LC_MAIN`), where a symbol is, whether a probe tag is in the artifact and at which file offsets, and whether the loaded copy is the built one |
| `scripts/darling-trace-guest.sh` | what a short-lived guest process ACTUALLY maps, and which files it executes |
| `scripts/darling-prefix-map.sh` | which prefix a process is rooted in, when it has no readable path |
| `scripts/prefix-cleanup.sh` | prefix-scoped process/mount cleanup, including guest processes that path matching cannot see |
| `scripts/guest-probe.h` | the probe contract, so a probe cannot silently measure nothing |
| `scripts/dwdiag` | one entry point for the run/judge/symbolize/crash family: `build`, `cycle`, `symbolize`, `crash`, `verdict`, `suite`, `witness`, `progress`, `courier`, `trace`, `denials`, `waits`, `unwind` |
| `scripts/dwdiag waits` | per ulock object: who waits, who owns, and whether a wake was EVER issued for it -- separating "nobody asked to wake it" from "a wake was issued and the server found nobody" |
| `scripts/dwdiag unwind` | guest frames, which host gdb cannot walk: per-thread `rsp` from the debugger, the words above it, filtered to guest images and symbolized. With `--catch SIGNAL` it instead attaches to a stopped target, arms a stop on that signal, resumes it and reports the signal, the registers and the instruction at `$pc` |

## Rules the tools enforce, each from a measured failure

* **A stop-at-start window must be SELECTIVE.** `MLDR_STOP_AT_START` takes a
  substring of the target's argv; an unconditional stop halts the shell that would
  launch the interesting process, so the window never opens on the target at all.
  Measured: the guest command's own bash stopped before clang ran and the linker
  never appeared.
* **A competing `sigaction` cannot catch a fatal signal.** The loader's re-arm loop
  and the runtime's own install alternate, and at delivery time the runtime handler
  owns the signal and swallows it. Measured: SIGABRT, SIGSYS and SIGILL sent to a
  guest process produced no witness line. The working path is the stop-at-start
  window plus `dwdiag unwind --catch SIGNAL`. In catch mode gdb must also be told to
  pass `SIGSTOP` through, because the target is already stopped by that signal.
* **A new instrument or command carries a contract.** `tests/waits-contract.sh` is
  the model: one fixture case per verdict class, so the command cannot silently
  collapse two different defects into one. It caught a real defect before its own
  first commit.

* **A probe must be a single string literal**, written in one syscall, with every
  ABI register saved and restored besides the `%rcx`/`%r11` that `syscall`
  clobbers. A tag assembled byte-by-byte exists nowhere as a string, so presence
  cannot be checked; a probe that clobbers `%rax` or `%rdi` returns a WRONG
  result rather than silence.
* **The raw `syscall` number is Linux-numbered in a Darling guest**: `write` is
  `1`, not `4`. A probe with `4` executes, does something else (Linux `stat`) and
  prints nothing -- indistinguishable from "the code was not reached".
* **A probe must be in the artifact under test.** Verify with
  `darling-artifact-manifest.sh --probe` or `darling-describe-artifact.sh --tag`
  before concluding anything from a silent run, and verify the deployed copies,
  not the build tree.
* **Deploy every runtime copy.** `dyld` is taken from `INSTALL_PREFIX/libexec/...`
  in some paths, and several components have two copies; a deploy to the obvious
  path alone is silent. `--component` accumulates; `--verify-probe` gates a run
  on the tag existing in the deployed artifacts.
* **A run can be served by another prefix's live runtime.** The log looks normal
  and every conclusion is about the wrong artifacts. Use
  `--assert-prefix-maps`; use `darling-trace-guest.sh` to see what a guest maps.
* **A guest process is not identifiable by path**: `/proc/<pid>/exe` is `ENOENT`
  (the image file is gone), `cmdline` is empty, `comm` is the stable name
  (`mldr`, `launchd`, `vchroot`, `shellspawn`), and `maps` is the only readable
  statement of what it runs.
* **A per-process diagnostic line is not a verdict.** Take `FINAL`, `pass=`,
  markers and counters only after the workload's own completion, and use one log
  path per run.
* **A probe must not add a syscall, and must build for every arch of the target.**
  A probe that got a thread identity from `gettid` added an emulated syscall to a
  hot path, and the measured run got shorter -- it stopped reaching the console.
  Take identity from the address of a local buffer: free, per-thread, and it does
  not need a register that 32-bit builds do not have.
* **Attribute probes.** Counts across several guest processes cannot say which one
  died; without an identity a histogram is not a bisection.
* **Check the premise before blaming the result.** Twice in one session a tool
  was "wrong" and was not: a `dd` had written an already-zero byte, and a probe
  tag really was absent because the probe wrote it one byte at a time.
* **Keep the file intact.** A scripted helper replacement cut a function out of
  `pthread_canceled.c` (the file came back empty) and the link failed with
  `Undefined symbols`; find the function's end by brace counting, assert the
  symbols that must survive, and write only after those assertions pass.

## Tooling work is not done when the tool runs once

Each tool in this list was written, used, and then corrected at least once,
because the first version quietly did less than it claimed (a `grep` without `-F`
turned a bracket-expression error into "not present"; `--component` assigned
instead of accumulating, so a two-component deploy left one stale and reported
`ok`; the harness matched its own command line and killed its own pipeline;
`prefix-cleanup` did the same). When a measurement contradicts expectation,
suspect the instrument first and prove its premises -- then fix the tool and say
so in `docs/direct-transport-descriptor-architecture.md`.

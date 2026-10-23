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
| `scripts/darling-boot-run.sh` | one measured run: clean start, unique log, the workload's own duration, the workload's own status plus markers and counters, cleanup, one `VERDICT` line (a declaration that names a value -- `--status-marker FIELD=` , or a `--marker`/`--poll` ending in `=` -- passes only on the integer 0). **It also writes the RUN BUNDLE** (below) and announces it on stdout, so the standalone-rules above are satisfied by the run itself. **It stamps `[dwdiag-env prefix=… log=… bundle=…]` as the log's FIRST line**, because `dwdiag` resolves the SERVER log from a `prefix=` field IN THE RUN LOG (a command-line `--prefix` does not override it for that) — without the stamp the tool falls back to a DEFAULT path and reads another prefix's `dserver.log` (MEASURED 2026-10-19: `SERVER-LOG-PATH … (from DEFAULT …)`, server-side facts silently dropped). It also calls `scripts/darling-stop-reason.sh` on every run and prints the cause line before `VERDICT` |
| `scripts/darling-stop-reason.sh` | **why** a run stopped, in ONE line, from the evidence the run already left: `missing-symbol` (symbol + referrer + the image that should supply it), `missing-image`, `extension-fault`, `open-errno` (the errno PAIRED with the path it belongs to), `launch-timeout` (`shellspawn` never ready), `crash`. `darling-boot-run.sh` calls it on every run and prints the line before `VERDICT`, so a bundle is READ and not merely reported. It reads the log AND the bundle's `captures/`, because a guest workload's stdout lands there: MEASURED 2026-10-19, the missing-symbol line was 0 hits in the harness log and present in `captures/` |
| `scripts/dwdiag mappings` | a core's OWN mapping table (NT_FILE): every mapping with its file offset, the mapping that owns an address, and the bytes the core carries at `ADDR:LEN`. `readelf -n` REFUSES this note on this host, and this was read by a throwaway script three times (twice with the wrong page/byte unit) before it became a command |
| `scripts/dwdiag env` | the deployed-copy digest (`RUN-ENV prefix=... mldr=<12hex> ...`) on its own, so a run started outside `verdict` can still say which build actually ran. One candidate table, in the tool |
| `west darling-build --deploy-only --deploy-component NAME` (wrapper: `scripts/darling-deploy-verify.sh`) | are ALL runtime copies of a component the file I just built (sha256 per copy); one table in `west_commands/runtime_artifacts.py` serves both this and `--deploy` |
| `scripts/darling-artifact-manifest.sh` | did any artifact change since a recorded baseline (works without git history), and does a probe tag exist in the deployed copies |
| `scripts/darling-describe-artifact.sh` | where a Mach-O starts (`LC_MAIN`), where a symbol is, whether a probe tag is in the artifact and at which file offsets, and whether the loaded copy is the built one |
| `scripts/darling-trace-guest.sh` | what a short-lived guest process ACTUALLY maps, and which files it executes |
| `scripts/darling-prefix-map.sh` | which prefix a process is rooted in, when it has no readable path |
| `scripts/prefix-cleanup.sh` | prefix-scoped process/mount cleanup, including guest processes that path matching cannot see |
| `scripts/guest-probe.h` | the probe contract, so a probe cannot silently measure nothing |
| `scripts/dwdiag` | one entry point for the run/judge/symbolize/crash family: `build`, `cycle`, `symbolize`, `crash`, `verdict`, `suite`, `witness`, `progress`, `courier`, `trace`, `denials`, `waits`, `unwind` |
| `scripts/dwdiag core` | read a core and name the faulting function AND symbolize the whole stack (signal, registers, mappings and frames come from it). Use it when dmesg is silent, the fault witness stayed armed, the guest masks the signal and a catch waits forever -- a guest's `ud2` runs NATIVELY, so none of the syscall-level machinery can see it. Mappings come from NT_FILE, never gdb (gdb lists NONE for a core whose image paths do not exist, which is every guest path), guest paths are de-virtualized back to the prefix, and the range scale is CHOSEN by scoring both against frames that land in existing files -- do not do any of that by hand. Cores are OFF here by default and `dwdiag cores` is what turns them on |
| `scripts/dwdiag loops` | decide whether a log's END is a process going round in circles: patterns repeating at the tail, plus how many DISTINCT lines the last 30 hold. The tail's variety is the measurement -- a spin ends in the same handful of lines (5-6), a run that exited keeps producing new ones (14). A varied tail RULES OUT a spin and nothing more; ask the PROCESS, not the log, whether it is still working |
| `scripts/dwdiag cores` | `status` reports the core_pattern, the size limit and a verdict (a leading `|` means the core is PIPED to a handler and nothing is kept -- that, not a `%`, is the test); `on` points the pattern at a file under `--dir`, remembering the previous value, and says that `ulimit -c unlimited` must ALSO be set in the crashing process; `off` restores. Use it before believing a crash left no evidence |
| `scripts/dwdiag trap` | read the HOST kernel's report of where a guest died (`dmesg`: process, reason, image, load base, instruction pointer) and symbolize the address inside a prefix. It parses BOTH shapes the kernel writes -- `traps: ... ip:HEX` and `NAME[PID]: segfault at HEX ip HEX` -- because a parser for one silently drops the other. Read `off` as the fact and the symbol as a hint: `symbolize` returns the nearest symbol at or below the address. A guest fault is delivered by the host, so this works when no core is kept |
| `scripts/dwdiag waits` | per ulock object: who waits, who owns, and whether a wake was EVER issued for it -- separating "nobody asked to wake it" from "a wake was issued and the server found nobody". It reads the LOG, so each stall line carries `proc=present|absent`, matched by the record's thread ids (a bare pid is not evidence: this host HAS /proc/100), and an all-absent set is reported as history rather than a current stall |
| `scripts/dwdiag unwind` | guest frames, which host gdb cannot walk: per-thread `rsp` from the debugger, the words above it, filtered to guest images and symbolized, ORDERED BY DEPTH. Sorting those words orders them by code address and turns a call chain into a set -- that was a real bug, and a unit test now pins the order. Frames come from the FRAME-POINTER chain when it validates (`frames_via=fp`) and from the word scan only otherwise (`frames_via=scan`), which is a set of plausible frames rather than a chain. With `--catch SIGNAL` it attaches to a stopped target, arms a stop on that signal, resumes it, and reports the signal, the registers, the instruction at `$pc`, and the frames resolved from the stack images gdb dumps while the target is stopped |
| `DARLING_DIAG` (in-process) | the facts only the runtime can see -- allocation history for a faulting pointer (`alloc`), CFURL release and its releaser (`url-free`), NSURL identity/refcount (`url-life`), the URL resource cache (`url-cache`). One comma-separated variable; registry in `docs/tooling.md`. The per-knob variables still work. They print on the guest's fd 1, so read the run's capture, and pass them as the token in the run's environment rather than adding another variable. |

## Rules the tools enforce, each from a measured failure

* **A run writes ONE self-describing BUNDLE, and announces it.** `scripts/darling-boot-run.sh` collects, per run, in
  `<log>.bundle`: both logs (the harness's own, and the prefix server's pre AND post -- `private/var/log/dserver.log`
  is append-only ACROSS runs, so only the delta is this run's), every guest capture WITH ITS PATH PRINTED (the xtrace
  naming `<arm-out-prefix>.trace.<pid>` / `.traceg.<pid>`, plus the workload's own redirected `*.out`), the child's
  environment and descriptor table read from the HOST while it was alive, any core written during the run, the
  abort/terminate text lifted WHOLE from wherever it landed, the plane/ledger counters, the deployed sha256 of every
  runtime copy (`BUNDLE-RUN-ENV`, from `dwdiag env`), launchd's own `Mach service lookup` lines, and the census that
  NAMES the instruments that stayed silent. Every item is a `MANIFEST` row: **PRESENT <path> or MISSING <reason>**.
  Read the console block (`BUNDLE dir=`, `BUNDLE-ITEM`, `BUNDLE-MISSING`, `BUNDLE-COUNT`, `BUNDLE-WITNESS`,
  `BUNDLE-COUNTERS`) instead of guessing where a word could be, and never present a partial bundle as complete --
  `INCOMPLETE` next to the manifest means the harness did not reach the end of the run. MEASURED why: the answer to
  the rung's question was in a DIFFERENT process's log than the one under test (launchd's, and the server's), and two
  of those logs were collected by nothing and announced nowhere.

* **The useful answer may be in ANOTHER process's log.** launchd's debug log
  (`<prefix>/private/var/log/com.apple.launchd/launchd-debug*.log`) records every Mach-service look-up WITH the
  asking process -- a resolved `Mach service lookup: X` and a landscape of `Mach service lookup failed: Y`. It is in
  the bundle and quoted in the console block for exactly that reason: "which service did the workload resolve" was
  answered from there, not from the workload's own output.

* **Nothing is truncated at a small constant; the bound is the record's own declared length.** A 512-byte cap cut
  the very buffer a `libc++abi: terminating ...` message was in, so the record that mattered arrived in half and read
  like a different one. In xtrace the bound is now the payload's own `msgh_size`/`syscall length` (or
  `XTRACE_MAX_PAYLOAD_BYTES`, default 64 KiB), and a cut is PRINTED
  (`...[truncated: printed the first N of M declared byte(s)]`). Same rule in the bundle: abort lines are copied
  whole and the longest one's length is reported.

* **An instrument must not be able to kill the thing it measures.** MEASURED: dereferencing a descriptor's ADDRESS on
  a formatting path segfaulted a traced arm 3 s in (arm `RC=139`, core `/tmp/t4cores/core.2915140`) and the run was
  lost. A bound on the LENGTH cannot prevent that -- the BASE can be stale. So every read of the traced programme's
  memory goes through `src/xtrace/safe_read.cpp`: a raw `process_vm_readv` (a syscall cannot raise, and a raw
  `syscall` is not routed through the tracer's own hooks), a short or unmapped read reported as
  `unreadable: <reason>`, and the Mach descriptor walk CLAMPED to `msgh_size` with `DECODE-STOPPED` when a message is
  merely truncated. `tests/run-xtrace-safe-read-contract.sh` holds that reader to real mappings and has a RED arm
  (the same address, dereferenced directly, must still die) -- without it the contract would pass against a reader
  that refuses everything. The 32-bit image does not read out-of-line content at all, because the guard cannot be
  built there.
* **A guest's illegal instruction is NATIVE, so no syscall-level tool can see it.** Darling does not interpret the
  guest's instructions: they run on the host CPU, and only the BOUNDARIES (syscall entry/exit, mappings, thread
  creation, signals) pass through our code. That is why `ud2` produced a silent `dmesg`, a silent fault witness and
  an empty catch, and why a core -- or the kernel's own trap line -- is the route that works. Before concluding
  "the platform is broken", check which of the two is available and read it.

* **Neither growth NOR flatness of a log tells you about YOUR process.** Growth is not evidence it is alive:
  MEASURED in the same run where `ld` had already died of SIGILL, the harness log kept growing because OTHER guest
  processes were printing, and the shell was found parked in `do_wait` -- the child it waited for was gone. The
  log belongs to the prefix, not to the process; ask the process itself (`/proc/<pid>`, `dwdiag waits`' `proc=`
  field, the run's own exit line), and use `dwdiag loops` only to rule a spin in or out.
* **A log that stopped growing is not a stalled process.** MEASURED three times in one session, twice
  leading me to a wrong diagnosis: `size` and line counts are identical whether the process is stuck or
  already EXITED, and the T4 linker was doing the second while I read it as the first. Ask the process
  (`/proc/<pid>`, `dwdiag waits`' `proc=` field, the run's own exit line), and read
  `dwdiag trap` for the kernel's account of how it died, before saying "stalled".
* **A masked signal cannot be caught by a debugger.** When a faulting guest masks the signal
  (`sigsegv-mask how=1` in the guest's own trace), gdb's `continue` never returns on it, so a catch
  session prints nothing and looks like a silent hang. The kernel's trap line is the route that works
  (`dwdiag trap`), and it exists whether or not a core is written.

* **A stop-at-start window must be SELECTIVE.** `MLDR_STOP_AT_START` takes a
  substring of the target's argv; an unconditional stop halts the shell that would
  launch the interesting process, so the window never opens on the target at all.
  Measured: the guest command's own bash stopped before clang ran and the linker
  never appeared.
* **A competing `sigaction` cannot catch a fatal signal.** The loader's re-arm loop
  and the runtime's own install alternate, and at delivery time the runtime handler
  owns the signal and swallows it. Measured: SIGABRT, SIGSYS and SIGILL sent to a
  guest process produced no witness line (`MLDR_HOLD_ON_SIGNAL` exists for this and is
  NOT a working capture). The working path is the stop-at-start
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
* **A deploy does not reach an already-running server.** A `darlingserver` that is
  already serving a prefix keeps serving the bytes it was started with, so a
  deploy is invisible to it and every conclusion drawn from that run is about the
  OLD binary. Measured 2026-10-13: a GREEN arm hung against a stale server until
  the prefix was cleaned up and the run repeated. Shut the prefix down (or
  otherwise prove the running server's identity) before believing a result that
  depends on server code, and treat a fresh deploy as unmeasured until then.
* **The guest's own probe output is not in the run log.** In-guest `__simple_printf`
  writes **fd 1** (`emulation/src/common/simple.c`), and a workload launcher may
  redirect the workload's fd 1/2 into its own file under the prefix (measured
  2026-10-13: `$PREFIX/private/var/tmp/xcodebuild-<workload>.out`); the harness run
  log then carries only the loader's/mldr's share. Reading the wrong stream gives
  conclusions that are incomplete rather than obviously wrong, which is the harder
  failure to notice. Confirm with `/proc/<pid>/fd` which file the workload's 1 and 2
  point at, and read that one.
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

## A measured run is read by stream, never by line pattern

In-path instruments that print run in the measured process's own address space, so
they write to the SAME fd as the thing being measured. Three effects then compound:
the volume pushes the few useful lines out of any tail or window; the filters in
front of a log (`grep -v` on an instrument prefix, a colour- or bracket-shaped
pattern) are written for one shape of noise and silently drop a line that mixes
noise with a real message; and because `grep` matches whole lines, a line that
holds both cannot be classified by its text at all. Measured cases: a guest
compiler's diagnostic survived only as the fragment `` generated.``, and an earlier
stage failure lost its message entirely.

Therefore:

* Split by STREAM. Give the guest's output its own fd or file and keep the host
  tools' diagnostics in the job log, so neither can consume the other. Until the
  launcher does that for you, have the workload write its result to a file under
  the prefix -- the prefix tree is host-visible, so the file is readable from
  outside without depending on transport fd separation.
* Ask the census, do not grep. `dwdiag witness` reports which registered
  instruments spoke, with counts and samples, and names the ones that stayed
  silent; a hand-written pattern can only find the line it was written for.
* Prefer not printing at all in a measured path: record to an out-of-band ring and
  decode after the run (the `dar-e1b3` direction), or gate the instrument behind an
  explicit switch that is read once at startup and defaults to off.
* When you do filter, filter only whole-line instrument shapes you have confirmed,
  and print what you removed -- a filter that reports nothing about its own
  discards is how a real diagnostic gets read as "the run said nothing".

## In-process instruments live behind one switch

Some facts exist only inside the guest: which block a faulting pointer belonged to, which caller released a CFURL,
whether an NSURL wrapper outlived its CFURL. Those hooks live in libmalloc, CoreFoundation and Foundation and are
enabled by ONE variable, `DARLING_DIAG` (`alloc`, `url-free`, `url-life`, `url-cache`; registry in
`docs/tooling.md`). Measured rules for adding another: an instrument not reachable from that variable is a fifth
variable no later session will find; a recorder installed from a constructor made every guest run fail; and
`__builtin_return_address` deeper than frame 2 failed 12/12 runs.

## Tooling work is not done when the tool runs once

Each tool in this list was written, used, and then corrected at least once,
because the first version quietly did less than it claimed (a `grep` without `-F`
turned a bracket-expression error into "not present"; `--component` assigned
instead of accumulating, so a two-component deploy left one stale and reported
`ok`; the harness matched its own command line and killed its own pipeline;
`prefix-cleanup` did the same). When a measurement contradicts expectation,
suspect the instrument first and prove its premises -- then fix the tool and say
so in `docs/direct-transport-descriptor-architecture.md`.

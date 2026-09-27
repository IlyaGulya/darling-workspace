# Diagnostic tooling for Darling guest/runtime work

Normative index. The reasoning and the measured incidents behind every rule here
are in `docs/direct-transport-descriptor-architecture.md` (sections 148-155 and
onward). The operational guidance an agent loads is the `darling-diagnostics`
skill; this file is the durable index it points at.

## Tools

| tool | answers | notes |
|---|---|---|
| `scripts/darling-boot-run.sh` | one measured run end to end | clean start (shutdown, kill by `exe` and `cmdline`, prove zero), unique log path, waits the workload's own duration, then one `VERDICT` line with the requested markers plus `sockets created`, `socket denials`, `urgent timeouts`, `courier misses`; cleans up and exits non-zero if the prefix is not clean. `--hatch` enables the socket-disabled migration hatch, `--env K=V` passes extra environment, `--verify-probe TAG` refuses to start unless the tag exists in the deployed artifacts, `--assert-prefix-maps` refuses to start when another prefix's runtime is alive |
| `scripts/darling-deploy-verify.sh` | are **all** runtime copies of a component the file just built | installs into every runtime path and compares sha256 per copy; `--component` **accumulates** |
| `scripts/darling-artifact-manifest.sh` | did any artifact change since a baseline, without git history | `--save`/`--check` over the build tree and every deployed copy; `--probe TAG` reports tag presence in every deployed copy |
| `scripts/darling-describe-artifact.sh` | where a Mach-O starts and what is really in it | `LC_MAIN` as a file offset (address = `__TEXT.vmaddr + entryoff - fileoff`), symbol address + disassembly, tag presence with file offsets, and whether each prefix copy is the built file |
| `scripts/darling-trace-guest.sh` | what a short-lived guest process actually executes | samples at 30 ms, unions every sample, filters to processes that appeared after the launch, prints the mapped Darling libraries with their sha256 |
| `scripts/darling-probe-report.sh` | a run log as a probe BISECTION | groups probes by identity, reports UNBALANCED stacks (entered, following probe never appeared), the last events in order, and the run's own reporting; reading this by hand went wrong three separate ways |
| `scripts/darling-prefix-map.sh` | which prefix a process is rooted in | reads `maps`, because a guest process has no readable path |
| `scripts/prefix-cleanup.sh` | prefix-scoped cleanup and a cleanliness gate | three matching arms: `exe`, `cmdline`, and `comm` + `maps` for guest processes; `--dry-run` first |
| `scripts/guest-probe.h` | the probe contract | single literal tag, one syscall, registers preserved, and the Linux syscall-numbering rule |

## Rules, each measured

1. **Probe contract.** One literal tag (so presence is checkable), one syscall,
   every ABI register saved and restored besides `%rcx`/`%r11`. A probe that
   clobbers `%rax` or `%rdi` returns a **wrong result**, not silence. This applies
   to every string a probe emits, not just its tag: an identity marker assembled
   character by character (as one version did for `" sp="`) exists nowhere in the
   artifact, so the presence check reports the feature missing and the next round
   is spent on the wrong suspect.
2. **The raw `syscall` number in a Darling guest is Linux-numbered**: `write` is
   `1`. A probe using `4` runs as Linux `stat` and prints nothing.
3. **A probe must be in the artifact under test, and in the copy that runs.**
   Both are checkable without running anything (rules 1 and the two artifact
   tools). A silent probe is not evidence about the code path until both hold.
4. **Deploy every copy** and verify by content; `dyld` in particular is taken
   from `libexec/...` on some paths.
5. **A run can be served by another prefix's live runtime** -- the log looks
   normal and every conclusion is about the wrong artifacts.
6. **A guest process is not identifiable by path**: `exe` is `ENOENT`, `cmdline`
   is empty, `comm` is the stable name, `maps` is the only readable statement.
7. **A per-process diagnostic line is not a verdict.** Read markers and counters
   after the workload's own completion, and keep one log path per run.
8. **Check the premise before blaming the result** -- and when a measurement
   contradicts expectation, suspect the instrument first.
9. **A probe must not add a syscall, and must be portable across the arches the
   component is built for.** `libsystem_kernel.dylib` is built for `x86_64` *and*
   `i386`, so `%rsp` is rejected by the 32-bit pass. An earlier probe obtained a
   thread identity with `gettid`, i.e. an extra EMULATED syscall on a hot path,
   and the run being measured got **shorter** -- the boot stopped reaching the
   console at all. Identity comes from the address of a local buffer: free,
   distinct per thread and per frame, no register, no syscall.
10. **Attribute a probe, or its counts explain nothing.** Fourteen entries and six
   returns across several guest processes cannot say which one died; a probe that
   prints an identity (the buffer address above) turns a histogram into a
   bisection. Two different guests in one log looked like one inconsistent guest
   until the probes carried that identity. And the identity must be the **same
   kind** in every component: a launchd tag without one cannot be lined up with a
   dylib probe that has one, so the thread whose `open-entry` has no
   `open-postcancel` cannot be confirmed as launchd's.
   It must also be **per-thread, not per-frame**. An identity taken from the
   address of the probe's own local buffer changes at every call, so it cannot
   correlate a caller with a callee (`sys_open` and `sys_openat_nocancel` have
   different frames), and "does this guest reach the syscall layer at all" becomes
   unanswerable by identity. Use the TCB self-pointer (`%fs:0` on x86_64,
   `%gs:0` on i386): per-thread, stable across the whole call chain, no syscall,
   and no TLS runtime requirement. That is what made it possible to confirm that
   launchd's thread prints its own seven tags and **no** probe from
   `libsystem_kernel` at all after `CONSOLE_OPEN_BEGIN`.

## Why these exist

Each tool was written, used, and corrected at least once, because the first
version quietly did less than it claimed:

* `grep` without `-F` on a tag (`[launchd-MAIN]` is a bracket expression) plus
  `|| echo 0` turned grep's `Invalid range end` into "the probe is not in the
  artifact";
* `grep -c` returns 1 for "no match", which was read as failure;
* `--component` assigned instead of accumulating, so `--component a --component b`
  deployed only `b`, reported `ok`, and left `a` stale -- a whole run then
  measured the wrong dylib;
* the run harness matched its own command line (it is invoked **with**
  `--prefix`) and killed its own pipeline; `prefix-cleanup` had the same defect;
* a probe wrote its tag one byte at a time, so no tag existed as a string and
  presence was unverifiable;
* a probe used syscall `4` instead of `1` and silently did nothing.

When you find another, fix the tool, add the rule here, and record the incident in
the architecture document.


## The diagnostics are ONE tool: `dwdiag`

Everything that used to be a separate shell script for this work -- judging a guest workload, running a set of them,
symbolizing an offset, resolving a crash -- is now a subcommand of the Rust tool in the sibling tooling repository:

```
scripts/dwdiag <symbolize|crash|verdict|suite> [OPTIONS]     # build-and-exec shim, stable path
  = tools/darling-debug-runner/target/release/darling-debug-runner diag ...
```

The tool is **vendored in this workspace** (`tools/darling-debug-runner`, source + README + `Cargo.lock`), so its code,
its documentation and every instruction that calls it live in one tree; the sibling repository of the same name is only a
fallback for an older checkout. Concrete, verified invocations:

```sh
# offset -> `symbol + offset` (server binary or guest dylib); `--addr` for an absolute address, `--json` to compose
scripts/dwdiag symbolize --binary ~/work/ringmm-build/src/external/darlingserver/darlingserver \
  --base-symbol dserver_crash_probe --delta 0x19d54c
# crash line -> location + stack walk + disassembly around the fault
scripts/dwdiag crash --binary ~/work/ringmm-build/src/external/darlingserver/darlingserver \
  --log $(ls -t /tmp/darling-boot-*.log | head -1)
# one workload, judged by its OWN line, naming the first denial and its caller
scripts/dwdiag verdict --prefix /tmp/dr-on-matched --mode sem_timed --args "300 1" --wait 60
# a set of workloads -> one table with denied/created per row (exit 1 if any row is not PASS)
scripts/dwdiag suite --prefix /tmp/dr-on-matched --wait-base 60 --require-zero-creations \
  -- 'sem_ready 2 :: sem_block 100 1 :: basic 20'
```

Why one tool rather than four scripts: they share two primitives (`llvm-nm` symbolization and reading a run log), they
must agree on one verdict rule, and every one of them was measured getting a verdict or a parse wrong in a way the others
could not see. The interface is meant to be composed rather than scraped:

* **one verdict rule** -- `RING_MACH_TEST mode=<M> ... pass=1`; the ABSENCE of that line is `FAIL`/`HANG`, never PASS
  (MEASURED: a harness marker that matched a workload's START line reported PASS for two workloads that never finished);
* **`--json` on every subcommand**, so callers chain `crash` into `symbolize` instead of parsing text;
* **stable exit codes** -- 0 ok/PASS, 1 verdict or acceptance failure, 2 usage, 3 tool error;
* it **delegates** rather than reimplements: prefix start/stop stays in the boot harness (`--boot-runner`), and the
  symbol table stays `llvm-nm`'s, because a second implementation of either is a second source of truth.

Defects this tool found in ITSELF while being used, each fixed and kept as a rule:

* the boot harness was invoked as `--cmd "" <command>`, i.e. two arguments, so it exited instantly and the verdict
  reported `NO-RUN` -- a tool defect that looked exactly like a workload that failed to start;
* the JSON escaping handled quotes, backslashes and newlines but not tabs or other control characters, so a crash
  document with a disassembly in it was **unparseable** -- a tool that emits "json" a consumer cannot parse is worse
  than one that emits text, because the consumer trusted it;
* crash fields were parsed with a first-match-else chain, so `addr` was silently empty whenever it shared a comma-field
  with `sig` (`[dserver-CRASH sig=b addr=0x0`) -- a parse that looked right and dropped a field.


### `dwdiag progress` -- where a run stopped

`darling-workspace/scripts/dwdiag progress --log RUN_LOG [--guest-log MLDR_DIAG_LOG] [--mode M]` answers the question a
stalled acceptance run always raises, from the two logs the run already wrote, without a shell pipeline: whether the
workload produced its own machine-readable line, the last `[mldr-ctl]` stage the guest loader reached (a `seq=N after-*`
bootstrap stage, the request it published, or the spin it is in), and the last plane op the **server serviced**.
Published-versus-serviced is the diagnosis: a request that the guest published and the server never serviced is a
server-side stop, and one the server serviced while the guest still waits is a completion that did not land.
`--json` is supported; `verdict` composes the same summary into a `VERDICT-STAGE` line whenever its verdict is not PASS,
so a HANG no longer needs a manual grep to be readable.

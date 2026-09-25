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

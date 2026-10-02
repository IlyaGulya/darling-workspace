# Diagnostic tooling for Darling guest/runtime work

Normative index. The reasoning and the measured incidents behind every rule here
are in `docs/direct-transport-descriptor-architecture.md` (sections 148-155 and
onward). The operational guidance an agent loads is the `darling-diagnostics`
skill; this file is the durable index it points at.

For the **interactive** counterpart to these batch tools — attaching to a live
runtime and asking what the server believes about one guest thread — see
`docs/darling-debugger.md`. The two are complementary: this file's tools produce
repeatable regression evidence, while the debugger inspects live state to form
the hypothesis that a regression then pins.

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
scripts/dwdiag <build|cycle|symbolize|crash|verdict|suite|witness> [OPTIONS]   # build-and-exec shim, stable path
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
  The absence is classified by the WORKLOAD'S OWN exit status, which the guest shell prints (`__DWDIAG_RC=$?`):
  `CRASH <SIG>` + `rc=128+N` (died of a signal -- the cause, named), `EXIT rc=N` (returned without its line), `HANG`
  (no status at all), `NO-RUN` (never started). MEASURED 2026-09-27: `basic 2`/`basic 3` die of `SIGSEGV` at the
  second iteration's port teardown, which produced byte-identical evidence to a deadlock and was reported as `HANG`;
  the investigation then spent a long stretch chasing a lock that never existed. `--json` carries `rc` and `signal`.
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


### `dwdiag build` and `dwdiag cycle` -- the iteration loop, with the paths out of the command

```
DWDIAG_BUILD=/home/ilyagulya/work/r1-repro-build DWDIAG_PREFIX=/tmp/r1-repro-prefix \
  scripts/dwdiag build --expect '[sigexc-in '
BUILD targets=libsystem_kernel.dylib,dyld rc=0 errors=0
  artifact .../libsystem_kernel.dylib stamp=12:33:08 bytes=2777588
  artifact .../dyld stamp=12:36:19 bytes=5755632

DWDIAG_BUILD=... DWDIAG_PREFIX=... \
  scripts/dwdiag cycle --mode basic --args 20 --repeat 3 --probe sigexc-in,native-exit
CYCLE[1/3] mode=basic verdict=CRASH SEGV (Segmentation fault) denied=0 created=0 rc=139
  instruments fired=[native-exit=2 sigexc-in=1] silent=[iter-marks] log=/tmp/dwdiag-verdict-...log
```

`build` exists because the pair rule is easy to violate: the same loader code is compiled twice, once into
`libsystem_kernel.dylib` and once into the `dyld` image, so building only one leaves the other stale -- measured
during this work, and it cost a full rebuild-and-run cycle to notice. `--expect STRING` verifies the instrument
string is present in the BUILT file, which is the check that turns "the probe printed nothing" from a mystery into
a statement about the artifact.

`cycle` exists because the four steps it composes were retyped as a long shell command at every attempt. The
artifact pair is the default, the build tree and the prefix come from `DWDIAG_BUILD`/`DWDIAG_PREFIX`, the verdict is
the workload's own line, and the instrument census comes from `witness` rather than from a grep: **silent** is
reported next to **fired**, because a probe that cannot speak is indistinguishable from a guard that does nothing.

### `dwdiag progress` -- where a run stopped

`darling-workspace/scripts/dwdiag progress --log RUN_LOG [--guest-log MLDR_DIAG_LOG] [--mode M]` answers the question a
stalled acceptance run always raises, from the two logs the run already wrote, without a shell pipeline: whether the
workload produced its own machine-readable line, the last `[mldr-ctl]` stage the guest loader reached (a `seq=N after-*`
bootstrap stage, the request it published, or the spin it is in), and the last plane op the **server serviced**.
Published-versus-serviced is the diagnosis: a request that the guest published and the server never serviced is a
server-side stop, and one the server serviced while the guest still waits is a completion that did not land.
`--json` is supported; `verdict` composes the same summary into a `VERDICT-STAGE` line whenever its verdict is not PASS,
so a HANG no longer needs a manual grep to be readable.

### 2026-09-27: two harness defects that made a run unreadable

* **`--marker` split multi-word markers.** The boot harness stored markers space-separated and re-split them with a
  plain `for`, so `--marker 'ITER 0 dropped'` became `ITER`, `0`, `dropped` and reported `MISS dropped` for a
  marker that was never queried as a whole. Markers are `|`-separated now and the IFS change is restored after the
  loop.
* **A probe on a pre-libc path used `getenv`.** A diagnostic added to the guest's `mach_msg_overwrite` trap read its
  hatch with `getenv` on first call; `mach_msg` runs during bootstrap, so the boot stopped reaching the workload
  (`Rootless shellspawn did not become ready within 30000ms`) and the harness reported only a generic failure. It
  now scans `/proc/self/environ` with raw syscalls, which is what `dserver-ring.c` already did. Any probe reached
  before libc MUST use that scan.

### 2026-09-27: the guest must be able to describe its own death

A guest crash is invisible from the host: guest tids are not host pids (so `/proc/<tid>` sampling cannot name the
faulting thread) and a `SIGSEGV` inside Darling's emulated-syscall window does not reach a handler the workload
itself installed -- the program-level reporter added to `ring_mach_msg_test` (signal, fault address, fault PC,
returning frames with image+symbol, `sigaltstack` for stack-overflow faults, `_exit(128+sig)` because `raise()` is
a denied `pthread_kill` under the hard-socket hatch) prints nothing in that case, while it does print for a
deliberate fault (`crash_test` mode). A guest fault reporter therefore belongs in the guest's own signal-exception
layer (`emulation/src/linux_premigration/signal/sigexc.c`, which already has register/mcontext helpers), not in the
program under test.

### 2026-09-27: a fatal guest signal had NO observability in a normal build

`sigexc.c` wraps every diagnostic in `kern_printf`, which is defined to **nothing** unless `DEBUG_SIGEXC` is
compiled in. The path that actually kills a guest -- default-effect signal handling -- therefore produced no line
anywhere, and hours went into reading a silent death as a deadlock. The file now emits one **bounded, raw** line per
fatal signal number per process:

```
[sigexc-fatal sig=15 code=0 addr=0x3E80018FAE6 pid=1636880 tid=1636880]
[sigexc-default sig=15 tid=1636880]
```

`__simple_fprintf` is a plain write, safe from a raw handler on `sigexc_altstack`; the once-per-signum bound keeps a
signal storm from flooding the log. Validated by running the shape it exists for: a deliberate guest fault
(`ring_mach_msg_test crash_test`) reports `sig=11 code=1 addr=0x0` for the faulting process **and** the fixture's own
reporter reports the same process -- two independent instruments agreeing.

Two rules follow from using it:

* **`sigexc.c` is compiled TWICE** (`emulation.dir` -> `libsystem_kernel.dylib`, `emulation_dyld.dir` -> the `dyld`
  image). Rebuilding `system_kernel` alone leaves the loader copy stale (measured: the `emulation_dyld` object was six
  hours older and contained no instrument), so a diagnostic in this file needs `--target dyld` and a deploy of **both**
  prefix copies, sha256-verified.
* **The harness's own stop signal is not the subject's death.** Every run of a hanging workload shows
  `[sigexc-fatal sig=15 code=0 ...]` for a *neighbouring* guest process with `code=0` (`SI_USER`, `uid=1000`, the
  sender's pid in `si_addr`): that is the tool stopping what it started. A verdict about the subject must name the
  subject's own pid.

### 2026-09-27: probes that killed the process they measured

Two diagnostics added to `ring_mach_msg_test` stopped the workload at the probe itself, and both were caught only
because the marks that should follow them disappeared:

* `pthread_sigmask(SIG_SETMASK, NULL, &cur)` -- `SIG_SETMASK` with a NULL set is undefined; the call never returned.
  The defined query (block an empty set, take the previous mask) works and is what the probe uses now.
* `sigaction(SIGSEGV, NULL, &old)` -- the disposition *query* did not return in this guest; the mark after it never
  appeared. The probe was removed rather than guessed at.

Keep every probe bracketed by its own mark, so a probe that breaks the subject is visible as a missing mark instead of
as a missing crash.

### 2026-09-27: `--repeat` and an explicit `LOG=` line (a verdict that cannot see flake)

The verdict command gained `--repeat N`, one `LOG=<path>` line per run, and a `STABILITY mode=… runs=… pass=…
stable=… distribution=…` summary; the exit status is non-zero unless every run passed. The same command produced
`PASS` and `CRASH SEGV` on consecutive runs all session, and each single-run reading was treated as a change in
behaviour. Any claim about a failing shape must now come from repeated runs.

`scripts/dwdiag` also rebuilds when the source is newer than the binary and prints `tool=… binary=…`. It previously
built only a **missing** binary, so an edited tool kept answering from the stale one (a new flag was absent from
`--help` while the sibling repository's copy had it).

### 2026-09-27: an oracle that reported PASS for a workload that never ran

`ring_mach_msg_test delay 0 N` read its iteration count from `argv[delay_ms ? 3 : 2]`, so with `delay_ms == 0` it
took the count from `argv[2]` -- the **delay** -- ran zero iterations and printed `pass=1`. Four such runs were read
as "the destroy is safe with a delay". The index is now a property of the mode, and `iters == 0` prints
`pass=0 error=no-op` and exits non-zero: a workload that did not execute must never report success.

Measured consequence, after the fix: `delay 0 1` and `basic 1` are the same workload and both crash, while
`delay 300 1` really runs and passes.

### 2026-09-27: the guest Darwin-syscall trace, and the two ways it broke the guest

`threadnoop 2` proved the current failing subject without ports, messages or destroys: **create a thread that
does nothing, twice** -> `CRASH SEGV` 4/4, and the death is inside the *second* `pthread_create` (the first
create/join completes; `[bsc wrap pre]` never prints for the second, and the server sees nothing for it). To name
the Darwin syscall reached there, the dispatcher gained an opt-in trace (`DARLING_GUEST_SYSCALL_TRACE=1`) in the
13-byte entry hook of `__darling_bsd_syscall` -- the same hook xtrace patches, occupied statically as `jmp rel32`
plus 8 NOP bytes.

Two implementations were MEASURED breaking the guest, and both lessons are now enforced by
`tests/run-guest-syscall-trace-contract.sh`:

* **The trace captured itself.** The first version printed with `__simple_printf`, whose output takes a `write`
  syscall through this very dispatcher: 21 of its first 28 lines were the emitter's own writes and the guest
  stopped starting (NO-RUN 3/3). The trace therefore emits with a RAW Linux `write` and formats its own line (the
  discipline the plane probes already use), and the contract fails if one syscall number dominates the trace.
* **The trampoline is copied, not invented.** A hand-rolled save/restore (7 pushes, `subq $16`) turned every run
  into NO-RUN with the hatch *off* as well, i.e. it corrupted syscall delivery globally. The implementation now
  copies xtrace's proven `trampoline_enter`/`trampoline_leave` pattern for this exact hook, and the contract's
  first claim ("the instrument preserves the guest when it is off") is the oracle that catches it.
* **The hook is a `call`, not a `jmp`.** MEASURED: a `jmp` into the trampoline is followed by the trampoline's
  `ret`, which returns to the *syscall's caller* and skips the entire dispatcher -- every Darwin syscall then
  returns garbage and every run is NO-RUN, hatch or not. xtrace installs this hook with
  `setup_hook(..., jump=false)`, i.e. a call; the mnemonic is the parameter's name. The 13 bytes hold
  `call rel32` + 8 NOP exactly as well as a `jmp` does, and a semantics-preserving `jmp` inside the same 13 bytes
  was measured to work, which is what separated "the hook area is not statically occupiable" from "my jump kind
  was wrong".

The contract is registered in `ci/run-host-tier.py` as an EXCLUDED contract with its reason: it needs a booted
prefix and a guest runtime, which the host tier does not own.

### 2026-09-27: instrument the branch that is COMPILED

`bsdthread_create.c` builds with `-DBSDTHREAD_WRAP_LINUX_PTHREAD` (measured in `build.ninja`), so its
raw-`clone` branch is compiled out entirely, and the `emulation_dyld` copy of the whole file is excluded by
`#ifndef VARIANT_DYLD`. Marks placed in the dead branch are invisible by construction while looking perfectly
correct in the source; check the built object (`strings` on the `.o`) before trusting any instrument in a file
that is built more than once.

### 2026-09-27: the dispatcher trace is real, and its COVERAGE is measured, not assumed

`tests/run-guest-syscall-trace-contract.sh` is green: the guest is unaffected with the hatch off, the traced arm
still completes, the log carries thousands of `[bsys nr=` lines covering dozens of distinct syscall numbers, and one
number does not dominate (the self-tracing signature). The contract's own first version had a defect worth keeping:
the verdict list arrives space-separated, so `NO-RUN PASS` satisfied a "no verdict without PASS" check and a run that
never started counted as "the instrument preserves the guest"; every verdict token must now be exactly `PASS`.

Calibration of what the trace does and does not see (measured, `threadnoop 1 --env DARLING_GUEST_SYSCALL_TRACE=1`):

* 10241 traced lines in the run, 81 distinct numbers -- the instrument is not silent;
* for the WORKLOAD's own thread, only 6 lines, and **no `nr=4`** (`write`) although that thread printed five marks
  through `printf`. The same `nr=4` floods appear for the harness's own binaries in the same run.

So the trace covers the `call __darling_bsd_syscall` entry (which `SYS.h` emits for every generated stub when
`DARLING` is defined) and does NOT cover whatever serves the workload binary's own `write` -- i.e. a second entry
that must be found before this tracer can be used to say "the second `pthread_create` issues no Darwin syscall".
Until then, "absent from the trace" is a statement about this entry only, and it must not be read as "no syscall".

### 2026-09-27: ROOT CAUSE of the session's silent guest death (fix applied and verified)

`_pthread_deallocate` (guest `libsystem_pthread`) freed the thread's stack+pthread-object region with
`mach_vm_deallocate(t->freeaddr, t->freesize)` -- called from the **joining** thread -- while the loader
(`darling_thread_entry`'s exit branch) already `munmap`s exactly that region (`t_freeaddr`/`t_freesize`, the
arguments the guest passes to `__darling_thread_terminate`), from the thread that is actually finished. Two owners,
two frees; the guest's fires while the memory is still live (the exiting thread's loader-side teardown and
libpthread's own bookkeeping read the pthread object that sits above the stack in that same mapping).

Signature it produced, all measured: a **silent** SIGSEGV (rc=139, no handler report, no server RPC) whose location
varied between the next `pthread_create`, the syscall dispatcher and the program's exit path; it needed a
create+join cycle (`threadnoop 2`, `basic >= 1`, `stress_churn`, `basic`'s helper thread) and passed with no thread
(`selfdrop`, `timeout`, `sem_ready`). glibc reported `free(): invalid pointer` in a crashing run once
`MALLOC_CHECK_=3` was set, which is what named the class.

Decided by measurement, not argument:

* disabling the **guest** deallocation (`DARLING_GUEST_NO_PTHREAD_DEALLOC=1`) -> `threadnoop 2`, `basic 3`,
  `stress_churn 3` all `PASS 3/3` stable;
* disabling the **loader** unmap -> still `CRASH` (so the guest's free is the harmful one);
* with the fix committed (deallocation removed, ownership left to the loader), no hatch involved:
  `threadnoop 2`, `basic 1`, `basic 3`, `delay 0 1`, `stress_churn 3`, `selfdrop 2`, `timeout 200 4` are all
  `PASS 3/3 stable`.

Lessons kept as rules: an mmap'd region's lifetime has ONE owner; a hatch that removes a free is how a double free is
decided (and it must be a hatch, not a guess); and `MALLOC_CHECK_`/`MALLOC_PERTURB_` in the loader's environment is
the cheapest way to make heap corruption speak.

### 2026-09-27: the workload's line must name the mode the CALLER asked for

`sem_gap <ms> <n>` runs `sem_block`'s implementation and reported `RING_MACH_TEST mode=sem_block ...`, while
`dwdiag verdict --mode sem_gap` looks for the mode it was asked to run. A **passing** 5.001 s semantic wait was
therefore reported as `EXIT rc=0 :: <no result line>` -- a false acceptance failure, and the kind of misreport that
makes a green workload look broken. The result line now carries the requested mode (`run_sem_block` takes its label
from the caller). Same rule as the verdict rule itself: the workload's own machine-readable line is the oracle, so it
must answer the question that was asked.

### 2026-09-27: a denial is a migration signal even when the row PASSES, and a row flake must not decide the suite

Two tool defects, both measured on the acceptance table after the `_pthread_deallocate` fix:

* `sem_gap 5000 1` passed 3/3 **while one run reported `denied=1`**, and the tool printed the denial's call site
  only for non-PASS rows -- so the one fact needed to remove the last datagram dependency was hidden by a green
  verdict. `verdict` now prints `DENIAL denied=<n> first-denial=<call> caller=<location> verdict=<v>` whenever
  `denied > 0`.
* Two rows failed in one suite run (`sem_gap 5000 1` HANG, `basic 20` NO-RUN) and then passed 3/3 and 4/4 when run
  individually, i.e. a row-level flake decided the suite verdict. `suite` retries a row that is not PASS **once**
  and prints `ROW-RETRY mode=<m> first=<v1> retry=<v2>`; the retry is announced, never silent, because hiding the
  first verdict is the same defect as a verdict that cannot fail. `--retry-failed-rows false` turns it off.

### `darling-boot-run.sh --cmd` replaces the boot command, so the default markers do not appear

Measured 2026-09-28: a churn run (`--cmd 'shell -c "…"'`) was reported `VERDICT FAIL` with `MARKER MISS HELLO=1` and
`MARKER MISS FINAL=1`, and it was read as a product failure. It was not: `--cmd` runs THAT command instead of the
default one, and `HELLO=1`/`FINAL=1` are printed by the default command, so their absence is expected and carries no
information about the product. When a run supplies `--cmd`, judge it by the markers that command prints and by the
counter block; the boot markers only apply to a default boot. A `--cmd` run that neither prints its own completion
marker nor its expected output has not executed the workload at all, and that is what the two zero counts above
(`checkout-skipped`, `R1-CHURN-DONE`) actually said.

## darling-debug-runner: self-attributing run logs (`RUN-ENV` / `[dwdiag-env ...]`)

FIXED (measured cause): a run's log recorded what the guest and the loader printed but not **which copy of the
loader served it**. Two prefixes existed -- one freshly deployed, one stale -- and a log served by the stale copy
was read as evidence about the fresh build, twice, before the mistake was caught by hand. Any per-thread claim
drawn from such a log ("this thread never entered the loader") was therefore unsound.

`dwdiag cycle`/`dwdiag verdict` now print a single machine-readable identity line before the workload starts,
write the same line to a sidecar next to the log, and append it to the log once the run has finished:

```
RUN-ENV prefix=/tmp/r1-repro-prefix mldr=8dcb3246bcb4 libsystem_kernel.dylib=13170ac1ab11 dyld=3f97b08dac35
[dwdiag-env prefix=/tmp/r1-repro-prefix mldr=8dcb3246bcb4 libsystem_kernel.dylib=13170ac1ab11 dyld=3f97b08dac35]
```

Rules this makes enforceable: a log that carries no `[dwdiag-env ...]` line was produced by an older tool or by a
hand-rolled command and cannot support a claim about a specific build; when two runs disagree, compare their
`mldr=`/`dylib=`/`dyld=` digests before comparing anything else; and the digests are of the **deployed** prefix
files, so they answer "what actually ran", not "what was built".

### `suite` honours `DWDIAG_PREFIX`

FIXED (measured friction): `cycle` resolved the runtime prefix from `DWDIAG_PREFIX` but `suite` required the
`--prefix` flag, so a run started with the variable exported died on clap's usage error and read like a broken
tool rather than a missing argument. `suite` now resolves the flag-or-variable pair the same way the other
subcommands do. Verified by use without the flag: `rows=2 failures=0 SUITE-VERDICT PASS`.

### `dwdiag source`: refuse a build tree that does not compile the file you edited

FIXED (measured friction): this stage edited the darlingserver's thread creator, ran `ninja darlingserver`, read
`ninja: no work to do`, and only then found that the build tree has **zero** rules mentioning that source file --
the target imports a prebuilt binary, so a probe there could never appear in a run and a cycle would have reported
"the instrument stayed silent" about a byte-for-byte old artifact.

`dwdiag source --build DIR --source FILE` answers from the generated build graph, not from timestamps (a target
that imports a prebuilt artifact looks up to date whatever the source tree says). It prints, one line per file:

```
SOURCE-UNBUILT  file=kern_support.c rules=0  build=... -- no rule ...; a run here would exercise the OLD artifact
SOURCE-CONSUMED file=threads.c      rules=54 build=...
```

Exit code 3 means "do not trust a run that claims to exercise this source", which turns a silent no-op probe into
a refusal. Verified by use on both cases above (`rules=0` rc=3; `rules=54` rc=0).

### Run logs are read lossily (a passing run must not be erased by one binary byte)

FIXED (measured defect): a run log carries whatever the guest and the loader wrote to fd 2, including raw
register/pointer dumps from probes that deliberately bypass libc. The verdict path used
`fs::read_to_string`, so the FIRST invalid byte made the read fail, and every caller treated that as "no log text":
`dwdiag verdict --log <log>` then reported **NO-RUN for a run that had printed its own `pass=1` result line**, with
the only evidence an error buried in a transcript. Reproduced and fixed on a real log:
`/tmp/dwdiag-verdict-284379-threadnoopr6.log` contained `RING_MACH_TEST mode=threadnoop iters=5 pass=1` and was
judged NO-RUN before the fix, PASS after it.

Every log read now goes through `read_log_lossy` (bytes -> `String::from_utf8_lossy`); `/proc` reads that must stay
strict keep `std::fs::read_to_string`. Consequence for method: any verdict recorded before this fix must be treated
as possibly a READ failure rather than a run failure, and re-judged offline with the current tool.

### A diagnosis reads the prefix the RUN used, from the run's own identity line

FIXED (measured defect): several diagnostics carried a hardcoded default of `/tmp/dr-on-matched`, so a run served by
another prefix had its server-side evidence read from the WRONG prefix's log -- and an absence there reads like "the
server never sent it". Observed on a real hanging run: `dwdiag progress` reported
`SERVER-SENT plane-doorbell-sent=19210 (from /tmp/dr-on-matched/private/var/log/dserver.log)` while the run used
`/tmp/r1-repro-prefix`.

`resolve_prefix()` now answers in a fixed order -- an explicit flag, then `DWDIAG_PREFIX`, then the `prefix=` field of
the `[dwdiag-env ...]` line the run itself wrote -- and the server-log default is derived from it. Verified by use on
the same log: `SERVER-LOG-PATH /tmp/r1-repro-prefix/private/var/log/dserver.log`.

### `--artifact` accepts the guest's pthread library

FIXED (measured friction): the deployable-component table knew `darlingserver`, `mldr`, `libsystem_kernel` and `dyld`
only, so `dwdiag cycle --artifact libsystem_pthread.dylib=...` refused with "unknown component" even though the
prefix installs it fine -- a dead end in the tool while diagnosing the thread-creation path, which lives in exactly
that library. The component is now in the layout table (both prefix copies) and verified by use:
`PREFIX-INSTALL MATCH ... usr/lib/system/libsystem_pthread.dylib` and the `libexec/darling/...` copy.

## A prefix is a SET, and a launch failure is its own verdict

Added to `scripts/dwdiag` after a measured two-hour wall. `dwdiag deploy --build B --prefix P` installs the seven
runtime components from one build tree and verifies each deployed copy by sha256 (paths owned by
`scripts/darling-artifact-manifest.sh`). `dwdiag cycle` prints `PREFIX-PREREQ` before running, and `dwdiag verdict`
returns `BOOT-FAIL (<cause>)` for logs carrying `Failed to exec launchd`, `shellspawn did not become ready` or
`no recognized stable state`.

The rule this encodes: a mixed-build prefix boots into a failure that looks like something else. In the measured
case the prefix held a darlingserver from the instrumented tree, guest libraries from a second tree and
shellspawn/launchd from a third; the run log said only `Failed to exec launchd: No such file or directory`, and the
verdict said `HANG (watchdog)` ten times in a row. A verdict about a prefix must never be readable as a verdict
about the workload, and a component set must never be assembled by hand one file at a time.

## ETXTBSY belongs to processes, so the tool names them

An aborted cycle left `darlingserver` (reparented to pid 1), `launchd` and `shellspawn` alive in the prefix. The next
install of `mldr` failed with `ETXTBSY`, and the tool reported only `installing .../mldr (after a shutdown attempt;
first error: Text file busy)` -- no holder, no next step, and the gate run died at staging rather than at the
workload. `bin/darling --rootless shutdown` stops the server the harness knows about, not the guest processes it does
not.

`install_artifact_into_prefix()` is now the ONE way both `deploy` and the cycle's staging path put a file into a
prefix: copy, and on failure shut the server down, stop the processes that still hold the prefix (matched on `exe`
AS WELL AS `cmdline`, because a guest process runs through the prefix's `mldr` so its `exe` is the loader and its
`cmdline` is the guest argv, while the server's `cmdline` names the prefix without `exe` doing so; the caller's own
ancestry is excluded at every depth), retry once, and report `PREFIX-STOP-HELD pids=...` when holders were found.

Verified by use: a deliberate holder executing a staged file produced `PREFIX-STOP-HELD ... pids=2359645`, the copy
then completed, and the destination was restored to the build's sha256.

## Server-log resolution (fixed 2026-09-30)

`dwdiag progress` reads the server's own log for the counts it prints beside a run
(`SERVER-SENT plane-doorbell-sent=...`). It used to fall back to a hardcoded
`/tmp/dr-on-matched` when the caller did not pass `--server-log`, so a run of
another prefix was reported with ANOTHER prefix's numbers: measured on a
`/tmp/r1-repro-prefix` boot failure, the report said 19228 doorbells read from
`/tmp/dr-on-matched/private/var/log/dserver.log`, and after the fix the same run
reads 16894 from its own prefix.

Resolution order is now: the explicit `--server-log`, then the prefix recorded in
the run log's own identity line, then the default. The chosen path is always
printed with its source, and the default case says so out loud, because a count
whose file belongs to another prefix is worse than no count.

## Guest fixture refresh (fixed 2026-09-30)

`dwdiag cycle` runs the guest fixture at `/private/var/tmp/ring_mach_msg_test`
(inside the prefix). It used to take whatever copy was already there, so a run of
an OLD fixture was indistinguishable from a run of the new one. Measured: the
`forkexec` mode was added to the workload source, the workload was rebuilt, and
the run still answered `RING_MACH_TEST mode=forkexec pass=0 error=unknown_mode`
because the prefix held a fixture built hours earlier.

`cycle` now refreshes the fixture from the build tree (`DWDIAG_BUILD`,
`src/tools/<name>`) before the run, verifies the copy by sha256 like every other
artifact, and prints the outcome:

```
FIXTURE-INSTALL MATCH <sha256-16> <path>            # refreshed, or already current
FIXTURE-INSTALL MISSING-BUILD-ARTIFACT <path>       # source absent; the run proceeds and says so
FIXTURE-INSTALL FAILED <error>
```

Verification by use: with the refresh in place the same `forkexec` run reports
`FIXTURE-INSTALL MATCH 6a91f572f3b789b3` and `verdict=PASS denied=0 created=0`.

## Workload: `forkexec` mode

The workload now implements the mode its own tool list advertised: it forks, the
child `execve`s the same binary with a small transport-using mode, and the parent
waits and counts results, printing

```
RING_MACH_TEST mode=forkexec iters=<n> child_ok=<n> child_bad=<n> pass=<0|1>
```

so a successful child is evidence the transport survives both fork and exec,
rather than an assumption.

## `cycle --capture-wchan` (added 2026-09-30)

`dwdiag cycle --capture-wchan` samples `/proc/<pid>/wchan` and the process state
for every process whose cmdline names the prefix, once a second, for the whole
run -- including the harness' boot wait, which is the window a boot failure
occupies. On a non-PASS verdict it prints the sample count and the last 24
samples:

```
CAPTURE-WCHAN samples=<n> prefix=<path>
CAPTURE-WCHAN s=<second> pid=<pid> state=<S|D|R> wchan=<symbol> cmd=<cmdline>
```

WHY: the boot failure this was written for leaves no guest-side evidence, and its
rate moves when the GUEST is instrumented (per-iteration marks took it from 10/10
to 7/10). Sampling from the host changes nothing in the guest, so this is the one
question that can be asked without perturbing the measurement. It prints the
count so that an empty capture is never read as "nothing was blocked".

Measured use: twelve consecutive boots with the flag on were all PASS, so the flag
had nothing to print -- which is itself the correct behaviour and the reason the
count must be printed rather than inferred.

### `--capture-wchan`: per-pid aggregation (added after the first real catch)

The first catch printed only the last 24 samples, and by then every guest process
of the prefix had exited: the output showed the harness shell waiting in
`do_wait` and nothing else -- true, and useless for the question asked. The flag
now prints, on a non-PASS verdict:

```
CAPTURE-WCHAN samples=<n> prefix=<path>
CAPTURE-WCHAN distinct-pids=<n>
CAPTURE-WCHAN last s=<second> pid=<pid> state=<S|D|R> wchan=<symbol> cmd=<cmdline>   # per pid
CAPTURE-WCHAN tail <...>                                                            # last 8 raw
```

so each process's LAST observed state and the second it was last seen are both
visible: a process that waited for 300 seconds is distinguishable from one that
vanished in the first second, which the tail alone could not show.

Measured value of the first catch (before this aggregation): a BOOT-FAIL run with
`denied=0 created=0` had NO prefix guest process alive at the end -- only the
harness shell in `do_wait` -- so the failure is not a live hang at the endpoint;
whatever stalls it happens earlier and the prefix has already torn down by the
time the watchdog expires.

### `--capture-wchan`: per-run reset (added after the second catch)

Across a `--repeat` series the sampler used to accumulate every run's samples, so
the `distinct-pids` list on a failure mixed runs and could not say which process
belonged to which run. The buffer is now cleared at the start of each iteration,
which makes the next catch attributable to a single run.

Census from the two catches so far (before the reset, so run attribution is not
available): the non-harness entries are the prefix's own launcher and guest shells
-- `bin/darling --rootless shell /bin/bash -c ...` in `sigsuspend` (one process)
and in `do_poll.constprop.0` (its companion) -- plus one instance of that same
shell pair in `state=D wchan=jbd2_log_wait_commit`, i.e. blocked on the HOST
filesystem journal. The harness shells themselves are always `do_wait` or
`pipe_read`.

That D-state is a candidate, not a conclusion: with the mixed buffer it may have
belonged to a passing run, and host-I/O sensitivity would also explain why
instrumenting the guest changes the failure rate. The per-run reset exists so the
next failed boot can be attributed before that hypothesis is repeated.

### `--gap-seconds` and the host-I/O test it enabled

`dwdiag cycle --gap-seconds N` waits N seconds between runs. It exists because
runs back-to-back keep the host filesystem journal busy, and host-I/O stalls were
the leading candidate for the boot flap; a gap is the cheapest way to test that
without touching the guest. It also stops one run's teardown from overlapping the
next run's boot, which is friction in its own right.

RESULT OF THE TEST -- host-I/O spacing is NOT the factor:

* a 30-run series with `--gap-seconds 5` produced three failures (runs 13, 15,
  22), which is the same rate as the pooled back-to-back series (~1-3 per 30);
* none of the three attributed captures contained the `state=D
  wchan=jbd2_log_wait_commit` entry that suggested the hypothesis, so that entry
  is now known to have belonged to a passing run in the mixed-buffer era, exactly
  as the earlier note warned.

What the attributed captures DO show, in all three: the prefix's own
`bin/darling --rootless shell ...` pair sits in its normal steady state
(`sigsuspend` and `do_poll.constprop.0`) with the harness shells in `do_wait` /
`pipe_read`, and nothing is blocked on a host resource. Two of the three failures
had `denied=0`; one had `denied=1`. So the flap is the guest shell pair failing to
complete its work, not the host stalling underneath it.

## Freeze-on-fail is now reachable from the diagnostic tool, and the snapshot is taken by the catcher (2026-10-01)

Two pieces of friction were solved in the same session, both because a caught failure was being destroyed
before it could be read.

* `dwdiag cycle` gained `--freeze-on-fail`. The boot harness has implemented the freeze for a while, but the
  tool that builds the harness command did not forward it, so holding a failure for inspection meant
  abandoning the tool and retyping the harness invocation by hand -- which loses `MLDR_DIAG_LOG`, the
  single-argument `--cmd` shape, and the deployment identity the tool records into the run's own log. The
  cycle stops at the first frozen failure and prints `CYCLE-FROZEN` with the log to inspect, because the
  frozen run owns the prefix and a following run would collide with it. Default is off: acceptance never
  freezes, and a frozen prefix is left running on purpose, to be cleaned with the command the harness prints.

* `snap-on-freeze.sh` (diagnostic only) takes the server snapshot in the same process that catches the
  freeze. WHY: the harness wraps the launcher in `timeout 150`, so a frozen incarnation is destroyed roughly
  a minute after the freeze is announced. The first attempt read the interesting fact from a second tool call
  and lost that race -- by inspection time the server process was gone, and the question (was the semaphore
  timed wait's timerfd armed, with which deadline, and had it ticked) could not be re-asked without another
  run. The script reads `/proc/<server>/fdinfo/<timerfd>` twice, two seconds apart, then the debugger's
  identity queries, so the armed deadline and the tick count are captured while the server is still alive.

Measured on the frozen failure (current build, `stress_mixed 20`): a guest thread of `launchd` (pid 1) sits
with `active_call` 62 `semaphore_timedwait`, `waiting_for_reply=true`, `suspended=true`, while the stall dump
reports `timer=1 tactive=1` for it -- a timer IS armed for the wait -- and the server's main loop is in
`epoll_wait` (which uses a 500 ms timeout whenever the stall dump is enabled, so the loop is cycling). The
workload's own boot-side evidence in the same window is `shellspawn` stopped after its last instrumented
step, which is NOT evidence of anything: that step is followed by a blocking `accept()` by design, and the
instrumentation simply ends there. Likewise `release-drops-pending` is background noise: it fires 19-26
times in a passing `basic` run and 70-102 times in a passing `stress_mixed` run.

## Provenance witness and gate (2026-10-01, dar-4cp9)

The transport phase produced strong runtime evidence from an untracked scratch source tree whose revision
matched no branch, so a closed Bead rested on source nobody could reproduce and the fix could not be exported
as a patch. That is a process defect, and it is now machine-checkable instead of being something a later
reader has to guess.

`dwdiag source --provenance` (the existing source-inspection command, extended rather than duplicated) reports:

* the source root the build tree was configured against, taken from `CMAKE_HOME_DIRECTORY` in its cache;
* for every component (darlingserver, mldr, xnu, launchd, dyld): revision, branch, dirty tracked file count and
  untracked file count, or `no-git`/`missing` when the directory is not a repository;
* the patch-series identity: sha256 of the profile's `patches.yml`, of the workspace `west.lock.yml`, and of
  the profile-composition lock (searched one level under `locks/`, because the registry itself lives in a
  subdirectory);
* the hashes of the artifacts actually deployed into the prefix, because a source identity says nothing about
  the binary a run executed.

It then prints `PROVENANCE-CANONICAL` or `PROVENANCE-NON-CANONICAL` with the reasons. `--require-canonical`
exits 2 when not canonical; `dwdiag cycle --provenance --require-canonical` prints the same witness at the end
of a series and refuses the series the same way. A dirty tree is NOT an error: diagnostic runs from dirty
source are legitimate. What is refused is calling such a run the evidence for a product claim.

MEASURED USE, both arms: against the scratch build the witness reports
`PROVENANCE-NON-CANONICAL ... reasons=src/external/darlingserver:untracked=21,...` (correct: that tree is
untracked) while still naming the revision it was cut from, the patches.yml hash, the composition lock hash
and four deployed artifact hashes including the server's; and a one-run series that PASSED was refused with
`PROVENANCE-REFUSED` and exit 2, which is the deliberately tested negative contract.

### Crash records identify their own image (2026-10-01)

This session reversed guest/server crash attribution twice, and both times the correct answer came from asking
which deployed image contains the reporting symbol. `dwdiag crash` now answers that itself: with `--prefix` (or
`DWDIAG_PREFIX`) it hashes every candidate image and reports, per image, whether it carries
`dserver_crash_probe`, then prints how many images report the probe, the prefix's live server host pid, and
`host-tid=unavailable` when the record genuinely does not carry one.

Measured on the recorded failure: five candidate images hashed, `images-reporting-the-probe=1`, with
`bin/darlingserver ... reports-dserver-crash-probe=y` and every guest image `no` -- i.e. the record belongs to
the server, which is the fact that had to be established by hand before. No product change was needed for this,
which matters because the scratch product tree is frozen until canonicalization completes.

### An error, an exhausted retry, and a component name (2026-10-01)

Three friction points found the hard way while chasing a guest abort, all fixed in the tool.

**A runtime error must reach the caller, not only the transcript.** `dwdiag prefix` and `verdict` are long runs,
so their stdout and stderr are redirected into `/tmp/dwdiag-transcript-*.out`. The clap usage errors were already
routed around that redirection, but a command's own error was not: passing an unknown component name to
`--artifact` made the whole invocation fail with `TRANSCRIPT=...` and `ESSENTIALS 0 line(s)` on the terminal and
the reason inside the file, which reads as "there was nothing to do" and cost an install that silently did not
happen. `main` now prints such an error through the same `say()` path the essentials use and exits 3. Verified:
`--artifact nosuchcomponent=/bin/true` prints `ERROR: --artifact nosuchcomponent: unknown component; known:
darlingserver, mldr, libsystem_kernel, dyld, libsystem_pthread.dylib` on the caller's stream with rc=3.

**A component may be named by its library file name.** `--artifact` takes the component key
(`libsystem_kernel`, `dyld`, `mldr`, `darlingserver`, `libsystem_pthread.dylib`), but what a caller has in front
of them is the file they just built, and the lookup ran before any pair was installed, so
`--artifact libsystem_kernel.dylib=...` aborted the invocation and a correct `dyld=...` beside it did nothing as
well. Both spellings now resolve.

**An exhausted plane retry is nameable.** `__dserver_plane_request_ex` retried nine times and printed
`[plane-retry]` once per process, but the exit that actually decides the caller's fate -- the one that hands back
-1 and sends the caller to the datagram fallback the hard socket gate then denies -- printed nothing, so "the
plane refused once and then answered" and "the plane never answered at all" left the same log. It now prints a
bounded `[plane-exhausted] n= op= tid= attempts=9 status=` line. Measured use: in a batch where the failing runs
all carried `call=checkin image=loader`, this instrument stayed silent, which is itself the answer -- those
failures are the loader's datagram-path checkin being denied, not a plane refusal.

### The loader's checkin diagnostics need two variables (2026-10-01)

`[mldr-ctl]` lines -- `checkin-publish BEGIN`, `deferred-checkin ... status= ready=`, the skip notice -- are
gated on `MLDR_COURIER_DIAG` (`mldr_diag_on()` reads exactly that), while `MLDR_DIAG_LOG` only chooses the
sink and falls back to fd 2 when unset. Setting the sink alone therefore produces a log full of
`[mldr-dthread ...]` lines, which are a different instrument, and a reader concludes the checkin diagnostics
are broken. Measured cost: one batch run under `MLDR_DIAG_LOG` alone, which answered nothing. Use both:

```
dwdiag verdict --prefix P --mode basic --args 20 \
  --env MLDR_COURIER_DIAG=1 --env MLDR_DIAG_LOG=/tmp/mldr-$n.log
```

What they answered when set: in crashing and passing runs alike the deferred checkin goes through the plane
(`checkin-publish BEGIN` then `deferred-checkin status=0 ready=1`), so the deferred checkin is not the call
that dies, and a run that reports `denied=1` can still PASS -- the denial is a correlation with the crash, not
its cause, and any explanation of that failure class has to name the call site.

### `handoff/` deletes files it does not own (2026-10-01)

`west dw handoff` regenerates `handoff/*.bundle` and, in doing so, removes files in that directory it does not
recognize. Measured twice: preservation diffs committed under `handoff/` were gone from the working tree after
the next `west dw handoff`, unstaged and unannounced, which is exactly the loss the archives existed to
prevent. Keep anything durable outside that directory; the preservation copies of the xnu mach_msg delta now
live in `artifacts/xnu-ring-mach-msg/`, and one `west dw handoff` run confirms they survive it (2 files still
present, 0 diff files left in `handoff/`).

## `dwdiag processes` (added with the stale-process correction)

A prefix-process census matched by executable path AND command line, `--list` and `--json`, exit 1 when the prefix is
not clean. Added because `pgrep -c <name>` said a prefix was empty while it held three stale guest launchers: the
launcher's process name is not the pattern, so the count was zero for the same reason a broken instrument is silent.
Use it before any claim that a prefix is clean, and use `scripts/prefix-cleanup.sh` -- which prints its own
prefix-owned before/after counts -- to act on the result.


> **Where this copy lives.** The tool is vendored into the workspace
> (`darling-workspace/tools/darling-debug-runner`) so its source, this README and every instruction that calls it are in
> one tree, and it is reached through the stable entry point `darling-workspace/scripts/dwdiag`, which builds it on first
> use. It was developed in a sibling repository of the same name; that repository is now only a fallback for a checkout
> that predates the move, and its keeper bundle in `tool-handoff/` is stale.

# darling-debug-runner

Runs commands in an isolated process group and writes reproducible debug
bundles. It can detect stalls from log activity, capture `/proc` state and GDB
backtraces, signal matching processes, and run preparation/capture/cleanup
hooks.

Build:

```sh
cargo build --release
```

Capture a process:

```sh
target/release/darling-debug-runner capture \
  --pattern darlingserver \
  --gdb \
  --gdb-cwd ~/work/darling \
  --gdb-executable ~/work/darling-build/src/external/darlingserver/darlingserver
```

Add `--tree` to capture the matched process and all of its descendants. During
`run` or `darling`, combine `--capture-gdb --capture-tree` to do the same on a
timeout or detected stall. Add `--gdb-namespace` when the target runs in a
nested PID namespace; the runner will also attach GDB using the target's
namespace-local PID.

Run a command with a hard timeout:

```sh
target/release/darling-debug-runner run \
  --name example \
  --timeout-seconds 60 \
  -- sleep 120
```

The `darling` subcommand gently shuts down the selected prefix before running
and optionally installs a freshly built `darlingserver`. On timeout or a
detected stall it uses `darling shutdown` instead of signaling the process
group.

Existing stall logs are followed from their current end, so old events do not
affect a new run. Use `--terminate-command` with `run` when the target has its
own safe shutdown mechanism.

The runner reports a command that exits unsuccessfully as `RESULT=failed` and
returns a non-zero status without treating it as a stall.


## Diagnostics: `diag`

The same binary also carries the diagnostics this workspace needs constantly, so that they are ONE tool with one verdict
rule and one set of exit codes instead of a pile of scripts. Every subcommand prints human output by default and
**`--json`** on request, so it composes (a JSON result can be piped into another tool or an agent) instead of being
scraped.

Exit codes for every subcommand: **0** ok / PASS, **1** verdict or acceptance failure, **2** usage, **3** tool error.

```sh
# --- symbolize: a reported OFFSET -> `symbol + offset`, for the server binary or a guest dylib -------------
# Probes report offsets rather than pointers because an address does not survive ASLR, and `self=` in a crash line is
# the base that turns a runtime pc back into a file offset.
darling-workspace/scripts/dwdiag symbolize \
  --binary ~/work/ringmm-build/src/external/darlingserver/darlingserver \
  --base-symbol dserver_crash_probe --delta 0x19d54c
darling-workspace/scripts/dwdiag symbolize --binary /path/to/libsystem_kernel.dylib \
  --base-symbol mach_driver_get_fd --delta 0x27a09 --near 4
darling-workspace/scripts/dwdiag symbolize --binary /path/to/darlingserver --addr 0x1323cb --json

# --- crash: a `dserver-CRASH` line -> location + stack walk + the disassembly around the fault -------------
darling-workspace/scripts/dwdiag crash --binary ~/work/ringmm-build/src/external/darlingserver/darlingserver \
  --log $(ls -t /tmp/darling-boot-*.log | head -1)
darling-workspace/scripts/dwdiag crash --binary /path/to/darlingserver \
  --line '[dserver-CRASH sig=b addr=0x684,self=...,pc=...,w0=...]'

# --- progress: WHERE a finished run stopped, without grepping two logs --------------------------------
# Reads the run log and (if given) the guest loader log and prints the three facts that decide a stall:
# whether the workload spoke, the last `[mldr-ctl]` stage the loader reached, the last plane op the guest
# PUBLISHED, and the last plane op the server actually SERVICED. Published-vs-serviced IS the diagnosis.
darling-workspace/scripts/dwdiag progress --log /tmp/dwdiag-verdict-1234-basic.log \
    --guest-log /tmp/mldr-diag.log --mode basic
#   PROGRESS workload=absent last-guest=after-seed pid=917234 last-published-op=5 last-served-op=4 serviced=7 denied=0 created=0 first-denial=<none>
# `--json` is supported, and `verdict` composes the same summary itself: a non-PASS verdict prints a
# VERDICT-STAGE line (with `MLDR_DIAG_LOG` picked up from the environment), because a bare HANG that does not
# say where it stopped is the manual-grep work this subcommand removes.

# --- verdict: ONE guest workload, judged by its OWN machine-readable line --------------------------------
# The absence of `RING_MACH_TEST mode=<M> ... pass=1` is FAIL or HANG, NEVER PASS. That rule exists because a harness
# marker that matched a workload's START line reported two hung workloads as passes in one session.
darling-workspace/scripts/dwdiag verdict --prefix /tmp/dr-on-matched --mode sem_timed --args "300 1" --wait 60 \
  --env DARLING_DISABLE_THREAD_RPC_UDS=1
# It also names the first denial AND resolves its caller, which is the next dependency to migrate:
#   VERDICT mode=sem_block verdict=HANG denied=1 created=0 first-denial=<call> caller=<symbol + 0xNN>

# --- suite: a SET of workloads (one boot each) -> one table with the acceptance counters ------------------
darling-workspace/scripts/dwdiag suite --prefix /tmp/dr-on-matched --wait-base 60 \
  --env DARLING_DISABLE_THREAD_RPC_UDS=1 --require-zero-creations \
  -- 'sem_ready 2 :: sem_block 100 1 :: sem_gap 5000 1 :: basic 20'
#   SUITE rows=4 failures=N require_zero_creations=1
#   SUITE-VERDICT FAIL        (exit code 1)
```

`verdict` and `suite` **delegate** the prefix lifecycle to the workspace boot harness (`--boot-runner`, default
`scripts/darling-boot-run.sh` relative to the caller's directory) and read its log; `symbolize` and `crash` delegate the
symbol table to `llvm-nm` and the disassembly to `objdump`. Nothing is reimplemented here, because a second
implementation would be a second source of truth for something the toolchain already answers.

## `diag witness` — which instruments spoke, and which stayed silent

```
scripts/dwdiag witness [--log PATH] [--json]
```

Reads a run log (default: the newest `dwdiag-verdict-*.log`) and reports a census of every instrument this project
ships -- `SEM-SITE`, the workload's `ITER` marks, the server's `stall-dump`, the in-memory ring dump, `plane-refuse`,
`dtape.msgq`, `dtape.wait_timer`, `rpc.*.begin`/`.reply`, `dserver-CRASH`, the workload's own watchdog and the
post-exec barrier -- with a hit count and one sample line each, followed by `WITNESS-SILENT <names>` for the ones that
never spoke.

Why it exists: a hand-written `grep` finds the line it was written for and cannot tell you which OTHER instrument never
ran, and "an instrument that silently does nothing is indistinguishable from no guard". Both failure modes were hit for
real: a `SEM-SITE` line was present while the pattern missed it, and two `dserver-CRASH` lines sat unnoticed in a log
that had been read twice as a transport stall. Adding an instrument means adding it to the `INSTRUMENTS` table in
`src/diag.rs`, or the census stops being a census. The table is checked by `cargo test`: every entry must match a
sample line, and every entry must have one, because a pattern that has drifted from its instrument's format reports a
live instrument as silent -- which happened twice in one session (`SEM-SITE` gained a `tcb=` field; the ring dump was
rewritten from `trace-record` to `dtape.ering seq=`).

## The verdict reports the workload's own exit status

A missing result line is not a hang. The guest command is wrapped so the guest shell prints `__DWDIAG_RC=$?`, and
`verdict` classifies accordingly: `CRASH <SIG>` (with `rc=128+N`, e.g. `CRASH SEGV rc=139`), `EXIT rc=N`, `HANG`
(no status at all), or `NO-RUN` (never started). `--json` carries `rc` and `signal`.

Measured need: a `SIGSEGV` at a workload's second iteration and a deadlock were indistinguishable before this, and
the absence of the line was read as a lock problem for far longer than it should have been.

## `--repeat N`: a single-run verdict cannot see flake

Every run prints `LOG=<path>` (stable and machine-readable, so callers stop globbing a temp directory) and, with
`--repeat N`, one `VERDICT[i/N]` line per run plus a summary:

```
STABILITY mode=basic runs=4 pass=0 stable=no distribution=CRASH SEGV=4
```

Exit status is non-zero unless **every** run passed. Measured need: one-run verdicts flipped between PASS and
`CRASH SEGV` for the same command all session, and the flips were read as changes in behaviour.

This is required, not optional, for any claim about a failing shape: `sem_ready 2` at `--repeat 4` is `PASS=4
stable=yes`, while `basic 1` is `CRASH SEGV=4 stable=no`.

`scripts/dwdiag` rebuilds the tool when its source is newer than the binary and prints which copy answered
(`dwdiag: tool=... binary=...`). Before that it only built a *missing* binary, so a source change was silently
ignored and `--help` described a different tool than the one that ran.


## Proactive decoding: what was fixed and why (2026-09-28)

A diagnostic that requires a second, hand-written step is a diagnostic that will be skipped when it matters. Two gaps
were found by using the tool on a real failure, and both were closed in `diag.rs`.

1. `crash` withheld information it already had. On a `dserver-CRASH sig=6` whose pc was outside the given binary it
   printed `LOCATION: <outside this binary's symbol range>` and an empty disassembly, while the run log's own panic
   backtrace named the whole call chain. It now prints, for every crash:
   - the **signal's meaning** (`6` -> `SIGABRT: abort() -- in C++ almost always an uncaught exception reaching
     std::terminate, or an explicit abort()`; `11` -> null/freed dereference; and so on), because `sig=6` versus
     `sig=11` changes the diagnosis completely;
   - the `addr` field's two 32-bit halves when it has that shape, explicitly labelled `HYPOTHESIS, unverified`, since
     a tgkill-raised signal carries a pid/uid pair there and guessing it as fact would be worse than silence;
   - **every `darlingserver(+0x...)` frame from the log, resolved to `symbol + offset`** against the given binary.
   That last one is what turned an opaque abort into a located defect: it produced
   `panic <- Assert <- retrieve_thread_self_fast <- thread_self_trap_for <- dtape_thread_self_trap_for <-
   Server::_serviceProcessControl`, which is the evidence that a trap cannot be answered inside the plane's pass.

2. `progress` said `workload=absent` for a crashed run, which is true and useless. It now prints
   `STOP-REASON crash: <line>` with the first panic-backtrace frames and the exact `dwdiag crash --binary ... --log ...`
   command that decodes it, and reports the same in `--json`.

Both changes are verified by use: `crash` resolved the frames above against the deployed `darlingserver`, and `progress`
reported the crash for the same log. The rule they encode: when a reading is empty, say **why** it is empty, and never
withhold a fact that is already in hand.


## Third round of proactive decoding (2026-09-28): the wake channel, and a classifier that had to be caught lying

The question violation A turns on is not "did something wake the server" but "WHICH channel woke it", and two
instruments were answering it wrongly.

1. The loader's `[plane-wake]` line said `via=doorbell-or-server-poll`, a label that cannot be told apart -- and the
   loader already knew the answer (it rings the doorbell only when it holds the descriptor). It now prints
   `via=doorbell` or `via=none` plus `db=<fd>`.
2. The same line ended in a LITERAL backslash-n instead of a newline, so every `[plane-wake]` record collapsed into one
   log line. Any line-based count of them undercounts, which is exactly how a reader concludes the wake path is quiet.
   Both the instrument and the tool now count OCCURRENCES, not lines.
3. `dwdiag progress` prints `WAKES plane-publishes=.. doorbell=.. none=.. unknown=..` with a verdict:
   `the bounded poll is LOAD-BEARING` when a publish had no doorbell to ring, `the poll is not what makes progress`
   when every publish rang it, and `record(s) predate the channel-aware instrument; re-run to judge` otherwise.

That third verdict is the one that had to be earned: the first version of the classifier used a SUBSTRING test, and the
retired label `via=doorbell-or-server-poll` contains `via=doorbell`, so it reported `doorbell=38 none=0` for a run whose
channel was never recorded -- wrong in the most dangerous direction, because it would have "proved" that the poll was
unnecessary. It now extracts the token after `via=` and compares it, and the same log honestly reports `unknown=38`.

Rule encoded: an instrument must not make two different facts look the same, and a classifier over instrument output
must match the value, never a prefix of it -- verification by use caught the second one immediately.


## Fourth round (2026-09-28): a regression tripwire, because I broke the boot twice by memory

Two broken boots in a row produced the same reading -- `plane-publishes=6` where a healthy boot shows 40+ -- and
noticing that required *remembering* the healthy number. A tool that needs the reader to remember a baseline is not
proactive, so `progress` now takes `--min-plane-publishes N` and prints
`WAKES-REGRESSION only N plane publishes, at least M expected: a boot that stops early looks exactly like this`.
It is explicitly a tripwire, not a proof: a floor that a known-good boot clears comfortably.

The root cause of the second break was a bundle consumed by the wrong consumer (a `PROCESS_DOORBELL` envelope adopted
instead of stored, while the lane attach still waited for it by token), and the log already had the vocabulary for it.
`progress` now surfaces `COURIER-MISSES` from the receive-side records, and -- deliberately -- prints
`none visible in this log (the receive-side instrument is env-gated; absence here is not evidence that no bundle was
lost)` when it finds nothing, because an empty reading that does not say why it is empty is the failure mode this whole
tool exists to prevent.


## Fifth round (2026-09-28): the server's own log is an input, because its absence was misread as its silence

Most of this session's wrong turns came from one invisible fact: **the server's stderr never appears in the run log**.
It goes to `<prefix>/private/var/log/dserver.log`. From a run log that simply lacks server lines I twice concluded
"the server does not send the doorbell" and "an exported variable does not reach the server" -- the first was false
(52 `plane-doorbell-sent` records in the prefix log) and the second is unproven, because the evidence I was reading
could not have contained it either way.

`progress` therefore takes `--server-log` (default the live prefix's `private/var/log/dserver.log`) and prints two
joined facts:

    SERVER-SENT plane-doorbell-sent=52 (from <path>)
    SERVER-GUEST-SPLIT the server sent the doorbell 52 time(s) while 11 guest drain window(s) expired empty:
      the descriptor is being SENT but not RECEIVED (look at the guest's receive path, not at the sender)

Counts only, never an interleaved ordering: the two files have different clocks and a merged sequence would be
fabricated. When the file is missing the report says so, so "no server facts" can never again be read as "no server
activity". Rule encoded: an absence of evidence in one source is not evidence of absence -- name the source.


## Sixth round (2026-09-28): no fabricated zeros in the transport census

The table printed `SPSC Ring 0` and `duplex Ring/mailbox 0` whenever their traces were simply DISABLED, so a reader
could not tell "no traffic" from "no instrument". Directive section 12 forbids exactly that. Rows whose instrument is
opt-in (the Ring traces, enabled by `DARLING_GUEST_RING_TRACE=1`) now report

    SPSC Ring (per-thread lane, ordinary calls)      UNMEASURED  RING_TRACE gen ENTER (only under ...) -- trace not enabled in this run

The rule this encodes, and it is the same one the rest of the tool already follows: a zero must come from a live
instrument, otherwise the row admits it was not measured.


## A guest crash, symbolized with one command and no arguments

```
scripts/dwdiag crash
```

Reads the NEWEST verdict log that actually carries a symbolizable fault, attributes the faulting instruction
pointer to an image using the `/proc/self/maps` dump the fatal-signal handler writes at the moment of death,
and resolves it to `symbol + offset`. No `--binary`, no offset arithmetic, no symbol table: the tool does
what used to be done by hand.

    CRASH-LOG /tmp/dwdiag-verdict-...-sem_ready.log (newest log with a symbolized fault)
    GUEST-FAULT sig=11 addr=0x6a rip=0x7033c5425d0a
    GUEST-IMAGE /tmp/.../usr/lib/system/libsystem_c.dylib base=0x7033c53a6000
    GUEST-SYMBOL ... (file offset 0x80d0a)

`--json` prints the same as one machine-readable line. A loader-side `dserver-CRASH` line still needs
`--binary <image>`, because that path derives its offset from the probe's own `self=` anchor.


### The caller chain

The same command also reports the candidate return addresses the fatal handler dumps (24 words from `rsp` plus the
`rbp` frame chain), each attributed to an image and resolved to a symbol:

    GUEST-CALLER w3=0x7bee805026e1 __vfprintf+0xe51 (libsystem_c.dylib file offset 0x8f6e1)
    GUEST-CALLER w11=0x7bee804f6d03 freopen+0x163 (libsystem_c.dylib file offset 0x83d03)

Attribution of every word is reported, including anonymous mappings and words with no symbol, because a filter that
discards the answer is worse than a noisy list: two such filters were found here by running the command (one required a
mapping path; another had lost its leading negation, so it kept only numeric local labels). Data words on the stack
appear as plausible symbols; the file offset is always printed so a wrong pick is visible.

## Installing the workload fixture: `--asset`

A runtime component is installed with `--artifact NAME=BUILT`, which expands to every copy the prefix layout needs.
The workload fixture is **not** a runtime component: it is a test asset, and it is installed with

```
--asset <SOURCE>=<DESTINATION>          # DESTINATION relative to the prefix
```

Two measured traps this option exists to close:

* The guest's `/usr/bin` resolves through `/Volumes/SystemRoot` to the **host's** `/usr/bin`, so copying the fixture
  into `<prefix>/usr/bin` does **not** put it where `--guest-command /usr/bin/...` looks. Use a guest path the prefix
  itself owns, such as `/private/var/tmp/...`, and install the asset at the matching prefix-relative destination.
* A fresh prefix has no `usr/bin`; the copy's original error named neither the missing directory nor the destination.
  The tool creates the destination's parent and names it in the error.

Every gate also prints `ASSET-CHECK guest=... host=... host-present=... asset-installs=...` before the workload runs,
so a fixture that cannot be seen is visible at the top of the run rather than as `workload=absent` at the end.

## Watchdogs: `--wait-base` must cover the workload's own duration

`verdict --wait` and `suite --wait-base` are the watchdog, not a poll interval: each row's harness sleeps for that
long and the run is killed with SIGTERM when it expires. MEASURED: the `basic` mode needs about 200 s of work, the
suite was run with `--wait-base 90`, its first attempt died at the boundary with no result line, and the row was
printed as a plain failure -- only the automatic retry (which prints `ROW-RETRY mode=... first=... retry=...`) let
the suite pass. A signal death with no result line is now printed as `WATCHDOG?` with the wait that was used and the
hint to raise `--wait-base`, because "the workload failed" and "we stopped measuring too early" are different claims
and only one of them is about the product. For the ring/mach workload a base of 400 s is what the acceptance run
uses.

### `diag build` -- build the pair the rule requires, and check the instruments are IN it

```
DWDIAG_BUILD=/path/to/build dwdiag build --expect '[sigexc-in '
BUILD targets=libsystem_kernel.dylib,dyld rc=0 errors=0
  artifact .../libsystem_kernel.dylib stamp=12:33:08 bytes=2777588
  artifact .../dyld stamp=12:36:19 bytes=5755632
```

`libsystem_kernel` and the dyld image are built **together**, because the same loader code is compiled twice and
building one leaves the other stale. `--expect STRING` is checked against the BUILT file: a probe whose string is
absent from the artifact cannot fire, and a silent probe that is really an absent probe is the most expensive
measurement mistake this project has made.

### `diag cycle` -- build, deploy, run, and say which instruments fired

```
DWDIAG_BUILD=/path/to/build DWDIAG_PREFIX=/tmp/prefix \
  dwdiag cycle --mode basic --args 20 --repeat 3 --probe sigexc-in,native-exit
CYCLE[1/3] mode=basic verdict=CRASH SEGV (Segmentation fault) denied=0 created=0 rc=139
  instruments fired=[native-exit=2 sigexc-in=1] silent=[...] log=/tmp/dwdiag-verdict-...log
```

The artifact pair (`libsystem_kernel`, `dyld`) is the default and is sha256-checked at EVERY deployed copy; the build
tree and the prefix come from `DWDIAG_BUILD` and `DWDIAG_PREFIX` so they are never retyped; the verdict is the
workload's own machine-readable line; and each run reports which registered instruments fired and which stayed
**silent**, which is what turns "the probe said nothing" into a fact about the instrument.

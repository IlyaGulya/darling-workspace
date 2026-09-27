
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
`src/diag.rs`, or the census stops being a census.

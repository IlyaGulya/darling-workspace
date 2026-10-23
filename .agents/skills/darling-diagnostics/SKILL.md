---
name: darling-diagnostics
description: >-
  Measure Darling guest/runtime behavior, verify deployments and interpret run
  evidence. Read before tracing, instrumentation, artifact comparison, loader
  closure analysis or fault capture. Use supported tools instead of ad-hoc scripts;
  do not mistake diagnostic output, silent probes or stale artifacts for verdicts.
---

# Darling diagnostics

Canonical source: `.agents/skills/darling-diagnostics/SKILL.md` in the manifest;
`.claude/skills` is its compatibility alias. Read the relevant sections of
[tooling](../../../docs/tooling.md) and current command help before acting.
The [generated command reference](../../../docs/tooling.md#the-diagnostics-are-one-tool-dwdiag)
is the complete roster; do not maintain another command list in instructions.
Historical reasoning belongs in diagnostic evidence/Beads and
[architecture notes](../../../docs/direct-transport-descriptor-architecture.md),
not new incident narratives in this skill.

## 1. Select an existing measurement

| Question | Supported route |
|---|---|
| Where did this run stop, and what evidence belongs to it? | `scripts/dwdiag job`, `progress`, `evidence`, `witness` |
| Which build, prefix and deployed copies served it? | `dwdiag env`, component deploy verification, artifact manifest and guest tracing |
| What faulted, or what is blocked? | `dwdiag core`, `trap`, `unwind`, `waits`; choose from the generated reference |
| One build/deploy/run iteration or a workload set? | `dwdiag build` + `cycle`; `verdict` / `suite` for workload-owned acceptance |
| A loader image/symbol gap? | `closure_analyze.py --sweep` / `--cover` / `--order`; `rung-driver.py stop` / `verify` |
| Live server belief about one thread? | `darling-debug` or LLDB glue; [debugger guide](../../../docs/darling-debugger.md) |

Read `darling-boot` before any prefix-backed command; read `darling-testing`
when designing a regression gate. Never reimplement a core parser, symbolizer,
namespace attach loop or instrument census beside tools that already supply it.
Use `--json` for composition, not scraped presentation text.

For an iteration, resolve `DWDIAG_BUILD` and `DWDIAG_PREFIX` from the owned
configuration. `dwdiag build --expect STRING` builds `libsystem_kernel.dylib`
AND dyld and confirms probe presence; `cycle --mode M --args A --repeat N
--probe tag1,tag2` composes deployment/run/census. See
[iteration contract](../../../docs/tooling.md#dwdiag-build-and-dwdiag-cycle----the-iteration-loop-with-the-paths-out-of-the-command).

## 2. Establish artifact and process identity

- For a libsystem_kernel change, build/deploy dyld together with the dylib: dyld
  contains a static emulation path. Never claim a closure-only deploy exercised it.
- Deploy all runtime copies through `mise run west darling-build --deploy-only
  --deploy-component NAME` or `scripts/darling-deploy-verify.sh`. Components
  accumulate; verify every copy's SHA-256. Use artifact-manifest/describe tools
  for baseline content, LC_MAIN, symbols and deployed literal probe tags.
- Focused server build: `mise run west darling-build --targets darlingserver
  --deploy --deploy-darlingserver` also deploys dyld/closure. Record that scope;
  use a verified server-only deployment path when isolation is required.
- An already-running server still serves its old bytes. Shut down via its owner
  or prove running identity before interpreting a deployment. A different
  prefix's live runtime can serve a plausible-looking run; assert prefix maps
  and use guest tracing when necessary.
- Read the run's own `[dwdiag-env ...]` / `RUN-ENV` digests and prefix. A log
  without this identity cannot establish which build ran. Compare identities
  before interpreting differences between runs.
- Prefer the fixture's raw-Linux `host_pid=` declaration for host identity;
  emulated Darwin pid and heuristic shell/loader matches are not substitutes.
  The managed ring fixture is `/private/var/tmp/ring_mach_msg_test`, not host
  `/usr/bin`; confirm the installed asset is the one being invoked.

## 3. Instrument without changing the measured behavior

- Follow [probe rules](../../../docs/tooling.md#rules-each-measured): one literal
  tag, one raw Linux write syscall (`1`, not Darwin `4`), preserve ABI registers
  other than architectural syscall clobbers. Verify the tag in BUILT and every
  DEPLOYED copy before treating silence as code-path evidence.
- No extra emulated syscall for probe identity; build every target architecture.
  Use stable per-thread identity shared across components, not a per-frame
  buffer address for cross-call correlation. Preserve the original function
  and surrounding symbols when editing a probe site.
- Prefer an out-of-band ring or startup-gated, default-off instrumentation over
  printing in a hot path. In-process instrumentation belongs under the shared
  `DARLING_DIAG` switch; use the [registry](../../../docs/tooling.md#in-process-diagnostics-one-switch-darling_diag).
  Pre-libc paths cannot call getenv or depend on constructors/runtime initialization.
- Read traced memory safely: use xtrace's guarded raw `process_vm_readv` reader,
  report unmapped/short reads, clamp descriptors to declared `msgh_size`, and
  report truncation. A length bound does not make a stale pointer safe.
  The 32-bit image must not read out-of-line payloads without that guard.
- Preserve declared record lengths and report any cap/truncation explicitly;
  never silently cut the abort/terminate message or fabricate absent bytes.

## 4. Read the run by owned streams

1. Read the announced BUNDLE manifest: every item is PRESENT or MISSING with a
   reason. Preserve logs, captures with paths, server pre/post delta, launchd
   service lookups, descriptor/environment capture, cores, counters and artifact
   identities. `INCOMPLETE` is not a complete archive. The answer can be in a
   different process's log, especially launchd or guest fd 1/2 captures.
2. Separate workload output from instrument streams. Prefer an owned result file
   under the prefix when transport descriptors are shared. Do not grep away
   mixed lines; if filtering verified whole-line noise, report removed records.
3. Use `dwdiag evidence` then `witness` to establish scope and census all registered
   instruments, including silent ones. An unavailable/partial log is not a
   measured zero; a grep for one tag cannot prove other instruments ran.
4. Log growth/flatness, phase and last-write age do not show whether YOUR process
   is alive or blocked. Ask `dwdiag processes`, `waits` or the workload's exit;
   `loops` diagnoses repetition, not successful execution.
5. `cycle --fresh` (implied by `--probe`) requires FRESHNESS evidence from that
   run's own workload lines. It is diagnostic execution policy, not a guest
   environment override or a cache switch: cycle does not invoke west test's
   verdict cache. Missing execution evidence must fail.
6. PASS requires the workload's own completed status/`pass=1` oracle and cleanup,
   never an intermediate diagnostic line. A missing completion is FAIL/HANG.
   Use unique log paths; retain warnings and acceptance counters.

## 5. Capture faults through the supported route

Guest instructions execute natively. A fault may bypass syscall instrumentation;
a silent probe/debugger or masked signal is not proof of no fault. Inspect core
availability with `dwdiag cores` (both core_pattern and process size limit matter),
then use `core`/`mappings` or the kernel's report through `trap`. Nearest-lower
symbol is a hint; preserve the actual offset and mapping identity.

For live capture use `dwdiag run --capture-gdb --capture-tree --gdb-namespace
--capture-pattern HANDLE ... -- LAUNCHER`, following the
[capture recipe](../../../docs/tooling.md#dwdiag-unwind----guest-frames-which-host-gdb-cannot-walk).
The observable handle is the loader, not a guest argv guessed from host cmdline.
For stop-at-start, select only the target argv substring; do not stop its shell.
Use `unwind --catch SIGNAL` with SIGSTOP passed through; competing sigaction
hooks and `MLDR_HOLD_ON_SIGNAL` are not reliable capture substitutes.

Finish with command rc, workload verdict, artifact identity, evidence paths and
prefix cleanup. Suspect the instrument's premises when measurements contradict
expectation; tooling fixes require behavioral coverage, not just one good run.

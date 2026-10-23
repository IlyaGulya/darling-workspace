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
[tooling](../../../docs/tooling.md), then verify current help, implementation and
returned evidence. A listed option or zero exit status is not proof of capture,
deployment or coverage. Linked references include historical recipes; resolve
paths and source authority through `darling-boot`, not copied examples.
The [generated command reference](../../../docs/tooling.md#the-diagnostics-are-one-tool-dwdiag)
indexes commands; inspect the selected command's actual contract before use.
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
| Live server belief about one thread? | `darling-debug` or LLDB glue; requires a compatible server launched with `DSERVER_DEBUG_ABI=1`; [debugger guide](../../../docs/darling-debugger.md) |
| Why was a syscall, RPC or Mach call refused? | `scripts/dwdiag denials --log GUEST_CAPTURE --kernel MATCHING_IMAGE`; caller resolution needs that image; `UNRESOLVED` is not absence of a denial |
| Which transport carried a message, and did the descriptor courier lose it? | `scripts/dwdiag courier` (packets per transport, endpoint purity) |
| Is the prefix or its process set stale, and what owns it? | `scripts/dwdiag processes`, `prefix`; `west dev status --json` for jobs/worktrees |

Read `darling-boot` before any prefix-backed command; read `darling-testing`
when designing a regression gate. Never reimplement a core parser, symbolizer,
namespace attach loop or instrument census beside tools that already supply it.
Use `--json` where supported. Pass this run's paths explicitly; default/latest
log discovery can select another run. The `dwdiag` shim can rebuild the host
runner even for `--help`; retain its printed tool/binary provenance.

For an iteration, resolve `DWDIAG_BUILD` and `DWDIAG_PREFIX` from the owned
configuration. `dwdiag build --expect STRING` defaults to the kernel/dyld build
targets and checks each requested tag in both configured artifacts. `cycle`
builds those targets, deploys kernel, dyld **and mldr** by default, refreshes the
ring fixture and runs its workload/census. Ensure every edited deployed
component was built; the default targets do not explicitly rebuild mldr.
These diagnostics consume a configured build/prefix; they do not establish the
manifest-source bootstrap contract. See the
[iteration contract](../../../docs/tooling.md#dwdiag-build-and-dwdiag-cycle----the-iteration-loop-with-the-paths-out-of-the-command).

## 2. Establish artifact and process identity

- For a libsystem_kernel change, build/deploy dyld together with the dylib: dyld
  contains a static emulation path. Never claim a closure-only deploy exercised it.
- Resolve explicit `--build-dir` and `--prefix` for `mise run west darling-build`;
  its defaults are not canonical-workspace discovery. `--deploy-only
  --deploy-component NAME` and `scripts/darling-deploy-verify.sh --build BUILD
  --prefix PREFIX --component NAME` copy/hash existing component destinations.
  They do not build, establish source provenance, lock/shut down the runtime or
  provide rollback. Stop it through its owner first. Components accumulate;
  inspect every expected destination: a missing parent is skipped, not repaired,
  and an all-skipped component can still return zero.
- A normal `--deploy --deploy-darlingserver` build also deploys dyld/closure.
  Record that scope; use explicit component-only deployment for a server-only
  experiment only after verifying its build and lifecycle prerequisites.
- Artifact-manifest covers its fixed boot-component inventory, not every dylib.
  Its `--probe` is a report, not a missing-tag failure gate; inspect presence
  yourself. Describe-artifact requires `FILE` even in tag mode; its optional
  symbol disassembly can be unbounded. Use `dwdiag symbolize` for symbol lookup.
- An already-running server still serves its old bytes. Shut down via its owner
  or prove running identity before interpreting a deployment. A different
  prefix's live runtime can serve a plausible-looking run; assert prefix maps
  and use guest tracing when necessary.
- Prefer the run's own `[dwdiag-env ...]` / `RUN-ENV` digests and prefix. They
  fingerprint a configured runtime set, not every loaded image; without them
  establish equivalent artifact/process identity explicitly or mark it unknown.
  Compare identities before interpreting differences between runs.
- `darling-trace-guest.sh` launches its own guest through `/bin/sh`; it is not an
  attach-only observer. Require that guest shell and an explicit durable `--log`,
  and never run it beside another owner of the prefix. A missing sampled mapping
  is not proof that a short-lived image never loaded.
- Prefer the fixture's raw-Linux `host_pid=` declaration for host identity;
  emulated Darwin pid and heuristic shell/loader matches are not substitutes.
  The managed ring fixture is `/private/var/tmp/ring_mach_msg_test`, not host
  `/usr/bin`; confirm the installed asset is the one being invoked.

## 3. Instrument with bounded perturbation

- Follow [probe rules](../../../docs/tooling.md#rules-each-measured): one literal
  tag, one raw Linux write, preserve ABI state. On x86_64, `syscall` write is `1`,
  not Darwin `4`; `scripts/guest-probe.h` is x86_64-only, not a portable i386
  implementation. Check every target ABI and deployed copy. Probe presence is
  necessary, not sufficient: gating, output routing and finite budgets can also
  explain silence. Instrumentation can change timing; do not claim zero effect.
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

1. Inspect the owning runner's actual evidence format. `darling-boot-run.sh`
   publishes a `MANIFEST` with PRESENT/MISSING/COUNT rows and an `INCOMPLETE`
   marker; top-level `dwdiag run` and West runtime units have different layouts.
   A printed BUNDLE path alone guarantees neither that manifest nor complete
   capture. Preserve command, logs, identities, missing-item reasons and required
   bytes, including symlink targets. The answer may be in launchd or guest fd
   captures rather than the harness log.
2. Separate workload output from instrument streams. Prefer an owned result file
   under the prefix when transport descriptors are shared. Do not grep away
   mixed lines; if filtering verified whole-line noise, report removed records.
3. For a West job, use `dwdiag evidence --state-dir JOB`; census the selected
   log with `dwdiag witness --log LOG`. They inventory available evidence and
   registered instruments, not complete runtime coverage. Missing/partial logs,
   disabled probes and exhausted record budgets are not measured zeros.
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
symbol is a hint. `symbolize --addr` expects an image virtual address, not an
absolute runtime PC or raw file offset. Resolve the mapping, Mach-O segment and
universal slice base; `maps` offsets include the fat slice's file offset.

For live capture select an observed host handle with `--capture-exe mldr` and
`--capture-pattern TARGET_SUBSTRING`, and provide `run --name NAME --bundle-root
OWNED_BUNDLES`. Check `gdb`, noninteractive privilege/ptrace and namespace access;
inspect the debugger output and captured identity, not just the runner rc.
`--gdb-ex 'handle ...'` alone does not attach before a fault: ordinary snapshots
can happen after the target exits. The current `--capture-on-fatal
--gdb-namespace` helper uses the namespace PID as nsenter's host target; do not
rely on that combination when host and namespace PIDs differ.

`unwind --pid HOST_PID` needs a live attachable target; `--catch SIGNAL` resumes
and waits for that signal. For stop-at-start select only the target argv substring,
not its shell, and pass SIGSTOP through. Read `frames_via`: `scan` is a set of
candidate addresses, not a backtrace. No successful guest unwind may be inferred
from perf collecting a user stack. See the
[capture reference](../../../docs/tooling.md#dwdiag-unwind----guest-frames-which-host-gdb-cannot-walk).

Finish with command rc, workload verdict, artifact identity, evidence paths and
prefix cleanup. Suspect the instrument's premises when measurements contradict
expectation; tooling fixes require behavioral coverage, not just one good run.

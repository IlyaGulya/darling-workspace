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
deployment or coverage. Resolve installation and guest-state paths through
`darling-boot`; historical evidence is not an operating recipe.
The [generated command reference](../../../docs/tooling.md#the-diagnostics-are-one-tool-dwdiag)
indexes commands; inspect the selected command's actual contract before use.
Historical reasoning belongs in diagnostic evidence/Beads and
[architecture notes](../../../docs/direct-transport-descriptor-architecture.md),
not new incident narratives in this skill.

## 1. Select an existing measurement

| Question | Supported route |
|---|---|
| Where did this run stop, and what evidence belongs to it? | `scripts/dwdiag job`, `progress`, `evidence`, `witness` |
| Which build, installed image and guest prefix served it? | `dwdiag env`, install receipt, artifact manifest and guest tracing |
| What faulted, or what is blocked? | `dwdiag core`, `trap`, `unwind`, `waits`; choose from the generated reference |
| One build/install/run iteration or a workload set? | `dwdiag build` + `cycle`; `verdict` / `suite` for workload-owned acceptance |
| A loader image/symbol gap? | `closure_analyze.py --sweep` / `--cover` / `--order`; `rung-driver.py stop` / `verify` |
| Live server belief about one thread? | The C++ ABI exists, but no shipped Rust client/LLDB glue. See [availability and current capture routes](../../../docs/darling-debugger.md); `dwdiag processes` / `unwind` / `run` provide host evidence, not server-state snapshots. |
| Why was a syscall, RPC or Mach call refused? | `scripts/dwdiag denials --log GUEST_CAPTURE --kernel MATCHING_IMAGE`; caller resolution needs that image; `UNRESOLVED` is not absence of a denial |
| Which transport carried a message, and did the descriptor courier lose it? | `scripts/dwdiag courier` (packets per transport, endpoint purity) |
| Is the prefix or its process set stale, and what owns it? | `scripts/dwdiag processes`, `prefix`; `west dev status --json` for jobs/worktrees |

Read `darling-boot` before any prefix-backed command; read `darling-testing`
when designing a regression gate. Never reimplement a core parser, symbolizer,
namespace attach loop or instrument census beside tools that already supply it.
Use `--json` where supported. Pass this run's paths explicitly; default/latest
log discovery can select another run. The `dwdiag` shim can rebuild the host
runner even for `--help`; retain its printed tool/binary provenance.

For an iteration, resolve `DWDIAG_BUILD`, `DWDIAG_PREFIX` (guest state only)
and the runner's global `--runtime-root R` explicitly. `dwdiag deploy --build B`
invokes the complete normal `west darling-build --install --with-test-data`
publication; it accepts no guest `--prefix` or component `--artifact` selection.
`cycle --target` adds build-only targets before that complete install.
`cycle --skip-build` reuses an existing verified image without building or
installing and rejects `--target`; `--repeat` runs workloads against the same
image rather than redeploying. New code needs a fresh configured absolute
install root, a stock ALL build and full install, never an in-place rewrite.
Keep diagnostics default-off. See the
[iteration contract](../../../docs/tooling.md#dwdiag-build-and-dwdiag-cycle----the-iteration-loop-with-the-paths-out-of-the-command).

## 2. Establish artifact and process identity

- For a libsystem_kernel change, build dyld together with the dylib: dyld
  contains a static emulation path. Consume both from the same complete image.
- Use `mise run west darling-build --build-dir B --runtime-root R --install`
  with absolute `CMAKE_INSTALL_PREFIX=R` and default `COMPONENTS=stock`.
  `R/bin/darling`, `R/bin/darlingserver` and `R/libexec/darling` are installed
  image paths; disjoint `P` is writable guest state, never a runtime provider.
  No partial deployment, `P/bin` discovery or duplicate system libraries in `P`.
- The CLI `installation_identity` summarizes receipt/path/SHA, artifact count,
  source revision/dirty state and provenance, not the full inventory.
  The schema2 receipt inventories the complete actual image, including normal
  install-hook files and actual CMake manifest hashes; verification rejects
  extras. A published image remains verifiable when its build tree changes.
- Artifact-manifest covers its fixed boot-component inventory, not every dylib.
  Its `--probe` is a report, not a missing-tag failure gate; inspect presence
  yourself. Describe-artifact requires `FILE` even in tag mode; its optional
  symbol disassembly can be unbounded. Use `dwdiag symbolize` for symbol lookup.
  Pass `--runtime-root R` to image-aware tools and `--prefix P` for guest state.
  `rung-driver.py --fail-on-installed-miss` gates installed closure misses.
  `suite --record-evidence PATH` writes standalone evidence, never mutates the
  immutable installation receipt.
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
- Generic `dwdiag run` wraps a host command; it neither selects a guest prefix
  nor owns guest shutdown. The inner managed command must own the lifecycle,
  or use the prefix-aware Darling mode. Prefix staging consumes only installed
  `R/libexec/darling/usr/bin/<fixture>` or an explicit `--asset`, digest-checks
  the staged copy under `P/private/var/tmp`, and fails on missing input. It is
  not a runtime-library deployment or a fallback to an old prefix copy.

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
3. For a finished West job, start with `dwdiag job --state-dir JOB`; use
   `progress --state-dir JOB` and `evidence --state-dir JOB` for focused views.
   They use persisted runtime/prefix selection and finite registered captures,
   not the observer's environment or mutable prefix history. Producers register
   known log paths before writing via `west-job.sh register`; discovery alone
   supplies no source boundary. Missing/partial captures and disabled probes
   are unavailable, not measured zeros; ambient cores/kernel history are not
   attributable replacements. Current process observations are a separate instant.
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

Completed bundles carry a finite `server.log` slice, not full pre/post history.
Finalized payloads may be `.zst`; `dwdiag` resolves logical raw paths automatically.
For other readers use `python3 -B scripts/darling-evidence.py cat PATH`, not
hand-written extraction. Archive only finalized owned diagnostics; preserve
metadata, unique proof and symlink targets. Read the
[storage contract](../../../docs/tooling.md#finalized-evidence-storage) before
bulk compression or compacting runtime evidence ahead of explicit GC.

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

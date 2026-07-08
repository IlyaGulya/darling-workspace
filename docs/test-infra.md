# Darling test infrastructure — design RFC

Status: local productized foundation; migration and CI rollout still open.
Owner: ilyagulya.

## 2026-07-08 Audit Refresh

The current upstream `darling-testsuite` still confirms the original direction:
use CMake/CTest as the backend and keep our local tooling as a thin
orchestration layer, not a second test framework. Upstream HEAD checked for
this refresh was `ce56358` (2026-07-05). It uses `add_test()`, CTest
`WILL_FAIL`, the install layout `darling-testsuite/{testcase,resource,manual}`,
`darling-testsuite-lib` for assertions/resources/XML, and
`darling-directsyscall` for direct kernel syscall tests.

The patch-profile audit found the real gap is not the choice of backend, but
test normalization and discoverability. Across `arch`, `homebrew`, and `perf`
there are 97 patch files with mixed proof styles: shell gates, raw C/C++ unit
tests, CMake/CTest targets, markdown acceptance notes, and the E-UNION
experiment runner. Many non-documentation fixes still have no committed red
test in their patch profile; `dar-r7z7` tracks that inventory.

Product direction:

- Keep upstream-compatible testcase sources and install layout as the portability
  seam.
- Put local ergonomics in metadata and `west test`: `bead:*`, `profile:*`,
  `patch:*`, `submod:*`, `env:*`, `diag:*`, `fuzz:*`, and `stress:*` labels.
- Support five runnable kinds through the same CTest surface: host
  unit/contract tests, Darling guest/runtime tests, macOS oracle tests, external
  package/repro gates, and bounded fuzz/stress jobs.
- Default host tests to `bare`, Darling guest tests to guarded
  `darling-debug-runner`, and forensic capture to explicit opt-in.
- Treat fuzzing as a labelled bounded runner contract: seed corpus, maximum
  time, artifact bundle, replay command, and minimized failures promoted to
  normal committed regressions.
- Grow `west test` selectors for `--profile`, `--patch`, red-test audit output,
  and manifest/submodule discovery so the runner can answer "what proves this
  patch?" without hand-grepping patch files.

### Patch-Local Red-Test Metadata

Local patch profiles do not need a separate upstream `darling-testsuite` patch
for every fix. The default workflow is: the smallest deterministic red test
travels with the fix in the same source repo and, when practical, the same patch
file/profile entry. Cross-repo bugs can use a small adjacent test patch in the
same profile. Upstream `darling-testsuite` remains the portable testcase style
and future destination, not a local blocker.

`patches.yml` entries may declare runnable proof metadata:

```yaml
- path: darlingserver/example-fix.patch
  module: darling/src/external/darlingserver
  bead: dar-example
  tests:
  - name: example_contract
    kind: contract        # unit|contract|guest|package|fuzz|stress|build|gate
    env: host             # host|darling|macos
    diag: bare            # bare|guarded|forensic
    red: true             # this proves RED->GREEN for the fix
    red-proof:
      mode: self          # self|source-base; normal runs still expect GREEN
      why-self: The script contains explicit bad and fixed arms.
    runner: script
    script: tests/run-example-contract.sh
    note: Fails on the parent commit, passes with this patch.
```

`red: true` does **not** mean the test should fail on the latest checkout.
Normal `west test --profile ...` runs are regression runs and must pass on the
current/fixed tree. It means the test is intended to prove a RED->GREEN
regression. That proof is exercised explicitly:

```sh
west test --profile homebrew --patch darling/mldr-thread-create-futex-wait.patch
west test --profile homebrew --patch darling/mldr-thread-create-futex-wait.patch --prove-red
```

RED proof modes:

- Every test with `red: true` must have `red-proof`. If a test is only a
  current-tree regression/acceptance gate, leave `red` unset instead of
  implying that `west test --prove-red` can prove the old tree fails.
- `red-proof: {mode: self, why-self: ...}`: the test contains its own
  bad-path oracle, such as running an old algorithm/model and requiring that it
  fails before running the fixed path. This is weaker than source-base proof;
  use it only when the negative case is explicit and self-contained.
- `red-proof: {mode: source-base, source-env: DSERVER_SRC_ROOT}`: `west test`
  takes the test from the current checkout, creates a temporary worktree at the
  patch's `source-base` (or `source-commit^` when no explicit base is recorded),
  points the named environment variable at that bad source tree, and expects the
  test to fail there before passing on the current tree. Use this only for
  source-root-aware scripts; do not rely on implicit checkout mutation.

Source/text checks are allowed only as auxiliary drift guards:

```yaml
  - name: example_source_contract
    kind: source-contract
    env: host
    diag: bare
    red: true
    red-proof:
      mode: source-base
      source-env: XNU_SRC_ROOT
    runner: python
    script: tests/west_source_contracts.py
```

`kind: source-contract` can prove that a hunk/symbol/comment is present or absent
on a source tree, but it does not prove runtime behavior. `west patch check`
therefore does **not** count source-contracts as patch coverage. A patch with
only source-contracts is reported as `SOURCE ... missing behavioral test` until
it also has a behavioral host/guest/build/package/fuzz/stress/gate test or a
real `test-exception`.

Use structured runners for common cases:

```yaml
  - name: dserver_stack_pool_tests_run
    kind: contract
    env: host
    diag: bare
    red: true
    runner: west-build
    target: dserver_stack_pool_tests_run
```

Script tests may declare arguments and environment without dropping to a shell:

```yaml
  - name: a0_gate_full_strict
    kind: guest
    env: darling
    diag: guarded
    red: true
    runner: script
    script: tests/a0-repro/a0-gate.sh
    args: [full]
    env-vars:
      A0_STRICT: '1'
    timeout-seconds: 600
    requires:
    - darling-prefix
```

Use `runner: python` for Python files that should be invoked through `python3`
rather than marked executable:

```yaml
  - name: progress_classifier
    kind: contract
    env: host
    diag: bare
    red: true
    runner: python
    script: tests/progress_classifier_test.py
```

Use `requires` for resources that the test framework can provide. Darling
guest/runtime scripts should declare `requires: [darling-prefix]`; `west test`
then supplies `DPREFIX` from `--prefix`, `--prefix existing:/path`,
`--prefix-profile homebrew`, or an already exported `DPREFIX`. Keep
`requires-env` only for low-level prerequisites that west cannot provision yet.
`west test --list` never requires those resources; real execution fails before
launch if a requirement is missing.

For metadata tests that use `requires: [darling-prefix]`, `west test` also owns
the resource lock and shutdown path. A real run takes `$DPREFIX/.west-test.lock`
before launching the test, holds it through cleanup, calls `darling shutdown`
for the selected prefix, and kills a matching leftover `darlingserver` if
shutdown did not finish cleanly. After cleanup it checks the remaining
`darlingserver` process tree for that prefix; leftover processes make the
`west test` run fail, even if the test payload itself passed. Pass
`--keep-prefix-running` only when intentionally keeping the prefix warm for a
manual debug loop.

For patch metadata, `diag: guarded` and `diag: forensic` are enforced by
`west test`, not by each script. `guarded` wraps the structured invocation in
`darling-debug-runner run --timeout-seconds ...`, writes a small debug bundle,
and kills the process group on timeout. `forensic` adds process-tree and GDB
capture. The runner is resolved from `--executor`, `PATH`, or the checked-out
`darling-debug-runner` west project (`target/release` preferred, then
`target/debug`). If a non-bare test is executed without a runner, `west test`
fails before launching the test. `--list` is still offline and shows the wrapper
shape without requiring the binary to exist.

Keep shell scripts thin. Static source-contract scripts should source a local
`contract-test-lib.sh` helper for common `fail`, `require_grep`, and
`require_text` assertions instead of copying that boilerplate into every test.
Guest runtime C fixtures should use a local `guest-verdict-test-lib.sh` helper
for the repeated DPREFIX flow: copy fixture into the prefix, launch
`darling shell`, poll for an `ORACLE_RC` verdict, print logs, and clean up the
host runner process. Bespoke scripts such as long A0 gates are acceptable, but
they should be the exception rather than the default shape for new tests.

Use `ctest-label` once the test is discoverable through the CTest registry.
This is a runnable selector: `west test` configures/builds the local
compatibility suite and executes `ctest -L <label>`.

```yaml
  - name: wait4_guest_contract
    kind: guest
    env: darling
    diag: guarded
    red: true
    ctest-label: bead:dar-example
```

`command:` is intentionally an override for corner cases only. Prefer
`runner/script`, `runner/target`, or `ctest-label` so `west test` owns how tests
are launched, filtered, deduplicated, and eventually wrapped by diagnostics.
`west patch check` validates structured entries and resolves `repo` against the
West manifest/path map. `west test` validates the script path against the actual
checkout immediately before running, because a profile may reference tests added
by another patch in the same stack and the current subrepo branch may not be the
profile integration tree.

If a non-documentation patch truly cannot carry a committed red test, record an
explicit exception:

```yaml
  test-exception:
    reason: doc-only
    note: Comment-only warning for code compiled out in all configurations.
```

The local gates are:

```sh
west patch check --profile arch
west patch check --profile arch --strict
west test --profile arch --list --red-only
west test --profile arch --patch darlingserver/stack-pool-empty-stack-handle.patch --list
west test --profile arch --patch darlingserver/stack-pool-empty-stack-handle.patch
```

Patch export must keep review diffs narrow. `west patch export` updates patch
files plus the touched entry's `source-commit` and `sha256sum` fields in
`patches.yml`; it must not reserialize unrelated entries or rewrite block
scalars/quoting across the profile.

Some gates need a consistent patch profile rather than the developer's current
mixture of fix branches. Mark those with `requires-profile: arch` (or another
profile name). `west test` will list those tests anywhere, but real execution is
allowed only when all modules touched by that profile are currently on
`integration/<profile>`, unless `--materialize-profile` is passed.

With `--materialize-profile`, selected profile metadata tests run from temporary
detached worktrees built from the West manifest revisions plus the profile's
patch files. For stacked profiles, base profiles are applied first. The live
checkout is not switched, and stale `integration/<profile>` branches are not
trusted for test assets. List mode never materializes worktrees.

## Problem

Regression reproducers for fixed bugs currently live as throwaway `/tmp/run-*.sh`
scripts and as prose in bead `notes` (e.g. `dar-e1j`, `dar-77o`). They are not
discoverable, not re-runnable, and not tied to the code they guard. We want:

1. A convenient way to run the tests a change could affect (fast local cycle on
   a PR), per submodule.
2. A full Darling-wide suite run.
3. Diagnosis when a test hangs — most Darling bugs are deadlock / lost-wakeup,
   not a clean assertion failure.
4. Tests colocated with the fix as much as upstream politics allow, without
   adding workspace metadata to the Darling source repos.

## What the industry does (this is not invented here)

The two closest analogues to Darling are **syscall/ABI compatibility layers**,
and both build the runner ON TOP of their existing build system rather than
writing a bespoke framework:

- **gVisor** (Linux syscall layer). One `cc_test` source is stamped by a Bazel
  macro into several targets that run the SAME binary in different environments
  — `_native` (host Linux, the differential oracle), `_runsc_systrap`,
  `_runsc_ptrace`, `_runsc_kvm`. Tests are tagged and selected by platform tag.
  Methodology: "write the test first and make sure it passes on Linux on the
  native platform" — i.e. the real OS is the oracle.
  <https://github.com/google/gvisor/blob/master/test/syscalls/README.md>

- **Wine** (Windows API layer). `winetest` builds one conformance test source
  for both Wine (`make test`) and real Windows (`make crosstest` →
  cross-compiled `.exe`). WineTestBot is a server farm of many Windows versions
  that runs the cross-compiled binaries — the same source validated across the
  matrix of target OS versions.
  <https://wiki.winehq.org/Wine_TestBot> ·
  <http://www.kegel.com/wine/sweng/2010/>

- **darling-testsuite** (the upstream Darling effort) already chose
  **CMake + CTest + Ninja** and ships its own loader-level test library
  (`darling-nostdlib`, `darling-directsyscall`, ObjC/CF assertions, XML report).
  Cases are `add_test()` entries, negative cases use `WILL_FAIL`, test names are
  hierarchical via `DARLING_PATH`/`DARLING_IDENTIFIER`. Cases are MIT-0 so they
  can also be compiled and run on real macOS.
  <https://github.com/darlinghq/darling-testsuite>

Takeaway: the runner = a thin layer over the build system's native test driver.
For Darling that build system is CMake, so the native driver is **CTest** —
isomorphic to gVisor-on-Bazel. CTest gives discovery (`--show-only=json-v1`),
labels (`-L`), parallelism, `WILL_FAIL`, JUnit (`--output-junit`),
`RESOURCE_LOCK` (serialise tests that share one prefix), and fixtures
(setup/teardown) for free.

## Considered alternatives

| Option | Verdict |
| --- | --- |
| Bespoke TAP runner | Rejected. Re-implements discovery/parallel/JUnit that CTest already has; TAP only pays off for sub-checks inside one binary, which the "1 binary = 1 bug = exit code" model does not need. |
| Bazel (like gVisor) | Rejected. Darling is ~150 CMake submodules; Bazel-over-CMake is a multi-month project and a non-starter upstream. |
| Plain CTest, no wrapper | Rejected. CTest has no "one test, N environments" concept and no hang diagnosis — exactly the two gaps below. |
| **CTest backend + thin `west test` + debug-runner executor** | **Chosen.** See below. |

CTest is the right backend even designing from scratch; that it matches the
upstream choice is a bonus that removes politics, not the reason.

## Design

Two gaps CTest does not close, addressed by a thin layer we own:

### Gap 1 — one source, many environments (the gVisor lesson)

`testkit/cmake/AddCompatTest.cmake` provides `add_compat_test()`, a generator
that mints one CTest entry per environment from one source and tags each with
labels the orchestrator consumes:

```cmake
add_compat_test(
  NAME       host_fork_lock_smoke
  SOURCE     regression/host_fork_lock_smoke.c
  ENVS       host            # host;darling;macos -> one ctest entry each
  BEAD       dar-gwn.5       # -> label bead:dar-gwn.5
  SUBMODULES xnu             # -> label submod:xnu
  MAY_HANG                   # -> route through the diagnostic executor
)
```

Labels emitted: `env:<env>`, `diag:<tier>`, `bead:<id>`, `submod:<name>`.
`env=darling` launches the binary via `${DARLING_SHELL}`; `env=macos` is the
native differential oracle; `env=host` runs directly (plain glibc HOST tests
like the loader-reset regressions).

### Gap 2 — diagnosable execution of hangs, WITHOUT cost blowup

The naive "wrap everything in the debug runner" is a trap on two axes:

- Speed: the runner's expensive features (gdb backtrace, `/proc` snapshot,
  rpctrace, process-tree capture) would slow every test.
- Disk: a real prefix's bundle dir reached **7.4G across 980 bundles** — 6.5G of
  it two `dserver.*.log` rpctrace files (1.6–1.7G each) from manual debugging,
  with nothing ever pruned. A naive runner writes a bundle on every run.

So diagnosis is a per-test TIER (`DIAG`), not a global switch, and the runner's
cost is paid only when it buys something:

| Tier | Wrapper | On green | On fail/hang | Default for |
| --- | --- | --- | --- | --- |
| `bare` | none — plain ctest exec | nothing | nothing | host, macos |
| `guarded` | runner as watchdog: hard timeout + process-group kill | ~20K text bundle (cmd/exit/stdout/stderr) | same ~20K bundle | darling |
| `forensic` | `--capture-gdb --capture-tree` | ~20K + gdb/proc | large bundle | opt-in per case |

Key properties that answer the speed/disk fear directly:

- `bare` writes **zero** artifacts (no wrapper at all). Stable HOST tests run at
  full ctest speed with no disk footprint — measured: the real dar-gwn.5
  regression as `bare` left nothing.
- `guarded` writes only a tiny text bundle (~20K: cmd/exit/stdout/stderr) per
  run — measured on a green run. This is the runner's current behaviour (it
  always makes a bundle in `run` mode); it is text-only, so 10k runs ≈ 200M and
  GC keeps it bounded. NOTE: if even 20K/run is unwanted, the cheap fix is a
  runner flag to skip the bundle on success — tracked as a follow-up, not done
  here (the runner is a separate repo with its own contract).
- rpctrace (the gigabyte source) is **never** on by default — forensic only, and
  even then opt-in separately. That, not the 20K text bundle, was the 7.4G.
- `bare` has literally no wrapper, so stable HOST tests run at full ctest speed.
- `guarded` is the guest default because a hang can come from the runtime, not
  the test logic — we observed a bare `darling shell echo` stall indefinitely;
  the watchdog converts that into a captured timeout instead of a wedged run.

Storage is bounded by GC: `west test --gc` keeps the newest `--keep-last N`
bundles and drops any over `--max-bundle-mb` (catches stray forensic
cores/traces). Verified: a 77M scratch dir with an 80M "forensic" bundle pruned
to 20K. This is the part neither CTest, gVisor, nor Wine offers, and the reason
`west test` exists rather than bare `ninja test` — but it is metered, not free.

### Orchestrator — `west test` (`west_commands/test.py`)

Sits beside `dw`/`patch`/`pr` in the existing control plane:

```
west test --all                 # full suite (wire into `west pr check` before publish)
west test --changed             # diff submodules vs manifest-rev -> -L submod:<changed>
west test --bead dar-e1j        # -L bead:dar-e1j  (beads graph -> live regression set)
west test --env host            # restrict environment
west test --diag guarded        # restrict diagnosis tier
west test --list                # show selection, no run
west test --gc --keep-last 20 --max-bundle-mb 64   # prune debug bundles
west test ... -j8 --output-junit r.xml   # passthrough to ctest
```

`--changed` is a fast local hint, NOT a CI gate: if the test↔submodule mapping
is incomplete a regression can slip through, so `--all` is mandatory before
publishing (planned: fold into `west pr check`).

## Running across macOS versions (the differential axis)

The point of the suite is differential: does Darling behave like real macOS. So
the same case must run on real macOS (the oracle) and on Darling, and ideally
across several macOS versions. Key facts (researched 2026-06):

- macOS binaries are **forward-compatible, not backward**: a binary built with a
  LOW deployment target (`-mmacosx-version-min=13.0`) against a recent SDK runs
  on 13.0 and every newer version. So the model is **build-once-run-many** — one
  universal binary, and the LIVE OS it runs on is the comparison axis, not a
  rebuild. A build-per-version matrix is only needed when the deployment target
  itself changes compile-time behaviour (availability branches) — opt-in later.
- Upstream already ships `availability.h` (`MACOS_10_0..MACOS_26_0`,
  `MIN_VERSION_MACOS_ABI_TARGET_SUPPORTED(min,max)` on
  `__MAC_OS_X_VERSION_MIN_REQUIRED`) for compile-time version gating, and — as of
  2026-06-14/15 — an **install layout** (`install(TARGETS -> testcase/)`,
  `install(DIRECTORY -> resource/)`) that is its transport: build, install the
  cases+resources, ship the dir to real macOS / Darling, run there.
- **Darling is a single fixed point** on the axis: it reports one version baked
  at its build time (`SystemVersion.plist`, currently 11.7.4; `EMULATED_VERSION`
  in kernel emulation). Not runtime-switchable — treat it as one `darling`
  environment compared against the matching real-macOS row.
- CI runner reality (2026): hosted `macos-14`, `macos-15` (+`-intel`),
  `macos-26`; `macos-13` is being retired; older than 13 needs a self-hosted VM
  farm (the WineTestBot model: build once, ship the binaries to pinned-version
  VMs, run, collect by version label).

How `add_compat_test` models this:
- `MIN_VERSION`/`MAX_VERSION` → sets `OSX_DEPLOYMENT_TARGET` (so it builds for
  that floor and runs upward) and emits a `macos:<min>-<max>` label.
- `INSTALL` + `RESOURCES` → emit the SAME `testcase/`+`resource/` install layout
  upstream uses, so a case authored here ships through their transport.
- `west test --label macos:15` selects a version slice (a CI matrix row picks
  its `runs-on` and the matching `--label`). Verified: a `macos` case with
  `MIN_VERSION 13.0 MAX_VERSION 15.0` configures clean and carries
  `macos:13.0-15.0`.

Not yet real here: a `macos` host to run on, and the install→ship→run plumbing
(that is the Tier 2 farm). The build knobs and labels are in place so wiring a
runner later is config, not redesign.

## Colocation & upstream stance

We develop a more ergonomic variant in our own tree while staying SEAM-COMPATIBLE
with upstream darling-testsuite (a convenience superset, not a fork). What is
shared vs ours:

| Layer | Shared with upstream (the seam) | Ours (ergonomics) |
| --- | --- | --- |
| Case source | format, MIT-0, nostdlib/directsyscall, availability.h | — |
| Registration | the `add_executable+link+add_test+install` it expands to | `add_compat_test()` wrapper |
| Ship to macOS | `testcase/`+`resource/` install layout | — |
| Run | plain ctest | `west test` (changed/bead/diag/label/gc) |

`add_compat_test` emits exactly what upstream writes by hand, so a case ports
upstream unchanged and the install dir is identical; if upstream later adds its
own helper we collapse into it. As of 2026-06-15 upstream still registers each
test by hand (3 lines × 282 files) — the wrapper addresses that exact pain.

- Test SOURCES use the upstream darling-testsuite format (CTest, MIT-0,
  nostdlib/directsyscall) so they import into that repo unchanged. We add cases,
  we do not fork the framework.
- ORCHESTRATION (`west test`, changed-only, beads, debug-runner) stays in this
  private workspace — Darling and darling-testsuite stay clean CTest.
- "fix + test in one PR" is handled the way WilsontheWolf suggested: testsuite
  as a submodule, the fix PR moves the submodule pointer (`west pr` already
  moves submodule pointers — see `dar-9h7`).
- CI tiering (decouples the SUID-in-container worry from getting value now):
  - Tier 0 (per PR, seconds): HOST regressions + reuse/lint, no prefix needed.
  - Tier 1 (submodule PR, minutes): build full Darling at the new submodule
    pointer, compare active West projects against their local `manifest-rev`
    refs plus dirty worktrees, normalize changed labels to project path
    basenames, and run `ctest -L submod:<changed>` (ccache already on in the
    root CMake). This is the only honest CI per CuriousTommy — submodules don't
    build alone.
  - Tier 2 (nightly/farm, à la WineTestBot): full suite on a matrix of Darling
    + real macOS versions. The testsuite's own long-term goal.

## Local Compatibility Suite

`testkit/` is a self-contained local compatibility suite; nothing under
`darling/` is touched. It compiles the REAL production code from the darling
checkout (auto-located as the sibling `../darling`, override with
`-DDARLING_SRC`).

- `testkit/cmake/AddCompatTest.cmake` — the `add_compat_test()` generator
  (EXTRA_SOURCES/INCLUDES/DEFINES/LIBS/WORKDIR let a case link the real code).
- `testkit/CMakeLists.txt` — registers compatibility cases, starting with the
  real dar-gwn.5 case.
- `west_commands/test.py` — the orchestrator, registered in `west-commands.yml`.

The dar-gwn.5 case is a REAL regression, not a stand-in: it mirrors
`tests/regression/run-glibc-fork-lock-reset.sh`, linking the harness against the
production `src/startup/mldr/glibc_fork_reset.c` (with `GLIBC_FORK_RESET_TEST_HOOKS`)
plus a dlopen'd TLS module.

Verified end-to-end on this machine:

- Builds the production `mldr/glibc_fork_reset.c` + harness + `tls_mod.so`;
  `west test --bead dar-gwn.5` passes in ~9–12s through real `west`.
- RED/GREEN proven by the harness output: with the reset DISABLED all three
  cases `HANG/DEADLOCK` (killed by SIGALRM); with the production reset ENABLED
  all three `PASS`. Exit 0 requires the bug to reproduce AND the fix to cure it.
- Diagnosis tiers: with no executor, `guarded` degrades to `diag:bare` (warned),
  test still runs. With the built `darling-debug-runner`, the same case runs as
  `diag:guarded`, COMMAND wrapped `<runner> run --name <test> --timeout-seconds
  60 -- <bin>` (confirmed via `--show-only=json-v1`).
- Selectors `--bead dar-gwn.5` (pass), `--bead nope` (empty), `--diag guarded`
  all work.
- `west test --gc --keep-last 2 --max-bundle-mb 64` pruned a 77M scratch dir
  (incl. an 80M over-cap bundle) to 20K; on the real dir it freed 6.5G (7.4G ->
  934M) by dropping two ~3.3G rpctrace bundles.
- Live evidence for the guest watchdog default: a bare `darling shell echo` on
  the existing prefix hung indefinitely (darlingserver stuck `Sl` 12+ min);
  guest tests therefore default to `guarded`, not `bare`.

## Open questions

- test↔submodule mapping: explicit `SUBMODULES` labels (chosen) vs directory
  tree vs build-graph. Labels are the gVisor/CTest-idiomatic answer; revisit if
  coverage gaps appear.
- GUEST (`env=darling`) execution needs a built prefix + Apple headers/toolchain
  for Mach-O cases. Local `west test --prefix-profile homebrew` is exercised;
  CI/container provisioning remains separate work.
- SUID removal for containerised Tier 1/2 — separate bead, not a dependency of
  Tier 0 or local `west test`.
```

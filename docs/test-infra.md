# Darling test infrastructure — design RFC

Status: local productized foundation with three-tier CI definitions; publication
and first remote matrix execution remain external rollout actions.
Owner: ilyagulya.

## CI execution contract

`.github/workflows/test-infra.yml` keeps privilege and trust boundaries explicit:

- every pull request runs changed-only host tests on a hosted Linux runner;
- pushes and manual trusted runs use a secretless self-hosted runner labelled
  `darling-rootless` for the `smoke:true` guest slice;
- nightly/manual runs execute the full rootless guest suite;
- one `macos-14` job builds and installs the native testcase bundle, then
  `macos-14`, `macos-15`, and `macos-26` run that identical artifact.

`ci/run-test-tier.sh` is the sole tier entrypoint. `ci/bootstrap-west.sh`
materializes a clean checkout before Linux tiers. Native macOS transport uses
the generated `compat-install-manifest.tsv`; `ci/run-macos-installed-tests.sh`
executes every installed testcase and validates its exact marker. Docker is
not part of the guest execution contract.

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
    coverage-tier: host   # runtime|compile|host|model|source
    runs: host            # host|guest|macos
    diag: bare            # bare|guarded|forensic
    red: true             # this proves RED->GREEN for the fix
    red-proof:
      mode: self          # self|source-base; normal runs still expect GREEN
      why-self: The script contains explicit bad and fixed arms.
    runner: source-contract-script
    script: tests/run-example-contract.sh
    note: Fails on the parent commit, passes with this patch.
```

Profiles may also define compact defaults. `test-profiles` are reusable test
defaults; `artifact-profiles` are reusable runtime deploy plans;
`resource-profiles` are caches/oracles/external runtime resources; and
`fixture-profiles` are setup/cleanup state. A test can use one or more profiles
with `use` or `extends`; later test fields override profile fields, nested
mappings merge recursively, and lists are replaced unless a field documents
special append behavior.

New manifests should use the explicit compact axes:

- `runs: host|guest|macos` declares where the test executes. `runs: guest`
  expands to the Darling guest envelope (`env: darling`, prefix lifecycle,
  launcher/debug timeout/cleanup); this must not be hidden in a catch-all field.
- `red-proof: source|runtime|self|none` declares how `west test --prove-red`
  proves the test fails without the fix. Keep this separate from `expect`,
  which describes green-run result expectations.
- `artifacts` lists build outputs that `west test` builds/deploys/restores
  during runtime proof.
- `resources` lists runtime/test resources such as DCC caches, host traces,
  stat deltas, or external services.
- `fixtures` lists setup/cleanup state such as E-UNION overlays or seeded
  prefix templates.

Do not introduce `needs`; it is too broad. Use `artifacts`, `resources`, and
`fixtures` so the manifest says what kind of dependency is involved.

```yaml
test-profiles:
  guest-c-runtime-red:
    kind: guest
    coverage-tier: runtime
    runs: guest
    diag: bare
    runner: guest-c-fixture
    repo: darling-workspace
    compile-flags: [-std=gnu11, -Wall, -Wextra, -Werror]
    red: true
    red-proof: runtime

artifact-profiles:
  xnu-kernel:
    module: darling/src/external/xnu
    build-targets: [system_kernel]
    deploy: [usr/lib/system/libsystem_kernel.dylib]

patches:
- path: xnu/example-fix.patch
  module: darling/src/external/xnu
  tests:
  - use: guest-c-runtime-red
    name: example_guest
    script: tests/example_guest.c
    ok-marker: EXAMPLE_GUEST_OK
    artifacts: xnu-kernel
```

The old verbose form remains valid during migration. New repetitive
guest/runtime metadata should prefer compact profiles so the manifest describes
what is unique about the test rather than restating runner boilerplate.
The `perf` profile carries the first real migrated examples:
`mldr_compact_fd_band_guest` uses the compact guest-C runtime RED profile plus
an `mldr-runtime` artifact profile, and `dcc2_valid_cache_guest` composes the
guest-command runtime RED profile with a DCC cache profile and `dyld-runtime`
artifact profile.

`coverage-tier` classifies the strength of evidence independently from `kind`:

- `runtime`: runs the real guest/runtime path (`env: darling`/`macos`, guest
  harnesses, package/runtime reproducers). This is the strongest publication
  evidence.
- `compile`: compiles, links, or builds a focused fixture/target that exercises
  the changed contract (`runner: c-fixture`, `runner: west-build`, build gates).
- `host`: executes a host-side behavioral contract script against real commands,
  generated outputs, or test assets, but not the full guest runtime.
- `model`: executes an explicit old-vs-fixed behavioral/state-machine model. It
  is a valid RED oracle when runtime reproduction is not stable or cheap yet,
  but it is weaker than runtime/compile evidence and should be visible as such.
- `source`: source/text audit only. It is not behavioral coverage and must use
  `kind: source-contract`.

If compact metadata omits `coverage-tier`, manifest normalization materializes
one conservative value from `kind`, `env`, and `runner` before any checker or
runner sees it. Set the field explicitly whenever the default would obscure an
intentional distinction, especially for `model`.

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
- `red-proof: {mode: guest-runtime-deploy, runtime-artifacts: [...]}`: the
  intended model for guest/runtime tests whose RED proof requires building and
  deploying bad runtime artifacts into the selected Darling prefix, then running
  the same guest fixture against bad and fixed runtimes. Metadata validation
  accepts `runner: guest-c-fixture`, `guest-command-fixture`, or the explicitly
  lifecycle-oriented `guest-runtime-script`, and requires declared runtime
  artifacts. Each artifact must declare `module`, Ninja `build-targets`, and
  `deploy` paths so the runner knows which source tree to materialize, what to
  build, and which prefix files to swap. `--prove-red --list` prints the deploy
  plan. Before allocating a runtime source forest, west validates every layer
  of the selected `base-profile` stack with `west patch verify`; an invalid
  layer is a profile applicability error, never a runtime RED result. Execution
  then creates a temporary bad source forest and CMake/Ninja build
  dir, shuts down the selected prefix, backs up the declared deploy paths,
  copies bad artifacts, requires the guest fixture to fail, restores the
  original artifacts, then runs GREEN on the current prefix. Do not substitute
  `source-base` for this mode. A valid runtime RED proof fails for the intended
  behavior, not just for any nonzero exit status. Prefer
  `expect-output-contains`/`expect-output-lacks` or a structured oracle that
  matches the bad behavior's diagnostic, timeout, errno, trace marker, or other
  stable symptom. A missing fixture source file, upload failure, compile setup
  failure, or unexpectedly passing bad runtime is an infrastructure failure to
  fix or track as a blocker, not a RED proof. Fixtures used to drive runtime
  RED/GREEN should be stable inputs owned by the workspace testkit/tests area
  unless a source patch deliberately injects diagnostics into the bad runtime.
  `prepare-fixture-before-deploy: true` is available for `guest-c-fixture`
  runtime proofs where the old runtime cannot be trusted to upload or compile
  the fixture. In that mode west uploads and compiles the guest C fixture on the
  current runtime, deploys the bad artifacts, then reuses the same guest binary
  id in run-only mode for RED. This is not a substitute for the bad-runtime
  oracle: if the run-only phase still fails during `darling shell` startup,
  namespace setup, RPC protocol bootstrap, or shellspawn readiness before the
  fixture reaches its own `main`, the proof remains blocked and needs a
  launch-free/direct harness instead of a broader matcher.
  For that split shape, use `red-proof.red-runner`: RED builds and deploys the
  bad runtime artifacts, runs the explicit RED runner under that deployment,
  checks the declared RED reason, restores artifacts, and then runs the
  original test as the GREEN runtime gate. The RED runner is for a real
  behavioral oracle such as a direct server protocol fixture; it is not an
  escape hatch for source matching or accepting unrelated startup failures.
  Runtime proofs may declare `red-proof.cmake-defines` for explicit CMake cache
  overrides needed by the proof, for example enabling a test/debug tool target.
  These defines are applied to both RED and GREEN runtime source builds, after
  the normal inherited/default feature flags, so the manifest remains the source
  of truth for non-default build shape.
  XNU `system_kernel` runtime proofs should also declare
  `red-proof.source-modules: [darling/src/external/darlingserver]` unless the
  proof has a specific reason not to. The libsystem_kernel build consumes
  RPC-generated headers/hooks from darlingserver; letting the source forest
  symlink the developer's live darlingserver checkout can mix profiles and make
  RED fail at build/link time for an unrelated branch state. Do not add broad
  runtime artifacts such as dyld just because the selected prefix can run it:
  each artifact must be part of the behavior under proof, or unrelated build
  failures can masquerade as RED.

Source/text checks are allowed only as auxiliary drift guards:

```yaml
  - name: example_source_contract
    kind: source-contract
    coverage-tier: source
    runs: host
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
    runs: host
    diag: bare
    red: true
    runner: west-build
    build-target: dserver_stack_pool_tests_run
```

Script tests may declare arguments and environment without dropping to a shell:

```yaml
  - name: a0_gate_full_strict
    kind: guest
    runs: guest
    diag: guarded
    red: true
    runner: script
    script: tests/a0-repro/a0-gate.sh
    args: [full]
    env-vars:
      A0_STRICT: '1'
    timeout-seconds: 600
```

Use `runner: python` for Python files that should be invoked through `python3`
rather than marked executable:

```yaml
  - name: progress_classifier
    kind: contract
    runs: host
    diag: bare
    red: true
    runner: python
    script: tests/progress_classifier_test.py
```

DCC cache guest tests should use the structured `dcc-cache` resource instead of
building cache files in ad hoc shell. The resource compiles the declared cache
builder, creates the cache under `/private/var/tmp`, exports the configured
guest environment variables, and removes the host cache directory after the
test. If the cache tools are test assets from a different module than the
runtime under test, set `source-ref` so west materializes only the declared
tools directory from that module instead of pulling the module into the runtime
source forest. Its default `install-root: guest-visible` selects the host root
that matches dyld's guest filesystem view: `DPREFIX` when
`DARLING_NOOVERLAYFS=1`, otherwise `DPREFIX/libexec/darling`. Use explicit
`install-root: base` or `install-root: prefix` only for tests that intentionally
validate one of those views.

`west test` provisions structured resources through typed providers, not
runner-local ad hoc setup. The current provider stack is ordered as:

1. `host-trace-files`: prepares prefix-relative host trace paths and exports
   their environment variables for host-launched fixtures.
2. `host-stat-deltas`: binds and preflights the host `darling-stat` tool used
   by guest runtime fixtures that assert before/after counter deltas.
3. `dcc-cache`: materializes/builds the declared cache tooling and injects the
   guest DCC environment.
4. `darling-eunion-prefix`: boots/verifies the E-UNION prefix and stages
   upper/lower fixture files.

Provider order is part of the contract: host observation paths and stat tools
are prepared before cache and prefix setup, and cache resources are prepared
before prefix fixtures that may boot or probe the runtime. New shared runtime
setup should become a provider with a focused contract instead of growing
individual runner bodies.

Runtime RED artifact planning lives in a separate helper layer. The pure
planning code owns build-target de-duplication, deploy-plan display, and mapping
guest-visible deploy paths to the prefix files that must be swapped. The
side-effecting build/deploy/restore sequence stays in `west test` until the
runtime lifecycle can be split further without changing behavior.

Darling prefix lifecycle helpers are also split from the runner where they are
pure enough to test directly. `west_commands/test_prefix.py` owns process-tree
discovery for `darlingserver <prefix>`, matching server PIDs, and stale
`.init.pid` removal. The runner still owns the side-effecting shutdown,
mount-cleanup, and lock orchestration.

`runner: guest-command-fixture` may check both process status and captured
output:

```yaml
    expect:
      returncode: any        # any|nonzero|timeout|integer
      output-contains:
      - 'dyld: DCC2: cache invalid/stale'
```

Use `returncode: any` only when the Darling launcher does not reliably propagate
the guest process status for the behavior under test. It is not a weaker oracle:
the test must still assert guest-visible output with `output-contains` or
`output-lacks`. For ordinary commands, prefer an exact integer status,
`nonzero`, or `timeout`.

Use `runner: c-fixture` for small host C fixtures that should be compiled and
executed directly by `west test`:

```yaml
  - name: select_fdset_conversion
    kind: unit
    runs: host
    diag: bare
    red: true
    red-proof:
      mode: source-base
      source-env: XNU_SRC_ROOT
    runner: c-fixture
    script: tests/select_fdset_contract.c
    include-dirs:
    - darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/select
    compile-flags: [-std=gnu11, -Wall, -Wextra, -Werror]
```

`c-fixture` compiles the fixture from the current test-asset checkout, but
resolves relative `include-dirs` against the source tree named by
`red-proof.source-env` during source-base RED proof. This lets the same fixture
compile against the bad tree for RED and the current materialized profile for
GREEN. `stub-headers` may list empty generated headers for isolated production
`.c` unit tests that include project-local headers not needed by the fixture.

Use `runner: source-contract-script` for workspace-hosted shell contracts that
execute current test assets against a source tree selected by `source-env`.
`west test` sets that environment variable to the selected profile's source tree
for normal GREEN runs, including `--materialize-profile`, and overrides it with
the temporary bad/source-base worktree for `--prove-red`. This keeps
workspace-hosted suites honest: the test asset can live in the workspace while
the source under test still comes from the current profile tree.

Use `runner: source-profile-script` when the shell contract is added by the
patch/profile itself. In RED proof, `west test` materializes the fixed GREEN
profile source tree first, runs the script from that tree, and points
`red-proof.source-env` at the temporary bad/source-base worktree. The same
profile-owned script is then run against the GREEN source tree. This proves the
old behavior fails for the intended reason without mistaking "the new test file
does not exist yet" for a regression.

Use `runner: source-script-fixture` only when the script itself belongs to the
source tree under test and already exists in both the RED source base and the
GREEN profile tree. Do not use it for shell scripts newly added by the patch:
the RED result would prove only that the script file is missing.
Executable source scripts run directly through their shebang; non-executable
source scripts fall back to `sh`.

Plain `runner: script` remains an escape hatch for tests with special process,
trace, or runtime orchestration. New source-base shell contracts should use
`source-contract-script` or `source-profile-script` instead of generic `script`.

When the patch parent in `source-base` cannot build the fixture because the
patch introduces the API under test, a source-base proof may set
`red-proof.source-revision` to an immutable earlier implementation commit in
the same source repository. `west test --prove-red` uses that revision only for
the RED arm; `source-base` remains the patch's real parent for patch ordering
and integration. This lets RED exercise the old behavior instead of merely
proving that a new symbol is absent. The same field is allowed for a
`guest-runtime-deploy` proof with `bad-profile: current-minus-patch`, where it
selects the known-buildable old runtime baseline. If that baseline predates
dependent profile patches, list those patches in
`red-proof.current-minus-skip-patches`; the skip is an explicit dependency
boundary, not an ignored application failure. The revision must be reviewable
and local to the module; do not use a floating branch name.

Use `runner: self-contract-script` for host scripts whose RED proof is fully
self-contained in the test itself: the script runs an explicit bad/model arm and
requires it to fail, then runs the fixed/current arm and requires it to pass.
These tests must declare `red: true` and `red-proof: {mode: self, why-self: ...}`.

Use `runner: guest-runtime-script` only for guest/runtime orchestration that the
structured guest fixture cannot express yet: multi-process gates, dserverdbg
oracles, prefix trace-file checks, or process-lifetime probes. It must declare
`runs: guest`; west still owns declared prefix resources, trace/temp files, and
runtime RED deployment.

Use `runs: guest` for tests that execute inside Darling. The compact form
expands to the low-level `requires: [darling-prefix]` envelope, and `west test`
then supplies `DPREFIX` from `--prefix`, `--prefix existing:/path`,
`--prefix-profile homebrew`, or an already exported `DPREFIX`. Use explicit
`resources`/`fixtures` for additional provisioned state, and keep `requires-env`
only for low-level prerequisites that west cannot provision yet. `west
test --list` never requires those resources; real execution fails before launch
if a requirement is missing.

If a real run reports missing prefix boot or guest compiler prerequisites, fix
the prefix through the framework instead of hand-editing it:

```sh
west darling-prefix-repair --prefix "$HOME/work/darling-prefix"
west darling-prefix-repair --prefix "$HOME/work/darling-prefix" --check
west darling-prefix-repair --prefix "$HOME/work/darling-prefix" --cleanup-mounts
```

The repair command creates the required `private/var/tmp` directories with mode
`1777` and restores canonical `CommandLineTools`/`DarlingCLT` clang links from
the versioned CLT already installed in the prefix. `west test` and
`west darling-doctor` share the same prerequisite checks, so a repaired prefix
is checked against the same contract that guest metadata tests require. The
`--cleanup-mounts` mode unmounts stale filesystems left under an otherwise idle
prefix; `west test` runs the same cleanup after `darling shutdown` and fails the
test run if mounts remain.

Historical rootless debug prefixes are separate from test scratch and must not
be removed with a broad `/tmp/darling-rootless-*` glob because that namespace
also contains source worktrees. Use `west darling-rootless-debug-cleanup --path
/tmp/darling-rootless-*-debug-* --dry-run` for one completed debug tree. It
refuses non-debug paths, mounted filesystems, and live processes whose
`DARLING_PREFIX` is inside the target. If ordinary removal reports an ownership
failure, rerun the same explicit command with `--sudo`; it uses
`rm --one-file-system` only after the same checks pass.

For metadata tests that use `runs: guest`, `west test` also owns the resource
lock and shutdown path. A real run takes `$DPREFIX/.west-test.lock` before
launching the test, holds it through cleanup, calls `darling shutdown` for the
selected prefix, and kills a matching leftover `darlingserver` if shutdown did
not finish cleanly. After cleanup it checks the remaining `darlingserver`
process tree for that prefix; leftover processes make the `west test` run fail,
even if the test payload itself passed. Pass `--keep-prefix-running` only when
intentionally keeping the prefix warm for a
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

Framework-internal contracts use Python modules in `tests/west_test_contracts/`.
The `tests/run-west-test-*-contract.sh` files are compatibility entrypoints and
should stay thin: change directory to the repo and invoke the matching Python
contract. Do not add large embedded-Python heredocs to those wrappers; if a
contract needs reusable logic, move it into a module and keep shell only for
CLI integration setup.

Use `ctest` once the test is discoverable through the CTest registry. This is a
runnable selector: `west test` configures/builds the local compatibility suite
or source fixture and executes `ctest -L <label>`.

```yaml
  - name: wait4_guest_contract
    kind: guest
    runs: guest
    diag: guarded
    red-proof: runtime
    ctest: bead:dar-example
    artifacts: [xnu-kernel]
```

For a test registered by the patched source repository itself, keep the source
CMake path explicit. `runner: darling-cmake-target-fixture` builds the patched
source target in an isolated superproject, then runs `ctest -L <label>` from
that build directory. Source-base RED proof still uses the current test asset
against the bad source tree; the fixture provides fallback target/test
registration when the old source did not yet have the CTest entry.

```yaml
  - name: libressl_nist_darling_cmake_target_regress
    use: darling-cmake-target-source-red
    build-target: darling_ec_tls_regress
    source-dir: libressl
    ctest: bead:dar-q95.6
```

The CTest backend command construction is deliberately small and separate from
patch/resource orchestration. `west_commands/test_ctest.py` owns `ctest`
argument building for label-backed patch tests and top-level selectors
(`--bead`, `--submodule`, `--env`, `--diag`, `--label`, `--changed`, list mode,
and passthrough args). `--submodule` accepts either a West project path
(`darling/src/external/xnu`) or the CTest label basename (`xnu`) and maps it to
`submod:xnu`. `west_commands/test.py` decides what to run and when to configure the
testkit; it should not grow new ad hoc CTest command assembly.
Source-repo CMake fixture execution lives in `west_commands/test_cmake.py`:
generated superprojects, Darling CMake macro shims, fallback CTest
registration, compiler launcher logs, and required compile-option checks belong
there rather than in the orchestrator.

`command:` is intentionally an override for corner cases only. Prefer
`runner/script`, `build-target`, or `ctest` so `west test` owns how tests
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
scalars/quoting across the profile. Export preflights the whole selected
profile before writing: every `source-branch`, `source-base`, and
`source-commit` must resolve, and suspicious patch-size growth is rejected
unless `--allow-large-output` is passed deliberately. Use
`west patch export --profile <profile> --patch <path>` for focused checks or
exports of one entry; the selector uses the exact `patches.yml` path and does
not write unrelated patch files or metadata entries.

Some gates need a consistent patch profile rather than the developer's current
mixture of fix branches. Mark those with `requires-profile: arch` (or another
profile name). `west test` will list those tests anywhere. On execution, if the
live checkout is not already fully on `integration/<profile>`, profile-bound
metadata tests are temporarily materialized in detached worktrees; this keeps
the developer's current checkout stable while making headers, source files, and
test assets come from the intended profile.

With `--materialize-profile`, selected profile metadata tests run from temporary
detached worktrees built from the West manifest revisions plus the profile's
patch files. For stacked profiles, base profiles are applied first. The live
checkout is not switched, and stale `integration/<profile>` branches are not
trusted for test assets. List mode never materializes worktrees.

For a bounded diagnostic A/B of a declared runtime provider, use
`--runtime-cmake-define NAME=VALUE`. The override is applied only to the
disposable runtime source/build/deploy transaction and is shown in its CMake
configuration; the profile remains the owner of required artifacts, source
modules, launcher environment, and cleanup. The option is for feature flags
such as `DARLING_GUEST_RECVSPIN=0`, not for changing framework-owned build
identity (`DARLING_PATCH_PROFILE`, install prefix, or build type). It must not
be committed into a profile merely to make a diagnosis pass.

Use `west patch check --quality` for low-noise structural audit warnings that
are not basic schema validity. `--strict-quality` turns those warnings into a
failing gate. Current checks intentionally focus on patterns that caused false
RED proofs in practice: XNU `system_kernel` runtime proofs without materialized
darlingserver, and non-dyld tests that deploy dyld as an unrelated artifact.

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
  Cases are `add_test()` entries; upstream negative cases can use `WILL_FAIL`, test names are
  hierarchical via `DARLING_PATH`/`DARLING_IDENTIFIER`. Cases are MIT-0 so they
  can also be compiled and run on real macOS.
  <https://github.com/darlinghq/darling-testsuite>

Takeaway: the runner = a thin layer over the build system's native test driver.
For Darling that build system is CMake, so the native driver is **CTest** —
isomorphic to gVisor-on-Bazel. CTest gives discovery (`--show-only=json-v1`),
labels (`-L`), parallelism, JUnit (`--output-junit`),
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

Our testkit intentionally does **not** expose CTest `WILL_FAIL`: it accepts any
non-zero exit, including an unrelated compiler, launcher, or timeout failure.
Use `EXPECT_FAILURE_MARKER` on `add_compat_test()` instead. The shared wrapper
requires both a non-zero exit and the declared fixed output marker before it
returns success to CTest.

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
`env=host` builds and runs a normal local executable (plain glibc HOST tests
like the loader-reset regressions). `env=darling` is source-driven: CTest calls
the shared `testkit/scripts/run-darling-c-test.sh` helper, which uploads the C
source into the selected prefix, compiles it with the guest CLT, and runs the
guest binary through `DARLING_LAUNCHER shell`; it must not run a Linux host
binary under Darling. `env=macos` is the native differential oracle.

Every `env=darling` registration receives the framework-owned
`runtime-profile:homebrew` label by default. Product tests therefore do not
name libraries, deploy paths, or an ordinary provider: west resolves that
provider's source profile, build targets, Mach-O closure, deployment and
restore transaction. `RUNTIME_PROFILE` is an override only when the test's
subject is a different product runtime, such as the rootless E-UNION variant
or a perf-only `darlingserver` build. It is not a general dependency list.

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
cores/traces). It also prunes stale runtime/source-profile scratch dirs older
than `--proof-scratch-max-age-hours` and retains at most
`--proof-scratch-keep-last N` fresh scratch dirs. The count cap matters after
a string of recent build failures: preserved CTest source/build trees cannot
silently fill the disk before the age threshold expires. Each GC run reports
every retained or pruned scratch directory with its path, size, age, and
retention reason; symlinks are deliberately ignored, so cleanup cannot follow
a matching name into a canonical worktree. Use `--dry-run` to inspect the plan
without deletion. Verified: a 77M scratch dir with an
80M "forensic" bundle pruned to 20K. This is the part neither CTest, gVisor,
nor Wine offers, and the reason `west test` exists rather than bare
`ninja test` — but it is metered, not free.

A failed runtime source/build is not ordinary scratch. It is retained as one
manifested unit under `.west-test/runtime-evidence`, with its source tree,
build directory, failure reason, provider context and owned Git worktrees.
Ordinary `west test --gc` never deletes those units. Removal is deliberate:
`west test --gc --gc-runtime-evidence` applies the configured proof-scratch
age/count policy and first removes only the worktrees listed by that unit's
manifest. A path is reported as preserved only after this manifest exists.

Long configure/build commands use the same bounded process runner but emit a
30-second heartbeat while their output is captured for failure diagnostics.
This keeps large targets such as `rootless_bootstrap` visibly alive without
turning compiler output into an unreviewable stream. If the caller is
interrupted, the next `west test --gc --gc-runtime-evidence` pass removes an
unlocked orphan `.inflight-*` unit and its recorded worktrees.

### Orchestrator — `west test` (`west_commands/test.py`)

Sits beside `dw`/`patch`/`pr` in the existing control plane:

```
west test --all                 # full suite (wire into `west pr check` before publish)
west test --changed             # diff submodules vs manifest-rev -> -L submod:<changed>
west test --bead dar-e1j        # -L bead:dar-e1j  (beads graph -> live regression set)
west test --submodule xnu       # -L submod:xnu
west test --env host            # restrict environment
west test --env darling --prefix-profile homebrew
west test --diag guarded        # restrict diagnosis tier
west test --fuzz                # restrict to fuzz:* labelled jobs
west test --stress              # restrict to stress:* labelled jobs
west test --list                # show selection, no run
west test --gc --keep-last 20 --max-bundle-mb 64 --proof-scratch-keep-last 2
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

# Darling workspace

This repository contains private coordination state, not Darling source code.
It is the source of truth for workspace manifests, tasks, unpublished branch
refs, PR drafts, and agent handoff.

## Project tool environment

- `mise.toml` is the canonical host-side CLI environment for this workspace.
  On a fresh checkout, review it, run `mise trust`, then run `mise install`
  from `darling-workspace`; from the West workspace root use
  `mise -C darling-workspace trust` and `mise -C darling-workspace install`.
- Run workspace orchestration with `mise run dw ...` and West commands with
  `mise run west ...`. Both tasks select the pinned environment and forward
  arguments and standard streams to the command. From the West workspace root,
  use `mise -C darling-workspace run dw ...` or
  `mise -C darling-workspace run west ...`.
  Put `--` after the task name when forwarding opaque argument lists or a
  literal `:::`; without that boundary mise treats `:::` as task separation.
- Bare `west` command names below are shorthand for the `west` mise task.
  Use `mise exec -- uv ...` for isolated Python environment management.
- Do not depend on globally selected mise shims or a user-global `uv`/`west`.
  A bare command is acceptable only inside a shell already activated by this
  project's mise configuration.
- From the manifest directory, use `mise run dw dev` for managed diagnostics.
  Configure/select a named context once with
  `mise run dw dev context NAME --prefix /absolute/path/to/prefix`, then use
  `mise run dw dev run homebrew-prepare`, `homebrew-preflight`, `exact-capture`,
  or `homebrew-source`. A new bootstrap prefix must be absent or empty.
  Resolve the workspace and prefix from configuration before running.
  Read `mise run dw dev run --help` for explicit overrides.
- Registered scenarios execute through initialized West command state.
- Contexts are local West configuration, not portable runtime artifacts.
  `--context` and explicit run overrides do not rewrite the active context.
  The default runtime is `homebrew-lz4-source`; without an explicit executor,
  the checked-out runner is incrementally built in release mode. `--dry-run`
  displays the underlying command without building, booting or creating a job.
- `dev run` attaches observation by default; `--detach` is explicit.
  Use the printed `mise run dw dev follow JOB` and `mise run dw dev cancel JOB` commands
  for reconnect/cancellation. Do not duplicate prefix locks or cleanup around
  these scenarios. For tests without a registered scenario, use `west test`
  and the supervised low-level job interface described below.
- Repository `.claude/skills/` files are the portable skill sources. Keep
  installed skill copies and agent entry references synchronized when changing
  this workflow; `mise run dw setup-local` preserves an existing differing
  source instruction file, so it is not an overwrite/synchronization command.
- Keep agent instructions and skills normative: commands, contracts, ownership
  and safety rules. Store chronology, incident narratives, measurements and
  one-off acceptance results in Beads or diagnostic evidence.
- Do not use `python3 -m venv` or `ensurepip` for automation. Some supported
  hosts intentionally ship Python without `ensurepip`. Create isolated
  environments with `mise exec -- uv venv <path>` and install packages with
  `mise exec -- uv pip install --python <path>/bin/python ...`.
- A clean isolated West environment uses the pinned versions, for example:
  `mise exec -- uv venv <path>` followed by
  `mise exec -- uv pip install --python <path>/bin/python west==1.5.0`.
  Do not install tools into the system Python or mutate the user's global
  Python environment.

- Canonical fix source: the clean `fix/*` branch.
- Portable integration source: patch files and `patches.yml`.
- Canonical PR text: `pr-drafts/*.md`.
- Process state: the owning Bead.
- Fork and upstream PRs are published views, not sources of truth.
- Run source commands in the West workspace, not in this manifest repository.
- Use `west dw beads ...` for issue operations. For comments, use
  `west dw beads comment <id> <text>`; it is a workspace alias for the Beads
  `comments add` subcommand.
- Beads flag spelling differs by command: `create` accepts `--labels`, while
  `list` filters with singular `--label`.
- Use the active harness's dedicated read, search, glob and edit tools where
  provided. For command-only transports, bound full-forest searches through
  `scripts/west-search.py PATTERN ROOT...` and use repository-local searches
  for focused work. Follow the harness's shell and process-supervision rules.
- In RTK shell transports, use `rtk proxy find` for compound find operations
  and `rtk bash -c` for shell syntax. Keep assignments and quoted free text
  within the execution interface's supported argument/environment mechanism.
- When passing Bead free text through `rtk bash -c`, do not use shell backticks
  in the title, description, or reason: Bash evaluates them before `west dw
  beads` receives the text. Use plain command names or a file-backed argument.
- For long commands without a managed `dev run` scenario, use
  `scripts/west-job.sh start --state-dir DIR -- <mise-managed-command>`,
  then stay attached with `scripts/west-job.sh follow --state-dir DIR`.
  The observer validates PID identity; its timeout leaves the job running.
  Reconnect with `follow`; use `status` for recovery after interruption.
  Do not implement separate nohup/setsid/PID/rc-file or shell-sleep polling
  recipes, and do not treat a detached tool handle as command completion.
- The runner publishes `BUNDLE` at creation and registers stdout/stderr with
  the active job; Homebrew registers its build-log directories automatically.
  Manual `--activity-log` is only for additional logs. `--forward-output`
  is post-execution replay, not live streaming. Observed phase and
  last-write age do not prove progress or a hang.
- Bootstrap runs `darling-doctor --scope workspace` before build/deploy and
  mandatory `--scope runtime` checks after guest smoke; `--scope all` is
  the default outside that split. On failure, inspect full command output and
  structured problems in the retained evidence archive before diagnosing a
  post-rollback prefix: the restored prefix is a different state.
- Run long contract scripts that invoke `west test` internally (notably
  `tests/run-west-test-metadata-contract.sh`) through `scripts/west-job.sh
  start` and inspect the recorded state. In `CODEX_CI` that contract refuses a
  direct launch before it can create nested test processes; `west-job` supplies
  the required explicit transport context.
- Guest/runtime measurement has its own tooling; use it instead of ad-hoc shell.
  The tools are `scripts/darling-boot-run.sh` (one measured run: clean start,
  unique log, the workload's own duration, markers plus counters, cleanup, one
  `VERDICT`), `scripts/darling-deploy-verify.sh` (sha256 over EVERY runtime copy
  of a component), `scripts/darling-artifact-manifest.sh` (content baseline
  without git history, and probe-tag presence), `scripts/darling-describe-artifact.sh`
  (LC_MAIN, symbol and tag location, and whether the loaded copy is the built
  file), `scripts/darling-trace-guest.sh` (what a short-lived guest process
  actually maps), `scripts/darling-prefix-map.sh`, and `scripts/prefix-cleanup.sh`
  (prefix-scoped, with the guest-process arm). The rules each one enforces and
  the incidents behind them are in `docs/tooling.md` and the `darling-diagnostics`
  skill; read them before instrumenting guest code. In particular: a probe must
  be a single literal tag written in one syscall with every ABI register
  preserved; the raw `syscall` number in a guest is LINUX-numbered (`write` is 1);
  a silent probe is not evidence until its presence in the DEPLOYED artifact is
  confirmed; and a run can be served by another prefix's live runtime.
- Use `west patch verify|apply|clean|list` for local integration profiles.
- Use `west darling-prefix-repair --prefix <prefix>` when guest tests report
  missing prefix prerequisites such as `private/var/tmp`, canonical
  `CommandLineTools`, or `DarlingCLT` clang links. Do not repair those by
  undocumented manual `mkdir`/`ln` sequences unless the command itself is what
  you are debugging.
- `--prefix PATH` only resolves a prefix; it does not create, boot or provision
  one. Bootstrap a fresh prefix as its own invocation before running any
  prefix-backed selection: `west test --prefix <prefix>
  --bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal` for the
  baseline launcher, or `homebrew-guest-toolchain-provisioning` to also install
  the reviewed guest CommandLineTools. Both reject `--profile`/`--patch` and the
  CTest selection options. A bootstrapped prefix has `<prefix>/bin/darling` and
  retains `<prefix>/.west-runtime-profile.json`; `west test` names that bootstrap
  command when a selection finds the selected prefix unusable. See
  `docs/test-infra.md`.
- Guest CTest cases compile inside Darling. Their runtime profile must declare
  `guest-toolchain: darling-command-line-tools`; `west test` provisions the
  official CommandLineTools packages through Darling's guest `installer` when
  the prefix does not already contain them. The package cache is external to
  patchsets; never commit CLT payloads or generated snapshots.
- CLT provisioning validates size, XAR boundaries, the compressed-TOC SHA-1
  and independently reviewed full-file SHA-256 before installation. Reject
  integrity mismatches before executing the installer. Publisher authentication
  is a separate verification obligation from local hashing.
- Native-tools preflight and full Homebrew source-build acceptance are distinct
  gates. Derive SDK and runtime identities from authoritative metadata and
  validate compatibility. Record incompatible combinations in the owning Bead;
  do not fabricate SDK versions or bypass package-manager compatibility checks.
- The managed exact-capture diagnostic intentionally times out its payload.
  Success requires a verified guest Mach-O, matching structurally complete
  ELF core, registers and prefix cleanup. The archive's `exact_complete` flag
  is independent and may be false for unreadable mappings such as Linux
  vsyscall pages; preserve those warnings.
- If a Darling guest run leaves mounted filesystems under a prefix, use
  `west darling-prefix-repair --prefix <prefix> --cleanup-mounts` after
  confirming no Darling processes are left. Do not ignore prefix mount tails;
  either clean them through tooling or keep the owning Bead open with the exact
  repro.
- Never glob-delete `/tmp/darling-rootless-*`: active source worktrees use that
  prefix. For one completed debug prefix use `west
  darling-rootless-debug-cleanup --path /absolute/path/to/debug-prefix --dry-run`
  first. The command refuses non-debug paths, live `DARLING_PREFIX` owners, and
  mounts; add `--sudo` only after that inspection reports an ownership failure.
- Treat clean `fix/*` branches as canonical editable source.
- Treat patch files and `patches.yml` as portable integration artifacts.
- Treat `integration/*` branches and profile `west.lock.yml` files as generated.
- Never edit `integration/*`, open PRs from them, or edit generated locks.
- Never edit a patch without refreshing its full source SHA and checksum.
- Unified patch archives contain significant blank context lines encoded as a
  single space, so `git diff --check` reports false trailing-whitespace errors
  when it checks the archive as ordinary text. Run the whitespace check with
  `':(exclude)patches/**/*.patch'`, then use `west patch export --check` and
  `west patch verify` to validate the patch payload itself. Do not rewrite
  those context lines or disable whitespace checks for source files.
- Publish PRs only with `west pr`, one Bead at a time, from clean `fix/*`
  branches. Never publish generated, backup, or historical mega-branches.
- Fork-local draft PRs are staging review objects, not private artifacts.
- Safe read-only PR commands are `list`, `dashboard`, `check`, `publish-plan`,
  `sync`, and `open --print`.
- Do not run any mutating PR command without explicit user approval:
  `fork-draft`, `upstream-draft`, `update-body`, or `ready`.
- Upstream PRs are strictly opt-in. Approval to create or update a fork-local
  PR never permits creating, updating, reopening, marking ready, or otherwise
  mutating an upstream PR. Every upstream mutation requires separate explicit
  user approval naming the upstream action.
- Never bulk-publish fixes or merge PRs from workspace automation.
- Never change a GitHub PR body directly. Update `pr-drafts/*.md`, then run the
  explicitly approved `west pr update-body`.
- Never publish when `west pr check` fails.
- Treat `publication-status` in a patch profile as an architecture gate:
  `blocked` is local-only, `provisional` may be staged in a fork-local draft
  but not published upstream, and only `ready` may be published upstream.
- Do not remove or weaken a publication blocker without resolving its owning
  Bead and updating the foundational review evidence.
- Run `west dw handoff` before ending a session that changed Beads or private
  branches.
- After `west dw handoff`, stage only the handoff files it actually changed;
  never use `git add -A` as a shortcut, because unrelated in-progress fixes
  would be misfiled in a handoff commit.
- Forest handoff does not automatically cover sibling tooling repositories.
  Preserve every changed canonical runner/tool branch in its actual keeper
  bundle and verify the tip with `git bundle list-heads`; do not assume that
  a successful forest handoff included it. Keep local context paths and
  diagnostic archives outside product patches.
- Never add workspace metadata, PR drafts, agent state, or Beads files to the
  Darling source repositories.
- Do not push investigation branches unless explicitly requested.
- Resolve edit destinations against the intended repository root. Use absolute
  paths for files outside the current directory, and verify new file placement
  before running tests. Keep workspace test assets in the manifest repository.
- For focused `darlingserver` validation, use
  `west darling-build --targets darlingserver --deploy --deploy-darlingserver`
  for a targeted build. With `--deploy`, this command deploys dyld/closure as
  well; record those artifacts in the experiment scope. Use a verified
  server-only deployment path when isolation is required.
- Patch-local red tests must be GREEN on the current checkout. `red: true`
  means the test is a RED->GREEN regression proof, not that latest should fail.
  Use `west test --profile ...` for the normal GREEN regression run and
  `west test --profile ... --prove-red` for explicit RED-proof mode. Do not
  fake source-base RED proofs with ad hoc shell; add shared runner support when
  a test needs current test assets executed against a bad/source-base tree.
  Use `runner: source-contract-script` for workspace-hosted shell contracts
  whose test asset stays fixed while `red-proof.source-env` switches the source
  tree under test. Use `runner: source-profile-script` when the script itself
  is introduced by the patch/profile: `west test --prove-red` must take that
  test asset from the materialized GREEN profile tree and point `source-env` at
  the bad/source-base tree for RED. Use `runner: source-script-fixture` only for scripts that
  already belong to the source tree in both RED and GREEN; do not use it for a
  script introduced by the patch, because RED would only prove "file missing".
  Executable source scripts are run through their shebang; non-executable ones
  fall back to `sh`.
  Use `runner: self-contract-script` for host scripts that contain explicit
  bad/model and fixed/current arms and therefore prove RED through
  `red-proof: {mode: self, why-self: ...}` without mutating source checkouts.
  Use `runner: guest-runtime-script` for guest/runtime orchestration that is not
  expressible as `guest-c-fixture` yet, such as dserverdbg gates, trace-file
  oracles, multi-process lifetime probes, or guarded A0-style acceptance gates.
  Prefer `red-proof: {mode: source-base}` when a regression can be proven
  against the bad source tree. `red-proof: {mode: self}` must include
  `why-self:` and is only for tests with an explicit bad/good behavioral model,
  generator execution oracle, or similarly self-contained negative case that
  executes behavior rather than matching source text.
  Tests that only prove the current/fixed tree stays GREEN must leave `red`
  unset.
- Do not mark `guest-c-fixture` tests `red: true` just because their patch has
  `red-proof: {mode: source-base}`. Source-base proof swaps source checkouts for
  source/compile tests; it does not rebuild and deploy the bad dylib/server into
  the Darling prefix. For deployed guest behavior, a guest test is a GREEN
  runtime gate unless the runner explicitly builds/deploys the bad runtime,
  restores the fixed/current runtime, and runs the same guest fixture against
  each.
  Use `red-proof: {mode: guest-runtime-deploy, runtime-artifacts: [...]}` for
  that model. The runner must use temporary source/build state and backup/restore
  every declared prefix deploy path; never fall back to current-prefix smoke or
  fake `source-base` proof for deployed guest behavior. A guest-runtime RED
  proof should also pin the failure reason with `expect-output-contains` or an
  equivalent structured oracle when the runner can capture output. Do not accept
  a RED arm that failed because the test fixture/source file was missing,
  upload/compile infrastructure failed, or the bad runtime unexpectedly passed;
  fix the fixture ownership/materialization or create a blocking task instead.
  Runtime fixtures should normally live in the workspace testkit/tests area and
  stay the same for RED and GREEN; the current-minus source forest is for
  building bad runtime artifacts, not for silently losing the test input.
  If the bad runtime is too unstable to upload or compile the guest fixture, a
  proof may use `prepare-fixture-before-deploy: true` to upload/compile on the
  current runtime and then run the prepared guest binary after bad artifact
  deployment. This only removes upload/compile from the RED cause. If the bad
  runtime still cannot start `darling shell` or reaches protocol/shellspawn
  failures before the fixture's `main`, do not keep adding source patches or
  broad matchers; create/use a launch-free or direct runtime harness and keep
  the shell-based proof blocked.
  If RED needs that alternate harness while GREEN should remain the normal guest
  runtime gate, express it as `red-proof.red-runner`. The bad-runtime phase then
  deploys the declared artifacts and runs the explicit RED runner; the GREEN
  phase still runs the original test on the fixed/current runtime. The RED
  runner must execute a real behavioral oracle for the old runtime, not source
  text matching or a startup/protocol failure unrelated to the patch contract.
  If the proof needs a non-default runtime build option such as a test/debug
  tool target, declare it under `red-proof.cmake-defines` so the RED/GREEN
  runtime source builds are reproducible; do not rely on the caller's local
  `CMakeCache.txt` having that target enabled.
  For XNU `system_kernel` runtime proofs, include
  `red-proof.source-modules: [darling/src/external/darlingserver]` unless there
  is a documented reason not to. libsystem_kernel builds consume RPC-generated
  darlingserver headers/hooks; symlinking the live checkout into a materialized
  current-minus source forest can mix profiles and turn RED into an unrelated
  build/link failure. Keep `runtime-artifacts` minimal: do not deploy dyld or
  other broad artifacts unless the patch/test is actually about that artifact.
  Run `west patch check --quality` after metadata edits that affect runtime
  proof shape.
- Runtime builds reuse an identity-keyed source forest and build tree. The store
  is `<manifest>/.west-test/runtime-build-cache`, a `.west-test/` state root;
  `WEST_RUNTIME_BUILD_CACHE_DIR` relocates it, `WEST_RUNTIME_BUILD_CACHE=off`
  disables reuse, `WEST_RUNTIME_BUILD_CACHE_MAX_BYTES` bounds it (default 12 GiB,
  pruned after each runtime profile build) and `WEST_RUNTIME_BUILD_CACHE_KEY`
  still overrides the derived key for CI. Reuse requires a matching identity:
  source revisions and patchset digests, the materializer version, the
  configured defines and targets, the compiler fingerprint and the deploy
  prefix. Never widen that match to make a run faster, and never reuse a source
  forest whose completion marker is absent: an interrupted materialization is
  discarded and rebuilt rather than consumed. Never delete the store with
  `rm -rf`: its entries are Git worktrees registered in the Darling repository
  and in every hydrated nested repository, so a manual wipe leaves registrations
  whose directories are gone. Discard through the framework, which unregisters
  them, and let `_add_detached_worktree` recover if one is found. The forest
  path must stay stable
  across runs, because ccache normalises absolute paths against the current
  working directory, so a per-run temporary root makes every object a miss
  regardless of `CCACHE_BASEDIR` or `-fdebug-prefix-map`.
- The stock Homebrew stack a replay needs is cached by identity. The store is
  `<manifest>/.west-test/stock-stack-cache`, a `.west-test/` state root;
  `WEST_STOCK_STACK_CACHE_DIR` relocates it, `WEST_STOCK_STACK_CACHE=off`
  disables it for an acceptance-grade run that has to build from source and
  says so in the run output, and `WEST_STOCK_STACK_CACHE_MAX_BYTES` bounds it
  (default 12 GiB, enforced before a snapshot is added). A snapshot is reused
  only when the pinned brew and homebrew-core revisions and input digests, the
  resolved formula versions, the guest CommandLineTools identity the framework
  verifies, the deployed runtime identity, the snapshot layout schema and the
  host architecture all match. Never widen that match to make a run faster, and
  never restore a snapshot whose completion marker is absent: a capture is
  built under a temporary path and published with its marker, so a killed
  capture is never consumed. A missing marker, a corrupt archive, a file whose
  recorded sha256 differs, or a lost formula source receipt is a miss, and a
  miss stages the pinned resource and rebuilds from source rather than serving
  a partially verified stack.
  A miss on a prefix that already holds an installation is adopted instead of
  staged: the stock replay phases consume the stack that is there, and staging
  refuses such a prefix by design, so a replay test declares the `homebrew-lz4`
  resource and gets the stack restored, adopted, or staged, in that order. A
  test whose subject is the from-source build itself declares
  `WEST_STOCK_STACK_RESTORE=off` in its own `env-vars`: it always stages,
  because a snapshot must never stand in for the build that test measures, and
  the store stays enabled so its completed stack is still captured for the
  replay phases. Cache decisions read the invocation's environment overlaid on
  the host environment, never one instead of the other.
  A test that consumes the stock stack IS the from-source measurement: it always
  executes, and no verdict is ever reused for it, because the receipt checks that
  prove the build cannot tell a freshly built keg from one that was restored or
  skipped. Reusing such a verdict would make the acceptance claim vacuous, so the
  framework counts those runs as `skipped-source` and never serves them; only
  guest work that is not the from-source measurement may be reused.
- The stock replay's wget-repeat phase reinstalls wget from source twelve times
  on purpose: the repetition is what exposes intermittent transport and
  lifecycle failures, so that count is the acceptance workload. For iteration
  only, `WEST_STOCK_WGET_ITERATIONS` lowers it; every shortened run reports
  `STOCK_WGET_REPEAT iterations=N acceptance=0` in its own output, and its
  result is not acceptance evidence. Do not shorten the count while collecting
  acceptance evidence, and do not put a compile cache in front of the guest
  compiler: the source build is the workload these tests measure, and their
  receipt checks cannot tell a cached build from a real one.
- Guest metadata verdicts are reused by identity: the test asset bytes, the test
  declaration, the runner with its args, timeout and expected markers, the
  runtime identity the test runs against, the stock stack snapshot identity when
  that resource is consumed, the workload parameters, the guest toolchain and
  the host architecture. Only a zero verdict is reused, a non-zero verdict is
  always re-executed, a reused verdict is announced with its identity digest and
  original run time, and `WEST_TEST_VERDICT_CACHE=off` forces a fresh run. A
  cached verdict cannot detect flake - the unreproduced brew config segfault
  passed on re-run and failed earlier - so acceptance claims need a fresh run on
  the current identity, not a cached one.
- `runtime-load-smoke` binds `tests/_runtime_load_probe.py` under both transports
  to reproduce the load the stock replays provoke (concurrent guest CLT compile
  and link, a fork/exec storm, descriptor churn) in minutes rather than hours.
  It is a regression signal, not a substitute for the end-to-end stock
  acceptance, and it stays in its own profile so the acceptance profile's claim
  is unchanged.
- Do not close patch coverage with source matching. Tests that grep, parse, or
  assert that specific code text exists are audit checks only; they must not be
  counted as the patch's behavioral test and must not be recorded as `kind:
  contract` or any non-source `coverage-tier` to make `west patch check` show
  HOST/MODEL/COMPILE/RUNTIME coverage. A patch is covered only by
  a test that executes behavior: guest/runtime test, C/host fixture, model test
  that drives the state machine, generator test that runs the generator and
  validates generated behavior, or a build/compile/link test that exercises the
  changed build contract. If behavior cannot yet be executed, leave the patch
  MISSING/SOURCE and create a task instead of faking coverage.
- For build-system patches, the RED proof must exercise the build path and
  target contract that the patch actually changes. Do not prove a CMake target
  fix through an alternate autotools/manual compile path, and do not inject the
  fixed compiler/linker flag from test metadata in a way that makes the bad
  source tree pass. If current test assets were added by the patch, the runner
  may copy those assets into a bad/source-base build tree, but the bad tree must
  still fail because of the old build behavior, not because a file is missing.
- Do not run cleanup/leak assertion contracts in parallel with commands that
  legitimately create temporary worktrees or prefixes, such as
  `west test --prove-red` or materialized-profile runs. Run those checks
  sequentially after the creating command exits, otherwise the assertion can
  report a false leak. In practice, do not put
  `tests/run-west-test-metadata-contract.sh`,
  `tests/run-west-test-prefix-cleanup-contract.sh`, or manual
  `west-profile-`/`west-red-proof-` leak checks in the same `multi_tool`
  parallel batch as any `west test` command. Treat cleanup/leak checks as a
  final separate phase after all RED/GREEN test commands have exited; if a
  cleanup contract fails while another `west test` was running, rerun it
  sequentially and fix the process mistake before making further claims.
- Classify patch test evidence with `coverage-tier`: `runtime`, `compile`,
  `host`, `model`, or `source`. Any bad/fixed behavioral model must be explicit
  `coverage-tier: model`; source/text audits must be `coverage-tier: source`
  and must not be counted as behavioral coverage. A `source-*` runner defaults
  to `source` unless metadata explicitly declares a behavioral tier; do not
  infer host/compile/runtime coverage from a source script. A source-only patch
  needs either a behavioral test or `test-exception` with both `reason` and a
  narrow `scope` naming the deferred behavioral owner.
- Prefer shared test helpers over ad hoc shell boilerplate. Static contract
  scripts should use a local `contract-test-lib.sh`; Darling guest C verdict
  tests should prefer `runner: guest-c-fixture` so `west test` owns upload,
  in-guest compilation, execution, timeout, verdict-marker checking, and prefix
  cleanup. Use a local `guest-verdict-test-lib.sh` only for corner cases the
  structured runner cannot express yet, and declare runtime prerequisites in
  patch metadata. Plain `runner: script` is an escape hatch for process/trace/
  runtime orchestration that the framework cannot express yet, not the default
  form for source-base contracts or profile-owned source scripts.
- Framework-internal contracts belong in small Python modules under
  `tests/west_test_contracts/`. Keep `tests/run-west-test-*-contract.sh` as
  thin compatibility entrypoints only; do not grow them with embedded Python
  heredocs or large inline fixtures unless the test is intentionally exercising
  shell/CLI integration.
- Do not use `python -m py_compile` for a working-tree syntax check: it leaves
  ignored `__pycache__` artifacts. Use
  `python3 -B scripts/check_python_syntax.py <paths...>` instead; it parses the
  files with `compile()` and leaves the tree clean.
- `tests/run-west-test-testkit-contract.sh` is the focused CLI contract for the
  local CTest/testkit bridge; update it when changing testkit registration or
  top-level CTest selectors.
- The E-UNION host CTest suite is source-bound and opt-in through
  `DARLING_ENABLE_EUNION_HOST_SUITE=ON`. Keep it disabled in the default
  testkit build; West enables it only in the separate `eunion-host` materialized
  source build, so guest runs do not compile against an unpatched checkout.
- `tests/run-west-test-add-compat-cmake-contract.sh` is the focused CMake
  contract for `add_compat_test()` command generation; update it when changing
  guest launch, argv, labels, or prefix environment behavior.
- `tests/run-west-test-guarded-timeout-contract.sh` is the focused guarded
  timeout contract for the CTest/debug-runner bridge; update it when changing
  `DARLING_TEST_EXECUTOR`, bundle-root propagation, or timeout wrapping.
- `tests/run-west-test-guest-command-contract.sh` is the focused behavioral
  contract for normalized `guest-command-fixture` execution; update it when
  changing guest command environment, timeout semantics, or result matching.
- `tests/run-darling-c-test-contract.sh` is the focused behavioral contract for
  source-driven guest CTest fixtures; update it when changing guest source
  transport, in-guest compilation, execution, or verdict matching. Source is
  sent by an explicit guest command into persistent `/private/var/tmp`, not by
  relying on `shell -c` stdin or a `/tmp` namespace surviving between stages.
- `tests/run-west-test-runtime-build-contract.sh` covers runtime source/build
  timeout forwarding; use `--runtime-build-timeout-seconds` to bound a
  diagnostic rebuild without changing normal profile deadlines.
- `west_commands/test_guest_c.py` owns metadata `guest-c-fixture` generation
  and execution. Keep `DarlingTest._run_guest_c_fixture()` as its thin West
  facade; do not move guest-C shell/diagnostic behavior back into `test.py`.
- `tests/run-west-test-gc-contract.sh` is the focused GC contract for debug
  bundles and stale runtime proof scratch dirs; update it when changing
  `west test --gc`, proof scratch naming, or dry-run pruning behavior.
- Treat `west test --gc` as unsafe for unattended use. The 2026-09-14 run deleted
  every non-timestamped directory under `darling-debug`, including another
  experiment root and the preserved CPack core, while its dry-run listed only
  timestamped west-test bundles - it selected bundles by age and count over every
  directory, so anything appearing after the plan was removed without ever being
  planned. That predicate is fixed and the plan now names the stale-worktree
  mutation the real run performs, so plan and effect agree on the three prune
  passes. Ownership is now required as well: the proof-scratch and guest-runner
  passes collect only marked scratch, job state carries its marker, and an
  unmarked name-match is reported instead of deleted - so a run reclaims less
  than it used to, deliberately, and says what it left. Snapshot the affected
  paths and copy required evidence outside the GC root before running it.
- One owned state root per task or lane. Set `DW_STATE_ROOT` and debug bundles,
  proof scratch, runtime evidence and `west dev` job state are created beneath it
  (`bundles/`, `scratch/`, `evidence/`, `jobs/`), so two concurrent lanes cannot
  write into each other's state. With a root declared, a maintenance pass refuses
  a `--bundle-root`, `--proof-scratch-root` or `--runtime-evidence-root` that
  points outside it, naming the root it refused to leave, because reaching
  another lane's state is silent until something is already gone. Undeclared
  keeps the previous defaults, so existing callers are unchanged.
- GC collects only what it can prove it owns, and a name is never ownership. A
  retained runtime-evidence unit is identified by its manifest, an in-flight one
  by its unit marker and its released flock, a published unit interrupted before
  its manifest by that same marker, a debug bundle by its timestamp name, and
  disposable scratch or job state by the `.west-test-scratch.json` marker its
  creator writes through `test_store.owned_scratch_dir` before filling it. A
  directory that merely shares one of those names is reported with its path and
  size and left alone, including an unowned guest-runner output. A manifest is
  written while its unit is still hidden and the rename that publishes it
  follows, so a published name always means complete. Removing a unit is
  announced before and after, since deleting a multi-gigabyte unit in silence is
  indistinguishable from a hang.
- `west test --gc --gc-runtime-evidence` collects `<topdir>/.west-test/runtime-evidence`
  unless `--runtime-evidence-root DIR` points it elsewhere. `--proof-scratch-root`
  scopes only the proof-scratch and guest-runner passes, so a run that looks
  scoped still collects the real evidence store: pass both when exercising a
  fixture, and use `--dry-run` to inspect either pass. Keep the only copy of
  acceptance evidence outside a GC-managed root.
- Never keep the only copy of acceptance evidence inside a GC-managed root such
  as `/home/ilyagulya/work/darling-debug`. Keep at least one copy outside it with
  a SHA-256 manifest. When evidence survives only as a hash or verdict record in
  a Bead or handoff, say so explicitly and re-run the gate instead of citing the
  destroyed archive as independently re-verifiable.
- `tests/run-west-patch-verify-contract.sh` is the focused behavioral contract
  for disposable worktrees used by `west patch verify`; update it when changing
  patch applicability, temporary-worktree cleanup, or Git maintenance policy.
- Run noisy or long `west test --prove-red` and full guest CTest selections
  through the supported job owner, not an output-limited foreground transport
  or bespoke background shell. Use the managed scenario when one exists;
  otherwise use `west-job start` followed by attached `follow`.
  Do not start a second prefix-backed run until the first has a final rc and
  prefix cleanup has completed. Observer timeout is not cancellation.
  `status` is a recovery snapshot, not an instruction to build a polling loop.
  In agent transport, use `follow` rather than the low-level `wait`; in an
  ordinary attached shell `wait` remains available. To reproduce one selected
  guest case with additional declared deployment providers, append
  `--with-runtime-profile NAME` for each; this changes deployment, not CTest
  selection.
- When creating Beads from a shell command, do not put unescaped backticks in
  `--description`: the shell treats them as command substitution. Use plain
  command text or safely single-quote/escape it, then verify the created ID.
- CTest `env=darling` entries are source-driven guest tests: `add_compat_test`
  must upload/compile/run the C source inside the selected Darling prefix via
  `testkit/scripts/run-darling-c-test.sh`. Do not run Linux host-built test
  binaries through `darling shell` and count that as guest coverage.
- Both CTest guest-C and metadata guest-C must route the low-level
  `launcher shell` transport through `testkit/scripts/darling-guest-shell.sh`.
  That helper owns the prefix environment and watchdog; metadata alone owns
  namespace retry, trace/stat resources, and diagnostic dumps around it.
- Darling guest tests should prefer `requires: [darling-prefix]` over
  `requires-env: [DPREFIX]`; let `west test --prefix/--prefix-profile` provide
  `DPREFIX`.
- Repetitive patch test metadata should use compact `test-profiles` and
`artifact-profiles` in `patches.yml` instead of restating the same
guest/runtime boilerplate in every test. Keep per-test entries focused on the
unique script, oracle, resources, and exceptional overrides.
- Shared `west test` runtime setup belongs in typed resource providers, not in
individual runner bodies. Add or extend `west_commands/test_resources.py` for
common resources such as host trace files, host stat deltas, DCC cache, or
E-UNION prefix setup, and cover provider ordering/selection with a focused
contract.
- A test declaration belongs to the profile whose patch chain satisfies its
assertions. `west test --profile <P>` loads tests only from
`patches/<P>/patches.yml`; `base-profile` governs tree composition, never test
declarations, so a stacked profile does not see its base profile's tests.
Attach a test to an entry that profile already owns rather than adding an entry
whose patch file lives in another profile.
- A profile resolves a patch entry to a file inside its own directory, and the
lock-first mapping locks the series the profile owns. An entry outside that
locked batch is invisible to the materializer while still breaking
`west patch apply`, `check` and `export`, so a declaration that needs one means
the series itself has to change, not the test.
- Register every contract runner: `tests/run-*-contract*.sh` in
`CONTRACTS` and `tests/west_test_contracts/*contract*.py` in
`EXPLICIT_CONTRACTS` of `ci/run-host-tier.py`, or list it in
`EXCLUDED_CONTRACTS` with a reason. The tier's census refuses to start when a
contract is accounted for nowhere, because a contract that nothing runs cannot
fail and therefore reports nothing.
- The census credits a contract only when it can see an invocation **by path**.
A name that appears only in a variable assignment, only on a line that passes a
probe flag, only in a comment, or only in a test-synthesized profile under
`patches/__*` does not mean the contract runs: a probe returns before the body
and a fixture is not a declaration. If the census reports a contract whose only
reference is one of those, the truthful answer is to register it, or to exclude
it with the reason it does not run - not to restore the mention.
- Register the contracts a chain drives, and keep the chain honest about the
rest. `tests/run-west-test-metadata-contract.sh` cannot complete without the
immutable mirror, so its eleven python contracts are registered individually in
`EXPLICIT_CONTRACTS` and the chain itself is excluded with that reason. A chain
that nothing runs is where contracts rot unseen; two of those eleven had.
- The identity that keeps a listing honest is the selection, not its spelling.
A metadata `ctest:` reference is pinned as the exact index the label resolved
to, so a test that greps the listing for `-L <label>` is pinning a command the
implementation does not produce. Compare what the two selections select.
- A contract that drives the tier must stub it, not execute it. Running the
host tier inside the host tier recurses, and driving its ~90 real contracts
makes the contract slow, load-sensitive and unregisterable. Stub the registered
contract paths in a throwaway mirror of the runner and assert the captured
command list: `tests/run-ci-test-tiers-contract.sh` is registered and runs in
about two seconds that way.
- The host tier schedules by weight, not by process count. A contract that
drives other runners holds more than one worker slot (`CONTRACT_WEIGHTS` in
`ci/run-host-tier.py`, each entry with its measured reason), because a flat
pool of `cpu_count` workers lets several of them oversubscribe the machine and
a load failure is indistinguishable from a regression. Never fix that class of
failure with a retry: a retry hides the failure this rule exists to surface.
- A profile-composition lock is derived, not hand-written. It records the tree
each module starts from and the tree every locked patch produces, and only a
replay of the locked series knows those values, so
`scripts/generate_profile_composition.py --check` is the review step and the
same command without `--check` reissues the file, naming the field that
disagreed. A series change stales the receipt of every profile whose chain
contains it - for the homebrew chain that is five files - and the materialization
refuses to run until they are reissued. The generator never reads a value out of
the checked-in lock, the materialization still validates against it, and a
style-only difference is reported as a tooling bug instead of being written over.
- `west patch export` refreshes the patch file and `patches.yml`. It does not
refresh the immutable lock, the migration receipt, or the profile-composition
binding, and it cannot: the lock needs a published immutable tag, and the
binding needs a replay. A series change is therefore complete only when all
four agree, which is what the migration-inventory and composition contracts and
the materialization check. The export now names the ones it left behind as soon
as it writes the artifact: the lock with the refspec that publishes the commit,
the receipt row with the counts it still records, and the derivation to re-run
with the closure of profiles it moves. It reports and never rewrites them - a
lock that cannot be published and a composition that cannot be replayed are
exactly the values that must not be guessed.
- Never pin a derived value in a contract. A tree the derivation computes, a
digest of a generated file, or a boundary the replay produces will move for
legitimate reasons, and a literal turns that into a regression report about the
ledger instead of about the code. Bind the value to its source - the file it is
derived from, the prerequisite it must agree with - and leave the value itself
to the gate that recomputes it: `--check` for a derived registry and
`--materialize-profile` for a profile composition. A stale receipt is what the
digest check catches; a literal only reports that the derivation ran.
- Name the supported command for the situation, not the tool. Five situations
  agents keep hand-rolling, and what to run instead:
  - An isolated authoring checkout: `west dev start --source SRC --destination
    DEST --base BASE --branch BRANCH --bead BEAD --module MODULE --evidence
    PATH`. It clones every repository in the gitlink closure, so `--dry-run`
    first if the source is large; it writes no discovery record yet, so record
    the destination yourself and remove it with `rm -rf DEST` when done.
  - Patch topology diagnosis - which module is out of state, which lock a patch
    belongs to: `west patch explain`, and `west patch preflight --repo CLONE
    --lock LOCK` for a lock's own evidence. Preflight judges the lock, its
    objects and its tags, not the checkout you are on, so it cannot see a module
    sitting on the wrong branch; `west patch apply` reports that, all of it.
  - Profile replay diagnosis: `west test --profile P --env ENV
    --materialize-profile` for the whole chain, `west patch materialize-lock
    --repo CLONE --lock SCHEMA_V2_LOCK` for one lock in isolation.
  - Where a guest run stopped (without grepping its two logs): `scripts/dwdiag progress --log RUN_LOG
  [--guest-log MLDR_DIAG_LOG] [--mode M]` prints whether the workload spoke, the guest loader's last stage, the op it
  published and the last plane op the server serviced; `dwdiag verdict` prints the same summary as `VERDICT-STAGE` on
  any non-PASS verdict.
- Job start and observation: `scripts/west-job.sh start --state-dir DIR -- …`
    then `follow --state-dir DIR`, `status --state-dir DIR`, `cancel
    --state-dir DIR`. `west dev follow DIR` is the same observer with the
    heartbeat off and the engine's flags forwarded. Do not hand-roll
    `follow | grep -v log-change-age | tail`; reconnect with `follow` instead.
  - Which instruments actually ran in a captured run: `darling-workspace/scripts/dwdiag witness [--log PATH]` prints a
    census of every registered instrument with counts and samples, then `WITNESS-SILENT <names>` for the ones that never
    spoke. Use it instead of grepping a run log by hand: a hand-written pattern finds only the line it was written for
    and cannot report that another instrument stayed silent, and both mistakes happened (a `SEM-SITE` line missed by its
    pattern; two `dserver-CRASH` lines read past as a transport stall).
  - State root ownership: `west dev status --json` reports the live job
    registry, temporary worktrees, source worktrees and the patch/doctor
    sections; `west dev context NAME --prefix PATH` selects the prefix a run
    uses; `<topdir>/.west-test/` holds the framework's own caches (runtime
    build, stock stack, verdicts).
  - Guest/runtime diagnostics: `scripts/dwdiag <symbolize|crash|verdict|suite>`
    -- one tool (Rust, vendored at `tools/darling-debug-runner`, built on first
    use) for the four questions this work asks constantly: resolve a reported
    offset to `symbol + offset` (`symbolize`), turn a `dserver-CRASH` line into
    a location plus a stack walk and the disassembly around the fault
    (`crash`), run ONE guest workload and judge it by its own machine-readable
    line (`verdict`), and run a SET of them into one table with the acceptance
    counters (`suite`). A verdict is PASS only when the workload's own
    `... pass=1` line is present; a missing line is FAIL/HANG and never PASS.
    Use `--json` to compose the result instead of scraping it, and do not
    hand-roll symbolizing, crash parsing or per-mode verdicts in shell.
- `west test` treats an empty selection as fatal, including a label filter that
matches nothing. Never narrow a selection to make a failing test disappear:
that converts a real signal into silence.
- The host tier sweeps both `homebrew` and `wget-residual` host metadata, so a
host test runs where its chain satisfies it instead of being gated away.
- The reviewed candidate audit anchors each entry to the source it was reviewed
against, so editing any anchored source invalidates it and the inventory
contract refuses with `candidate audit source SHA mismatch: <repo>:<path>`. Run
`scripts/generate_namespace_writer_registry.py --check` after such an edit and
regenerate before starting a tier: otherwise the sweep fails on the audit rather
than on the change, which costs a whole tier run to learn.
- The checked-in audit registries under `locks/patch-stack/` and `lifecycle/`
have no generator script; their contracts are the only thing that can notice
when they drift from the tree, so those contracts stay registered rather than
excluded.
- Runtime RED artifact planning belongs in `west_commands/test_runtime.py`.
Keep pure plan/display/target-mapping logic there with focused contracts; leave
only side-effecting build/deploy/restore orchestration in `west_commands/test.py`.
- Prefix lifecycle pure helpers belong in `west_commands/test_prefix.py`.
Process-tree matching and stale init-pid decisions should be tested there;
keep only side-effecting shutdown, mount cleanup, locking, and signal delivery
in `west_commands/test.py`.
- CTest command construction belongs in `west_commands/test_ctest.py`.
Keep label selector composition, list mode, and passthrough argument assembly
there with focused contracts; `west_commands/test.py` should orchestrate, not
hand-build CTest argv in multiple places.
- Source-repo CMake fixture execution belongs in `west_commands/test_cmake.py`.
Keep generated superproject shims, fallback CTest registration, compiler
launcher logging, and required compile-option checks there; `west_commands/test.py`
should only dispatch the invocation and pass reporter/executor context.
- For `runner: guest-command-fixture`, use `expect.returncode: any` only when
the Darling launcher cannot reliably propagate the guest program status for
the behavior under test. Pair it with a concrete guest-visible
`output-contains`/`output-lacks` oracle; do not use it to hide missing
  behavior or flaky exits.
- When validating a source change in `libsystem_kernel` against a real prefix,
  deploy dyld together with `libsystem_kernel.dylib`; dyld carries a static
  emulation path, so closure-only deploys can leave guest runtime tests running
  stale syscall behavior even when the dylib was rebuilt.
- `west test` owns the Darling prefix lifecycle for metadata tests whose
  compact form says `runs: guest` (expanded form: `requires:
  [darling-prefix]`): it takes `$DPREFIX/.west-test.lock`, runs the test, then
  runs `darling shutdown` for that prefix and kills a matching leftover
  `darlingserver` if needed. If prefix processes still remain after cleanup,
  the test run must fail. Use `--keep-prefix-running` only for deliberate fast
  local iteration.
- Patch metadata tests with `diag: guarded` or `diag: forensic` must run
  through `darling-debug-runner`; keep timeouts/capture in `west test`, not as
  unbounded bespoke shell around every guest test.
- Prefer compact CTest selectors for product tests registered in CMake/CTest:
  `ctest: <label>`, `runs: host|guest|macos`, `red-proof: source|runtime|self`,
  `build-target: <target>`, plus explicit `artifacts`, `resources`, and
  `fixtures`. `ctest-label`, `env`, `target`, and expanded `red-proof.mode`
  are supported expanded keys. Write manifests using the compact axes.
  Do not introduce `needs`; use `artifacts` for
  build/deploy outputs, `resources` for caches/oracles/external services, and
  `fixtures` for setup/cleanup state.
- For top-level product-suite selection, prefer `west test --submodule
  <west-project-path-or-name>` over spelling raw `submod:*` regexes by hand.
  Keep submodule label normalization in `west_commands/test_ctest.py`.
- Use `runner: python` for non-executable Python test files; do not use
  `command:` just to spell `python3 path/to/test.py`.
- `west patch export` must not create unrelated `patches.yml` formatting churn.
  Treat block-scalar/quoting rewrites as a tooling bug, not acceptable review
  noise.
- `west patch export` preflights all selected entries before writing patch files.
  If it reports stale `source-base`/`source-commit` metadata or suspiciously
  large output, repair the metadata/tooling first; use `--allow-large-output`
  only for a reviewed intentional large patch.
  Export also rejects committed generated evidence such as JSON snapshots,
  census captures, handoff notes, and build-output logs. Keep those in the
  diagnostic archive, not in a product patch; `west patch check --quality`
  reports existing violations and `west patch verify` refuses to apply them.
  Export and verify also reject `Co-Authored-By` trailers naming Claude
  or Codex; preserve real authorship and remove automation trailers from local
  commit messages before exporting a patch.
  Use `west patch export --profile <profile> --patch <path>` for focused
  checks/exports of a single profile entry; full-profile export remains the
  default when no patch selector is given.
- When moving or inserting entries in `patches.yml`, anchor edits on unique
  `- path:` blocks or use a structural edit and verify the resulting ordering.
  Do not insert after generic repeated keys such as `github:`/`upstream:`; patch
  order is semantic and must match each entry's `source-base`.
- For stacked runtime RED proofs, prefer materializing the selected profile
  minus the patch under test through `guest-runtime-deploy`. Keep the guest test blocked
  with the exact materialization conflict when that composition cannot run.
  A RED runtime must retain the profile's required boot/lifecycle dependencies;
  an incompatible server's startup failure is not regression proof.

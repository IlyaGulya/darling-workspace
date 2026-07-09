# Darling workspace

This repository contains private coordination state, not Darling source code.
It is the source of truth for workspace manifests, tasks, unpublished branch
refs, PR drafts, and agent handoff.

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
- `rtk find` intentionally rejects compound predicates/actions such as
  `-exec`; use `rtk proxy find ...` for those commands instead of retrying the
  same command shape.
- Use `west patch verify|apply|clean|list` for local integration profiles.
- Use `west darling-prefix-repair --prefix <prefix>` when guest tests report
  missing prefix prerequisites such as `private/var/tmp`, canonical
  `CommandLineTools`, or `DarlingCLT` clang links. Do not repair those by
  undocumented manual `mkdir`/`ln` sequences unless the command itself is what
  you are debugging.
- If a Darling guest run leaves mounted filesystems under a prefix, use
  `west darling-prefix-repair --prefix <prefix> --cleanup-mounts` after
  confirming no Darling processes are left. Do not ignore prefix mount tails;
  either clean them through tooling or keep the owning Bead open with the exact
  repro.
- Treat clean `fix/*` branches as canonical editable source.
- Treat patch files and `patches.yml` as portable integration artifacts.
- Treat `integration/*` branches and profile `west.lock.yml` files as generated.
- Never edit `integration/*`, open PRs from them, or edit generated locks.
- Never edit a patch without refreshing its full source SHA and checksum.
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
- Never add workspace metadata, PR drafts, agent state, or Beads files to the
  Darling source repositories.
- Do not push investigation branches unless explicitly requested.
- When using `apply_patch` from this workspace, use absolute paths for files
  outside the current working directory and verify new files with `git status`
  or `find` before running tests. A relative `tests/...` path from
  `/home/ilyagulya/work/darling-dev` creates files outside
  `darling-workspace`; treat that as a process bug and fix it immediately.
- For focused `darlingserver` validation, use
  `west darling-build --targets darlingserver --deploy --deploy-darlingserver`
  instead of the default broad build. The current command still deploys
  dyld/closure when `--deploy` is present; if that matters for a clean
  experiment, record the prefix/baseline state and prefer an explicit
  server-only deploy path or improve the command before claiming isolation.
- Patch-local red tests must be GREEN on the current checkout. `red: true`
  means the test is a RED->GREEN regression proof, not that latest should fail.
  Use `west test --profile ...` for the normal GREEN regression run and
  `west test --profile ... --prove-red` for explicit RED-proof mode. Do not
  fake source-base RED proofs with ad hoc shell; add shared runner support when
  a test needs current test assets executed against a bad/source-base tree.
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
  fake `source-base` proof for deployed guest behavior.
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
  `host`, `model`, or `source`. Any old-vs-fixed model must be explicit
  `coverage-tier: model`; source/text audits must be `coverage-tier: source`
  and must not be counted as behavioral coverage.
- Prefer shared test helpers over ad hoc shell boilerplate. Static contract
  scripts should use a local `contract-test-lib.sh`; Darling guest C verdict
  tests should prefer `runner: guest-c-fixture` so `west test` owns upload,
  in-guest compilation, execution, timeout, verdict-marker checking, and prefix
  cleanup. Use a local `guest-verdict-test-lib.sh` only for corner cases the
  structured runner cannot express yet, and declare runtime prerequisites in
  patch metadata.
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
- Runtime RED artifact planning belongs in `west_commands/test_runtime.py`.
Keep pure plan/display/target-mapping logic there with focused contracts; leave
only side-effecting build/deploy/restore orchestration in `west_commands/test.py`.
- For `runner: guest-command-fixture`, use `expect.returncode: any` only when
the Darling launcher cannot reliably propagate the guest program status for
the behavior under test. Pair it with a concrete guest-visible
`output-contains`/`output-lacks` oracle; do not use it to hide missing
  behavior or flaky exits.
- When validating a source change in `libsystem_kernel` against a real prefix,
  deploy dyld together with `libsystem_kernel.dylib`; dyld carries a static
  emulation path, so closure-only deploys can leave guest runtime tests running
  stale syscall behavior even when the dylib was rebuilt.
- `west test` owns the Darling prefix lifecycle for metadata tests that declare
  `requires: [darling-prefix]`: it takes `$DPREFIX/.west-test.lock`, runs the
  test, then runs `darling shutdown` for that prefix and kills a matching
  leftover `darlingserver` if needed. If prefix processes still remain after
  cleanup, the test run must fail. Use `--keep-prefix-running` only for
  deliberate fast local iteration.
- Patch metadata tests with `diag: guarded` or `diag: forensic` must run
  through `darling-debug-runner`; keep timeouts/capture in `west test`, not as
  unbounded bespoke shell around every guest test.
- Prefer `ctest-label` for tests registered in the compatibility suite. It is a
  runnable selector, not documentation; `west test` builds the suite and runs
  `ctest -L <label>`.
- Use `runner: python` for non-executable Python test files; do not use
  `command:` just to spell `python3 path/to/test.py`.
- `west patch export` must not create unrelated `patches.yml` formatting churn.
  Treat block-scalar/quoting rewrites as a tooling bug, not acceptable review
  noise.
- `west patch export` preflights all selected entries before writing patch files.
  If it reports stale `source-base`/`source-commit` metadata or suspiciously
  large output, repair the metadata/tooling first; use `--allow-large-output`
  only for a reviewed intentional large patch.
- When moving or inserting entries in `patches.yml`, anchor edits on unique
  `- path:` blocks or use a structural script and verify ordering with `rg`.
  Do not insert after generic repeated keys such as `github:`/`upstream:`; patch
  order is semantic and must match each entry's `source-base`.
- For stacked runtime RED proofs, do not accept a historical `source-commit^`
  bad runtime when later patches in the same module can change boot/lifecycle
  behavior. Prefer a `guest-runtime-deploy` proof that materializes the current
  profile minus the patch under test, or leave the guest test blocked with the
  exact materialization conflict. A RED failure from an old, incompatible server
  is not a valid regression proof.

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
- Use `west dw beads ...` for issue operations.
- Use `west patch verify|apply|clean|list` for local integration profiles.
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
- Do not close patch coverage with source matching. Tests that grep, parse, or
  assert that specific code text exists are audit checks only; they must not be
  counted as the patch's behavioral test and must not be recorded as `kind:
  contract` to make `west patch check` show TESTED. A patch is covered only by
  a test that executes behavior: guest/runtime test, C/host fixture, model test
  that drives the state machine, generator test that runs the generator and
  validates generated behavior, or a build/compile/link test that exercises the
  changed build contract. If behavior cannot yet be executed, leave the patch
  MISSING/SOURCE and create a task instead of faking coverage.
- Prefer shared test helpers over ad hoc shell boilerplate. Static contract
  scripts should use a local `contract-test-lib.sh`; Darling guest C verdict
  tests should use a local `guest-verdict-test-lib.sh` and declare runtime
  prerequisites in patch metadata.
- Darling guest tests should prefer `requires: [darling-prefix]` over
  `requires-env: [DPREFIX]`; let `west test --prefix/--prefix-profile` provide
  `DPREFIX`.
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

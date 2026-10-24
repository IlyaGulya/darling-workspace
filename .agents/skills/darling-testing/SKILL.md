---
name: darling-testing
description: >-
  Select, author, register, or interpret Darling test evidence. Use before
  changing test metadata, runners, CMake/CTest registration, RED/GREEN proofs,
  or claiming behavioral coverage, runtime smoke, or source-build acceptance.
---

# Darling testing: execute the contract, report the evidence

Canonical source: `.agents/skills/darling-testing/SKILL.md`; `.claude/skills`
is only a relative alias. Run workspace commands from the manifest with
`mise run west ...` or `mise run dw ...`; prepend `$HOME/.local/bin` to PATH
when needed. Read the relevant reference and current command help first.
This skill does not authorize unrequested tests/builds. Evidence requirements
below are policy, not a promise that every runner enforces them.

## Workflow

1. **Select the owned contract.** Identify the changed behavior and owning
   source project. Prefer existing source-owned CTest cases; use West's
   `--bead`, `--submodule`, `--env`, `--diag` and `--label` selectors as applicable.
   `--changed` is a local hint, not complete coverage. Resolve scoped cases and
   actual environment variants, not incidental argv spellings or temporary CTest
   indices. `--list` is discovery, not execution: it may configure CMake and
   create temporary source worktrees. Declare runtime prerequisites separately;
   adding a runtime provider does not select tests. Empty/inapplicable selections
   and formal skips establish no coverage; never narrow away a failure.
   See [selection authority](../../../docs/test-infra.md#authority-identity-and-selection)
   and [CTest selection](../../../docs/test-infra.md#ctest-selection-bridge).
2. **Choose evidence honestly.** Classify actual execution as `host`, `compile`,
   `model` or `runtime`; `coverage-tier` expresses that classification in metadata,
   not an automatically proven strength. Source/text matching is only `source`
   audit (`kind: source-contract`), never behavioral coverage. Explicit bad/fixed
   models are `model`. Execute the changed contract and workload smoke/oracle,
   not merely setup. For build fixes, exercise the changed CMake target/path
   without injecting the fix through test metadata. If blocked, report source-only
   evidence and a blocking task naming the deferred behavioral owner.
   See [evidence and ownership](../../../docs/test-infra.md#behavioral-evidence-and-proof-ownership).
3. **Keep declarations and assets with their owner.** Product fixtures and
   CMake/CTest registrations stay in the source project; workspace assets stay
   in the manifest. Preserve CTest's arguments, environment, resources, fixtures,
   working directory, timeout and verdict instead of duplicating them in a
   runner. Prefer existing structured guest C/command/argv runners and typed
   resources over ad-hoc shell setup. Keep current test inputs available
   independently of old source; the same intended contract must reach both
   RED and GREEN. A filename alone does not prove equivalent scenarios.
   See [selection authority](../../../docs/test-infra.md#authority-identity-and-selection)
   and [CMake/CTest registration](../../../docs/test-infra.md#compatibility-test-registration).
   Xcode build scenarios use the shared license setup in
   `scripts/xcodebuild-workload-run.sh`: with user-authorized acceptance it checks
   the selected Xcode, accepts when necessary through Darling's bundled guest
   `sudo`, and verifies again as the ordinary guest user. Never use host `sudo`.
   Preserve its separate phase logs; do not replace acceptance with plist/receipt
   writes or count license readiness as build success. See
   [Xcode setup](../../../docs/test-infra.md#xcode-build-workload-license-setup).
4. **Make RED truthful.** Current-source regression runs must be GREEN.
   RED/GREEN needs the same behavioral oracle against known-bad and fixed
   implementations, failing for the intended reason. Source-base proof redirects
   a structured runner to bad/fixed source; it does not change a deployed runtime,
   and guest-C source-base proof is explicitly rejected. Runtime proof needs an
   actual bad/fixed build/deploy transaction, declared artifacts/dependencies and
   rollback/cleanup, not a checkout swap. Preserve boot/lifecycle compatibility
   and record nondefault build defines and paired source modules.
   Existing `--prove-red` support is metadata-bound, not a general comparison
   command for arbitrary source-owned CTest cases; do not recreate retired
   authoring workflows to obtain it. Use a direct behavioral harness or report
   the exact unsupported proof/blocker. Match the failure phase and domain
   symptom, never shellspawn/protocol/setup failure before fixture `main`.
   Provider-build regressions require an explicit provider-under-test oracle;
   download/cache/setup failures are not provider RED. A self-contained model
   remains model evidence, not deployed-runtime proof.
   See [evidence and ownership](../../../docs/test-infra.md#behavioral-evidence-and-proof-ownership)
   and [proof modes](../../../docs/test-infra.md#test-metadata-and-runners).
5. **Own runtime execution.** Before guest launch read `skill://darling-boot`;
   before deployment identity/tracing read `skill://darling-diagnostics`.
   Bootstrap absent/empty prefixes separately: path selection is not provisioning.
   Guest C compiles/runs in Darling, not as a Linux binary through a launcher.
   Verify the runtime that served the run, including dyld for real-prefix
   libsystem_kernel validation. Run long commands through `scripts/west-job.sh
   start` with attached `follow`; use a named dev scenario only when its dry-run
   resolves to the intended manifest-native route, not legacy authoring metadata.
   Keep one lifecycle owner/run per prefix through final rc AND cleanup; observer
   timeout is not cancellation. Use the owner's cancel path and separate lanes
   for concurrent experiments.
   Require the actual diagnostic tier: metadata guarded/forensic execution fails
   without a debug runner, but `add_compat_test()` currently warns and relabels
   such cases `bare` when no executor is configured. Check resolved labels and
   execution evidence; a requested tier is not guaranteed protection. Leftover
   owned processes/mounts prevent a clean PASS.
   See [operator entrypoints](../../../docs/test-infra.md#local-operator-entrypoints),
   [bootstrap](../../../docs/test-infra.md#bootstrap-a-fresh-prefix-before-a-prefix-backed-run)
   and [diagnostic tiers](../../../docs/test-infra.md#diagnostic-execution-tiers).
6. **Register the proof and maintain its owner.** Update the focused contract
   for the changed interface; keep helpers in their owning `test_*` module and
   `test.py` orchestration thin. Register shell/Python contracts in
   `ci/run-host-tier.py`'s census or exclude with a reason. Its static accounting
   is not an execution verdict: comments, assignments, probes and generated
   declarations do not execute a contract. Tier-wiring tests stub runners rather
   than recursively executing the tier. Preserve `CONTRACT_WEIGHTS` scheduling;
   fix oversubscription instead of hiding load failures with retries. Compare
   resolved selections/derived inputs, not literal generated digests.
   See [focused contracts](../../../docs/test-infra.md#framework-ownership-and-focused-contracts),
   [census](../../../docs/test-infra.md#what-the-contract-census-credits) and
   [scheduling](../../../docs/test-infra.md#scheduling-the-host-tier-by-weight).
7. **Separate iteration from acceptance.** Preserve each cache's exact identity,
   completion and integrity gates. Fresh acceptance executes the current identity:
   set `WEST_TEST_VERDICT_CACHE=off` for metadata guest verdict reuse and inspect
   other runner/tier caches separately. Only completed zero verdicts are reusable;
   report their identity and original run time, never call them fresh execution.
   The metadata runner excludes RED proofs and `homebrew-lz4` consumers from
   verdict reuse; this is not a universal fresh-source-build guarantee.
   When measuring a source build, disable stock restore with
   `WEST_STOCK_STACK_RESTORE=off` and do not use a guest compiler cache. Snapshot
   receipts do not prove fresh compilation. Runtime build caches/host ccache may
   prepare the runtime, but a reused build is not fresh-build evidence; the verdict
   switch does not disable those caches or stock restores.
   Stock wget-repeat acceptance is twelve source reinstalls, each with its oracle.
   Shortened runs and smaller load-smoke workloads are iteration/regression signals,
   not full acceptance. Report preparation, native-tools preflight, exact diagnostic
   and full source-build acceptance separately.
   See [cache and acceptance rules](../../../docs/test-infra.md#cache-identity-and-fresh-acceptance)
   and [Homebrew prerequisites](../../../docs/test-infra.md#reproducible-homebrew-build-prerequisites).

## Completion checks

- Name executed selections/cases, actual evidence tier, RED reason/GREEN result
  where applicable, source/runtime/deployment identity, workload counts and
  fresh/cache status. Never claim an unexecuted, skipped or blocked gate passed.
  Native/hosted claims require their own execution, not local Linux evidence.
- Confirm owned inputs, focused contracts and census registration cover the
  changed interface. Run cleanup/leak assertions only as a final separate phase
  after every materializing command exits; final rc alone is not cleanup evidence.
- Use owned durable state roots. Before GC inspect its `--dry-run` and affected
  paths; never run it unattended or manually wipe registered cache worktrees.
  `west test --dry-run` is GC-only, not a test execution preview. Ordinary GC
  preserves failed runtime evidence; collecting it requires `--gc-runtime-evidence`.
  Scope both `--proof-scratch-root` and `--runtime-evidence-root` for GC fixtures;
  one does not scope the other. Preserve acceptance bytes outside GC roots with
  SHA-256 hashes; a surviving digest/verdict cannot replace the missing archive.
  See [GC scope](../../../docs/test-infra.md#diagnostic-execution-tiers) and
  [state ownership](../../../docs/environment.md#durable-state-and-cleanup).
- Before handoff follow `skill://darling-durability`. Record exact blockers and
  remaining owned runtime/job state rather than upgrading smoke, models, audits,
  cache hits or partial diagnostics into acceptance.

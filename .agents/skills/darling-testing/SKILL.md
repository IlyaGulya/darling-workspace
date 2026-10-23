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
`mise run west ...` or `mise run dw ...` (use `$HOME/.local/bin/mise` when not
on PATH). This skill does not authorize unrequested tests/builds.

## Workflow

1. **Choose evidence before running.** Identify the changed behavior and its
   owning source project. Select `coverage-tier: host`, `compile`,
   `model`, or `runtime` honestly; source/text matching is only `source` audit,
   never behavioral coverage. Explicit bad/fixed models are `model`. Execute
   the actual contract and workload smoke/oracle, not merely successful setup.
   For build fixes, use the changed CMake target/path without injecting the fix
   through metadata. If execution is blocked, report source-only evidence and
   a blocking task naming the deferred behavioral owner.
   See [evidence and ownership](../../../docs/test-infra.md#behavioral-evidence-and-proof-ownership).
2. **Keep assets and declarations with their owner.** Product CMake/CTest
   fixtures stay colocated with the source project; workspace test assets stay
   in the manifest. Prefer CTest registrations and typed artifact, resource and
   fixture declarations over runner-local setup. Use structured runners and
   keep test inputs available independently of the old source being exercised.
   The same intended input must reach RED and GREEN.
   See [evidence and ownership](../../../docs/test-infra.md#behavioral-evidence-and-proof-ownership)
   and [CMake/CTest registration](../../../docs/test-infra.md#compatibility-test-registration).
3. **Make RED truthful.** Current-source regression runs must be GREEN. Claim
   RED/GREEN only when the same behavioral oracle executes against known-bad
   and fixed implementations and fails for the intended reason on the bad one.
   An explicit bad/fixed model is model evidence, not deployed runtime evidence.
   Runtime proof requires declared minimal artifacts, bad build/deploy,
   backup/restore, and the same fixture on the fixed runtime—not a checkout swap
   or an infrastructure failure. Preserve required boot/lifecycle dependencies.
   Do not broaden matchers to accept shellspawn/protocol failures before fixture
   `main`; use a direct behavioral harness or record the exact blocker. Declare
   nondefault build defines and required paired source modules.
   See [evidence and ownership](../../../docs/test-infra.md#behavioral-evidence-and-proof-ownership).
4. **Own runtime execution.** Before any guest launch, read
   `skill://darling-boot`; before deployment identity/tracing, read
   `skill://darling-diagnostics`. Bootstrap absent/empty prefixes separately;
   path selection is not provisioning. Guest C compiles in Darling, not on
   Linux. Verify the deployed runtime that served the run, including dyld with
   real-prefix libsystem_kernel validation. Use managed scenarios or supervised
   `west-job start` plus attached `follow`, one run per prefix until final rc
   and cleanup. Timeout of the observer is not cancellation. Guarded/forensic
   runs require the debug runner; leftover processes/mounts fail the run.
   See [operator entrypoints](../../../docs/test-infra.md#local-operator-entrypoints)
   and [bootstrap](../../../docs/test-infra.md#bootstrap-a-fresh-prefix-before-a-prefix-backed-run).
5. **Register the proof and maintain its owner.** Update the focused contract
   for the interface changed; keep pure helpers in their owning `test_*` module
   and `test.py` orchestration thin. Register shell/Python contracts in the host
   census or exclude with a reason. A comment, probe, assignment or generated
   declaration alone is not execution. Tier-wiring tests stub runners, never recursively
   execute the tier; preserve weighted scheduling instead of hiding load failures
   with retries. Compare resolved selections/derived inputs, not incidental argv
   spellings or literal generated digests.
   See [ownership and focused contracts](../../../docs/test-infra.md#framework-ownership-and-focused-contracts),
   [census](../../../docs/test-infra.md#what-the-contract-census-credits), and
   [scheduling](../../../docs/test-infra.md#scheduling-the-host-tier-by-weight).
6. **Separate iteration from acceptance.** Preserve exact cache identity and
   completion/integrity gates. Fresh acceptance disables verdict reuse and
   executes the current identity; source-build measurements never reuse verdicts
   or substitute stock snapshots/guest compile caches for the measured build.
   Stock wget-repeat acceptance is twelve source reinstalls; shortened runs and
   smaller load-smoke workloads are iteration/regression signals only. Report preparation,
   native-tools preflight, exact diagnostic, and full source-build acceptance
   separately. See [cache and acceptance rules](../../../docs/test-infra.md#cache-identity-and-fresh-acceptance)
   and [Homebrew prerequisites](../../../docs/test-infra.md#reproducible-homebrew-build-prerequisites).

## Completion checks

- Name executed selections, actual evidence tier, RED failure reason/GREEN
  result, runtime/deployment identity, workload counts and fresh/cache status;
  never claim an unexecuted or blocked gate passed. Do not narrow away failures.
- Confirm test inputs, source ownership, focused contracts and host
  census registration cover the changed interface. Native/hosted claims require
  their own execution, not local Linux evidence.
- Run cleanup/leak assertions only as a final separate phase after all
  materializing test commands exit. Preserve acceptance bytes outside GC roots
  with hashes; a missing archive cannot be cited as independently verifiable.
  Use owned durable state roots and framework GC, never manually wipe cached
  worktrees. See [diagnostic execution and GC](../../../docs/test-infra.md#diagnostic-execution-tiers).
- Before handing off changes, follow `skill://darling-durability`. Record exact
  blockers and remaining owned runtime/job state rather than upgrading smoke,
  model, source audits, cache hits, or partial diagnostics into acceptance.

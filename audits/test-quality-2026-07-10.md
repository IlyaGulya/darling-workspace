# Test Quality Audit - 2026-07-10

Scope:
- `patches/homebrew/patches.yml`
- `patches/arch/patches.yml`
- normalized through `west_commands.test_manifest.load_test_profile`

Current raw metadata inventory after the structured runner migrations:
- 146 test rows total: 132 homebrew, 14 arch
- runners: 52 `guest-c-fixture`, 35 `c-fixture`, 21 `source-contract-script`, 10 `source-script-fixture`, 9 `self-contract-script`, 4 `guest-runtime-script`, 4 `object-symbol-fixture`, 3 `source-profile-script`, 3 `python`, 2 `west-build`, 1 `source-build-fixture`, 1 `cmake-configure-fixture`, and 1 ctest-label shorthand row
- env: recalculated by `west patch check` as behavioral coverage: homebrew 78 covered, arch 13 covered
- red proof modes: 56 `source-base`, 22 `guest-runtime-deploy`, 10 `self`, 58 non-red/no proof rows

Implemented quality gates in this pass:
- `west patch check --quality`
- `west patch check --strict-quality`

The first low-noise rules are intentionally narrow:
- XNU `guest-runtime-deploy` proofs that build `system_kernel` must materialize `darling/src/external/darlingserver` as a source module, because libsystem_kernel consumes RPC-generated headers/hooks from darlingserver.
- Non-dyld-scoped runtime proofs should not deploy dyld; broad unrelated runtime artifacts can make RED fail for the wrong reason.

Fixed by this audit:
- Added `red-proof.source-modules: [darling/src/external/darlingserver]` to the remaining XNU `system_kernel` runtime proofs that lacked it:
  - `xnu/select-pselect-fdset.patch`
  - `xnu/fork-wait-timeout-parent.patch`
  - `xnu/eunion-hardening.patch` (the former mkdir-upper-create slice)
- Removed unrelated dyld runtime artifacts from non-dyld proofs:
  - `xnu/psynch-cvsignal-args.patch`
  - `xnu/eunion-hardening.patch` (the former mkdir-upper-create slice)
- Documented the runtime source-forest rule in `AGENTS.md` and `docs/test-infra.md`.
- Added `runner: source-contract-script` for workspace-hosted shell contracts
  that execute fixed test assets against a source tree selected by
  `source-env`. This separates source-base contract tests from generic
  `runner: script` escape hatches.
- Migrated the repeated E-UNION host suite metadata from generic `script` to
  `source-contract-script` for the 20 workspace-hosted source-base contracts.
  The runner semantics did not change; the metadata now exposes the domain
  intent and lets future quality checks distinguish it from ad hoc shell.
- Migrated 9 `arch` host contract scripts that already live in source trees to
  `source-script-fixture`. While doing that, fixed `source-script-fixture` to
  execute executable scripts directly through their shebang instead of forcing
  `/bin/sh`; this is required for source contracts that use bash features such
  as `set -o pipefail`.
- Added `runner: self-contract-script` for host scripts whose RED proof is
  self-contained in the script, and migrated 9 homebrew self-proof shell
  contracts to it. This keeps the existing execution model but removes another
  group from the generic script escape hatch.
- Migrated one arch source-base shell contract to `source-contract-script`,
  made the first E-UNION guest C fixture explicitly `guest-c-fixture`, and
  added `runner: guest-runtime-script` for the remaining guest/runtime
  orchestration scripts that cannot yet be expressed as `guest-c-fixture`.
- Added `runner: source-profile-script` for source-base shell contracts whose
  test script is introduced by the patch/profile itself. This materializes the
  GREEN profile source tree first, runs the profile-owned test asset from that
  tree, and points `red-proof.source-env` at the bad/source-base tree for RED.
  The final three generic main `runner: script` entries were migrated:
  `darlingserver/psynch-cvwait-balanced.patch`,
  `xnu/psynch-cvsignal-args.patch`, and
  `xnu/psynch-negative-errno.patch`.
- Reviewed the two runtime-sensitive compile-only XNU patches:
  - `xnu/fork-postfork-child.patch`: keep as compile-tier. The invariant is
    exact production `sys_fork()` child-path ordering (`__mldr_postfork_child`
    before guard/socket refresh/checkin, and no child hooks on parent/error
    paths). A guest smoke test would only observe indirect fork lifecycle
    symptoms and would not prove the call-order regression more directly.
    Broader deployed fork lifecycle validation belongs to dar-q95.29.9 /
    dar-r7fc.2.1.
  - `xnu/sigexc-debug-flood.patch`: keep as object-symbol compile-tier. The
    regression is the default object depending on `__simple_kprintf`; the
    contract proves default builds lack that dependency while `-DDEBUG_SIGEXC`
    still enables it. A guest log/perf gate would be a noisy indirect oracle;
    deployed kprintf/log-flood runtime coverage is tracked by
    dar-sigexc-hotpath-kprintf-runtime-gate-9mpv.

Current automated result:
- `west patch check --profile homebrew --quality --strict-quality`: no quality warnings
- `west patch check --profile arch --quality --strict-quality`: no quality warnings
- `tests/run-west-test-metadata-contract.sh`: includes a synthetic bad profile proving `--strict-quality` rejects an XNU runtime proof that omits materialized darlingserver, a `source-contract-script` probe proving the runner receives `source-env`, and a real `source-profile-script` RED/GREEN proof where the profile-owned script is absent from the live checkout.
- Focused E-UNION proof: `west test --profile homebrew --patch xnu/eunion-core.patch --prove-red` fails on the bad XNU source tree, then passes the GREEN materialized E-UNION suite (`228 tests, 0 failed`).
- `west test --profile arch --env host`: passes after the arch source-script
  migration and shebang fix.

Remaining quality debt:
- Main patch metadata in `homebrew` and `arch` now has 0 generic
  `runner: script` rows. Five nested runtime `red-runner` entries still use
  `runner: script` as explicit dserverdbg/runtime oracles; these are runtime
  escape hatches, not source-base contracts.
- No additional guest tests were added for `xnu/fork-postfork-child.patch` or
  `xnu/sigexc-debug-flood.patch` because their current compile/object contracts
  are the direct RED proof. Add deployed guest coverage only when the broader
  fork lifecycle or kprintf/log-flood tasks provide a stable runtime oracle that
  fails for the intended reason.

Not treated as current blockers:
- Existing source-contract tests are source-tier and do not count as behavioral coverage.
- `arch` has only one runtime-tier test and many host/script contracts, but that profile contains architectural/local tooling work where the next step is targeted migration, not a safe blanket rewrite.

# Test Quality Audit - 2026-07-10

Scope:
- `patches/homebrew/patches.yml`
- `patches/arch/patches.yml`
- normalized through `west_commands.test_manifest.load_test_profile`

Current normalized inventory:
- 152 test rows total: 135 homebrew, 17 arch
- runners: 52 `guest-c-fixture`, 46 `script`, 35 `c-fixture`, 4 `object-symbol-fixture`, 3 `python`, 2 `west-build`, 1 each `darling-cmake-target-fixture`, `source-build-fixture`, `source-script-fixture`, `cmake-configure-fixture`
- env: 90 host, 56 darling, 6 unspecified/list-only style rows
- red proof modes: 57 `source-base`, 22 `guest-runtime-deploy`, 10 `self`, 57 non-red/no proof rows

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
  - `xnu/eunion-24-mkdir-upper-create.patch`
- Removed unrelated dyld runtime artifacts from non-dyld proofs:
  - `xnu/psynch-cvsignal-args.patch`
  - `xnu/eunion-24-mkdir-upper-create.patch`
- Documented the runtime source-forest rule in `AGENTS.md` and `docs/test-infra.md`.

Current automated result:
- `west patch check --profile homebrew --quality --strict-quality`: no quality warnings
- `west patch check --profile arch --quality`: no quality warnings
- `tests/run-west-test-metadata-contract.sh`: includes a synthetic bad profile proving `--strict-quality` rejects an XNU runtime proof that omits materialized darlingserver.

Remaining quality debt:
- 46 tests still use `runner: script`. Some are acceptable thin wrappers, but this remains the largest source of ad hoc behavior. They should be migrated class-by-class into structured runners/profiles or explicitly documented as script-only exceptions.
  Tracked as `dar-test-infra-sp5.11.15`.
- Two XNU patches remain runtime-sensitive but compile-only in current metadata:
  - `xnu/fork-postfork-child.patch`
  - `xnu/sigexc-debug-flood.patch`
  These may be defensible as narrow compile/build contracts, but they need explicit review. If the behavior can be guest-tested without making the proof flaky or artificial, add guest coverage. Otherwise document why compile evidence is the correct tier.
  Tracked as `dar-test-infra-sp5.11.16`.

Not treated as current blockers:
- Existing source-contract tests are source-tier and do not count as behavioral coverage.
- `arch` has only one runtime-tier test and many host/script contracts, but that profile contains architectural/local tooling work where the next step is targeted migration, not a safe blanket rewrite.

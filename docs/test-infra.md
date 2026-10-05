# Darling test infrastructure

This guide defines workspace test APIs, ownership, evidence requirements and
operator commands. Record execution results, incidents and task status in Beads
and diagnostic archives.

## Local operator entrypoints

For ordinary Homebrew preparation, native-tools preflight, source-build attempts
and guest exact-capture diagnostics, use the named `mise run dw dev context` /
`mise run dw dev run` workflow in [Reproducible Homebrew build prerequisites](#reproducible-homebrew-build-prerequisites).
Run commands from the manifest directory with the `mise run dw ...` or
`mise run west ...` proxy task. Both tasks select the pinned environment and
forward arguments unchanged. From the workspace root use
`mise -C darling-workspace run dw ...` or `run west ...`.
Bare West names in prose identify underlying APIs.

The lower-level selectors, metadata examples and CI helpers below are
authoritative for framework work and advanced scenarios. For long prefix-backed
agent runs outside the four named scenarios, start one recorded job and attach
its observer:

```sh
scripts/west-job.sh start --state-dir /absolute/path/to/new-job -- \
  mise run west test --profile wget-residual \
  --patch xnu/posix-spawn-failure-ownership.patch --env darling \
  --prefix /absolute/path/to/prepared-prefix --prove-red
scripts/west-job.sh follow --state-dir /absolute/path/to/new-job
# Reconnect with follow; request owner cleanup when cancellation is needed:
scripts/west-job.sh cancel --state-dir /absolute/path/to/new-job
```

Use a new job state directory for each run. Do not use log silence as proof of a
hang or kill launchd/server processes by guessed PID or broad name patterns.
Do not overwrite or delete runtime binaries while the prefix is live; use
West's owning shutdown/deployment transaction and retain its failure evidence.

### Bootstrap a fresh prefix before a prefix-backed run

`--prefix PATH` only resolves a path; it does not create, boot or provision a
prefix. A selection that needs a prefix therefore runs against a directory that
is not usable until the bootstrap-only provider has run, and `west test` now
names that exact command instead of leaving the operator to find it from a
downstream failure. The recipe is two steps, and the first one is its own
invocation:

```sh
mise run west test --prefix /absolute/path/to/owned-prefix \
  --bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal
mise run west test --prefix /absolute/path/to/owned-prefix \
  --bootstrap-runtime-profile homebrew-guest-toolchain-provisioning
mise run west test --prefix /absolute/path/to/owned-prefix --profile wget-residual \
  --patch xnu/posix-spawn-failure-ownership.patch --env darling
```

The diagnostic prints the same command in bare West form (`west test --prefix
... --bootstrap-runtime-profile ...`); the `mise run west ...` proxy forwards its
arguments unchanged.

`--bootstrap-runtime-profile` is prefix provisioning, not a test selection. It
is refused together with `--profile`/`--patch`, with the CTest selectors
(`--bead`, `--label`, `--submodule`, `--changed`, `--fuzz`, `--stress`), and with
`--list`, `--with-runtime-profile`, `--reuse-prefix-runtime` and raw CTest
passthrough arguments. It runs `darling-doctor --scope workspace`, builds and
deploys the declared provider, proves it with a bounded guest smoke and the
runtime doctor scope, and only then retains it. The two bootstrap-capable
profiles differ in what they provision:

| profile | provisions |
| --- | --- |
| `homebrew-rootless-bootstrap-minimal` | the rootless E-UNION baseline, including the launcher at `<prefix>/bin/darling`, and no guest CommandLineTools |
| `homebrew-guest-toolchain-provisioning` | the same baseline plus the reviewed guest CommandLineTools package set, installed through the guest `installer` |

Both write the retained provider marker `<prefix>/.west-runtime-profile.json`
(schema, profile, source profile, guest toolchain, launcher/build fingerprint),
which `--reuse-prefix-runtime`, the retained-prefix identity checks and the
stock-stack and verdict caches verify. A bootstrapped prefix is distinguishable
from a fresh one without running anything:

- `<prefix>/bin/darling` exists; a fresh prefix has no launcher, and an explicit
  `--prefix` never falls back to another prefix's launcher.
- `<prefix>/.west-runtime-profile.json` names the provider that provisioned the
  prefix. A missing marker means "never bootstrapped"; a marker for a different
  profile or fingerprint means the prefix was provisioned for other work.
- only the provisioning profile leaves `<prefix>/.west-command-line-tools.json`
  and the canonical `Library/Developer/CommandLineTools/usr/bin/clang`. A guest C
  fixture on a baseline-only prefix fails its guest-compiler prerequisite check
  and is told to bootstrap with the provisioning profile.

Use a short, owned prefix that is absent or empty. Bootstrap creates a missing
prefix root under its lifecycle lock, but it does not relax the admission checks
for a populated or mode-mismatched prefix, and a provider that stages a stock
Homebrew installation refuses a prefix that already holds one.
`--prefix-profile homebrew` is a shortcut to one prepared prefix, not a
bootstrap.

### Direct bootstrap from an explicit build directory

`west darling-bootstrap` is the West-native path: the source state is whatever
West has checked out, the build variant is whatever `--build-dir` was configured
with, and the command turns that build into a working prefix without looking up
a runtime profile, a source profile, a patch, a lock or a materialized forest.

```sh
mise run west darling-bootstrap --prefix /absolute/path/to/owned-prefix \
  --build-dir /absolute/path/to/build-dir
```

It asserts that the build directory's own `CMakeCache.txt` carries the plan's
configured defines and refuses a build directory for a different variant,
naming the entry and printing the `cmake -S ... -B ...` line that would produce
this one. It then builds the plan's targets, deploys through the same
`RuntimeDeploymentService` transaction the profile bootstrap uses (typed mode
marker, CMake component manifests, Mach-O closure resolution, the same
deployment receipt), and runs one guest smoke inside that deployment, so the
deployment and its receipt are retained only if the smoke passes. The doctor
split is the usual one: `--scope workspace` before build/deploy, `--scope
runtime` after the guest smoke, followed by an explicit receipt check.

The plan is `testkit/darling-bootstrap.yml`, and it is not a runtime profile: it
declares the build targets, the expected cmake defines, the typed runtime mode,
the launcher environment and one component per closure resource, and it selects
no source revision. A plan that names `source-mode`, `source-profile`,
`source-modules`, `patch`, `lock`, `revision` or `materialize` is refused by
name, and `tests/run-darling-bootstrap-contract.sh` pins that refusal, the
malformed-plan refusals, the define gate and the fact that the plan's Ring
defines are exactly the values the manifest-native Ring provider declares.

`--bootstrap-runtime-profile` remains the profile-coupled path and the way the
`guest-toolchain-provisioning` profile installs the reviewed guest
CommandLineTools; the direct entrypoint covers the runtime closure, which is
what a prefix needs in order to boot.

## CI execution contract

`.github/workflows/test-infra.yml` keeps privilege and trust boundaries explicit:

- host tests run on pull requests, pushes, schedule, and the selected manual tier;
- pull requests, pushes, and the selected manual tier run guest smoke on the
  official GitHub-hosted `ubuntu-latest` runner;
- the full rootless guest suite is a separately selected manual tier;
- the manual `macos` tier builds an installed bundle on `macos-14`, then
  configures consumption of that artifact on `macos-14`, `macos-15`, and
  `macos-26`. This configuration is not proof of a successful hosted run.

`ci/run-test-tier.sh` is the sole tier entrypoint. `ci/bootstrap-west.sh`
materializes a clean checkout before Linux tiers. Hosted Linux jobs set
`DARLING_WEST_UPDATE_JOBS=8`; `ci/west-update-parallel.sh` groups projects by
path depth, completes parents before descendants, and delegates independent
projects within each level to West through one bounded worker pool. It prints
the complete failing project logs. Setting `DARLING_WEST_UPDATE_JOBS=1` selects
West's native sequential update path for debugging. Native macOS packaging
exports the final configured CTest registrations into a relocatable installed
bundle. Local and SSH execution share `ci/native-transport.py`, CTest, and the
source-owned verdict helper.
Native CI uploads a tar archive of the complete bundle, not its raw directory.
`macos-archive BUNDLE ARCHIVE` preserves member modes, links and resources;
`macos-extract ARCHIVE NEW_DIRECTORY` requires a fresh destination and restores
permissions independently of the receiver's umask, without restoring archived
ownership. The artifact service may normalize the outer archive to `0644`
without changing executable or data-file modes inside it.
Docker is not part of the guest execution contract.

The host tier runs `tests/west_test_contracts/native_artifact_contract.py`.
It compiles a resource-reading fixture and checks mode preservation through
archive creation, extraction and installed execution. It checks bytes, executable/data/
directory modes, hidden resources and symlink targets, and rejects extraction
into an existing directory. Run the same contract on macOS to obtain native
transport evidence:

```sh
python3 -B tests/west_test_contracts/native_artifact_contract.py
```

Local Linux RED/GREEN evidence is not a native macOS or hosted-matrix PASS.

## Native convergence contract and inventory

### Inventory boundary and evidence

The collector normalizes concrete `patches/*/patches.yml` files with the
production `test_manifest.load_test_profile`, resolving repository names through
West. It counts each file's entries independently, not inherited/composed runtime
stacks. A binding is a patch-to-test entry, not a unique executable or a PASS.


The collector `scripts/audit-test-registration.py` and reviewed applicability
decisions `audits/native-test-applicability.json` are versioned in this
workspace. The latter is audit policy, not a second runtime test registry.
Reproduce from a configured West checkout with the pinned tool environment:

```sh
mise exec -- uv run --no-project --with west==1.5.0 python -B \
  scripts/audit-test-registration.py /tmp/darling-native-inventory
```

The collector configures testkit for discovery only, does not build product
targets or execute tests. Every binding requires explicit applicability review,
independent of runner or coverage tier. Policy is keyed by
`profile:patch-path:ordinal`, with a SHA-256 fingerprint of the normalized test
declaration and relative West project path. An unknown binding, reused name on a
new binding, or changed declaration fails before a successful snapshot is
written. Source-content/reference review is separate from this metadata
drift guard. Generated `configure.log`, `ctest.json` and
`inventory.json` stay outside version control. The snapshot records all
bindings, owners, source hashes, normalized proof/resources, applicability
and explicit unresolved prerequisites.

### Applicability review commands

Moving reviewed policy when declarations change profile or shift index is a
supported command in the same collector, not hand work. All three modes read the
declaration identity the census writes; none of them builds or executes anything:

```sh
scripts/audit-test-registration.py report
scripts/audit-test-registration.py carry --move OLD=NEW [--move ...] [--drop KEY ...]
scripts/audit-test-registration.py rekey --shift OLD=NEW [--shift ...] [--drop KEY ...]
```

`report` is read-only. It prints every binding with its `identity_sha256` and
separately lists policy records that have no binding, bindings that have no
policy, and identity mismatches, and exits non-zero while any of them remains.
`carry` moves the policy and reason verbatim between the keys the reviewer names,
in one invocation when a re-recording moves many. `rekey` does the same for a
pure index shift, where old and new keys are ordinals of the same
`profile:patch-path`. The reviewer always supplies the mapping: the tool never
guesses one, refuses to invent a policy for a key that had none, refuses to
carry a review across keys whose declarations differ in more than the key, and
refuses to overwrite a record the mapping names neither as a source nor as a
`--drop`. A mapping whose targets already carry the review of their current
declaration is already applied, so re-running it changes nothing.

`--workspace` selects the declarations under review and `--record` selects the
record to read or rewrite; both default to this workspace. An inconsistent
record can therefore be measured without touching the versioned file:

```sh
scripts/audit-test-registration.py report --record /tmp/applicability-probe.json
```

These commands maintain the metadata record only: they move decisions a reviewer
already made and never make one. A carried reason is the reviewed text, so a
declaration whose meaning changed needs a new review rather than a re-key.

The applicability review contract builds a throwaway West workspace and covers
report, carry, rekey, their refusals, and that a refused or repeated command
leaves the record unchanged. It runs against a fixture copy of the record, never
the versioned file:

```sh
tests/run-native-applicability-review-contract.sh
```

The versioned focused host contract exercises inherited-profile census,
independent patch bindings, West alias resolution, rejection of an unknown
compile binding and a reused name, declaration-change rejection, and discovery
without product builds or test execution:

```sh
mise exec -- uv run --no-project --with west==1.5.0 python -B \
  tests/west_test_contracts/native_inventory_contract.py
```
The aggregate entrypoint `tests/run-native-inventory-contract.sh` runs that
focused contract and the real current-workspace census, then removes its
temporary discovery output. `ci/run-test-tier.sh host` includes it through
`ci/run-host-tier.py` as an uncached command: cached host evidence must not
bypass a current metadata/applicability review.


Resolve `unresolved_in_current_checkout` script references against their
declared profile sources. Live-tree absence does not establish missing
materialized-profile coverage. Inventory discovery does not execute guest or
native test cases.

### Authority, identity and selection

- Keep fixture sources and source-owned CMake/CTest registrations colocated
  with their owning project. Do not move all suites into workspace testkit.
- CTest/source registration defines the scenario, arguments, environment,
  resources, working directory, deadline and verdict. Patch metadata binds
  that case to patch-specific runtime prerequisites, diagnostics and RED proof.
  Preserve legitimate direct host/model/source/build runners.
- Identify a case by project, source-suite scope and existing declared case
  name; retain exact discovered CTest names as execution instances. For example,
  workspace `testkit` case `getattrlist_name_objtype_guest` has the existing
  `darling/...` and `macos/...` instances. Do not require invented `name:` labels,
  globally unique third-party names, or mass renames.
- Environment and reference/bad/fixed roles are execution dimensions. Compare
  fixture/contract digests and deliberate scenario parameters; the same source
  filename alone does not prove the same test. Shared cases may bind many patches.
- Resolve existing scoped selectors to exact CTest instances before applying
  target-environment selection. `--list` and execution must agree. An explicit
  request with no applicable case must say why; it cannot establish coverage.
  Merely changing `runs: guest` to `runs: macos` does not change a guest runner.
- Local macOS and SSH are transports for the same native execution contract.
  Use isolated owned work directories and bounded cleanup. A Linux-built binary
  or successful shell transport is never a native Darwin reference.

### CTest selection bridge

Patch metadata can bind existing CTest registrations with `ctest: <label-regex>`,
`ctest-name: <exact-existing-name>`, or both for a scoped exact-name selection.
These references do not copy fixture commands or require new `name:` labels.
A reference cannot override `command`; explicit fixture runners are separate.

West configures the selected source scope and discovers CTest JSON before
filtering environment and diagnostics. `env:*` labels define the actual
variants; an unlabelled upstream registration belongs to the configured host
(`macos` on Darwin, otherwise `host`) in both metadata and direct CTest selection.
A binding's explicit `runs`/`env` restricts its variants; it never relabels a
guest runner. Darling runtime and RED ownership must use a Darling-scoped
binding, separate from a native reference to the same scenario.

`--list` configures discovery without building product targets, executing tests,
or acquiring a prefix. A source-bound profile may need temporary worktrees.
Both listing and execution replay the exact discovered registrations. CTest
indices are local to that configured build, not persistent case IDs: existing
names and suite directories retain identity, including equal names in different
source suites. Installed transports must derive their contract from the source
registration, not persist these temporary build paths or indices.

Missing references, missing requested variants, ambiguous registrations and
unintended empty selections fail closed. Bead/submodule selectors match complete
labels, while `--label` is a user-supplied regex. Runtime-profile prerequisites
are shown during discovery and deployed only for execution.

The behavioral CLI contract runs disposable CMake/CTest suites through the real
West loader, exercises metadata and direct selectors, executes the selected host
cases, and preserves CTest's formal `SKIP_RETURN_CODE` in JUnit. It also covers
exact upstream-style names, colliding suite names, disjoint indices, unlabelled
native cases, and invalid bindings:

```sh
tests/run-west-test-ctest-backend-contract.sh
mise run west test --env macos --list
mise run west test --env darling --bead dar-e1j --list
```

The host tier includes this contract. Discovery listings are not native or guest
execution verdicts.

Upstream uses CMake/CTest PASS/FAIL, not a TAP protocol.
Its [unsupported helpers](https://github.com/darlinghq/darling-testsuite/blob/master/lib/darling-testsuite/src/darling-testsuite/unsupported.c)
only print availability messages and do not themselves produce a formal skip.
West does not reinterpret those messages as a new protocol. A zero exit through
an unsupported-API branch is not evidence that the API was exercised; a formal
skip must come from the source-owned CTest verdict contract.

### Preserve the complete execution and verdict contract

The installed native bundle preserves final CTest arguments, environment,
working directory, resources, properties and per-case timeout.
`darling_install_native_bundle()` exports registrations after all source scopes
have been configured. Unrelocatable paths and unsupported dylib dependencies
fail packaging rather than silently producing an incomplete bundle.

```sh
ci/run-test-tier.sh macos-installed BUNDLE [-R REGEX]
ci/run-test-tier.sh macos-ssh HOST BUNDLE [-R REGEX]
```

Both transports require real Darwin/Mach-O execution and use fresh result
directories outside the immutable bundle. Set `DARLING_NATIVE_RESULTS_DIR` to
a nonexistent output path; otherwise a temporary directory is allocated.
`DARLING_NATIVE_REMOTE_PATH` supplies the remote tool search path.
`DARLING_NATIVE_TIMEOUT_SECONDS` bounds execution (default 1800 seconds);
`DARLING_NATIVE_TRANSFER_TIMEOUT_SECONDS` bounds transfers (default 120).
SSH uses a token-owned temporary workspace and collects results before cleanup.

Exit codes are 0 for pass/formal skip, 1 for semantic test failure, and 2 for
infrastructure failure. Results include `execution.json`, `native-build.json`,
CTest discovery, JUnit and logs; SSH retains remote results under `remote/`.
Build identity records source/CTest digests, compiler/SDK and build options;
execution identity records bundle digest, OS build, architecture and Rosetta.
CTest runs through a results-owned facade so nested discovery does not write
`Testing/` into the bundle.

Real-Mac acceptance on `misakaindrive`, macOS 26.5.1 (25F80), arm64, CTest
3.31.6 exercised relocation after deleting the source/build trees, arguments,
environment, resources, exact success and negative markers, normal exit 137,
signal rejection, timeout, formal skip and late CTest property changes.
Local and SSH runs agreed on all nine representative verdicts. Separate SSH
selections returned pass, skip and infrastructure-error outcomes. Evidence is
under `~/work/darling-debug/dar-759a.4-8XFgdS/`. This is transport acceptance,
not a hosted matrix or product compatibility result.

Focused regressions are `native_bundle_contract.py` (real Mac),
`native_transport_contract.py`, and `native_artifact_contract.py` under
`tests/west_test_contracts/`.

For a declared success marker, require successful execution and the exact
literal output line on every path. For declared expected failure, preserve
the explicit failure oracle and failure stage; unrelated upload, compile,
bootstrap or runner errors do not prove a semantic RED. Treat timeout as a
supervision outcome, not automatically as an expected incompatibility.

Record separate semantic PASS/FAIL, infrastructure error, not-run and
inapplicable-with-reason outcomes. Record normal exit versus signal termination;
do not equate a raw process status with shell convention `128 + signal`.
Reference, bad-runtime and fixed-runtime results are separate records with
full fixture/contract identity, canonical and deployed source SHAs, deployed
artifact hashes, OS build, architecture/Rosetta, compiler/SDK and flags.
No result from one role substitutes for another.

### posix_spawn ownership fixture

`tests/posix_spawn_failure_guest.c` has one source-owned CTest definition,
`posix_spawn_failure_ownership`, with Darling and installed macOS variants.
Patch metadata binds these names. The dedicated runtime provider selects
`wget-residual`. Both dyld and libsystem_kernel are runtime artifacts
because dyld embeds the syscall emulation path.

The fixture checks:

- failed ENOENT spawn exposes no waitable child or SIGCHLD;
- 64 fast successful children retain status and notification;
- 64 successful children can be reaped by an application SIGCHLD handler;
- a pipe handshake proves spawn returns before the child is released;
- an unrelated successful child's pending SIGCHLD survives a failed spawn.

Do not interpret `waitpid(..., WNOHANG) == 0` immediately after failed spawn as
a leak: private cleanup can be transient. The fixture uses blocking waitpid to
ask whether any child is returned to the application; CTest bounds hangs
without counting them as semantic RED.

```sh
ci/run-test-tier.sh macos-package /absolute/path/to/bundle
ci/run-test-tier.sh macos-installed /absolute/path/to/bundle \
  -R '^macos/posix_spawn_failure_ownership$'
ci/run-test-tier.sh macos-ssh HOST /absolute/path/to/bundle \
  -R '^macos/posix_spawn_failure_ownership$'
mise run west test --profile wget-residual \
  --patch xnu/posix-spawn-failure-ownership.patch --env darling \
  --prefix /absolute/path/to/prefix --prove-red
```

Run the prefix-backed command through `scripts/west-job.sh` in agent transport.
The selected runtime provider declares its launcher environment; metadata
selection binds it before the initial prefix shutdown as well as during the
provider's deployment/execution. Conflicting launcher environments for one
selection are rejected before acquiring the prefix.

Source inputs declared by `wget-residual` locks use Git bundles under
`source-bundles/wget-residual/`. Relative `mirror.url` paths resolve against the
declaring lock file's directory. Validate every declared base/source tag,
ordered commit graph and tree; an unavailable bundle fails even if the caller
has the objects. Keep source inputs outside generated `handoff/`, which the
transactional handoff replaces wholesale.

Runtime-source admission follows the registered typed mapping and its
profile-composition lock, not a hardcoded list of profile names. Register a new
composition in `locks/patch-stack/lock-first-profiles-v1.yml`; its prerequisite
trees, ordered series, batch count and final trees must all validate before
materialization.

The lock-first contract
(`tests/run-west-patch-stack-lock-first-contract.sh`) exercises the real
profiles and therefore switches live module repositories to their profile
branches for the duration of the run. Do not run it in parallel with any other
West operation that materializes or applies a profile; its assertions must also
stay derived (planner output versus the declared inventory) or structural, never
hash literals of checked-in composition data, because those churn on every
reviewed profile refresh and the canonical boundary evidence is
`west patch verify`, which replays the real trees.

Applicability preflight has a 300-second per-profile default.
`--runtime-build-timeout-seconds` overrides both source preflight and build
phase deadlines. A verifier timeout leaves source validity undetermined; it
does not establish a patch conflict or a runtime regression.

The CTest bridge propagates guest upload, compile, run and timeout phases.
Require the failure oracle in the guest execution phase for runtime RED;
compiler diagnostics containing the marker do not satisfy that oracle.

### Reproducible Homebrew build prerequisites

The opt-in `homebrew-lz4-source` runtime profile uses `wget-residual` and
the source-owned `rootless_toolchain` component. It includes native `cut`,
system `openssl` with its configuration/certificate data, and Perl 5.18/5.28
standard libraries and XS bundles. Perl uses its normal CMake install rules,
staged through `DESTDIR`; absolute dSYM destinations must not populate the
live prefix before West deployment. Explicit component providers take
precedence over duplicate build/staging copies during Mach-O closure discovery.

Deployment checkpoints the complete atomic transaction manifest after each
file. Preserve rollback, restart recovery, ownership and symlink checks.

From the manifest directory, select the supported native CLT 13.2 package and
configure a named context. `DARLING_CLT_PACKAGE` selects the existing provisioner:
it verifies the reviewed whole-file SHA-256 and installs the distribution through
the guest `installer`. The default catalog supplies CLT 9.2 / SDK 10.13, which
does not satisfy this Homebrew runtime's SDK requirement. Package selection is
an environment setting, not a persisted dev-context value.

Use a short, owned prefix that is absent or empty, not an untyped populated
directory. Expanded AF_UNIX socket paths include the host prefix and must fit
Linux's 108-byte `sun_path`, including the terminating NUL. Keep the ordinary
guest temporary directory; do not weaken the expanded-path guard.
Bootstrap creates a missing prefix root under its lifecycle lock. This does not
relax admission checks for populated or mode-mismatched prefixes.

```sh
export DARLING_CLT_PACKAGE=/absolute/path/Command_Line_Tools_for_Xcode_13.2.pkg
mise run dw dev context homebrew --prefix /tmp/dar-hb
mise run dw dev run homebrew-prepare
mise run dw dev run homebrew-preflight
mise run dw dev run exact-capture
mise run dw dev run homebrew-source
```

`mise run dw` selects the pinned mise environment. Contexts live in local West
configuration (`dev-<name>.*`), not checked-in host-specific paths. `dev context`
selects the active context; `dev run --context NAME` and explicit path flags
override it without rewriting configuration. The default runtime is
`homebrew-lz4-source`; bundle storage defaults to the workspace parent's
`darling-debug` directory. Without an explicit executor, the checked-out
runner is incrementally built in release mode before execution.

### Guest runtime invocation and container constraints

Rootless prefixes are launched with the global `--rootless` flag and the
profile's launcher environment; a bare `<prefix>/bin/darling shell ...` fails
with "is not setuid root, which is mandatory" and must not be used as a probe:

```sh
env DPREFIX="$P" DARLING_PREFIX="$P" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 \
    DARLING_EUNION=1 "$P/bin/darling" --rootless shell /bin/bash --login -c '<command>'
```

Guest C sources compile inside the guest with the provisioned CommandLineTools
and its SDK; the SDK stubs do not export Darling-specific entry points, so a
guest test either links the deployed dylib explicitly or resolves the symbol
with `dlsym`:

```sh
/Library/Developer/CommandLineTools/usr/bin/clang \
    -isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -o <bin> <src>
```

Container runs need a glibc base image. The host-side launcher and
`darlingserver` are dynamically linked and require `GLIBC_2.38`; a musl image
such as the pinned Alpine cannot execute them at all, so use a digest-pinned
glibc image whose libc is at least that new, run as the ordinary user with
`--cap-drop=ALL --network=none`, and mount the bootstrapped prefix read-write at
its baked absolute path so the launcher's recorded prefix still resolves.

`dev run` starts a recorded West job and immediately attaches its observer.
It prints the job identity plus exact reconnect/cancel commands. `--detach`
starts without observing; `mise run dw dev follow JOB` reconnects and
`mise run dw dev cancel JOB` requests owner cleanup. `--dry-run` displays the
underlying command without building, booting, or creating a job.

Retained runtime reuse checks the selected source profile and its complete
`base-profile` chain in the manifest repository, the source lock, runtime
definitions, source module revisions from the West root, and the deployed
launcher. A changed input or an older fingerprint schema requires a new
`homebrew-prepare` run; do not rewrite a retained marker to bypass this check.

The exact-capture diagnostic uses initialized West command state, validates
the retained provider fingerprint, and owns the normal prefix lock/cleanup.
Its payload intentionally times out; diagnostic success requires the guest
readiness marker, identity-hashed Mach-O image, structurally complete ELF core
for the same guest process, registers, and successful prefix cleanup.
The archive's separate `exact_complete` flag can be false for unreadable
mappings. Retain Linux vsyscall warnings and report incomplete capture
accurately. An ordinary timed-out test fails.

The preflight executes field extraction, Perl SHA-256/POSIX/Socket operations
and XS loading, and system OpenSSL X509 fingerprinting. The host requires the
guest verdict as well as successful transport. The `homebrew-lz4` resource runs
this preflight before downloading/staging stock brew inputs. No formula changes,
insecure TLS options, or binary-only Perl installation are involved.
The separate host `homebrew_component_staging_isolation` contract exercises real
CMake relative/absolute installs and symlinks, including prefix paths with spaces.

Validate SDK identity against its authoritative package metadata and retain
provenance evidence. The SDK must satisfy the selected runtime and Homebrew's
compatibility checks. Do not invent version metadata or disable those checks.
Record preparation, preflight, exact diagnostic and source-build results
separately in Beads and diagnostic archives; one passing scenario does not
establish success for another.


### Native applicability and migration gates

Review each binding's native applicability using
`audits/native-test-applicability.json`. `report`, `carry` and `rekey` in
`scripts/audit-test-registration.py` maintain that record; they move decisions a
reviewer already made and never make one. Distinguish public semantic references,
implementation-only or internal diagnostics, and workloads requiring ABI,
SDK, compiler, descriptor-limit or package setup. An unresolved applicability
decision does not approve stub behavior, security-policy bypass or SDK-label
consistency as compatibility evidence. Compile, host, model and source evidence
do not establish native inapplicability.

Applicability classification is not an execution verdict. An internal
classification applies to the complete oracle, not every public API used by
its workload. Do not obtain shared GREEN by weakening assertions or merely
adding `ENVS macos`. Review errno domains, descriptor inheritance, attribute
support, timing assumptions and ABI availability against a native reference.

Keep applicability decisions with the owning patch and Bead. Retain internal
RPC/fault/trace, DCC, overlay and prefix-lifecycle gates on Darling; expose a
separate semantic reference when the workload has a meaningful public contract.
Require compatible native-reference, bad-runtime RED and fixed-runtime GREEN
evidence through supported runners. Cut each case over completely before
removing its duplicate wrapper. Workspace-only registration changes do not
rewrite canonical source commits; source fixture changes require
fix-branch/export SHA and checksum refresh.

Tie publication checks to the owning patch's semantic proof and review; do not
substitute an unrelated package gate for that evidence.

## Test architecture

Use CMake/CTest as the backend and keep workspace tooling as a thin
orchestration layer. Preserve the upstream `add_test()`, installed
`darling-testsuite/{testcase,resource,manual}` layout,
`darling-testsuite-lib` assertions/resources/XML and `darling-directsyscall`
interfaces.

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
- Use `west test` selectors and metadata to identify the proof for a patch.

### Patch-Local Red-Test Metadata

Local patch profiles do not need a separate upstream `darling-testsuite` patch
for every fix. The default workflow is: the smallest deterministic red test
travels with the fix in the same source repo and, when practical, the same patch
file/profile entry. Cross-repo bugs can use a small adjacent test patch in the
same profile. Upstream `darling-testsuite` supplies the portable testcase style;
upstream publication is not a local test prerequisite.

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

Runtime preflight uses `west patch verify --applicability-only`: on a clean CI
runner the fork's local `source-branch` refs are intentionally absent, so this
mode checks patch integrity and application to the pinned manifest revisions
without confusing missing developer branches with a runtime failure. Full
`west patch verify` checks source-branch/export drift for publishing.

E-UNION host behavior follows the same rule. `testkit/CMakeLists.txt` builds
the production XNU sources and the workspace harness as a CTest target, while
the fixture setup is a CTest fixture prepared by
`experiments/e-union/run.sh --prepare-fixture DIR`. Both normal GREEN runs and
source-base RED runs select
the `eunion-host` label; only `DARLING_XNU_SRC` changes between them. The
source-bound host suite is opt-in through
`-DDARLING_ENABLE_EUNION_HOST_SUITE=ON`; West supplies that flag only for the
separate materialized source build. The default testkit build keeps it off, so
guest CTest selection cannot compile an E-UNION harness against the unpatched
checkout before runtime-profile deployment.

Where a host test is declared is part of what it means. `west test --profile <P>`
reads tests only from `patches/<P>/patches.yml`; `base-profile` composes the tree
but does not inherit test declarations, and a patch entry resolves to a file
inside that profile's own directory. The E-UNION host suites are the worked
example: they compile `experiments/e-union/runner.c`, whose
`large_directory_checks()` runs unconditionally and asserts behaviour that needs
`xnu/eunion-large-directory.patch` and `xnu/eunion-content-fd-validation.patch`,
and both series belong to `wget-residual`. Declared under `homebrew` they were
structurally RED - the same harness reported `305 tests, 14 failed` on the
homebrew tree and `305 tests, 0 failed` on the wget-residual tree. They hang off
a wget-residual entry now, and the host tier sweeps `wget-residual` as well as
`homebrew` (about 100 seconds end to end, mostly materialization) so they run in
CI where their chain satisfies them. Relocating a declaration is not free: an
entry outside the profile's locked series is invisible to the lock-first
materializer while still breaking `west patch apply`, `check` and `export`, so a
series changes only when the series, not the test, is the thing that is wrong.

### What the contract census credits

`ci/run-host-tier.py` refuses to start when a contract is neither registered
(`CONTRACTS`, `EXPLICIT_CONTRACTS`) nor excluded with a reason
(`EXCLUDED_CONTRACTS`). The transitive part of that rule is deliberately narrow:
a contract is credited only when a source the tier already reaches invokes it
**by path**. Four ways of naming a contract prove nothing, and all four were
found in the tree:

- a variable assignment that stores the path for a call made elsewhere:
  `tests/run-west-job-contract.sh` assigned
  `tests/run-west-test-metadata-contract.sh` to a variable and called it only
  with `--transport-gate-probe`, which prints a marker and returns before the
  body. The chain, and the eleven python contracts it drives, were counted as
  covered while nothing executed them; two of the eleven had rotted to
  `AttributeError` unseen.
- a line that only passes a probe flag. Probes are the convention
  (`--transport-gate-probe`, `--self-contract-probe`,
  `--metadata-display-contract-probe`); a probe is a gate test, not an
  execution of the contract behind it.
- a comment. While this rule was being written, a comment naming the chain in
  the runner was itself enough to keep it "covered".
- a test-synthesized profile under `patches/__*`. Those are fixtures a running
  test materializes, not declarations, and one leftover credited the chain
  again.

The eleven contracts that chain owns are registered individually in
`EXPLICIT_CONTRACTS` now, so they execute in about 1.6 seconds together. The
chain runner itself stays in `EXCLUDED_CONTRACTS` with its reason: its west
steps materialize profiles whose lock-first batches fetch
`refs/tags/patch-stack/*` from the immutable mirror. That fetch is bounded and
non-interactive, so it now fails closed with a named unreachable-mirror error
instead of hanging for twenty minutes, but a workstation without mirror access
still cannot run the chain to the end. Registering the chain is what finishes the
job where the mirror is reachable.

One stale assertion in that chain was repaired rather than excused. It grepped
the `west test --list` output for `ctest .* -L bead:dar-gwn.5`, but a metadata
`ctest:` reference that resolves is pinned as the exact index it resolved to,
so the listing carries `-I 28,28,1`. The step now parses the guarded payload
and compares what the label and the resolved selection actually select, which
is a stronger oracle than the string it replaced - and it is the same
distinction the display matcher's own contract covers with its fixtures.

### Scheduling the host tier by weight

The tier runs its commands with a slot budget rather than one slot per process.
A contract that drives other runners or a nested tier declares its cost in
`CONTRACT_WEIGHTS`, each entry with the measured reason (42 runner invocations
for the metadata chain, 11 for the testkit root, 34 subprocess sites in
`dev_check_contract.py`). Commands holding more than one slot cannot be
scheduled beside a peer that leaves them no room, light commands are submitted
first so a waiting heavy command does not block them, and a weight keyed on a
command that does not exist fails the tier instead of being ignored. The
motivation is that a load failure and a regression look identical in the
result: `run-west-extension-help-contract.sh` and `dev_check_contract.py` both
failed inside the tier and passed alone. The answer is scheduling, never a
retry.

The tier's own wiring contract stopped executing the tier to check it.
`tests/run-ci-test-tiers-contract.sh` copies `ci/run-host-tier.py` and
`ci/run-test-tier.sh` into a mirror repository, reads the registered contract
paths out of the copied runner, writes a stub for each, and runs the mirror
tier for real - so the census, the weighted scheduling and the command
construction all execute, while the ~90 contracts are stubs that record their
invocation. It runs in about two seconds, is registered in the tier it checks,
and still fails when a tier command line changes (verified by mutating the
wget-residual sweep argv and a guest-toolchain command line).

### Reissuing a profile composition

A `locks/patch-stack/<profile>-profile-composition-v2.yml` lock is the receipt
for a profile's series: per module, the tree the module starts from, the tree
every locked patch produces, and the final and integration-final trees. Only a
replay knows those values, and `scripts/generate_profile_composition.py` does
that replay - the same lock-first machinery the materializer uses - then renders
the file in the checked-in style.

The worked example is the thread-create fix joining the `mldr` series. The
materialization refused to run with

```
darling/mldr-thread-create-futex-wait.patch: immutable replay tree a07675d1c363cb8ac1c4f38b2a1051dfe11d5627
differs from expected profile boundary tree 8dd0c122dc6eb51d743f15d70ce0fc2c351fe698
```

and the derivation reproduced `a07675d1` independently. Comparing the two trees
showed exactly two differing files, and the derived blobs were the fixed ones
(`ea61d009`, from the fix commit) against the receipt's pre-fix ones
(`daf89968`, from the first commit) - so the receipt was stale, not the replay
wrong. Reissuing five files (homebrew, perf, arch, wget-residual,
ring-comparison) made `west test --profile homebrew --env host
--materialize-profile` materialize and run 64 metadata cases with no failures.

Two properties make the derivation trustworthy rather than circular. It never
reads a value out of the checked-in lock - trees come from a replay of the
locked immutable refs, and the entry list is built from the typed mapping rather
than through `plan()`, which would bind a dependent profile's still-stale
prerequisite digest. And the receipt stays a check: materialization compares
against it on every run, which is exactly how the drift was found.

Rules the derivation follows, each with its reason:

- **The entry list comes from the mapping, not from the lock.** A dependent
  profile cannot even be planned while its checked-in lock still records the old
  digest of the prerequisite being reissued.
- **`starting.tree` of a module is the tree before its first locked patch.** For
  a stacked profile that is the prerequisite profile's final tree, which is why
  the profile graph is derived prerequisite-first.
- **Nested modules reproduce byte-for-byte; a parent tree does not
  necessarily.** A parent tree carries gitlink commit IDs, which are generated
  lifecycle evidence, and `verify_inherited_parent_boundary` exists to normalize
  untouched child records for that reason.
- **`integration_final_tree` equals `final_tree`** for every module, which the
  generator asserts instead of assuming.
- **A style-only difference is a failure, not a write.** Formatting churn in a
  generated registry hides the real change; the same principle already applies
  to `west patch export`.
- **A write run reports the same field-level diff as `--check` and exits zero**;
  only `--check` promises not to have fixed anything.

Cost is the reason this is a deliberate command and not part of the tier: each
invocation replays every profile in the requested chain, needs the immutable
mirror, and takes minutes. The tier's gate for a stale receipt is the profile
materialization it already runs; the focused contract
`tests/run-profile-composition-derivation-contract.sh` covers the derivation's
decisions cheaply - which field a drift is reported against, the style refusal,
the module mapping, and a prerequisite described from the derived bytes.

### What an export leaves behind

A series lives in four artifacts, and `west patch export` writes two of them:

| artifact | refreshed by |
|---|---|
| `patches/<profile>/<path>.patch` and `patches.yml` | `west patch export` |
| `locks/patch-stack/<module>-<patch>-v1.yml` | hand-edited today; needs a create-only tag published to the mirror first |
| `locks/patch-stack/migration-inventory-v1.yml` | hand-edited today; restates the lock |
| `locks/patch-stack/<profile>-profile-composition-*.yml` | `scripts/generate_profile_composition.py` |

The other two cannot be derived inside the export, so it reports instead of
guessing, at the moment the change is in hand. On a consistent tree it says
nothing; with a lock that still describes the previous series it prints, for
each exported entry:

```
darling-mldr-thread-create-futex-wait-v1.yml: still records source_commit dd6b42e5…,
but darling/mldr-thread-create-futex-wait.patch now exports c0640633… (2 commit(s))
  publish it create-only: git push https://github.com/darling-next/darling.git c0640633…:refs/tags/patch-stack/v1/sources/c0640633…
  then refresh the lock (schema-v2, mirror.source_oid/source_ref, source_commit,
  ordered_commits, expected_tree) and its row in migration-inventory-v1.yml -
  neither has a refresh command today
  reissue the compositions this moves: scripts/generate_profile_composition.py
  --profile homebrew (its dependents: perf, wget-residual, arch, ring-comparison)
```

The dependents are the transitive closure of the compositions that name this
profile as a prerequisite, which is why the homebrew chain lists four further
profiles: those are the leaves a homebrew change reaches.

This exists because the alternative was measured. Adding one commit to the
thread-create series was found late and three times over: first by a receipt
contract, then by the tier's registry contract, and finally by the homebrew
profile materialization, which is the most expensive place to learn it. The
report is covered by `tests/run-patch-series-bindings-contract.sh`, which proves
the four decisions cheaply on a synthetic locks root: which binding is behind,
the commit the artifact carries, the refspec that publishes it, the receipt row
that still disagrees, and the transitive closure - plus silence when everything
agrees, because a reminder that fires on a current tree is noise.

Guest
E-UNION cases use `guest-c-fixture` metadata because their lower and
upper trees must be staged inside an isolated Darling prefix by the typed
`darling-eunion-prefix` provider. The `eunion-overlay` fixture profile keeps
that setup declarative:

- `template-files`, `template-symlinks`, and `upper-files` describe the lower
  template and any pre-existing upper entries;
- `cleanup-dirs` bounds per-test state to `/private/var/tmp/west-*` paths;
- `forbid-template-paths` asserts that a mutating guest operation did not write
  into the immutable template;
- `require-upper-paths` asserts that the expected object was materialized in
  the writable upper layer;
- `verify-template-files-after` checks template contents, modes, and xattrs
  after the runtime is shut down.

The first two oracle lists are checked while Darling is still alive, because
runtime shutdown removes its private sockets and other runtime-created nodes.
Template-file assertions run after shutdown and before fixture cleanup. A
template symlink target containing `..` is rejected by default; use the
explicit `allow-parent-target: true` only for a test whose purpose is to check
symlink containment or rejected escape behavior.

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

Explicit metadata and compact profiles are supported. Prefer compact profiles
for repetitive guest/runtime metadata so the manifest describes each test's
unique requirements. For example, in the `perf` profile:
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
mise run west test --profile homebrew --patch darling/mldr-thread-create-futex-wait.patch
mise run west test --profile homebrew --patch darling/mldr-thread-create-futex-wait.patch --prove-red
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
3. `descriptor-trace`: allocates the workspace-scoped host capture directory for
   a guest fixture's RPC descriptor-transport observation and validates its
   declared windows.
4. `dcc-cache`: materializes/builds the declared cache tooling and injects the
   guest DCC environment.
5. `darling-eunion-prefix`: boots/verifies the E-UNION prefix and stages
   upper/lower fixture files.

Provider order is part of the contract: host observation paths, stat tools and
descriptor captures are prepared before cache and prefix setup, and cache
resources are prepared before prefix fixtures that may boot or probe the
runtime. New shared runtime setup should become a provider with a focused
contract instead of growing individual runner bodies.

Runtime RED artifact planning lives in a separate helper layer. The pure
planning code owns build-target de-duplication, deploy-plan display, and mapping
guest-visible deploy paths to the prefix files that must be swapped. The
side-effecting build/deploy/restore sequence stays in `west test` until the
runtime lifecycle can be split further without changing behavior.

### RPC descriptor-transport windows

`descriptor-trace` makes one `runner: guest-c-fixture` test's RPC descriptor
transport observable from the host. It is the gate for a call whose ABI used to
carry a file descriptor and must not any more:

```yaml
    descriptor-trace:
      windows:
      - id: console-open-control
        expect-descriptor-messages: at-least-one
      - id: vchroot-fdless-valid
        expect-descriptor-messages: none
```

Each window id names a boundary pair the fixture publishes around one operation.
The fixture prints `DSERVER_DESCRIPTOR_WINDOW BEGIN|END <id>` to its stdout and
resolves `/tmp/darling-descriptor-window-<id>-begin|end` with `readlink`, which
the guest emulation performs as a raw Linux `readlinkat` in the fixture's own
process; that syscall is what makes the boundary visible in the trace.

`west test` runs the whole fixture runner under

```sh
strace -f -ff -tt -yy -x -s <limit> --seccomp-bpf \
  -e trace=sendmsg,recvmsg,sendmmsg,recvmmsg,readlink,readlinkat -o <dir>/trace
```

`--seccomp-bpf` is required: without in-kernel filtering the runtime's adaptive
RPC spin loops time out under ptrace. The runner scopes every declared window to
the messages between its two markers in the same per-process trace file and
prints the counts it observed:

```text
WEST_DESCRIPTOR_TRACE window=<id> call-messages=<n> descriptor-messages=<n> expect=<none|at-least-one> verdict=<ok|failed> trace-file=<name>
WEST_DESCRIPTOR_TRACE_DESCRIPTOR window=<id> syscall=<sendmsg|recvmsg|sendmmsg|recvmmsg> trace-line=<n> <strace line>
WEST_DESCRIPTOR_TRACE_OK windows=<n>
```

`expect-descriptor-messages: none` fails when the window carried any
`SCM_RIGHTS` ancillary data. A window that observed no call-direction message
fails too: an operation that stopped issuing its RPC must not pass a
zero-descriptor assertion. Every declared window must also appear in the guest
transcript, so a window the fixture never reached cannot be satisfied by an
unrelated instruction stream, and traffic in another process's trace file cannot
satisfy it either. A run should declare at least one unaffected FD-bearing call
(`console_open` replies with the console socket) as the negative control that
proves the observation sees descriptor transfers at all.

Captures and their per-process trace files are kept in the manifest repository
under `.west-test/descriptor-trace/<test name>/`, and the runner prints the exact
`strace` command it used. `strace` must be installed on the host; a missing
`strace` fails the run instead of skipping the observation. The split
`prepare-fixture-before-deploy`/run-only phases cannot be captured, because the
server started by the prepare phase already owns the guest tree, so the runner
rejects that combination.

Darling prefix lifecycle helpers are also split from the runner where they are
pure enough to test directly. `west_commands/test_prefix.py` owns process-tree
discovery for `darlingserver <prefix>`, matching server PIDs, and stale
`.init.pid` removal. The runner owns the side-effecting shutdown,
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
When the fixture also needs a paired module, give it a distinct test-level
`source-env` for the selected profile's module root and derive the peer from
that profile layout. Keep `red-proof.source-env` separate for the source under
test. The relocated script's directory is not the complete profile forest.

Use `runner: source-script-fixture` only when the script itself belongs to the
source tree under test and already exists in both the RED source base and the
GREEN profile tree. Do not use it for shell scripts newly added by the patch:
the RED result would prove only that the script file is missing.
Executable source scripts run directly through their shebang; non-executable
source scripts fall back to `sh`.

Plain `runner: script` is an escape hatch for tests with special process,
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
`runs: guest`; West owns declared prefix resources, trace/temp files, and
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
mise run west darling-prefix-repair --prefix "$HOME/work/darling-prefix"
mise run west darling-prefix-repair --prefix "$HOME/work/darling-prefix" --check
mise run west darling-prefix-repair --prefix "$HOME/work/darling-prefix" --cleanup-mounts
```

The repair command creates the required rootless runtime directories
(`private/var/db`, `private/var/db/launchd.db`,
`private/var/db/launchd.db/com.apple.launchd`, `var`, `var/run`, and `var/tmp`) plus the
`private/var/tmp` directories. The latter keep mode `1777`; the runtime
parents are created with mode `755` and are then owned by Darling's normal
launchd bootstrap. `private/var/db` is required because launchd and launchctl
create their overrides database below
`private/var/db/launchd.db/com.apple.launchd` during the first system bootstrap.
The nested database directories are provisioned explicitly because launchctl's
one-level metadata repair cannot create missing parents in a clean rootless
prefix. It also restores canonical
`CommandLineTools`/`DarlingCLT` clang links from the versioned CLT already
installed in the prefix. Runtime profiles that run source-driven guest CTest
cases additionally declare `guest-toolchain: darling-command-line-tools`.
The typed West provider checks the default compiler and SDK, downloads the
official package set from Darling's existing CommandLineTools distribution
endpoint only when they are absent, installs each package through the guest
`/usr/bin/installer`, verifies the official HTTPS host, package size, XAR
envelope, the API SHA-1 of the **compressed XAR table of contents**, and the
reviewed SHA-256 of the **entire package** before installation. Neither digest
alone establishes publisher authentication; signed-publisher review is recorded
separately in `clt-provenance-041-90419.txt`.
Package bytes stay in the external West cache; prefix-owned staging files are
removed after installation.
`west test` and
`west darling-doctor` share the same prerequisite checks, so a repaired prefix
is checked against the same contract that guest metadata tests require. The
`--cleanup-mounts` mode unmounts stale filesystems left under an otherwise idle
prefix; `west test` runs the same cleanup after `darling shutdown` and fails the
test run if mounts remain.

`west darling-doctor --scope workspace` checks manifest drift without inspecting
runtime state; `--scope runtime` checks build/deploy and prefix postconditions.
The default `all` preserves both sets. Bootstrap runs the workspace scope before
prefix mutation/build/deploy and the runtime scope after guest readiness.
On runtime doctor failure or timeout, full raw stdout/stderr and structured
problem rows are saved under the existing runtime evidence `diagnostics`
directory before rollback. The fatal message names the relative artifact;
the surrounding failure handler reports its final retained archive directory.

Historical rootless debug prefixes are separate from test scratch and must not
be removed with a broad `/tmp/darling-rootless-*` glob because that namespace
also contains source worktrees. Use `west darling-rootless-debug-cleanup --path
/tmp/darling-rootless-*-debug-* --dry-run` for one completed debug tree. It
refuses non-debug paths, mounted filesystems, and live processes whose
`DARLING_PREFIX` is inside the target. If ordinary removal reports an ownership
failure, rerun the same explicit command with `--sudo`; it uses
`rm --one-file-system` only after the same checks pass.

For metadata tests that use `runs: guest`, `west test` also owns the resource
lock and shutdown path. A real run flocks the prefix's open parent-directory
descriptor before launching the test and holds it through cleanup, without
creating a lock file inside the prefix. It calls `darling shutdown` for the
selected prefix, then stops matching leftover server and rootless guest
processes. Rootless guest discovery also handles scrubbed `DARLING_*` variables:
an `mldr` process's `__mldr_sockpath` identifies an inherited directory fd,
whose device/inode must match the selected prefix. The original socket owner
may already have exited; the guest's own retained fd remains the ownership
evidence. A shared loader installation or guest argv alone is not sufficient.
Leftover processes make the run fail even if the payload passed. Pass
`--keep-prefix-running` only when intentionally keeping the prefix warm for a
manual debug loop.

For patch metadata, `diag: guarded` and `diag: forensic` are enforced by
`west test`, not by each script. `guarded` wraps the structured invocation in
`darling-debug-runner run --timeout-seconds ...`, writes a small debug bundle,
and uses prefix-owned shutdown for guest commands (process-group termination for
generic commands). `forensic` adds `--capture-exact --capture-tree`, plus exact
prefix ownership selection for detached guests, before shutdown or artifact
restoration. The runner is resolved from `--executor`, `PATH`, or the checked-out
`darling-debug-runner` west project (`target/release` preferred, then
`target/debug`). If a non-bare test is executed without a runner, `west test`
fails before launching the test. `--list` is offline and shows the wrapper
shape without requiring the binary to exist.
For command invocations and metadata guest-C fixtures, the outer West deadline
reserves 300 seconds after a forensic executor's deadline for capture and
cleanup, rather than the ordinary 15-second grace. This does not extend the
payload deadline or make a timed-out test pass. Capture is bounded; an
executor that exceeds the grace fails the run.
The exact archive defaults to a shared 60-second deadline and 512 MiB cap.
Mapped images are identity-checked and hashed; cores, mappings, fd state and
registers stay with those images instead of depending on restored prefix files.
Incomplete capture is explicitly recorded, including unreadable kernel mappings.
Cores can contain secrets. `mise run dw dev run` incrementally builds the workspace
release runner by default. For low-level `west test`, build the updated runner
and pass `--executor` explicitly when `PATH` selects an older installed binary.

`dev run` uses `scripts/west-job.sh follow` to stream observed output and report
recognized runtime/preflight/guest stages, log paths, and last-write age.
Silence is not classified as a hang. The runner publishes its bundle when
created and automatically registers stdout/stderr with the active job.
Homebrew resources register guest build-log directories before execution.
New files, rotation, truncation and reconnect do not require manual discovery
of generated paths.

Registration is an atomic, immutable `activity-logs.d/*.logs` record under
`WEST_JOB_STATE_DIR`: NUL-delimited kind/path pairs, with `file` or `directory`
kinds and absolute host paths. Directory discovery is nonrecursive and bounded;
stdout text cannot register paths. Explicit repeatable `--activity-log` is
available on the low-level `start`/`follow` commands for additional logs.
Runner `--forward-output` is post-execution replay, not live streaming.


Guarded CTest registrations pass `--forward-output` to the executor. This replays
captured stdout/stderr after execution, preserving guest phase and domain-oracle
markers through nested watchdogs without accepting compile or transport errors
as runtime RED. Rebuild `darling-debug-runner` after updating its source; use
`--executor ../darling-debug-runner/target/release/darling-debug-runner` to select
the workspace build explicitly when `PATH` selects a different installed tool.

Keep shell scripts thin. Static source-contract scripts should source a local
`contract-test-lib.sh` helper for common `fail`, `require_grep`, and
`require_text` assertions instead of copying that boilerplate into every test.
Existing guest runtime fixture implementations may use the local
`guest-verdict-test-lib.sh` helper for copying fixtures, guest launch, bounded
`ORACLE_RC` observation and host-runner cleanup. That is framework-internal
plumbing, not an operator boot/poll recipe: invoke registered fixtures through
`west test` with its prefix lock and shutdown ownership, under `west-job` for
long agent runs. Prefer structured guest fixture/CTest metadata for new tests;
bespoke scripts such as long A0 gates are exceptions.

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
mise run west patch check --profile arch
mise run west patch check --profile arch --strict
mise run west test --profile arch --list --red-only
mise run west test --profile arch --patch darlingserver/stack-pool-empty-stack-handle.patch --list
mise run west test --profile arch --patch darlingserver/stack-pool-empty-stack-handle.patch
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

The export gate also rejects generated evidence files in a patch: JSON
snapshots, census/stat captures, handoff notes, and build-output logs. These
belong in a diagnostic archive, not in a product branch or review patch.
`west patch check --quality` reports existing violations, while
`west patch verify` and export refuse them. A large patch is acceptable only
when its source tree contains real product code/tests and the explicit
`--allow-large-output` override is reviewed; the override never permits
generated evidence.
The same gate rejects legacy `Co-Authored-By` trailers naming Claude or Codex;
those automation trailers must be removed from local commit messages before
export, while real authorship and unrelated trailers remain intact.

Some gates need a consistent patch profile rather than the developer's current
mixture of fix branches. Mark those with `requires-profile: arch` (or another
profile name). `west test` will list those tests anywhere. On execution, if the
live checkout is not already fully on `integration/<profile>`, profile-bound
metadata tests are temporarily materialized in detached worktrees; this keeps
the developer's current checkout stable while making headers, source files, and
test assets come from the intended profile.

With `--materialize-profile`, selected profile metadata tests run from temporary
detached worktrees built from the frozen West manifest and typed immutable
schema-v2 locks. For composed profiles, typed prerequisite profiles are replayed
first in dependency order. Historical patch archives and stale
`integration/<profile>` branches are not materialization inputs. The live
checkout is not switched, and list mode never materializes worktrees.

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

## Compatibility test registration

`testkit` uses CTest for discovery (`--show-only=json-v1`), labels (`-L`),
parallelism, JUnit (`--output-junit`), resource locks and setup/teardown fixtures.
Use `EXPECT_FAILURE_MARKER` on `add_compat_test()` for an expected negative
case. The shared wrapper requires both nonzero exit and the declared fixed
output marker; an unrelated compiler, launcher or timeout failure is not proof.

### One source, multiple environments

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
  DIAG       guarded         # -> route through the diagnostic executor
)
```

Labels emitted: `env:<env>`, `diag:<tier>`, `bead:<id>`, `submod:<name>`.
`env=host` builds and runs a normal local executable (plain glibc HOST tests
like the loader-reset regressions). `env=darling` is source-driven: CTest calls
the shared `testkit/scripts/run-darling-c-test.sh` helper, which uploads the C
source into the selected prefix, compiles it with the guest CLT, and runs the
guest binary through `DARLING_LAUNCHER shell`; it must not run a Linux host
binary under Darling. Upload is an explicit guest `printf` command rather than
an assumption that `shell -c` preserves host stdin; staged files live under
prefix-owned `/private/var/tmp` because guest `/tmp` may be recreated for each
launcher invocation. `env=macos` is the native differential oracle.

Every `env=darling` registration receives the framework-owned
`runtime-profile:homebrew` label by default. Product tests therefore do not
name libraries, deploy paths, or an ordinary provider: west resolves that
provider's source profile, build targets, Mach-O closure, deployment and
restore transaction. `RUNTIME_PROFILE` is an override only when the test's
subject is a different product runtime, such as the rootless E-UNION variant
or a perf-only `darlingserver` build. It is not a general dependency list.

The rootless provider also declares the guest shell it needs to execute a
guest test: `darling/src/external/bash` builds and deploys `/bin/bash`. The
prefix-baseline provider uses the same declaration, so a clean hosted runner
does not depend on an accidentally pre-populated prefix. This belongs in the
runtime profile because `shellspawn` executes `/bin/bash` inside the guest;
installing a host binary or adding an ad-hoc CI copy would hide a broken
runtime deployment.

Runtime deployment also applies the guest-safe permission policy at the
transaction boundary: deployed files lose group/world write bits and all
parents used by the deployment lose group/world write bits. This is required
for launchctl's plist validation when a build runs with a cooperative host
umask. The transaction records every changed directory mode and restores it
alongside file contents, so a failed or temporary runtime proof leaves
the prefix unchanged.

The optional bootstrap syscall trace uses `strace -D`: tracing runs detached
from the launcher wait path, so long-lived launchd/shellspawn daemons cannot
make a completed guest command look hung. The trace records their syscalls in
the diagnostic directory, and West owns lifecycle cleanup.

### Diagnostic execution tiers

Select diagnosis per test with `DIAG`:

| Tier | Wrapper | Successful payload | Failure or timeout | Default for |
| --- | --- | --- | --- | --- |
| `bare` | plain CTest execution | no executor bundle | no executor bundle | host, macos |
| `guarded` | watchdog with hard timeout and owner cleanup | command/stdout/stderr/result bundle | diagnostic bundle | darling |
| `forensic` | guarded plus `--capture-exact --capture-tree` and prefix ownership selection | guarded bundle | timeout/stall exact archive, default 512 MiB cap | opt-in per case |

Guest cleanup uses the prefix owner; generic commands use process-group
termination. Guarded/forensic execution requires a diagnostic runner and fails
before launch when one is unavailable. Text log size depends on the payload.
Exact archive limits do not bound ordinary stdout/stderr or owner shutdown.
Enable expensive tracing explicitly; do not classify silence as a hang.

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
without deletion.

A failed runtime source/build is not ordinary scratch. It is retained as one
manifested unit under `.west-test/runtime-evidence`, with its source tree,
build directory, failure reason, provider context and owned Git worktrees.
Ordinary `west test --gc` never deletes those units. Removal is deliberate:
`west test --gc --gc-runtime-evidence` applies the configured proof-scratch
age/count policy and first removes only the worktrees listed by that unit's
manifest. A path is reported as preserved only after this manifest exists.

Long configure/build commands use the same bounded process runner, forwarding
configure output and throttled Ninja progress milestones live while retaining
the complete output for failure diagnostics. A heartbeat is emitted only every
30 seconds when no output arrives. This keeps large targets such as
`rootless_bootstrap` visibly alive without turning compiler output into an
unreviewable stream. The rootless prefix bootstrap smoke uses the same live
guest executor: guest stdout/stderr is forwarded as it arrives, and every
heartbeat includes the pre-cleanup process, socket, resource-limit, and runtime
path snapshot. If the caller is
interrupted, the next `west test --gc --gc-runtime-evidence` pass removes an
unlocked orphan `.inflight-*` unit and its recorded worktrees.

Failure diagnostics that rebuild a runtime can pass
`--runtime-build-timeout-seconds` to override the profile deadline for each
configure/build phase. CI combines that framework-level limit with an outer
five-minute command deadline, so a diagnostic retry cannot consume the whole
job after the original test has already failed.

### Orchestrator — `west test` (`west_commands/test.py`)

Use these selectors from the manifest directory through `mise run west`:

```
mise run west test --all                 # full suite
mise run west test --changed             # diff submodules vs manifest-rev -> -L submod:<changed>
mise run west test --bead dar-e1j         # -L bead:dar-e1j
mise run west test --submodule xnu       # -L submod:xnu
mise run west test --env host            # restrict environment
mise run west test --env darling --prefix-profile homebrew
mise run west test --diag guarded        # restrict diagnosis tier
mise run west test --fuzz                # restrict to fuzz:* labelled jobs
mise run west test --stress              # restrict to stress:* labelled jobs
mise run west test --list                # show selection, no run
mise run west test --gc --keep-last 20 --max-bundle-mb 64 --proof-scratch-keep-last 2
mise run west test ... -j8 --output-junit r.xml   # passthrough to ctest
```

`--changed` is a local selection hint, not evidence of complete coverage.
Publication requires the owning patch's declared proof and review gates.

## Running across macOS versions

Run the same semantic case on Darling and a compatible native macOS reference.
Bind results to the actual OS build, architecture, compiler/SDK, deployment
target and fixture identity. Compile-time availability branches can require
separate builds; runtime results from one version do not establish another.

Use the source's `availability.h` and deployment-target declarations for
compile-time version gating. `add_compat_test()` accepts `MIN_VERSION` and
`MAX_VERSION` and emits a `macos:<min>-<max>` label. Inspect the actual CTest
labels before selecting with `--label`, which accepts a regex.

Use `INSTALL` and `RESOURCES` for the `testcase/` and `resource/` installed
layout. Package and transfer the bundle through the CI archive/installed
interfaces described above; preserve executable modes, links and resources.
Select the native host explicitly for `macos-ssh`; record its identity with the
result. Compare Darling against a suitable native reference using its
authoritative advertised runtime version, not fabricated SDK/version metadata.

## Colocation & upstream stance

Keep testcase sources and installed artifacts compatible with upstream
`darling-testsuite`; workspace ergonomics belong in the orchestration layer:

| Layer | Shared with upstream (the seam) | Ours (ergonomics) |
| --- | --- | --- |
| Case source | format, MIT-0, nostdlib/directsyscall, availability.h | — |
| Registration | the `add_executable+link+add_test+install` it expands to | `add_compat_test()` wrapper |
| Ship to macOS | `testcase/`+`resource/` install layout | — |
| Run | plain ctest | `west test` (changed/bead/diag/label/gc) |

`add_compat_test()` emits CMake registration and installation rules compatible
with that source and artifact layout.

- Test SOURCES use the upstream darling-testsuite format (CTest, MIT-0,
  nostdlib/directsyscall) so they import into that repo unchanged. We add cases,
  we do not fork the framework.
- ORCHESTRATION (`west test`, changed-only, beads, debug-runner) stays in this
  private workspace — Darling and darling-testsuite stay clean CTest.
- Keep publication scope and submodule pointer changes explicit in the owning PR.
- Use the CI execution contract at the start of this document for host,
  rootless guest and native installed tiers. Source-bound host cases use
  `west test --profile homebrew --materialize-profile` so they compile against
  the selected patched sources.

## Local Compatibility Suite

`testkit/` is a self-contained local compatibility suite; nothing under
`darling/` is touched. It compiles the REAL production code from the darling
checkout (auto-located as the sibling `../darling`, override with
`-DDARLING_SRC`).

- `testkit/cmake/AddCompatTest.cmake` — the `add_compat_test()` generator
  (EXTRA_SOURCES/INCLUDES/DEFINES/LIBS/WORKDIR let a case link the real code).
- `testkit/CMakeLists.txt` — compatibility case registration.
- `west_commands/test.py` — the orchestrator, registered in `west-commands.yml`.

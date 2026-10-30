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
  mise run west test --env darling --runtime-root /absolute/path/to/runtime \
  --prefix /absolute/path/to/prepared-prefix
scripts/west-job.sh follow --state-dir /absolute/path/to/new-job
# Reconnect with follow; request owner cleanup when cancellation is needed:
scripts/west-job.sh cancel --state-dir /absolute/path/to/new-job
```

Use a new job state directory for each run. Do not use log silence as proof of a
hang or kill launchd/server processes by guessed PID or broad name patterns.
Never rewrite a published installation. New code gets a new complete runtime
root; retain image identity and the lifecycle owner's failure evidence.

`darling-boot-run.sh` handles SIGINT/SIGTERM through its ordinary prefix teardown
while retaining the external lease, then exits 130/143 respectively. Cancellation
is never a workload PASS; inspect the finalized cleanup observations before
reusing the prefix, including when the owner reports a signal exit.

Noisy/long RED proofs, full guest CTest selections and contracts that invoke
`west test` internally use the same supervisor. In particular,
`tests/run-west-test-metadata-contract.sh` requires the explicit `west-job`
transport context under `CODEX_CI`. Stay attached with `follow`; observer
timeout does not cancel a job and a detached handle does not mean completion.
Use `status` only for interruption recovery, never a polling loop; agent
transports use `follow`, not low-level `wait`. Do not start another run on the
same prefix before final rc and cleanup.

Cleanup/leak assertions run as a separate final phase after all commands that
create temporary worktrees/prefixes have exited. Never parallelize
`tests/run-west-test-metadata-contract.sh`,
`tests/run-west-test-prefix-cleanup-contract.sh` or manual scratch leak checks
with `west test`, RED proof or source materialization. A failure observed during
such overlap requires sequential
execution before a leak claim.

Two or more agents that need their own prefix at the same time use an isolation
lane instead of sharing one: `west dev lane create NAME --base P --runtime-root R`,
then `west test --lane NAME --runtime-root R ...` for the run. The lane gives
the run its own copy-on-write guest state, state root,
`TMPDIR`/`XDG_RUNTIME_DIR` and process scope. Runtime publication remains
separate; concurrent runs may share a verified immutable image, never writable
guest state. The mechanism, its
measurements, its cleanup contract and its own tests are in
[lane-isolation.md](lane-isolation.md); it is optional and no existing command
changes behaviour without `--lane`.

### Xcode build workload license setup

`scripts/xcodebuild-workload-run.sh --workload minproj|iosapp|both` includes license
preparation before each build. Running these test scenarios authorizes the selected
Xcode installation's normal license acceptance; obtain the user's agreement to its
Xcode/SDK terms before invoking them. Other guest tests do not accept licenses.

The shared guest helper, `testkit/scripts/xcode-license.sh`, runs that installation's
`xcodebuild -license check`. If necessary it runs `-license accept` through
Darling's bundled guest `sudo -n`, then requires another successful check in the
ordinary guest context. Existing acceptance skips another accept command. There
is no host acceptance cache, fabricated plist or synthetic receipt: a fresh prefix
or a changed Xcode is checked by the real executable again. A failed accept or
verification prevents the project build from starting.

The prepared installation must contain Darling's Mach-O `sudo` from
`src/tools/sudo.c`, supplied by the complete normal product install. This tool
changes emulated guest credentials, not Linux host credentials, and preserves
`DEVELOPER_DIR`. The runner validates and names the installation's image explicitly
through `/Volumes/SystemRoot`; it never invokes the Linux host's `sudo` or asks
for host administrative rights. Missing guest `sudo` stops before boot rather
than falling back to a host command.
Rootless launchd must also initialize `/Library/Preferences` with mode `0755`
before workloads run. Xcode's license writer uses an atomic Foundation file write
and does not create its parent directory. Keep this prerequisite in launchd's
shared runtime initialization, not a per-test `mkdir` or fabricated license file.

The runner holds the shared external prefix lease before staging the project
and through guest execution, shutdown and evidence retention. An inherited
lease is validated before any prefix writes.

Use an already prepared, idle prefix and one managed job:

```sh
scripts/west-job.sh start --state-dir "$DW_STATE_ROOT/jobs/xcode-build" -- \
  mise exec -- bash scripts/xcodebuild-workload-run.sh \
  --runtime-root "$R" --prefix "$PREFIX" --xcode-app "$XCODE_APP" --workload both
scripts/west-job.sh follow --state-dir "$DW_STATE_ROOT/jobs/xcode-build"
```

The setup reports `XCB_LICENSE_RC`; `XCB_RC` remains the actual build's status and
is absent when setup prevented that build. `XCB_SCENARIO_RC` is the harness's
completion status, not a replacement for the build/artifact oracle. License
readiness alone never makes a workload PASS.

Each run preserves preparation logs and status separately under
`<log-dir>/<workload>.license/`, using verified zstd storage when effective. The
resolved initial license refusal is not mixed into the build's capture, where it
would falsely look like the build's terminal stop. Read compressed phase logs with
`python3 -B scripts/darling-evidence.py cat PATH`.

### Bootstrap a fresh prefix before a prefix-backed run

`--runtime-root R` selects an ordinary product installation;
`--prefix P` selects disjoint writable guest state, not an installation.
Use an absent/empty owned prefix and bootstrap separately from test selection:

```sh
mise run west darling-bootstrap --runtime-root "$R" --prefix "$P" \
  --build-dir "$B" --guest-toolchain darling-command-line-tools
mise run west test --workload WORKLOAD_ID --source-root "$S" \
  --runtime-root "$R" --prefix "$P" --env darling
```

Omit the toolchain option for runtime-only smoke. Guest C requires the real
reviewed CLT and SDK, installed through the guest installer and verified by the
provisioner. No host compiler substitution, invented SDK metadata or system-file
injection. CLT package/cache integrity and publisher authentication are distinct.

### Direct bootstrap from an explicit build directory

Configure `B` with absolute `CMAKE_INSTALL_PREFIX=R`, default
`COMPONENTS=stock`, then build ALL and perform the complete normal install.
`west darling-build --build-dir B --runtime-root R --install` publishes a new
absent/empty root; `--with-test-data` additionally installs test assets.
Bootstrap can build/install from `--build-dir B`. On a fresh image,
`--no-build` with `--build-dir B` installs already-built files; it does not skip
installation. To reuse a verified published image without rewriting it, omit
`--build-dir` and pass `--no-build`. `--smoke-timeout-seconds N` bounds smoke.

Installation layout is `R/bin/darling`, `R/bin/darlingserver`,
`R/libexec/darling`. The product launcher owns first-use guest-state initialization.
No `P/bin` discovery, runtime providers/plans, partial deploys or duplicate
system-library copies. Published roots are immutable: new code gets a new root.
The full schema-2 receipt inventories actual image files, normal install-hook
output and actual CMake manifest hashes; verification rejects extras and does
not depend on later contents of the original build directory.

Bootstrap smoke is not the workload oracle and does not imply doctor or native
acceptance. Run scoped doctor explicitly when needed. The default rootless path
does not create Linux user, mount or PID namespaces; do not treat their denial
as an ordinary rootless prerequisite failure. Attribute it to the operation that
actually requests one. Read [boot guidance](../.agents/skills/darling-boot/SKILL.md).


## CI execution contract

`.github/workflows/test-infra.yml` keeps privilege and trust boundaries explicit:

- host tests run on pull requests, pushes, schedule, and the selected manual tier;
- pull requests, pushes, and the selected manual tier run guest smoke on the
  official GitHub-hosted `ubuntu-latest` runner;
- the full rootless guest suite is a separately selected manual tier;
- the manual `macos` tier builds an installed bundle on `macos-14`, then
  configures consumption of that artifact on `macos-14`, `macos-15`, and
  `macos-26`. This configuration is not proof of a successful hosted run.

Linux guest tiers select separate `DARLING_RUNTIME_ROOT` and writable prefixes.
They prepare a normal stock ALL build and full install, then consume that image
for smoke/full/toolchain selections; no CI-only system-library injection.
The separate `guest-toolchain` and `clt-integrity` tiers retain real CLT
provisioning/integrity gates. Workflow configuration establishes no guest PASS.

For maintenance, inspect the same explicitly scoped GC request before removal:

```sh
mise run west test --gc --dry-run \
  --proof-scratch-root "$DW_STATE_ROOT/proof-scratch" \
  --runtime-evidence-root "$DW_STATE_ROOT/runtime-evidence"
```

Ordinary GC preserves failed runtime evidence. `--gc-runtime-evidence` explicitly
opts into collecting it; preserve required acceptance bytes outside these roots
with SHA-256 hashes first. Never run GC unattended or delete prefix lease files.

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

The collector normalizes flat `testkit/workloads.yml` through production
`test_manifest.load_workloads`, resolving repository names through West. Each
declaration binding is counted independently, not deduplicated by coincidental
test names. A binding is not an executable count or a PASS. Applicability review
is source-only inventory, not native execution.


The collector `scripts/audit-test-registration.py` and reviewed applicability
decisions `audits/native-test-applicability.json` are versioned in this
workspace. The latter is audit policy, not a second runtime test registry.
Reproduce from a configured West checkout with the pinned tool environment:

```sh
mise exec -- uv run --no-project --with west==1.5.0 python -B \
  scripts/audit-test-registration.py "$DW_STATE_ROOT/native-inventory"
```

The collector configures testkit for discovery only, does not build product
targets or execute tests. Every binding requires explicit applicability review,
independent of runner or coverage tier. Policy is keyed by the binding
identity, with a SHA-256 fingerprint of the normalized test
declaration and relative West project path. An unknown binding, reused name on a
new binding, or changed declaration fails before a successful snapshot is
written. Source-content/reference review is separate from this metadata
drift guard. Generated `configure.log`, `ctest.json` and
`inventory.json` stay outside version control. The snapshot records all
bindings, owners, source hashes, normalized proof/resources, applicability
and explicit unresolved prerequisites.

### Applicability review commands

Moving reviewed policy between stable workload IDs is a supported command,
not hand work. Neither command builds or executes product tests:

```sh
scripts/audit-test-registration.py report
scripts/audit-test-registration.py carry --move OLD=NEW [--move ...] [--drop KEY ...]
```

`report` is read-only. It prints every binding with its `identity_sha256` and
separately lists policy records that have no binding, bindings that have no
policy, and identity mismatches, and exits non-zero while any of them remains.
`carry` moves the policy and reason verbatim between the keys the reviewer names,
in one invocation when a re-recording moves many. Stable workload IDs do not
shift when neighboring declarations move. The reviewer always supplies the mapping: the tool never
guesses one, refuses to invent a policy for a key that had none, refuses to
carry a review across keys whose declarations differ in more than the key, and
refuses to overwrite a record the mapping names neither as a source nor as a
`--drop`. A mapping whose targets already carry the review of their current
declaration is already applied, so re-running it changes nothing.

`--workspace` selects the declarations under review and `--record` selects the
record to read or rewrite; both default to this workspace. An inconsistent
record can therefore be measured without touching the versioned file:

```sh
scripts/audit-test-registration.py report --record "$DW_STATE_ROOT/applicability-probe.json"
```

These commands maintain the metadata record only: they move decisions a reviewer
already made and never make one. A carried reason is the reviewed text, so a
declaration whose meaning changed needs a new review rather than a re-key.

The applicability review contract builds a throwaway West workspace and covers
report, carry, their refusals, and that a refused or repeated command leaves the
record unchanged. It runs against a fixture copy of the record, never
the versioned file:

```sh
tests/run-native-applicability-review-contract.sh
```

The focused host contract exercises independent workload bindings, West alias
resolution, unknown bindings, reused names, declaration drift and discovery
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
declared sources. Live-tree absence does not establish missing coverage.
Inventory discovery does not execute guest or native test cases.

### Authority, identity and selection

- Keep fixture sources and source-owned CMake/CTest registrations colocated
  with their owning project. Do not move all suites into workspace testkit.
- CTest/source registration defines the scenario, arguments, environment,
  resources, working directory, deadline and verdict. Workload metadata binds
  that case to installed prerequisites and diagnostics; self-contained negative
  oracles remain fixture-owned. Preserve legitimate direct host/model/source/build runners.
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
  Merely changing `env: darling` to `env: macos` does not change a guest runner.
- Local macOS and SSH are transports for the same native execution contract.
  Use isolated owned work directories and bounded cleanup. A Linux-built binary
  or successful shell transport is never a native Darwin reference.

### CTest selection bridge

Workload metadata can bind existing CTest registrations with `ctest-label: <label-regex>`,
`ctest-name: <exact-existing-name>`, or both for a scoped exact-name selection.
These references do not copy fixture commands or require new `name:` labels.
A reference cannot override `command`; explicit fixture runners are separate.

West configures the selected source scope and discovers CTest JSON before
filtering environment and diagnostics. `env:*` labels define the actual
variants; an unlabelled upstream registration belongs to the configured host
(`macos` on Darwin, otherwise `host`) in both metadata and direct CTest selection.
A binding's explicit `env` restricts its variants; it never relabels a
guest runner. Darling runtime ownership must use a Darling-scoped
binding, separate from a native reference to the same scenario.

The West entrypoint configures the workload bridge with its own Python
interpreter, preserving the pinned West dependency environment through CTest.
Plain CMake users must select an interpreter containing West when executing
structured workloads; unrelated native CTest cases do not import West.

`--list` configures discovery without building product targets, executing tests,
or acquiring a prefix. `--source-root S` selects prepared source inputs, not a
source-replay recipe. Both listing and execution preserve the discovered registrations. CTest
indices are local to that configured build, not persistent case IDs: existing
names and suite directories retain identity, including equal names in different
source suites. Installed transports must derive their contract from the source
registration, not persist these temporary build paths or indices.

Missing references, missing requested variants, ambiguous registrations and
unintended empty selections fail closed. Bead/submodule selectors match complete
labels, while `--label` is a user-supplied regex. Runtime and guest-toolchain
prerequisites are separate from selection; discovery does not provision them.

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

Every direct runner goes through `west_commands/test_execution.py:run_bounded`.
With `output_line` set, the child's stdout/stderr are FILES, never pipes: a
supervised command may leave a descendant behind, that descendant inherits the
capture descriptors, and closing a pipe reader when the leader exits turns the
descendant's next write into a delivered SIGPIPE (measured on this host: leader
rc 0, descendant signal 13; the same workload without `output_line` exited 0).
Live lines are read from those files with `pread` at absolute offsets, so the
reader never moves the file position the child shares. One `pump` call forwards
a fixed byte budget and a growing stream therefore cannot hold the deadline or
the heartbeat loop hostage, and a read error is reported as a collection
failure rather than as an empty stream inside a successful result. The
behavioural form of all three properties lives in
`tests/run-west-test-execution-contract.sh`.

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

Transport evidence must exercise relocation independently of the original
source/build trees, preserve arguments/environment/resources and late CTest
properties, and distinguish success, expected failure, ordinary exit, signal,
timeout, formal skip and infrastructure failure. Retain local and SSH results
separately; neither establishes a hosted matrix or product compatibility result.

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
Test metadata binds these names. Both dyld and libsystem_kernel are runtime
artifacts because dyld embeds the syscall emulation path.

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
mise run west test --env darling --runtime-root "$R" \
  --prefix /absolute/path/to/prefix --source-root "$S"
```

Run prefix-backed commands through `scripts/west-job.sh` in agent transport.
The selected ordinary installation supplies the launcher, and the owner binds
`DPREFIX` before launch and shutdown. Source selection does not change installed
runtime bytes. A timeout before fixture execution is infrastructure failure,
not semantic RED.

The CTest bridge propagates guest upload, compile, run and timeout phases.
Require the failure oracle in the guest execution phase for runtime RED;
compiler diagnostics containing the marker do not satisfy that oracle.

### Reproducible Homebrew build prerequisites

Homebrew workloads consume the complete ordinary stock installation, including
native `cut`, system OpenSSL configuration/certificates and Perl standard
libraries/XS bundles through normal product install rules. Never restore these
through per-workload component staging or guest shadow copies.

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
mise run dw dev context homebrew --runtime-root "$R" --prefix "$P"
mise run dw dev run homebrew-prepare
mise run dw dev run homebrew-preflight
mise run dw dev run exact-capture
mise run dw dev run homebrew-source
```

`mise run dw` selects the pinned mise environment. Contexts live in local West
configuration (`dev-<name>.*`), not checked-in host-specific paths. `dev context`
selects the active context; `dev run --context NAME` and explicit path flags
override it without rewriting configuration. Select the runtime explicitly;
without an explicit executor the checked-out runner may be built before execution.

### Guest runtime invocation and container constraints

The product launcher selects production rootless defaults. A necessary direct
invocation uses the installation launcher and explicit writable state:

```sh
DPREFIX="$P" "$R/bin/darling" shell /bin/bash --login -c '<command>'
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
`--cap-drop=ALL --network=none`, with host namespace prerequisites satisfied.
Mount `R` read-only at its installed path and the separate `P` read-write.

`dev run` starts a recorded West job and immediately attaches its observer.
It prints the job identity plus exact reconnect/cancel commands. `--detach`
starts without observing; `mise run dw dev follow JOB` reconnects and
`mise run dw dev cancel JOB` requests owner cleanup. `--dry-run` displays the
underlying command without building, booting, or creating a job.

Retained evidence must bind the selected installation receipt/digests, source,
guest toolchain and workload inputs; never rewrite identity markers to force reuse.
The exact-capture diagnostic owns the normal prefix lease and cleanup.
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

Validate SDK identity against its authoritative package metadata and retain
provenance evidence. The SDK must satisfy the selected runtime and Homebrew's
compatibility checks. Do not invent version metadata or disable those checks.
Record preparation, preflight, exact diagnostic and source-build results
separately in Beads and diagnostic archives; one passing scenario does not
establish success for another.


### Native applicability and migration gates

Review each binding's native applicability using
`audits/native-test-applicability.json`. `report` and `carry` in
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

Keep applicability decisions with the owning change and its Bead. Retain internal
RPC/fault/trace, DCC, overlay and prefix-lifecycle gates on Darling; expose a
separate semantic reference when the workload has a meaningful public contract.
Require compatible native-reference, bad-runtime RED and fixed-runtime GREEN
evidence through supported runners. Cut each case over completely before
removing its duplicate wrapper. Workspace-only registration changes do not
rewrite canonical source commits; source fixture changes require a
fix-branch SHA and checksum refresh.

Tie publication checks to the owning change's semantic proof and review; do not
substitute an unrelated package gate for that evidence.

## Test architecture

Use CMake/CTest as the backend and keep workspace tooling as a thin
orchestration layer. Preserve the upstream `add_test()`, installed
`darling-testsuite/{testcase,resource,manual}` layout,
`darling-testsuite-lib` assertions/resources/XML and `darling-directsyscall`
interfaces.

- Keep upstream-compatible testcase sources and install layout as the portability
  seam.
- Put local ergonomics in metadata and `west test`: `bead:*`,
  `submod:*`, `env:*`, `diag:*`, `fuzz:*`, and `stress:*` labels.
- Support five runnable kinds through the same CTest surface: host
  unit/contract tests, Darling guest/runtime tests, macOS oracle tests, external
  package/repro gates, and bounded fuzz/stress jobs.
- Default host tests to `bare`, Darling guest tests to guarded
  `darling-debug-runner`, and forensic capture to explicit opt-in.
- Treat fuzzing as a labelled bounded runner contract: seed corpus, maximum
  time, artifact bundle, replay command, and minimized failures promoted to
  normal committed regressions.
- Use `west test` selectors and metadata to identify the proof for a patch.

### Behavioral evidence and proof ownership

Choose the smallest test that executes the changed contract, then retain the
actual workload's smoke/oracle. Source matching is an audit, never a substitute
for behavior or a reason to relabel evidence as host/compile/runtime. Report
an unexecutable behavior change as source-only evidence with a blocking task
naming the deferred behavioral owner.

For build changes, exercise the changed CMake target/build path, not an
alternate manual/autotools compile. Do not inject the fixed flag through test
metadata into the bad tree. Current test assets may be copied to the bad tree;
RED must fail because of old behavior, not a missing asset.

Resolve destinations against the intended repository root; use absolute paths
outside the manifest and keep workspace-owned assets here. Product fixtures and
their CMake registrations remain with their owning source project. Keep test
inputs available independently of the old implementation being exercised.
Runtime RED/GREEN proof must preserve required boot/lifecycle dependencies;
an incompatible server's startup failure is not regression evidence. Record
exact source/build composition conflicts as blockers.

An empty `west test` selection is fatal. Never narrow a selection to hide a
failure. Declare deployment dependencies separately from CTest selection.

For real-prefix `libsystem_kernel` validation, the complete installed image must
include matching dyld: its static emulation path otherwise serves stale syscall
behavior. Build ALL, install into a new root and record the entire image's scope.
Confirm the run's installation and process identity through [diagnostic tooling](tooling.md),
not merely build success.

E-UNION host behavior has its own CTest path. `testkit/CMakeLists.txt` builds
the production XNU sources and the workspace harness as a CTest target, while
the fixture setup is a CTest fixture prepared by
`experiments/e-union/run.sh --prepare-fixture DIR`. The `eunion-host` label
selects the host suite against the prepared source chosen by `--source-root`.
Independent bad/fixed source runs must retain the same fixture and oracle.
Changing a host source input does not change an installed guest runtime. The
source-bound host suite is opt-in through
`-DDARLING_ENABLE_EUNION_HOST_SUITE=ON`; West supplies that flag only for the
separate source build. The default testkit build keeps it off; guest selection
consumes the explicit installed runtime rather than rebuilding it per test.

The standalone harness requires `XNU_SRC_ROOT` or `DARLING_SRC`. Native fixture
state lives beneath an absolute `DW_STATE_ROOT`, in short `h/` paths checked
against the real Linux Unix-socket limit. Fixture preparation refuses unknown,
symlink, differently owned and whole-state-root destinations before resetting
anything. CTest records its binary directory as the fixture owner; standalone
scratch is unique and removed by that invocation.

### What the contract census credits

Register every `tests/run-*-contract*.sh` in `CONTRACTS` and every
`tests/west_test_contracts/*contract*.py` in `EXPLICIT_CONTRACTS` in
`ci/run-host-tier.py`, or use `EXCLUDED_CONTRACTS` with a reason. The tier refuses
unaccounted contracts. Transitive credit requires a reachable invocation **by
path**: variable assignments, probe-only calls, comments, and synthesized
declarations alone do not execute a contract.

Register the contracts that a chain actually drives. A chain that depends on
unavailable inputs does not execute its leaf contracts: register reachable
contracts independently or record the explicit exclusion and blocker.

A resolved metadata `ctest-label:` binding may select an exact CTest index.
Compare the selected cases, not a literal `-L` spelling in a command listing.
Never pin derived trees, generated-file digests or replay boundaries to literal
values: bind assertions to their authoritative inputs and use the deriving
gate's `--check` mode.

After editing reviewed namespace-writer candidate sources, run
`scripts/generate_namespace_writer_registry.py --check` and regenerate the
anchored registry before a tier: source SHA drift invalidates that audit.

### Scheduling the host tier by weight

Contracts that drive other runners declare their measured slot cost in
`CONTRACT_WEIGHTS` in `ci/run-host-tier.py`. Preserve weighted scheduling rather
than treating every process as one worker; fix oversubscription, never mask it
with retries.

A contract that tests tier wiring must stub registered contract paths in a
throwaway mirror and assert captured commands, not recursively run the real
tier. `tests/run-ci-test-tiers-contract.sh` implements that pattern.

### Test metadata and runners

Shipping runtime API regressions use `guest-c-fixture`: the reviewed guest CLT
compiles the selected source, and the ordinary installed launcher executes it
under the shared prefix owner. `backtrace-async` exercises the real libc and
libpthread against x86_64 Swift extended-frame inputs, including bounded writes,
continuation addresses and task identity:

```sh
mise run west -- test --source-root "$S" --runtime-root "$R" --prefix "$P" \
  --env darling --workload backtrace-async
```

`--workload os-log-enablement` exercises the installed logging API with real
Clang Blocks and encoded `os_log` emissions: type ordering, hook filtering,
reentrancy and replacement ownership, private/public and immutable/writable
string handling, stable message snapshots, caller-owned copies and invalid-input
traps. Its failure diagnostics print the actual and expected message values.
Native semantic references and guest execution are independent evidence.

`--workload kprintf-logging` checks actual delivery of the private kernel
diagnostic payload into the fresh server-log suffix, including whitespace
trimming. Its declaration sets the existing host `DSERVER_LOG_LEVEL=info`
before server startup; ordinary production logging defaults are unchanged.
Do not reuse a running server with a different logging configuration.

`--workload unix-sockaddr-boundary` uses the launcher-published prefix to test
104/107-byte Linux-expanded socket names, rejection at 108 without truncation,
stream/datagram roundtrips and caller-capacity guards. It needs an owned writable
guest prefix short enough to construct those boundary inputs. This is a
Darling host-expansion gate, not a native macOS pathname-capacity claim.

E-UNION cases validate immutable lower inputs from the optional product
`tests-data` component in `R`. The typed `darling-eunion-prefix` resource stages
only per-case upper state in `P`; it never installs or replaces runtime files.
Flat workload fixture fields keep those assertions and upper inputs declarative:

- `template-files` and `template-symlinks` assert the already installed lower
  inputs; `upper-files` describes per-case entries to create in `P`;
- `cleanup-dirs` bounds per-test state to `/private/var/tmp/west-*` paths;
- `forbid-template-paths` asserts that a mutating guest operation did not write
  into the immutable template;
- `require-upper-paths` asserts that the expected object was materialized in
  the writable upper layer;
- `verify-template-files-after` checks template contents, modes, and xattrs
  after the runtime is shut down.

Lower-file `xattrs` values are either UTF-8 strings or strict `{base64: "..." }`
mappings for byte-exact binary values. `absent-xattrs` accepts only a native
missing-attribute result; permission, unsupported-namespace and other read errors
fail the oracle rather than being treated as absence.

The first two oracle lists are checked while Darling is still alive, because
runtime shutdown removes its private sockets and other runtime-created nodes.
Template-file assertions run after shutdown and before fixture cleanup. A
template symlink target containing `..` is rejected by default; use the
explicit `allow-parent-target: true` only for a test whose purpose is to check
symlink containment or rejected escape behavior.

Declarations live in flat `testkit/workloads.yml`. Preserve stable workload IDs,
runner arguments, environment, resources, fixtures, timeouts and exact oracles.
Source revisions are selected explicitly, not composed by workload metadata.

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
Set `coverage-tier` explicitly when a conservative classification would obscure
an intentional distinction, especially for `model`.

`red: true` does **not** mean a regression should fail on the selected fixed
checkout. A negative oracle must be explicit and fail for the intended domain
symptom; retain its output alongside the fixed-path result.

The flat catalog accepts only `red-proof: {mode: self, why-self: ...}`:
the fixture itself exercises a known-bad/model arm and requires its rejection
before exercising the fixed arm. Metadata does not create that oracle.
The current `west test` parser has no `--prove-red` command-line option and
the catalog rejects source-base/runtime proof recipes. Do not revive profiles,
current-minus reconstruction, per-test artifact swaps or source replay.

For real bad/fixed comparison, prepare the source inputs independently and run
the same current fixture against each explicitly selected source or installation.
Runtime RED requires genuinely known-bad and fixed complete immutable images,
with isolated owned prefix lifecycles. Source-root selection alone changes no
installed bytes. Report an unsupported comparison as a limitation rather than
silently replacing it with source matching.

RED must fail for the intended errno, trace marker, output or other behavioral
symptom after fixture execution. Missing source, upload/compile failure,
namespace/RPC/shellspawn failure before `main`, and an unexpectedly passing
bad runtime are not RED. Preserve the current fixture independently of old source.
Record explicit nondefault build defines and paired source modules. In
particular, libsystem_kernel consumes darlingserver-generated RPC inputs and
dyld embeds emulation; unrelated build failures must not masquerade as RED.

Source/text checks are allowed only as auxiliary drift guards:

```yaml
  - id: vchroot_fdless_rpc_invariant_contract
    module: darling/src/external/darlingserver
    kind: source-contract
    coverage-tier: source
    env: host
    diag: bare
    runner: python
    repo: darling-workspace
    source-env: DSERVER_SRC_ROOT
    script: tests/vchroot_fdless_rpc_invariant_contract.py
```

`kind: source-contract` can prove that a hunk/symbol/comment is present or absent
on a source tree, but it does not prove runtime behavior, so it does **not**
count as behavioral coverage. Report a source-only audit separately and identify
the missing behavioral host/guest/build/package/fuzz/stress gate and its owner.

Use structured runners for common cases:

```yaml
  - id: stack_pool_tests_run
    module: darling/src/external/darlingserver
    kind: contract
    env: host
    diag: bare
    runner: west-build
    target: dserver_stack_pool_tests_run
```

Script tests may declare arguments and environment without dropping to a shell:

```yaml
  - id: a0_gate_full_strict
    module: darling/src/external/darlingserver
    kind: guest
    env: darling
    diag: guarded
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
  - id: progress_classifier
    module: darling/src/external/darlingserver
    kind: contract
    env: host
    diag: bare
    red: true
    red-proof:
      mode: self
      why-self: The fixture owns explicit negative classifier cases.
    runner: python
    script: tests/progress_classifier_test.py
```

DCC cache guest tests should use the structured `dcc-cache` resource instead of
building cache files in ad hoc shell. The resource compiles the declared cache
builder, creates the cache under `/private/var/tmp`, exports the configured
guest environment variables, and removes the host cache directory after the
test. Tool assets are selected independently from runtime identity; do not
inject system libraries into guest state or mutate the immutable installation.
Prefix-relative writable cache files belong to `P`, not `R/libexec/darling`.

`west test` provisions structured resources through typed providers, not
runner-local ad hoc setup. The current provider stack is ordered as:

1. `host-temp-files`: prepares declared prefix-relative host files and exports
   their paths to host-launched fixtures.
2. `host-trace-files`: prepares prefix-relative host trace paths and exports
   their environment variables for host-launched fixtures.
3. `host-stat-deltas`: binds and preflights the host `darling-stat` tool used
   by guest runtime fixtures that assert before/after counter deltas.
4. `descriptor-trace`: allocates the workspace-scoped host capture directory for
   a guest fixture's RPC descriptor-transport observation and validates its
   declared windows.
5. `dcc-cache`: materializes/builds the declared cache tooling and injects the
   guest DCC environment.
6. `darling-eunion-prefix`: boots/verifies the E-UNION prefix and stages
   upper/lower fixture files.
7. `homebrew-lz4`: restores a verified stock-stack snapshot or stages pinned
   source-build inputs, then captures a completed stack when enabled.

Provider order is part of the contract: host observation paths, stat tools and
descriptor captures are prepared before cache and prefix fixture setup.
Keep shared fixture setup with its typed provider and focused contract, not
duplicated in individual runner bodies. These are fixture-resource providers,
not retired runtime-image providers; compiler provisioning is explicit bootstrap.

Typed resources own fixture data and cleanup, not runtime publication.
Bad/fixed runtime comparison consumes complete separately published images;
source-root redirection alone cannot establish a runtime change.

### Cache identity and fresh acceptance

Caches accelerate iteration, not acceptance claims. Never widen identity
matching or consume an entry without its completion marker.

| Store (under `<manifest>/.west-test/`) | Controls | Required identity/integrity |
| --- | --- | --- |
| `stock-stack-cache` | `WEST_STOCK_STACK_CACHE_DIR`, `WEST_STOCK_STACK_CACHE=off`, `WEST_STOCK_STACK_CACHE_MAX_BYTES` (default 12 GiB, enforced before capture) | Pinned brew/core revisions and input digests, resolved formula versions, verified guest CLT, deployed runtime, layout schema, host architecture |
| Guest verdicts | `WEST_TEST_VERDICT_CACHE=off` forces execution | Asset bytes, declaration, runner/args, timeout/oracles, runtime, consumed stock snapshot, workload parameters, guest toolchain, host architecture |

Stock captures are built at temporary paths and published with completion
markers. Missing markers, corrupt archives, SHA-256 mismatches or missing
formula source receipts are misses, never partially trusted restores. A miss
stages pinned resources and rebuilds; an already installed stack is adopted
instead because staging refuses that prefix. Replay tests declare `homebrew-lz4`
and receive restore, adoption or staging in that order.

When the test measures the source build, set `WEST_STOCK_STACK_RESTORE=off` in
its own `env-vars` so it stages and builds rather than restoring. Leave the
store enabled to capture the completed stack for later replay, or disable the
whole stock cache explicitly for acceptance that requires it and retain the
reported decision. Cache controls use invocation environment overlaid on host
environment. Source-build measurements always execute: their verdicts are
never reused (`skipped-source`), because receipts cannot distinguish a genuine
fresh build from restored/skipped work.

The stock wget-repeat workload reinstalls wget from source **twelve times**.
`WEST_STOCK_WGET_ITERATIONS` is iteration-only: shortened runs print
`STOCK_WGET_REPEAT iterations=N acceptance=0` and are not acceptance evidence.
Do not put a compile cache in front of the guest compiler; the source build is
the measured workload and receipts cannot detect a cached compile.

Only zero guest verdicts may be reused; announce identity digest and original
run time. Nonzero verdicts re-execute. A reused pass cannot detect flakes:
acceptance requires fresh execution on the current identity, with verdict reuse
disabled. Smaller load-smoke workloads (concurrent guest CLT compile/link,
fork/exec storms, descriptor churn) are regression signals, not substitutes
for full stock acceptance.

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
`strace` fails the run instead of skipping the observation. The capture must
start with the owned workload; attaching after an earlier prepare phase cannot
establish a complete descriptor-transfer window.

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
  - id: select_fdset_conversion
    module: darling/src/external/xnu
    kind: unit
    env: host
    diag: bare
    runner: c-fixture
    source-env: XNU_SRC_ROOT
    script: tests/select_fdset_contract.c
    include-dirs:
    - darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/select
    compile-flags: [-std=gnu11, -Wall, -Wextra, -Werror]
```

`c-fixture` compiles the fixture from the declared asset checkout. A
`source-env` binds its source input to the selected module under
`--source-root S`; it is not an instruction to reconstruct another revision.
`stub-headers` may list empty generated headers for isolated production
`.c` unit tests that include project-local headers not needed by the fixture.

Use `runner: source-contract-script` for workspace-hosted shell audits against
the selected source. `west test` sets the declared `source-env` to that module
under the prepared `--source-root S`. `source-script-fixture` instead executes
the source-owned `source-script` with its explicit `cases`, against that same
selected source. These runners are source-audit evidence by default, not an
automatic bad/fixed replay or runtime proof. When comparing independent trees,
keep current fixture inputs and paired modules available in both; a missing new
fixture in old source is not RED.
Plain `runner: script` remains an escape hatch for special process/trace
orchestration; prefer structured runners and typed resources.

Use `runner: self-contract-script` for host scripts whose RED proof is fully
self-contained in the test itself: the script runs an explicit bad/model arm and
requires it to fail, then runs the fixed/current arm and requires it to pass.
These tests must declare `red: true` and `red-proof: {mode: self, why-self: ...}`.

Use `runner: guest-runtime-script` only for guest/runtime orchestration that the
structured guest fixture cannot express yet: multi-process gates, dserverdbg
oracles, prefix trace-file checks, or process-lifetime probes. It must declare
`env: darling`; West owns declared prefix resources and trace/temp cleanup.

`env: darling` selects guest-runtime execution and requires an installed runtime
and owned prefix. Declare `requires: [darling-prefix]` for host controllers
that need prefix state. Select `--runtime-root R` and `--prefix P`; use explicit
typed fixture fields for additional writable state. Discovery does not
provision resources. Preserve every runner's args, environment and phase oracle.

If a run reports missing boot or compiler prerequisites, distinguish initial
provisioning from repair. Bootstrap an absent/empty owned `P` against the
selected installation, using `--guest-toolchain darling-command-line-tools`
when guest compilation is needed. This is a bootstrap CLI option, not a workload
metadata field; flat guest-C workloads derive compiler requirements but do not
install CLT automatically.

For an existing idle prefix, inspect before repairing:

```sh
mise run west darling-prefix-repair --prefix "$P" --check
mise run west darling-prefix-repair --prefix "$P"
```

Repair creates required runtime/tmp parents and restores canonical
`CommandLineTools`/`DarlingCLT` clang links from versioned CLT already installed
in the prefix. Guest tmp directories retain mode `1777`; runtime database
parents use `0755` and remain owned by normal launchd bootstrap. Repair cannot
install a missing CLT package or manufacture SDK identity.

The explicit bootstrap provisioner checks compiler/SDK prerequisites and stages
reviewed package inputs for the guest `/usr/bin/installer`. Catalog input
verification includes the official HTTPS host, package size, XAR boundaries,
the API SHA-1 of the **compressed XAR table of contents**, and the reviewed
whole-package SHA-256. `DARLING_CLT_PACKAGE` selects the one pinned local
CLT 13.2 package; its whole-file SHA-256 is checked before and after staging.
Neither digest establishes publisher authentication. Preserve provenance review
separately; package bytes remain in the external cache and prefix staging is
removed after installation.
`west test` and
`west darling-doctor` share the same prerequisite checks, so a repaired prefix
is checked against the same contract that guest metadata tests require.
`--cleanup-mounts` can unmount stale filesystems and terminate identified mount
holders. Stop the previous lifecycle owner first and inspect
`--check --cleanup-mounts` before mutation. A remaining process or mount keeps
cleanup incomplete; a cleanup rescue cannot make the earlier run PASS.

`west darling-doctor --scope workspace` checks manifest drift;
`--scope runtime --runtime-root R --prefix P --build-dir B` checks selected
configuration, image receipt/integrity and guest-state prerequisites. Default
`all` combines scopes; `--boot-smoke` explicitly launches a guest and requires
ownership. A doctor pass is not a workload verdict.

Preserve runtime failure records, including server/RPC/launchd/guest fd logs
and explicit missing-item reasons. Heavy syscall/stack captures remain opt-in;
instrumented execution can perturb behavior and is not interchangeable with
the untraced arm. Never upgrade an incomplete archive to complete evidence.

The legacy `darling-rootless-debug-cleanup` helper recognizes only disposable
debug trees directly under `/tmp`, not canonical durable-state prefixes or
general source worktrees. It is not a reason to create new prefixes there.
For one existing owned legacy tree, use `west darling-rootless-debug-cleanup
--path ABS --dry-run`; never pass a glob. It refuses detected live processes
and mounts. `--sudo` is only for the same explicit path after a reported
ownership failure; never broadly delete prefixes or worktrees.

For guest-runtime execution, West owns an external persistent per-prefix sibling lease
through shutdown and cleanup, distinct from the product's `P/.lifecycle.lock`.
Never delete that lease file or normalize raw prefix spelling before product
safety checks. The owner calls `darling shutdown` for the
selected prefix, then stops matching leftover server and rootless guest
processes. Ownership requires runtime identity tied to the selected prefix:
the process's retained directory descriptor must match its device/inode, or
its actual runtime mappings must identify that prefix. Shared executable paths,
argv and environment strings alone are insufficient. Observer ancestors and
the FUSE daemon serving a lane are not guest processes to terminate.
Leftover processes make the run fail even if the payload passed. Pass
`--keep-prefix-running` only when intentionally keeping the prefix warm for a
manual debug loop.

Nested lifecycle controllers borrow an actual inherited, exclusively locked
descriptor; `WEST_TEST_PREFIX_LEASE` by itself grants no ownership. The shared
owner validates the raw prefix, lease-file inode and current-user exclusive
`flock`. CTest forwards the descriptor explicitly; actual runtime children do
not inherit it. Never synthesize lease markers to bypass acquisition.

The CTest workload bridge borrows exclusion, not a previous workload's running
server. Each structured guest workload resets and shuts down its runtime even
under a suite lease; its declared server environment therefore applies before
startup. Only an explicit `--keep-prefix-running` preserves the warm runtime,
including its original startup configuration, for a manual debug loop.

Cleanup rescue can make a failed run safe, but never converts it into a clean
PASS. Acceptance requires normal product shutdown plus zero owned processes
and mounts.

For test metadata, `diag: guarded` and `diag: forensic` are enforced by
`west test`, not by each script. `guarded` wraps the structured invocation in
`darling-debug-runner run --timeout-seconds ...`, writes a small debug bundle,
and uses prefix-owned shutdown for guest commands (process-group termination for
generic commands). `forensic` adds `--capture-exact --capture-tree` before the
West owner shuts down the selected guest prefix. Generic runner `run` does not
own guest lifecycle; the managed inner command or prefix-aware mode must do so.
The runner is resolved from `--executor`, `PATH`, or the checked-out
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

### Framework ownership and focused contracts

Keep `west_commands/test.py` as the side-effecting orchestrator, not another
implementation of helper logic:

| Owner | Contract |
| --- | --- |
| `west_commands/test_guest_c.py` | Metadata guest-C generation/execution; `DarlingTest._run_guest_c_fixture()` stays a thin facade |
| `west_commands/runtime_install.py` | Normal complete immutable build/install, runtime resolution and image identity |
| `west_commands/test_prefix.py` | Pure process-tree matching and stale init-pid decisions; orchestration owns shutdown, mounts, locks and signals |
| `west_commands/test_ctest.py` | CTest label/list/passthrough construction and submodule normalization |
| `west_commands/test_cmake.py` | Source CMake superproject shims, fallback registration, compiler logs and required options |
| `west_commands/test_resources.py` | Shared typed runtime setup, with provider ordering/selection contracts |

Update the matching focused contract when changing these interfaces (all
entrypoints below are under `tests/`):

| Interface | Focused contract |
| --- | --- |
| Testkit registration / top-level selectors | `run-west-test-testkit-contract.sh` |
| `add_compat_test()` launch, argv, labels, prefix environment | `run-west-test-add-compat-cmake-contract.sh` |
| `DARLING_TEST_EXECUTOR`, bundle roots, timeout wrapping | `run-west-test-guarded-timeout-contract.sh` |
| Guest-command environment, timeout, result matching | `run-west-test-guest-command-contract.sh` |
| Guest-C source transport, compile/run/verdict | `run-darling-c-test-contract.sh` |
| Complete runtime installation / bootstrap | `west_test_contracts/runtime_install_contract.py`, `west_test_contracts/darling_bootstrap_contract.py` |
| GC, proof scratch naming, dry-run pruning | `run-west-test-gc-contract.sh` |

Both metadata and CTest guest-C paths route low-level `launcher shell` through
`testkit/scripts/darling-guest-shell.sh`, which owns prefix environment and the
watchdog. Metadata owns namespace retry, trace/stat resources and diagnostic
dumps around that shared transport.

The testkit and guest-C shell contracts print the failing command and preserve
their failure artifacts instead of deleting the only diagnostic capture.

For a requested working-tree Python syntax check, use
`python3 -B scripts/check_python_syntax.py <paths...>`, not `python -m py_compile`,
which leaves ignored `__pycache__` artifacts.

Use CTest bindings once the test is discoverable through the source-owned
registry. For example, this flat workload entry selects the existing host
E-UNION suite without copying its fixture command:

```yaml
  - id: eunion_hardening_host_suite
    module: darling/src/external/xnu
    kind: unit
    coverage-tier: host
    env: host
    diag: bare
    ctest-label: eunion-host
    ctest-name: host/eunion_hardening_host_suite
```

For source-owned target builds, `runner: darling-cmake-target-fixture` keeps
`source-dir`, `target`, `ctest-label` and required build options explicit in the
flat declaration. It builds the selected source in an isolated superproject.
Current fixture files and fallback target registration can keep an independent
bad/fixed comparison meaningful when old source lacked the registration; they
do not authorize source reconstruction or insert the fix into the bad build.

The CTest backend command construction is deliberately small and separate from
fixture-resource orchestration. `west_commands/test_ctest.py` owns `ctest`
argument building for workload bindings and top-level selectors
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
`runner/script`, `target`, or `ctest-label` so `west test` owns how tests
are launched, filtered, deduplicated, and eventually wrapped by diagnostics.
`west test` validates the script path against the actual
checkout immediately before running.

If a non-documentation change cannot carry a behavioral regression, record the
specific limitation and deferred gate in the owning Bead. A source audit or a
metadata annotation does not waive the missing execution.

For bounded diagnostic A/B, configure separate explicit builds and publish
separate complete images. Record nondefault CMake defines and source identities;
never change install identity or inject flags into workload declarations merely
to make a diagnosis pass.

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
launcher invocation. A fixture may also declare extra object files or static
archives to link into its in-guest compile with a repeated `--link-file PATH`:
each host path must exist (a missing one fails before any guest command and
names the path), is copied host-side into the same prefix-owned directory as the
source -- the prefix tree is host-visible, and the real guest has no decoder, so
no transfer through the guest is involved -- and is appended to the guest
compile line after the source, so an unresolved symbol fails at the compile stage
rather than at run time. `env=macos`
is the native differential oracle.

Guest registrations consume the explicit installed runtime; they do not select
source profiles, component copy lists or temporary library swaps. The normal
stock image supplies its guest `/bin/bash` and system dependencies. Preserve
normal install permissions and launchctl's plist safety requirements; do not
hide an incomplete image with a host binary or ad-hoc CI copy.

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
| `forensic` | guarded plus `--capture-exact --capture-tree` | guarded bundle | timeout/stall exact archive, default 512 MiB cap | opt-in per case |

Guest cleanup belongs to the managed prefix owner; generic runner `run`
supervises the host command/process group without adopting ambient guest state.
Metadata guarded/forensic execution requires a diagnostic runner and fails
before launch when one is unavailable. `add_compat_test()` differs: without a
configured executor it warns and relabels the registration `bare`. Inspect
resolved labels and actual execution before claiming diagnostic protection.
Text log size depends on the payload; exact archive limits do not bound ordinary
stdout/stderr or owner shutdown. Enable expensive tracing explicitly; do not
classify silence as a hang.

Storage is bounded by GC: `west test --gc` keeps the newest `--keep-last N`
bundles and drops any over `--max-bundle-mb` (catches stray forensic
cores/traces). It also prunes stale runtime and proof scratch dirs older
than `--proof-scratch-max-age-hours` and retains at most
`--proof-scratch-keep-last N` fresh scratch dirs. The count cap matters after
a string of recent build failures: preserved CTest source/build trees cannot
silently fill the disk before the age threshold expires. Each GC run reports
every retained or pruned scratch directory with its path, size, age, and
retention reason; symlinks are deliberately ignored, so cleanup cannot follow
a matching name into a canonical worktree. Use `--dry-run` to inspect the plan
without deletion.

A failed runtime execution is retained as one manifested unit under
`.west-test/runtime-evidence`, with the original failure, provider context and
closed diagnostics. Finalized payloads are stored as verified `.zst` files when
effective; readable `evidence-index.json` files preserve logical paths and hashes.
Manifest-native units retain the source inventory, `CMakeCache.txt`,
`build-artifacts.json`, produced runtime images and compiler logs/core dumps, then discard reproducible
build objects/resources. Images remain usable for symbolization. An archival
failure preserves the remaining raw state and is reported, not treated as success.
Older units may still carry full build/source trees and owned Git worktrees.
Ordinary `west test --gc` never deletes these units. Explicit
`--gc-runtime-evidence` applies the proof-scratch age/count policy and removes only
the worktrees recorded by the selected unit. A path is reported as preserved only
after its manifest exists.

Before removing an old full unit, use
`python3 -B scripts/darling-evidence.py compact-runtime UNIT --archive-root DIR`
to retain its diagnostics, provenance and content-deduplicated runtime images.
The archive must be outside GC roots; verify its SHA-256 manifests before GC.
Generated objects and source worktrees are omitted deliberately, not claimed as
archived. See [finalized evidence storage](tooling.md#finalized-evidence-storage).

Runtime build-cache maintenance is a separate opt-in:
`west test --gc --gc-runtime-build-cache --runtime-build-cache-root DIR`.
Inspect `--dry-run` first. It removes idle entries with valid ownership receipts,
unregisters nested Git worktrees through the framework and reports actual errors.
Producer/Git locks, unexpected source edits or commits, unknown directories and
symlinks are preserved. Source and build-use locks span their consuming context,
including deployment; ordinary bounded eviction uses the same safe disposal.

GC requires ownership, not just a matching name. Runtime evidence is owned by
its manifest or an in-flight unit marker with released flock; manifests are
written while hidden and then atomically published. Debug bundles use timestamp
names; scratch and job state require `.west-test-scratch.json`, created through
`test_store.owned_scratch_dir` before population. Unmarked name matches,
including guest-runner outputs, are reported with path/size and preserved.
Announce unit deletion before and after removal.

For GC fixtures, pass **both** `--runtime-evidence-root DIR` and
`--proof-scratch-root DIR`: the latter scopes only scratch/guest-runner passes,
not the default `<topdir>/.west-test/runtime-evidence` store. Inspect `--dry-run`
and snapshot affected paths before mutation. Preserve required acceptance
archives outside GC roots with a SHA-256 manifest; a surviving hash/verdict
alone is not independently re-verifiable evidence and requires a new gate run.
Use one declared `DW_STATE_ROOT` per lane/task; maintenance must refuse root
overrides outside it. Durable prefixes, lanes, jobs and evidence do not belong
under host `/tmp`; see [state ownership](environment.md).

Retain complete configure/build output and bounded phase deadlines without
treating progress or heartbeat output as success. Bootstrap smoke is setup,
not the workload oracle. Interrupted evidence units remain lifecycle-owned:
an explicit `west test --gc --gc-runtime-evidence` pass may remove an unlocked
orphan `.inflight-*` unit and its recorded worktrees under the same ownership
and preservation rules.

Bound diagnostic configure/build phases explicitly and retain timeout evidence.
A diagnostic retry is not fresh acceptance and cannot erase the original failure.

### Orchestrator — `west test` (`west_commands/test.py`)

Use these selectors from the manifest directory through `mise run west`:

```
mise run west test --all                 # full suite
mise run west test --changed             # diff submodules vs manifest-rev -> -L submod:<changed>
mise run west test --bead dar-e1j         # -L bead:dar-e1j
mise run west test --submodule xnu       # -L submod:xnu
mise run west test --env host            # restrict environment
mise run west test --env darling --runtime-root "$R" --prefix "$P"
mise run west test --workload WORKLOAD_ID --source-root "$S" --runtime-root "$R" --prefix "$P"
mise run west test --diag guarded        # restrict diagnosis tier
mise run west test --fuzz                # restrict to fuzz:* labelled jobs
mise run west test --stress              # restrict to stress:* labelled jobs
mise run west test --list                # show selection, no run
mise run west test --gc --keep-last 20 --max-bundle-mb 64 --proof-scratch-keep-last 2
mise run west test ... -j8 --output-junit r.xml   # passthrough to ctest
```

`--changed` is a local selection hint, not evidence of complete coverage.
Publication requires the owning change's complete behavioral proof and review gates.

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
- Keep publication scope and submodule pointer changes explicit in the owning change.
- Use the CI execution contract at the start of this document for host,
  rootless guest and native installed tiers. Source-bound host cases run
  through `west test` so they compile against the checked-out sources.

## Local Compatibility Suite

`testkit/` is a self-contained local compatibility suite; nothing under
`darling/` is touched. It compiles the REAL production code from the darling
checkout (auto-located as the sibling `../darling`, override with
`-DDARLING_SRC`).

- `testkit/cmake/AddCompatTest.cmake` — the `add_compat_test()` generator
  (EXTRA_SOURCES/INCLUDES/DEFINES/LIBS/WORKDIR let a case link the real code).
- `testkit/CMakeLists.txt` — compatibility case registration.
- `west_commands/test.py` — the orchestrator, registered in `west-commands.yml`.

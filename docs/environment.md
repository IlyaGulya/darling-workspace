# Environment (one canonical workspace, one iOS experiment area)

Status of this note: normative for "where do I work". It records paths and rules, not history.

## Canonical product workspace

    /home/ilyagulya/work/wr-fresh

The surrounding West workspace owns canonical source. Preserve foreign dirty
checkouts; a prepared clean derived `fix/*` source worktree is a valid build
input, not a second source authority. Record its exact revision and gitlink closure.

Run orchestration and West commands from the manifest directory, not the workspace root:

    cd /home/ilyagulya/work/wr-fresh/darling-workspace
    mise run west -- status
    mise run dw dev ...

Build paths:

    .west-test/runtime-build-cache    framework-managed, identity-keyed runtime builds.
                                      Never `rm -rf` it: entries may be registered Git worktrees.
                                      Use explicit ownership-aware build-cache GC below.
    build/toolchain                   the iOS toolchain build (configured, reusable).

Source checkouts, Git history, private inputs, toolchain/build forests and
active build caches are protected inputs, never retention candidates. Only a
new normal installation created inside an explicit `DW_STATE_ROOT` enters
managed image retention; unregistered, legacy or outside roots are never
adopted or removed.

## Host tools and agent activation

`mise.toml` is the canonical host CLI environment, selecting Python 3.14.8 and
uv 0.8.17. Review it, then run these commands from the manifest:

```sh
$HOME/.local/bin/mise trust
$HOME/.local/bin/mise install
$HOME/.local/bin/mise run host-setup
```

From the West root use `mise -C darling-workspace` with the same commands.
`host-setup` explicitly runs `uv sync --frozen`; it is not an install hook.
The nonpackaged host project uses one standard `.venv`, activated by mise for
shell Python, West, dw and lane-tool tasks. `UV_PYTHON` points uv to the
project-selected mise interpreter rather than a global Python. Direct host
Python dependency pins, including West 1.5.0, PyYAML 6.0.2 and jsonschema
4.23.0, are authoritative in `pyproject.toml`; `uv.lock` records their complete
locked resolution.
After a dependency change, update the lock deliberately before frozen setup.
Inspect an existing `.venv` and preserve it with its owner if it belongs to
unrelated work before syncing; do not overwrite or delete another owner's
environment. Do not install another copy of a tool merely because the project
environment is not active.

Linux process supervision requires a kernel with pidfd support and a Python
build exposing both `os.pidfd_open` and `signal.pidfd_send_signal`. A Python
version number alone does not establish those build capabilities; a missing
API is a host prerequisite failure, not permission to weaken cleanup safety.

Use `mise run dw ...` for managed orchestration and `mise run west ...` for West
commands. From the West root add `-C darling-workspace`. Bare West commands in
documentation are shorthand for that task. Put `--` after the task name when
forwarding opaque arguments or a literal `:::`; otherwise mise parses task
separation. Do not depend on globally selected shims or user-global uv/west.

The project adds `$HOME/.local/bin` to PATH so child commands can discover mise
and Beads' `br`; install/provide those tools there before use if absent.
West itself comes from the project `.venv`, not a user-global or pipx install.
For separate, genuinely isolated tooling use `mise exec -- uv venv PATH` and
`mise exec -- uv pip install --python PATH/bin/python ...`, not system pip,
`python3 -m venv` or ensurepip. Such environments do not replace the locked
host project environment.

Rootless COW lanes use the pinned upstream containerd fuse-overlayfs snapshotter
and the exact reviewed `fuse-overlayfs` workspace-tool revision in `west.yml`,
plus host `/usr/bin/ctr` from containerd v2.2.2, util-linux `flock`,
`mount.fuse3`, `fusermount3`, `/dev/fuse` and the user systemd manager.
Snapshotter release v2.1.7, source commit
`da57796c7d0a2b608abf173651cf148706e33337`, is pinned for Linux amd64 only.
Other hosts require a separately reviewed upstream release/platform and hash;
there is no system snapshotter or alternate storage backend fallback.
The canonical FUSE host-tool source is `<topdir>/fuse-overlayfs`, separate from
Darling product source. It is a reviewed local hard fork of the Rust provider;
system 1.x and the unmodified upstream Rust revision are not substitutes.
Cargo/Rust 1.88 or newer and network access to locked Cargo dependencies are
provider build prerequisites. These are host lane dependencies, not ordinary
product launch dependencies.

After `mise run host-setup`, provide the exact reviewed West commit in that
checkout, then run `mise run lane-provider-build`. For unpublished hard-fork commits a
clean machine needs the reviewed source Git bundle/commit from its owner;
the upstream URL alone cannot fetch an unpublished fix. Import that bundle
into the declared checkout and select the exact `west.yml` revision. Do not
replace it with upstream HEAD, a Cargo cache checkout or modified upstream
bytes labelled with the old revision.

The explicit task archives the pinned commit (not working-tree edits), builds
with `cargo build --locked --release`, and publishes beneath
`<topdir>/.west-state/workspace-tools/fuse-overlayfs/<revision>/`. Its
`provenance.json` records source revision/path, lockfile and executable SHA-256,
and compiler versions. `mise run lane-provider-path` verifies and prints the
selected absolute executable. Lane creation uses that resolver regardless of
PATH and fails with the build prerequisite if absent or mismatched. Existing
installations are never overwritten; an invalid existing installation requires
owner investigation rather than forced rebuilding over a live provider.
Use a new pinned revision for changed source. Never rewrite an active provider,
lane base or published runtime image.

After building the reviewed provider and providing the host ctr client, run
`mise run lane-snapshotter-install`. This explicit setup downloads the upstream
`containerd-fuse-overlayfs-2.1.7-linux-amd64.tar.gz` release archive, verifies
SHA-256 `d54148043c22381af89cec2a167431e40668716404a1eb682ca69dfb890376f3`,
and accepts only its single regular `containerd-fuse-overlayfs-grpc` member.
To reuse an existing download, pass
`mise run lane-snapshotter-install -- --archive /absolute/path/release.tar.gz`;
the same archive and extracted-binary hash checks are mandatory. No Go build,
new dependency manager, host-tool replacement or lane-launch download occurs.
The immutable installation is published atomically beneath
`<topdir>/.west-state/workspace-tools/containerd-fuse-overlayfs/<source-commit>/`.
Its `provenance.json` records the upstream release, source commit, archive URL
and digest, extracted executable digest, and absolute host ctr path with its
actual version and executable SHA-256.

`mise run lane-snapshotter-path` verifies that receipt, the pinned executable,
the unchanged host ctr identity and the separately reviewed FUSE provider,
then prints the absolute snapshotter path. Lane creation only resolves these
existing tools; missing or changed provenance fails closed with setup guidance.
Setup never overwrites an existing revision, even if its receipt is invalid.
Investigate changed installations with their owner rather than deleting or
replacing tools that might serve mounted lanes. Derived manifest worktrees use
the actual enclosing West root containing `.west/config` for source and install
paths, not the manifest's immediate parent or a guessed alternate workspace.

Portable skills live in `.agents/skills/`; `.claude/skills` is the relative
compatibility symlink `../.agents/skills`. `mise run dw setup-local` exposes that
same source to the product checkout and installs its instruction template and
local excludes. It preserves differing instructions and refuses conflicting
skill paths without overwriting them. See [agent setup](../README.md#agent-skills-and-local-guardrails)
for activation. After skill edits, resolve active `skill://` aliases to physical
files and synchronize only genuinely separate installed copies, preserving local
additions. Do not search conversation archives for installation copies.

Issue operations use `mise run west dw beads ...`; comments use `comments add ID TEXT`.
`create` uses `--labels`, but `list` filters with `--label`. Safely quote free
text: shell backticks in descriptions/comments execute commands before Beads
receives them. Verify the returned ID.

Use the active harness's dedicated file/search/edit tools. With command-only
transports, bound forest searches using `scripts/west-search.py PATTERN ROOT...`.
RTK transports need `rtk proxy find` for compound find and `rtk bash -c` for shell
syntax; keep quoted text within that interface's argument/environment contract.

## Durable state and cleanup

Declare one owned `DW_STATE_ROOT` per task/lane. Without an override, durable
runtime state lives under `<topdir>/.west-state/`: prefixes, bundles, scratch,
evidence and jobs; standalone lane storage defaults to `<topdir>/.darling-lanes`,
while a managed run's lane root defaults to `S/l`.
`west test --fresh-prefix-from`, `west darling-fresh-prefix` and dev-check smoke
prefixes resolve beneath the state root. `/tmp` is only disposable compiler
scratch or guest-internal storage, never a prefix, lane, job or evidence archive.

Temporary experiments use one producer-owned managed run, not ad hoc
`create -> exec -> destroy`:

    west dev lane run (--base INERT_PREFIX | --parent FROZEN_PARENT) \
      [--root L] [--state-root S] [--runtime-root R] [--source SOURCE] \
      [--name NAME] [--allow-root] [--keep] [--pin] [--min-free-gib 30] \
      [--evidence-keep-last 5] [--evidence-max-gib 1] [--runtime-keep-last 2] \
      [--json] -- COMMAND...

`S` defaults to `DW_STATE_ROOT` or the canonical workspace state, and `L`
defaults to `DW_LANE_ROOT` or `S/l`. `L` must be at or below `S`; the root must
be absolute, owned and not a symlink. Keep a real guest prefix at most 75 bytes;
autogenerated names are short `r` plus 12 hex characters. `--base` imports an
inert unmounted guest prefix, not a runtime. For repeat experiments deliberately
create and freeze a persistent seed once, then run children from it. `--parent`
requires the original frozen parent mount to remain retained. `--allow-root` is
per-new-view, is never inherited, and requires approved existing `fuse.conf`
setup; the managed run does not alter host policy. See
[lane isolation](lane-isolation.md) and the [boot skill](../.agents/skills/darling-boot/SKILL.md)
for provider, snapshotter and launch prerequisites.

The outer run owner stays outside the guest cgroup and holds a typed run
journal, exclusive lease and PID/start-time identity. It executes through the
actual lane `exec`, stops only its own scope, verifies native prefix cleanup,
seals closed evidence before removing the native graph, and never glob-deletes
layer directories, prefixes, caches or locks. A persistent retired lane
directory, tombstone and coordination leases intentionally survive as small
metadata. Success and workload failure normally retire the lane.
`workload_rc` preserves the actual command status separately from `final_rc`,
which is the owner's status including cancellation or cleanup failure; a
`completed` phase means resource lifecycle finished, not workload PASS.
Missing, unknown or cancelled status is never PASS. Capture or ownership
ambiguity leaves blocked raw state and its reason rather than a fabricated
success. `--keep` retains a quiesced mounted lane for inspection outside
automatic retirement and is released with
`west dev lane recover --release-kept --run RUN_DIR`; `--pin` protects completed evidence,
not the raw prefix.

A new run recovers known dead-owned prior runs and applies bounded retention.
Inspect owners and final states with `west dev lane runs --state-root S --json`;
finish only unlocked interrupted owners with
`west dev lane recover --state-root S [--run RUN_DIR] [--release-kept] --json`. Active
locks, live PID generations, kept runs and unknown entries are preserved.
Recovery uses the same native cleanup; a SIGKILL rc remains unknown if it was
never observed. Closed evidence lives under `S/runs/run-UUID/`: mutable
`run.json` control and an immutable `payload/` with full `SHA256SUMS` and
capture provenance. It selects logs, cores, receipts, state diagnostics and
command outputs, never the whole prefix, SDK, source, Git, build or caches.
Unknown source, worktree or payload entries fail closed instead of being
silently omitted. The guest-C runner's empty `guest-c-fixtures` container carries
no payload and does not block capture; nonempty or symlinked containers remain
errors and preserve the raw lane.

Evidence retention defaults to the newest 5 ordinary completed runs and a
1 GiB soft budget, with the newest completed oracle plus pins, live, kept and
blocked resources exempt. The count is the first bound; byte pressure may evict
older exempt-free entries inside that window but never the newest or protected
ones. A `--evidence-keep-last` or `--evidence-max-gib` override changes the
invoking owner's policy only. Evidence retirement is journaled and survives detach,
deletion or unpin interruption; runtime evidence pins are released only after
the actual evidence deletion, and the completed journal remains until it is
acknowledged.

Deliberately persistent or long-lived lanes use
`west dev lane create NAME --base PREFIX --runtime-root R` and
`west dev lane exec NAME -- COMMAND`, or `west test --lane NAME --runtime-root R`.
These low-level actions remain for retained bases and persistent lanes, not as
a shortcut for temporary experiments. Each lane has separate writable guest state,
TMPDIR, XDG_RUNTIME_DIR and process scope; the inert base is never written.
Use `lane verify` and `lane destroy`. Fresh state identity is published by the
product: `DPREFIX=DEST R/bin/darling bind-clone BASE`, not by copying identity files.
Clone admission checks root identity, ownership and permissions. Lanes preserve
the base root's mode instead of inheriting the caller's umask.
Workspace lifecycle ownership uses a persistent external sibling lease file,
distinct from `P/.lifecycle.lock`; never delete the lease file. Preserve raw
prefix spelling for the product's retained-FD/no-follow/`..` safety checks.
Nested managed controllers borrow the verified locked descriptor, not an
environment marker alone; actual guest runtime processes must not inherit it.
Runtime environment markers without retained ownership capabilities are
unverified residue: they block a clean verdict but never authorize a signal.
Read-only residual PID reports and verified-owned PID reports are distinct;
cleanup must not remove runtime state or claim zero residue from the latter alone.
Named contexts select local `--runtime-root R --prefix P` configuration.

### Image retention

Only new normal installations inside an explicit `DW_STATE_ROOT` are registered
by `runtime_install` or `darling-build`. Old, unregistered or outside roots are
never adopted or removed, and source, Git, private inputs and active build
forests are not candidates. After a successful new publish, scoped retention
runs and keeps the newest two images plus all pins, lane/run/evidence references,
and live readers, mappings, mounts and leases. Deletion requires a complete
schema-2 normal-install receipt, matching root generation, no references, both
external locks and a complete census. Cross-namespace bind aliases are resolved;
the privileged census helper is read-only; any failure preserves. Failed,
partial or ambiguous images are preserved and reported, never given a fabricated
receipt or successful fallback.
Zombie entries do not pin images once every thread is confirmed exited. A zombie
leader with live or unreadable thread membership still blocks reference judgement.

`west dev lane images --state-root S --json` is the dry-run plan.
`west dev lane pin-runtime|unpin-runtime --state-root S --runtime-root R --reason REASON --json`
adds or releases a manual named pin on a registered image. `--runtime-keep-last`
may not go below two images.

Managed build/install and managed-run admission refuse to start a heavy
allocation when current free space is below 30 GiB. This is an admission floor,
not a reservation or quota; it does not predict the allocation's final size.
`west dev lane run` exposes `--min-free-gib` for an
explicit different floor. Retention never runs as a background global sweeper
and never adopts or cleans legacy/unregistered roots; generic GC remains an
explicit, inspected operation that is never run unattended.

### West job state

New `scripts/west-job.sh` states are marked and lifetime-locked. Completion
finalizes the observed command rc, compacts the logical bare log through the
evidence index, and, when the state lies beneath one, prunes only this script's
own finalized states under `DW_STATE_ROOT` or `WEST_JOB_RETENTION_ROOT`. Defaults are
`WEST_JOB_RETENTION_KEEP_LAST=5` and `WEST_JOB_RETENTION_MAX_BYTES=1GiB`;
newest/current, pinned, active, unmarked and unknown states are preserved.
Legacy states are never adopted. `follow` holds a separate shared reader lease
across output and final-rc delivery, the producer uses a different lifetime
exclusive lock, and pruning needs both exclusive locks. Following a live job
therefore neither waits for it nor risks deletion from under the observer.
Read completed logs through the logical evidence resolver (for example
`darling-evidence.py cat PATH`); do not assume the bare `log` remains a regular
file after compaction. Finalization and pruning do not rewrite the recorded
command rc.

### Generic GC and isolated source authoring

Never run generic GC unattended. Inspect `--dry-run`, snapshot affected paths,
and copy required evidence outside GC-managed roots with a SHA-256 manifest first.
GC must prove ownership, not infer it from a name. Unknown directories remain
untouched. Never manually remove runtime-build-cache entries: use
`mise run west -- test --gc --gc-runtime-build-cache --runtime-build-cache-root DIR`
with `--dry-run` first. It preserves locks, unexpected source edits and unknown
entries, and unregisters owned worktrees before deletion. Read
[GC scope](test-infra.md#diagnostic-execution-tiers) before any maintenance.
With `DW_STATE_ROOT`, maintenance roots must stay inside that owner's root.
`--proof-scratch-root` does not scope runtime evidence: fixture GC must also pass
`--runtime-evidence-root`. Never keep the sole acceptance archive in a GC root.
If only a digest/verdict remains, record that limit and rerun the gate.

For isolated source authoring use `west dev start --source SRC --destination DEST
--base BASE --branch BRANCH --bead BEAD --module MODULE --evidence PATH`, with
`--dry-run` first. It clones the gitlink closure and creates no discovery record;
record the destination and remove only that owned disposable checkout when done.

## iOS experiment area

    source workspace   /home/ilyagulya/work/wr-fresh
    build directory    /home/ilyagulya/work/wr-fresh/build/toolchain
    Xcode input        /home/ilyagulya/work/local-target/xcode-26.3/Xcode.app   (read-only)
    Swift overlay      /home/ilyagulya/work/wr-fresh/toolchain-overlays/swift-6.3.1-macos
    sibling-fork reference
                       /home/ilyagulya/work/wr-fresh/vibedarling   (read-only shallow clones of
                       the VibeDarling forks: a source of implementations that already exist.
                       NEVER a build source, never a West project, never a source authority.)

Rules that keep the area usable:

* Select a complete ordinary installation `R` and a separate owned guest state
  `P`. Runtime changes require a new full installation root, never hand copies,
  runtime-additions scripts or guest-visible shadow libraries.
* Keep the Swift overlay's provenance as diagnostic evidence; it is not a product
  installation and must not be copied into a production image.
* Apple's `ld` needs the filesystem-capable libc++ (including
  `std::__fs::filesystem::path::__filename()`). A missing symbol is a real
  runtime compatibility failure, not a reason to inject a replacement into `P`.

## Xcode input (read-only)

    path          /home/ilyagulya/work/local-target/xcode-26.3/Xcode.app
    version       26.3
    build         17C529
    SDK           iPhoneOS26.2 (`.../Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS26.2.sdk`;
                  `iPhoneOS.sdk` is a convenience symlink to it)
    size          12 G; the unpacked `.xip` (Content / Content.cpio / Metadata) sits in
                  /home/ilyagulya/work/local-target/xip
    Swift package /home/ilyagulya/work/local-target/swiftpkg/swift-6.3.1-RELEASE-osx-package.pkg

Do not modify the Xcode tree in place and do not scatter copies into `/tmp`.

## Prefixes

`R` is an immutable product installation: `R/bin/darling`,
`R/bin/darlingserver`, `R/libexec/darling`. `P` is writable guest state selected
by `DPREFIX` or managed `--prefix`; the two roots must be disjoint.
Configure absolute `CMAKE_INSTALL_PREFIX=R`, default `COMPONENTS=stock`, build
ALL and install normally. A published image is never rewritten, even after its
build directory changes. No launcher discovery in `P/bin` or duplicate system
library deployment into guest state.

```sh
mise run west darling-build --build-dir "$B" --runtime-root "$R" --install
mise run west darling-bootstrap --runtime-root "$R" --prefix "$P" --no-build \
  --guest-toolchain darling-command-line-tools
```

Omit CLT for runtime-only smoke. Guest compilation requires real reviewed CLT
installed through the guest installer; repair cannot manufacture a missing SDK.
A new installation is an absent/empty owned root inside the explicit state root;
managed build/install admission applies the 30 GiB free floor described above.
Long runs use `scripts/west-job.sh start|follow|cancel` or dev run/follow/cancel;
managed experiments use the lane `run` owner. Wait for final status and prefix
cleanup; external rescue cannot produce clean PASS.

## Retained legacy source and evidence

`/home/ilyagulya/work/darling-gwn-resume` and
`/home/ilyagulya/work/darling-dev` are retired, not source authorities or setup
targets. Selective source/repro keepers are recorded in
`<workspace>/.hunt-value-audit/keepers.tar.gz`, `keepers.SHA256SUMS` and the
archive's `keepers-manifest.json`; they are not a full backup or executable
defaults. Retain useful unmerged inputs and their checksums outside GC roots.
Missing historical inputs must not silently select current source instead.

The external `darlingserver-port`, `darlingserver-timerfix`, and `tmp-apply-probe`
worktrees depend on the common Git store under
`hunt/delete-prep-20261009/legacy-debugger/retained-git/darling/src/external/darlingserver/.git`.
That store is not disposable cache while those worktrees depend on it.


## The swift submodule's LFS objects, when the LFS host is unreachable

Reviewed Swift inputs include SHA-256-addressed LFS objects served from
`https://git-lfs.darlinghq.org/lubos/darling-swift`. Preserve content identity
when using a reviewed reachable mirror. A missing historical archive is not
a recovery source. `git lfs fsck --objects` checks content addressing, not
publisher authentication; do not fabricate objects or SDK identity.

## A direct launcher invocation needs DPREFIX

The launcher defaults to `~/.darling` when `DPREFIX` is absent. Its executable
pathname does not select writable guest state, so launching the correct image
without `DPREFIX` can inspect a different prefix. Managed lifecycle owners set
the selected prefix; a generic host-command wrapper does not provide that
ownership.

Set `DPREFIX="$P"` on every necessary direct `"$R/bin/darling"` invocation.
`DARLING_PREFIX` alone is not the product launcher's guest-state selector.

## Installed-image identity

For current identity use `darling-doctor --scope runtime --runtime-root R
--prefix P --build-dir B` and the installation receipt. The full schema-2
receipt records actual image files, normal install-hook output and actual
CMake manifest hashes; verification rejects extra files. It verifies the
published image independently of subsequent changes to `B`, not build-versus-
guest shadow-copy equality. Runtime-only audit does not establish guest acceptance.

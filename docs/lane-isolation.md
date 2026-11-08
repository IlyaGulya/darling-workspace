# Isolation lanes: two Darling experiments at once

Current operational guidance is below; dated observations are retained separately
as historical evidence from 2026-10-18 (Ubuntu 24.04, kernel 6.8, this workstation),
not verification of the current runtime cutover.

An isolation lane gives one experiment its own copy-on-write guest prefix,
state root, temporary/runtime directories and process ownership. Cooperative
experiments can share one immutable installed runtime without sharing writable
guest state. Storage is one containerd fuse-overlayfs snapshotter store per lane
root, mounted by the exact reviewed West FUSE driver; there is no legacy or
selectable alternate backend.

`west dev lane` manages this lifecycle; `west test --lane NAME` runs the existing
test runner in an active lane. `west dev lane run` is the default mandatory route
for a temporary experiment: it creates a fresh lane, executes the command,
captures evidence and retires the lane normally. `lane_snapshotter.py` owns the
upstream snapshot
API adapter and storage services, `lane.py` owns product lifecycle and admission,
and `dev_lane.py` owns CLI/reporting. A lane changes where a command runs and who
owns it, not which workload is selected. Product `bind-clone`, not workspace
record rewriting, binds every new prefix to its mounted directory identity.

## Why this mechanism, in host facts

The following host measurements are historical observations, not current setup
instructions or proof of the selected runtime:

| Question | Measured answer | Consequence |
|---|---|---|
| Can an unprivileged user mount a kernel `overlay`? | No. `kernel.apparmor_restrict_unprivileged_userns=1`; `unshare --user --map-root-user` fails at `write /proc/self/uid_map: EPERM` | A kernel overlay at the prefix is impossible without root, so the supported lane is a `fuse-overlayfs` layer created by `west dev lane`; no root helper is used |
| Is the workspace filesystem copy-on-write? | No. `/` is ext4 and `cp --reflink=always` returns `Operation not supported` | `west darling-fresh-prefix --from` degrades to a multi-gigabyte byte copy; a lane must be a layer, not a copy |
| Can an unprivileged user serve a directory tree through `fuse-overlayfs`? | Yes, in ~15 ms, through the distribution's setuid `fusermount3`, with `user_id=1000` on the mount | `fuse-overlayfs` is the rootless copy-on-write layer. It needs no user namespace, no capability and no host security change, and the kernel only lets the mounting user unmount it |

Two more that shape the *process* story:

* cgroup v2 is delegated to `user@1000.service` (`cpu memory pids`), but this shell's own
  `session-*.scope` is root-owned, so a process cannot be migrated into a hand-made cgroup. A
  `systemd --user` **scope** is therefore the ownership primitive: `systemd-run --user --scope
  --unit=darling-lane-<name>-<digest>` puts the whole command tree in its own cgroup, keeps
  detached, re-parented, environment-scrubbed descendants in it after the scope's main process
  exits, and `systemctl --user stop` kills exactly that tree (all measured). The unit carries a
  digest of the lane's own path **and** a `--description` naming it, because a unit name is global
  to the user manager while a lane is not: without the digest, two lane roots that chose one name
  claimed one unit, and one lane's run stopped the other's experiment. `west dev lane status`
  prints the unit, so nothing has to spell it by hand, and a scope whose description names a
  different lane is refused rather than "reused".
* Workspace lifecycle ownership uses a persistent external per-prefix sibling lease,
  `<P.parent>/.west-prefix-lease-<sha256(P.name)>`. Different lane prefixes have
  independent leases, even when prefixes share a parent. Never delete the lease
  file; it is distinct from the product's `P/.lifecycle.lock`.
  Destruction keeps the lane's parent directory and coordination files, so a
  controller retaining an old open lease cannot be split from a same-name lane
  recreated later.

FUSE providers run as collected transient user services. After a view is
unmounted, its exited unit may disappear between inspection and `stop`.
Release accepts that race only after rechecking both unit absence and the
unmounted view; a retained service, mount or uninspectable state remains an
error. Snapshot layers are removed only after provider release.

## Commands

Run lane commands from the manifest as `mise run west -- <west arguments>`; the
examples below show the West CLI itself.

Provision the exact tools explicitly before creating lanes:

```sh
mise run lane-provider-build
mise run lane-snapshotter-install
mise run lane-provider-path
mise run lane-snapshotter-path
```

See [host prerequisites](environment.md#host-tools-and-agent-activation) for
reviewed source availability, the pinned upstream release, host ctr identity,
`/dev/fuse`, `fusermount3` and a working systemd user manager. Creation resolves
verified immutable tools; it never downloads, rebuilds, searches for a substitute
provider on PATH or silently switches backend. Tool executables serving existing
mounts must never be overwritten.

Guest disk-image mounts such as `hdiutil attach` invoke a setuid FUSE helper.
Create a **new** view with explicit root access when that workload needs it:
`west dev lane run --parent FROZEN_PARENT --allow-root -- COMMAND` for a
temporary experiment, or
`west dev lane create NAME --parent FROZEN_PARENT --allow-root` for a persistent
lane. The default remains owner-only; the option is not inherited from a frozen
parent. It never remounts an existing view. A temporary run may import an inert
base with `--base` instead of a frozen parent.

An administrator must first approve and enable `user_allow_other` in
`/etc/fuse.conf`, preserving a backup of that file. The CLI checks this host
prerequisite before creating lane state and never edits the configuration.
The option selects the reviewed provider's `RootAndOwner` ACL, **not** its
all-users `allow_other` mode. Kernel mount information still shows the
`allow_other` backing flag; the provider admits only root and the mounting
owner, with ordinary file permissions unchanged.

For an existing owner-only lane, finish its workload and ordinary cleanup,
freeze it, then create a persistent child or use a managed run with
`--allow-root`. Do not change an active lane's mount options or restart another
lane's provider.

Select an absolute owned, **short** lane root beneath your durable task state
or owned workspace state. Guest AF_UNIX pathname limits apply after expanding
the prefix and symlinks: `/var/run/shellspawn.sock` becomes
`<prefix>/private/var/run/shellspawn.sock` (prefix bytes + 32). With the current
108-byte host address, normal bootstrap therefore needs a prefix no longer than
75 bytes; other guest socket paths can impose tighter limits. Abstract courier
names have a separate bound. A short snapshotter socket does not remove these
product-runtime limits; do not use symlink aliases or truncate names to bypass them.
Bootstrap the base separately with the selected normal installation, then finish
product shutdown and cleanup before import. `--base` selects guest state, never
an installed image; live base processes or mounts are refused. `create` and
`run` require exactly one of `--base` and `--parent`. Set `DW_LANE_ROOT` at or
below `DW_STATE_ROOT` when a persistent seed must serve managed runs, whose
default lane root is `S/l`.

**Persistent seed.** Deliberately retained guest state uses the low-level
actions. Prepare it through the supported owner while it is active, then freeze
it only after all executions and product cleanup have ended:

```sh
export DW_STATE_ROOT=/absolute/owned/state
export DW_LANE_ROOT="$DW_STATE_ROOT/l"

# Import an inert guest prefix; runtime selection is separate and immutable.
west dev lane create seed --base /abs/base-prefix --runtime-root /abs/runtime
west dev lane exec seed -- <prepare command>
west dev lane freeze seed
```

**Temporary experiments.** A temporary experiment must use the managed run,
which always creates a fresh lane and refuses an existing namespace. It executes
through the actual lane `exec`, captures evidence, then retires the lane with the
same supported destroy path.

```sh
# Fork from the frozen seed; runtime/source selection is inherited by default.
west dev lane run --parent seed -- <command>

# Or import an inert base directly when no reusable seed is needed.
west dev lane run --base /abs/base-prefix --runtime-root /abs/runtime -- <command>

# Retain a quiesced mounted lane for inspection, then release it.
west dev lane run --parent seed --keep --name inspect-a -- <command>
west dev lane recover --state-root "$DW_STATE_ROOT" \
  --run "$DW_STATE_ROOT/runs/run-<id>" --release-kept --json

# Protect completed evidence from bounded retention; inspect/recover owners.
west dev lane run --parent seed --pin -- <command>
west dev lane runs --state-root "$DW_STATE_ROOT" --json
west dev lane recover --state-root "$DW_STATE_ROOT" --json
```

A managed run's evidence lives under `S/runs/run-UUID/`: mutable `run.json` and
an immutable `payload/` with full `SHA256SUMS` and capture provenance. Evidence
selection covers logs, cores, receipts, state diagnostics and command outputs,
never the whole prefix, SDK, source, Git or build caches. `--keep` retains a
quiesced mounted lane outside automatic retirement; `--pin` protects completed
evidence, not the raw prefix. A `completed` phase means the resource lifecycle
finished, not that the workload passed: `workload_rc` is the command's own status
and `final_rc` also includes cancellation or cleanup failure. Missing, unknown
or blocked status is never PASS. Ambiguous capture or ownership leaves a
`blocked` run with its reason and raw state; recovery preserves it rather than
inventing success.

`lane runs` and `lane recover` preserve active owner locks, live PID
generations, kept runs and unknown entries, and recover only dead-owned
interrupted owners through the same native cleanup. A managed run requires a
fresh lane name and never adopts existing, old or unregistered lane/run data.
See [state ownership and cleanup](environment.md#durable-state-and-cleanup) for
roots, retention bounds and image policy.

**Explicitly persistent lanes.** Low-level `create`/`exec`/`destroy` remain for
long-lived lanes, deliberate bases and inspection, not as a shortcut for a
temporary experiment:

```sh
west dev lane create persistent-a --parent seed
west dev lane status persistent-a
west dev lane verify persistent-a
west dev lane exec persistent-a -- <command>
west test --lane persistent-a --runtime-root /abs/runtime --workload WORKLOAD_ID

# Freeze it to serve further children, or retire it through the supported path.
west dev lane freeze persistent-a
west dev lane run --parent persistent-a -- <command>
west dev lane destroy persistent-a

# Delete reverse ancestry; a referenced parent is refused without losing its mount.
west dev lane destroy seed
```

Freeze commits the snapshot **without unmounting the original view**. It requires
an idle lane scope, exclusive lane admission, an available exclusive prefix lease
and a quiescent managed prefix. A parent must already be frozen and retain its
original mount; `create` does not freeze implicitly, and a managed run never
freezes its own lane. Frozen lanes remain inspectable/verifiable, but `exec` and
`west test --lane` reject them. There is no thaw operation: create a persistent
child, or use a managed `lane run --parent FROZEN_PARENT` for a temporary one, to
continue work. The root's operation lease serializes storage graph and
lane-record publication.

Destroy retires a lane rather than deleting its lease namespace. It removes
`lane.json`, the guest prefix, writable data and work/evidence directories, but
keeps the same parent directory, `.execution.lock`, external prefix lease and
owned product sibling lock files, plus `.retired-lane.json`. This small marker
contains only schema/name/root/UID and the parent device/inode: it holds no
runtime-image, source or snapshot references and pins no runtime installation.
Retired names disappear from `list`; `status` treats them as missing. A managed
run invokes this same retirement automatically and leaves the same persistent
lock and tombstone metadata; that small residue is intentional, not leaked guest
storage. Do not destroy a managed owner's lane out-of-band while it is live, and
never delete the lane parent, locks or lane root to reclaim space.

A later `create` with the same name can reuse that exact retired parent and
lease domain with a newly selected base or frozen parent and installed runtime.
Only a validated minimal tombstone is reusable: unexpected data, replaced
parents, wrong ownership or nonregular coordination files fail closed. Never
remove a lease or delete/recreate its parent by hand to make reuse succeed.


**Freeze is cooperative CLI immutability, not a kernel read-only remount.**
The retained frozen view is still writable by the owning host user. Do not write
it directly, launch a runtime against it or modify snapshotter layer directories.
Children depend on frozen layer bytes remaining stable.

`west test --lane NAME` selects an existing active lane and cannot combine with
`--prefix`. Select the same immutable installation recorded by the lane. Workload
and source selection remain independent of the installed image. Both test and
exec re-enter their own per-lane scope before admission; a managed run executes
through that lane `exec`. The test controller retains its normal external prefix
lease through execution/cleanup. Arbitrary `exec` commands instead hold a lane
admission lease; no lifecycle descriptor or borrowed-lease marker is passed into
the command or its runtime children. Nested supported controllers acquire their
own prefix lease normally. Freeze requires exclusive admission and cannot race
an admitted command. The transient scope owns materialization, guest work and
cleanup; an interrupted run with remaining owned processes prevents another
experiment in that lane. Low-level `exec` commands preserve the mounted view and
its writes for inspection/reuse; a managed run retires its lane unless `--keep`
retained it.

**Admission fails closed.** A missing/remounted view, changed mount lifetime,
incomplete/failed lane mode or invalid frozen ancestor prevents execution and
forking. The retained identity includes boot ID, mount ID and device/inode;
a reboot ends that lifetime. Do not reanchor metadata to make a lost view appear
valid. No working per-lane user scope means `west test --lane` refuses execution.
`west dev lane exec --root "$DW_LANE_ROOT" --no-scope NAME -- <command>` is the
explicit **UNSAFE FOR CONCURRENT AGENTS** escape from scope ownership only; it
does not bypass mount/mode admission or the lane admission lease.

## Layout and environment

```text
<lane root>/                   DW_LANE_ROOT; otherwise <west topdir>/.darling-lanes
  .snapshotter/
    store.json                 exact store/tool ownership
    operation.lock             serialized graph/lifecycle operations
    commit.json                transient durable intent preserving labels across upstream Commit
    data/                      upstream-owned metadata and numeric layer directories
  <name>/
    lane.json                  schema 2; mode, snapshot ancestry, mount/runtime/source identity,
                               plus the managed-run owner identity for a temporary lane
    .execution.lock            shared exec admission; exclusive create/freeze/destruction
    .west-prefix-lease-<hash>   persistent external lease for merged; survives retirement
    .darling-prefix-lock-v2-*  owned product sibling locks; also survive retirement
    merged/                    retained FUSE view; the actual guest prefix/lock domain
    state/                     DW_STATE_ROOT: bundles, scratch, runtime evidence
    tmp/  run/                 TMPDIR, XDG_RUNTIME_DIR (0700)
    build/                     lane-specific build selection
    logs/  cores/  receipts/    logs, FUSE diagnostics, cores and runtime baselines
  <retired name>/              same directory/inode; no lane.json or merged/
    .retired-lane.json         schema/name/root/UID/device/inode only; no runtime pins
    .execution.lock            same admission inode
    .west-prefix-lease-<hash>   same prefix lease inode
    .darling-prefix-lock-v2-*  retained owned product sibling locks, when present
```

A managed run keeps its closed evidence outside the lane, in
`<state root>/runs/run-UUID/`; lane retirement does not remove it.

There are no per-lane `upper/` or `work/` directories. The snapshotter alone owns
layer IDs, storage paths and ancestry. Status exposes mode, parent, snapshot,
snapshot kind, storage root, children, layer bytes/inodes, retained mount and
scope/process diagnostics. Those are observations, not permission to edit the
store database or remove a physical layer.

Each root has an owned systemd snapshotter service and short socket beneath
`/run/user/UID`; every retained FUSE view has its own independently owned service.
The services outlive individual commands. A lane command sees `DPREFIX`,
`DARLING_PREFIX`, `DW_STATE_ROOT`, `TMPDIR`, `XDG_RUNTIME_DIR`, `DW_LANE`,
`DW_LANE_NAME`, `DW_LANE_BUILD_DIR`, `DARLING_BUILD_DIR` and, when recorded,
`DW_LANE_SOURCE`.

Choose build `B` explicitly and configure absolute `CMAKE_INSTALL_PREFIX=R`;
default `COMPONENTS=stock`, ALL build and complete normal install publish a new
immutable image at `R`. Runtime images are separate from writable lane state;
new code requires a new image root, never an overwrite of a published image.
See [boot guidance](../.agents/skills/darling-boot/SKILL.md) for the supported
build/bootstrap interfaces and real guest CLT requirements.

Historical build-directory/cache observation (2026-10-18; non-operational):
exporting only `DW_LANE_BUILD_DIR` did not redirect `west darling-build`;
the measured lane build lock was
`/tmp/lane-bd/bd/build/.west-darling-build.lock`, versus host default
`~/work/darling-build`. Six identity locks and two 4.1 GiB source forests
were observed in the then-current runtime-build cache. The end-to-end
two-source `west test` runtime-build claim was not exercised. These observations
do not authorize retired profile materialization or current default-path use.

The first import copies an inert base into the physical Source returned by the
snapshotter's parentless prepare, preserving root metadata, then commits it.
Imports are cached by safe base stat/tree identity within that store, not copied
again for every lane. After import the original base path is provenance, not a
live lower-directory dependency: moving/changing it does not change an existing
lane. No layer belongs to the original base directory.

Every actual lane is an active FUSE child. The selected real launcher performs
`DPREFIX=DEST R/bin/darling bind-clone SOURCE`, where SOURCE is the inert original
prefix for an import or the retained frozen parent view for a fork. Only the
product publishes destination lifecycle identity. Preserve raw prefix spelling
for its safety checks; never manufacture lifecycle metadata.

`verify` checks managed lower layers and frozen-layer stat identity, retained
mount lifetime, the recorded installed runtime's artifact content baseline, and
optional source identity. The tree digest includes stat metadata (path, size,
mtime, inode and mode); it detects replacement/touch drift but is not a content
hash or a security boundary. Runtime content baseline verification is a separate
claim from those stat checks. Verification does not boot a guest.

## What a lane isolates, and what it does not

| Isolated | How |
|---|---|
| Prefix contents | snapshotter-managed COW inheritance and whiteouts; active writes never change the imported/frozen lower layers |
| Runtime processes | per-lane `systemd --user` scope; `systemctl --user stop` reaches only that tree |
| Runtime state root, temp, runtime dir | `DW_STATE_ROOT`, `TMPDIR`, `XDG_RUNTIME_DIR` under the lane |
| Logs, cores, deploy receipts | lane directories; the independent FUSE service logs to `logs/fuse-overlayfs.log` |
| Prefix lock domain | persistent external per-prefix sibling lease; independent lane prefixes do not serialize |
| IPC identity | the runtime derives its courier/socket names from the prefix path, so two lanes cannot share one courier |

| Not isolated | Why, and what to do about it |
|---|---|
| `/dev/shm` | the host's, because rootless Darling does not mount a private one. Names a guest creates are not lane-prefixed; treat a fixture that needs private shared memory as needing its own lane *and* a check |
| Network namespace | not created. Two lanes share the host network |
| CPU, memory, pid limits | the lane scope gives *ownership*, not limits. Nothing in this mechanism caps a lane's resource use |
| Kernel | shared. A lane is not a security boundary against hostile code; it is an isolation boundary between cooperative experiments |
| Frozen snapshot writes by the host user | freeze is a cooperative lifecycle rule, not kernel-enforced read-only access; never mutate a retained frozen view |
| Source checkout | a lane records an independent checkout (`--source`) and verifies its manifest revision; it does not copy source. Two lanes that share one checkout are two writers of one source tree; point each `--source` at its own worktree (`west dev start`, or `git worktree add` plus `west darling-source-worktree prepare`, which materializes the gitlink closure from local objects) |
| Installed runtime image | may be shared read-only; publish a fresh complete root for different code, never mutate a shared image |
| Other framework caches | `<topdir>/.west-test/{runtime-evidence,test-verdict-cache,stock-stack-cache}` are shared. They are keyed by content or identity and published atomically, but they are host state a lane does read |
| Host defaults and other prefixes | still visible, not selected safely by visibility; pass explicit runtime root/build/prefix paths and never fall back to another owner's state |
| `/tmp`, page cache, shared libraries, the systemd user manager, the FUSE kernel module | shared host resources. A lane isolates a prefix and a process tree, not a machine |
| CPU, memory, PIDs | no limits are applied. The lane scope gives ownership; a runaway build in one lane is still a runaway build on the host |

## Measurements

Everything in **Measurements**, including the build/stopping demonstration,
is retained dated 2026-10-18 evidence. Old paths, prefix-runtime deployment and
identity-rewrite descriptions are historical quotations, not operational recipes.

Historical profile-materialization observation (non-operational): two lanes on
the then-current same profile held different input stores (`…-e4c37dd6…` and
`…-71080692…`); `wget-residual` cost about 2.8 GB and 10 minutes of
`git-upload-pack` per lane on a warm host. This records the retired machinery's
cost, not a supported source selection route.



One base (`prefix-fresh1`: 703.5 MiB apparent, 10 019 entries), one host (Ubuntu 24.04, kernel 6.8,
ext4 root, warm page cache), one workload (a full Darling boot plus a guest `echo HELLO=1`). The
comparison is against the mechanism a lane replaces for the same job: `west darling-fresh-prefix
--from BASE`, which byte-copies the prefix because the filesystem has no reflink.

| | isolation lane (`fuse-overlayfs`) | byte copy |
|---|---|---|
| create | **0.49 s** (0.49, 0.49, 0.52) | 0.87 s |
| extra disk at create | **53 248 B** (52 KiB) | 765 419 520 B (730 MiB) |
| extra disk after a full boot + guest workload | **143 360 B** (140 KiB); the writable layer itself is 94 208 B | a copy is the whole prefix |
| full boot + guest workload, wall clock | **28.07 s** | 27.90 s |
| verdict / workload log lines | PASS / 1371 | PASS / 1368 |

So the layer costs about 0.17 s in 28 s (≈0.6 %) and does the same work (1371 against 1368 log
lines), while replacing ~730 MiB of per-experiment disk with ~52 KiB. **Bootstrap time added by a
lane is zero**: a lane reuses the base's runtime and prefix state, and the only write it makes on
the base's behalf is its own prefix-state record. That write is load-bearing, not cosmetic: a plain
copy of the same base is refused with `runtime prefix state belongs to another prefix: recreation
required` until the record is rebound to the copy's identity, which is exactly the step
`west dev lane create` performs automatically.

This is one paired measurement of ONE workload, not a bound and not a universal overhead. A
workload that reads many distinct prefix files may shift the ratio, a heavier workload may shift it
the other way, and nothing here was measured under contention beyond the two lanes of the
concurrency contract. Read it as: on a boot plus a guest `echo`, the layer was within measurement
noise of a byte copy and used three orders of magnitude less disk. Any claim about a different
workload needs its own paired run.

### Two lanes, two independent builds

MEASURED (the end-to-end demonstration recorded in the owning bead): two lanes over one base, each
created with `--source` pointing at its own worktree (`243f21f5` and `43a496d7`), cost 57 344 B
(56 KiB) of extra disk each and were created in `0.60 s` and `0.54 s`. `cmake` + `ninja
darlingserver` then ran **concurrently in both lanes**: `18.47 s` wall for both on the first run and
`9.85 s` on the second, which is the host's shared ccache warming (that shared cache is the one thing
the two builds are allowed to share, and it holds objects, not artifacts: the two outputs really
differ, `9d342e53…` built from A's revision and `c878ea06…` from B's). Each lane's build tree is
218 MiB, and neither worktree had a modified file afterwards (`git status --porcelain` empty in
both). A then deployed its own build into its own prefix (`…/da/merged/bin/darlingserver`), which
left B's prefix and the base at the base's own `cd92d46a…`.

### What the demonstration showed about the base and about stopping

Same run: both lanes owned live processes at the same instant in their own cgroups
(`darling-lane-da-9d7f9fb949c8.scope`, `darling-lane-db-…`), A's cgroup was stopped mid-flight
(A's owned processes `0` afterwards) while B's own run continued to its own `VERDICT: PASS` with
`socket-denials=0 urgent-timeouts=0 courier-misses=0`; B's console never named A's prefix; both
`verify` runs reported the base's content baseline and tree identity intact; `destroy` left no mount
and no daemon behind; and the base's `darlingserver` was `cd92d46a…` before and after. A's own run
exited `rc=2` because it was stopped, which is the expected outcome of the stop and not a verdict
about its artifact.

## Cleanup

`west dev lane destroy` is the only supported way to remove a lane; a managed
run invokes it automatically after capturing evidence. Do not call it
out-of-band on a live owner's lane.

1. validate schema/store ownership, retained mount identities and graph
   references **before** disrupting a retained parent; descendants prevent
   parent deletion without stopping its view;
2. stop only this lane's owned scope and verified prefix processes, then require
   product cleanup and no remaining runtime mounts/processes; retain exclusive
   lane admission and the external prefix lease through deletion and retirement;
3. publish the durable `destroying` mode before irreversible storage removal,
   then gracefully unmount its FUSE view; a still-mounted view prevents layer
   removal, and the independent view service is released only after unmount;
4. remove the snapshot through the upstream graph API, remove the owned guest,
   record and work/evidence directories, and publish the minimal retired marker
   **without deleting or replacing the parent or persistent coordination files**.
   No direct layer-directory/database deletion is supported. An interrupted
   `destroying` lane can be completed by `destroy` again; it cannot execute.

Delete descendants before ancestors. Once no lane references an imported base,
its snapshot can be reclaimed; an empty store stops its owned snapshotter
service. Another lane's FUSE service must remain live throughout sibling removal.

A directory without an owned supported marker is not a lane. Schema-1 lanes
are rejected explicitly: no adoption, migration-by-editing or deletion of legacy
layers. Keep old active lanes on their original checkout under their existing
owner. A missing original retained mount is a fail-closed diagnostic, not an
invitation to remount/rewrite its identity; preserve the evidence and resolve
through the owner instead of forcing graph teardown.

Lane roots are not GC-managed. A lane's own state and evidence live in that
lane; a managed run's closed evidence lives under `S/runs/run-UUID/`. Preserve
required evidence outside a lane before destroying it. The retained tombstone is
intentional, not leaked guest storage. Evidence retention never removes these
parents, their leases or the store coordination namespace. Do not recursively
delete a lane root as an alternative cleanup path.

## Rootless Podman: evaluated, not adopted

This section retains the dated 2026-10-18 evaluation, not current acceptance.


Measured on this host (podman 4.9.3, rootless, `subuid`/`subgid` configured, no capabilities added,
no host security setting changed):

* Rootless Podman itself works: `podman run --rm docker.io/library/busybox true` succeeds.
* **A lane's layer cannot be bind-mounted into a container at all.** `podman run --userns=keep-id -v
  <lane>/merged:/prefix:ro ubuntu:24.04 ls /prefix` fails in container init with
  `error mounting "<lane>/merged" to rootfs at "/prefix": create mountpoint for /prefix mount:
  stat <lane>/merged: permission denied: OCI permission denied`. A plain ext4 directory with the
  same invocation mounts and lists fine, so the obstacle is the user-owned FUSE mount, not the path.
* **A Darling rootless bootstrap does not work inside the container either**, on a prefix a
  container *can* see: with a plain copy of the base bind-mounted, a copy that boots PASS on the
  host, the same launcher inside `--userns=keep-id` stops at the first step with
  `Cannot prepare Darling prefix lifecycle: cannot open runtime prefix lifecycle lock: Permission
  denied`.

The remaining questions — nested namespaces, IPC, ptrace, cleanup — are therefore moot until those
two are solved, and solving them means materializing a prefix *inside* the container (or giving it
the layer as a privilege it must not need). That is the per-experiment copy this mechanism exists to
avoid, so Podman is not a lane backend. It is not rejected on principle: it is rejected because the
part it would have to carry - the prefix layer - is the part it cannot see.

## Tests

The host regression `tests/west_test_contracts/lane_contract.py` requires the
real provisioned snapshotter/client/driver, `/dev/fuse`, `fusermount3` and the
systemd user manager. It uses owned state-root scratch, imports one physical
Source, commits and mounts a parent, commits while retaining its mount, forks
two children and a grandchild, checks updates/whiteout isolation and referenced
parent refusal, then removes reverse ancestry and asserts no mounts/services.
It does not fabricate a runtime or prove product/guest execution.

The existing prefix-backed gates are
`tests/run-darling-lane-test-lane-contract.sh` (real `west test --lane` lifecycle,
optionally `--guest-workload ID`) and
`tests/run-darling-lane-concurrency-contract.sh` (two actual guests, including
product shutdown of A while B runs). Both take `--runtime-root R --base P`,
require a real bootstrapped **idle** base and the same host tools, explicitly
freeze one parent, and create children with `--parent`. Never run the gates
against one base concurrently: use separate prepared base prefixes/owners.
Run each through `scripts/west-job.sh start`/`follow`, select a short absent/empty
`--root` beneath owned durable state, and pass the gate's own `--keep` only when
its scratch directory must be retained. These gates intentionally exercise the
low-level lane lifecycle, not the temporary managed-run owner.
Both gates check minimal retirement and preserved parent/admission/prefix-lease
inodes for both children and their parent, as well as storage/service teardown.
The concurrency gate keeps B running until the controller observes A's product
shutdown and releases B through its own guest-state marker. Its deadline still
covers boot and workload; a fixed sleep must not consume most of that budget.
The interface gate additionally retains an old lease descriptor across real
CLI destroy/create of the same name, proves mutual exclusion against the normal
current prefix-lease helper in both directions, and boots the recreated lane
through a guest file round-trip, Ready publication and ordinary product shutdown
before destroying it again. Its default mode is interface evidence with that
narrow runtime smoke, not acceptance of an arbitrary named workload.
The optional guest-workload phase also requires that workload's installed
prerequisites and its matching `--label`/`--guest-marker`.

Guest PASS requires the complete workload oracle and ordinary product shutdown,
plus zero owned processes/runtime mounts. Lane teardown must also leave no
retained FUSE mounts, daemons or active empty-store service. Rescue cleanup,
wording/source audits and fabricated lifecycle records cannot produce clean PASS.

Historical 2026-10-18 test observation (non-operational): the interface contract's
offline mode ran 33 checks, including 12 lifecycle runs per lane concurrently,
in 3.3 s (0.33 s per lifecycle run). The former guest phase had 46 checks but
required staged profile mirrors absent from that tree; that missing prerequisite
was not a guest failure or proof of concurrent guest acceptance. The separate
concurrency demonstration and its actual outcomes are preserved in Measurements.

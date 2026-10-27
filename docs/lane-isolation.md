# Isolation lanes: two Darling experiments at once

Current operational guidance is below; dated observations are retained separately
as historical evidence from 2026-10-18 (Ubuntu 24.04, kernel 6.8, this workstation),
not verification of the current runtime cutover.

An isolation lane gives one experiment its own copy-on-write prefix, its own state root, its own
temporary and runtime directories, and its own process ownership, so two agents can run two Darling
experiments at the same time without sharing mutable product state. Stopping one lane cannot reach
another, and the base prefix a lane is layered over is never written.

The mechanism is one West subcommand, `west dev lane`, plus one opt-in flag on the existing test
runner, `west test --lane NAME`. The implementation is `west_commands/lane.py` (every rule) and
`west_commands/dev_lane.py` (argument shapes and reporting). There is no second test framework: a
lane changes *where* an existing command runs and *who owns its processes*, never what it runs.

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

## Commands

```sh
# Create a lane. The base is stable writable guest state and must be inert: a base with
# live processes or mounts is refused, because its runtime sockets live in the lower layer
# and a lane would resolve them to the base's own server. The immutable runtime image is
# selected separately; a lane never inherits a launcher from its base.
west dev lane create lane-a --base /abs/path/to/base-guest-state --runtime-root /abs/runtime

# Inspect it: mounts, owned processes, its cgroup and unit name, its upper-layer disk use,
# base identity.
west dev lane status lane-a
west dev lane list
west dev lane verify lane-a     # base and source unchanged since creation

# Run anything inside it: prefix, DW_STATE_ROOT, TMPDIR, XDG_RUNTIME_DIR, DARLING_BUILD_DIR and cgroup.
west dev lane exec lane-a -- ./scripts/darling-boot-run.sh --runtime-root /abs/runtime --prefix /abs/lane-root/lane-a/merged --wait 20
west dev lane exec --root /abs/lane-root lane-a -- <command>   # --root precedes the lane name

# --no-scope runs WITHOUT the lane's cgroup. It is refused nowhere and labelled everywhere, because
# nothing about the run looks different afterwards: UNSAFE FOR CONCURRENT AGENTS.
west dev lane exec lane-a --no-scope -- <command>

# The existing test runner, in a lane. Everything else about the invocation is unchanged.
west test --lane lane-a --runtime-root /abs/runtime --label name:some-case
west test --lane lane-a --runtime-root /abs/runtime --source-root /abs/source --workload WORKLOAD_ID

# Stop and remove exactly this lane.
west dev lane destroy lane-a
```

`west test --lane NAME` selects an existing lane explicitly; it cannot be
combined with `--prefix`. The runner re-enters its lane scope before doing work.
Select the same immutable runtime image recorded by the lane, not a launcher
from its writable base. Workload selection remains independent of source/runtime.

The runner re-enters itself once, before doing any work, under
`systemd-run --user --scope --unit darling-lane-<name>-<digest>.scope`, so materialization, the boot,
the guest case and the run's own cleanup are all inside one kernel-enforced scope. The recap and
`west-test-lifecycle.log` name the lane, its prefix and its state root, so a lane run's output says
which lane produced it. The run destroys nothing of the lane: `merged/` and `upper/` keep the run's
writes for inspection, and a second run gets a second scope. The scope is transient and ends with
the run — which is why an *interrupted* run leaves its processes inside its own scope and the next
run into that lane is refused (`lane scope … still owns N process(es); stop it with 'systemctl --user
stop …' or remove the lane`) instead of quietly starting a second experiment in the same lane.

**Lane execution fails closed.** With no working per-lane `systemd --user` scope there is no
per-lane process ownership, so a run that would be parallel is refused rather than started unowned:
`west test --lane` dies, and `west dev lane exec` refuses unless `--no-scope` says otherwise in
those words. The single escape is that flag, and the only labelled mode in this mechanism is the
unisolated one.

## Layout and environment

```
<lane root>/<name>/            root defaults to <west topdir>/.darling-lanes; DW_LANE_ROOT overrides
  lane.json                    schema, name, base identity + tree digest, source revision, created
  merged/                      the prefix a run points at: fuse-overlayfs over the base (own lock domain)
  upper/                       everything a run writes; delete the lane to discard it
  work/                        overlayfs working directory
  state/                       DW_STATE_ROOT: bundles, scratch, runtime evidence
  tmp/  run/                   TMPDIR, XDG_RUNTIME_DIR (0700)
  build/                       where a lane's own build belongs (DW_LANE_BUILD_DIR)
  logs/  cores/  receipts/     run logs, cores, deploy receipts, the fuse daemon's own log
```

A command started with `west dev lane exec` or `west test --lane` sees `DPREFIX`,
`DARLING_PREFIX`, `DW_STATE_ROOT`, `TMPDIR`, `XDG_RUNTIME_DIR`, `DW_LANE`, `DW_LANE_NAME`,
`DW_LANE_BUILD_DIR`, `DARLING_BUILD_DIR` and (when a source was recorded) `DW_LANE_SOURCE` set to
that checkout's path.

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

The base is stable guest state and remains the immutable lower layer of the
lane overlay; it is not the installed runtime's system root. Only the product
may publish clone identity: lane creation invokes
`DPREFIX=DEST R/bin/darling bind-clone BASE`. The product validates and writes
the destination lifecycle record through the merged view; it lands in the
upper layer. Workspace Python never rewrites lifecycle records. Preserve raw
prefix spelling for product safety checks.

Two different checks say that, and they are not the same claim:

* **runtime content** — a runtime-root artifact baseline is recorded with
  `scripts/darling-artifact-manifest.sh --runtime-root R --save RECEIPT`;
  verification checks the recorded image, not runtime copies in guest state.
* **tree identity** — a stat-only digest over path, size, mtime, inode and mode for every entry. It
  is a drift detector: it also fires when a file is replaced by an identical one or merely touched.
  It is NOT a content-integrity proof and is not presented as one.

## What a lane isolates, and what it does not

| Isolated | How |
|---|---|
| Prefix contents | copy-on-write layer; base never written (`verify` proves it) |
| Runtime processes | per-lane `systemd --user` scope; `systemctl --user stop` reaches only that tree |
| Runtime state root, temp, runtime dir | `DW_STATE_ROOT`, `TMPDIR`, `XDG_RUNTIME_DIR` under the lane |
| Logs, cores, deploy receipts | lane directories; the FUSE daemon logs to `logs/fuse-overlayfs.log` |
| Prefix lock domain | persistent external per-prefix sibling lease; independent lane prefixes do not serialize |
| IPC identity | the runtime derives its courier/socket names from the prefix path, so two lanes cannot share one courier |

| Not isolated | Why, and what to do about it |
|---|---|
| `/dev/shm` | the host's, because rootless Darling does not mount a private one. Names a guest creates are not lane-prefixed; treat a fixture that needs private shared memory as needing its own lane *and* a check |
| Network namespace | not created. Two lanes share the host network |
| CPU, memory, pid limits | the lane scope gives *ownership*, not limits. Nothing in this mechanism caps a lane's resource use |
| Kernel | shared. A lane is not a security boundary against hostile code; it is an isolation boundary between cooperative experiments |
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

`west dev lane destroy` is the only way to remove a lane, and it refuses rather than forces:

1. stop the lane's `systemd --user` scope (kernel-enforced; it cannot reach another lane, and a
   scope whose description names a different lane is refused rather than stopped);
2. terminate only processes that prove ownership of *this* prefix — the existing evidence the
   repository already uses (`DARLING_PREFIX`/`DARLING_ROOTLESS` in the environment, or the runtime
   holding this prefix's exact directory inode);
3. stop the lane's own `fuse-overlayfs` daemon, identified by program **and** the lane's path in its
   argv — never by the program name alone;
4. unmount the lane's layer (the kernel permits this only for the mounting user), refusing to delete
   a still-mounted lane;
5. remove the tree only when no process, mount or socket still owns it.

A directory without a `lane.json` of the right schema is refused and left alone: a name is never
ownership. A lane whose *base* is running is refused at creation for the same reason.

The `west dev lane` root is not a GC-managed state root. `DW_STATE_ROOT` keeps a lane's own
evidence inside the lane; nothing here writes into another lane's root.

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

Current lane regression entrypoints are
`tests/west_test_contracts/lane_contract.py`,
`tests/run-darling-lane-test-lane-contract.sh` and
`tests/run-darling-lane-concurrency-contract.sh`. Read their current parsers
before running; select runtime root and guest state separately. Source inventory
and offline/synthetic ownership tests are not guest execution or native PASS.
Guest acceptance requires the full selected workload oracle and ordinary product
shutdown with zero owned processes/mounts; rescue cleanup cannot yield clean PASS.

Historical 2026-10-18 test observation (non-operational): the interface contract's
offline mode ran 33 checks, including 12 lifecycle runs per lane concurrently,
in 3.3 s (0.33 s per lifecycle run). The former guest phase had 46 checks but
required staged profile mirrors absent from that tree; that missing prerequisite
was not a guest failure or proof of concurrent guest acceptance. The separate
concurrency demonstration and its actual outcomes are preserved in Measurements.

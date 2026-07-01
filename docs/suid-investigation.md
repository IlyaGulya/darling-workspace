# Running Darling guest tests without host setuid (SUID blocker)

Investigation for `dar-test-infra-sp5.2`. Goal: a documented way to run
`darling shell` (env=darling guest tests) in CI without depending on a
host-installed **setuid root** `darling` launcher.

All findings below were verified on this host: Ubuntu kernel **6.8.0-124**,
`unprivileged_userns_clone=1`, `apparmor_restrict_unprivileged_userns=1`
(Ubuntu 24.04 default).

## What actually requires privilege

The "mandatory setuid" gate is a single check in
`darling/src/startup/darling.c:64`:

```c
if (geteuid() != 0) { missingSetuidRoot(); return 1; }
```

> Sorry, the `darling` binary is not setuid root, which is mandatory.
> Darling needs this in order to create mount and PID namespaces and to perform mounts.

The setuid bit is **only** a way to reach `euid==0`. The real requirement is
**effective root for a set of privileged operations done once at prefix
bring-up**, then privileges are permanently dropped before any guest code runs
(`darlingserver.cpp`, `perma_drop_privileges()` before launchd). The privileged
ops are:

| Op | Where | Why root |
|---|---|---|
| `unshare(CLONE_NEWUTS\|CLONE_NEWIPC)` | darling.c:792 | new namespaces |
| `unshare(CLONE_NEWNS)` | darlingserver.cpp:598 | mount namespace |
| `clone(CLONE_NEWPID)` | darlingserver.cpp:693 | PID namespace |
| `mount("tmpfs", /dev/shm)` | darlingserver.cpp:613 | shared mem |
| **`mount("overlay", prefix)`** | darlingserver.cpp:623 | **the hard one** |
| `mount("proc", ...)` | darlingserver.cpp:707 | procfs for new pidns |

The **overlayfs mount** is the binding constraint. Source comment
(darlingserver.cpp:598): *"Since overlay cannot be mounted inside user
namespaces, we have to setup a new mount namespace and do the mount while we can
be root."*

There is **commented-out user-namespace support** in darling.c:831-862
(uid_map/gid_map writing, marked *"if we enable user namespaces"*) — never
finished, because of the overlay constraint above.

## Can we drop setuid via unprivileged user namespaces?

Short answer on this host: **no, blocked by AppArmor**, not by the kernel.

- Plain userns creation works:
  `unshare --user sh -c 'id'` → `uid=65534`. ✓
- But Ubuntu 24.04 ships an `unprivileged_userns` AppArmor profile
  (`/etc/apparmor.d/unprivileged_userns`) whose first line is
  **`audit deny capability`** — it strips *all* capabilities inside an
  unprivileged userns. So even `unshare --map-root-user` fails:
  `write failed /proc/self/uid_map: Operation not permitted`, and `mount`
  inside the ns has no CAP_SYS_ADMIN.

So on a stock 24.04 host, unprivileged-userns overlay is off the table without
either (a) disabling the AppArmor restriction
(`sysctl kernel.apparmor_restrict_unprivileged_userns=0`, host-wide, needs
root) or (b) authoring a permissive AppArmor profile for the launcher.

Note the kernel itself (6.8) *does* support unprivileged overlay-in-userns
since 5.11 — this is purely the distro's userns lockdown. On a kernel ≥5.11 host
**without** the AppArmor restriction (older Ubuntu, most non-Ubuntu distros, or
24.04 with the knob off), the commented-out uid_map path in darling.c is the
thing that would need finishing to go truly rootless. That is a Darling-core
change, out of scope for the test harness.

## What works today (verified)

Both verified live on this host, prefix at `~/.darling`, with a healthy
darlingserver already running:

1. **Setuid launcher** (the normal install):
   `~/work/darling-prefix/bin/darling` is `-rwsr-xr-x root root`.
   `darling shell echo GUEST_HELLO` → `GUEST_HELLO`, rc=0. ✓
   (This also confirms the local prefix is currently functional — relevant to
   `dar-test-infra-sp5.3`.)

2. **Non-setuid launcher run as root** (privileged-container / root-CI path):
   the *build-tree* launcher `~/work/darling-build/src/startup/darling` is
   **not** setuid (`-rwxrwxr-x ilyagulya`) — this is exactly what produced the
   "not setuid root" error live. But invoked through `sudo` so the process is
   already `euid==0`, the mandatory check passes and guest exec works:
   `sudo darling shell echo GUEST_AS_ROOT` → `GUEST_AS_ROOT`, rc=0. ✓

The takeaway: **the setuid bit is not what we need in CI — effective root is.**
A CI agent that is already root (root user in a privileged container) runs the
plain, freshly-built launcher with no setuid step at all.

## Recommendation for CI (decision)

Decouple Tier 1/2 (guest) CI from any host setuid by running the guest stage in
a container that grants the **single** capability the bring-up mounts need, and
pointing the harness at the build-tree launcher:

- Jenkins `docker { args '--cap-add SYS_ADMIN --security-opt apparmor=unconfined
  -u 0:0' }`. Verified live: `--cap-add SYS_ADMIN` alone is enough for *every*
  Darling bring-up mount (overlay, tmpfs /dev/shm, proc, the PID/UTS/IPC
  namespaces). This is strictly less than `--privileged` — one door, not all of
  them — and is the recommended CI minimum. Some strict daemons also need
  `--security-opt seccomp=unconfined` if the default seccomp profile blocks
  `mount`/`unshare`; add it only if a mount fails.
- The container process being root (`-u 0:0`) satisfies the `geteuid()==0` gate,
  so **no setuid bit** is involved at all.
- The existing `ci/Jenkinsfile` only **builds packages** — it never runs
  `darling shell`. So the guest stage is net-new; we are not regressing
  anything. It can be a separate stage gated on a `darling`-capable agent.
- `west test --env darling` then drives ctest exactly as locally.

Stopgaps / alternatives, in order of preference:

1. **`--cap-add SYS_ADMIN` container** (above) — works today, no Darling change,
   minimal privilege. Recommended. `--privileged` also works but opens every
   capability; prefer the single cap.
2. **Install the .deb/.rpm in the container** (which sets the setuid bit during
   packaging) and run as a normal user — same as a real user install. Heavier
   per-run, but mirrors production exactly.
3. **Finish the rootless path in Darling core** (uncomment + complete the
   uid_map work in darling.c, rely on kernel ≥5.11 unprivileged overlay or
   fuse-overlayfs). The only path to a *fully unprivileged* container (no caps).
   Largest effort, Darling-core change, tracked as `dar-test-infra-sp5.8`.

## Privilege matrix (all verified live, kernel 6.8, this host + Docker)

What it takes to run a `mount` (the binding op) — by where Darling runs:

| Environment | userns | caps | mounts work? | why |
|---|---|---|---|---|
| host, `unshare -r` (Ubuntu 24.04) | own | — | ❌ EPERM on uid_map | host AppArmor `unprivileged_userns` |
| host, parent-written map | own | — | ✅ map ok, ❌ mount EACCES | AppArmor denies CAP_SYS_ADMIN in userns |
| default container | — | — | ❌ even tmpfs EACCES | no CAP_SYS_ADMIN |
| container, `unshare(NEWUSER)` | own | — | ❌ EPERM | Docker seccomp blocks unshare |
| container + `seccomp=unconfined` | own | — | userns made, ❌ mount EACCES | container rootfs forbids nested mount |
| **container + `--cap-add SYS_ADMIN`** | — | SYS_ADMIN | ✅ **all: tmpfs, overlay, pidns** | direct mount right |
| container + `--privileged` | — | all | ✅ | — |
| rootless Docker / Podman | engine's | — | ✅ (expected) | engine delegates subuid maps |

Key conclusion: **fully unprivileged ("no caps") is not a single `docker run`
flag.** `mount(2)` requires CAP_SYS_ADMIN; the only no-root source is a userns
mapped to uid 0, and a *nested* userns inside a rootful container's rootfs does
NOT get real mount rights (proven: EACCES even with seccomp off). It needs the
**engine** to be rootless (Podman / rootless Docker) AND Darling to be
userns-aware — neither alone suffices. That two-sided change is `sp5.8`.

## Experiment log (`dar-test-infra-sp5.8`)

**E1 — copy-mode reach (`DARLING_NOOVERLAYFS=1`).** Darling already supports a
non-overlay bring-up: `shouldUseOverlayFs()` (darlingserver.cpp:305) honours
`DARLING_NOOVERLAYFS=1` and falls back to `copyAndSetAttributes()` instead of
the overlay mount.
- As a normal user (no root): **fails at the `geteuid()!=0` gate**
  (darling.c:64) — *before* any mount logic runs. So copy-mode alone does not
  enable rootless start; the early root gate is the first wall.
- As root, separate prefix: **`COPYMODE_ROOT_HELLO`, rc=0, and NO overlay mount
  on the prefix** (`mount | grep prefix` empty). Confirms the overlay mount is
  fully avoidable today via the existing flag.
- Takeaway: removing overlay is solved (flag exists), but the `geteuid()` gate
  and the remaining tmpfs/proc mounts still need either a cap or a working
  userns. Copy-mode is a necessary ingredient for rootless, not sufficient
  alone — it must combine with the userns path (Variant A).

**#3 — gate-softening recon (probe, reverted).** Softened the `geteuid()!=0`
gate behind `DARLING_ROOTLESS=1` on a throwaway branch, built the launcher
incrementally, ran as a normal user, then **fully reverted** (source back to
stock, rebuilt). Nothing pushed.
- The gate softening works: the launcher entered `CLONE_NEWUSER` and got past
  the "mandatory setuid" wall. **Next wall:** "You do not own the prefix
  directory" — euid ended at 65534, not 0, because the self-written `uid_map`
  was rejected.
- Isolated the cause in clean C probes:
  - **self-map** (unshare in same proc, write own `/proc/self/uid_map`):
    **fails** here — `uid_map: Operation not permitted`, map empty,
    `setuid(0)=EINVAL`. This is the Ubuntu 24.04 AppArmor lockdown.
  - **fork-map** (child unshares, *parent* writes child's `/proc/<pid>/uid_map`):
    mapping **succeeds** — `child after map: uid=0 euid=0`. But the subsequent
    `mount("tmpfs")` still returns `EACCES` — AppArmor strips CAP_SYS_ADMIN in
    the unprivileged userns.
- **Structural lessons for the real change:**
  1. `uid_map` must be written by a **parent after fork**, not self-mapped —
     `darling.c:831`'s commented block is already parent-side, so the shape is
     right.
  2. Even correctly mapped, the mount needs a host **without** the AppArmor
     userns lockdown (non-Ubuntu, `apparmor_restrict_unprivileged_userns=0`, or
     a rootless engine that pre-arranges the userns). Not validatable on stock
     Ubuntu 24.04 without weakening host AppArmor (declined).
- **Conclusion:** the userns path is structurally viable and small, but its
  payoff is entirely environment-dependent. The next real step (E2) should run
  on a non-Ubuntu host or under rootless Podman, **not** here.

### Breakthrough from the challenge/brainstorm (subagents) — a *second* path

A skeptical re-read of the code (confirmed by hand) found that **Darling's guest
root is not a kernel mount or chroot at all** — `vchroot` is pure userspace path
indirection: `setVchrootDirectory()` (`process.cpp:152`) `readlink`s
`/proc/self/fd/<n>` to a string, and that string is sent to `mldr`
(`dserver_rpc_vchroot_path`, `mldr.c:909`) which uses it as `root_path`. There is
**no `pivot_root` and no `chroot()`** anywhere in darlingserver. So the overlay
mount is *only* a directory-merge tool (system tree + writable prefix → one
tree), and the merge has unprivileged substitutes.

That reframes the whole problem. The privileged ops decompose, and each was
probed live (kernel 6.8, this host):

| Op | Purpose | Unprivileged replacement | Probe result |
|---|---|---|---|
| `mount overlay` | merge system+prefix | copy-mode (in code) / fuse-overlayfs / pre-bake | copy-mode verified (E1) |
| `mount tmpfs /dev/shm` | shared mem | engine already provides a tmpfs `/dev/shm` | ✅ **B1**: `/dev/shm` is tmpfs on host *and* in default docker, writable — Darling can skip it |
| `clone(CLONE_NEWPID)` + `mount proc` | launchd pid 1; reap orphans | `prctl(PR_SET_CHILD_SUBREAPER,1)` | ✅ **B2**: set + reaped an orphaned grandchild as a normal user; no host `getpid()==1` assumption exists (reaping is explicit `waitpid`, thread.cpp:142) |
| `unshare(NEWNS)` | scope the mounts above | not needed once no mounts remain | NEWNS at darlingserver.cpp:600 exists only to scope the mounts |
| `unshare(NEWUTS\|NEWIPC)` | isolation | likely droppable | no `sethostname`/`shmget`/`semget`/`msgget` anywhere — not load-bearing |

But: a normal user **cannot** `unshare(NEWUTS|NEWIPC)` nor `unshare(NEWNS)`
(both `EPERM`) — so namespaces still need CAP_SYS_ADMIN *or* a userns *unless
they are dropped*. That is the crux of the second path.

**Two distinct routes to unprivileged Darling now exist:**

1. **Keep overlay, add userns** (the Apptainer/Podman playbook):
   finish `darling.c:831` (parent-written uid_map) + `CLONE_NEWUSER` + overlay
   `-o userxattr` (kernel ≥5.13) or fuse-overlayfs fallback, keep a setuid build
   for legacy. Environment-fragile (Ubuntu 24.04 AppArmor blocks it here).
   Tracked: `dar-test-infra-sp5.8.5`.
2. **Remove every privileged op** (the no-mount path): copy-mode +
   skip-`/dev/shm` (B1) + subreaper-instead-of-pidns (B2) + drop
   NEWUTS/NEWIPC/NEWNS once nothing needs scoping → **no mount and no required
   namespace → neither CAP_SYS_ADMIN nor a userns needed**, runs in a plain
   `docker run`. Simpler, dodges the AppArmor problem entirely. Tracked:
   `dar-test-infra-sp5.8.4` (blocked on `.8.1` B2, `.8.2` B1, `.8.3` NS-audit).

The no-mount path (route 2) is the more promising and is unique to Darling's
userspace-vchroot architecture — most container tools can't take it because they
rely on a real kernel rootfs. Route 1 is the proven-elsewhere fallback.

### Experiment beads spawned

- `sp5.8.1` **E-B2** — subreaper replaces pidns+proc mount. *Probe passed; needs
  core prototype + guest-pid-1-view check.*
- `sp5.8.2` **E-B1** — skip `/dev/shm` mount. *Probe passed.*
- `sp5.8.3` **E-NS** — audit/drop NEWUTS/NEWIPC/NEWNS. *Code-grep done; strong
  evidence they're droppable once mounts go.*
- `sp5.8.4` **E-NOMOUNT** — end-to-end prototype combining all four (route 2).
- `sp5.8.5` — adopt the Apptainer playbook (route 1).
- `sp5.9` — 3-tier CI (host all-PRs / SYS_ADMIN guest-smoke post-merge / nightly
  full + macOS oracle); ship now, don't block on fully-unprivileged.

E2 (full userns impl) / E3 (fuse-overlayfs) remain under route 1.

### Route 2 prototype — built and run (`sp5.8.4`)

Implemented a single `DARLING_ROOTLESS=1` flag on a local experiment branch
(`exp/route2-nomount` in both the darling repo and the darlingserver submodule;
committed locally, **not pushed**; working tree then restored to the original
branch, stock binaries rebuilt, 7 G test prefix freed, installed darlingserver
restored). The flag gates **every** privileged op:

- launcher (`darling.c`): skip the `geteuid()!=0` gate, skip `setuid(0)`, skip
  `unshare(NEWUTS|NEWIPC)`, skip `joinNamespace(NEWNS)`, add
  `PR_SET_CHILD_SUBREAPER`.
- server (`darlingserver.cpp`): `g_rootless()` helper; skip the `getuid()!=0`
  root gate; make the three privilege-drop/regain helpers no-ops; skip
  `unshare(NEWNS)`; skip the `/dev/shm` mount; force copy-mode; replace
  `clone(CLONE_NEWPID)` with plain `fork()`; skip the `proc` mount; skip the
  `fchownat` in copy-mode.

**Run as a normal user (uid 1000, no root, no caps, no userns):**

- ✅ **Passed** the root gate, materialized the copy-mode prefix (7.1 G / 88 k
  files), **darlingserver came up as uid 1000** (verified `ps`), forked launchd
  instead of `clone`, did zero mounts. The entire bring-up ran unprivileged.
- A packaging detail surfaced: 3 CUPS backend files in `libexec` ship `0700
  root:root` and aren't readable by a non-root user, so copy-mode failed on
  them. Worked around with `chmod o+r` (a rootless package would ship them
  `0755` or omit them). **Not a blocker.**
- ❌ **Hard wall:** launchd dies immediately — `launchd: This program is not
  meant to be run directly.` (`mldr <defunct>`). Root cause: `launchd.c:163`
  guards `if (getpid() != 1 && getppid() != 1)`. Without `CLONE_NEWPID`, launchd
  is neither pid 1 nor a child of pid 1, so it refuses to boot. **The PID
  namespace is load-bearing for launchd's pid-1 identity — not just reaping.**

**What route 2 proved:** overlay, `/dev/shm`, the mount namespace, UTS/IPC, and
the setuid bit are **all removable and run unprivileged** (verified end to end).
The **single** irreducible privileged requirement that remains is the **PID
namespace**, because launchd insists on being pid 1. And `CLONE_NEWPID` needs
CAP_SYS_ADMIN or a user namespace.

So the problem shrinks to exactly one thing. Options to close it:

1. **Patch `launchd.c:163`** to accept a rootless sentinel (e.g. honour a
   `DARLING_NO_PIDNS` env) so launchd boots as non-pid-1. Smallest change, but
   alters launchd semantics — anything assuming pid 1 must be checked.
2. **Keep `CLONE_NEWPID` but acquire it via an unprivileged user namespace**
   (`unshare(NEWUSER|NEWPID)` — allowed unprivileged where AppArmor permits, no
   mount needed). This is route 1's userns but for the *pidns only*, with no
   overlay/mount — much narrower than the full route-1 plan.
3. **Accept that route 2 needs a userns purely for the pidns.** Still a big win:
   no mounts, no setuid, one namespace via userns.

Net: a fully-unprivileged Darling is reachable, and the surface is now a single,
well-understood blocker (launchd's pid-1 check) rather than the whole bring-up.

Bead status: `sp5.8.2` (skip /dev/shm) and `sp5.8.3` (drop NEWNS/UTS/IPC)
**closed-confirmed**; `sp5.8.1` (subreaper) **open-partial** (reaping works, but
pidns still needed for launchd); `sp5.8.4` carries the synthesis + next options.

### Route 2, variant 1 — patch launchd's pid-1 guard (tested)

Patched `launchd.c:163` (the guard) **and** `runtime.c:1389` (`pid1_magic`) to
honour `DARLING_ROOTLESS`, built launchd + darlingserver + launcher from the
experiment branch, ran as a normal user (no root/caps/userns), then restored
everything.

- ✅ **launchd now boots** — passes the guard, runs as uid 1000, a persistent
  `/sbin/launchd` process (not `<defunct>`). The guard patch itself works.
- ❌ **but launchd then hangs** — blocked in `recvmsg` on its darlingserver unix
  socket (`/proc/<pid>/stack`: `__skb_wait_for_more_packets →
  unix_dgram_recvmsg`), spawns no services, shellspawn socket never appears.

**Deeper root cause (the real coupling).** darlingserver keys every task on its
**namespace-relative pid**. The client reports its pid through
`dserver_rpc_hooks_get_pid`, which is a bare `getpid()`
(`mldr/resources/dserver-rpc-defs.h:26`). darlingserver builds the task with
that as the nsid (`call.cpp:74`: `make_shared<Process>(requestMessage.pid()=host
pid, header->pid=getpid(), …)`). **With** `CLONE_NEWPID`, launchd's `getpid()`
is 1 → the bootstrap task registers as nsid 1 and the Mach bootstrap port lookup
matches. **Without** the pidns (route 2), `getpid()` returns the host pid, the
init task is not nsid 1, and the Mach bootstrap/RPC routing never lines up → the
`recvmsg` wait never completes.

So the PID namespace is load-bearing not just for launchd's self-check (that
patched cleanly) but for **darlingserver's task-identity model**: the init task
must report nsid == 1. Patching the guard is necessary but not sufficient.

**Two ways to finish route 2:**

- **(A) Force the init process's reported pid to 1 when rootless** — mldr's
  `get_pid` hook returns 1 for the init process under `DARLING_ROOTLESS`, and
  darlingserver accepts a host-pid ≠ 1 mapping to nsid 1. Keeps the no-userns
  property; deeper change to the identity model.
- **(B) Acquire `CLONE_NEWPID` via an unprivileged user namespace**
  (`unshare(NEWUSER|NEWPID)`, no mounts). Smaller, and launchd keeps `getpid()
  == 1` for free — but reintroduces a userns (blocked by AppArmor on stock
  Ubuntu 24.04, fine elsewhere).

**Net:** route 2 removes overlay, `/dev/shm`, the mount namespace, UTS/IPC and
setuid — all verified unprivileged. The single irreducible requirement is the
PID namespace, because the init task must be nsid 1. The choice is (A) emulate
nsid-1 without a pidns, or (B) get the pidns from a userns. Both are tracked
under `sp5.8.4`; the experiment branch `exp/route2-nomount` holds the working
prototype (local commits, not pushed).

For the harness, no code change is required: `add_compat_test`'s `darling` env
already launches via `${DARLING_SHELL}`, and `west test --executor` wraps it
with the watchdog. The CI agent topology is the only lever.

### Route 2 closure — variants A & B implemented and under test (`sp5.8.4.1/.2`)

Both PID-namespace-closure variants are prototyped on `exp/route2-nomount`
(local commits only, **not pushed**). Run as a normal user, no setuid.

**Variant B (E-PID-B, `sp5.8.4.2`) — pidns via unprivileged userns.**
`DARLING_ROOTLESS_USERNS=1` makes darlingserver create launchd via
`clone(CLONE_NEWUSER|CLONE_NEWPID)`; the child maps itself to uid 0 in the new
userns, so `CLONE_NEWPID` is legal with no real privilege and launchd keeps
`getpid()==1` for free. No mounts. The server **parent** stays in the host
namespaces (keeps real uid + host pids for client RPC).
- Host environment check (precise): unprivileged userns is **blocked** on this
  Ubuntu 24.04 box (`kernel.apparmor_restrict_unprivileged_userns=1`; writing
  `/proc/self/uid_map` → EPERM) — expected.
- Container matrix for B: **`docker run --security-opt seccomp=unconfined`**
  ALONE enables unprivileged userns+pidns (no caps, no `--privileged`) — the
  default seccomp profile was the only blocker. **Rootless Podman** (installed:
  podman 4.9.3 + uidmap, subuid/subgid preconfigured) nests userns+pidns with
  **zero host privilege** — the canonical fully-unprivileged target.

**Variant A (E-PID-A, `sp5.8.4.1`) — nsid 1 without any pidns.**
`DARLING_ROOTLESS=1` (no userns). The init process is forced to register as
nsid 1 on the server so the Mach bootstrap/RPC routing lines up. Surface, in
order of discovery while debugging:
1. Client reports nsid 1: env `__mldr_rootless_pid1` (set by darlingserver only
   for the init mldr, consumed/unset in `unset_special_env`); mldr's get_pid
   hook returns 1; emulation layer asks mldr via a new `dserver_report_pid1`
   elfcalls callback (cached). ✓ launchd registered as **nsid 1**.
2. exec handling: `process.cpp` located the main thread by `nstid == nsid`,
   which fails when nsid is forced to 1 but the thread keeps its host tid → fixed
   by also accepting `thread->_tid == process->_pid` under rootless. ✓ got past
   "Main thread for process died?".
3. fork: a raw-fork child inherited init's `__mldr_report_pid1`/emulation cache
   and collided at nsid 1 → reset both in `__mldr_postfork_child` +
   `__dserver_report_pid1_reset()` in the fork child. ✓ got past the first
   fork-child-checkin timeout.
4. duplicate registration: a **secondary thread** of launchd still reported the
   real host pid before the pid1 flag was observed, creating a phantom second
   process `(host pid)` alongside `(1)`. Fixed authoritatively **server-side**:
   darlingserver records the init host pid it forked (`g_rootlessInitHostPid`)
   and, in `call.cpp`, forces nsid 1 (and uses it as the registry key) for any
   checkin whose SO_PEERCRED host pid matches — robust against client races.
- After the server-side anchor: launchd registers as **nsid 1 with no
  duplicate**, the clock-port/ipc-send flood is **gone**, and the
  fork-child-checkin timeout is gone. The nsid-1 identity is solid.

**Both variants converge on the SAME remaining wall.** Tested A on the host
(no userns) and B in **rootless Podman** (real userns+pidns) — both:
- bring up the copy-mode prefix unprivileged,
- boot launchd to pid/nsid 1 cleanly (no identity errors),
- fork exactly one child: **`/bin/launchctl bootstrap -S System`**, which then
  **hangs in `ep_poll`** waiting on a Mach reply, while launchd waits in
  `skb_wait_for_more_packets`. shellspawn (and every other LaunchDaemon) never
  starts, so the launcher times out connecting to the shellspawn socket.

Because A (no userns) and B (userns pidns) reach the **identical** stall by two
independent routes, the remaining issue is **not** the PID-namespace closure
(both solved that) — it is launchd's service bring-up: `launchctl bootstrap`'s
Mach call to the init bootstrap port doesn't complete in this rootless/copy-mode
configuration. That is a separate, deeper dtape/Mach-bootstrap-port problem,
not a privilege/namespace one.

**Net for `sp5.8.4`:** the PID-namespace blocker that motivated both beads is
*functionally closed* — there are now two working, fully-unprivileged ways to get
launchd to pid/nsid 1 (A: server-anchored nsid; B: unprivileged userns). The new,
narrower blocker is `launchctl bootstrap` Mach routing, shared by both and
independent of how the pidns is obtained.

### Route 2 closure — E-BOOTSTRAP root cause FOUND (`sp5.8.4.3`)

The earlier "Mach bootstrap-port" hypothesis was **wrong**. Reproduced variant A
on the host with debug instrumentation (vchroot tracing + launchctl `load`-path
markers, all reverted afterward) and traced the failure to the exact syscall.
The Mach bootstrap handshake between launchctl and launchd **works perfectly** —
every RPC round-trip completes. The real blocker is a **filesystem-emulation gap
in copy-mode**, nothing to do with Mach or the pidns:

Causal chain (proven from dserver debug log + per-step markers):
1. Rootless = **copy-mode prefix with no bind mounts**, so `<prefix>/proc` is an
   empty placeholder dir — there is **no `/proc/self/mounts`** inside the vchroot.
2. `launchctl bootstrap -S System` reaches `load -D all`
   (`launchctl.c:2482`→`load_and_unload_cmd`). It globs
   `/System/Library/LaunchDaemons` (exists, 20 plists) and calls `readpath()`,
   which `opendir()`s the directory.
3. `opendir()` (libc `gen/FreeBSD/opendir.c`) calls `__fd_is_on_union_mount(fd)`
   → `fstatfs(fd)` (opendir.c:72). `__kernel_supports_unionfs()` is true, so this
   path always runs.
4. `sys_fstatfs64` (libsystem_kernel `…/bsd/impl/stat/fstatfs.c:57`) opens
   **`/proc/self/mounts`** to fill `f_mntonname`. vchroot expands that to
   `<prefix>/proc/self/mounts`, which **does not exist** → `sys_open` returns
   ENOENT → `fstatfs` returns ENOENT.
5. `__fd_is_on_union_mount` returns <0 → `__opendir_common` `goto fail` →
   `opendir()` returns NULL, `errno=ENOENT`. (The `open(O_DIRECTORY)` itself
   *succeeded* — fd was valid; the failure is the `fstatfs`→`/proc/self/mounts`
   step *after* the open.)
6. `readpath` silently skips the dir → `load` builds an empty job list
   (`pass1 count=0`, "nothing found to load") → launchd is told to load nothing →
   **no LaunchDaemon is ever forked** (launchd makes exactly one
   `fork_wait_for_child`, for launchctl itself, and never again). shellspawn never
   starts → launcher times out on `…/var/run/shellspawn.sock`.

This is exactly why A (no userns) and B (userns pidns) converge: **both use the
copy-mode prefix with no `/proc`**, so both hit the same `fstatfs`/`opendir`
failure regardless of how the pidns was obtained. The `ep_poll` / `skb_wait`
observation in the bead was a red herring — that is launchd's normal idle
kevent/run-loop wait after it has (correctly) done nothing to load, plus
launchctl's normal 60 s oneshot-timer `kevent` at the end of
`system_specific_bootstrap`.

**Fix direction (not yet implemented — diagnosis only this session).** Make
`/proc` resolvable inside the copy-mode prefix without a privileged bind mount.
Options, cheapest first:
- Make `sys_fstatfs64` tolerate a missing `/proc/self/mounts` (treat open-failure
  as "not a union mount, no mount info" rather than propagating ENOENT) — this is
  the single line that breaks `opendir`, and `f_mntonname` is non-essential for
  the load path. Smallest, most targeted.
- Or have vchroot resolve guest `/proc` to the **host** `/proc` (Linux-root
  relative) instead of `<prefix>/proc` (there is already special-casing for
  `/proc` symlinks in `vchroot_userspace.c`; extend it so `/proc/self/mounts`
  reads the real procfs). Bigger but fixes all `/proc` reads in copy-mode.
- Or have the rootless launcher populate a minimal `<prefix>/proc/self/mounts`
  (a static file) at prefix init. Hacky; doesn't help other `/proc` reads.

The pidns closure (A and B) is functionally done; **E-BOOTSTRAP is now a concrete,
localized copy-mode `/proc` / `fstatfs` bug, not a Mach problem.** All experiment
work remains local on `exp/route2-nomount` (nothing pushed); debug instrumentation
was reverted and stock binaries/branches restored after the trace.

### Route 2 — E-BOOTSTRAP fixed (both candidates), new wall exposed (`sp5.8.4.3`)

Implemented and tested **both** fix candidates on local branches (nothing pushed):
- **Fix 1 — `sys_fstatfs64`** (xnu `…/bsd/impl/stat/fstatfs.c`, branch
  `exp/route2-fstatfs-fix` off the xnu commit route2 pins, `81982805`). When
  `/proc/self/mounts` can't be opened, return 0 with the statfs already filled by
  the Linux `fstatfs` syscall, leaving only the cosmetic `f_mntonname`/`f_fstypename`/
  `f_mntfromname` strings empty, instead of returning the open error.
- **Fix 2 — `__fd_is_on_union_mount`** (libc `gen/FreeBSD/opendir.c`, branch
  `exp/route2-opendir-fix` off `5a38c8d`). When `fstatfs` fails, treat the
  directory as **not** a union mount (return 0) rather than propagating `rc<0`.

**Test (variant A, no userns, host).** Each fix built/installed in isolation
(the other reverted), fresh copy-mode prefix, run via the debug runner with a
hard timeout + process-group terminate. launchctl was instrumented with a direct
`DBGF()` file writer to `/var/log/launchctl-dbg.txt` (syslog path is invisible
under `_launchctl_is_managed`). **Both fixes give byte-identical results:**
```
DBG glob(/System/Library/LaunchDaemons) ret=0 gl_pathc=1 errno=2
DBG readpath(/System/Library/LaunchDaemons): opendir OK, scanning   <- was "opendir failed errno=2"
DBG pass1 final count=14                                            <- was 0
```
So **both fixes fully resolve E-BOOTSTRAP**: `opendir` now succeeds and launchctl
finds + submits 14 jobs (shellspawn among them, `RunAtLoad=true`, not Disabled).

**Which is more correct: Fix 1.** `fstatfs` callers are many — libc `statvfs`/
`fstatvfs`, `SCPLock`, `wc`, `tail`, sqlite3, OpenJDK, `SecTranslocate`, and
`opendir`. In rootless copy-mode every one of them currently gets a spurious
ENOENT. Fix 1 restores correct Darwin semantics (`fstatfs(valid_fd)` succeeds)
and heals **all** callers; the `/proc/self/mounts` scan was only ever cosmetic.
Fix 2 patches a single caller and leaves the underlying syscall bug in place.
Fix 2 is the more conservative change (can't regress other fstatfs consumers),
but Fix 1 is the right layer. **Recommendation: ship Fix 1.** (Optionally keep
Fix 2's defensive "fstatfs-failure ⇒ not-a-union" as belt-and-suspenders, since
it is also a legitimate robustness improvement.)

### Route 2 — FUNCTIONALLY CLOSED (`sp5.8.4.3` → `sp5.8.4`)

With the E-BOOTSTRAP fix in place, a fully-unprivileged rootless guest **boots and
runs a real shell**:
```
$ DARLING_ROOTLESS=1 DPREFIX=<fresh> darling-rootless shell \
      /bin/bash -c 'echo UID=$(id -u); uname -sm; echo PIPE | tr A-Z a-z'
UID=1000
Darwin x86_64
pipe
exit status: 0          # shellspawn.sock created; opendirectoryd/securityd/etc. spawn
```
No setuid, no mount/PID/UTS/IPC namespace, no overlay, no `/dev/shm`. Verified with
**Fix 1 alone** (clean code, instrumentation reverted) on variant A.

**A second wall (“E-SOCKET”) turned out to be a test-harness artifact.** While
instrumenting, a run could stall with: launchd `ipc_server_init` →
`mkdir("/var/tmp/launchd")` fails **ENOENT** (copy-mode prefix had no `/var/tmp`)
→ launchd never creates its `LAUNCHD_SOCK_PREFIX/sock` UNIX socket → launchctl’s
`submit_job_pass`→`launch_msg` gets **ENOTCONN (errno 57)** → SUBMITJOB never
reaches launchd (`ipc_readmsg2` never sees it) → launchd dispatches/​spawns nothing.
Root: this only happens when the prefix **directory is pre-created empty**, because
`darling.c` `main()` calls `setupPrefix()` only when `checkPrefixDir()` reports the
prefix absent — an existing (even empty) dir skips the standard dir list (which
includes `/var/tmp`). The old `/tmp/run-rootless-darling.sh` did `mkdir -p $PREFIX`
first, so it bypassed `setupPrefix()` and inflicted the wall on itself. **Letting
the launcher create the prefix normally → `setupPrefix()` makes `/var/tmp` →
launchd’s socket comes up → everything spawns.** So route 2 needs **only** the
E-BOOTSTRAP fix; no `/var/tmp` code change for normal use. (Optional hardening:
`ipc_server_init` could create the parent dir, or `setupPrefix()` could also run
when an empty prefix dir pre-exists — defends against a pre-created prefix.)

**Decision: Variant 1 adopted on `exp/route2-nomount`.** The xnu submodule on
`exp/route2-nomount` now includes commit `4ebed05` “sys_fstatfs64 tolerate missing
/proc/self/mounts”, and the main repo records that pointer (`6624650`). libc is back
to its clean route2 pointer (`5a38c8d`); Variant 2 is preserved (unused) on
`exp/route2-opendir-fix` as the conservative alternative. Re-verified end-to-end on
the final config: rootless guest runs `bash -c '… id -u; uname -sm; tr …'` →
`UID=1000`, `Darwin x86_64`, exit 0. All `DBGF` instrumentation reverted; installed
prefix binaries are clean code + Variant 1. Nothing pushed.

## Bottom line

- SUID is **not mandatory**; *effective root at bring-up* is. ✓ proven both
  ways (setuid launcher; non-setuid launcher as root).
- **CI plan (today):** run env=darling tests in a `--cap-add SYS_ADMIN` root
  container (one capability, not `--privileged`); no setuid, no harness change.
  Unblocks `dar-test-infra-sp5.4` / `.7`.
- **Fully unprivileged (no caps)** is a two-sided change — rootless engine
  (Podman/rootless Docker) **plus** userns-aware Darling (finish darling.c:831 +
  userxattr overlay or fuse-overlayfs). Neither alone works (proven). Tracked as
  `dar-test-infra-sp5.8`; copy-mode (E1) is one ingredient, already in code.

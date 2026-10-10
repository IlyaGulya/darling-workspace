# Handoff — iOS/Xcode T4 under Darling (2026-10-10)

Written for the next agent. Facts only; every claim here was measured in this workspace, and the
places where an earlier inference was WRONG are called out so nobody relitigates them.

## 1. Goal and where the ladder stands

Goal: build a real iOS application with Xcode under Darling on Linux, reproducibly, for CI.

- T1 (real Xcode clang runs in the guest): PASS
- T2 (real arm64 iPhoneOS objects from the real SDK): PASS — the gate's three compiles return `rc=0` on a healthy runtime
- T3 (real Apple `ld` launches): PASS
- **T4 (minimal iOS link): RED, and now RED for a known reason**

The gate (`tests/run-ios-toolchain-contract.sh`) drives T4 as a `clang` link of
`hello_c.o + hello_main.o -> hello_ios`. It currently does not link, and the reason is the
**invisible parking** defect described in section 4.

## 2. Canonical state (read this before touching anything)

- Workspace: `/home/ilyagulya/work/wr-fresh/darling-workspace` (manifest repo, branch `change/ios-toolchain`).
- Prefix under test: `/home/ilyagulya/work/wr-fresh/prefix-ios` (minimal bootstrap, currently **bootable, clean**).
- Build dir: `/home/ilyagulya/work/wr-fresh/build/toolchain` (this is the one the prefix is baked for).
- Component checkouts right now:
  - `darling/src/external/xnu` — branch `fix/private-ulock-return-channel` @ `572ecca` (my ulock fix, see 3.1). Carries pre-existing dirty files from earlier sessions (`simple.c`, `sigexc.c`, `open.c`).
  - `darling/src/external/darlingserver` — branch `fix/psynch-ring-classification` @ `21f8a0c` (ahead of its pin; contains the server half of the ulock fix).
  - `darling/src/external/libdispatch`, `darling/src/external/libpthread` — **detached at their manifest pins** `380f03c1` / `8fd9324b` as of this handoff (see 5.1). Their probe branches `diag/pool-growth-markers` still exist and hold the commits; libdispatch's uncommitted work is saved at
    `hunt/t4-bsdreply-trace/diag-patches/libdispatch-uncommitted.patch`.
  - `darling` itself — `1534501b` (reported `diverged` from the pin by the doctor; pre-existing).
- Verified commits made this session (all in the manifest repo unless noted):
  - `572ecca` (xnu fork, branch `fix/private-ulock-return-channel`) — raw ulock errno + wake rings the fallback queue
  - `3ea9e60e` — libxml2 declared as a runtime addition, guest-visible mirror installs new files
  - `86463d25` — docs: a direct launcher invocation needs `DPREFIX`
  - `939ef59f`/`439ef59f` — runtime: say what a prefix holds + bootstrap note + doctor message
  - `979e6198` — harness/cleanup count mounts by mountpoint
  - `f877f5a1`, `c6458406` — the Xcode-free strand reproducer and its driver env fix
  - `d9330d99` — doctor: report a build tree that moved after a deploy
  - `ace5e1a7`, `05fdbee5`, `6e585f26` — doctor boot smoke (own session, run-log quoting, poll line, builtin echo, measured costs)
  - `c68e84d1` — deploy: verify the chain boots automatically and roll back when it does not
  - `95d7021f` — doctor: name a component on a diagnostic branch

## 3. What is fixed and verified

### 3.1 The T4 SIGILL (fixed)

`__os_unfair_lock_unlock_slow + 0x110` in `libsystem_platform` executed its `ud2` because the
private `__ulock_wake/wait/wait2` syscall stubs were generated with `cerror`, which rewrites the raw
negated errno (`-ENOENT`) into `-1`. Consumers (libplatform, libdispatch, libpthread) read the RAW
value and accept only a small set, so the call was classified as unexpected and killed the process
(measured with a core: the saved return value was `-1` while the emulation produced `-ENOENT`).
Darwin is safe because `bsd/kern/sys_ulock.c` (`munge_retval`) stores `-ret` into `*retval` and
returns 0; Darling's emulation delivers that value in the return instead.

Fix: custom stubs `libsyscall/custom/__ulock_{wake,wait,wait2}.S` (installed by the generator's
existing custom-stub mechanism) plus a successful wake that also rings the host futex.
Verified: 4/4 minimal repro runs stop producing `DISPATCH_INTERNAL_CRASH`/SIGILL (about half before).

### 3.2 A missing runtime dependency (fixed)

The real `ld` aborted with `dyld: Library not loaded: /usr/lib/libxml2.2.dylib` (from Foundation).
`src/external/libxml2/install` is now a declared runtime addition, and the guest-visible mirror also
*installs* declared files that do not exist there yet (it used to refresh only files both trees
already had), so `usr/lib/libxml2.2.dylib` now exists (refreshed count 48 -> 49).

### 3.3 The Xcode-free strand reproducer (verified)

`tests/ios_ring_strand_guest.c` + `tests/run-ios-ring-strand-contract.sh`, ~31s for both arms, no new
print layer: it distinguishes transport by the existing semantics (the futex fallback returns a
constant 1 while the server-owned queue returns the true remaining depth, `{1,1,1,1}` vs `{0..K-1}`).
Measured on the current runtime:

```
arm=product  fixture=fail harness=FAIL host_futex_wait=1 depth_contract=violated expect_host_futex=0   <- the RED
arm=datagram fixture=pass harness=PASS host_futex_wait=1 depth_contract=violated expect_host_futex=1
```

### 3.4 The development self-check (verified; now automatic)

- `west darling-doctor --scope runtime --prefix P --build-dir B` reports "the prefix does not hold the
  current build" when deployed bytes and the build tree diverge (stale-source check).
- `west darling-build ... --deploy` now runs the post-deploy doctor **with the boot smoke**, so a
  deploy that leaves an unbootable chain is caught and **the transaction is rolled back**. This
  caught five bad deploys in a row this session and kept the prefix bootable (`--skip-boot-smoke`
  opts out; `--skip-post-doctor` skips everything).
- `west darling-doctor --scope workspace` warns when a component sits on a `diag/*` branch (probe
  output interleaves with guest verdict lines). It immediately found `libdispatch` and `libpthread`
  on `diag/pool-growth-markers`.

## 4. The T4 blocker, as measured

**Mechanism (from a code scout, with file:line):** a blocking op that cannot get a ring lane (no lane
/ c2s ring full / the ~3s lane-acquire budget exhausted) falls through to the retired per-thread
datagram, which the hard socket gate denies (`[rpc-socket-DENIED]`); `ulock_wait` then parks the guest
thread in a **host futex the server cannot see**, while the paired wake rides the server path and
reports success. `psynch_mutexwait` aborts in the same situation instead.

**Measured shape of the stall** (fresh runtime, real link):
- guest: both `ld` threads in `futex_wait_queue` (Linux syscall 202), word value 29;
- server: process `darlingserver 4 3 prefix-ios 6 1000 1000 8 0`, one thread in `ep_poll` (event
  loop believes it has nothing to service) and one thread in `futex_wait_queue`.

**Architectural fix (design step 6):** no blocking op may park invisibly. Either it rides the server
queue, or it fails loudly the way `psynch_mutexwait` does. The host-futex fallback may remain only
where a lane objectively cannot exist (pre-migration loader image, fork child) — the strand
reproducer's datagram arm tests exactly that path and expects the futex there.

**The op ledger** (designed and implemented this session, `west_commands`-independent, in the runtime):
one declarative registry `DSERVER_LEDGER_POINTS` in
`darlingserver/include/darlingserver/rpc-supplement.h` (labels `g-pub`, `g-nolane`, `s-rx`, `s-drop`,
`s-tx`, `g-rep`), one line format, a shared `(tid, seq)` key (tid is the guest host tid == server
nsid; seq is the per-lane slot the guest writes and the server echoes; `seq=0` means the lane was not
used). Enable with `DARLING_OP_LEDGER=1` (default off, cap `DARLING_OP_LEDGER_MAX`, default 65536).
Reading: `grep '\[ople tid=T seq=S\]' run.log` — the first missing point is the break. Removal path:
delete the registry block and every site with the token `ople`.
**It has NOT yet been exercised end-to-end**, because the runtime that carries it cannot currently be
deployed into the prefix (see 5.1).

## 5. Open blockers

### 5.1 A dev deploy that replaces `dyld` leaves the chain unbootable — dar-8iz1

Bisect matrix (every attempt auto-rolled back by the boot gate, prefix stayed bootable):

| attempt | result |
|---|---|
| `--deploy --deploy-darlingserver` (dyld + 35 closure + server) | FAIL 5, shellspawn not ready |
| `--deploy` (dyld + closure, no server) | FAIL 5 |
| `--targets darlingserver dyld mldr mldr32 --deploy --deploy-closure-names libsystem_kernel.dylib --deploy-darlingserver --deploy-mldr` | FAIL 5 (summary named `+ mldr` only) |
| `--deploy --deploy-darlingserver --deploy-closure-names libsystem_kernel.dylib` | FAIL 5 |
| after pinning `libdispatch` + `libpthread` to their manifest revisions | FAIL 5 — **hypothesis eliminated** |

The profile's own deployment boots. Every failing attempt replaced `dyld` and differed from the
receipt (`f48c3a905d7a` vs receipt `3db0f1957d30`).

**Not yet tested:** pinning `xnu` and `darlingserver`. The attempt aborted because `git checkout`
refused (`Aborting`) — `darling/src/external/xnu` carries uncommitted changes from earlier sessions
(`darling/src/libsystem_kernel/emulation/src/common/simple.c`,
`.../linux_premigration/signal/sigexc.c`, `.../xnu_syscall/bsd/impl/fcntl/open.c`).
Do: save those as a patch (as was done for libdispatch), pin both, `darling-build ... --deploy
--deploy-darlingserver` under the gate, and only name the incompatibility if it still fails.

### 5.2 The ulock step-6 fix

Not started. Its acceptance is the strand contract's product arm going `fixture=pass`
(`host_futex_wait=0`, `depth_contract=ok`), then the toolchain gate T1–T4.

### 5.3 The ledger end-to-end exercise

Blocked by 5.1. Once a development runtime can be deployed, run the strand contract with
`IOS_UNFAIR_LOCK_GUEST_ENV='DARLING_OP_LEDGER=1'` and read the first missing point.

## 6. Commands that work (copy-paste)

```bash
cd /home/ilyagulya/work/wr-fresh/darling-workspace
export PATH="$HOME/.local/bin:$PATH"
DP=/home/ilyagulya/work/wr-fresh/prefix-ios
B=/home/ilyagulya/work/wr-fresh/build/toolchain

# Doctor (both scopes). --build-dir is REQUIRED: the default is not this build tree and the runtime
# scope then fails on a missing CMakeCache.txt.
mise run west -- darling-doctor --scope runtime --prefix $DP --build-dir $B
mise run west -- darling-doctor --scope runtime --prefix $DP --build-dir $B --boot-smoke   # ~13s healthy
mise run west -- darling-doctor --scope workspace --prefix $DP --build-dir $B

# Deploy (the post-deploy doctor runs the boot smoke and rolls back an unbootable chain)
mise run west -- darling-build --build-dir $B --prefix $DP --deploy --deploy-darlingserver \
    --shutdown-before-deploy --force

# Xcode-free strand reproducer (two arms, ~31s; also verifies prefix hygiene)
DPREFIX=$DP DARLING_BUILD_DIR=$B bash tests/run-ios-ring-strand-contract.sh
#   with the ledger:
DPREFIX=$DP DARLING_BUILD_DIR=$B IOS_UNFAIR_LOCK_GUEST_ENV='DARLING_OP_LEDGER=1' \
    bash tests/run-ios-ring-strand-contract.sh

# Toolchain gate T1-T4 (harness boots the prefix itself)
DPREFIX=$DP XCODE_APP=/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app \
    bash tests/run-ios-toolchain-contract.sh

# Prefix recovery after an interrupted run
scripts/prefix-cleanup.sh --prefix $DP --settle 2         # prefix-owned processes -> 0
env DPREFIX=$DP $DP/bin/darling --rootless shutdown
mise run west -- darling-prefix-repair --prefix $DP        # lifecycle files / stale pid+socket
#   Last resort (documented remedy; drops the iOS runtime additions, so re-run them after):
#   rm -rf $DP && WEST_RUNTIME_BUILD_CACHE=off scripts/west-job.sh start --state-dir <dir> -- \
#     mise run west -- test --prefix $DP --bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal
#   then: scripts/ios-toolchain-runtime-additions.sh --build-dir $B --prefix $DP --receipt <path>

# Runtime additions (also the place to declare a new missing dependency; never copy by hand)
scripts/ios-toolchain-runtime-additions.sh --build-dir $B --prefix $DP --receipt <path>

# Beads
mise run west -- dw beads show dar-8iz1
mise run west -- dw beads comment <id> "<text>"          # no backticks in shell-passed text
```

## 7. Pitfalls that cost real time here (read before debugging)

1. **`DPREFIX`.** `prefix/bin/darling --rootless shell …` resolves the prefix from `DPREFIX`, which
   defaults to `~/.darling`. Without it a perfectly healthy prefix answers
   `no recognized stable state: recreation required`. Always pass `DPREFIX` (or `DARLING_PREFIX`)
   on a direct launcher invocation. This once cost an unnecessary prefix recreation.
2. **What a prefix holds.** A bootstrap builds a runtime from the profile's **pinned** sources and
   deploys that; a working-tree change is in the prefix only after an explicit
   `darling-build … --deploy` run *after* the bootstrap. Evidence: deployed guest
   `libsystem_kernel.dylib` carried 0 occurrences of a development marker while the build dir had 30.
   The bootstrap now prints this, the doctor detects the divergence, and a deploy verifies the boot.
3. **The consistent chain.** Deploying `dyld`/closure without the loader pair can leave a chain that
   never reaches a shell (`Rootless shellspawn did not become ready within 30000ms`). This is a
   measured class, not a hint; dar-8iz1 has the matrix.
4. **Probe builds.** Components on `diag/*` branches write probe lines to fd 2 that interleave with
   the guest's own verdict lines, so a fixture can look broken and a log can look like a hang. The
   doctor now names them (it found `libdispatch` and `libpthread`). Keep fixes on clean `fix/*`
   branches and return checkouts to their pins before bootstrapping/deploying.
5. **The harness judges by its `--poll` line, not by `--marker`.** Without `--poll` it waits for
   another test's pattern and reports `VERDICT: FAIL` after the full wait.
6. **A minimal prefix has no `/bin/echo` and no `/bin/sh`** in the guest: smoke commands must be
   shell **builtins** (`echo MARKER`), otherwise the harness reports a failure that looks like an
   unbootable chain.
7. **The harness signals its own process group when a guest run fails.** Anything that shells out to
   it must run it in its own session (`start_new_session=True`), or the caller dies with SIGTERM and
   loses its report (this happened to the doctor and to the strand driver).
8. **Mount hygiene counts by mountpoint, not by substring.** Another lane's overlay instance names
   this prefix in its `lowerdir`; a substring match made every run report `mounts: 1` and fail
   (dar-c4ff). Fixed in `scripts/darling-boot-run.sh` and `scripts/prefix-cleanup.sh`.
9. **Bootstrap needs `WEST_RUNTIME_BUILD_CACHE=off`** in this workspace (dar-390v): the shared cache
   registers Git worktrees that make the evidence-unit check refuse the bootstrap.
10. **`darling-debug`** (the live server-state client) is **ours**, not upstream: server side in this
    fork's `darlingserver` (`src/debug-connection.cpp` etc., opt-in `DSERVER_DEBUG_ABI=1`), client in
    the sibling repo `/home/ilyagulya/work/darling-native-debugger-runner` (prebuilt at
    `target/release/darling-debug`, subcommands `threads/wait/transport/list/…`). The client needs the
    server to expose the listener: the currently deployed `bin/darlingserver` predates the debug ABI
    (string absent), while the built one has it.

## 8. Wrong starts — do not relitigate

- "Darling writes a proprietary core format": **false** — the core is a standard ELF64 core; the
  failure was passing the core as the executable to gdb without the binary. `dwdiag cores on` +
  `dwdiag core` is the supported route and it named the faulting function.
- "The prefix state file is broken / repair cannot clear it" (dar-73pt): **false** — the refusal came
  from not passing `DPREFIX`. The bead was corrected and closed.
- "The `FUTEX_PRIVATE_FLAG` on a shared mapping is the cause": unproven; the futex retry I added for
  it was reverted. The real cause was the cerror'd errno (3.1).
- "The ledger's sink interleaves and breaks the harness": **false** — those runs were on a prefix
  whose chain did not boot (5.1), which is why they produced nothing.

## 9. Evidence in this workspace

- `hunt/t4-bsdreply-trace/` — deploy logs (each bisect `deploy-*.log`), gate logs (`gate-*.log`),
  strand runs (`ring-strand-*.log`), doctor runs (`doctor-bootsmoke*.log`), the core-derived
  measurements, and `diag-patches/`.
- Cores: `/tmp/t4cores/` (generated with `dwdiag cores on`).
- Beads: **dar-8iz1** (deploy/dyld, open), **dar-lf4b** (the ulock slice; comments record the core
  measurement, the server-side stall, and the T4 advance), **dar-c4ff** (mount hygiene, fixed),
  dar-390v (cache vs evidence unit), dar-1bi3 (CLT profile vs minimal bootstrap, still open),
  dar-73pt (closed as a wrong record).

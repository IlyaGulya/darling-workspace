# WR — recovered Ring product candidate (West manifest checkpoint)

`WR` is the first reproducible Git/West representation of the recovered Ring
product. It supersedes the earlier "reconstruct W1–W5 from topic history" plan:
the accepted product source survives as a physical forest, so it is preserved
directly as ordinary per-component commits instead of being re-derived from
fragments.

Source authority (ADR 0001): this `darling-workspace` commit plus the resolved
West manifest. `west manifest --freeze` is derived evidence, never a second
source of truth. The old patch/profile/lock layer stays `FROZEN_LEGACY`.

## Recovery sources

- physical forest: `/home/ilyagulya/work/r1-repro` (pristine; 571 970 files, one
  empty `init` commit, everything untracked) — the materialized forest the
  accepted runtime was built from.
- its build: `/home/ilyagulya/work/r1-repro-build` (configured against the
  forest; `rootless_bootstrap` builds rc=0).
- secondary delta evidence:
  `/home/ilyagulya/work/darling-dev/evidence/r1-transport-sources-20260928T112410Z.tar.gz`
  (438 paths) + `.sha256`, and
  `darling-workspace/diagnostics/ring-loader-fixes-20260930/` (the later
  loader fixes).
- earlier reconstruction trees: `r1-clean-base`, `r1-clean-ref`,
  `r1-clean-build`.

The Sep-28 archive is a recovery **input**, not the final product: it predates
the Sep-30 loader fixes. `WR` preserves the **Sep-30 forest** (`r1-repro`), so
tree equality is the physical forest, not the archive.

## Component recovery bridge commits

Each bridge commit's tree equals the corresponding pristine `r1-repro`
component tree (verified by entry count: files + symlinks). Bridges are ordinary
commits whose parent is the revision the forest base was materialized from; no
historical authorship is claimed.

| component | branch | bridge commit | base parent |
| --- | --- | --- | --- |
| darling | `recovery/transport-bridge-darling` | `dfc302f60232a436624887d61745b58394ec9321` | `5f2d7401` |
| darlingserver | `recovery/transport-bridge-darlingserver` | `9e1d3d989caf5a918c91cb399e5a3efea947e729` | `89751e64` |
| xnu | `recovery/transport-bridge-xnu` | `3ef268818f870cff7a1fd315004220389b4b2bb9` | `5f26a4c2` |
| libmalloc | `recovery/transport-bridge-libmalloc` | `8c3d3863f2add948cbcb9667a8fb767969a2d688` | `a57991e2` |
| dyld | `recovery/transport-bridge-dyld` | `7f0fd6d9672b08bfcab9fe53453d565f5f6eb7c7` | `63f667cf` |
| installer | `recovery/transport-bridge-installer` | `bb68430c98dd295c6f3a91c4a4796be99cf92423` | `88764e61` |
| perl | `recovery/transport-bridge-perl` | `6c27f05a29fa6dc76fd14e485c4a76f100e6be3e` | `a65d68be` |
| libkqueue | `recovery/transport-bridge-libkqueue` | `660ebc3ae078a264503f1f54a9b76f65d46bfe2f` | `f673c801` |
| libressl-2.8.3 | `recovery/transport-bridge-libressl-2.8.3` | `45f14a83115947ba36a33572e29975a8dd811eb5` | `2a56b36b` |
| libpthread | `recovery/transport-bridge-libpthread` | `8fd9324b044470a6f659f02477785df2c89160eb` | `f07f265b` |

`libunwind` is pinned to `d5fbc8180ea2e06efa693ee03a768e532a98671f` (a build
prerequisite, not part of the recovery: without `-fno-jump-tables` on
`unwind_static` the dyld/system_loader link fails).

darling's bridge commit also aligns its submodule gitlinks with the manifest
pins (the nine recovered components above plus `libunwind`, `libplatform`,
`objc4`); every other gitlink is unchanged.

### Exclusions (recorded, not silent)

- `src/external/darlingserver.product` — a staging copy of `darlingserver`
  present in the forest, referenced by no `CMakeLists.txt`, cmake file, script,
  manifest or build directory. Excluded as a forest-local artifact.
- `darling/docs/darling-docs` — a West-managed nested project inside darling's
  worktree (its own manifest entry), not darling product source; excluded from
  darling's worktree status via `.git/info/exclude`.

## Checkpoint state

- workspace commit: `WR` = `795451d1` on `change/ring-reintegration-v1` (the
  manifest-pins checkpoint; later documentation-only commits do not change the
  pin set).
- resolved component SHAs: the table above (also `west list`).
- `west manifest --freeze` digest:
  `sha256 8dd3170bf3819cb8b30eca7514333d4029e6e3f653517c0fd6502f36a907730c`
  (all eleven pins resolve in the freeze; the freeze reads `manifest-rev`, so
  `west update` was run for the recovered projects first).
- `dirty = 0` for every recovered component; untracked product source = 0.
- `west manifest --validate` passes.

## Next (after this checkpoint)

1. Build `WR` fresh from a disposable checkout with the canonical configure/build.
2. Boot it through the **canonical harness** (the one that supplies
   `DPREFIX` + `DARLING_PREFIX` + rootless settings), not a hand-written
   environment: a manually approximated environment produced a fake
   `plane-refuse why=no-page` defect (see `docs/ring-reintegration-dag.md`).
3. If the `vchroot: execv: Bad file descriptor` seen in the manual smoke does
   **not** reproduce under the canonical harness, classify it as a
   manual-harness artifact and proceed to the Ring gates; if it does reproduce,
   resolve the actual `_execv` binding for the deployed `vchroot` Mach-O before
   adding further instrumentation.
4. Gates: focused Ring/RPC host contracts, then 9/9 first attempt
   (`ROW-RETRY 0`, `created=0`, `denied=0`), then ≥200 consecutive clean boots.
5. Provenance extension; NOFILE (`dar-dar6x4-perf-5dq.34`) only after Ring
   acceptance.

`r1-repro` is evidence only from this point: no further product edits there.

## Checkpoint identity

```text
workspace  change/ring-reintegration-v1 @ 3e5562f9  (WR checkpoint + token-identity fix)
xnu        f3e71f3997a898ea7962e43dfad32c500efae97e  fix/ring-courier-token-image-identity
darling    30fa003112412ae9f7b4d72302eef1df0c242732  fix/ring-courier-token-image-identity
```

## WR boot blocker resolved: cross-image fd-courier token collision

The canonical-harness smoke **did** reproduce `vchroot: execv: Bad file
descriptor` (resolving the open question in step 3 above), so it is a product
defect, not a manual-harness artifact.

Resolved boundary and measured chain (each step is a probe, not an inference):

```text
vchroot  execv("/sbin/launchd")
  -> sys_execve            entered (fname=/sbin/launchd)
  -> sys_open              ret=7   (openat resolved /home/.../prefix/sbin/launchd)
  -> sys_read              ret=256 (Mach-O magic)
  -> checkout              ret=-9  EBADF          <-- here
  -> server case CHECKOUT: resolveFdCourierBundle(...) != Resolved -> status = -EBADF
     [courier-store]        pid=242210 token=5189026761396706776 kind=1   (lane backing)
     [courier-kindmismatch] pid=242210 token=5189026761396706776 want=2 got=1
```

Root cause: the token is `generation*A ^ pid*B ^ per-image counter`. mldr and
libsystem_kernel are two copies of that formula inside **one** process: same
pid, same generation, counters that both start at the same value. The loader's
first lane-backing bundle (kind=1) and the guest's first execve checkout bundle
(kind=2) therefore carried the **same** token; the server keys a pending
descriptor by token, dropped the checkout envelope as a duplicate, and the
checkout then resolved as `KindMismatch` (want=2 got=1) -> `-EBADF` -> boot
stopped before shellspawn, with launchd never reaching its runtime.

Retired hypothesis (measured, not assumed): `vchroot` closing its root `dfd`
(`orig=4 valid=1`, `dfd=3`, `same=0`). Closing it was restored after the
experiment; the failure was identical with the fd left open.

Fix (normal commits, both `fix/ring-courier-token-image-identity`):
mix the per-image counter's own address into the token in both senders.

```text
xnu      f3e71f3997a898ea7962e43dfad32c500efae97e
darling  30fa003112412ae9f7b4d72302eef1df0c242732
```

Verified: with the diagnostics reverted, the canonical-harness rootless
bootstrap smoke reaches `launchd-RUNTIME_ENTER` with no
`execv: Bad file descriptor`.

## Next boundary (measured, not yet resolved)

Boot now advances to launchd PID 1 runtime and then a launchd-spawned child
(pid 365580 in that run) crashes after successfully attaching a ring lane:

```text
[srv-process-register #1 pid=365580 ...]
[dring-attach] RING_ATTACH_END pid=365580 ... rc=0
[sigexc-fatal sig=11 code=128 addr=0x0 pid=365580 tid=365580]
[dserver-CRASH sig=b addr=0x0 ... pc=0x75fa2b04a994 ...]
[sigexc-fatal sig=4 code=2 addr=0x7DEB9A6796D8 pid=365580 tid=365580]
Rootless shellspawn did not become ready within 60000ms
```

That crash is the next thing to diagnose; no instrumentation has been added for
it yet.

### Next-boundary measurements (plane attach)

The child that crashes does so right after a **successful** plane attach:

```text
[dring-plane-attach] ok tid=365580 rc=0 reject=0 wake=24
[sigexc-fatal sig=11 code=128 addr=0x0 pid=365580 tid=365580]
[dserver-CRASH sig=b addr=0x0 ... pc=0x75fa2b04a994 ...]
[sigexc-fatal sig=4 code=2 addr=0x7DEB9A6796D8 pid=365580 tid=365580]
```

Control run with `DARLING_GUEST_PLANE_ATTACH_OFF=1` (the guest-side hatch at
`dserver-ring.c:672`): **no `sigexc-fatal` appears at all**, but boot also stops
earlier -- the plane is refused (`[dring-plane-attach] no-page` ->
`[dring-attach-fail] plane-unanswered-no-uds`) and launchd reaches
`RUNTIME_INIT_DONE` but never `RUNTIME_ENTER`/`BOOTSTRAPPER_SCHEDULED`. That
run's tail shows the UDS fallback with `reason=ATTACH_FAILED lane_state=0`, so
the control is suggestive (the crash needs a live plane) but not conclusive:
the plane-off workload never executes the instruction that faults.

Inherited, non-usable state is visible in the same probe and is NOT the cause:
the child reports `b0_active=1 b0_owner=<parent pid> b0_borrowed=1`, i.e. it
inherited the parent's `g_borrowed[0]` record across fork; `gr_borrowed_lane_usable()`
requires `owner_tid == tid`, so the record is ignored.

Next step: instrument the plane-attach success path in the child (or capture the
guest fault with `dwdiag run --capture-gdb`), not the courier/token path.

### The child's death is a signal-delivery failure, not a ring transport failure

The launcher's captured stderr does not carry the whole story; the prefix's
`private/var/log/dserver.log` does. For the crashing child (pid 390425 in that
run) the server recorded:

```text
plane-doorbell-sent pid=390425 token=... via=envelope
attach-lane-op pid=390425 tid=390425 size=2816 status=0 reject=0 token=...
[sigprocess-no-interrupt] delivering a signal without an interrupt context tid=390425 nstid=390425
Uncaught exception from processCall (call dserver_callnum_sigprocess); replying -22
fd-courier-conn-closed fd=23 ... peerPid=390425 isLoader=1
[P:390370(1)]: timed out waiting 30 seconds for fork child checkin
```

So the ring attach and the lane it created were fine (`status=0`): the child's
fatal path is that it faulted (SIGSEGV, `addr=0x0`) while the thread had **no
interrupt context** (`thread.cpp:1134`), so the server could not deliver the
signal, `dserver_callnum_sigprocess` threw, the reply was `-22`, and the child
died. PID 1 then timed out waiting for that forked child's checkin.

Fault address resolution (same-run `/proc/<pid>/maps` union, sampled at 20 ms
for comm `mldr`/`launchd`/`vchroot`/`shellspawn`): the secondary SIGILL
`addr=0x708273EFE6D8` falls in

```text
mldr  prefix/usr/lib/system/libsystem_platform.dylib  708273ef9000-708273eff000
      -> offset 0x56d8 = _os_unfair_lock_recursive_abort + 0x4
```

i.e. the dying process reached libplatform's deliberate
"Trying to recursively lock an os_unfair_lock" trap, consistent with the signal
failing to be delivered and the process aborting rather than reporting.

Scratch harnesses used for this round live outside the product tree:
`/home/ilyagulya/work/ring-reint-state/wr_smoke_maps.py` (smoke + maps union)
and `wr_maps_watch.py` (standalone sampler). Nothing in the product tree was
changed for this measurement.

Next: find the *first* fault in the child (the SIGSEGV at 0x0 that has no
interrupt context) -- capture it with `dwdiag run --capture-gdb`, or instrument
the child's startup after `RING_ATTACH_END`. The signal-delivery refusal itself
(`sigprocess-no-interrupt`) is the second half of the same boundary.

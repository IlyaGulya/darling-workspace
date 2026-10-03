# Ring re-integration DAG (W0 -> accepted Ring product)

Status: working document for the Ring re-integration pass. It is a compact,
evidence-backed map of the surviving Ring work onto the bootable Git/West
baseline. It is **not** a patch/profile manifest and it is not source authority:
source authority is the component Git commits plus the `darling-workspace`
manifest commit whose `west.yml` pins their exact SHAs
(`docs/adr/0001-workspace-manifest-is-the-product.md`).

The previous patch/profile/lock layer is `FROZEN_LEGACY`
(`docs/2026-10-03-ring-source-and-workflow-cutover.md`). Old metadata under
`patches/` and `locks/patch-stack/` is provenance evidence only. It is
nevertheless the most complete record of the product line, and this document
uses it only to recover source commit identities, ordering and dependencies.

## W0 — bootable baseline

Workspace branch `change/ring-reintegration-v1`, created from
`change/accepted-git-pin` (`5136c25f`). Component pins:

| component | path | revision |
| --- | --- | --- |
| darling | `darling` | `4aa9b4ddcd2d4b54a54d6797061d53d62370dad0` |
| darlingserver | `darling/src/external/darlingserver` | `a693e31a3df59ffe8f01b8ced9cf423be6389afc` |
| xnu | `darling/src/external/xnu` | `97dd9e57f04340530f2a11060bc8849a61f062ca` |
| libmalloc | `darling/src/external/libmalloc` | `a57991e2651226a675654bd96e5d9ab6bec288c5` |
| dyld | `darling/src/external/dyld` | `323ba11611e074b0a412b133ece0685862e9b00e` |
| libsystem | `darling/src/external/libsystem` | `08df454b6eb0df9400aa4c39839a7efd6efd2c3c` |

Measured properties of this manifest commit:

- W0's xnu **already enables the guest-side perf#18 ring transport by default**
  (`DARLING_RING_TRANSPORT`, default ON, `xnu/.../emulation/CMakeLists.txt`), and
  W0's darlingserver compiles `src/ring.cpp` plus the A0 corrections
  (`perf: consolidate shared-memory ring transport`, `fix: consolidate A0 hang
  corrections`, the postfork ownership gate). The broad perf#18 ring transport
  (`P3`..`D18a`, duplex lane, per-thread lanes) is therefore *in* W0.
- W0's `darling` loader has **no loader-side ring plane**: `src/startup/mldr`
  contains no `DSERVER_RING_TRANSPORT` and no `signal_atomic.h`. The loader
  plane and its later attribution fixes survive only as exact file snapshots.
- Component topic tips in the surviving `fix/*` refs are built on synthetic
  `patch-stack/v1/bases/*` snapshots of the frozen `wget-residual` chain, so
  pinning a topic tip does not recreate the product. Each topic commit below was
  checked against W0's ancestry (subject set) to separate genuine additions from
  re-created copies; only the additions are listed.

### W0 build prerequisite (already required to build, not just to boot)

| component | commit | why |
| --- | --- | --- |
| libunwind | `d5fbc818` | `-fno-jump-tables` on `unwind_static`; without it the `dyld`/`system_loader` link fails with `ld: chained binds not implemented yet in l_reltable._ZN9libunwind...` (dar-rru4, dar-ofka, dar-deploy-gate-broad-build-fails-ukvp) |

`d5fbc81` is a single commit directly on top of W0's libunwind pin
(`a91da1a`), so it is a fast-forward. The re-integration branch pins it in
`west.yml`; without it no Git-only `rootless_bootstrap` build links.

## Measured W0 status (2026-10-03)

| step | result |
| --- | --- |
| Git-only configure + `ninja rootless_bootstrap` | rc=0 only after the libunwind pin; without it the dyld/system_loader link fails |
| Git-only prefix bootstrap (runtime deployment planner over `darling-rootless-bootstrap.json`, 101 file copies) + product prefix init | ok, typed prefix state published |
| ring-ON guest smoke | **fails**: darlingserver starts and reports `perf#18 shared-memory ring transport ACTIVE`, the boot reaches `shellspawn` and then `Rootless shellspawn did not become ready within 60000ms`. `DARLING_SERVER_FAST_OPS=0` does not change it. |
| `DSERVER_RING_TRANSPORT=OFF` build | **does not compile**: `darlingserver/src/metrics.cpp` uses `dserver_ring_op_class` / `DSERVER_RING_CLASS_*` unconditionally, so the perf-line server requires the ring transport compiled in |

Boot-path dependency measured while porting the loader plane: the accepted
`mldr.c` snapshot calls `__mldr_fd_courier_send_envelope(..., DSERVER_FD_COURIER_KIND_PROCESS_CONTROL)`
and reads `struct dserver_process_control`, i.e. the arch `rpc-supplement.h`
surface. W0's darlingserver `a693e31a` has **no** `DSERVER_FD_COURIER_KIND_PROCESS_CONTROL`,
so the loader plane cannot compile against W0's server; the snapshot's
`rpc-supplement.h` is byte-identical (sha256 `10d6685a47bac712...`) to
`ddc0bf7b:include/darlingserver/rpc-supplement.h`.

Consequence: the layers are not independent in this codebase. The arch
`darlingserver` surface (process-management plane + descriptor courier) is a
hard prerequisite of the loader ring plane, so the W3 server foundation has to
land with (or before) W1. W0's darlingserver is on the perf line and is not the
product's server.

The ported loader commit is kept on `darling fix/ring-plane-loader`
(`45f4ed74`); it does not compile until the arch server surface is pinned or
ported.



### Arch-line Git trees are themselves incomplete

Pinning the arch components directly does **not** yield a buildable tree:
`darlingserver ddc0bf7b`'s `CMakeLists.txt` references
`tests/exec_completion_test.cpp`, which is absent from the `ddc0bf7b` Git tree
(`git ls-tree`), so the arch tip depended on untracked materialized-forest
files. Mixing the arch `darlingserver`/`xnu` with the perf-line `darling` also
fails to configure for the same reason. The arch lineage is a source of
semantic intent, not a pinnable product state.

## Recovery input: the surviving product source tree

The accepted product source tree **survives on disk**:

- `/home/ilyagulya/work/r1-repro` — the materialized forest itself (Sep 30):
  one empty `init` commit (`8f33c0c`), 21 untracked top-level entries, the whole
  tree untracked. Its `src/startup/mldr/mldr.c` contains `DSERVER_RING_TRANSPORT`
  and `src/external/darlingserver/include/darlingserver/rpc-supplement.h`
  contains `DSERVER_FD_COURIER_KIND_PROCESS_CONTROL`, i.e. the loader ring plane
  plus the process-control descriptor courier.
- `/home/ilyagulya/work/r1-repro-build` — a build of that tree (configured
  `CMAKE_HOME_DIRECTORY=/home/ilyagulya/work/r1-repro`, Debug,
  `DARLING_RING_TRANSPORT=ON`, `DSERVER_RING_TRANSPORT=ON`, EUNION + rootless),
  with built `darlingserver`, `mldr`, `dyld` dated 2026-10-02.
- `/home/ilyagulya/work/r1-clean-base` — the earlier (Sep 28) reconstruction
  (`darling 5f2d7401` + the `r1-transport-sources` delta); it differs from
  `r1-repro` in 18 files (the later loader/guest fixes).
- `/home/ilyagulya/work/r1-clean-ref` and `/home/ilyagulya/work/r1-clean-build`
  also survive.

Because `r1-repro` is a file tree and not Git history, re-integrating it means
landing it as ordinary per-component commits: for each component (`darling`,
`darlingserver`, `xnu`, `libmalloc`, `launchd`, `dyld`, `installer`, ...) diff
`r1-repro` against the pinned base revision, commit onto that component's `fix/*`
branch, then pin the resulting SHAs in a `darling-workspace` manifest checkpoint
and verify build plus a ring-ON boot smoke.

### Secondary recovery input: the r1 transport source archive

`/home/ilyagulya/work/darling-dev/evidence/r1-transport-sources-LATEST` ->
`r1-transport-sources-20260928T112410Z.tar.gz` (438 non-directory paths) with a
matching `.sha256` manifest. It carries the Sep-28 transport delta:

| area | paths | notable files |
| --- | --- | --- |
| `src/external/darlingserver` | 128 | `src/ring.cpp`, `src/darlingserver.cpp`, `src/server.cpp`, `include/darlingserver/rpc-supplement.h`, `tests/exec_completion_test.cpp` (the file the arch Git tree is missing) |
| `src/external/xnu` | 143 | `emulation/.../dserver-ring.{c,h}`, `emulation/include/common/signal_atomic.h` |
| `src/startup/mldr` | 19 | `mldr.c`, `signal_atomic.h`, `resources/dserver-rpc-defs.h` |
| others | ~148 | `src/launchd/src`, `src/external/libmalloc`, `src/external/dyld`, `src/external/installer`, `src/external/perl`, `src/external/libressl-2.8.3`, `src/external/libkqueue`, `Developer/Platforms/MacOSX.platform` |

The archive records `base=/home/ilyagulya/work/r1-clean-ref` and an empty
`pinned` field; `scripts/r1-clean-base-reproduction.sh` is the existing tool for
the "materialize a pinned revision + apply a content delta" shape (its own
`SRC_REPO` is a different, older tree). Prefer `r1-repro` when it is available;
use the archive when only the delta is needed.

### Measured status of the recovered product tree

- `ninja -C /home/ilyagulya/work/r1-repro-build rootless_bootstrap` -> rc=0
  (1046 targets).
- A Git-only prefix deploy + product prefix init succeeds (102 file copies).
  The launcher bakes `INSTALL_PREFIX` at compile time, so the prefix must equal
  `r1-repro-build`'s `CMAKE_INSTALL_PREFIX` (`/tmp/r1-repro-prefix`); deploying
  to any other directory makes the launcher print
  `Failed to start darlingserver` (it `execl`s
  `INSTALL_PREFIX "/bin/darlingserver"`, `src/startup/darling.c:1139`).
- Boot with the correct prefix reaches the ring plane: dserver logs
  `perf#18 shared-memory ring transport ACTIVE ... fiber_dispatch=on`, and the
  product instruments run — `[mldr-seed] seeded`, `[dring-adopt] post-claim ok`,
  `[srv-lane-attach]`, `[srv-checkin]`, `[afunix-send] scm=1 fdcnt=1`.
- Boot then fails deterministically at the vchroot op:

  ```text
  [plane-refuse] n=8 why=no-page op=8 a=0 b=0 tid=... 
  [plane-exhausted] n=1 op=8 tid=... attempts=9 status=-1
  [rpc-socket-DENIED] ... call=vchroot image=kernel delta=0x28271 denied=1
  vchroot: Undefined error: 0
  [native-exit status=3 ...]
  Rootless shellspawn did not become ready within 60000ms
  ```

  i.e. the urgent/reentrant plane has no page for the vchroot op at boot, the
  bounded publish retry is exhausted (`attempts=9`), the request falls back to
  the ordinary socket path where it is denied, and `vchroot` fails, so
  shellspawn never becomes ready. This is the causal boot failure to fix; it is
  the shape of the open Beads `dar-b5pe` (transiently held plane slot),
  `dar-n8p7` (declined per-thread transport) and `dar-o1qj` (checkin/attach
  denial). Both the W0 ring-ON tree and this product tree end at the same
  shellspawn-readiness symptom.

## The product line is the arch (`perf#30`) lineage

The last accepted Ring runtime's deployed artifacts
(`mldr=52b03b0ac229 libsystem_kernel.dylib=ab8a45074848 dyld=be43e3e77534
darlingserver=339583b74931`) were produced by the **`perf#30` process-control
plane / descriptor-courier** work, i.e. the `patches/arch` lineage, not by the
perf#18 ring-comparison topics. Evidence:

- the diagnostics bundle's `rpc-supplement.h` pre-edit sha256 (`8ff5faaf...`)
  equals the `r1-repro` materialized-forest file, and its shipped content maps to
  `patches/arch/darlingserver/plane-courier-port.patch`, whose source commit is
  `ddc0bf7b` (`fix/plane-courier-port`, bead `dar-ssr1`);
- the diagnostic `srv-process-register`/`srv-checkin`/`srv-lane-attach`/
  `srv-kthread-create` instruments exist only in that lineage;
- the accepted `mldr.c`/`threads.c` snapshots carry a process-control client,
  a bounded thread-self-bootstrap retry, the atomic reply-state protocol and
  published-vs-seen reply attribution, and **no** committed patch or branch in
  this workspace carries that loader delta.

Consequence: the re-integration target is `W0 + arch lineage + ring-comparison
correctness fixes`, and the loader plane has to be re-landed from the
diagnostics snapshot as an ordinary committed change.

### Arch chain (source commits, in order; recovered from `patches/arch/patches.yml`)

| component | commit | subject |
| --- | --- | --- |
| libunwind | `d5fbc818` | build: avoid jump tables in static libunwind for dyld |
| xnu | `8d746cb7` | xnu: stop ring retries after publish |
| xnu | `aadfa984` | a1: classify RPC send disconnects for interruptible waits |
| xnu | `8becacfa` | tests: cover RPC disconnect status contract |
| darlingserver | `5288c5cd` | A0-ARCH stage 3: perf A/B (the skipped check) -- NO REGRESSION |
| darlingserver | `51add588` | darlingserver: coalesce selected standard signals |
| darlingserver | `8e72db00` | a0: retry transient shellspawn startup miss |
| darlingserver | `03038154` | tests: pin ring committed-unknown no-retry contract |
| darlingserver | `54640a38` | duct-tape: fail closed for reachable stubs |
| darlingserver | `f41af247` | rpc: tag UDS replies with packed call serial |
| darlingserver | `f1419bc3` | a0: capture client RPC log per synth leg |
| darlingserver | `24546ba9` | a1: map interruptible RPC disconnect send to EINTR |
| darlingserver | `a60da290` | dserver: guard stack pool against empty stack handles |
| darlingserver | `1b806454` | a0: add focused synth filter and exit markers |
| darlingserver | `93392fef` | tests: cover recent signal and RPC contracts |
| darlingserver | `a0877988` | message: reject truncated control data |
| darlingserver | `b545cdbe` | darlingserver: coalesce pending SIGUSR1 |
| darlingserver | `305781e7` | timer: stop starving the timerfd |
| darlingserver | `ddc0bf7b` | darlingserver: land the process-management plane and descriptor courier (product subset) |
| darling | `30835a35` | test: mark host regression runner executable |
| darling | `78cf1a0e` | shellspawn: preserve signal exit status |

The arch chain is on a **different genealogy** from W0's components: e.g.
darlingserver `ddc0bf7b` and W0's `a693e31a` diverge at `14a9d364` (57 commits
one way), xnu `8becacfa` and W0's `97dd9e57` diverge at `5f26a4c2`, darling
`78cf1a0e` and W0's `4aa9b4dd` diverge at `5f2d7401`. W0's darlingserver is a
*consolidation* of the A0-ARCH work (`fix: consolidate A0 hang corrections`), so
the arch chain and W0 overlap semantically and must be ported, not pinned
wholesale.

## Ring-comparison correctness fixes absent from W0

| component | commit | subject | layer |
| --- | --- | --- | --- |
| darling | `69bb6aed4e` | Own ring wake descriptors in the shared loader across fork | foundation |
| darling | `a74bec2e33` | Defer guest signals while holding the shared FD registry | foundation |
| darling | `9ef131b2de` | Build both loaders after generated signal definitions | build |
| xnu | `bc21f8ca` | xnu: stop ring retries after publish | ordinary path |
| xnu | `877025a2` | Preserve nonreplay completion semantics in corrected ring guest composition | foundation |
| xnu | `42e78ffd` | Generate paired RPC headers for completion regression | test |
| xnu | `756a62fb` | Expose a semantic failure marker for published RPC replay | test |
| xnu | `19c387f5` | Resolve paired profile headers in isolated completion proofs | test |
| xnu | `044dd407` | Respect generated RPC tail padding in ring completions | ordinary path |
| xnu | `0360502b` | Stop publishing to lanes retired by another guest image | ordinary path |
| xnu | `e0c157b0` | Transfer ring FD lifetime to the shared loader owner | foundation |
| xnu | `aaa1c29a` | Document loader-owned fork descriptor retirement | foundation |
| xnu | `5c01c2f6` | Defer guest signals across descriptor guard table locks | foundation |
| darlingserver | `3dc3a97d` | Generate paired RPC headers instead of skipping host ring gates | foundation |
| darlingserver | `800fd28f` | Keep physical execution exit out of logical XNU wait state | process control |
| darlingserver | `546d0a07` | Retire superseded thread rings before replacing their ownership | ordinary path |
| darlingserver | `0a6cfd07` | Canonicalize stats socket identity across rootless prefix capabilities | process control |
| darlingserver | `b59b1cfd` | Keep ring port traps on fibers across contended IPC locks | ordinary path |
| darlingserver | `6bedae81` | Retire obsolete guest-local FD teardown model | ordinary path |
| libmalloc | `5981f906` | Retain immutable region hash generations for concurrent readers | later fix |
| libmalloc | `15f5405f` | Build retained rack generation regression as native Darling executable | later fix |

Cross-component pairs: loader FD ownership (`darling 69bb6aed` <-> `xnu
e0c157b0`/`aaa1c29a`), signal deferral (`darling a74bec2e` <-> `xnu 5c01c2f6`,
both adding `signal_atomic.h`), lane retirement (`darlingserver 546d0a07` <->
`xnu 0360502b`), paired generated RPC headers (`darlingserver 3dc3a97d` <->
`xnu 42e78ffd`/`19c387f5`).

## Loader plane + attribution fixes (snapshot only)

Surviving only as exact file snapshots + patches:
`diagnostics/ring-loader-fixes-20260930/` (the MANIFEST carries sha256 values
and the pre-edit hash each patch applies to). These are the perf#30/plane
lineage plus an uncommitted `r1-repro` loader delta:

| file | carries |
| --- | --- |
| `src/startup/mldr/mldr.c` | loader ring plane (`DSERVER_RING_TRANSPORT`), deferred/second checkin ordering, atomic process-slot protocol, `[checkout-reply-unseen]`/`[checkin-reply-unseen]` reply attribution, bounded retry of the main-thread-port read |
| `src/startup/mldr/elfcalls/threads.c` | loader-side thread/wake plumbing for the plane |
| `src/external/darlingserver/include/darlingserver/rpc-supplement.h` | psynch family classified on the ring (`SIMPLE_C2S` / `SIMPLE_C2S\|BLOCKING`) |
| `src/external/darlingserver/src/call.cpp` | server-side request/reply attribution |
| `src/external/darlingserver/src/server.cpp` | server-side recordings used as instruments (diagnostic, not a functional fix) |

The `integration/accepted-transport-content` bridges (`darling 705c5630`,
`darlingserver 32aca765`) carry the four loader/server snapshots + the elfcalls
hooks; `darling 705c5630`'s `mldr.c` and `threads.c` are byte-identical to the
snapshot files (sha256 verified). The bridge does **not** add
`src/startup/mldr/signal_atomic.h`, which the snapshot `mldr.c` includes, so
that file still has to be ported from `darling a74bec2e33`.

## Layer plan (conceptual; boundaries derived from the evidence)

```text
W0  bootable baseline + libunwind build fix (this manifest commit)
W1  foundation: paired RPC headers + host ring gates, loader ring plane with
    wake/FD ownership, the two signal-deferral halves, ring-completion oracle
W2  ordinary/blocking ring path: publish/retire/tail-padding, superseded-ring
    retirement, fiber-trapped port ops, obsolete teardown removal
W3  process control/bootstrap: the arch process-management plane + descriptor
    courier + urgent slots + descriptor adoption, execution-exit ownership,
    stats-socket identity
W4  later measured fixes: libmalloc generation ownership, psynch classification,
    reply attribution and bounded retries from the snapshot
W5  final accepted Ring product checkpoint
```

Each layer gets a component commit set and a manifest checkpoint commit on
`change/ring-reintegration-v1`; the manifest commit is the tested revision.

## Evidence anchors

- diagnostics snapshot bundle: `diagnostics/ring-loader-fixes-20260930/`
- arch packaging (source-commit identities only):
  `patches/arch/patches.yml`, `locks/patch-stack/`
- migration experiments (input, not product):
  `darling integration/accepted-transport-bridge` (`ae69ff63`),
  `darling integration/accepted-transport-content` (`705c5630`),
  `darlingserver integration/accepted-transport-content` (`32aca765`),
  workspace branches `change/current-accepted-transport` (`1ed32f34`),
  `change/ring-line` (`66b36bb1`), `change/ring-line-accepted` (`22b8c497`).
- surviving topic tips: `fix/ring-fd-ownership` (`9ef131b2`),
  `fix/ring-comparison-server-refreshed` (`6bedae81`),
  `fix/ring-comparison-guest` (`5c01c2f6`),
  `fix/malloc-region-generation` (`15f5405f`),
  `fix/plane-courier-port` (`ddc0bf7b`).
- owning Beads: `dar-1il` (perf#18), `dar-ssr1` (perf#30 plane/courier),
  `dar-gwn.7.7` and children, `dar-rpc-correctness-z27x`,
  `dar-dar6x4-perf-5dq.30`, `dar-n8p7` (psynch classification),
  `dar-rru4`/`dar-ofka` (libunwind link).

# Ring re-integration DAG (W0 -> accepted Ring product)

Status: working document for the Ring re-integration pass. It is a compact,
evidence-backed map of the surviving Ring work onto the bootable Git/West
baseline. It is **not** a patch/profile manifest and it is not source authority:
source authority is the component Git commits plus the `darling-workspace`
manifest commit whose `west.yml` pins their exact SHAs
(`docs/adr/0001-workspace-manifest-is-the-product.md`).

The previous patch/profile/lock layer is `FROZEN_LEGACY`
(`docs/2026-10-03-ring-source-and-workflow-cutover.md`). Old metadata under
`patches/` and `locks/patch-stack/` is provenance evidence only.

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

Notes measured on this manifest commit:

- W0's xnu **already enables the guest-side ring transport by default**
  (`DARLING_RING_TRANSPORT` default ON,
  `xnu/darling/src/libsystem_kernel/emulation/CMakeLists.txt`), and W0's
  darlingserver already compiles `src/ring.cpp` and the A0 corrections. The
  broad perf#18 ring transport (`P3`..`D18a`) and the server consolidation are
  therefore *in* W0.
- W0's `darling` loader has **no loader-side ring plane**: `src/startup/mldr`
  contains no `DSERVER_RING_TRANSPORT`. The loader plane and the later
  attribution fixes survive only as the diagnostics snapshot and the migration
  bridges (see "Evidence anchors").
- Component topic tips are built on synthetic `patch-stack/v1/bases/*`
  snapshots of the frozen `wget-residual` chain, so pinning a topic tip does
  not recreate the product. Each topic commit below was checked against W0's
  ancestry (`git log --format=%s` subject set) to separate genuine additions
  from re-created copies; only the additions are listed.

## Missing work, grouped by function

### A. Ring/RPC foundation (shared structures, ABI, compile-time gates)

| component | commit | subject | files |
| --- | --- | --- | --- |
| darlingserver | `3dc3a97d` | Generate paired RPC headers instead of skipping host ring gates | `tests/run-ring-shm-validate.sh` |
| xnu | `877025a2` | Preserve nonreplay completion semantics in corrected ring guest composition | emulation `CMakeLists.txt`, `dserver-ring.h/.c`, tests |
| xnu | `42e78ffd` | Generate paired RPC headers for completion regression | `darling/tests/ring-completion/run.py` |
| xnu | `756a62fb` | Expose a semantic failure marker for published RPC replay | `darling/tests/ring-completion/fixture.c` |
| xnu | `19c387f5` | Resolve paired profile headers in isolated completion proofs | `darling/tests/ring-completion/run.py` |
| darling | `69bb6aed4e` | Own ring wake descriptors in the shared loader across fork | `mldr.c`, `elfcalls/elfcalls.c/.h` |
| darling | `a74bec2e33` | Defer guest signals while holding the shared FD registry | `mldr.c`, `mldr/signal_atomic.h` (new), `dserver-rpc-defs.h` |
| darling | `9ef131b2de` | Build both loaders after generated signal definitions | `mldr/CMakeLists.txt` |
| xnu | `5c01c2f6` | Defer guest signals across descriptor guard table locks | `common/signal_atomic.h` (new), `dserver-rpc-defs.h`, `dserver-ring` |
| xnu | `e0c157b0` | Transfer ring FD lifetime to the shared loader owner | `elfcalls_wrapper.h`, `guarded/table.c`, ring |
| xnu | `aaa1c29a` | Document loader-owned fork descriptor retirement | `dserver-ring.h/.c` |

`a74bec2e33` (darling loader) and `5c01c2f6` (xnu guest) are the two halves of
one invariant: guest signals are deferred while a shared FD-registry / guard
table lock is held.

### B. Ordinary/blocking ring path

| component | commit | subject |
| --- | --- | --- |
| xnu | `bc21f8ca` | xnu: stop ring retries after publish |
| xnu | `044dd407` | Respect generated RPC tail padding in ring completions |
| xnu | `0360502b` | Stop publishing to lanes retired by another guest image |
| darlingserver | `546d0a07` | Retire superseded thread rings before replacing their ownership |
| darlingserver | `b59b1cfd` | Keep ring port traps on fibers across contended IPC locks |
| darlingserver | `6bedae81` | Retire obsolete guest-local FD teardown model |

### C. Process control / bootstrap ownership

| component | commit | subject |
| --- | --- | --- |
| darlingserver | `800fd28f` | Keep physical execution exit out of logical XNU wait state |
| darlingserver | `0a6cfd07` | Canonicalize stats socket identity across rootless prefix capabilities |

`800fd28f` is the scheduler/execution-ownership correction recorded as
`dar-gwn.7.7.3` (physical-release projection removed; the canonical refresh on
`fix/ring-comparison-server` carries the same change as `4b12bfd5`).

### D. Later measured correctness fixes

| component | commit | subject |
| --- | --- | --- |
| libmalloc | `5981f906` | Retain immutable region hash generations for concurrent readers |
| libmalloc | `15f5405f` | Build retained rack generation regression as native Darling executable |

libmalloc is `dar-gwn.7.7.4`; the allocator correction is already accepted on
its own line and is independent of the ring transport.

### E. Loader plane + attribution fixes (snapshot only)

Surviving only as exact file snapshots + patches:
`diagnostics/ring-loader-fixes-20260930/` (MANIFEST carries sha256 values).

| file | what it carries |
| --- | --- |
| `src/startup/mldr/mldr.c` | the loader ring plane (`DSERVER_RING_TRANSPORT`), the deferred/second checkin ordering, atomic process-slot protocol, `[checkout-reply-unseen]` / `[checkin-reply-unseen]` reply attribution, bounded retry of the main-thread-port read |
| `src/startup/mldr/elfcalls/threads.c` | loader-side thread/wake plumbing for the plane |
| `src/external/darlingserver/include/darlingserver/rpc-supplement.h` | psynch family classified on the ring (`SIMPLE_C2S` / `SIMPLE_C2S\|BLOCKING`) |
| `src/external/darlingserver/src/call.cpp` | server-side request/reply attribution |
| `src/external/darlingserver/src/server.cpp` | server-side recordings used as instruments (diagnostic, not a functional fix) |

Deployed artifacts of the passing run (RUN-ENV of the passing runs):
`mldr=52b03b0ac229 libsystem_kernel.dylib=ab8a45074848 dyld=be43e3e77534 darlingserver=339583b74931`.

## Layer plan (conceptual; boundaries derived from the evidence above)

```text
W0  bootable baseline (this manifest commit)
W1  foundation: paired RPC headers, loader ring plane + wake/FD ownership,
    shared signal deferral halves, ring-completion oracle scaffolding
W2  ordinary/blocking ring path correctness: publish/retire/tail-padding,
    superseded-ring retirement, fiber-trapped port ops, obsolete teardown removal
W3  process control / bootstrap: execution-exit ownership, stats-socket identity
W4  later measured fixes: libmalloc generation ownership, psnr classification,
    reply attribution and bounded retries from the snapshot
W5  final accepted Ring product checkpoint
```

Each layer gets a component commit set and a manifest checkpoint commit on
`change/ring-reintegration-v1`; the manifest commit is the tested revision.

## Evidence anchors

- diagnostics snapshot bundle: `diagnostics/ring-loader-fixes-20260930/`
- migration experiments (input, not product):
  `integration/accepted-transport-bridge` (`ae69ff63`),
  `integration/accepted-transport-content` (`705c5630`),
  darlingserver `integration/accepted-transport-content` (`32aca765`),
  and workspace branches `change/current-accepted-transport` (`1ed32f34`),
  `change/ring-line` (`66b36bb1`), `change/ring-line-accepted` (`22b8c497`).
- surviving topic tips: `fix/ring-fd-ownership` (`9ef131b2`),
  `fix/ring-comparison-server-refreshed` (`6bedae81`),
  `fix/ring-comparison-guest` (`5c01c2f6`),
  `fix/malloc-region-generation` (`15f5405f`).
- owning Beads: `dar-1il` (perf#18), `dar-gwn.7.7` and children,
  `dar-rpc-correctness-z27x`, `dar-ssr1`, `dar-dar6x4-perf-5dq.30`,
  `dar-n8p7` (psynch classification).

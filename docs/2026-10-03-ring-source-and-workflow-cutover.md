# Handoff: West/manifest workflow cutover and the Ring source-identity outcome (2026-10-03)

Read this before touching Ring work. It records what is decided, what is proven, what is
lost, and exactly which refs carry the surviving material. Nothing here is a product
verdict about Ring.

## 1. Workflow decision

```text
WEST_MANIFEST_WORKFLOW = ACTIVE
PATCH_WORKFLOW         = FROZEN_LEGACY
```

Evidence that is already sufficient and must not be redone:

```text
fresh GitHub workspace (west init -m https://github.com/IlyaGulya/darling-workspace)
exact manifest pins, zero project drift
Git-only configure + build (build OK) with no patch/profile/materializer step
bootable non-Ring baseline (bootstrap passed, login shell)
```

The patch/profile/lock/materialization layer is frozen and archived at tag
`archive/patch-stack-final`. Do not repair its lineage defects. Do not delete it yet;
`west_commands/`, tests and metadata still reference it.

Authority model (ADR `docs/adr/0001-workspace-manifest-is-the-product.md`):

```text
source      component Git commits + a darling-workspace commit whose west.yml pins exact SHAs
workspace   darling-workspace main; its commit is the product revision
deployed    receipt: workspace commit + component SHAs + artifact sha256
behavioral  CTest/testkit + dwdiag verdicts
planning    Beads
```

`west manifest --freeze` output is derived evidence, never a second source of truth.

## 2. Ring source claim — corrected

Do **not** state that all functional Ring source was preserved; that is unproven.

```text
The exact materialized source state of the last accepted Ring runtime is no longer
available as one reconstructable Git product state.
```

What exists instead:

- topic branch histories (see section 4);
- old patch/profile metadata under `patches/` and `locks/patch-stack/` (frozen);
- exact per-file snapshots for selected later-stage files
  (`diagnostics/ring-loader-fixes-20260930/`, with a MANIFEST of sha256 values);
- artifact hashes of the passing runs, recorded in that MANIFEST:
  `mldr=52b03b0ac229 libsystem_kernel.dylib=ab8a45074848 dyld=be43e3e77534 darlingserver=339583b74931`;
- runtime acceptance/cleanup evidence under `/home/ilyagulya/work/darling-debug/`,
  and the Beads/architecture ledger.

The bridges created in this session are **migration experiments**, not the accepted Ring
product. Do not pin them as the product and do not debug them as if they were.

## 3. Migration experiments (exact refs)

Workspace branches in `darling-workspace`:

```text
change/accepted-git-pin          5136c25f   nine accepted components, exact SHAs; darlingserver
                                            resolved to the gitlink the product commit references
change/ring-line                  66b36bb1   ring-comparison topic tips
change/ring-line-accepted         22b8c497   states derived from composition final_tree records
change/current-accepted-transport 1ed32f34   the live working checkouts' identity (bridge ae69ff63)
change/accepted-transport-content 9ae2f2f1   content bridge v2 pins + fork remotes
fix/bootstrap-evidence-cache-worktrees db0640d0  the bootstrap fix used by the fresh workspace
main                              ff0cd614   published coordination line (frozen patch layer intact)
```

Component bridges:

```text
darling          integration/accepted-transport-bridge                  ae69ff63  (live-worktree gitlinks)
darling          integration/accepted-transport-content                 705c5630  (v2: snapshots + elfcalls)
darling          published ref integration/accepted-transport-content-v2-2026-10-03
darlingserver    integration/accepted-transport-content                 32aca765
darlingserver    published ref integration/accepted-transport-content-2026-10-03
```

Bridge content:

```text
darling        src/startup/mldr/mldr.c              accepted-stage snapshot (sha256 7bc73821…)
               src/startup/mldr/elfcalls/threads.c  accepted-stage snapshot (sha256 7fa20742…)
               src/startup/mldr/elfcalls/elfcalls.c hooks taken from 69bb6aed4e
               src/startup/mldr/elfcalls/elfcalls.h hooks taken from 69bb6aed4e
darlingserver  include/darlingserver/rpc-supplement.h accepted-stage snapshot (sha256 10d6685a…)
               src/call.cpp                            accepted-stage snapshot (sha256 fd3b3a9b…)
```

Measured behaviour of the bridges:

```text
DARLING_RING_TRANSPORT=ON
    the bridge now COMPILES (build OK)
bootstrap
    guest smoke times out (180 s) — no boot
why this is NOT a product verdict
    the Ring content was placed on a non-Ring/perf base (darling 4aa9b4dd,
    darlingserver a693e31a). Ring requires its own line; a four-file overlay on a
    pre-Ring base is not the product.
```

`dserver_rpc_vchroot` arity mismatch (xnu 97dd9e57 wants the two-argument form, live
darlingserver 6ad1bb27 generates the one-argument form) is **not** a bridge defect: the
RPC shape is defined by `darlingserver/include/darlingserver/rpc-supplement.h`, and the
accepted content of that file is exactly what the bridge carries.

The missing stage change `src/external/darlingserver/src/server.cpp` was inspected: it is
25 lines of **diagnostic instrumentation** ("CREATOR RECORD, SERVER SIDE": a bounded
raw-write counter of kernel-thread creations). It is not a functional fix and cannot
cause a boot failure. It is reproducible from its patch if ever wanted.

## 4. Surviving Ring topic tips (migration input, not a product)

```text
darling        fix/ring-fd-ownership                  9ef131b2de45
darling        69bb6aed4e  "Own ring wake descriptors in the shared loader across fork"
               (adds the elfcalls hooks the accepted mldr snapshot calls)
darling        a74bec2e33  "Defer guest signals while holding the shared FD registry"
darling        9ef131b2de  "Build both loaders after generated signal definitions"
darlingserver  fix/ring-comparison-server-refreshed   6bedae813d26   (9 commits, base bc8e688a3b)
xnu            fix/ring-comparison-guest              5c01c2f64445   (28 commits, base 9736367393)
libmalloc      fix/malloc-region-generation           15f5405f735a   (2 commits, base a57991e265)
```

These bases are `wget-residual` chain states (`bc8e688a` is literally
"profile base: darlingserver state produced by the wget-residual prerequisite profile"),
so simply pinning these tips does not recreate the accepted product. They are input for
a deliberate re-integration.

Published for durability in `darling-next` under `integration/accepted-2026-10-02`,
`integration/ring-line-2026-10-02`, `integration/ring-line-accepted-2026-10-02`,
`integration/product-2026-10-02`, `integration/accepted-transport-2026-10-03`.

## 5. Architectural contract to re-derive

```text
ordinary/thread RPC        per-thread logical SPSC Ring
blocking thread RPC        Ring + fiber suspend/resume + futex
caller-S2C                 duplex shared-memory lane/mailbox
bootstrap/lifecycle        process shared management plane
urgent/reentrant           preallocated shared urgent slots
real fd transfer           process SCM_RIGHTS courier
wake                       one process eventfd doorbell
ordinary AF_UNIX RPC       0
per-thread AF_UNIX endpoint nonexistent
hidden transport FD slope/thread 0
```

Later accepted correctness fixes that must be accounted for during re-integration
(derive the definitive list from Beads/history, not from this list alone):

```text
process-control generation/ownership
missing descriptor token failure
bounded descriptor-transfer retries
loader retry semantics
execve no-datagram-fallback
pthread_canceled semantics
dtape timer ordering
other measured post-Ring fixes
```

## 6. Acceptance oracles

Do not rerun these here; the next session uses them. Canonical sources are
`docs/tooling.md`, `docs/test-infra.md`, `AGENTS.md` and the owning Beads:

```text
focused host/RPC/Ring contracts   west test --env host (testkit/CTest) + tests/ host contracts
9-mode acceptance                 dbus-runner/CTest selection as recorded in the owning Bead;
                                  judged by the workload's own machine-readable line
ROW-RETRY / created / denied      the same run's counters, read only after its completion marker
boot stability                    >= 200 consecutive clean boots
FD slope                          hidden transport FD slope per thread = 0
```

A verdict is PASS only from the workload's own `... pass=1` line; a missing line is
FAIL/HANG.

## 7. Tool status

Keep and use:

```text
dwdiag provenance / freeze-on-fail / processes / verdict / suite
darling-debug (thread, transport, identity-map)
crash self-identification (dserver_crash_probe)
slot state-machine host contract
```

Pending narrow tooling issue (no new subsystem required):

```text
the post-deploy doctor still compares against the historical deploy-baseline.md5 and
rolls back intentional development deployments; today the daily loop needs
--skip-post-doctor
recommended new invariant: built artifact hash == deployed artifact hash for the current
workspace/build receipt, with the historical comparison kept as an explicit regression mode
```

## 8. NOFILE state (recorded, not worked on)

```text
hidden transport FD slope/thread = 0
1/8/16/32 thread matrix: flat transport slope
server-side NOFILE raise: real, still needs removal
strict low guest limits expose a baseline bootstrap FD floor
```

NOFILE resumes only after Ring is re-integrated and accepted.

## 9. Workspace and disk hygiene

- The disposable trees created this session under `/tmp` (`west-cutover-20261002`,
  `wc-cont2`, `wc-prefix*`, `wc-preserved`) are **gone** — the environment's tmp cleaner
  removed them; verification at handoff time shows 38 GB free and no `/tmp/wc-*` left.
  The `wc-preserved` diffs (small, from already-lost materialized forests) are therefore
  not available.
- Durable copies of everything that matters are the git refs in section 3/4 and the
  snapshots under `darling-workspace/diagnostics/ring-loader-fixes-20260930/`.
- The retired build cache still holds ~40 GB under
  `darling-workspace/.west-test/runtime-build-cache` (its `source/` forests are gone; what
  remains are `build/` trees). Reclaim only through the framework or
  `git worktree remove`, never `rm -rf` on the store.
- `darling-dev` was repaired (158 dead `core.worktree` pointers, one dead `.git` pointer,
  12 prunable worktrees, detached HEAD attached). It is legacy/non-authoritative; its
  remaining modified submodules were deliberately left alone.

## 10. Next canonical session

Do not continue historical Ring archaeology. Perform a **controlled Ring
re-integration on the bootable West baseline**:

```text
base      change/accepted-git-pin (or a successor) — builds and bootstraps
input     the surviving topic histories (§4), the verified file snapshots (§3),
          the frozen patch/profile metadata for provenance only
method    add the Ring changes in bounded pieces, each validated by build + boot + the
          9-mode gate; record every acceptance receipt in the owning Bead
refs      keep each step a normal fork-side ref; never force-push, never upstream
```

Environment prerequisites: `swift` needs credentials for `https://git-lfs.darlinghq.org`
(not needed by the core closure build); the fresh full update costs ~34 minutes and 5.8 GB.

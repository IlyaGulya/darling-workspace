# Product-delta landing matrix (2026-10-01)

Purpose: convert the transport-phase measurements into durable canonical state. The scratch tree is
evidence only; it is not a base. Canonical base = the authoritative `arch` profile stack
(`a0-arch-redesign.patch` and its source/lock/receipt/composition machinery).

## Measured status of the deltas

Marker counts are literal string counts in the file named for each tree. "Canonical" columns are
`darling-gwn-resume/darling` (branch `fix/recovered-gwn-runtime`) and `darling-dev/darling`
(integrated perf profile). Scratch root is `/home/ilyagulya/work/r1-repro/src`.

| file | logical change | Bead / evidence | scratch | canonical branch | canonical integrated | needed? | depends on |
|---|---|---|---|---|---|---|---|
| `external/darlingserver/src/server.cpp` | management-plane bounded retry (`__dserver_plane_request_ex`) | dar-4cp9 (transport close record) | present | absent | absent | yes (accepted architecture) | - |
| `external/darlingserver/src/server.cpp` | descriptor-token missing becomes an error (`plane-token-missing`) | dar-4cp9 | 3 | 0 | 0 | yes | - |
| `external/darlingserver/src/server.cpp` | timer expired-deadline guard (`currentStillPending` rule) | dar-4cp9 / dar-7tq3 | 3 | 0 | 0 | yes | - |
| `external/darlingserver/src/server.cpp` | timer later-override clamp | dar-7tq3, hunk sha256 45d51dc09f78 | 1 | 0 | 0 | yes | expired-deadline guard |
| `external/xnu/.../resources/*kqchan*.c` | guest kqchan bounded transfer retry | dar-4cp9 | file name not yet resolved | - | - | yes | - |
| `startup/mldr/mldr.c` | loader retry of its own `-2` | dar-4cp9 | marker not found by the probe used | present (1) | present (1) | check before porting | - |
| `launchd/src/launchd.c` | four diagnostic probes in the previously uninstrumented boot window | dar-6h74 evidence | 4 | 0 | 0 | NO (diagnostic only) | - |

## What the matrix already shows

* Every transport-phase semantic delta that this session relied on is present in the scratch tree and
  absent from both canonical trees. The `dar-gwn.7.7.5` closure therefore rests on unlanded scratch
  source; that is the process gap this document exists to close, and it is why a fresh canonical
  materialization plus a full acceptance re-run is mandatory before any further discovery work.
* The four launchd probes are diagnostic-only and must not be carried into a product patch.
* The loader `-2` retry marker was found in both canonical trees, so its status needs a targeted
  comparison rather than a keyword count before it is classified.

## Preservation

Scratch product inventory with hashes: `/home/ilyagulya/work/dbg-srv-build/scratch-freeze-1808/`
(`product-deltas.sha256`, `deployed-vs-built.sha256`, `timer-arm-clamp-hunk.cpp`, `build-tree.txt`).
Deployed server and built server agree: sha256 `ca2dd3f7a184010e2be4a98aad4fbd8812f420d126876bd60a23b2744d88e80d`.
No product edits in the scratch tree until canonicalization completes.

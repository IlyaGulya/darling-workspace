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

# Perf work triage — 2026-07-01

Full audit of the current perf line vs the `homebrew` west patch profile. Question that
prompted it: *is our perf work correctly captured in the homebrew patchset?* Answer: **no** —
none of the current perf#18/#21b/#24c* line is in the profile; it lives on branches and (worst)
in uncommitted worktrees. This triages every piece into **keep / experiment / recon-only** and
names its correct destination.

Legend for "destination":
- **fix/\*** — clean per-repo branch = canonical editable source (workspace rule).
- **profile** — exported via `scripts/export_patches.py` into a west patch profile, verified by
  `west patch verify`.
- **branch-only** — fine to leave on a topic branch; not shippable as-is.
- **artifact** — docs/measurements; belong in-tree under `tests/`/`docs/`, never in a patch profile.

## A. darlingserver (branch `perf/shmem-ring-abi-validator`, 40+ commits ahead of manifest)

Two very different bodies of work are mixed on this one branch:

1. **perf#18 shared-memory ring transport** — ~40 SOURCE commits (P2 ABI/validator → P3 datapath
   → P5/P6/P7/P8 op migrations → duplex lane → per-thread lanes → default-ON). This is a large,
   coherent, *shipped* feature: the deployed `darlingserver 835946f9` was built with it (the ring
   NOTICE in every dserver.log).
   - **KEEP.** Destination: its own clean `fix/perf-ring-transport` (or a dedicated `perf` profile),
     NOT mixed into `homebrew`. It's big enough to be its own profile/PR track.
   - Risk if left as-is: only on this topic branch; not in any profile; a profile regenerate won't
     touch it (it's not `integration/*`), but it's invisible to `west patch`/handoff-by-patch.

2. **perf#20a–#24c* recon + tooling** — census docs (`tests/PERF*.md`), bpftrace scripts, and the
   `tools/closure-cache/*` DCC2 builder/applier + `dyld-reader-wip/`. These are measurements and
   standalone offline tools, not darlingserver runtime changes.
   - **ARTIFACT** (keep in-tree under `tests/`/`tools/`), EXCEPT the DCC2 builder/applier which is
     real reusable tooling → keep, but it's a *tool*, not a patch to darlingserver source.
   - `tools/closure-cache/` currently has **uncommitted** edits (dcc2-builder guest-path fix +
     dyld-reader-wip refresh) → commit them onto the branch so handoff carries them.

## B. xnu (branch `perf/shmem-ring-guest` `caddc2b7`, 21 commits ahead)

perf#18 **guest-side** ring transport (`dserver-ring.c` +2424 lines in libsystem_kernel/emulation,
per-thread lanes, GR_MAX_LANES). The guest counterpart to A.1.
- **KEEP but EXPERIMENT-gated.** Critical caveat: this branch does **NOT boot** (#90 — a fresh
  closure built from it wedges launchd in early libsystem init). The bootable baseline is the
  manifest/recorded xnu (`a0328833`).
- Destination: keep on `perf/shmem-ring-guest`; do NOT fold into `homebrew` (would break boot).
  If the ring-guest work is to ship, it needs its own profile AND the boot regression fixed first.

## C. superproject darling (branch `integration/homebrew`, 1 commit ahead of base)

`e82a69150` **perf#21b: compact protected internal fd band (mldr)**.
- **KEEP — this is a real, validated win** (dup_fd 13.3% → 0.09%; mldr `f0cd2a82` is deployed and
  in the doctor baseline).
- **HIGH RISK today:** it is committed **only** onto `integration/homebrew`, which is a GENERATED
  branch (`west patch apply` rebuilds it). A regenerate would **discard perf#21b**.
- Destination: **RESCUE into a clean `fix/mldr-compact-fd-band` + export to the homebrew profile**
  as `darling/mldr-compact-fd-band.patch`. Do this before any `west patch clean/apply`.

## D. dyld (uncommitted worktree on `fix/homebrew-psynch-ruby-hang`)

perf#24c2e DCC2 dyld2-classic reader: `M CMakeLists.txt, dyld3/Loading.{cpp,h}, src/dyld2.cpp,
src/ImageLoaderMachOCompressed.cpp` + `?? dyld3/DCC2Reader.{h,cpp}`.
- **KEEP but WIP** (the 2-dylib live smoke still crashes in `instantiateFromCache` — scattered
  LINKEDIT; task #91 open).
- **HIGHEST RISK:** uncommitted. `west dw handoff` cannot carry dirty worktrees; any
  `git checkout`/`west update` on dyld would lose it. A copy exists in
  `tools/closure-cache/dyld-reader-wip/` (also uncommitted).
- Destination: **commit onto a `fix/dyld-dcc2-reader` branch** now (even as WIP), so it's durable;
  export to a profile only once the smoke is green.

## E. dyld/libunwind/objc4/zlib `WIP: preserve Homebrew debugging changes` commits

These `WIP` commits are **pre-existing homebrew debugging state, NOT our perf work.** Leave as-is
(they're the declared drift baseline). Not part of this triage's keep/throw decisions.

## F. libplatform / libpthread

At their manifest pointers, no drift beyond the profile. Nothing of ours here.

---

## Priority actions (in order; each needs a clean `fix/*` — none published upstream w/o approval)

1. **Rescue perf#21b** off `integration/homebrew` → `fix/mldr-compact-fd-band` → export into
   `homebrew` profile. (Highest value + at-risk-of-silent-loss.)
2. **Commit perf#24c2e** dyld DCC2 edits → `fix/dyld-dcc2-reader` (WIP ok). Removes the
   uncommitted-loss risk; do not export until smoke green.
3. **Commit the uncommitted `tools/closure-cache/` edits** onto the darlingserver branch.
4. Decide profile strategy for the **ring transport** (A.1 + B): own `perf` profile, kept separate
   from `homebrew`; the guest side (B) stays experiment-gated until the boot regression is fixed.
5. Recon docs/measurements: leave as in-tree artifacts; not profile material.

Nothing here is published upstream. Fork/upstream PRs remain opt-in, one bead, explicit approval.

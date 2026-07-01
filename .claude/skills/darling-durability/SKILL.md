---
name: darling-durability
description: >-
  Make in-progress Darling work durable so it survives checkout/reset/handoff:
  triage dirty & uncommitted work, rescue at-risk commits onto clean fix/*
  branches, commit the manifest repo, run west dw handoff, and verify. Use
  before ending a session, before any west update / git checkout / west patch
  apply, or whenever `git status` shows uncommitted work in the Darling
  workspace. Prevents silent loss of perf work (e.g. perf#21b nearly lost off a
  generated integration branch; the whole manifest repo was once uncommitted).
---

# darling-durability: make workspace work durable

The Darling workspace has three durability traps, all of which have bitten us:
1. **Uncommitted worktree changes** — lost on any `git checkout`/`west update`.
   `west dw handoff` CANNOT carry dirty worktrees (it only prints them).
2. **Commits only on a GENERATED `integration/<profile>` branch** — `west patch
   apply`/`clean` regenerates that branch and DISCARDS anything committed only there.
3. **An uncommitted manifest repo** (`darling-workspace`) — the single most fragile
   spot; it holds the exported patch profile (patches.yml + patch files), tooling,
   and the handoff bundles themselves.

Canonical rules (workspace CLAUDE.md): clean `fix/*` branch = editable source;
patch files + `patches.yml` = portable; `integration/*` + profile `west.lock.yml`
= generated (never edit). **Never push** unless explicitly asked.

## Procedure

### 1. Triage — find every at-risk piece
```
cd ~/work/darling-dev
west forall -c 'test -n "$(git status --porcelain)" && echo "DIRTY: $(pwd)"' 2>/dev/null | grep -i dirty
```
Also inspect the manifest repo itself (it is NOT in `west forall`):
```
git -C darling-workspace status --porcelain
```
For the superproject, distinguish real dirt from submodule-pointer drift:
```
git -C darling status --porcelain --ignore-submodules=all --untracked-files=no
```
(empty = only submodule pointers moved = expected, not a durability risk).

Classify each: **keep** (commit it), **rescue** (on a generated branch → move),
or **artifact/experiment** (commit but not for a profile). Read
`docs/perf-work-triage-*.md` for the current keep/rescue map.

### 2. Rescue commits stranded on a generated integration branch
If a keeper commit sits only on `integration/<profile>`, cherry-pick it onto a
clean `fix/*` at **manifest-rev** (NOT onto the integration tip):
```
REV=$(west list -f '{revision}' darling)      # resolve manifest revision
git -C darling branch -f fix/<name> $REV
git -C darling switch fix/<name>
git -C darling cherry-pick <sha>              # clean pick => independent of the profile
git -C darling switch integration/<profile>   # restore working branch
```
A clean cherry-pick onto manifest-rev also MEASURES independence from the profile.

### 3. Commit uncommitted keepers onto clean fix/* branches
For each repo with real dirt (e.g. dyld reader, tooling), create/switch a
`fix/*` (or the owning topic branch) and commit — WIP commits are fine; the goal
is durability, not working code. Make the message name the bead and mark WIP if
the feature isn't done.

### 4. Commit the manifest repo
Stage content first, but EXCLUDE handoff-generated artifacts (handoff refreshes
them next), then commit; you re-commit the artifacts after handoff:
```
cd darling-workspace
git add -A
git reset -q handoff/ .beads/issues.jsonl base.xml locked.xml state/repos.tsv \
              west.lock.yml patches/homebrew/west.lock.yml
git commit -m "Commit workspace state: <what>"
```

### 5. Handoff
```
cd ~/work/darling-dev && west dw handoff
```
Then verify the new branch tips actually landed in the bundles:
```
git -C darling-workspace bundle list-heads handoff/src__external__<repo>.bundle | grep <branch>
git -C darling-workspace bundle list-heads handoff/root.bundle | grep <branch>
```

### 6. Commit the refreshed handoff artifacts
```
cd darling-workspace
git add -A handoff/ .beads/issues.jsonl base.xml locked.xml state/repos.tsv \
             west.lock.yml patches/homebrew/west.lock.yml
git commit -m "Refresh handoff bundles + locks"
git status --porcelain   # must be empty
```

### 7. Final safety check
- Prod binaries still at baseline (`west darling-doctor`, or md5 the deployed
  dyld/mldr/dserver vs `deploy-baseline.md5`): 79b22273 / f0cd2a82 / 835946f9.
- No stray procs (`pgrep -af 'mldr|darlingserver|vchroot'`).
- Every keeper branch tip printed and confirmed in a bundle.

## Hard rules
- Never push to any remote unless explicitly asked (LOCAL commits only).
- Never edit `integration/*` branches or generated locks; rescue onto `fix/*`.
- Back up dirty files to the job tmp dir before large staging, as a safety net.
- Prod baseline binaries stay byte-identical; restore if you deployed.

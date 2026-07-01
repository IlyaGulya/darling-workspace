# Darling workspace control plane

Private West manifest and portable coordination state for development on
Darling without adding personal metadata to Darling or its upstream submodules.

This repository is the source of truth for the development workspace. A
checkout under `~/work/darling-dev/darling` is a working copy, not the durable
record of tasks, unpublished branches, or PR preparation.

## Ownership boundaries

- `west.yml`: active multi-repository workspace manifest.
- `west.lock.yml`: frozen upstream workspace revisions.
- `patches/`: reproducible local integration profiles with provenance.
- `handoff/`: Git bundles containing every local non-default branch.
- `.beads/issues.jsonl`: shared task graph for humans and agents.
- `pr-drafts/`: PR descriptions and review notes.
- `state/repos.tsv`: reproducible snapshot of checked-out commits and branches.
- `bin/dw`: workspace commands.

Darling's `.gitmodules` remains the upstream build contract. West provides the
developer control plane over the same repositories. The old `repo` XML
manifests remain temporarily as migration evidence and fallback.

## Setup on another machine

```bash
west init -m git@github.com:IlyaGulya/darling-workspace.git ~/work/darling-dev
cd ~/work/darling-dev
west update
west dw restore
west dw beads sync --import-only --rebuild
west patch verify --profile homebrew
west patch apply --profile homebrew --roll-back
```

## Daily use

```bash
west status
west forall -c 'git log -1 --oneline'
west dw summary
west dw beads ready
west dw restore
west patch list --profile homebrew
west patch verify --profile homebrew
west patch apply --profile homebrew --roll-back
west patch clean --profile homebrew
west darling-doctor            # verify manifest/build/deploy alignment BEFORE building or booting
west darling-build             # doctor-gated ninja build of dyld + closure (add --deploy to install)
west dw handoff
```

`west darling-doctor` is the guard against the drift that caused the perf#24c2c-pre
detours: it checks each project's working tree against its **West manifest** revision
(intentional drift is declared in `doctor-allow-drift.txt`), that the build dir's
`CMAKE_INSTALL_PREFIX` matches the prefix baked into the setuid launcher (a mismatched
build can never boot the prefix), and that the deployed dyld/mldr/darlingserver match the
known-good `deploy-baseline.md5`. Run it before any build/deploy/boot. `west darling-build`
runs the doctor as a pre-gate, refuses to build on failure (unless `--force`), and re-checks
after `--deploy`. Update `deploy-baseline.md5` when a legitimate rebuild changes what is
deployed.

`west dw handoff` exports Beads, refreshes manifests, and creates Git bundles
for every local branch except an unchanged `main`/`master`. This includes
active topics, clean PR branches, and backup snapshots. The bootstrap flow
syncs `base.xml`, then restores all those branch refs from the bundles.

Uncommitted worktree changes cannot be handed off. `dw handoff` prints every
dirty repository so it can be committed or intentionally discarded first.

## Sharing code without fork noise

Share stable work as normal upstreamable commits and clean PR branches. Export
branches selected for local composition with:

```bash
./scripts/export_patches.py homebrew \
  --source-root /path/to/existing/darling
./scripts/export_patches.py homebrew --check \
  --source-root /path/to/existing/darling
```

`patches/<profile>/patches.yml` records the source branch and commit, Bead or
PR, patch checksum, and application order. `west patch apply` uses
`git am --3way`, creates clean `integration/<profile>` branches, records the
top-level submodule pointers, and writes a frozen profile lock.

## Patch profile invariants

- Canonical editable source: clean `fix/*` branch in the owning repository.
- Portable integration source: patch files plus `patches/<profile>/patches.yml`.
- Generated local state: `integration/<profile>` branches and the profile
  `west.lock.yml`.
- Historical source: `backup/*` and preserved mega-branches.

Never edit or open a PR from `integration/<profile>`. Never edit patch files or
generated locks manually. Refresh patches with `scripts/export_patches.py`,
then run `west patch verify`. Source commits must be full 40-character SHAs.

`west patch clean` only operates when affected repositories are on the matching
integration branch or detached at `manifest-rev`, and refuses dirty worktrees.
`--force` is reserved for intentional recovery. The tracked profile lock is
updated only by a successful `west patch apply`.

## Pull request workflow

GitHub publication is a separate state machine over the same clean branches:

```bash
west pr list --profile homebrew
west pr dashboard --profile homebrew
west pr check --profile homebrew dar-q95.3
west pr publish-plan --profile homebrew dar-q95.3 --target fork
west pr fork-draft --profile homebrew dar-q95.3 --dry-run
west pr fork-draft --profile homebrew dar-q95.3
west pr sync --profile homebrew dar-q95.3
west pr upstream-draft --profile homebrew dar-q95.3
west pr update-body --profile homebrew dar-q95.3 --target fork
west pr ready --profile homebrew dar-q95.3 --target upstream
west pr open --profile homebrew dar-q95.3 --target upstream
```

A fork draft compares `fix/*` against `preupstream/<base>` inside the
`IlyaGulya` fork. `west pr fork-draft` updates that staging base from
`manifest-rev`, pushes the exact `source-commit`, and opens a draft without
notifying Darling maintainers. An upstream draft is a separate PR from the same
fork branch into `darlinghq`.

PR bodies are generated from the `## Title` and `## Body` sections in
`pr-drafts/*.md`. GitHub URLs and synchronized state are stored under each
patch's `github.fork` and `github.upstream` sections. Publishing is always
single-Bead and explicit; there is no bulk publish or automatic merge command.

One private manifest repository is intentional. Split Beads only if it needs
different access control or an independent lifecycle.

See `docs/branch-migration.md` for the branch workflow and
`docs/mega-branch-audit-2026-06-12.md` for the completed migration audit,
residual dispositions, and conditions for retiring the historical refs.

`west.yml` is the selected workspace backend and includes private workspace
tools such as `darling-debug-runner`. The old `repo` manifests are retained as
migration evidence and fallback only. See `docs/west-spike.md` for validation
results.

# Darling workspace control plane

Private `repo` manifest and portable coordination state for development on
Darling without adding personal metadata to Darling or its upstream submodules.

This repository is the source of truth for the development workspace. A
checkout under `~/work/darling` is an existing working copy, not the durable
record of tasks, unpublished branches, or PR preparation.

## Ownership boundaries

- `base.xml`: publicly fetchable revisions used to bootstrap.
- `locked.xml`: exact local SHA of every repository for auditing.
- `handoff/`: Git bundles containing every local non-default branch.
- `.beads/issues.jsonl`: shared task graph for humans and agents.
- `pr-drafts/`: PR descriptions and review notes.
- `state/repos.tsv`: reproducible snapshot of checked-out commits and branches.
- `bin/dw`: workspace commands.

Darling's `.gitmodules` remains the upstream build contract. Google `repo`
provides the developer control plane over the same repositories: bulk status,
sync, topic branches, commands, and reproducible bootstrap.

## Setup on another machine

```bash
git clone <private-control-repo> ~/work/darling-workspace
~/work/darling-workspace/bin/dw bootstrap ~/work/darling-dev \
  <private-control-repo>

cd ~/work/darling-workspace
DW_DARLING_SRC=~/work/darling-dev/darling bin/dw setup-local
bin/dw beads sync --import-only --rebuild
```

If the source checkout is elsewhere:

```bash
cp workspace.env.example workspace.env
# Edit DW_DARLING_SRC in workspace.env.
```

## Daily use

```bash
bin/dw status                  # repo status
bin/dw dirty                   # only dirty projects
bin/dw topic fix/foo darling/src/external/xnu
bin/dw forall 'git log -1 --oneline'
bin/dw source-sync
bin/dw beads ready
bin/dw handoff
```

`dw handoff` exports Beads, refreshes both manifests, and creates Git bundles
for every local branch except an unchanged `main`/`master`. This includes
active topics, clean PR branches, and backup snapshots. The bootstrap flow
syncs `base.xml`, then restores all those branch refs from the bundles.

Uncommitted worktree changes cannot be handed off. `dw handoff` prints every
dirty repository so it can be committed or intentionally discarded first.

`dw setup-local` writes only checkout-local files and `.git/info/exclude`; it
does not edit Darling's tracked `.gitignore`.

## Sharing code without fork noise

Share stable work as normal upstreamable commits and PR branches. For work that
must move between machines before it is ready for a fork:

```bash
git -C ../darling format-patch --output-directory \
  ../darling-workspace/patches/<topic>/ <base>..HEAD
```

Store the base commit and target repository in that topic's README. Avoid
committing build products, SQLite databases, logs, or uncommitted source diffs
to this repository.

One private manifest repository is intentional. Split Beads only if it needs
different access control or an independent lifecycle.

See `docs/branch-migration.md` for converting the preserved mega-branches into
clean per-PR branches and eventually retiring the old checkout.

`west.yml` is the selected workspace backend and includes private workspace
tools such as `darling-debug-runner`. The old `repo` manifests remain only
until the first integration profile and branch migration pass. See
`docs/west-spike.md` for validation results.

# Darling workspace control plane

Private `repo` manifest and portable coordination state for development on
Darling without adding personal metadata to Darling or its upstream submodules.

## Ownership boundaries

- `base.xml`: publicly fetchable revisions used to bootstrap.
- `locked.xml`: exact local SHA of every repository for auditing.
- `handoff/`: minimal Git bundles containing unpublished topic commits.
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

`dw handoff` exports Beads, refreshes both manifests, and creates minimal Git
bundles for topic commits not contained in `origin/main` or `origin/master`.
The bootstrap flow syncs `base.xml`, then restores those topic branches from
the bundles. This moves committed WIP without pushing it to upstream or forks.

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

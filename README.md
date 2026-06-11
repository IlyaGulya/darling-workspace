# Darling workspace control plane

Private, portable state for development on Darling without adding personal
metadata to Darling or its upstream submodules.

## Ownership boundaries

- `../darling`: source code, commits, branches, and upstream-ready tests only.
- `.beads/issues.jsonl`: shared task graph for humans and agents.
- `pr-drafts/`: PR descriptions and review notes.
- `state/repos.tsv`: reproducible snapshot of checked-out commits and branches.
- `bin/dw`: workspace commands.

Darling's `.gitmodules` remains the source of truth for repository layout and
build pins. This repository does not replace submodules with Google `repo`.

## Setup on another machine

```bash
git clone <private-control-repo> ~/work/darling-workspace
git clone --recurse-submodules git@github.com:IlyaGulya/darling.git ~/work/darling
cd ~/work/darling-workspace
bin/dw setup-local
bin/dw doctor
bin/dw beads sync --import-only --rebuild
```

If the source checkout is elsewhere:

```bash
cp workspace.env.example workspace.env
# Edit DW_DARLING_SRC in workspace.env.
```

## Daily use

```bash
bin/dw status
bin/dw beads ready
bin/dw snapshot
bin/dw sync
```

`dw sync` exports Beads to JSONL and refreshes `state/repos.tsv`. Review and
commit this control repository separately from source commits.

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

One control repository is intentional. Split Beads into a second repository
only if it needs different access control or an independent lifecycle.

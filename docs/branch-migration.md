# Branch and PR migration

`darling-workspace` is the durable source of truth. The existing
`~/work/darling` checkout is an import source and temporary working copy.

## Current state

- Beads and dependencies live in `.beads/issues.jsonl`.
- PR descriptions live in `pr-drafts/`.
- `handoff/manifest.json` inventories private branch refs and exact SHAs.
- `handoff/*.bundle` contains the commits behind those refs.
- `base.xml` contains publicly fetchable bases for a clean `repo sync`.

## Migration rules

1. Never prepare a PR directly from the historical
   `fix/homebrew-psynch-ruby-hang` mega-branch.
2. Each upstream PR gets one clean branch in the repository that owns the
   change.
3. Each clean branch maps to one Bead and one file in `pr-drafts/`.
4. A top-level Darling branch contains only the required gitlink update and
   top-level tests or files.
5. Diagnostic and abandoned work remains in a named `backup/*` bundle ref,
   not mixed into a PR branch.
6. Fork branches are publication targets, not the primary backup.

## Per-fix flow

```bash
# Start from the public base in the repo-managed client.
bin/dw topic fix/<name> darling/src/external/<project>

# Cherry-pick or rework only the relevant commits.
git -C <project> range-diff <base>..<old-branch> <base>..fix/<name>

# Validate, update the Bead and PR draft, then capture the workspace.
bin/dw beads update <issue> --status=in_progress
bin/dw handoff
git add .
git commit -m "Update <issue> PR preparation"
git push
```

Only after review of the range-diff and tests:

```bash
git -C <project> push -u origin fix/<name>
gh pr create --draft --repo darlinghq/<repo> --head IlyaGulya:fix/<name>
```

Record the PR URL in the Bead. Once the PR exists, the branch may still remain
in the bundle until it is merged.

## Recommended order

1. Preserve all current refs with `bin/dw handoff`.
2. Create a fresh repo-managed client from `base.xml`.
3. Restore bundle refs during `bin/dw bootstrap`.
4. Process already-split clean branches first (`darlingserver`, `xnu`,
   `libplatform`, `perl`).
5. Split remaining commits from the mega-branches into one branch per draft.
6. Prepare top-level gitlink PRs only after their submodule PRs have stable
   commit IDs.
7. Compare every clean series with `git range-diff`.
8. After all Beads have either a PR URL or an explicit archived disposition,
   archive `~/work/darling` and prune obsolete fork branches.

## Retirement gate

The old checkout can be removed only when:

- `bin/dw handoff` reports no uncommitted source changes;
- every private branch appears in `handoff/manifest.json`;
- every intended fix has a Bead and PR draft;
- a clean bootstrap succeeds on another directory or machine;
- bundle restoration and the required builds/tests pass.


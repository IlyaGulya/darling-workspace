# ADR: agent-friendly fork authoring

**Status:** accepted for the current fork workflow. This is a decision from
completed disposable experiments; it authorizes neither a production migration
nor a change to West or the 76 existing patches.

## Decision

Use **enhanced plain Git** as the default authoring and recovery interface.
Keep West as workspace materialization, and immutable Git refs plus a
versioned lock, expected resulting tree, and clean-ODB/preflight as the only
canonical durable state.

Jujutsu is allowed only as an **optional expert authoring clone**. It is not
allowed in an active West workspace and must follow the graph-invariant command
contract below. Git-spice is accepted only as an **optional, replaceable local
stacked-branch/review-layer helper** after the commit series already exists as
ordinary Git. It is not an authoring or canonical-state replacement.

GitButler is not accepted for a long dependent series. Sapling remains
rejected. GitHub `gh stack` remains a watchlist item until it leaves preview
and has a separate bounded evaluation.

## Architecture by layer

| Layer | Decision | Guardrail |
| --- | --- | --- |
| 1. Workspace | West materializes repositories and manifests. | Never author in an active West worktree. |
| 2. Durable patch state | Immutable Git refs, frozen/versioned lock, ordered OIDs, expected tree, clean-ODB/preflight. | Tool metadata is never canonical. |
| 3. Authoring/recovery | Enhanced plain Git by default. | Explicit range/base, bounded commands, reflog and rebase-abort recovery. |
| 4. Review-layer organization | Git-spice, opt-in only. | All resulting commits and refs remain ordinary Git. |
| 5. Publication/review | GitHub when separately authorized, or `format-patch`. | Immutable Git artifacts, not tool state, are handed off. |

## Evidence and rationale

### Enhanced plain Git — accepted default

The shared 19-change lifecycle is ordinary linear Git history with tree
`bfc8cb47203df7ce70b74447843d580aa779499e`; retirement retained that tree with
18 remaining downstream changes. It works in fresh stock-Git clones, supports
`format-patch`, has no hidden graph state, and provides direct recovery through
reflogs and `rebase --abort`. It is the lowest-complexity path and fallback if
an optional tool disappears.

### Jujutsu — optional expert clone, not default

The clean JJ run reached the common 19-change expected tree. Change IDs retain
logical identity across rewrites, and native propagation can make one conflict
resolution practical. But Change ID alone does not preserve stack shape:
earlier exploration showed an unbounded source rewrite can capture a parked
working-copy descendant. Use a separate colocated clone, park `@` outside the
publication range, use explicit Change ID/revset ranges, and after every
mutation check count, linear topology, bookmark tip, selected Change IDs, and
absence of divergence. Ordinary-Git publication and a fresh stock-Git clone
remain mandatory.

### GitButler — rejected for long dependent stacks

The corrected native path used `but commit empty --after` and direct `but rub`,
not unassigned workspace content. It still produced a genuine pre-synthetic
content conflict in `dserver-ring.h`. Native resolution materialized Git stages
1/2/3 and two marker regions, requiring a content choice before the deliberate
synthetic add/add conflict. That exceeds the one-manual-resolution invariant.
The early state was also GitButler-owned and not fully stock-Git auditable.
GitButler is therefore not approved even in an authoring clone for this stack.

### Git-spice — optional publication layer only

Git-spice v0.31.0 represented the accepted 19 commits as ordinary `5/5/5/4`
branches. Repeated restack was a no-op; empty amend propagated through
descendants and preserved the expected tree. A local bare fork and fresh
stock-Git clone retained 19 linear commits, 19 patches, and preflight validity.

Its boundary is deliberate:

- Commit-content `fixup` is experimental and failed before ref mutation here;
  it is not an approved content-edit path.
- Dependent reorder conflicted and recovery was `gs rebase abort`, not an
  operation-log contract.
- In fork mode only the trunk-adjacent branch can be a cross-fork review head;
  upper layers remain fork-relative until lower landing and restack.
- `gs repo sync` rejected the local-path remote. That proves a local-path
  limitation only; it does not characterize hosted remotes. A minimal Git
  `fetch` plus `merge --ff-only upstream/main` bridge handled the simulation.
- Landing did not auto-retire `layer-1`: explicit `gs branch untrack layer-1`
  was required to make `layer-2` formally trunk-adjacent, and untrack left the
  ordinary Git ref for separate cleanup. Both following restacks were no-ops,
  leaving 14 downstream commits and the same expected tip tree.

Do not deploy git-spice broadly now. Offer it only as opt-in local review-layer
organization; reconsider after a separately authorized hosted-remote pilot.

### Other alternatives

- **Sapling:** previously rejected; it did not provide a robust, deterministic
  noninteractive fit for the common lifecycle.
- **GitHub `gh stack`:** watchlist only while preview; no credential,
  dependency, or operational decision until a stable bounded evaluation.

## Practical agent command contract

1. West only materializes. Create a fresh disposable or dedicated authoring
   clone with no alternates; do not mutate the active West worktree.
2. Before every mutation record base, ordered range, count, linearity, tree,
   and cleanliness. Use explicit ranges, never an inferred loose tip.
3. Default to bounded plain-Git `rebase`, `cherry-pick`, `format-patch`, and
   explicit `rebase --abort`/reflog recovery; validate metadata after rewrites.
4. With JJ, map frozen Git OIDs to Change IDs once. After the first rewrite use
   only Change IDs/revsets; park `@`; require no divergence, expected count,
   linear graph, preserved Change IDs, and tip bookmark after each operation.
5. With git-spice, first create accepted Git commits with plain Git. Use only
   tracking/split/restack/review layers, inspect private metadata, retain Git
   recovery, and after landing use explicit Git bridge plus untrack and separate
   ordinary-ref retirement.
6. Before handoff, validate immutable refs and frozen lock with clean-ODB
   preflight, exact tree, fresh stock-Git clone, and `format-patch`. This ADR
   authorizes no external publication action.

## Evidence locations

- Plain Git: `/tmp/dar-agent-friendly-fork-authoring-qz6e/plain/full/retirement-report.txt`
- JJ clean: `/tmp/dar-agent-friendly-fork-authoring-qz6e/jj-clean/final-report.txt`
- JJ native (invariant rationale): `/tmp/dar-agent-friendly-fork-authoring-qz6e/jj-native/jj-native-report.txt`
- GitButler: `/tmp/dar-agent-fork-authoring-tools-6x44/gitbutler-corrective/resolve-classification-report.md`
- Git-spice: `/tmp/dar-agent-fork-authoring-tools-6x44/git-spice/run/git-spice-phase2-report.md`
  and `/tmp/dar-agent-fork-authoring-tools-6x44/git-spice/post-landing/`.

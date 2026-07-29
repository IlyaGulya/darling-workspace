# Patch-stack profile composition

An immutable schema-v2 series lock and a profile result state two different
facts.  A series lock remains the sole source of truth for its declared base,
ordered immutable commits, metadata, and standalone expected tree.  It is not
rewritten merely because a profile applies the same patch content after another
profile has changed that repository.

`*-profile-composition-v2.yml` uses schema version 3. It binds one exact
lock-first mapping by filename, SHA-256, batch identity, grouped module order,
profile starting tree, each applied boundary tree, and each module final tree.
It additionally binds the exact regular frozen root `west.lock.yml` by
SHA-256, so every untouched West project is part of the profile input rather
than ambient local state. `final_tree` is the tree immediately after the final patch series;
`integration_final_tree` is the complete profile result after the normal
parent integration record.  They differ only where an overlapping parent
records nested West project gitlinks.  It never replaces or edits the series
lock it references.

A stacked profile additionally names its prerequisite composition lock by
filename and SHA-256, repeats the frozen-manifest identity, and records the
exact prerequisite final tree for every module. It does not record or fetch a
generated profile integration commit OID. For the overlapping `darling`
parent, validation compares non-gitlink content plus each managed child tree:
the raw parent Gitlink OIDs are lifecycle evidence and may differ between
otherwise identical materializations. A standalone `west patch apply --profile
perf` first materializes homebrew from its own immutable series locks,
validates those module trees, and only then replays perf. This keeps
identity-dependent replay commits in evidence only.

The old perf references `43b4e876…` and `585b0e89…` are classified as
generated homebrew integration and generated applied replay commits,
respectively. Neither is a canonical source or needs publication. The genuine
immutable mldr source is `93ba455b8e3d8ee3d04c712b3579c3d5b5e78fb7`, with
its original base `50b2e05dd9e21d9f39e35d947f830ae651aa3366`; both are
already declared by the historical series lock and reachable through its
mirror tags.

During replay, an entry whose actual starting tree equals its immutable base
tree must produce the series lock's standalone `expected_tree`. If the trees
differ, the materializer first proves native replay identity (linear commit
count, exact `git range-diff`, and stable patch-id) and then requires the
declared profile boundary tree. The historical mldr series illustrates the
distinction: its standalone lock tree is `1fce0600…`; the pre-Rootless
homebrew boundary yielded `5befc5cf…`, while Rootless Batch 8 now yields
`ef340c5f…`. The old `cd4b07ee…` value belongs to a superseded generated
profile integration and is not a canonical source or prerequisite. A missing
composition boundary fails closed.

## Homebrew audit

The original independent clean-ODB audit recorded 69 native replays.
Rootless productization Batch 8 appends three direct-boundary series (typed
runtime mode in Darlingserver and Darling, plus the AF_UNIX expanded-path
length fix in XNU), bringing the exact grouped total to 72. Twenty-nine
entries start on an inherited profile base rather than their standalone source
base: 1 Darlingserver, 18 XNU, and 10 Darling.  All 29 are classified
`INHERITED_BASE_DELTA`: their complete ranges have exact range-diff equality,
equal stable patch IDs, and equal changed-path sets.  There are zero
`SEMANTIC_CHANGE` entries and zero source-series rewrites. The three appended
series each declare the exact previous applied integration commit as their
immutable base.

| classification | Darlingserver | XNU | Darling | total | new immutable source rewrites |
| --- | ---: | ---: | ---: | ---: | ---: |
| `INHERITED_BASE_DELTA` | 1 | 18 | 10 | 29 | 0 |
| `EQUIVALENT_PATCH_REPLAY` | 0 | 0 | 0 | 0 | 0 |
| `SEMANTIC_CHANGE` | 0 | 0 | 0 | 0 | 0 |

The table is deliberately about the 29 series/base mismatches.  It does not
silently classify an unrelated frozen West project revision as a patch-series
mismatch: that project has no series lock or archive identity to compare.

The resulting module boundary trees are recorded in
`homebrew-profile-composition-v2.yml`; the final trees are Darlingserver
`596cc49f5ff12d4fca8accb58ea453362e9ae1d1`, XNU
`53c8fa45a1ac94bdfc2ced0b3179e43659dffabf`, and Darling
`5e8144538bdc7cb7958dc22edc4016e8b64a6591`.

## Rootless prerequisite cascade

Because Perf and Arch explicitly consume the Homebrew composition, the three
new Batch 8 module trees are not ambient state. Perf replays its seven entries
from those exact trees. Its XNU and Darling series remain immutable historical
inputs with new profile boundaries; the three Darlingserver entries use
append-only profile-integration locks because the typed runtime target and the
existing ring target both extend the same CMake target list. Their final trees
are XNU `15a50d8f…`, Darlingserver `f0e535f3…`, and Darling `fbef26d2…`.

Arch then consumes that exact Perf result. Its first eight Darlingserver
entries and all three XNU entries replay unchanged with new declared
boundaries. The stack-pool entry required one reviewed mechanical integration:
the existing `dserver_runtime_mode_tests` block and the incoming
`dserver_stack_pool_tests` block are both retained exactly once. Four
subsequent one-entry series were restacked without conflict on that actual
profile boundary. The final Darling shellspawn series likewise retains both
independent includes—typed runtime mode and structured wait-status—without
changing either control flow. The restacked locks yield Arch final trees XNU
`3e1dbe60…`, Darlingserver `8a9ec6b9…`, and Darling `9c0d96fb…`.

No accepted immutable ref is rewritten. The v8 bases and sources remain a
local-only publication proposal until their create-only hosted refs are
separately reviewed and authorized.

## Runtime-source lifecycle

`RuntimeSourceMaterializer` consumes the same typed composition plan.  Its
temporary worktrees establish the first immutable base for each module, then
carry every inherited profile boundary forward.  For the overlapping
`darling` parent this includes a disposable integration commit after each
profile phase, recording the already-materialized nested West project
gitlinks exactly as normal `west patch apply` does.  That generated commit ID
is lifecycle-only evidence, never a series source OID; the next phase remains
authorized by the typed profile boundary trees.  The lifecycle worktrees and
all transaction refs remain disposable.

Batch evidence includes a normalized `profile_composition` manifest whenever a
schema-v3 mapping declares one.  It makes the profile start, boundaries, final
trees, tree-based prerequisites, and the exact mapping binding reviewable without
confusing generated integration commit IDs with canonical source identity.

## Frozen workspace baseline gate

The composition manifest currently proves every module that participates in a
patch-series replay.  A full profile build also depends on untouched projects
from the frozen root `west.lock.yml`; those projects must not be treated as
ambient local state.  The Arch build audit found precisely such a missing
assertion: the frozen manifest pins objc4 to `1a12df76…`, while the active
worktree has a local-only corrective commit `53342cec…`.  The clean candidate
therefore fails its normal Release compile at objc4's debug-macro consistency
check. Its standalone clean-ODB closure and macro contract are recorded in
`objc4-macro-contract-decision.md`; a future manifest advance must use that
closure, never an active-worktree object.

This is neither an `INHERITED_BASE_DELTA` nor a series semantic resolution. It
is a separate frozen-workspace-baseline blocker. Profile-composition schema v3
now binds the root manifest checksum; a separately reviewed immutable
manifest/project change must still supply the objc4 correction (or establish
another reviewed canonical baseline). Until then clean-ODB patch replay is
valid, but the complete workspace result and deployed behavioural gates are
not.

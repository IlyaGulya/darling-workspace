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
homebrew boundary yielded `5befc5cf…`, while prefix-lifecycle Batch 9 yields
`e5c5611c…` in the real overlapping West layout after its validated managed
child gitlinks are recorded. The old `cd4b07ee…` value belongs to a superseded
generated profile integration and is not a canonical source or prerequisite.
A missing composition boundary fails closed.

## Homebrew audit

The original independent clean-ODB audit recorded 69 native replays.
Rootless productization Batch 8 appended three direct-boundary series (typed
runtime mode in Darlingserver and Darling, plus the AF_UNIX expanded-path
length fix in XNU). Prefix-lifecycle Batch 9 appends one typed lifecycle series
for Darlingserver and one for Darling, bringing the exact grouped total to 74. Twenty-nine
entries start on an inherited profile base rather than their standalone source
base: 1 Darlingserver, 18 XNU, and 10 Darling.  All 29 are classified
`INHERITED_BASE_DELTA`: their complete ranges have exact range-diff equality,
equal stable patch IDs, and equal changed-path sets.  There are zero
`SEMANTIC_CHANGE` entries and zero source-series rewrites. The five appended
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

The Batch 9 module boundary trees were recorded in
`homebrew-profile-composition-v2.yml`; its final trees were Darlingserver
`2d6f0321cfe205dba7302666bc470adb79a44003`, XNU
`53c8fa45a1ac94bdfc2ced0b3179e43659dffabf`, and Darling
`7297ee393ed21d13484b1734e5e5694967f96851`.

## Rootless prerequisite cascade

Because Perf and Arch explicitly consume the Homebrew composition, the two
new Batch 9 module trees are not ambient state. Perf replays its seven entries
from those exact trees. Its XNU and Darling series remain immutable historical
inputs with new profile boundaries; the three Darlingserver entries use
append-only profile-integration locks because the typed runtime target and the
existing ring target both extend the same CMake target list. Their final trees
are XNU `15a50d8f…`, Darlingserver `43da4fa5…`, and Darling `e5c5611c…`.
The normalized source-only Darling tree is `77abde10…`; the authoritative
composed boundary additionally records the already-materialized child gitlinks.

Arch then consumes that exact Perf result. Its first eight Darlingserver
entries and all three XNU entries replay unchanged with new declared
boundaries. The stack-pool entry required one reviewed mechanical integration:
the existing `dserver_runtime_mode_tests` block and the incoming
`dserver_stack_pool_tests` block are both retained exactly once. Four
subsequent one-entry series replay without conflict on that actual profile
boundary. Prefix-lifecycle changes adjacent main-function context, so
stack-pool uses a new append-only v10 immutable source whose resulting tree
remains `a390561a…`; the verifier is not weakened to accept the historical
context identity. The final Darling shellspawn series likewise retains both
independent includes—typed runtime mode and structured wait-status—without
changing either control flow. The restacked locks yield Arch final trees XNU
`3e1dbe60…`, Darlingserver `8b39e628…`, and Darling `8cbc1350…`.

No accepted immutable ref is rewritten. The v8 and v10 bases and sources remain
a local-only publication proposal until their create-only hosted refs are
separately reviewed and authorized.

## EUNION parent-dominance admission

Batch 10 (`darling-homebrew-eunion-parent-dominance-batch-10`) adds
`xnu/eunion-upper-parent-dominance.patch` after the existing XNU series in
grouped execution order. All 74 prior series retain their relative order,
source locks, and Homebrew applied boundaries. The new schema-v2 recipe binds
base `cb2cf623d9cd641ed4f9e899a0187acdedc423ea`, the single ordered source
commit `0ae4c3fecc658859002346356ddfc2ae01391166`, and source tree
`c0f7e6685242e284938bb6d9d652e55b311bed17`.

Native `format-patch`/`git am` recipe generation on the existing Homebrew XNU
boundary produced `553220e4a0b9ebb7905d39abcd0880dcb2718b69`. Replaying the
unchanged Perf and Arch source recipes then produced XNU final trees
`d28cb624090489594c896738ef3c0e159120b048` and
`2396da43c219e7dba14df419d97c30e5f36cba53`. The dependent compositions bind
these exact trees and the refreshed prerequisite digests. This authoring
replay is not clean-ODB acceptance evidence.

Publication remains blocked: the new content-addressed base/source tags need
separate create-only hosted publication authorization. Local source branches
and handoff bundles do not replace the immutable-ref fetch gate. Canonical
applicability, exact replay identity, and clean-ODB acceptance remain required;
no source tree, inherited boundary, or publication check is waived.

For the next admission, commit/export the independent source range first,
derive its exact linear `git rev-list --reverse BASE..TIP` and
`git show -s --format=%T TIP`, and add a new schema-v2 recipe with the existing
content-addressed mirror-ref convention. In a disposable repository, start at
the owning module's current composition boundary and use the existing
`patch_stack_lock_first._cherry_pick` native replay helper on each declared
commit to derive the applied tree. Add the series at its grouped module tail,
advance the typed batch/count and runtime count guard, then refresh mapping
SHA-256, dependent profile replay boundaries, and composition SHA-256 bindings
in prerequisite order. Parent gitlinks remain generated lifecycle evidence,
not authored source identity. Finally run canonical applicability through
`west patch verify --profile homebrew --applicability-only` and the dependent
profiles, with reviewed immutable refs available, before claiming acceptance.

## EUNION ownership admission

Batch 12 (`darling-eunion-ownership-batch-12`) appends
`xnu/eunion-ownership-syscalls.patch` after parent dominance, retaining all 76
prior series. Its source base is `50db68ce8504da6c9c8f24ddfd31d737b1f1db23`;
ordered commits are `6823b8ae1b4945ce6da0e7aee43269b15d938735` and
`812e6e820009dde5f5af832233ebd120b7258fb6`. The latter also fixes symlink
following after case-insensitive name correction.

Native replay produces XNU final trees
`dcd79f5ae99340e590f5ee904464f578999823a1` (Homebrew),
`85d308400571030b4a5417ef722621bc7ee2d4b4` (Perf), and
`cc31b4d449edb26201266153453c839e46420769` (Arch).
These are authoring boundaries, not hosted or clean-ODB acceptance.

Ownership calls now execute real Linux operations on UPPER copies.
Host fixtures observe kernel metadata effects, permission errors, symlink
semantics, and unchanged LOWER inode metadata in sibling and nested layouts.
`chown_ownership_guest` replaces the active disabled-ownership guest test.
The immutable 14-fixture Mach-O corpus remains historical evidence; current
prebuilt Homebrew selection contains 13 fixtures and excludes its ENOTSUP case.
No old receipt or binary is relabeled as evidence for new ownership behavior.

`west test --materialize-profile` explicitly replays the selected profile,
even when live checkouts already carry that profile's integration branch name.
This prevents newly admitted patches from being tested against stale GREEN
source. Publication and the stock Homebrew product milestone remain separate
gates.

Local product acceptance passed on a newly provisioned rootless prefix with
native CLT 13.2: unchanged stock Homebrew built lz4 1.10.0 from source
(`poured_from_bottle=false`, `built_as_bottle=false`). The installed program
roundtripped 262144 bytes both before and after shutdown/reuse; the LOWER
template digest remained identical. The guarded ownership guest fixture and
source-base host RED/GREEN also passed. `dar-arsg` and `dar-nmda` are closed
for this local behavior; repeated wget reinstall (`dar-gwn.7`) and publication
remain separate gates.

This is acceptance evidence for the native-CLT-13.2 prefix, not a general
Homebrew compatibility verdict. Validate each selected SDK against authoritative
package metadata and retain provenance; do not invent version metadata or
suppress SDK compatibility checks. Use [test-infra.md](test-infra.md) for
managed context/run commands and their separate acceptance criteria.

The subsequent wget run exposed a missing rootless Homebrew resource:
`etc/resolv.conf`, already installed by the ordinary Darling build. Userland
commit `681329d6b6cb007e86316c572b138de88f1cfb8b` includes that stock file
in the source-owned toolchain component. After provider rebuild/deploy,
guest curl resolves `ghcr.io` and completes certificate-verified HTTPS
(`https://ghcr.io/v2/` returns its expected HTTP 401), without prefix repairs.
This closes the DNS prerequisite, not the wget cancellation/freeze gate.

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

Parent replay normalization includes child modules inherited through the entire
checksum-bound prerequisite graph, including Homebrew-only children carried
through Perf into Arch. Only unchanged, declared gitlink records are normalized
in a temporary index; ordinary parent content and source-authored child changes
still require exact replay. Final verification independently compares inherited
child trees with their typed boundaries. For historical integration branches,
an inherited-only child is checked through the parent's recorded gitlink because
that child has no integration branch for the later profile.

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

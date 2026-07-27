# objc4 debug-build macro contract — local publication proposal

Status: locally validated as a proposed repository baseline advance; not
published and not yet bound to any production manifest or profile.

## Decision

Treat `53342cec3dcc9cde6dd1aa935bbe26d88c3e5aa8` as the proposed next objc4
repository baseline. It is a reviewed, self-contained direct child of the
frozen revision, rather than an application-specific patch series. Therefore
the future production change should advance the frozen objc4 manifest revision
after create-only publication of its immutable source ref. The schema-v2 lock
is retained as provenance and clean-ODB verification evidence; it must not be
added to a profile patch mapping.

## Forensic inputs

The frozen manifest pins `darling-src-external-objc4` to
`1a12df76d12bfc9fdfffadb290f7742763568765`. A clean Arch Release top-level
configure followed by the `objc_obj` build failed at
`runtime/objc-runtime.mm:128` with `mismatch in debug-ness macros`: the target
received global `-DOBJC_IS_DEBUG_BUILD=1` together with Release `-DNDEBUG`.

`53342cec3dcc9cde6dd1aa935bbe26d88c3e5aa8` is a direct child of that base
and changes only `runtime/CMakeLists.txt` (seven additions, three removals).
It was local-only during the forensic audit; its reviewed immutable
base/source tags are now published and verified separately.
The focused forensic records, original compiler command, and SHA inventory are
kept outside the workspace in the preserved forensic evidence bundle; they are not a
publication closure.

The locally authored commit `53342cec3dcc9cde6dd1aa935bbe26d88c3e5aa8` is
the reviewed candidate. Its `OBJC_IS_DEBUG_BUILD=0` form is safe because it
is target-private to `objc_obj`: an external consumer receives no definition,
so the `defined(OBJC_IS_DEBUG_BUILD)` declaration remains debug-only at the
public boundary. This differs from the historical rejected global definition,
which leaked `=0` to consumers.

The reviewed candidate is:

- parent/base: `1a12df76d12bfc9fdfffadb290f7742763568765`;
- one changed file: `runtime/CMakeLists.txt`;
- Debug `objc_obj` receives exactly `OBJC_IS_DEBUG_BUILD=1`;
- Release `objc_obj` receives exactly `OBJC_IS_DEBUG_BUILD=0`;
- external consumers receive neither definition;
- expected tree: `28c39bbb8e68d2d99c887260de2da8435fa79db3`.

| Previous input | Status | Canonical successor | Tree |
| --- | --- | --- | --- |
| `1a12df76d12bfc9fdfffadb290f7742763568765` | frozen baseline | base, retained | `6edeab0fd09232da4e89b3ca2a692395a249e823` |
| `53342cec3dcc9cde6dd1aa935bbe26d88c3e5aa8` | reviewed baseline candidate, hosted immutable source tag verified | same commit | `28c39bbb8e68d2d99c887260de2da8435fa79db3` |
| `7539ff6cdd597e48abc1ab7a77ad0dbdc6b3ca92` | forensic alternative | superseded before closure | `357318649bb88e238f2ef0910be6758023ca029b` |

The macro is an object-like compile definition, not a function-like macro;
therefore it has no caller arguments, evaluation count/order, or `if/else`
statement-expansion behavior.  Its complete contract is target-local
configuration selection: `DEBUG == OBJC_IS_DEBUG_BUILD` inside the runtime,
and a debug-only public declaration only when the macro is defined.

## Validation and closure

Release and Debug top-level Ninja configurations both compiled `objc_obj`.
Release emitted exactly `-DOBJC_IS_DEBUG_BUILD=0` for the private runtime
target; Debug emitted exactly `-DOBJC_IS_DEBUG_BUILD=1`; a distinct consumer
target received neither. A Release top-level `darlingserver` consumer link
also completed. Existing unrelated warnings were retained; no macro-contract
warning or error was introduced.

Two independent local bare closure copies are retained in the preserved
publication-evidence bundle. Both contain the reviewed create-only base/source
tag refs, have no alternates, shallow,
partial, or replace configuration, and pass `git fsck --full --no-dangling`.
The dual clean-ODB cherry-pick and `format-patch | git am --3way` results both
produce the expected tree.  These are local review evidence, not a
publication source.

## Immutable publication record

The following immutable refs are already published and were rechecked through
`ls-remote` as exact peeled OIDs in
`https://github.com/darling-next/darling-objc4.git`:

```
refs/tags/patch-stack/v1/bases/1a12df76d12bfc9fdfffadb290f7742763568765
refs/tags/patch-stack/v1/sources/53342cec3dcc9cde6dd1aa935bbe26d88c3e5aa8
```

The frozen objc4 revision may therefore advance in `west.yml` and
`west.lock.yml` in the separately reviewed production change, with affected
profile-composition bindings regenerated. Do not add the provenance lock to a
patch mapping.

## Composition follow-up decision

The objc4 candidate remains a separately reviewed frozen-manifest baseline
proposal. It is not a profile-series input. A later clean composition audit
showed that `caf5e675…` and `ebf0b01d…` differ only in generated nested
Gitlink OIDs. That is a profile-composition protocol issue, not an
objc4-derived source identity or a reason to select either generated tree as
the canonical baseline. The protocol must compare the child trees and the
parent's non-gitlink content before either value can be replaced.

The previously referenced perf OIDs
`43b4e876ad032635cfc5308ada0dc1bd383398b9` and
`585b0e89a7be83eaf8b8c0bd0ea7e69d1add0fea` are generated homebrew/profile
replay outputs, not immutable series sources. Their absence from the public
mirror is therefore not an object-closure blocker and no active-workspace
object may be used to restore them. Schema-v3 profile composition binds the
reviewed prerequisite module trees and frozen manifest instead of generated
integration commit identities. Perf's genuine immutable mldr series remains
the separately closed base `50b2e05dd9e21d9f39e35d947f830ae651aa3366` and
source `93ba455b8e3d8ee3d04c712b3579c3d5b5e78fb7`.

The remaining objc4 decision is independent: a future reviewed frozen-manifest
advance must make the `53342cec…` baseline available from its standalone
closure before it becomes a production prerequisite. It must not be smuggled
into a profile-series lock.

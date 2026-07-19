# Canonical lock-based patch-stack materialization

## Phase 1 audit: legacy materialization

`west patch` currently selects a profile, reads its `patches/<profile>/patches.yml`,
and may compose it through `base-profile`.  `west_commands/patch.py` creates and
maintains `integration/<profile>` lifecycle refs, uses disposable worktrees for
applicability checks, and applies each mbox artifact with `git am --3way
--committer-date-is-author-date`.  The legacy patch file is therefore both a
review/export artifact and executable input to profile materialization.

The legacy inputs have consumers beyond `patch.py`: the patch verifier and
profile tests consume `patches.yml`; `scripts/export_patches.py` produces the
mbox artifacts; and the test/PR helpers use profile and integration-ref state.
Those consumers continue unchanged in this phase.  Legacy safety guarantees
include a disposable applicability worktree, `git am --3way` conflict handling,
profile-aware ordering, and cleanup of temporary worktrees.  Its source of
truth, however, is an effective profile/base plus mbox input, rather than the
immutable commit graph declared by a canonical lock.

## New local command

`west patch materialize-lock --repo <clean-clone> --lock <schema-v2.yml>` accepts
only schema-v2 locks.  It first runs the existing read-only preflight, then
fetches exactly the declared immutable base and source tags into unique
transaction refs.  It verifies their OIDs, the exact ordered linear range,
non-merge parents, author/committer metadata, and the declared resulting tree.
It does not apply an mbox and never uses a topic or integration branch as an
input.

Preflight is not permissive: `INCOMPLETE` may proceed only if every completed
check is `PASS` and the only incomplete checks are unavailable declared objects
or immutable tags that this command fetches.  A mixed `INCOMPLETE` result that
also contains a `FAIL` is rejected before any fetch.

The result is verified in a disposable worktree.  Its worktree and transaction
refs are removed before a create-only local result ref is atomically written
under `refs/west/patch-stack-results/`.  The command rejects dirty, shallow,
partial, alternate, and replace-object repositories.  Result refs must pass
`git check-ref-format` and remain inside that namespace.  An existing result
ref is never changed.

Every attempt writes atomic JSON evidence (unless that write itself fails):
status/verdict, transaction ID, lock SHA-256, fetched OIDs, ordered commits,
resulting tree, result-ref state, and each cleanup operation.  Evidence-write
failure rolls back a result ref created by the transaction only if it still
resolves to the expected source OID.  Primary failures are not overwritten by
cleanup or evidence failures.  `KeyboardInterrupt`/SIGINT follows the same
cleanup path and is then re-raised.  The West command returns zero for a valid
materialization and one for every materialization error; `--json` emits the
success evidence on stdout.

The disposable root includes its transaction ID
(`west-lock-materialize-<id>`), so recovery addresses only that transaction's
worktree and never scans or removes another concurrent transaction's directory.

## Shadow-equivalence pilot

All runs used fresh local repositories, immutable hosted tags, a clean object
database (`git fsck --no-dangling`), and two byte-identical `format-patch`
exports from the materialized result.

| Case | Lock commits | Canonical/result tree | Legacy `git am --3way` tree | Result |
| --- | ---: | --- | --- | --- |
| `darling/sandbox-exec-pass-through` | 1 | `9c6a6c6750b31b3e42ff62efedf9d124d8836cd5` | `9c6a6c6750b31b3e42ff62efedf9d124d8836cd5` | equal |
| XNU `perf/shmem-ring-guest` | 17 | `c0b2c145f7f26734853657b165da88cc51ec7f46` | `c0b2c145f7f26734853657b165da88cc51ec7f46` | equal |
| installer `normalize-payload-paths` then `archive-path-containment` | 1 + 3 | `a9006cc5d62875c93b868d34c5a05361f663805b`, then `a616bc3fd5afce5c9ca62a34e3d0fad30c4b45c6` | same respective trees | equal |

The dependent installer run materialized each lock from its own declared
immutable base tag; it did not rely on a preceding local result ref.  A caller
that substitutes such a local ref for a declared immutable base is rejected by
schema/preflight.  This is intentional: lock order is graph-declared, not an
implicit mutable-worktree state.

## Phase 3 proposal: one-profile shadow switch

Do not switch profiles globally.  Add an opt-in shadow mode for the existing
`homebrew` profile and begin with the one-commit
`darling/sandbox-exec-pass-through` entry.  In one disposable clean clone it
would materialize the lock to a namespaced local result ref and separately run
the current legacy `git am` path, comparing each effective tree to the lock's
canonical expected tree.  The legacy patch remains mandatory as the review and
fallback artifact.  Only after repeated equal shadow results should a narrowly
scoped profile flag select the lock path for that single entry; no manifest,
profile default, or legacy artifact is changed by this proposal.

## Phase 3 opt-in shadow pilot

`west patch apply --profile homebrew --shadow-lock` is an opt-in comparison
only. It first constructs a plan from typed
`locks/patch-stack/shadow-series-v1.yml` metadata (`profile`, `module`, patch
path, and contained lock filename); `patch.py` has no series-name allowlist.
The selected profile must have exactly one metadata entry and that exact
module/path pair must occur exactly once in the profile's actual patch list.
Missing or duplicate entries fail before preparation or `git am`. The lock
filename must be relative to `locks/patch-stack`, contain no `..`, and resolve
there without a symlink escape. The ordinary `git am --3way` path remains the
only authoritative materialization path and writes the same integration ref
whether the flag is absent or present.

At the selected mbox application point, shadow creates two independent
disposable clean-ODB repositories. Both seed their base only by fetching the
lock's immutable mirror `base_ref`, then verify the resulting base OID; no
upstream URL/raw-SHA seed is used. One applies the same legacy `git am --3way`
mbox, observing its complete `From <OID>` chain, count, fetched base, and
resulting tree. The other invokes the schema-v2 canonical materializer from
immutable mirror refs. Those observed legacy values must equal the lock, and
both resulting trees must equal its expected tree. Any mismatch,
preflight/fetch error, or cleanup failure fails the apply. Its JSON evidence
contains only OIDs, trees, lock SHA-256, verdict and cleanup state; clones,
objects, bundles and result refs disappear with the transaction. By default
evidence is transaction-addressed; `--shadow-evidence PATH` is the explicit
alternative for a caller-selected published file.

Phase 3B hosted acceptance should remain manual-only: run the flag once in a
fresh homebrew materialization clone, preserve the JSON evidence, compare the
unchanged `integration/homebrew` ref to a no-flag run, and require a reviewer
to inspect both trees before considering any broader opt-in.

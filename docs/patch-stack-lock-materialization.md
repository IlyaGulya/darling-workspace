# Canonical lock-based patch-stack materialization

## Historical baseline

The original implementation used versioned `patches/<profile>/*.patch` mboxes
as both review artifacts and executable profile input. That design established
profile ordering and native `git am --3way` behavior, but it depended on patch
preimages that were not guaranteed to exist in a fresh object database.

The canonical source of truth is now the schema-v2 lock:

- immutable create-only base/source refs with exact OIDs;
- exact ordered linear commits;
- complete metadata;
- expected source tree.

Schema-v3 profile-composition locks separately bind those standalone series to
their actual grouped profile bases and expected applied trees.

## Single-lock command

`west patch materialize-lock --repo <clean-clone> --lock <schema-v2.yml>`
accepts only schema-v2 locks. It runs preflight, fetches exactly the declared
immutable base/source tags into transaction refs, validates the graph and
tree, and verifies the result in a disposable worktree.

An `INCOMPLETE` preflight may proceed only when every completed check is PASS
and the only missing inputs are the declared immutable objects/refs that the
transaction will fetch. Mixed FAIL/INCOMPLETE evidence is rejected.

The disposable worktree and transaction refs are removed before a create-only
local result ref under `refs/west/patch-stack-results/` is published. Result
refs pass `git check-ref-format`; existing refs are never changed. Atomic JSON
evidence records transaction identity, fetched OIDs, ordered commits, tree,
result-ref state, and each cleanup operation. Evidence or cleanup failure
removes a newly created result ref only by expected-OID compare-and-delete.
SIGINT follows the same cleanup and is re-raised.

Transaction roots contain a unique ID (`west-lock-materialize-<id>`), so
recovery never scans or deletes another job's directory.

## Profile batch replay

`west patch apply` and RuntimeSourceMaterializer validate the complete typed
batch before mutation, perform at most one immutable union fetch transaction
per module, and replay every immutable commit in grouped order. The temporary
mbox is generated outside the production repository with native
`git format-patch`; native `git am --3way --committer-date-is-author-date`
preserves the accepted message/whitespace semantics.

Profile-wide failure, cleanup failure, evidence failure, or SIGINT rolls back
all touched repositories. No partial integration ref, generated lock,
rebase-apply state, fetched ref, disposable worktree, or success evidence is
allowed.

All production profiles are canonical:

- homebrew: 72 series;
- perf: 7 series on the homebrew prerequisite;
- arch: 19 series on homebrew and perf.

## Early equivalence evidence

Initial clean-ODB pilots proved lock/tree equivalence for the sandbox canary,
the 17-commit XNU perf series, and the dependent installer series. Subsequent
Batch 1–7 and multi-profile acceptance established the full mappings. The
historical shadow mode used during migration is retired; it is not an
operational path.

The independent oracle now fetches only declared immutable refs and uses plain
Git cherry-pick. Production uses immutable format-patch/git-am. Their final
module trees, generated locks, mappings, manifests, and cleanup evidence must
match.

## Review and recovery export

`west patch export-locks --profile <profile> --output <new-directory>`
validates the immutable closure and emits one deterministic native
format-patch mbox per `(module, patch)` plus `evidence.json`. Evidence records
SHA-256, stable patch IDs, commit order/count, and resulting tree. The export
contains no Git object database, alternate, pack, bundle, or checkout archive.

Historical archives remain read-only forensic/provenance fixtures. They are
not fallback or acceptance inputs.

# Patch-stack read-only preflight

`west patch preflight --repo REPO --lock LOCK [--json]` only inspects local
Git state. It never fetches, checks out, updates a ref, changes the index or
worktree, or contacts a host. The lock schema is
`schemas/patch-stack-lock-v1.schema.json`; locks must declare
`schema_version: 1`, full lowercase SHA-1 OIDs, and an immutable OID equal to
`source_commit`. The immutable ref has one canonical form:
`refs/patch-stack/v1/sources/<source_commit>`; its suffix must exactly equal
`source_commit`.

It proves local object availability, immutable-ref/OID agreement when the ref
exists, ordered single-parent history from base to source, expected source
tree, repository cleanliness, alternates, shallow/partial state and author /
committer completeness. `UNKNOWN` means incomplete local evidence and never
means success. A locally unavailable immutable ref is incomplete (exit 2); a
locally resolved ref at a different OID is invalid (exit 1). Exit codes: 0
valid, 1 invalid contract, 2 missing evidence, 3 dirty-only failure, 4
filesystem/Git/tool error. Any unexpected nonzero Git command after the
repository probe is an `ERROR` (exit 4), never a pass inferred from empty
stdout.

Later slices own disposable authoring-clone creation, backup/recovery,
fixup/review, clean-ODB network gates, local publication, upstream planning,
retirement and 76-patch recovery. This command does not authorize any of them.

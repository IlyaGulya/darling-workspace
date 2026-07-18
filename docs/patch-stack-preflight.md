# Patch-stack read-only preflight

`west patch preflight --repo REPO --lock LOCK [--json]` only inspects local
Git state. It never fetches, checks out, updates a ref, changes the index or
worktree, or contacts a host. It validates the selected versioned JSON Schema
using the Draft 2020-12 keywords used by these schemas before applying
semantic cross-field checks.

Schema v1 (`schemas/patch-stack-lock-v1.schema.json`) remains for existing
local/custom-ref users. It declares `schema_version: 1`, full lowercase SHA-1
OIDs, and an immutable OID equal to `source_commit`. The immutable ref has one
canonical form:
`refs/patch-stack/v1/sources/<source_commit>`; its suffix must exactly equal
`source_commit`.

Schema v2 (`schemas/patch-stack-lock-v2.schema.json`) is for hosted immutable
tags. It declares `schema_version: 2` and records both sides of the immutable
boundary. Its `project.path` is exactly `.` and means the Git top-level passed
as `--repo`, never the process CWD; v2 rejects a `--repo` below that top-level.

```yaml
upstream:
  url: https://github.com/darlinghq/darling-xnu
  base_commit: <base SHA>
mirror:
  url: https://github.com/darling-next/darling-xnu
  base_ref: refs/tags/patch-stack/v1/bases/<base SHA>
  base_oid: <base SHA>
  source_ref: refs/tags/patch-stack/v1/sources/<source SHA>
  source_oid: <source SHA>
```

The v2 tag suffixes and OIDs must exactly equal `upstream.base_commit` and
`source_commit`. Both refs are resolved with `^{commit}`, so lightweight and
annotated tags are accepted only when they peel to the declared commit.

It proves local object availability, immutable-ref/OID agreement when refs
exist, ordered single-parent history from base to source, expected source tree,
repository cleanliness, alternates, shallow/partial state and author /
committer completeness. `UNKNOWN` means incomplete local evidence and never
means success. A locally unavailable immutable ref or tag is incomplete (exit
2); a locally resolved ref at a different OID is invalid (exit 1). Exit codes: 0
valid, 1 invalid contract, 2 missing evidence, 3 dirty-only failure, 4
filesystem/Git/tool error. Any unexpected nonzero Git command after the
repository probe is an `ERROR` (exit 4), never a pass inferred from empty
stdout.

Later slices own disposable authoring-clone creation, backup/recovery,
fixup/review, clean-ODB network gates, local publication, upstream planning,
retirement and 76-patch recovery. This command does not authorize any of them.

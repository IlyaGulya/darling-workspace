# XNU hosted immutable-tag pilot

The XNU perf pilot is described by
`locks/patch-stack/xnu-perf-v1.yml` (schema v2). The downstream canonical
mirror is `darling-next/darling-xnu`; upstream provenance remains
`darlinghq/darling-xnu`.

Its `project.path: .` means exactly the Git top-level supplied through
`--repo`; it is intentionally independent of the caller's current directory.

Its only canonical hosted refs are lightweight tags:

```text
refs/tags/patch-stack/v1/bases/e1db4266f50415c013371fc57e8f38a0423493ec
refs/tags/patch-stack/v1/sources/88dcbf670cd4d1c000dd7f7d95324784bafb0dca
```

The tags are content-addressed: a publisher must fail closed if either exists
at another peeled commit, must never force-update it, and must repeat the same
create-only command to prove no-op idempotency. A repository ruleset must be
active for `refs/tags/patch-stack/v1/**/*` before either canonical tag is
created. It blocks tag updates and deletions without a bypass actor while
leaving initial creation available.

Before canonical publication, create a single sacrificial probe tag in the
covered `probes/` namespace and prove server-side rejection of both update and
delete. A successful update or delete is a stop condition: do not create the
canonical tags.

Consumers use a fresh ordinary Git repository with no alternates and fetch
only the two explicit tag refspecs. They must run `west patch preflight` using
the v2 lock, require `VALID`, confirm the 17 single-parent commits and expected
tree, then generate review artifacts with `format-patch`. This process neither
uses active West remotes nor creates integration/topic refs.

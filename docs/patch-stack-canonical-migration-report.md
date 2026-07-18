# Canonical immutable patch-stack migration inventory

The frozen inventory records 94 patch series across `arch` (18),
`homebrew` (69), and `perf` (7). It is machine-readable in
`locks/patch-stack/migration-inventory-v1.yml` and deliberately excludes
temporary paths and handoff implementation noise.

The unit is an mbox series, rather than a patch file incorrectly treated as
one commit: the inventory records 185 ordered commits, one artifact per
series, the declared `source-base` where present, and the complete ordered
set of `From <OID>` headers. The inventory contract rejects duplicate YAML
keys and verifies exact metadata/artifact correspondence and available-object
linearity.

This is a post-migration snapshot: 13 `READY`, 76 `RECOVERABLE_LOCAL`, and 5 `ALREADY_MIGRATED`
(the XNU and LibreSSL pilots plus the three first-batch
series).
No source was approximated. The recoverable entries have exact metadata in a
trusted worktree but their frozen bundle lacks a standalone clean-object
closure; they are not publication candidates until recovered independently.

The first batch contains three now-`ALREADY_MIGRATED` series, all in
`darling-next/darling`: `ci-host-regression-tests` (three commits from
`ef8429103cfd792e05449eeaf3607622838984b8`), `shellspawn-exit-status`, and
`mldr-glibc-fork-reset`. This is one repository
because it is the only repository with qualifying clean frozen closure; no
less-certain cross-repository stack was substituted merely for diversity.

Repository ruleset `19140134` is active for
`refs/tags/patch-stack/v1/**/*`, without bypass and with update/deletion
blocked. Its sacrificial probe rejected both update and deletion. Each batch
stack has separate content-addressed base/source tags and a separate v2 YAML
lock; no bases or tips were merged artificially.

The existing protected `refs/tags/patch-stack/v1/bases/3d22c6fd6a78c02e49c28f6eb89b1e1f89d9d390`
is a retained noncanonical legacy tag: no lock references it. The canonical
CI base is the create-only
`refs/tags/patch-stack/v1/bases/ef8429103cfd792e05449eeaf3607622838984b8`.

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

This is a post-migration snapshot: 6 `READY`, 76 `RECOVERABLE_LOCAL`, and 12 `ALREADY_MIGRATED`
(the XNU and LibreSSL pilots, the three first-batch series, and batch 2's four
Darling series, Batch 3's two dependent rootless series, and Batch 4's
rootless-prefix-initialization branch series).

Batch 3 proves dependent publication: immutable
`bases/492a00f4929e5aba60607d9fed3e868bc4a3aeba` and
`sources/72006d6a61504c5123b463f8369a8e09bc4b23cf` materialize the first
two-commit series, while that source is also the second series' immutable base
and leads to `sources/e257950104da34ec0646f2faeb5a23e1e80c05d4`. A fresh
combined object database proves the four ordered commits and final tree
`b1eed46329d3d6e1065c58c2d9456d71ea453bbb`.

The rootless-prefix-initialization series is a branch from `38d47a…`, not a
continuation of bootstrapper tip `e257950…`: its canonical source tree is
`beab23c25745954b0eb810a0c3e0b4a20a91dd6f`. Applying its mbox through the
normal Git-am materialization mechanism on effective tip `e257950…` instead
produces reproducible materialized tree `357e507ffb908cb37ac04d38479ebf3fa12f9b28`.
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

Batch 2 uses the existing canonical Darling base
`d014d57080972464a2baabfa299cc6e85041dd0e`. Its source tags are
`sources/c44476a36ee892b73184ddde36b6e9a50fa2d2f6` (sandbox exec, one
commit), `sources/b597d6855c64ee0d9bed23e4d518560170ec1c49` (SDK Homebrew
detection, two commits), `sources/06f98a4481ffcb2467f5ad8f269ed16a6a61571c`
(build drift gate, two commits), and
`sources/acf345173c280ce1b607a0e4a80b9585ab4202d7` (commpage map, one
commit). Each was independently fetched into a clean object database, passed
foreign-CWD preflight and fsck, and produced identical two-run format-patch
output.

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

This is a post-migration snapshot: 0 `READY`, 0 `RECOVERABLE_LOCAL`, and 94 `ALREADY_MIGRATED`
(the XNU and LibreSSL pilots, the three first-batch series, and batch 2's four
Darling series, Batch 3's two dependent rootless series, and Batch 4's
rootless-prefix-initialization branch series, plus Batch 5's remaining six
Darling series, plus the recovered XNU series and 40 recovered Darlingserver
series, plus the final seven external recovery series). All 94 frozen patch
series now have standalone hosted immutable object closure.

Darlingserver repository-scoped recovery moved all 40 of its formerly
`RECOVERABLE_LOCAL` series to `ALREADY_MIGRATED`. The exact mbox chains include
the 40-commit `a0-arch-redesign` series without collapse, 11 other `arch`
series, 25 `homebrew` series, and three `perf` series. Exact base/source,
ordered-parent topology, trees, and author/committer metadata were audited
read-only from the active Darlingserver object database, then copied without
replacement commits or alternates into an isolated bare repository. Every
series passed a separate clean-ODB preflight, fsck and deterministic two-run
`format-patch` check first against the isolated closure and then against the
hosted-only canonical tags.

Darlingserver ruleset `19165720` is active, has no bypass actors, covers
`refs/tags/patch-stack/v1/**/*`, and blocks both update and deletion. Its
sacrificial probe rejected both operations with `GH013`; 80 create-only
base/source refs (40 unique bases and 40 unique sources) were then published
and a repeat create-only push was idempotent. Forty schema-v2 YAML locks now
record the individual series.

The final external recovery batch covers libunwind, libplatform, Perl,
libpthread, Installer and dyld. All seven series were recovered from exact
trusted-worktree objects into six separate isolated bare repositories. Six
new active no-bypass tag rulesets protect the canonical namespace: libunwind
`19166835`, libplatform `19166838`, Perl `19166841`, libpthread `19166843`,
Installer `19166845`, and dyld `19166846`. Each repository's single
sacrificial probe rejected update and deletion with `GH013`. Fourteen
create-only base/source tags and seven v2 locks then passed hosted-only clean
ODB preflight, fsck and deterministic `format-patch` verification. Installer
explicitly preserves `88764e… → 25b9db… → 6d1ca4… → 0cd5a2… → 825fde…`, with
`25b9db…` published in both base and source namespaces.

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
No source was approximated. All series that were formerly recoverable have
now been restored from exact trusted-worktree objects and have a standalone
hosted immutable clean-ODB closure.

Batch 5 publishes the remaining six verified Darling series: two-commit
`mldr-stack-mmap-fallback` (`f018846…` to `1618c856…`), one-commit
`mldr-thread-create-futex-wait` (`73a11f…` to `dd6b42…`), one-commit
`mldr-recv-adaptive-spin` (`67f40c…` to `3fd7a8…`), one-commit
`rootless-shellspawn-delay` (`88a1b9…` to `f8aa74…`), two-commit
`rootless-shellspawn-lifecycle` (`f8aa74…` to `0d817c…`), and one-commit
`mldr-compact-fd-band` (`50b2e0…` to `93ba45…`). Each has distinct immutable
base/source tags and a v2 lock, then passed foreign-CWD preflight, fsck, and
two byte-identical `format-patch` exports in a fresh object database.

The shellspawn dependency is deliberately limited to
`88a1b9… → f8aa74… → ce8062… → 0d817c…`: `f8aa74…` is both delay's source and
lifecycle's base. `72006d…` is a different child of `88a1b9…`, outside this
chain; it must not be described as a Batch 3 continuation.

The XNU repository-scoped recovery copied exact objects for all 29 formerly
recoverable `darling-next/darling-xnu` series from the trusted XNU worktree to
an isolated bare object database without alternates or replacement commits.
Every base/source/ordered commit, tree and author/committer record was checked
before publication; each series then passed a separate fresh-clone preflight,
fsck and two byte-identical `format-patch` exports. The active no-bypass XNU
tag ruleset `19137295` was retained unchanged. The topology includes the
three-series RPC chain, the four-node E-UNION path, the four-node perf path,
and twelve independent branch tips from `5f26a4…`; `j7e7-lane-wakefd-sentinel`
starts at existing pilot source `88dcbf…` but does not alter the pilot.

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

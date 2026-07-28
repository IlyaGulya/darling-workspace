# Legacy runtime retirement audit

This audit records the operational boundary after canonicalization. Historical
patch archives remain versioned review/provenance/recovery data, but no
production or manual acceptance path executes them.

## Canonical inventory and order

| Profile | Stack | Own grouped module order | Own series |
| --- | --- | --- | ---: |
| homebrew | homebrew | darlingserver, xnu, libplatform, perl, libressl-2.8.3, libpthread, darling, installer | 69 |
| perf | homebrew → perf | darling, xnu, dyld, darlingserver | 7 |
| arch | homebrew → perf → arch | libunwind, xnu, darlingserver, darling | 19 |

The canonical grouped order is module insertion order and then profile order
within each module. Ancestry is checked only within a repository. Every one of
the 95 own-profile series has a schema-v2 immutable lock and a bound
schema-v3 composition boundary.

## Materialization call sites

| Consumer | Archive input? | Authority |
| --- | --- | --- |
| `west patch apply` | no | typed lock-first plan |
| RuntimeSourceMaterializer profile checkout | no | same typed batch primitive |
| RuntimeSourceMaterializer current-minus | no | typed locks with explicit omission |
| regular host `--materialize-profile` | no | runtime-source canonical |
| manual homebrew oracle | no | independent immutable cherry-pick |
| manual Arch acceptance | no | default composed lock-first |
| `west patch verify` applicability | no | immutable lock replay |
| `west patch status` | no | profile-composition trees |
| `west patch export-locks` | no | immutable format-patch |

The public legacy and shadow switches, the shadow workflow, the legacy oracle,
and archive-application helpers have zero callers and are removed. Invalid
typed metadata cannot silently choose another path.

`--roll-back` remains temporarily as a deprecated parser-only compatibility
alias because the immutable `darling/build-drift-gate` diagnostic still prints
that spelling. Its value is not passed to canonical orchestration: canonical
apply always performs fail-closed rollback. Removing the alias requires a
separate reviewed restack of that immutable diagnostic commit.

## Typed non-executable archive allowlist

`locks/patch-stack/archive-consumers-v1.yml` lists every retained archive
consumer, classifies the registry as
`NON_EXECUTABLE_ARCHIVE_PROVENANCE_RECOVERY`, gives each consumer an exact
`access: read|write`, and marks `executes_archive: false`. The two write
capabilities are the explicit authoring exporters; they refresh archives and
profile metadata but never execute an archive:

- checksum/provenance and source-export drift in `west_commands/patch.py`;
- PR dashboard/publish checks and authoring refresh;
- guest Mach-O owning-patch checksum/source-provenance contracts;
- one non-reading lock-first rollback fixture path;
- the perf hidden-blob forensic classification;
- migration inventory/checksum coverage.

Generated test fixtures may exercise native Git mechanics, but are not reads
of versioned profile archives.

## Perf forensic exception

`patches/perf/xnu/shmem-ring-guest.patch` references missing blob
`024ca655535df09af16e052c927001ac50484aff`. The blob is absent from the
declared canonical base/source closure and was observed only in a contaminated
historical active ODB.

Classification:
`LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE`.

The blob is not copied or published. Perf authority is dual clean-ODB
canonical verification: independent immutable cherry-pick against production
immutable format-patch/git-am. Archive checksum/provenance remains review
evidence, not applicability evidence.

## Resolved Arch topology

The original A0 Arch lock was valid on its declared standalone base but
conflicted with the actual preceding profile state. Reviewed versioned
profile-integration locks preserve the approved typed-wake/MicroState,
shellspawn, and downstream semantics. The current Arch mapping has 19 entries
and passed composed 69/7/19 local and hosted acceptance.

The historical conflict bundles remain provenance. They do not authorize
automatic conflict-side selection and are not runtime inputs.

## Recovery and deletion

Recovery mboxes are regenerated from immutable locks with
`west patch export-locks`. The exporter records ordered OIDs, resulting trees,
stable patch IDs, and SHA-256 and removes its object-bearing transaction before
publishing output.

Runtime fallback code can be removed independently of archive data. Deleting
or externally archiving the versioned `.patch` files remains a separate
retention decision for upstream review, provenance, and forensic owners.

# Legacy mbox operational retirement

Historical `patches/**/*.patch` files are provenance, review, and recovery
records. They are not executable materialization inputs. Canonical profile
state is the typed schema-v2 immutable lock graph plus its schema-v3 profile
composition.

## Production and CI call-site inventory

| Entry point | Profile/mode | Class | Materialization source |
| --- | --- | --- | --- |
| `west patch apply --profile homebrew` | `default-lock-first` | normal CLI | Batch 9, 74 immutable series |
| `west patch apply --profile perf` | `default-lock-first` | normal CLI | homebrew prerequisite plus perf 7 |
| `west patch apply --profile arch` | `default-lock-first` | normal CLI/manual Arch tier | homebrew 74, perf 7, arch 19 |
| `west patch apply ... --lock-first` | `explicit-lock-first` | compatibility alias | same typed plan as no-flag |
| `west test --profile homebrew --materialize-profile` | runtime-source canonical | regular host CI | Batch 9 in lifecycle-owned worktrees |
| `RuntimeSourceMaterializer.profile_worktree_checkout()` | runtime-source canonical | host/runtime tests | typed homebrew/perf/arch stack |
| runtime-source current-minus RED proof | canonical-minus-one | tests | immutable locks with a typed omission |
| manual `patch-stack-lock-first.yml` control | `immutable-cherry-pick-oracle` | manual oracle | declared immutable refs in fresh ODBs |
| manual `patch-stack-lock-first.yml` candidate | `default-lock-first` | manual acceptance | production native format-patch/git-am replay |
| manual `test-infra.yml` Arch tier | `default-lock-first` | manual acceptance | composed 74/7/19 canonical stack |
| guest-smoke/toolchain/full | no profile apply | regular CI | consumes built runtime; no archive apply |

`--legacy-mbox`, `--shadow-lock`, `--shadow-evidence`, the shadow workflow,
and their runtime implementations have zero callers and are removed in this
review. A corrupt or incomplete mapping fails before production worktree
mutation and cannot select an archive fallback.

The immutable `darling/build-drift-gate` diagnostic still prints
`--roll-back`. The CLI therefore retains that spelling as a deprecated
parser-only compatibility no-op until a separate reviewed restack updates the
diagnostic. Canonical apply does not branch on the value and always rolls back
fail-closed.

`west patch verify --applicability-only` now fetches and replays the typed
immutable locks in disposable clean worktrees. `west patch status` validates
the `integration/<profile>` trees against the typed composition. Neither
operation executes an archive. `west patch export-locks` creates review or
recovery mboxes with native `git format-patch` directly from validated
immutable objects.

## Exact canonical inventory

| Profile | Batch | Own series | Grouped module order |
| --- | --- | ---: | --- |
| homebrew | `darling-homebrew-eunion-sidecar-batch-10` | 74 | darlingserver, xnu, libplatform, perl, libressl-2.8.3, libpthread, darling, installer |
| perf | `darling-perf-lock-first-batch-1` | 7 | darling, xnu, dyld, darlingserver |
| arch | `darling-arch-lock-first-batch-1` | 19 | libunwind, xnu, darlingserver, darling |

The immutable oracle independently validates every declared base/source ref,
exact linear ordered commit list, no-merge topology, complete author and
committer metadata, canonical tree, and final module composition. For parent
repositories it independently derives integration-only gitlink OIDs, while
requiring exact non-gitlink content and exact managed-child publication. It
performs one union fetch per module, uses no active ODB, alternate, partial
clone, shallow clone, replace ref, cache, or archive, and publishes evidence
only after its disposable roots are removed.

The hosted comparator requires the oracle and production candidate to agree on
module trees, frozen manifest, ordered generated-lock hashes, typed batch
identity, and canonical evidence. The implementations are deliberately
different: plain Git cherry-pick versus production format-patch/git-am.

## Archive consumers

`locks/patch-stack/archive-consumers-v1.yml` is the exact typed allowlist.
Its classification is `NON_EXECUTABLE_ARCHIVE_PROVENANCE_RECOVERY`; each
entry declares `access: read|write` separately from the invariant
`executes_archive: false`. It includes:

- `west_commands/patch.py` reads archives for checksum, provenance-quality,
  and source-export drift only;
- `west patch export` and `scripts/export_patches.py` have typed write access
  and refresh versioned review artifacts/profile metadata from reviewed source
  branches;
- PR tooling reads checksums for review/publish drift;
- guest Mach-O metadata/build contracts bind reviewed sources to owning
  archive checksums without compiling or applying archive payload;
- focused lock-first rollback uses one owning archive path as a non-reading
  fixture;
- the perf hidden-blob forensic contract reads the affected index stanza;
- the migration inventory verifies archive census and checksums.

None of these consumers materializes a profile. The perf
`shmem-ring-guest.patch` remains classified
`LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE`; its missing historical blob is
not imported into an immutable closure.

The canonical exporter records, per series, the module/patch identity,
base/source OIDs, complete ordered commit list, count, resulting tree, mbox
SHA-256, and stable patch IDs. Two independent clean-ODB exports must be
byte-identical. Export output contains no `.git`, objects, packs, alternates,
or bundles.

## Deletion and recovery boundary

Runtime fallback and shadow code can be deleted now; archive data cannot.
Archives remain useful review/provenance artifacts and emergency-readable
records. Recovery no longer applies them: regenerate an mbox from immutable
locks with:

```text
west patch export-locks --profile <homebrew|perf|arch> --output <new-directory>
```

The output path must not exist. The object-bearing transaction is removed
before the provenance-only directory is published. A recovery rehearsal must
verify `evidence.json` and then may apply the generated mbox in an independent
repository.

Future archive deletion is a separate data-retention decision. It requires
owners of upstream review, export provenance, and forensic evidence; it is not
part of runtime retirement.

## Bead/readiness decision

- `dar-umoc.1` (manual legacy oracle migration): closable after this code lands
  and ordinary CI proves the immutable oracle contract.
- `dar-umoc.2` (remaining legacy-first profiles): closable after the same CI;
  homebrew/perf/arch are all typed canonical.
- `dar-umoc.3` (archive retention/export/recovery): the runtime blocker is
  resolved by canonical export; archive deletion itself remains a separate
  retention task, not a feature-work dependency.

Verdict: **READY_FOR_DARLING_FEATURE_WORK**, subject to landing review and one
ordinary host/guest-smoke CI. A hosted run is optional follow-up evidence, not
required to establish that archives are no longer executable inputs.

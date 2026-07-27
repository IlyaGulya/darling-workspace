# Legacy runtime retirement audit

This audit is the source plan for retiring archive-backed patch application
from production materialization. It records consumers, not a deletion: patch
archives remain versioned review, export, provenance, and recovery artifacts.

The machine-readable profile/lock census is
[`legacy-runtime-profile-inventory-v1.json`](../locks/patch-stack/legacy-runtime-profile-inventory-v1.json).
It derives its closure statement from the frozen migration inventory and
canonical migration report. The three production profiles have no missing or
duplicate source-tip lock: homebrew has 69 series / 97 ordered commits, perf
has 7 / 29, and arch has 18 / 59. All 94 series have schema-v2 immutable-lock
closure and prior hosted clean-ODB evidence.

## Actual profile order

| Profile | Stack | Own grouped module order | Own series / commits |
| --- | --- | --- | --- |
| homebrew | homebrew | darlingserver, xnu, libplatform, Perl, LibreSSL, libpthread, darling, Installer | 69 / 97 |
| perf | homebrew -> perf | darling, xnu, dyld, darlingserver | 7 / 29 |
| arch | homebrew -> perf -> arch | libunwind, xnu, darlingserver, darling | 18 / 59 |

The canonical grouped execution order is module insertion order followed by
profile order within each module. For a stacked runtime-source forest this is
the grouped order of the complete profile stack; ancestry is checked only
within a module repository, never across repositories.

## Consumer classification

| Consumer | Class | Runtime archive input? | Retirement action |
| --- | --- | --- | --- |
| `west_commands/patch.py` `patch apply` | production materialization | historically yes | use typed immutable mapping for all profiles; later remove public legacy switch |
| `RuntimeSourceMaterializer.profile_worktree_checkout` | regular host runtime source materialization | historically yes | use typed immutable mapping for all profile stacks |
| `ci/run-test-tier.sh --materialize-profile` | regular host CI entry point | indirect via runtime source | policy-contract canonical profile route |
| `patch-stack-lock-first.yml` control | manual oracle | yes, intentionally | replace with test-only legacy oracle |
| `patch-stack-shadow.yml` / `patch_stack_shadow` | manual diagnostic comparison | yes, intentionally | move behind test-only oracle boundary |
| `west patch verify` | provenance/checksum/applicability | yes | retain; not production materialization |
| `west patch export` / `scripts/export_patches.py` | review/upstream export | yes | retain; future archive strategy owns it |
| test fixtures and contracts | test-only | sometimes | keep explicit allowlist only |
| recovery documentation and bundles | emergency recovery | yes | retain pending archive strategy |

The migration report establishes that immutable refs, schema-v2 locks, ordered
commits and expected trees are the canonical production state. Archive mbox
files are not canonical runtime inputs after this transition.

## Required retirement boundary

Production code may generate a temporary mbox only from a declared immutable
commit using native `format-patch` followed by native `git am`; it may not read
`patches/<profile>/*.patch` to materialize a production profile. Archive-based
`git am` remains limited to the future test-only legacy oracle and the
explicit verify/export/recovery allowlist.

The future removal remains deliberately split:

1. remove the public runtime legacy switch and runtime archive apply path after
   canonical differential and rollback acceptance; and
2. separately decide archive retention, deterministic regeneration, or external
   storage after review/export/provenance/recovery owners approve.

## Perf archive forensic exception

`patches/perf/xnu/shmem-ring-guest.patch` is retained as a review and
provenance artifact, but it is **not** a portable materialization authority.
Its declared canonical record is
[`xnu-perf-v1.yml`](../locks/patch-stack/xnu-perf-v1.yml): base
`e1db4266f50415c013371fc57e8f38a0423493ec`, source/tip
`88dcbf670cd4d1c000dd7f7d95324784bafb0dca`, seventeen ordered commits, and
expected tree `c0b2c145f7f26734853657b165da88cc51ec7f46`.

The archive's D13 hunk for
`darling/src/libsystem_kernel/emulation/src/linux_premigration/vchroot_userspace.c`
contains this preimage index:

```text
024ca655535df09af16e052c927001ac50484aff..dfcb460aa664fc3df2e0eff2d398e05d9e8e3e10
```

Native `git am --3way` against the exact declared base in a fresh upstream and
immutable-ref clean ODB fails because blob
`024ca655535df09af16e052c927001ac50484aff` is absent. It is reachable only
from the historical active West XNU ODB, not from the declared base or source
refs. It must never be copied, published, or used for materialization.

Classification: `LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE`.

Perf differential acceptance therefore uses two independent clean-ODB
canonical mechanisms: a test-only `immutable-cherry-pick-oracle` that fetches
only declared immutable refs and replays the lock's ordered commits with plain
Git, and the production `default-lock-first` native format-patch/git-am batch
replay. Their trees, order, metadata, expected trees and cleanup state must
match. This exception is typed per profile; it does not silently change the
homebrew legacy oracle or any other profile. `west patch verify` continues to
check archive checksum/provenance, but must not claim clean-ODB applicability
for this archive.

## Arch integration topology exception

The immutable series
[`darlingserver-a0-arch-redesign-v1.yml`](../locks/patch-stack/darlingserver-a0-arch-redesign-v1.yml)
is internally sound: in a fresh clean ODB, both plain `git cherry-pick` and
native `format-patch | git am --3way` replay all forty ordered commits from
declared base `f4530b5f635672a815eff0f344ec7c91bc1ec12b` to its expected tree
`d415bb9218bd066f36bc54d6d6fb4abb5508dfc5`.

It is not compatible with the actual preceding arch integration state
`ed0a3771ceff8389070e925532b7277f9cf651de`: both mechanisms conflict at
ordinal 3, `80e8f944c0148e93f2b6f0a1501b8de7e8a3aa47`, in `src/thread.cpp`.
The archived `a0-arch-redesign.patch` has the same third-patch stable patch-id
and fails identically on that state, so it does not contain an additional
historical conflict resolution. This is a profile-integration topology
conflict, not a format-patch/git-am, identity, empty-commit, lock, or
immutable-object-closure defect.

The safe remediation is a separately reviewed canonical integration series
based on the exact preceding canonical arch tree, retaining original
author/source provenance and carrying new immutable refs, ordered commits,
expected tree, and a versioned lock. No automatic conflict-side choice is
authorized.

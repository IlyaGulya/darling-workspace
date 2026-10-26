# Rootless opt-in

This document specifies the low-level product launcher contract, not a recipe
to bypass managed lifecycle ownership. Rootless is the normal product default.
For managed preparation, real guest toolchains, workloads and diagnostics use
the [test workflow](test-infra.md#bootstrap-a-fresh-prefix-before-a-prefix-backed-run)
and the canonical boot/diagnostics skills. Execute long managed runs through
`scripts/west-job.sh start` / `follow`.

The ordinary immutable installation at absolute `R` contains `R/bin/darling`,
`R/bin/darlingserver` and `R/libexec/darling`. Separate, disjoint writable guest
state at `P` is selected by `DPREFIX`, not by the launcher's pathname:

```sh
DPREFIX="$P" "$R/bin/darling" shell /bin/bash
```

The normal launcher initializes first-use guest state. Do not discover a
launcher in `P/bin` or copy system libraries into `P`. Configure absolute
`CMAKE_INSTALL_PREFIX=R`, retain default `COMPONENTS=stock`, build ALL and
perform the complete normal CMake install. Publish a fresh configured root
for changed code; never rewrite a published image.

The normal build enables `DARLING_EUNION`; a deliberately E-UNION-disabled
build rejects E-UNION modes before touching guest state. No `--rootless`
option, legacy environment triplet or setuid adjustment is required for the
production default. Exact `--rootless` remains an explicit selection of the
same `rootless-eunion` contract.

The launcher alone accepts historical `DARLING_ROOTLESS`,
`DARLING_NOOVERLAYFS` and `DARLING_EUNION` compatibility inputs. Supplied
values must be exactly `0` or `1`; invalid/incomplete rootless or conflicting
combinations fail. It normalizes selection to `DARLING_RUNTIME_MODE` and
removes the legacy variables before starting downstream components. New
instructions must not depend on those compatibility inputs.

Launcher long options are exact. In particular, `--root` is rejected rather
than treated as an abbreviation for `--rootless`; mode selection and command
dispatch consume the same single parsed result.

The four real runtime modes are `privileged-overlay`, `privileged-copy`,
`privileged-eunion`, and `rootless-eunion`. E-UNION modes additionally require
an E-UNION-capable build; mode selection never substitutes copy or overlay
after that check.

Rootless mode does not create privileged mount/PID namespaces. It assembles
the installed lower image with the writable upper and initializes missing
guest state through the normal launcher. Every new prefix receives the strict
schema-v2 `.darling-prefix-state-v2` record. It binds schema version, runtime
mode, monotonic generation, prefix device/inode, owner identity, and
provenance. The one supported compatibility transition upgrades an otherwise
valid `.darling-runtime-mode-v1` prefix in place. A malformed, newer,
cross-prefix, multiply-linked, symlinked, wrongly owned, or mode-incompatible
state is rejected before mutation.

The launcher represents the post-anchor prefix as an assignment-resistant
owning capability with runtime typestate. Its zero-overhead one-element-array C
type rejects ordinary direct copy-initialization/assignment, while the runtime
state remains authoritative: reopening an owned handle fails without discarding
its descriptors, the original path is discarded, and a moved-from capability
is unusable. This is not an absolute compile-time linearity guarantee. Stable
prefix state (`missing`, `empty`, `legacy-v1`, or `current-v2`) and transaction
intent are separate tagged variants. The
transaction journal persists its exact operation and phase; recovery defines
and tests every operation × journal-phase × stable-state combination, rejecting
impossible combinations rather than inferring a choice from independent
booleans. Lifecycle actions are create, reuse, upgrade/repair, recreate, and
delete.

Each product mutation holds the persistent `P/.lifecycle.lock` `flock`,
revalidates that the locked inode is still the named lock, writes and fsyncs its journal, fsyncs every
staged regular file and the staged directory tree bottom-up, atomically
publishes with `renameat2`, fsyncs the containing directory, and only then
cleans old state. The lock file is never unlinked by lifecycle operations, so a
waiter cannot remain queued on an obsolete inode while a newcomer locks a
replacement. A three-process behavioral contract exercises that exact race.
SIGINT or failure at an early, middle, or late phase therefore leaves either
the previous valid prefix or the new valid generation. The behavioral suite
executes 24 interruption cases: early/middle/late failure and SIGINT for
create, upgrade, recreate, and delete; the separate 144-case decision-table
test exhausts all operation × journal-phase × stable-state inputs. Typestate
does not replace fd-relative revalidation, locking, fsync, or atomic
publication. Transaction files, stage trees, state temporaries, workdirs,
sockets, and process metadata are lifecycle-owned and removed or recovered
idempotently.

Workspace orchestration holds a separate external per-prefix sibling lease
through the whole run lifecycle, including first-use initialization and
cleanup. It is not product prefix content and is not `P/.lifecycle.lock`.
Never delete the lease file: waiters must keep locking the same inode. Unknown
content without recognized state remains an error, not permission to erase it.

`P` is the writable upper; `R/libexec/darling` is the installed immutable
lower, not a subtree of `P`. A successful managed run leaves the installed
image unchanged and removes its init PID, sockets, owned processes and runtime
mounts after prefix-scoped product shutdown:

```sh
DPREFIX="$P" "$R/bin/darling" shutdown
```

An already copied or COW-mounted inert clone is bound through the product:
`DPREFIX=DEST R/bin/darling bind-clone BASE`. This operation publishes clone
lifecycle identity; it neither copies data nor boots. Workspace code must not
rewrite the lifecycle record. Use supported workspace clone/lane owners to
coordinate leases and staging.

The launcher is ROOTLESS BY DEFAULT and needs no setuid bit; the privileged launcher is retired and is
refused unless `DARLING_FORCE_ROOTFULL_MODE=1` asks for it, so a setuid message can only mean a privileged
mode was requested explicitly. On a
rootless invocation it permanently changes its real, effective, and saved user
and group IDs to the non-root invoking identity before inspecting or writing
the prefix. Darlingserver independently verifies that credential boundary
before any runtime-state mutation and never falls back to privileged startup.
The prefix itself must be a real directory. The launcher walks every component
once with `fstatat(AT_SYMLINK_NOFOLLOW)` and `openat(O_NOFOLLOW)`, retains the
verified directory and parent descriptors, and creates missing directories
through create-only temporary names plus `renameat2(RENAME_NOREPLACE)`.
Darlingserver receives those descriptors across `exec`, verifies that the
parent/leaf and sibling workdir still name the exact retained inodes, and never
reopens the original prefix spelling. All subsequent prefix state is mutated
relative to retained descriptors with the `*at` APIs. The few Linux interfaces
without an fd-relative form (`mount`, Unix-socket `bind`, and `vchroot`) receive
stable `/proc/self/fd/N` aliases backed by the retained descriptors.

Leaf and intermediate symlinks, post-inspection rename/symlink swaps, staged
workdir replacements, and marker symlinks therefore fail closed without
writing through to an attacker-selected target. Init PID publication is atomic,
state cleanup and log creation are fd-relative, and lifecycle failures remove
their unpublished temporary files/directories.

Without a mode override the launcher selects `rootless-eunion`; there is no
implicit WSL1 privileged fallback. Retired privileged modes are refused unless
`DARLING_FORCE_ROOTFULL_MODE=1` explicitly opens that gate. They are not an
ordinary setup or repair path. Launcher options must precede `shell`, `exec`,
`shutdown`, `bind-clone` or the program-path command.

Preserve the caller's raw `DPREFIX` spelling: do not normalize away `..` or
symlinks before product validation. Prefix selection, runtime mode validation,
retained descriptors and lifecycle ownership are complementary safety checks.

Preserve immutable source identity when admitting prefix-lifecycle changes.
Propagate changed module boundaries through dependent composition locks and
verify proposed bases/sources in independent clean object databases before
considering hosted publication.

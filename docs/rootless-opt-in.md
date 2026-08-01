# Rootless opt-in

The supported user-facing rootless launcher mode is:

```sh
DPREFIX=/path/to/a/disposable-prefix darling --rootless shell
```

The option is available only in a build configured with
`-DDARLING_EUNION=ON`; the normal default build keeps that capability off and
rejects E-UNION modes before touching a prefix. It selects the typed
`rootless-eunion` runtime contract for that launcher invocation. The launcher
is the only compatibility boundary: it
accepts the historical `DARLING_ROOTLESS`, `DARLING_NOOVERLAYFS`, and
`DARLING_EUNION` triplet only when every supplied value is exactly `0` or `1`,
rejects incomplete or conflicting combinations, and normalizes the result to
one `DARLING_RUNTIME_MODE` value. The legacy variables are removed before
darlingserver, mldr, launchd, or shellspawn runs, so downstream components
cannot independently reinterpret the mode.

Launcher long options are exact. In particular, `--root` is rejected rather
than treated as an abbreviation for `--rootless`; mode selection and command
dispatch consume the same single parsed result.

The four real runtime modes are `privileged-overlay`, `privileged-copy`,
`privileged-eunion`, and `rootless-eunion`. E-UNION modes additionally require
an E-UNION-capable build; mode selection never substitutes copy or overlay
after that check.

Rootless mode does not create privileged mount/PID namespaces and requires a
prefix prepared for E-UNION operation. Every new prefix receives the strict
schema-v3 `.darling-prefix-state-v3` record. It binds schema version, runtime
mode, monotonic generation, prefix and sidecar device/inode identities, owner
identity, and provenance. Runtime deployment accepts only that current typed
state; it never treats the legacy `.darling-runtime-mode-v1` marker as a
rootless fallback. A malformed, newer, cross-prefix, multiply-linked,
symlinked, wrongly owned, or mode-incompatible state is rejected before
mutation.

Normal startup of an already-current typed prefix retains a shared read-only
lifecycle lease, so concurrent commands and a verified restart do not wait on
another reader. Only creation, recovery, explicit recreation, and deletion
request the exclusive writer lease. Both modes use bounded acquisition;
deadline failures report the requested mode and exact lock inode instead of
hanging behind a surviving holder.

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

Each mutation holds a per-prefix `flock`, revalidates that the locked inode is
still the persistent named lock, writes and fsyncs its journal, fsyncs every
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

During `west test` deployment, the lifecycle-owned `.west-test.lock` does not
count as prefix content. Any other content without a recognized state remains
an error. A newly runner-owned prefix remains byte-empty through source
hydration, profile materialization, CMake configure, and Ninja build. Initial
cleanup is observational for that empty directory; immediately before
deployment the newly built launcher is the first writer and atomically
publishes the current typed state. The writable prefix is the upper layer; its
`libexec/darling` subtree is the immutable lower template. A successful run
must leave the lower template unchanged and must remove its init PID, Unix
sockets, child processes, and other runtime state after `darling --rootless
shutdown`.

The installed launcher remains setuid for the existing privileged modes. On a
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

Without `--rootless` or a compatibility override, the launcher selects
`privileged-overlay` (or the existing WSL1 `privileged-copy` fallback), retains
the privileged startup contract, and does not enter rootless startup. The
option must precede the `shell`, `exec`, `shutdown`, or program-path command.

The canonical publication proposal is append-only. Homebrew Batch 9 contains
74 ordered entries and appends one prefix-lifecycle series each for
Darlingserver and Darling after the Batch 8 typed-mode sources. Its changed
module boundaries are explicitly propagated through the seven-entry Perf and
nineteen-entry Arch composition locks; existing immutable sources are never
rewritten. All proposed new bases/sources are verified in independent local
clean ODBs before any hosted publication is considered.

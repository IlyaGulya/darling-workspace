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
prefix prepared for E-UNION operation. Every new prefix receives a versioned
`.darling-runtime-mode-v1` marker. An existing prefix with a missing or
different marker is rejected without migration or partial runtime setup; use a
fresh disposable prefix instead. During `west test` deployment, the
lifecycle-owned `.west-test.lock` does not count as prefix content: the mode
marker is still installed transactionally as the first product entry, and is
rolled back with the deployment on failure. Any other content without the
marker remains an error. The writable prefix is the upper layer; its
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

The canonical publication proposal is append-only. Homebrew Batch 8 contains
72 ordered entries and introduces one typed-mode series each for
Darlingserver and Darling plus the XNU AF_UNIX length series. Its changed
module boundaries are explicitly propagated through the seven-entry Perf and
nineteen-entry Arch composition locks; existing immutable sources are never
rewritten. All proposed new bases/sources are verified in independent local
clean ODBs before any hosted publication is considered.

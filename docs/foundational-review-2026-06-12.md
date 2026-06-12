# Darling Homebrew fixes: foundational review

Date: 2026-06-12

## Decision

The current 20-patch Homebrew profile is a useful local integration profile,
but it is not a set of 20 publication-ready root-cause fixes.

The review assigns:

- 3 `ready` fixes: narrow defects with the correction at the owning ABI/build
  boundary and adequate evidence;
- 6 `provisional` fixes: root-cause-directed changes whose concurrency,
  lifecycle, or metadata invariants still need direct tests or a supported
  design;
- 11 `blocked` fixes: unsafe implementations, private cross-layer protocols,
  defensive bounds, or success-returning compatibility stubs.

`west pr check` now enforces these decisions. Blocked fixes cannot be staged.
Provisional fixes may use a fork-local draft for review, but cannot be
published upstream. The patch profile remains usable locally.

## Blocking findings

### Memory safety in getattrlist

`dar-q95.17` contains an inverted short-buffer condition in
`getattrlist_generic.c`: when `FSOPT_REPORT_FULLSIZE` is not set and the caller
buffer is too small, it increases the local `bufferSize` to `spaceNeeded` and
then copies that amount to the caller. This can write past the supplied buffer.

The same encoder reserves eight bytes for `ATTR_FILE_RSRCLENGTH` but writes and
advances only four, masks `stat` and `readlink` failures as successful
attributes, truncates fd-derived names, and duplicates Darwin constants and
layouts locally. `dar-q95.18` adds a second independent encoder with different
semantics, fake HFS tags, silent per-entry failures, and unchecked resume
seeks.

Disposition: both branches are blocked. Replace them with one shared,
bounds-checked attribute encoder under `dar-q95.29.1`.

### Private psynch error ABI

`dar-q95.19` returns BSD psynch errors as negative values directly in the
syscall return register, and `dar-q95.16` teaches the imported libpthread to
understand that Darling-only convention. This repairs the observed symptom by
changing both sides of a private protocol instead of making the Darwin syscall
boundary conformant.

The mutex path retries every negative value, not only `EINTR`. A permanent
error can therefore become an infinite loop.

Disposition: both branches are blocked. Define one Darwin-compatible
syscall/cerror contract under `dar-q95.29.2`.

### Unbounded fd_set conversion

`dar-q95.3` correctly identifies the 32-bit Darwin versus native-word Linux
`fd_set` layout mismatch and fixes it in the syscall mediation layer. However,
caller-controlled `nfds` directly determines three stack allocations and all
source/destination accesses. There is no Darwin `FD_SETSIZE` or allocation
bound.

Disposition: correct architecture, unsafe implementation. Blocked pending
`dar-q95.29.3`.

### Host fork recovery depends on private glibc memory

The generic post-fork `elf_calls` hook in `dar-q95.12` is a reasonable
extension point. The implementation in `dar-q95.14`, however, scans
`_rtld_global` for byte signatures of private `pthread_mutex_t` and list
layouts, then mutates those objects in the child. It assumes offsets, a 16 KiB
scan window, mutex representation, list state, and a unique three-mutex
signature. Detection failure is silent.

Disposition: the hook is provisional; the current glibc reset is blocked.
Design a supported or explicitly versioned host-runtime boundary under
`dar-q95.29.9`.

### Success-returning stubs are not root fixes

These changes deliberately make an unsupported operation appear successful:

- `dar-q95.10`: Darwin thread/process priority calls are no-ops and
  `getpriority` does not reflect `setpriority`;
- `dar-q95.11`: `SIOCGIFCONF` reports an empty interface list;
- `dar-q95.21`: `sandbox-exec` discards the policy and executes without
  isolation;
- `dar-q95.5`: Perl is reconfigured around the missing
  `_NSGetExecutablePath` API.

They may remain in a clearly identified local compatibility profile, but they
must not be presented as foundational fixes. Replacement work is grouped
under `dar-q95.29.4`.

### Fork timeout bounds damage, not cause

`dar-q95.13` replaces an unbounded fork-child checkin wait with a hardcoded
30-second timeout. It avoids a permanent hang, but it neither explains nor
repairs a missing checkin and collapses interrupted, timed-out, and internal
failure into the same caller result.

Disposition: local defensive guard only. Replace with an explicit fork
transaction/liveness protocol under `dar-q95.29.7`.

## Per-fix classification

| Bead | Change | Review | Reason |
| --- | --- | --- | --- |
| `dar-q95.1` | balanced cvwait sequence | provisional | semantic correction needs direct generation-state matrices |
| `dar-q95.8` | microthread resume permit | provisional | correct race window, but a generationless permit needs wake-epoch/state-machine proof |
| `dar-q95.13` | fork checkin timeout | blocked | damage bound, not lifecycle repair |
| `dar-q95.2` | cvsignal/cvbroad argument ABI | ready | concrete width and argument-position defects at the ABI adapter |
| `dar-q95.3` | select/pselect fd_set conversion | blocked | unbounded stack allocation and buffer access |
| `dar-q95.10` | Darwin priorities | blocked | success-returning no-op |
| `dar-q95.11` | SIOCGIFCONF | blocked | explicit empty-success stub |
| `dar-q95.12` | post-fork elfcalls hook | provisional | sound extension point, but currently serves a blocked implementation |
| `dar-q95.4` | bzero return register | ready | narrow x86 ABI compatibility defect with regression coverage |
| `dar-q95.5` | disable NSGetExecutablePath in Perl | blocked | package workaround for a missing platform API |
| `dar-q95.6` | LibreSSL strict aliasing | ready | correct compiler contract for legacy aliasing code with P-curve regressions |
| `dar-q95.15` | cancel stale wait timer | provisional | matches XNU `thread_unblock`; needs direct race/counter tests |
| `dar-q95.16` | libpthread negative psynch returns | blocked | private ABI plus retry-all-negative infinite-loop risk |
| `dar-q95.17` | getattrlist name/object type | blocked | caller-buffer overflow and incomplete error semantics |
| `dar-q95.18` | getattrlistbulk | blocked | duplicated non-conformant attribute subsystem |
| `dar-q95.19` | negative psynch errno | blocked | wrong layer for Darwin syscall error behavior |
| `dar-q95.20` | sigexc SA_RESTART | provisional | correct mediation layer; handler-state invariants and conformance tests missing |
| `dar-q95.14` | glibc raw-fork reset | blocked | private, heuristic, version-sensitive glibc mutation |
| `dar-q95.21` | sandbox-exec pass-through | blocked | silently removes a security boundary |
| `dar-q95.22` | SDK metadata/symlink | provisional | metadata must be derived from authoritative SDK provenance |

Direct invariant coverage for the provisional psynch and microthread changes is
tracked by `dar-q95.29.6`; signal work by `dar-q95.29.5`; SDK provenance by
`dar-q95.29.8`.

## Migration completeness

`~/work/darling` resolves to the West checkout
`~/work/darling-dev/darling`; there is no second dirty source tree anymore.
The only dirty managed repository during this review was `darling-workspace`,
containing the recorded fork-draft state and this audit.

The previous tree-level mega-branch comparison remains valid and found one
real omission, already repaired in `dar-q95.14`. This review additionally
scanned all West repositories for dirty files and unreachable commits. Recent
user-authored unreachable commits occurred only in already known repositories:

- top-level Darling: alternate/rebased SDK, sandbox, mldr, and embedded runner
  commits;
- darlingserver, xnu, and libpthread: historical or alternate hashes of the
  extracted fixes and discarded experiments;
- objc4 and zlib: the previously rejected WIP snapshots.

No new owning repository or new product-change category appeared. The
standalone `darling-debug-runner` contains the embedded runner history through
`9ed5c811` and later cleanup through `c85f1bd1`.

The root, xnu, and darlingserver handoff bundles verify against their owning
repositories and contain the historical and clean refs. The remaining bundles
and explicit dispositions are recorded in
`docs/mega-branch-audit-2026-06-12.md`.

## Clean West worktree gate

It is safe to use the current West checkout as the only active source
worktree. It is not yet safe to delete the handoff bundles or garbage-collect
all historical objects.

Keep the archive until these existing migration tasks are resolved:

- `dar-q95.26`: dyld `_elfcalls` retention;
- `dar-q95.27`: objc4 debug macro contract;
- `dar-q95.28`: residual top-level build/install changes;
- `dar-l76`: final placement of repro and diagnostic assets.

After those tasks, refresh and verify every bundle from its owning repository,
confirm `west status` is clean, then historical mega refs may be removed from
normal branch lists. Bundles remain the final recovery artifact until that
retirement checkpoint is committed and pushed.

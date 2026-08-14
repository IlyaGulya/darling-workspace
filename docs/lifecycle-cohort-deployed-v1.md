# Lifecycle cohort deployed opt-in gate v1

This gate is the first real product acceptance for the writer cohort.  It is
strictly opt-in: the build uses `DARLING_LIFECYCLE_COHORT_V1=ON`, each launcher
transaction exports `DARLING_LIFECYCLE_COHORT_V1=1`, and the repository default
remains `OFF`.  Global production routing remains `deferred`; this gate neither
reclassifies nor routes the remaining incompatible writers.

The contract executes two real Rootless guest sessions against one task-owned
prefix. The first session performs an ordinary `darling shell` RPC, kills the
retained shellspawn identity through a pidfd, waits for launchd KeepAlive to
publish a different process and observed socket identity (`device`, `inode`,
`ctime_ns`), and performs a second shellspawn RPC. It then performs a real
per-user launchd RPC. A task-owned, pre-boot LaunchDaemon waits on a bounded,
task-owned trigger so the accepted shellspawn restart runs first. It then calls
the actual Mach per-user lookup from a launchd-managed process and performs a
legacy launchd request over the Rust-published AF_UNIX endpoint. This forces system
launchd to start its direct per-user launchd child, exercises the returned
endpoint, and
requires one retained `0700` dynamic directory and Unix socket below
`/private/var/tmp/launchd-<pid>-<nonce>/sock`. The ctime
component distinguishes a valid immediate filesystem-inode reuse after exact
retirement from retaining the old endpoint. It then requests product shutdown. The second session
boots the same prefix, performs another RPC, proves reuse retained the exact
`.lifecycle.lock` inode while creating a new session root, and shuts down.

The reuse cycle requires a fresh per-user process identity and a new bounded
dynamic directory name while retaining the exact persistent lifecycle-lock
inode. After each shutdown, the external observer requires:

- no prefix-owned process;
- no `.init.pid`, Darlingserver, controller, system launchd, per-user launchd,
  shellspawn, or dynamic `launchd-*` endpoint/directory;
- no same-UID process retaining an FD below the prefix;
- no mount below the prefix;
- no lifecycle staging, quarantine, or GC tail;
- successful nonblocking exclusive reacquisition of the persistent lifecycle
  lock.

All commands, transitions, output, process/FD scans, and filesystem scans have
explicit finite budgets.  The evidence report records exact source identities,
the deployed launcher digest, process/starttime identities, endpoint inodes,
command-output digests, and both cleanup censuses.  The Python program is only
an external fault driver and observation harness; lifecycle authority and
namespace mutation remain in the Rust controller.
The probe executable, plist, exact trigger and bounded stdout/stderr captures
are provisioned only in the task-owned prefix, bound into source/build
identity, and removed on both success and failure after product teardown.

Before boot, the harness validates the source identity with a closed Draft
2020-12 schema and independently recomputes the workspace/derived Git
commit/tree identities, every semantic-closure and deployed-artifact SHA-256,
the canonical source-map objects, build-input digests and typed prefix-state
digest. Named tamper negatives must all fail. Cleanup uses `lstat` and an
fd-relative, no-follow directory traversal: dangling endpoint symlinks are
residue, while any directory-open, enumeration or lstat error fails closed.

This gate is not permission to enable the route by default or expand the
cohort.  Those changes require a separate review after this deployed evidence
is accepted.

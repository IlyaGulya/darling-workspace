# Darlingserver main-log lifecycle cohort v1

This is the third bounded `dar-4ush.7` writer cohort. It routes exactly
`/private/var/log/dserver.log` when `DARLING_LIFECYCLE_COHORT_V1=1`; the build
default remains `OFF` and global production routing remains `DEFERRED`.

Rust owns the retained prefix descriptor, the exact session-long exclusive
`.lifecycle.lock` lease, fd-relative `private/var/log` traversal, creation and
identity validation. It transfers only an `O_WRONLY|O_APPEND|O_CLOEXEC`
descriptor to Darlingserver. Darlingserver never receives the prefix or parent
authority for this operation. The legacy retained-prefix-relative open remains
the behavior when the cohort is disabled.

The log is persistent data, not an endpoint: clean shutdown validates its
retained parent/name/inode identity but does not unlink or truncate it. A named
replacement makes shutdown fail closed and is preserved. Symlinks, wrong
object type, wrong ownership, wrong mode and multi-link files are rejected
before writer transfer. The v1 threat model is cooperative same-UID writers
that obey the exact lifecycle lease; hostile writers outside that protocol
remain out of scope.

The Perf-derived `dserver-auxlog.txt` producer is a separate incompatible
writer. It is intentionally absent from the C ABI and this acceptance gate.

Local acceptance consists of:

- the focused Rust authority/replacement/ABI tests;
- production C++ compilation through the opt-in CMake target;
- two real guest-ready boot/RPC/shutdown/reuse cycles;
- exact persistent log inode and exactly one `O_WRONLY|O_APPEND` holder while
  running;
- zero processes, endpoints, FD holders, mounts and lock tails after each
  shutdown;
- source-scope and deployed-binary digests embedded in the bounded JSON report.

The deployed contract uses the existing command and teardown deadlines from
the accepted cohort harness and a ten-second bounded FD-holder drain. Evidence
is local review material until a separately approved controlled landing.

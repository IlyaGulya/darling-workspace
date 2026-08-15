# FD-relative guest namespace authority pilot

`dar-4ush.7.3.2` is an architecture gate, not production routing. The route and
default remain `OFF` / `DEFERRED`.

The Rust controller issues a session-generation-bound lease over retained
prefix and `.lifecycle.lock` capabilities. An exact `SCM_RIGHTS` envelope flows
through Darlingserver to `mldr` before guest entry. `libsystem_kernel` hides and
guards the five internal descriptors (including the controller pidfd) and authorizes mutations by acquiring a
shared operation lock acquired through an independently reopened gate OFD.
Controller revocation first closes admission irreversibly and then attempts an
exclusive bounded drain. Controller death is independently observed through the
pidfd on every admission. An `SCM_RIGHTS` duplicate is deliberately not mistaken
for an independent `flock` open-file-description.

A drain timeout returns an owning `CohortFinishError::DrainPending`; neither the
controller worker nor namespace cleanup is started. After a completed drain,
cleanup still requires a distinct one-packet `CLEANUP` command and its ACK. An
EOF, parent death, transport error, absent ACK, malformed packet, or any unknown
command irrevocably selects forensic preservation. Commands are carried over
`SOCK_SEQPACKET`; eventfd arithmetic cannot turn accumulated preserve requests
into cleanup. Preserve-requested and preserve-acknowledged are distinct phases,
and a missing preserve ACK never permits a later normal finish. The C ABI returns
`DARLING_LIFECYCLE_FINISH_DRAIN_PENDING` while preserving the same controller
pointer for retry. Destructive cleanup is reachable only after the exclusive
gate drain succeeds and the child has acknowledged the explicit cleanup packet.

Cleanup uses a monotonic Rust typestate:
`ACTIVE → DRAINING → CLEANUP_REQUESTED → CLEANUP_COMMITTED → FINISHED`.
Successful `send(CLEANUP)` is the irreversible commit point. A later ACK loss
returns the separate `DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING` status while
retaining the exact controller solely for ACK/exit/reap collection. It can never
enter preserve or abandon. The C++ owner mirrors this as a distinct `CLEANING`
phase; `DRAIN_PENDING` alone may transition to `ABANDONING`.

The production C++ owner retries that transition three bounded times. If the
gate remains held, it consumes the pointer through the typed abandon ABI:
admission remains revoked, the Rust worker is stopped and joined, Rust ownership
is freed, and retained namespace evidence is deliberately not cleaned. A
kill/pidfd/wait failure returns `DARLING_LIFECYCLE_ABANDON_PENDING` with the exact
pointer and `ProcessWorker` still owned for retry. The C++ destructor performs a
bounded retry series and aborts the owning process rather than returning while a
live pointer could be lost. `adopt()` refuses a second controller instead of
overwriting ownership.

The C++ owner has a monotonic `EMPTY → ACTIVE → ABANDONING → EMPTY` phase. Once
abandon begins it never invokes normal finish again. Before any abandon fault can
be reported, the controller child receives and acknowledges forensic-preserve
mode; its exit path then disarms `cleanup_all()`. The child also disarms cleanup
on command-channel failure before returning ownership. Real production-child
subprocess fixtures stop command reads across multiple retries and kill the
parent both before and after preserve ACK; the exact endpoint inode remains in
both cases.

The corrective deliberately enables none of the four requested mutation paths.
All return `ENOTSUP` after authenticated admission and before mutation because
upper-only existence cannot exclude a same-name lower object. The retained-FD
resolver remains prototype code, not an enabled mutation authority.

The earlier C-side unlink/whiteout transaction was removed: it could overwrite
staging and lose recovery ownership. `libsystem_kernel` is linked `-nostdlib`, so
the existing `std` Rust boundary cannot be linked into it. Create/mkdir and any
lower/whiteout transition remain blocked until bootstrap carries a sealed
command capability to a Rust transaction service plus a retained lower-template
capability. This is an explicit architecture stop, not a post-mutation check or
a claimed security-ready pilot.

Relative-dirfd forms, metadata operations, general copy-up, and the remaining
inventory are deliberately not claimed. `.7.3.1` is not started. Direct native
Linux syscalls by injected ELF code are outside the Darwin syscall threat
surface; no Darwin guest syscall can close, duplicate, enumerate, or reconfigure
the protected descriptors.

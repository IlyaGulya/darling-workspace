# FD-relative guest namespace authority pilot

`dar-4ush.7.3.3` is an architecture implementation gate, not production routing. The route and
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

`.7.3.3` replaces the disabled C mutation prototype with one bounded production
carrier: the existing per-thread Darlingserver RPC. The guest sends only a
fixed opcode, session-generation plus kernel-random transaction ID, bounded
relative guest paths, exact whitelisted flags and guest-effective mode. The
guest reads its kernel `Umask` without changing process state; Rust creates with
a safe bootstrap mode and applies the exact effective mode inode-relative, so
Darlingserver's umask is irrelevant and special mode bits are not truncated.
Darlingserver copies guest memory and forwards that envelope to the
Rust controller; it owns no upper/lower descriptors and performs no namespace
syscall.

The product deployment transaction publishes one bounded
`.darling-runtime-lower-binding-v1` direct child of the prefix. It binds the
exact prefix generation and inode, the relative runtime destination
`libexec/darling`, its device/inode/type/mode/uid/gid, the exact deployed
`bin/darlingserver`, and a 128-bit deployment transaction identity. A
user-owned destination is valid; neither deployment nor Rust changes ownership.
For focused product deployment this is requested explicitly with
`west darling-build --deploy --deploy-manifest PATH
--bind-runtime-lower-root`; the flag is opt-in and does not change the default
route. Publication and every rollback remain in that one deployment
transaction.

Rust opens the binding, lower root, and deployed controller fd-relative from
the already-retained prefix with
`openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS)`. The
binding's named and opened inode/content, lower name/inode, prefix generation,
and deployed controller are revalidated before every transaction. The running
`/proc/self/exe` must be the bound deployed controller inode. C++ supplies no
lower pathname in ABI v3, and no absolute path is consulted after session
acquisition. Missing, stale, cross-prefix, replaced, malformed, truncated, or
oversized bindings fail closed. The controller already owns the retained upper
prefix. It classifies each leaf as absent,
upper-only, lower-only, both or whiteout, serializes concurrent requests, and
persists a write-ahead journal in an exclusive, mode-0700 sidecar next to (not
inside) the guest prefix, and retains up to 128 exact request/outcome tombstones
without eviction. The private quarantine and journal are invisible to guest
resolution/readdir. Every begin and mutation phase is fsync-bound before its
success can be reported; restart reconstructs exact bound or typed unbound
recovery authority and closes admission. Committed creates reopen only the
durable `P` identity and therefore retain replayable result FDs. Committed
unlink quarantine uses durable `GC_PENDING → GC_DONE`, so a crash after commit
cannot orphan hidden garbage. A torn final WAL record is physically truncated
and fsynced before admission; later commits and a second restart cannot attach
to a corrupt tail. Any ambiguous WAL write/fsync failure closes admission. Once that
budget is full, new IDs fail before mutation while old replay remains safe.
The exact created, quarantined, or published inode remains in a pending-commit
capability until `C` is durable. A COMMIT write/fsync failure transfers that
capability to `RECOVERY_PENDING`; clean sidecar teardown is then forbidden.
Recovery storage is separately capped at two exact authorities per admitted
transaction; exhaustion also rejects before mutation.
Create/mkdir are admitted
only for an absent target. Unlink is admitted only for an upper-only source;
rename requires an upper-only source and absent destination. All lower-only,
both-layer, whiteout and ambiguous forms return `ENOTSUP` before mutation.

Create returns a duplicate of the exact Rust-retained inode through the normal
RPC `SCM_RIGHTS` reply. Receive uses `MSG_CMSG_CLOEXEC` and the guest validates
`FD_CLOEXEC` again before exposing the descriptor. It never reopens the result
by pathname. A replacement
after mutation becomes a bound recovery obligation, preserves both inode and
replacement, and forces forensic shutdown instead of destructive cleanup.
Parent replacement after resolution cannot redirect the syscall because Rust
uses the retained parent FD. Unlink and rename first move a retained source to
a transaction-unique quarantine name and verify its inode; mismatches preserve
typed authority for both displaced source and replacement. Normal quarantine
garbage is deleted only after admission revocation and gate drain.

Outstanding recovery is a distinct `RECOVERY_PENDING` typestate. It cannot
enter abandon or cleanup; the production owner remains parked with admission
revoked and retains the Rust controller and exact recovery FDs. If that process
is killed or the host reboots, the sidecar WAL is the recovery consumer on the
next controller acquisition; recovery is no longer dependent on an in-memory
handoff alone.

The earlier C-side unlink/whiteout transaction remains removed:
`libsystem_kernel` is linked `-nostdlib` and is only a thin RPC client. Lower
and whiteout transitions stay blocked until their journaled Rust protocol and
recovery ownership are reviewed. This is an architecture implementation gate,
not deployed product acceptance; routing and default stay `DEFERRED` / `OFF`.

Relative-dirfd forms, metadata operations, general copy-up, and the remaining
inventory are deliberately not claimed. `.7.3.1` is not started. Direct native
Linux syscalls by injected ELF code are outside the Darwin syscall threat
surface; no Darwin guest syscall can close, duplicate, enumerate, or reconfigure
the protected descriptors.

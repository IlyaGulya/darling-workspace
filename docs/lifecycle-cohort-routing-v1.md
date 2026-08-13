# Rootless lifecycle writer cohort v1

This is the first bounded production-writer migration slice for `dar-4ush.7`.
It does **not** enable the global lifecycle route. Five product records and
one Rust-owned transport record are classified `cohort-ready`; every other
namespace writer remains
`incompatible`, and the production build option defaults to `OFF`.

## Authority

The host Darlingserver starts one Rust supervisor process after prefix pre-init
and before launchd is released. The supervisor watches the owning Darlingserver
through a pidfd, enters a separate session, closes every inherited descriptor
outside its finite retained capability set, and survives the existing product
or owned-process-group `SIGKILL` shutdown long
enough to perform exact cleanup. Explicit finalization remains bounded; owner
death is an equally mandatory cleanup trigger. Rust duplicates and retains the exact prefix
directory capability inherited by Darlingserver, validates the Rootless typed prefix state,
opens the exact `.lifecycle.lock` with
`O_NOFOLLOW`, validates but does not mutate an existing inode before taking
`flock(LOCK_EX)`, and revalidates the named lock inode after
acquisition and before every request. The same open-file description remains
locked for the routed session.

The controller owns publication and retirement of:

- `.init.pid`;
- `.darlingserver.sock`;
- `/var/run/shellspawn.sock`;
- the system `/var/tmp/launchd/sock` endpoint.

Publication preserves the established product metadata: `.init.pid`, launchd
and shellspawn are mode `0600`, while the Darlingserver control socket remains
mode `0775`. The acquired `.darling-prefix-state-v2` must contain the canonical
schema, `runtime_mode=rootless-eunion`, positive generation, exact prefix
device/inode and owner, and lifecycle-v2 provenance. Its retained inode and
complete content are revalidated before every routed mutation.

Darlingserver adopts its pre-bound listener directly. Guest launchd and
shellspawn use a fixed, bounded `SOCK_SEQPACKET` protocol over the hidden
`/.lc-v1.sock` endpoint. The short vchroot-visible name keeps the expanded host
pathname within Linux `sockaddr_un.sun_path`. Rust binds
and retains that endpoint fd-relatively under the same lease; a regular Unix
path is required because Darling's guest AF_UNIX conversion vchroot-expands
paths and does not preserve Linux abstract addresses. The envelope contains a
per-session 256-bit nonce and a finite endpoint enum. Publication is a
two-phase transaction. Rust keeps the namespace object in
`PENDING`, sends its retained listener with `SCM_RIGHTS`, and accepts ownership
only after the consumer has validated, duplicated, adopted and (for launchd)
registered the listener and returns an authenticated `COMMIT`. EOF, response
delivery failure before `COMMIT`, malformed decision, explicit `ABORT`,
duplicate failure and listener-adoption failure all retire the exact pending
inode before another publication can proceed. A complete authenticated
`COMMIT` is the irreversible ownership handoff: Rust records the owner before
attempting the final diagnostic ACK, and the consumer retains its listener if
that ACK is interrupted, times out or is lost. The C transport snapshots the
control name and nonce once before connecting and moves that nonce into its
typed pending-publication state; `COMMIT` and `ABORT` never reread mutable
process environment. Rust authenticates the peer UID, requires
`SO_PEERPIDFD`, binds
that retained pidfd to the `SO_PEERCRED` PID through its kernel fdinfo, and
returns already-bound listener descriptors through `SCM_RIGHTS`; guest code
receives no prefix or lock descriptor and contains no lifecycle reducer. The
Darwin transport cannot request Linux `MSG_CMSG_CLOEXEC`; both consumers call
it only in their single-threaded bootstrap phase and set `FD_CLOEXEC` before
launchd starts jobs or shellspawn begins accept/fork processing. Malformed
responses still drain and close every received ancillary descriptor.
The
launchd capability is bound to the direct session-root child by retained
pidfd/starttime identity and the exact bounded Linux mldr/vchroot argv envelope
`vchroot PREFIX /sbin/launchd`; the prefix argument must byte-match the anchor
used by Rust acquisition. The shellspawn capability is bound to the direct
child of that retained launchd identity with a single expected guest argv0.
Each endpoint owner's pidfd
remains retained until exact-owner retirement, preventing numeric PID reuse
from authorizing a later process. A KeepAlive restart after an ungraceful
shellspawn death performs a typed `GONE` transition on that retained pidfd,
retires only the exact retained socket inode, and republishes a fresh listener
for the newly authenticated child.

Malformed or unauthorized requests are bounded per connection but never
consume a controller-lifetime request counter. A same-UID guest flood can be
rejected indefinitely without terminating the authority process or deleting
the live product endpoints.

The nonce is not a general guest secret. Launchd passes it only to the
first-cohort shellspawn job, strips it before every other spawn, rejects user
environment mutations of the reserved keys, and removes those keys from its
environment-export RPC. Kernel peer identity remains mandatory even when the
nonce is correct.

The routed path is fail-closed. A missing controller, malformed envelope,
wrong nonce, split lock, endpoint replacement, duplicate publication or
unsupported per-user launchd request does not fall back to pathname mutation.
Listener publication and event-loop activation are one transaction at the C
boundary: if launchd cannot register the delivered descriptor with kqueue, it
closes the capability and requests exact Rust-owned retirement before
returning failure.
The legacy path remains compiled only while the opt-in route is disabled, so
product behavior is unchanged in this review slice.

## Compatibility boundary

`cohort-ready` is intentionally weaker than global `compatible`. It means the
writer has a reviewed Rust-authority route and a retained exact lease when the
cohort option is enabled. It does not claim that unrelated `/var/run`,
`/var/tmp`, deploy, repair, E-UNION or guest daemon writers cooperate. Global
production routing remains blocked until those records migrate in their own
cohorts. Per-user launchd dynamic endpoints are split into a separate
incompatible record.

## Local contract

Run:

```sh
DARLING_LIFECYCLE_DARLING_ROOT=/path/to/darling \
  DARLING_LIFECYCLE_DARLINGSERVER_ROOT=/path/to/darlingserver \
  ./tests/run-lifecycle-cohort-routing-contract.sh
```

The contract builds the real Rust static library, compiles the production C
transport, and executes it against a task-owned prefix. It proves exact lock
contention, fd-relative publication, `SCM_RIGHTS` listener delivery, wrong-
nonce flood rejection without authority exhaustion, retained-pidfd peer
authorization, shellspawn `SIGKILL`/KeepAlive republish, post-publication
activation rollback, stable-nonce commit across activation-environment drift,
lost-final-ACK commit retention, owner-`SIGKILL` cleanup, launchd/shellspawn
retirement, replacement preservation and bounded cleanup. The source/registry
pass requires exactly six cohort-ready
records, zero globally compatible records, and leaves the remainder
incompatible.

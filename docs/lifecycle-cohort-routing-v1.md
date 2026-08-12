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
death is an equally mandatory cleanup trigger. Rust opens and retains the prefix directory,
validates the Rootless runtime marker, opens the exact `.lifecycle.lock` with
`O_NOFOLLOW`, validates but does not mutate an existing inode before taking
`flock(LOCK_EX)`, and revalidates the named lock inode after
acquisition and before every request. The same open-file description remains
locked for the routed session.

The controller owns publication and retirement of:

- `.init.pid`;
- `.darlingserver.sock`;
- `/private/var/run/shellspawn.sock`;
- the system `/private/var/tmp/launchd/sock` endpoint.

Publication preserves the established product metadata: `.init.pid`, launchd
and shellspawn are mode `0600`, while the Darlingserver control socket remains
mode `0775`. The acquired legacy marker must contain the canonical
`DARLING_RUNTIME_MODE_V1=rootless-eunion` record and its retained inode and
content are revalidated before every routed mutation.

Darlingserver adopts its pre-bound listener directly. Guest launchd and
shellspawn use a fixed, bounded `SOCK_SEQPACKET` protocol over the hidden
`/private/var/run/.darling-lifecycle-controller-v1.sock` endpoint. Rust binds
and retains that endpoint fd-relatively under the same lease; a regular Unix
path is required because Darling's guest AF_UNIX conversion vchroot-expands
paths and does not preserve Linux abstract addresses. The envelope contains a
per-session 256-bit nonce and a finite
endpoint enum. Rust authenticates the peer UID, requires `SO_PEERPIDFD`, binds
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
activation rollback, owner-`SIGKILL` cleanup, launchd/shellspawn
retirement, replacement preservation and bounded cleanup. The source/registry
pass requires exactly six cohort-ready
records, zero globally compatible records, and leaves the remainder
incompatible.

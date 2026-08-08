# Rust-owned quarantine GC consumer v1

This document describes the infrastructure-only consumer for the
`QUARANTINE_GC_REQUIRED` handoff emitted by the Rust lifecycle controller. It
is not production routing and does not change Darling, Darlingserver, XNU,
profiles, locks, or runtime defaults.

## Ownership boundary

The endpoint cleanup backend moves an endpoint and its public placeholder into
a private quarantine and exposes a checked `RecoveryPending` →
`QuarantinePending` transition. The latter is consumed exactly once with
`QuarantinePending::into_parts()`. The returned
`QuarantineObligation` owns:

- the source and quarantine parent descriptors;
- the endpoint and placeholder descriptors retained before the exchange;
- source/parent/placeholder identities and both current names;
- the runtime scope brand, retained writer-parent/lock descriptors, lock name,
  and deletion progress bits.

No GC operation reopens a parent or endpoint by an absolute path. Every
identity check and `unlinkat` is relative to the retained quarantine parent.
An identity mismatch, replacement, failed syscall, interruption, or budget
exhaustion returns the current obligation and the unprocessed tail in order.
The GC consumer accepts no terminal response; only `Cleaned::finalize()` emits
the unique final `SUCCESS` event.

The consumer itself cannot mint a writer-stop proof. The Rust controller's
`Quiescent` typestate issues the sealed `ControllerQuiescenceGrant` through
`from_quiescent` only for the retained obligation, after stopping namespace writers and holding the
exclusive lifecycle lock. An invalidated or mismatched authority fails closed
before deletion; the lock is revalidated fd-relatively and with a non-blocking
kernel `flock` check. Product routing is still deferred; this is the issuer
seam that routing will consume later.

## Writer threat model

This infrastructure slice uses a cooperative-writer protocol: every writer
that can mutate the runtime namespace must first hold the same retained
exclusive lifecycle lock. A hostile same-UID writer that ignores that protocol
is outside the v1 threat model; an `fstatat` followed by `unlinkat` cannot make
an unconditional promise against such a writer. The GC therefore treats lock
identity/lease loss as fail-closed and never claims hostile-writer ABA
protection. Before production routing, the writer inventory contract must
cover every lifecycle mutation entrypoint and prove that it acquires this
lease; no Python/path-based cleanup is part of the route.

## Bounded operation

`GcLimits` has an authoritative maximum of 16 obligations and 128 charged
validation/deletion steps. The limits cannot be raised by a trace or transport
payload. A successful run deletes exactly the retained endpoint and
placeholder objects and produces a new `ControllerResponse` with:

```text
verdict = SUCCESS
obligations = []
journal = Prepared, CapabilitiesAcquired, MembershipBound,
          ShutdownRequested, Drained, Quiescent, Cleaned, Finalized(SUCCESS)
```

Thus the normal lifecycle can end in `SUCCESS` without
`QUARANTINE_GC_REQUIRED`; no product consumer is connected yet.

## Recovery matrix

The local contract covers:

- pre-existing replacement/ABA before the cooperative handoff (the replacement
  remains untouched);
- replacement of the named lifecycle lock (the retained writer lease is
  rejected before any unlink);
- an unleased writer is blocked by the retained exclusive flock;
- SIGINT/fault after endpoint deletion, with a typed progress bit and a retry
  that does not repeat the endpoint unlink;
- endpoint-level accounting across a retry (one obligation counts as one
  endpoint even though it owns two quarantine objects);
- bounded multi-obligation tail preservation;
- malformed ownership topology and missing external authority.

`GcFailure::into_parts()` is the only way to transfer the pending typestate,
retained obligations, deleted count, and typed error to a recovery owner. A
successful GC returns `Cleaned`, which alone can be finalized into the terminal
`SUCCESS` response. There is no
path-based fallback cleanup and no silent destruction of a quarantine handle.

Run the focused local contract with:

```sh
tests/run-lifecycle-quarantine-gc-contract.sh
```

The wrapper assigns one task-owned `TMPDIR` and removes that exact namespace on
both success and failure; the Rust fixture keeps forensic state only while the
typed failure is being asserted.

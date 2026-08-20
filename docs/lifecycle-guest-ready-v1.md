# Trace-driven guest-ready lifecycle lane v1 (dar-4ush.5.2)

This lane is the guest/product complement to the accepted `.5.1` Linux kernel
capability lane. It leaves `DARLING_LIFECYCLE_COHORT_V1` unset, keeps production
routing `DEFERRED`, and makes no product-source or runtime-default change.

The wrapper provisions a task-owned Rootless E-UNION prefix through the
existing `homebrew-rootless-bootstrap-minimal` provider when a prebuilt prefix
is not supplied. The Python process is only a bounded external observer and
fault driver. Rust first supplies the canonical phase/terminal template for
each trace. The lane performs three graceful cycles against the same prefix:

1. guest-ready RPC, rejected pidfd signal, graceful shutdown, and retained
   root pidfd `GONE`;
2. retained-holder deadline followed by graceful drain;
3. a fresh root identity in the same prefix, another RPC, and graceful reuse;

It then runs isolated late-fork, root-death-before-snapshot, and real `SIGINT`
fault sessions. Those cases end through exact task-owned fail-closed cleanup;
they are not mislabeled as graceful product transitions.

The first three cycles are real `boot -> RPC -> graceful shutdown -> reuse`
cycles. Every one of the six accepted lifecycle bytecodes receives a closed,
mode-tagged observation. `lifecycle-fuzz --verify-guest-observation` replays
the bytecode through the Rust reducer, checks the exact production phase order,
terminal, identity witnesses, resource budgets, and requires an empty
unresolved-obligation set. Python does not implement a parallel reducer or
carry a second phase/terminal table.

The machine report binds the workspace commit/tree and lock/profile digests,
the semantic harness closure, the prefix generation and inode, and SHA-256 plus
inode metadata for `darling`, Darlingserver, launchd, shellspawn, mldr, dyld,
and libsystem_kernel. Named tamper negatives cover identity, ordering, missing
events, terminal, unresolved obligations, trace cross-pairing, unknown fields,
and oversized transport.

All reads, commands, transitions, output, process/FD scans, tree scans, and the
report have explicit bounds. Cleanup is fd-aware: a same-UID process retaining
any descriptor below the exact task prefix is part of the census even when it
has lost `DPREFIX` from its environment. The final verdict requires no prefix
processes or FD holders, runtime endpoints, mounts, transaction refs, or
changed prefix-root identity.

The cleanup writer model is intentionally limited to the task-owned test
namespace after its complete process/FD-holder census reaches zero. It does
not claim protection against an unrelated hostile same-UID process racing a
test-only stale-name retirement.

Run with an already provisioned task-owned prefix:

```text
DARLING_LIFECYCLE_GUEST_ROOT=/tmp/dar-4ush.5.2 \
DARLING_LIFECYCLE_GUEST_PREFIX=/tmp/dar-4ush.5.2/darling-rootless-guest-ready \
DARLING_LIFECYCLE_GUEST_EVIDENCE=/tmp/dar-4ush.5.2/evidence \
DARLING_LIFECYCLE_GUEST_REPORT=/tmp/dar-4ush.5.2/report.json \
tests/run-lifecycle-guest-ready-contract.sh
```

Without `DARLING_LIFECYCLE_GUEST_PREFIX`, the wrapper owns bootstrap and exact
prefix removal as well. The success marker is
`GUEST_READY_REAL_KERNEL_LANE_5_2_VALID`.

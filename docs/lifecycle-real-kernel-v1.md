# Hermetic real-kernel lifecycle lane v1 (dar-4ush.5.1)

This lane is the `.5.1` kernel-capability smoke layer after the deterministic
`.4` fuzz infrastructure. It does not route Rootless, E-UNION, or any other
production consumer, and it does not change source locks, mappings, refs,
workflows, or runtime defaults. `.5.2` (guest-ready Darling boot, namespace
churn, and production session reuse) remains a separate, explicitly deferred
acceptance lane; these fixtures must not be reported as guest-ready.

The wrapper creates one task-owned `TMPDIR`, builds the accepted `.4`
`lifecycle-fuzz` producer inside that directory, loads all six checked-in
golden traces, and runs the host fixtures from
`tests/west_test_contracts/lifecycle_real_kernel_contract.py`.

Every accepted `.4` trace is first replayed by the Rust `lifecycle-fuzz`
oracle. Its typed event data selects one real kernel action plan, and the
returned observation is included in the report. The Python code is only the
bounded transport/host adapter; it does not construct a second reducer. Each
observation is then sent back through
`lifecycle-fuzz --verify-kernel-observation <trace-hex>` and must receive
`KERNEL_OBSERVATION_ACCEPTED` before the case can pass.

The verifier input is a closed JSON envelope tagged by `mode`, with
`deny_unknown_fields` on both the envelope and its common facts. PID-reuse
requires either the same PID with different `/proc` starttimes or a different
replacement PID, plus `pidfd_send_signal -> ESRCH` and a live replacement;
late-fork requires the controller-observed PID/starttime, acknowledgement, and
post-drain `GONE` witness. The Rust adapter reads at most 16 KiB and rejects
unknown, truncated, incomplete, or oversized envelopes before replay.

## Kernel mechanisms

The lane proves, on the host kernel rather than in the reducer:

* cgroup v2 creation, retained cgroup directory identity, `cgroup.procs`,
  `cgroup.events`, bounded process churn, retained pidfds,
  `pidfd_send_signal`, and an unrelated process that survives member drain;
* `renameat2(RENAME_NOREPLACE)` with retained directory FD, inode identity,
  ABA replacement refusal (`EEXIST`), and mode-000 unlink/rmdir cases;
* filesystem AF_UNIX endpoint creation, connection, payload exchange, close,
  and pathname removal;
* six trace-driven process-session cases covering signal-gone,
  signal-rejected, retained-holder deadline, PID identity replacement, late
  fork, and root exit before snapshot. Signal-gone records a second
  `pidfd_send_signal` returning `ESRCH`; rejected-signal keeps the pidfd open
  and proves `EINVAL`; PID replacement records `(pid,starttime)` and is
  classified honestly as a retired-pidfd replacement when numeric reuse does
  not occur; late-fork records and verifies the child identity before the
  controller acknowledges and drains it;
* three process-session `READY → SHUTDOWN → DRAINED` cycles reusing one
  disposable session root. Each cycle has a real child process, a forked
  worker, a pidfd, and an AF_UNIX endpoint. These are kernel-session fixtures,
  not a claim that `.5.2` guest-ready boot or `.7` production routing is
  already enabled.

The cgroup fixture probes delegation first and uses `sudo -n` only when the
probe fails. The helper has a 45-second process deadline,
creates one uniquely named cgroup, and must remove it after proving
`populated 0`; it never scans or signals unrelated processes. If passwordless
privilege or cgroup v2 is unavailable, the lane fails with the exact helper
error rather than skipping the kernel proof.

## Teardown contract

The lane fails closed unless the owned root has no endpoints, no transaction
paths, no tracked child processes, and no FD growth beyond the bounded
launcher overhead. It measures mountinfo before/after, process identities,
socket/transaction paths, and (when `DARLING_LIFECYCLE_BASELINE` is supplied)
a bounded baseline digest. With no baseline it reports
`baseline_scope: NONE` and `baseline_mutated: null`, never a fabricated
boolean. The shell owner removes only its exact task-owned temporary root after
the JSON result is captured. No retained baseline prefix is used or modified.

The interruption fixture sends `SIGINT` to a nested session process group and
proves that the group and its endpoint are gone before the lane continues.

Run locally:

```text
tests/run-lifecycle-real-kernel-contract.sh
```

The result marker is `REAL_KERNEL_LANE_5_1_VALID`. The report carries the six
trace names and bytecode, Rust oracle summaries, trace-driven kernel
observations, kernel release/architecture, cgroup/pidfd identity,
rename/permission identities, cycle markers, and measured teardown census.

# Concrete Linux lifecycle backend v1

This slice is a task-owned fixture backend for the Rust controller architecture
gate. It is deliberately not selected by a West factory and does not change
Darling, Darlingserver, XNU, profiles, locks, mappings, or runtime defaults.

`LinuxBackend` owns the concrete Linux facts for one transaction:

- marker, launcher, `.init.pid`, and `var/run` are opened relative to the
  inherited anchor with `O_NOFOLLOW` and retained descriptors;
- marker content, mode, owner, and launcher content digest are observed before
  membership binding;
- the session root is bound to a retained cgroup-v2 directory identity, a
  startup session token, and its original controller-parent identity; a PID
  copied into `.init.pid` without those facts is rejected;
- a launch-time child-subreaper capability is required before the runtime root
  is spawned.  The bounded fixed-point census reads the retained cgroup-v2
  `cgroup.procs` snapshot (filtered by the startup session ID, retained
  `(pid,starttime)` capabilities, and children reparented to this subreaper)
  and then walks every
  `/proc/<pid>/task/<tid>/children` edge and observed TID.  This keeps a
  `setsid(2)` descendant in the kill set after it is reparented before the
  first acquisition census, while never treating an unrelated same-cgroup
  process as runtime-owned.  It includes
  reparented/orphaned descendants, grandchildren, and late-fork changes.  A
  cgroup or children file that exceeds its byte budget is an error, never
  `complete=true`;
- every discovered process receives a retained pidfd and `(pid, starttime)`
  identity;
- signals use `pidfd_send_signal`, mapping `SENT`, `GONE`, `REJECTED`, and
  `DEADLINE` into the Rust signal reducer;
- the exclusive lifecycle lease is flocked and bound to its retained inode
  before marker, PID, cgroup, or process observations.  Every signal,
  observation, and cleanup revalidates that named identity;
- `pidfd_send_signal` errors are typed: only `ESRCH`, `EAGAIN`/`ETIMEDOUT`,
  and permission errors become lifecycle results; `EBADF` and unknown errors
  fail closed;
- before signaling, a stop-barrier guard freezes the retained root and member
  pidfds; every error path resumes the stopped set (or returns a typed
  `STOPPED_PROCESS` obligation).  Quiescence requires the retained lease, the
  cgroup/session drain, and a bounded fixed point;
- `.init.pid` is one retained descriptor from acquisition through cleanup;
  named identity is checked only at pre-mutation boundaries, never rebound by
  a later pathname open;
- cleanup uses `renameat2(RENAME_EXCHANGE)` with a private placeholder,
  registers a retained `QuarantineObligation` immediately after the exchange,
  with an endpoint FD duplicated before the exchange, verifies the exchanged
  inode, and moves both exact objects into private
  quarantine names with `RENAME_NOREPLACE`.  The backend never performs a
  final `fstatat`→`unlinkat` garbage-collection sequence: it returns
  `QUARANTINE_GC_REQUIRED` for an external quiescent controller to own.  If a
  replacement appears after either final identity check, the proof is
  invalidated and the object remains as a typed recovery obligation; no
  replacement is unlinked.

The fixture tests create a private temporary prefix with real Unix sockets and
task-owned `setsid` shell process trees. They drive the ordinary Rust transitions
through acquisition, membership binding, pidfd shutdown, drain, quiescence,
and fd-relative cleanup. RED→GREEN cases cover a foreign PID, split-lock,
orphaned cgroup members, grandchild and late-fork census changes,
stop-barrier failure/resume, retained `.init.pid` swap, EBADF pidfds,
regular-file endpoints, and replacement after each final quarantine identity
check. The replacement tests require a fail-closed response while preserving
the replacement. Fixture `Drop` drains child pidfds before removing its exact
temporary root.

The Python adapter remains transport-only. It is not involved in acquisition,
membership, signaling, quiescence, or cleanup.

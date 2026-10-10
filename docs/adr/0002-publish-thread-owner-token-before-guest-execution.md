# ADR: publish a nonzero, unique thread owner token before guest execution

Status: superseded by 0004-one-implementation-per-public-concurrency-symbol.md. Its premise -- that the
guest thread's owner token was never published -- was refuted by measurement: the token is present and
stable, and the lock the guest actually dispatched to was a weak no-op stub. The invariant it states
(a nonzero owner token before lock-capable execution) remains valid and is carried forward.

## Context

Darwin unfair locks use the calling thread's Mach thread-port name, read directly from TSD slot
`__TSD_MACH_THREAD_SELF`, as their owner identity. Zero is the unlocked value. A successful
compare-and-exchange of zero to a zero identity does not acquire exclusive ownership.

Thread identity allocation and guest publication are separate responsibilities. Darling's server owns
Mach thread identities and the task IPC namespace. The image launcher owns the initial guest stack and
the apple vector. Guest libpthread owns the guest pthread structure and its TSD. Host-only loader
threads are not guest pthreads and must not be classified as defective guest threads solely because
their GS-relative slot reads zero.

Apple's libpthread accepts an initial `th_port` apple entry and falls back to `mach_thread_self()` when
it is absent, including after fork. The contract is therefore a valid identity before guest
lock-capable execution, not the presence of `th_port` on every Darwin exec path.

## Decision

**The Darling loader must publish the server-issued initial-thread identity through the guest apple
vector before entering dyld; every guest thread-start boundary must publish a valid owner token before
executing lock-capable guest code.**

Authorities:

| Layer | Authority and obligation |
| --- | --- |
| Server / duct-tape kernel | Allocate a valid Mach thread-port name in the owning task's IPC space; preserve distinct names for simultaneously live guest threads. |
| Loader | Obtain the initial identity through the existing bootstrap protocol, reject invalid replies, and publish `th_port` in the apple vector. Treat startup identity failure as fatal. |
| Guest libpthread | Install the published token into its own TSD; validate startup and postfork identities before admitting guest execution. |
| Guest thread bridge | Establish the correct TSD base and publish the server-issued token before invoking pthread or workqueue entry points. |
| Workspace tests and diagnostics | Execute behavioral checks and distinguish guest pthreads from host-only threads. |

The loader must use the identity returned for the guest initial thread, not its Linux TID or a
manufactured fallback. The handoff must respect the port-right ownership expected by libpthread, whose
main-thread identity reference is consumed during termination. Both supported stack encodings must
contain the same semantic entry.

Libpthread must fail initialization if its supplied or fallback identity is invalid. It must validate
the actual running TSD view, not merely a detached pthread structure. The fork child must obtain its
identity in the child's IPC namespace; it must not inherit the parent's owner token as authority. No
late-init repair is the normal startup path.

## Consequences

- Initial-thread identity publication is implemented where Darling actually constructs the guest stack,
  not in an unused XNU exec-vector producer.
- A missing or failed identity handoff can no longer silently admit a guest thread whose unfair-lock
  owner is zero. Successful guest startup with a zero owner token becomes impossible by contract.
- Identity allocation remains server-owned. The loader does not invent identities or duplicate a Mach
  namespace.
- A loader bootstrap token must be transferred with a clear port-right lifetime; temporary loader
  ownership and libpthread termination ownership must not accidentally add leaks or consume the same
  right twice.
- Main-thread, pthread, workqueue, fork-child and exec/re-exec transitions are covered by direct TSD
  observations and behavioral exclusion tests. Tests compare simultaneously live threads; port-name
  reuse after termination is not forbidden.
- Diagnostics report raw tokens and thread classification. They do not infer lock acquisition history
  or blame an unclassified host thread.
- This invariant does not prove that every unfair-lock failure or application re-entry is repaired.
  End-to-end workload gates remain separate acceptance checks.
- Workspace-owned tests and this ADR live in the manifest repository. Runtime changes live in the
  components that own the launcher, guest pthread initialization and thread bridge.

## Rejected alternatives

### Unconditionally add `th_port` in XNU `kern_exec.c`

This is suitable only for an image whose guest stack is built by that exec implementation. Darling's
active launcher constructs its own stack, so changing this producer does not repair its handoff. The
existing Darwin fallback for ordinary exec also shows that unconditional apple-vector publication is
not the sole valid Darwin identity path.

### Set `ip_new_thread` to force the existing XNU branch

`ip_new_thread` represents a spawn/vfork-created thread and is consumed by task switching, reference
handling and spawn policies. Changing that lifecycle meaning merely to emit an apple entry introduces
unrelated obligations and does not reach Darling's active stack builder.

### Refill the token in `__pthread_late_init`

Late repair leaves an interval in which a guest thread can already execute code with an invalid owner
identity. It also does not run as the universal fork-child or worker-start path. Startup validation and
correct publication are required instead.

### Make libplatform reject zero as the primary repair

Fail-fast lock consumption can limit damage but cannot supply a valid identity or make other TSD
consumers correct. It is not the identity owner and is not the selected runtime repair. No
instrumentation is added to unfair-lock entry paths.

## Enforcement

A workspace guest fixture independently reads the actual TSD owner slot and compares it with the thread
identity in the guest pthread structure and a valid trap result. It holds all census threads live behind
a barrier, includes the main thread in contention, executes pthread and dispatch entry paths, and
repeats the checks across fork and exec. The exclusion oracle uses atomic occupancy tracking rather than
a racy volatile flag.

The fixture is registered in testkit/CTest and selected by a real guest CI tier. Host behavioral
contracts exercise both stack encodings, valid and invalid identity replies, port-right transfer, and
diagnostics classification; shell contracts are registered in `ci/run-host-tier.py` `CONTRACTS`, Python
contracts in `EXPLICIT_CONTRACTS`. A genuinely guest-only contract is listed in `EXCLUDED_CONTRACTS`
with its concrete guest-tier owner and reason, not silently omitted.

A core-thread identity diagnostic reports per-thread TSD base, token, readable/unreadable status and
guest/host/unknown classification without requiring an unfair-lock abort frame. Synthetic behavioral
fixtures cover zero guest identity, duplicate live guest identity, host-only zero identity, missing
memory and healthy identities.

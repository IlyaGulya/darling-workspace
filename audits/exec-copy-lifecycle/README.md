# Exec and copy-service lifecycle contract

## Implemented server coordinator

`ExecCompletion` is a small production helper, serialized by `Process::_rwlock`.
It deliberately does not own syscalls, Mach task replacement, epoll registration,
or process destruction. Those resource effects remain handwritten in Process
and the existing exec-preparation caller in `src/call.cpp`.

* Birth starts attempt 0, successful-image generation 0, no pending listener.
  Each Process owns its own coordinator; a fork child's new Process does not
  inherit the parent's coordinator state or pending callback capability.
* `beginExec` registers one attempt, not a successful image. It increments a
  bounded 64-bit attempt ID, rejects overlap/null listeners, and refuses wrap.
  Failed submission leaves state unchanged. This is not a public authentication
  token. Existing call.cpp passes the exact shared FD object into both begin and
  its monitor callback; no numeric-FD comparison or caller migration shim exists.
* An open nonblocking pipe means pending. A failure byte consumes the attempt
  without changing image generation. EOF consumes it and increments image
  generation exactly once. This relies on the existing exec pipe protocol
  (CLOEXEC writer, explicit failure byte); arbitrary writer loss is not an
  independent executed-image attestation.
* Checkin can consume the same EOF before epoll delivery. A monitor may consume
  only its own still-current shared listener object. Old callbacks cannot read
  a newer pipe, double-complete, or advance generation. Pending attempts cannot
  be silently overwritten. A retry after failure is a different attempt.
* Darling EOF retires old task memory immediately, even before a replacement
  checkin. Native EOF also retires memory and terminally retires the coordinator
  under the lock, then invokes existing Process death teardown after unlocking.
  This closes the callback-to-death window against another attempt/checkin.
  Death cancels any pending listener and is terminal; queued EOF cannot revive it.
* `prepareCheckin` still fences the old task before interrupting an old syscall
  on an authenticated main-thread replacement checkin. `notifyCheckin` publishes
  a replacement task only for a non-fork main-thread recheckin, not merely because
  an epoll event arrived. Dead/native-retired Process instances reject checkin.
  Failed exec does not itself retire memory or replace the task.
* The retirement boundary is the first server-observed successful EOF or main
  replacement checkin, not the actual host kernel exec instant. Old RPCs retain
  their retired task; this is not proof against arbitrary native writes between
  kernel exec and server observation, nor a global protected memory supervisor.
* `ExecReplacement` is the production ownership handshake across every unlocked
  thread-clear/create/publish/predecessor-release interval. Exactly one checkin
  owns replacement. Death immediately retires authority, but defers physical
  task/thread/semaphore teardown until that owner finishes. A dying owner
  publishes only a memory-retired coherent context so `current_thread` points
  to the new thread before releasing its predecessor, then drains pending death
  instead of announcing NOTE_EXEC. Repeated death claims physical teardown once.
  No Process lock is held across duct-tape operations that can suspend.

### Existing generation sources are not interchangeable

`duct-tape/src/task.c` allocates audit-token `val[7]` from a global 32-bit
`next_pidversion` during task creation (fork and task replacement); it wraps and
is neither an exec-attempt ID nor a cryptographic identity. Failed exec does not
create a replacement task. The helper's successful-image generation is local to
one Process and may advance at EOF before the new audit task is created.

The FD-courier envelope's `process_generation` is learned from peer traffic and
used by server courier state to prevent descriptor/token pairing across that
connection's incarnations. It is peer-supplied, not the server's successful exec
counter, audit pidversion, or a trusted executed-code identity. Neither existing
source supplies copy-service security bootstrap or protected authority.

## Required copy-service contract — NOT implemented product security

The only current copy service is a disposable diagnostic prototype. The following
rules describe the required product boundary, not a deployed feature or fallback:

1. Birth is unbound and rejects copies. Binding must be performed by a protected,
   authoritative supervisor to a process identity, successful-image generation,
   endpoint incarnation and permitted memory authority; not by trusting a client
   generation field or an inherited FD alone.
2. Fork creates a fresh unbound child. Parent bindings and queued requests do not
   transfer. Explicit child bootstrap is required before any copy.
3. Failed exec preserves generation and its binding. Successful exec invalidates
   the old generation, endpoint and outstanding work before new authority is
   usable; the new image starts unbound and requires fresh binding.
4. Revocation invalidates the endpoint incarnation and all its in-flight requests;
   rebinding must never validate old requests, even within the same image.
5. Death revokes everything permanently. PID/FD reuse, delayed callbacks, inherited
   descriptors and timeout delivery cannot revive dead authority.
6. A request writes at most once. Cancellation/timeout is terminal for that request
   identity, not permission to replay. A write must revalidate current authority
   at its irreversible memory-write boundary, serialized against revoke/exec/death.

**Main-goal blocker:** trusted supervisor/TCB protection, authoritative image
attestation and copy-service birth/bind/revoke/fork/exec bootstrap do not exist.
This contract/model cannot provide them, protect all native memory/FD mutation
paths, or establish full Cryptex/executed-code authorization. Blocked-syscall
preservation with a real service and hostile native mutations remain unproved.

## Executable finite interaction model

`ExecCopyLifecycle.tla` explores two attempts, up to two successful generations,
one abstract copy request, delayed/repeated completion delivery, failed exec,
bind, timeout, write and death. Initial unbound state represents either birth or
an independently bootstrapped fork child. Completion atomically increments image
and revokes binding; there is no native replacement after death. The model has
no fairness or liveness claim. It abstracts away pipe reads, locks, task references,
FD-courier pairing, explicit child creation, standalone revoke/rebind endpoint
incarnations and supervisor authentication. In particular it does not prove the
unimplemented copy service or claim bisimulation with the full server.

Invariants: `TypeOK` bounds every state component; `NoStaleCompletion` forbids
accepting a callback other than the live unconsumed attempt; `NoLateWrite`
forbids writes after death or against a revoked/old image binding; `AtMostOnce`
bounds both each attempt completion and the request write; `NoTimeoutReplay`
forbids writes from timed-out/already-completed requests. Sticky violation flags
record events at the action boundary; they are not assumptions constraining Next.
Unsafe switches remove precisely those guards, exposing counterexample traces.

Use the shared runner (no separate invocation implementation here):

```sh
audits/rpc-plane-model/run-all.sh --jar "$TLA_JAR" \
  --model-dir audits/exec-copy-lifecycle --module ExecCopyLifecycle \
  --case safe.cfg:TypeOK:OK \
  --case safe.cfg:NoStaleCompletion:OK \
  --case safe.cfg:NoLateWrite:OK \
  --case safe.cfg:AtMostOnce:OK \
  --case safe.cfg:NoTimeoutReplay:OK \
  --case unsafe-stale.cfg:NoStaleCompletion:VIOLATED \
  --case unsafe-stale.cfg:AtMostOnce:VIOLATED \
  --case unsafe-write.cfg:NoLateWrite:VIOLATED \
  --case unsafe-replay.cfg:AtMostOnce:VIOLATED \
  --case unsafe-replay.cfg:NoTimeoutReplay:VIOLATED
```

Expected unsafe witnesses: complete attempt 1 twice; submit a copy then exec or
 die before write; timeout then write; or write then write again. These are
expected model outcomes, not execution results recorded by this change.

## Direct production-helper scenarios and verification handoff

Existing source-owned `exec_completion_test.cpp` executes real nonblocking pipes
against the actual helper. `checkinBeforeMonitor`, `monitorBeforeCheckin`,
`staleMonitorDuringNextExec` map to completion ordering/at-most-once guards;
`failureBeforeEOF` and `generationsAndDeath` map to failed exec, attempt/image
separation, overlap rejection and terminal death; `nativeCheckinBeforeMonitor`
exercises native EOF. `oldImageBeforeCompletion` checks pending pipe polling.
`replacementDeathOwnership` directly exercises the production ownership helper:
death before begin, duplicate/concurrent begin rejection, death/repeated death
inside the replacement interval, owner finish followed by one teardown claim,
successive live replacements and death after publication. Creation, publication
and predecessor release all deliberately retain the same active owner; helper
tests do not execute duct-tape resource allocation/destruction. Those handwritten
effects still need runtime integration evidence and are not in the TLA+ model.
No helper scenario asserts that the abstract copy service exists. No direct
production-helper counterpart exists for modeled copy timeout/write authorization;
that absence remains a product blocker, not model coverage.

Parent should build the existing `dserver_exec_completion_tests` target and run
`ctest --test-dir BUILD -R '^dserver_exec_completion$' --output-on-failure`,
then run the model matrix above. Guest main-thread/pthread/fork/failed-exec/successful-
exec and delayed monitor integration needs a newly built normal installation;
helper/model results are host/model evidence only. This slice ran no builds,
tests, formatters, TLC or guest launches.

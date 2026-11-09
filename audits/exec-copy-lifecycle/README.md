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
* Server task retirement starts at the first server-observed successful EOF or
  main replacement checkin. Separately, the production Rust supervisor rotates
  its copy binding at the kernel EXEC event; an old binding cannot address the
  replacement image even before server checkin. Neither mechanism authenticates
  executable contents or excludes every native/shared-memory writer.
* `ExecReplacement` checks each thread-clear, allocation, publication and
  predecessor-release phase across unlocked intervals. A separate resource
  state records live authority, revocation, exclusive physical teardown and
  completed release. Death revokes immediately; an active replacement owner
  first publishes a retired coherent context, repoints `current_thread`, releases
  its predecessor, then drains deferred death instead of announcing NOTE_EXEC.
  Repeated death cannot claim teardown twice. No Process lock spans dtape work
  that can suspend. The resource interaction model is documented separately in
  [exec-replacement-model](../exec-replacement-model/README.md).

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

## Required protected-copy contract — NOT implemented product security

The production Rust supervisor supplies kernel image-bound copying, not the
protected, attested authority described below. Earlier diagnostic prototypes and
this abstract model are not substitutes for that required product boundary:

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

**Main-goal blocker:** supervisor/TCB isolation, authoritative executable-closure
attestation and protected birth/bind/revoke/fork/exec authorization remain absent.
The limited production image binding does not protect all native memory/FD
mutation paths or establish full Cryptex/executed-code authorization. Native
tests exercise blocked-syscall preservation, but do not prove protection against
hostile shared-domain or asynchronous mutation.

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
exclusive admission, death at each resource phase, premature-transition rejection,
successive live replacements, missing-main ownership return and exactly-once
teardown. These helper tests do not execute duct-tape allocation/destruction.
The separate replacement model exposes outstanding resource effects and death
interleavings; actual suspension and `current_thread` publication still require
runtime integration evidence.
No helper scenario asserts that the abstract copy service exists. No direct
production-helper counterpart exists for modeled copy timeout/write authorization;
that absence remains a product blocker, not model coverage.

Build the existing `dserver_exec_completion_tests` target and run
`ctest --test-dir BUILD -R '^dserver_exec_completion$' --output-on-failure`,
then run the model matrix above. Guest main-thread/pthread/fork/failed-exec/successful-
exec and delayed monitor integration require a newly built normal installation;
helper/model results remain host/model evidence only.

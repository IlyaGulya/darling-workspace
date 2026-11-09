# Supervisor Task and copy-freeze contract

This is a bounded abstraction of the production Rust supervisor, not a secure
copy service and not installed-runtime evidence. All expected results below
must be exercised by the integration owner; they are not recorded run results.
No configuration removes all writers to manufacture a protected-copy success.

## Source/action mapping

| Source | Model action/state |
| --- | --- |
| `task.rs::Stop` and `Task::observe_stop/event_stop/resumed` | `stop`: run, trace, group, listen; `InterruptResult`, `Release` |
| `Task::Life`, newborn initial event | `newborn`, `NewbornResult` |
| `Task::Interrupt`, successful `PTRACE_INTERRUPT` then later wait report | `pending`, `InterruptBegin` / `InterruptResult` |
| `Task::exit_stop/freeze_action`, `Core::freeze`, de_thread dependency | task 2 at EXIT; `ResumeExit` / `ReapExit`; task 1's interrupt result waits for sibling reap |
| `Task::Vfork`, `Core::freeze` widening to tree | `vfork`, `Scope`, task 1 blocked and task 3 separately interruptible |
| `Core::status(EXEC)` image rotation, `Core::execute` token check | `ExecResult`, `image`, `requested`, `CopyResult` / `RejectCopy` |
| `Core::freeze` completed scan and `process_vm_*` call/result | `Ready`, `BeginCopy` / `CopyResult` |
| another mm sharer, backing-object/files-domain mutation, outstanding kernel write | `ExternalWrite` interleaving between copy begin and result |

Production state transitions are private, fixed-size enums: lifetime is separate
from stop state, vfork dependency and interrupt state. Successful ptrace resume
(or the existing ESRCH completion policy) commits `Task::resumed`; interrupt
pending commits only after a successful kernel request. Genuine group stops
resume with LISTEN, not SYSCALL; newborn initial stops are not genuine group
stops. An EXIT stop overrides group selection and progresses before freezing.
The synchronous mutex/condvar slot and C ABI are unchanged.

There are three bounded tasks: tasks 1 and 2 share a TGID, task 3 has a different
TGID. Task 2 starts at an EXIT stop; task 1 represents the surviving/executing
member after identity rebasing. Image epochs are bounded to 1 and 2, one copy
request and one writer effect. `ExecResult` abstracts the observed exec event
AFTER the sibling is reaped, rather than atomizing its dependency with EXIT.
The initial newborn and genuine group-stop arms are separate configurations.
The model does not prove PID rebasing, signal delivery counts, SIMD preservation,
whole thread-group image bookkeeping, arbitrary descendant admission, or Linux
syscall semantics: those are native workload obligations.

`InterruptBegin`/`InterruptResult`, `ResumeExit`/`ReapExit`, and
`BeginCopy`/`CopyResult` are distinct transitions. The owner does not process
another status inside the synchronous process_vm call; independent writers can
still act. For `files`, the modeled effect is a writer through a shared file or
backing object that can change copied mappings, not the false claim that every
shared file table directly aliases anonymous memory.

`InvExitProgress` is bounded enabledness, not temporal termination. Actual
progress additionally assumes scheduling and eventual delivery of enabled
kernel stop/reap results. No unconditional liveness claim is made for blocked
kernel IO. `CHECK_DEADLOCK FALSE` permits deliberately terminal done states;
progress failures are checked explicitly, not silently accepted as termination.

The `kernel` arm deliberately grants the stronger full-owned-tree stop (all
three tasks), even without a vfork dependency. This is a conservative security
experiment, not a claim that ordinary group-copy requests acquire that scope.
Its expected counterexample shows why even that stronger stop is insufficient.

## Explicit case matrix

| Config | Invariant | Expected | Meaning |
| --- | --- | --- | --- |
| guarded | InvLimitedFreeze | OK | selected live tasks stopped or vfork-blocked |
| guarded | InvGroupPreserved | OK | real group stop survives interrupt and release |
| guarded | InvExitProgress | OK | no stranded de_thread EXIT dependency |
| newborn | InvBirth | OK | initial stop is consumed before copying |
| newborn | InvCopyBinding | OK | stale request rejected after exec epoch rotation |
| newborn | InvLimitedFreeze | OK | newborn does not bypass stop wait |
| vfork | InvVforkScope | OK | blocked parent widens to owned tree |
| vfork | InvExitProgress | OK | widening does not strand EXIT |
| no-group | InvGroupPreserved | VIOLATED | lost LISTEN restoration releases real group stop |
| no-exit | InvExitProgress | VIOLATED | sibling held at EXIT prevents next result |
| no-wait | InvLimitedFreeze | VIOLATED | interrupt request is not stop acknowledgement |
| no-binding | InvCopyBinding | VIOLATED | stale epoch accepted after exec |
| no-tree | InvVforkScope | VIOLATED | vfork peer outside TGID remains running |
| guarded | InvProtectedCopy | VIOLATED | other-TGID mm sharer writes during copy |
| files | InvProtectedCopy | VIOLATED | backing/files-domain writer writes during copy |
| kernel | InvProtectedCopy | VIOLATED | outstanding kernel effect survives task stopping |

Run from the manifest with the existing shared runner (no new runner):

```sh
bash audits/rpc-plane-model/run-all.sh \
  --jar "$TLA_JAR" \
  --output-root "$DW_STATE_ROOT/supervisor" \
  --model-dir audits/process-supervisor-model --module Supervisor --workers 2 \
  --case guarded.cfg:InvLimitedFreeze:OK \
  --case guarded.cfg:InvGroupPreserved:OK \
  --case guarded.cfg:InvExitProgress:OK \
  --case newborn.cfg:InvBirth:OK \
  --case newborn.cfg:InvCopyBinding:OK \
  --case newborn.cfg:InvLimitedFreeze:OK \
  --case vfork.cfg:InvVforkScope:OK \
  --case vfork.cfg:InvExitProgress:OK \
  --case no-group.cfg:InvGroupPreserved:VIOLATED \
  --case no-exit.cfg:InvExitProgress:VIOLATED \
  --case no-wait.cfg:InvLimitedFreeze:VIOLATED \
  --case no-binding.cfg:InvCopyBinding:VIOLATED \
  --case no-tree.cfg:InvVforkScope:VIOLATED \
  --case guarded.cfg:InvProtectedCopy:VIOLATED \
  --case files.cfg:InvProtectedCopy:VIOLATED \
  --case kernel.cfg:InvProtectedCopy:VIOLATED
```

## Native consumer case and remaining scope

The existing `dserver_process_supervisor_tests` workload retains its original
copy/short fault/concurrent caller, fork admission, failed exec, real group stop,
blocking read/SIMD/EINTR/SA_RESTART, leader/nonleader exec, surviving group-leader
exit, image revocation and teardown oracles. A new separate-stack
`CLONE_VM|CLONE_VFORK` child blocks on the existing pipe. Requests while its
parent is vfork-blocked exercise child copying, parent binding retention, tree
freeze and subsequent child-exit/VFORK_DONE progress. No expected internal stop
state is forced: parent transfer may succeed at an observed trace stop or report
ESRCH if only the vfork-blocked parent is available. Both preserve the existing
public API's behavior.

Required protected-runtime scope remains wider than this refactor: enumerate
and exclude all mm/files-domain writers (including outside owned descendants),
handle outstanding asynchronous/kernel mutations, and bind any resulting
quiescence proof to the image/domain epochs and copy lifetime. Group/tree
stopping alone does not establish those properties. The three protected-copy
counterexamples deliberately remain expected failures of that stronger claim.

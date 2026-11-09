# Exec replacement: resource ownership across suspension

This bounded safety model corresponds to darlingserver `ExecReplacement` and
`Process::notifyCheckin` / `Process::notifyDead`. It is not an installed-runtime
proof, a memory-copy service, or a claim that all external writers are stopped.

## Production protocol and source mapping

All phase transitions are serialized by `Process::_rwlock`; the owner retains
exclusive replacement ownership while that lock is dropped. `ExecCompletion`
(pipe identity, attempt IDs, image generations and EOF semantics) is unchanged.

| Model action | Production boundary |
| --- | --- |
| `Begin` | `begin()` admits one owner, entering `ClearingThreads`, before the thread-clear loop drops the lock. |
| `ClearResult` | `threadsCleared()` enters `Allocating` after non-main threads have been removed and the surviving main thread restored; task/thread/semaphore allocation and old semaphore destruction execute unlocked. |
| `AllocateResult` | `allocationComplete()` enters `Allocated` on reacquiring the lock after all new handles exist and old semaphores have been destroyed. |
| `Publish` | The coherent assignment block repoints `_dtapeTask`, `mainThread->_dtapeThread` and every semaphore before `published()` enters `Published`. If death won, new task memory authority is retired before these assignments. |
| `ReleaseBegin` | `beginPredecessorRelease()` enters `ReleasingPredecessor` under the lock, then the lock is dropped before `dtape_thread_release(oldThread)` / `dtape_task_release(oldTask)`. |
| `ReleaseResult` | `predecessorReleased()` enters `PredecessorReleased` only after both releases return and the lock is reacquired. |
| `Finish` | `finish()` requires `PredecessorReleased`, returns to `Idle` and reports deferred death. |
| `Death` | `notifyDead` immediately sets `_dead`, retires `ExecCompletion`, and retires published task memory; `requestDeath()` moves `Live` to `Revoked`, deferring physical teardown while any replacement phase is active. Repeated death cannot steal that ownership. |
| `TeardownBegin` | An idle `requestDeath()` moves `Revoked` to `TeardownOwned`; only that caller drains threads and schedules physical task release. |
| `TeardownResult` | The kernel-async callback destroys the fork semaphore and releases the task unlocked, then records `teardownComplete()` under the lock (`Released`). |

The missing-main-thread error path uses `abandonClearedThreads()`, which is only
legal during `ClearingThreads`: no replacement handles have been allocated.
It returns ownership and drains deferred death, or preserves the existing fatal
missing-main error. This error path is tested by the host helper, not modeled.

The model represents effects separately from their begin actions: death can
interleave during thread clearing, allocation, predecessor release, and physical
teardown. It abstracts the bounded collection of threads and semaphores into one
completion boundary each; `current = "new"` means the whole publication block,
not only one pointer write. Atomic publication is justified by the process lock.

## Independently stated safety properties

* `InvAuthority`: dead processes never expose live memory authority.
* `InvPublication`: a published current thread has an allocated replacement.
* `InvCurrentThread`: physically released predecessors are never current.
* `InvFinish`: completed replacement has no outstanding effect, has published a
  successor, and has released its predecessor.
* `InvExclusive`: physical teardown cannot overlap an owned replacement effect.
* `InvExactlyOnce`: physical teardown completes at most once.

Each unsafe configuration weakens one transition, not the invariants. Expected
counterexamples are: premature publication while allocation is outstanding;
premature predecessor release while `current` is old; finish while allocation
is outstanding (also permits physical teardown racing that effect); dead
publication restoring authority; and a second physical teardown after completion.

## Exact parent-run matrix

From the canonical manifest, run the existing shared isolated runner. It checks
each listed invariant separately and requires both the expected TLC exit status
and a complete success/counterexample log. No model execution was performed by
the implementation agent.

```sh
audits/rpc-plane-model/run-all.sh \
  --jar "$TLA_JAR" \
  --output-root "$DW_STATE_ROOT/exec-replacement" \
  --model-dir audits/exec-replacement-model --module ExecReplacement \
  --case guarded.cfg:InvAuthority:OK \
  --case guarded.cfg:InvPublication:OK \
  --case guarded.cfg:InvCurrentThread:OK \
  --case guarded.cfg:InvFinish:OK \
  --case guarded.cfg:InvExclusive:OK \
  --case guarded.cfg:InvExactlyOnce:OK \
  --case publish-dead.cfg:InvAuthority:VIOLATED \
  --case publish-early.cfg:InvPublication:VIOLATED \
  --case release-early.cfg:InvCurrentThread:VIOLATED \
  --case finish-early.cfg:InvFinish:VIOLATED \
  --case finish-early.cfg:InvExclusive:VIOLATED \
  --case double-teardown.cfg:InvExactlyOnce:VIOLATED
```

Host integration uses existing CMake target `dserver_exec_completion_tests` and
CTest case `dserver_exec_completion`. After the parent builds the integrated
server target in its selected build tree:

```sh
ctest --test-dir "$SERVER_BUILD" -R '^dserver_exec_completion$' --output-on-failure
```

The binary must emit `DSERVER_EXEC_COMPLETION_CONTRACT_OK`. Besides existing pipe
cases it exercises death at all six owned replacement boundaries, repeated
deferred and terminal death, exclusive admission, two successful replacements,
missing-main ownership return, and rejected early allocation completion,
publication, predecessor release, physical teardown completion and finish.

## Bounds and assumptions

One process, one exec replacement, two delivered death notifications, one
aggregate predecessor and one aggregate successor are modeled. The actual helper
also tests successive live replacements. No fairness or liveness assertion is
made: dtape/XNU work can suspend indefinitely; arbitrary blocked kernel IO is not
assumed to finish. Kernel effects are assumed to complete as called without
throwing, and valid allocation calls are assumed to return valid handles, as in
the existing production code. Crash recovery, allocation failure unwinding,
shared-mm/files mutation and supervisor stop guarantees are outside this model.
`CHECK_DEADLOCK FALSE` permits deliberately stalled effects and terminal states;
it does not assert progress.

The checked API enforces the legal sequence, not the truth of arbitrary caller
claims. Source mapping matters: production performs pointer publication before
`published()` and physical release before `predecessorReleased()`. Real dtape
suspension and `current_thread()` behavior still need parent-owned runtime
interleaving/ASan verification. Existing trace points are retained; no diagnostic
fault hooks or new runtime services are introduced.

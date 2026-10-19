# Handoff — plane deferred-arm RED/GREEN gate (to the Xcode workstream)

Status: the RED gate is corrected, self-tested offline, and ready for the coordinated
GREEN run. No product code was touched by this work. Branch:
`audit/plane-deferred-arm-2026-10-19`.

## 1. What changed since the accepted RED evidence

The first gate treated `call-deferred site=setPendingCall-parked` as proof of the defect
and required zero occurrences for GREEN. That is wrong: the diagnostic fires at **arm
time**, before the dispatcher is reached, so a correctly disarmed Call still produces it.
The gate now reads the lifecycle as phases and correlates them by request identity
(the run's `pid`, which equals the main thread's `tid`, plus callnum 16).

| phase | record | RED (unmodified) | GREEN (disarm-on-refusal) |
|---|---|---|---|
| P1 initial temporary arming | `setPendingCall-parked … tid=P number=16 … pending=0` | ≥ 1 | **≥ 1 (not zero)** |
| P2 dispatch refusal | (unlogged today) | — | — |
| P3 disarm | `plane-call-disarmed tid=P call=16 …` (**new diagnostic**) | absent | ≥ 1 |
| P4 retention after refusal | `setPendingCall-parked … tid=P number=16 … pending=1` | ≥ 1 | **0** |
| P5 deferred insertion | `doWork-parked … tid=P number=16` | ≥ 1 | **0** |
| P6 late execution | `plane-call-unowned … call=16 …` (**new diagnostic**, emitted when the call runs) | absent* | **0** |
| P7 completion publication | `plane-deferred-completion` | 0 | 0 |

\* In RED the unowned completion happens but the unmodified build has no record for it;
P4/P5 plus the absent `plane-deferred-completion` carry the RED verdict. The GREEN
"completion ownership" assertion is exactly `plane-call-unowned == 0`.

Re-publication rejection (`setPendingCall rejected call (number 16)`) is counted from the
run-scoped server delta, because that record carries no pid in the current build.

## 2. The minimum gated diagnostics to add (product agent)

Two unconditional, bounded (first 8) records and one field:

1. **Disarm** — in `Thread::dispatchOrQueuePlaneCall`, on the `!queueIfBusy` refusal, after
   clearing the slot:
   `plane-call-disarmed tid=%d call=%u pending_before=%d`
   (one category literal, same style as `call-deferred`).
2. **Unowned completion** — in `Thread::pushCallReply`, when
   `expectedCall->replySuppressed() && !expectedCall->planeCompletion().armed()`:
   `plane-call-unowned call=%u pid=%d seq=%u status=%d`.
   This is the terminal anomaly: the handler ran and the answer has no destination.
3. **Attribution** — add `tid=%d pid=%d` to the existing
   `setPendingCall rejected call (number …)` line (`src/call.cpp`), so a rejection is
   attributable without relying on the run-scoped delta.

Without (1), `--oracle product --expect green` returns **INVALID** (the GREEN ownership
assertion cannot be evaluated), never PASS. Without (2), the "completion ownership" half of
GREEN is unverified.

## 3. Final invocation

RED, on a private runtime built from the unmodified sources (the deployed server must carry
the `call-deferred` instrument; pin the binary):

```sh
tests/run-plane-deferred-arm-contract.sh \
  --prefix <private-prefix-or-lane> \
  --build-dir <build-tree-with-guest-toolchain> \
  --expect red --oracle existing --repeat 3 \
  --expected-server-sha 90e4eee4b6db4f131d1aa5a6a8835dc344528887084d8222127f8827825a2b1b
```

GREEN, after the correction is built and deployed to the same private runtime, with the
three diagnostics present:

```sh
tests/run-plane-deferred-arm-contract.sh \
  --prefix <private-prefix-or-lane> \
  --build-dir <build-tree> \
  --expect green --oracle product --repeat 3 \
  --expected-server-sha <sha256 of the corrected deployed darlingserver>
```

Both runs are host-only except for the prefix boot; the fixture is built from the build
tree's own `ring_mach_msg_test` compile/link commands, so no product target is added.

## 4. Acceptance criteria

A run is **PASS** only when all of the following hold, per repeat:

1. `PDA status=0` (the guest completed; both opens succeeded) and the harness reported
   `VERDICT: PASS`.
2. The harness named the expected prefix in `RUN-ENV`; the bundle has `server.pre.log` and
   `server.post.log`; the run's pid appears in the delta.
3. The deployed `bin/darlingserver` sha256 equals `--expected-server-sha`.
4. The phase signature matches the table above.

GREEN additionally means, semantically:

* **the refused Call cannot execute later** — P5 (`doWork-parked`) is zero, and for the
  product oracle `plane-call-unowned` is zero;
* **no duplicate application** — only one publication completes; the retries that the RED
  server answered with `-EAGAIN` do not happen (`P4 == 0`, rejection `== 0`);
* **correct guest-visible result** — `PDA status=0`; the first plane request is served (no
  datagram fallback is needed for the *first* open). Note: the current fixture cannot
  distinguish "served by the plane" from "served by the fallback" guest-side, because for
  `CONSOLE_OPEN` the fallback runs on the worker's thread and succeeds by design. The
  server-side `plane-call-disarmed`/`plane-call-unowned` pair is the load-bearing half;
* **correct completion ownership** — `plane-call-disarmed ≥ 1` and
  `plane-call-unowned == 0`;
* **resource cleanup** — a disarmed Call is destroyed; the fixture's op carries no request
  descriptor, so the gate's cleanup evidence is the absence of a deferred execution plus the
  harness's post-run `prefix processes after: 0, mounts: 0` line.

## 5. Test safety (implemented)

* Exit codes: `0` PASS, `1` FAIL, `2` INVALID. **INFRASTRUCTURE FAILURE IS NEVER PASS.**
* The guest's own status is a declared harness status field (`--status-marker 'PDA status='`),
  so a missing or non-zero guest status fails the arm rather than looking like a verdict.
* An absent workload, a boot failure, a missing server pre/post log, a provenance mismatch, a
  server crash/panic, or a changed deployed sha all classify **INVALID**.
* The first line of the verdict prints the prefix, the deployed sha and the run directory.
* The gate refuses `prefix-verify14` and the canonical `prefix-ios` by name.

### Isolation limitation (measured, unresolved)

* **Prior incident.** The first RED run (2026-10-19 ~14:44) used `prefix-verify14` while the
  active Xcode workload was running there; the harness's prefix-scoped cleanup killed two of
  that workload's processes. This is recorded in `evidence/plane-deferred-arm/README.md`
  and is the reason the gate now refuses that prefix.
* **Lanes.** Two isolation lanes were attempted, both over immutable bases carrying the same
  probe server (`sha256 90e4eee4…`):
  * over `prefix-ios-base` — boot failed: `cannot remove relative prefix entry: No such
    device or address` (stale shellspawn socket in the overlay);
  * over `prefix-ios-clean` — the server started and serviced plane ops, but
    `shellspawn did not become ready within 30000ms`, so no workload ran.
* **Fresh copy.** `west darling-fresh-prefix --from prefix-ios-clean --path
  /tmp/darling-fresh-prefix-plane-audit` copied 1.3 GB, but the copied lifecycle state
  belongs to the source prefix (`runtime prefix state belongs to another prefix: recreation
  required`), and no supported command rebinds it.
* Consequence: **no further live run was attempted by this milestone.** Repeated RED is
  established by two earlier runs against the same unmodified binary (the 14:44 run with the
  `semaphore_timedwait` trap and the 15:00 run with the Mach-receive trap; both show P1, P4,
  P5 and no owned completion). The corrected oracle is proven by
  `tests/run-plane-deferred-arm-oracle-selftest.sh`, which replays the real captured RED
  delta and synthetic GREEN deltas. The live GREEN run needs a bootable private runtime;
  resolving the lane/fresh-copy boot is the remaining infrastructure task and it is assigned
  to the corrected build's owner, not to the product change.

## 6. Arm-after-queue: separate, not reproduced

The second window (queue → worker drain → completion → `setPlaneCompletion` too late) was
**not** reproduced against a live server and must not be reported as reproduced. It is
modelled only: `audits/rpc-plane-model`, configuration `arm-after-queue`, violating
`Inv7QueuedCallHasOwner` (the atomicity requirement) and consequently `Inv3`/`Inv4`.
Its real RED proof remains pending; the model counterexample is the acceptance predicate for
the atomicity half of the correction.

## 7. Artifacts

| path | contents |
|---|---|
| `tests/plane_deferred_arm_guest.c` | fixture (control/park arms, `PDA status=`) |
| `tests/run-plane-deferred-arm-contract.sh` | live gate + `--analyse` oracle mode |
| `tests/run-plane-deferred-arm-oracle-selftest.sh` | host-only oracle proof (10 cases) |
| `evidence/plane-deferred-arm/` | matched RED trace, verdict, run logs, provenance |
| `audits/rpc-plane-model/` | TLA+ model, configs, counterexamples |
| `audits/darlingserver-rpc-plane-audit-2026-10-19.md` | the audit and its plan |
| `audits/darlingserver-rpc-plane-audit-backlog.md` | the remaining findings with preconditions |

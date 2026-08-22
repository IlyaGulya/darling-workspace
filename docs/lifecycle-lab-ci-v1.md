# Lifecycle Lab CI

Lifecycle Lab CI schedules the existing Rust reducer, explorer, fuzz, real-kernel and guest-ready contracts. It does not interpret lifecycle events and does not provide another oracle.

## Ownership and routing

`.github/workflows/lifecycle-lab.yml` directly lists every required command. Pull requests and pushes run the deterministic boundary, trace/recovery, explorer and fuzz contracts. The schedule runs independent ASan, TSan, Miri and bounded fuzz campaigns. The protected `lifecycle-landing` environment runs real-kernel followed by guest-ready acceptance.

`DARLING_LIFECYCLE_COHORT_V1` remains OFF and product routing remains DEFERRED. The contour does not mutate profiles, locks, refs, product sources or runtime defaults.

## Runner

`ci/run-lifecycle-lab.sh` executes exactly one caller-supplied command. Before spawn it subscribes to the Linux process connector and records every kernel fork event with TGID/starttime identity and a retained pidfd. That exact ledger bounds aggregate RSS and owns termination even after `setsid()`, double-fork, exec or environment clearing, without changing parentage, session or command resource policy. Lost kernel events fail cleanup closed. It returns the command exit code. Timeout/resource failures return 124 and cleanup failures return 125. An unavailable process-event authority fails closed.

The runner prints one stable diagnostic line:

```text
LIFECYCLE_LAB_RESULT status=PASS|FAIL reason=... exit_code=N
```

`--result PATH` optionally writes one short `result.json` containing status, reason, exit code, elapsed time, peak RSS and cleanup counts. It is diagnostic only. Git SHA and the hosted workflow run are the source provenance; ordinary execution has no self-verifying manifest, source checksum inventory, artifact allowlist or checksum package.

## Budgets

The workflow supplies explicit YAML arguments used by every command: 900 seconds, 12288 MiB aggregate process RSS, 4 MiB output, 30 seconds cleanup, 32 failure artifacts and 64 MiB total failure artifacts. Fuzz commands additionally retain their existing 512-input, 64-event, 60-second libFuzzer and 2048 MiB libFuzzer limits.

Unsupported sanitizer or toolchain exits nonzero and therefore fails the job; it is never translated into success. A crash that produced corpus/minimized evidence remains a failed command.

## Local use

```text
ci/run-lifecycle-lab.sh --result /tmp/result.json -- tests/run-lifecycle-operation-boundary-contract.sh
ci/run-lifecycle-lab.sh --failure-artifacts /tmp/failure -- ci/run-lifecycle-fuzz-campaign.sh asan
```

On success, staged corpus and command output are removed with the task root. On failure, `--failure-artifacts` retains the bounded command log plus any staged corpus/minimized inputs. CI uploads that directory only from a failed job.

## Triage and replay

1. Use the failing workflow run SHA and exact failing command printed by Actions.
2. Inspect the optional result for timeout, RSS, output or cleanup classification.
3. Replay the exact command locally through the runner. Replay fuzz findings through the ordinary deterministic Rust explorer/verifier before changing product code.
4. Retain deterministic failure diagnostics for 7 days and scheduled/landing failures for 30 days.
5. Escalate escaped processes, mounts, sockets, task roots or unreaped children as CI infrastructure defects. Escalate a minimized lifecycle invariant failure as its own Bead; do not repair product semantics inside the CI contour.
6. Never rerun a failed hosted campaign until its first failure has been classified.

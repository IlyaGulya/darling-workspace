---
name: darling-boot
description: >-
  Safely run Darling boot and guest smoke tests using the current prefix-scoped
  lifecycle tools. Use before guest launches, shellspawn, clang-in-guest, or DCC
  smoke tests. Prevent overlapping boots and orphaned prefix processes without
  touching other sessions.
---

# Darling boot: verify ownership, run once, verify cleanup

This skill is operational guidance, not a snapshot of the current workspace.
Re-check the current repository rules, CLI help, runtime profiles, and lifecycle
implementation before using it. If they disagree, fix this skill rather than
following stale commands.

## Source and activation

The canonical source is `.claude/skills/darling-boot/SKILL.md` in the selected
manifest repository. `skill://darling-boot` is the harness's active installation,
not necessarily that checkout. Resolve its physical backing path and any
workspace skill aliases before editing; synchronize separate active copies from
the canonical source. Do not treat installed guidance as runtime
configuration, and do not search conversation archives for installation copies.

## Identify the exact runtime

- Resolve the West workspace, manifest repository, install prefix, guest prefix,
  launcher, build directory, and runtime profile from current configuration.
  The install prefix and guest prefix may differ. Do not substitute a remembered
  home directory, default prefix, setuid launcher, or runtime mode.
- From the manifest repository, use `mise run dw ...` for the managed interface
  and `mise run west ...` for advanced West commands. Both tasks select the pinned
  mise environment for every command and forward arguments without an extra
  separator. From the West root use `mise -C darling-workspace run dw ...` or
  `mise -C darling-workspace run west ...`. Do not invent managed aliases for
  advanced West interfaces.
- Inspect `mise run west darling-doctor --help` before choosing its flags.
  `--prefix` names the install prefix; `--extra-prefix` checks an additional
  runtime/test prefix. `--scope workspace` checks manifest/source drift;
  `--scope runtime` checks build/deployment, boot prerequisites, baseline, and
  extra prefixes; `--scope all` is the default combined audit. Bootstrap runs
  workspace scope before provisioning and runtime scope afterward against the
  deployed prefix/build with `--no-baseline-file`. This deliberate split avoids
  treating an empty prefix as a broken deployed runtime; it does not waive drift.
  Diagnose unexplained source/build/deploy mismatches rather than adding broad
  exclusions or presenting a scoped pass as a full audit.
- A bootstrap doctor failure retains full JSON and stdout/stderr under
  `diagnostics/bootstrap-*.json` and `diagnostics/bootstrap-*.{stdout,stderr}.log`
  in the reported runtime evidence archive, including command, return code and
  timeout state. Read that evidence, not only the bounded console summary.
- A declared runtime RED proof intentionally deploys bad artifacts. Its runner
  must validate the exact source/artifact plan and own backup, restoration, and
  fixed-runtime verification against the actual declared artifacts.

## Prefer the supported runner

For the four supported scenarios, configure an owned prefix once, then use the
managed entrypoint from the manifest repository:

```bash
mise run dw dev context homebrew --prefix <owned-absolute-prefix>
mise run dw dev run homebrew-prepare
mise run dw dev run homebrew-preflight
mise run dw dev run homebrew-source
mise run dw dev run exact-capture
```

These are separate operations, not an instruction to run every scenario for
every task. `homebrew-prepare` provisions and proves the retained provider;
preflight/source reuse it, and exact capture diagnoses the retained provider.
The default runtime profile is `homebrew-lz4-source`; Homebrew scenarios require
that profile. The default executor is the West-root sibling
`darling-debug-runner/target/release/darling-debug-runner`, built incrementally
with `cargo build --release` before starting the job. Evidence defaults to
`<workspace-parent>/darling-debug`. Context accepts `--runtime-profile`,
`--executor`, and `--bundle-root`; run accepts these overrides plus `--prefix`
and `--context`. An explicit executor bypasses the automatic runner build:
confirm its provenance instead of assuming it matches current source.

`mise run dw dev run <scenario> --dry-run` prints the resolved West command without
building, creating a prefix/job, or launching a guest. Context configuration is
a separate local write. A real run prints `JOB=<state-dir>` and follows by
default; `--detach` starts without following. Reconnect with
`mise run dw dev follow <state-dir>`; request cancellation with
`mise run dw dev cancel <state-dir>`, then follow to final status and verify cleanup.

For advanced registered guest tests, use `mise run west test ...` with
explicit prefix/profile and supported diagnostic flags. Current metadata prefix
tests acquire `$DPREFIX/.west-test.lock` and use a prefix-scoped lifecycle owner.
Check current implementation before relying on these details. Do not start a
competing manual boot or cleanup command.

Supervise advanced finite runs with:

```bash
scripts/west-job.sh start --state-dir <unique-absolute-dir> -- mise run west test <supported-arguments>
scripts/west-job.sh follow --state-dir <same-dir>
```

Do not substitute manual `nohup`, `setsid`, tail loops, or repeated status polls.
An observer timeout does not stop the job. Resume `follow`; use one-shot `status`
after a transport interruption only to recover state. Do not infer completion
from a detached tool handle. Do not start a second prefix-backed run until the
first has a final exit status and its prefix cleanup has completed. Follow the
active harness's process-supervision rules where it requires another transport.

## Lifecycle invariants

1. Establish that no other run owns the selected prefix. Use the current runner's
   lock and process-identity tracking; process names alone do not prove ownership.
2. Request graceful shutdown through the current prefix-scoped lifecycle path.
   Verify leftover processes and mounts for that prefix even if shutdown exits 0.
3. Let that lifecycle owner handle verified leftovers. NEVER use global
   `pkill`, `killall`, or `pgrep | xargs kill` for darlingserver, mldr,
   launchd, or vchroot. An orphan must be tied to the selected prefix/namespace
   before any termination. If ownership cannot be established, stop and retain
   diagnostics rather than guessing.
4. Allow teardown to settle. Start one managed boot and observe the actual
   readiness/verdict condition. A created process or socket alone is not proof
   that the requested guest operation passed. Avoid tight retry/kill loops.
5. On failure, preserve diagnostics and complete prefix-scoped teardown before
   another attempt. Do not layer a boot over a half-dead runtime.

Use `--keep-prefix-running` only for deliberate local iteration with explicit
ownership; it is not a workaround for cleanup failures.

## Logs, toolchain integrity, and exact capture

- The runner flushes `BUNDLE=<path>` before execution and automatically registers
  bundle `stdout.log`/`stderr.log` with the managed job's live follower before
  spawning the payload. No manual activity-log registration or tail loop is
  needed. Retain the reported bundle, process state, and final verdict. Log silence alone
  is not evidence of a hang. A timeout or heartbeat describes observation, not
  the cause; diagnose the preserved runtime state before retrying.
- Let the supported guest-toolchain provisioner verify CLT inputs. For
  catalog-backed Apple packages, `digest` is SHA-1 of the compressed XAR TOC, not SHA-1 of the
  whole `.pkg`. Provisioning checks package size/XAR structure, that TOC digest,
  and the reviewed whole-package SHA-256 allowlist. Do not weaken verification,
  compare the catalog digest to whole-file SHA-1, or delete a valid cache on
  that mistaken comparison. A separately selected CLT package has its own
  reviewed whole-file SHA-256 requirement; do not substitute packages or
  fabricated stamps.
- Derive SDK identity and supported versions from authoritative package metadata
  and the actual installed SDK. Preserve compatibility gates; no fake metadata,
  symlinks, or version claims may turn a mismatch into a pass. Record blockers
  in Beads with evidence. Runtime preparation does not establish Homebrew source
  acceptance.
- `exact-capture` intentionally expects the guest payload to time out after its
  readiness marker. Diagnostic PASS requires a checksummed guest Mach-O image,
  matching valid core payload, thread registers, and prefix cleanup; a payload
  that exits normally is not the expected result. Report the diagnostic verdict
  separately from archive-wide `exact_complete`. A valid diagnostic can report
  `exact_complete=false` for unreadable mappings such as `[vsyscall]`; retain
  the manifest's omissions/errors rather than claiming a complete archive.

## Repair and completion

- Missing tmp directories or CLT links: use
  `mise run west darling-prefix-repair --prefix <guest-prefix> --check`,
  then the supported repair command when necessary. Do not improvise mkdir/ln
  repairs. Check the current test profile's toolchain provisioning first.
- For stale mounts, first establish that no live process owns the prefix, then
  use `mise run west darling-prefix-repair --prefix <guest-prefix> --cleanup-mounts`.
  Check its result; do not ignore mount tails or glob-delete debug prefixes.
- Record the final command status, guest-visible verdict, and prefix-scoped
  process/mount cleanup. A cleanup failure keeps the run failed or blocked.
- Never compare against hashes copied from this skill. If deployment is involved,
  capture the actual pre-run artifacts and let the supported deploy/restore
  workflow verify them. Do not overwrite unrelated prefixes or production state.
- Use the supported runtime deployment transaction for file replacement and
  rollback. It stages files beside their destinations and atomically renames
  them; do not overwrite a running launcher/server in place or bypass ownership
  checks to work around `ETXTBSY`. Atomic replacement does not authorize deploying
  into another run's prefix. Bootstrap retains a provider only after its gates
  pass; transactional test deployments must satisfy their restoration checks.

## Implementation anchors to re-check

- `mise.toml`, `bin/dw`, and `west_commands/dev_scenarios.py`: task proxies, contexts, scenarios, follow/cancel.
- `west_commands/doctor.py` and `test_bootstrap.py`: scoped checks and full evidence.
- `west_commands/guest_toolchain.py`: reviewed CLT integrity requirements.
- `west_commands/test_diagnostics.py`: diagnostic versus archive completeness.
- `west_commands/deploy_transaction.py`: atomic replacement and rollback.
- Sibling `darling-debug-runner`: automatic live logs and exact capture.
- `west_commands/test.py`: test orchestration and prefix integration.
- `west_commands/test_prefix.py`: prefix lifecycle owner and process identity.
- `west_commands/test_guest_execution.py`: bounded guest transport/shutdown.
- `west_commands/darling_prefix_repair.py` and `prefix_repair.py`: repair CLI.
- `scripts/west-job.sh`: supervised finite-job transport.

These locations can move. Locate their current replacements before acting.

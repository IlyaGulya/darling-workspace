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
following stale commands. Historical perf#23a explains the risk of rapid
boot/kill churn; it is not evidence that a current failure has the same cause.

## Identify the exact runtime

- Resolve the West workspace, manifest repository, install prefix, guest prefix,
  launcher, build directory, and runtime profile from current configuration.
  The install prefix and guest prefix may differ. Do not substitute a remembered
  home directory, default prefix, setuid launcher, or runtime mode.
- Run project CLIs through the workspace's mise environment. From the manifest
  repository use `mise exec -- west ...`; from the West root use
  `mise -C darling-workspace exec -- west ...`.
- Inspect `west darling-doctor --help` before choosing its flags.
  `--prefix` names the install prefix; `--extra-prefix` checks an additional
  runtime/test prefix. Diagnose unexplained source/build/deploy mismatches before
  booting. Intentional profile drift must be identified and validated by the
  current materialization/deployment workflow, not hidden with broad exclusions.
- A declared runtime RED proof intentionally deploys bad artifacts. Its runner
  must validate the exact source/artifact plan and own backup, restoration, and
  fixed-runtime verification; historical baseline hashes are not its oracle.

## Prefer the supported runner

Use `west test` for registered guest tests with an explicit prefix/profile.
Current metadata prefix tests acquire `$DPREFIX/.west-test.lock` and use a
prefix-scoped lifecycle owner. Check the current implementation before relying
on those details. Do not start a competing manual boot or cleanup command.

For long runs, use the current `scripts/west-job.sh` interface:

`scripts/west-job.sh start --state-dir <unique-absolute-dir> -- <mise-managed-command>`

Then stay attached with:

`scripts/west-job.sh follow --state-dir <same-dir>`

An observer timeout does not stop the job. Resume `follow`, or use `status`
after a transport interruption. Do not infer completion from a detached tool
handle. Do not start a second prefix-backed run until the first has a final exit
status and its prefix cleanup has completed. Follow the active harness's process
supervision rules when it provides a different required transport.

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

## Repair and completion

- Missing tmp directories or CLT links: use
  `mise exec -- west darling-prefix-repair --prefix <guest-prefix> --check`,
  then the supported repair command when necessary. Do not improvise mkdir/ln
  repairs. Check the current test profile's toolchain provisioning first.
- For stale mounts, first establish that no live process owns the prefix, then
  use `west darling-prefix-repair --prefix <guest-prefix> --cleanup-mounts`.
  Check its result; do not ignore mount tails or glob-delete debug prefixes.
- Record the final command status, guest-visible verdict, and prefix-scoped
  process/mount cleanup. A cleanup failure keeps the run failed or blocked.
- Never compare against hashes copied from this skill. If deployment is involved,
  capture the actual pre-run artifacts and let the supported deploy/restore
  workflow verify them. Do not overwrite unrelated prefixes or production state.

## Implementation anchors to re-check

- `west_commands/test.py`: test orchestration and prefix integration.
- `west_commands/test_prefix.py`: prefix lifecycle owner and process identity.
- `west_commands/test_guest_execution.py`: bounded guest transport/shutdown.
- `west_commands/darling_prefix_repair.py` and `prefix_repair.py`: repair CLI.
- `scripts/west-job.sh`: supervised finite-job transport.

These locations can move. Locate their current replacements before acting.

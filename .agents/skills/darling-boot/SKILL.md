---
name: darling-boot
description: >-
  Safely prepare, launch, repair and stop Darling prefixes. Read before guest
  launches, shellspawn, clang-in-guest, DCC smoke tests, runtime deployment or
  prefix cleanup. Prevent overlapping boots and orphaned prefix processes.
---

# Darling boot

Canonical source: `.agents/skills/darling-boot/SKILL.md` in the manifest;
`.claude/skills` is a compatibility symlink, not a second source. Resolve active
skill aliases before editing. Verify commands against current help/configuration;
correct stale instructions rather than following copied paths or hashes.

## 1. Resolve ownership and prerequisites

- Read [environment](../../../docs/environment.md) before choosing source, build,
  install prefix, guest prefix, launcher or state root. Install and guest prefixes
  may differ. Keep short socket paths, but never put a prefix or job under `/tmp`.
  Expanded AF_UNIX paths must fit the 108-byte `sun_path`, including NUL; do not
  weaken the guard or change guest temporary-directory semantics to bypass it.
- Establish that no other run owns the prefix. Use supported locks and process
  identity, not names alone. Each concurrent experiment needs its own lane:
  [state ownership and lane safety](../../../docs/environment.md#durable-state-and-cleanup). Never write the inert base.
- Rootless is the launcher default. Privileged/rootfull mode is retired and
  requires `DARLING_FORCE_ROOTFULL_MODE=1`; a setuid-root complaint is not a reason
  to change a normal rootless installation.
- `--prefix` only selects a path. Bootstrap an absent/empty prefix in a standalone
  invocation before any selection; see [bootstrap](../../../docs/test-infra.md#bootstrap-a-fresh-prefix-before-a-prefix-backed-run).
  Baseline profile: `homebrew-rootless-bootstrap-minimal`; guest compiler profile:
  `homebrew-guest-toolchain-provisioning`. Do not combine bootstrap and CTest selectors.
- Guest CTest compiles inside Darling. Declare
  `guest-toolchain: darling-command-line-tools`; let the provisioner install the
  reviewed CLT via the guest installer. Never commit CLT packages or snapshots.
- Before installation, require size/XAR-boundary checks, compressed-TOC SHA-1 and
  independently reviewed whole-file SHA-256. Catalog SHA-1 is NOT whole-file SHA-1.
  Publisher authentication is separate from local hashing. A separately selected
  `DARLING_CLT_PACKAGE` also needs its reviewed SHA-256. Never weaken verification.
- Derive SDK/runtime identity and compatibility from authoritative metadata and
  installed contents. No fabricated versions, links or package-manager bypasses.
  Homebrew's native CLT 13.2 selection and older CLT 9.2/SDK 10.13 are not
  interchangeable. Record incompatibilities in the owning Bead.

## 2. Select the supported owner

From the manifest, use `mise run dw dev context NAME --prefix /absolute/path`,
then `mise run dw dev run SCENARIO`. Scenarios: `homebrew-prepare`,
`homebrew-preflight`, `homebrew-source`, `exact-capture`; do not run all four
unless the task calls for them. Homebrew scenarios use `homebrew-lz4-source`.
Contexts live in local West configuration, not portable runtime artifacts.
`--context` and explicit run overrides do not rewrite the active context.

Read `mise run dw dev run --help` for executor, profile, prefix and bundle
options. Without an explicit executor the checked-out runner builds incrementally
in release mode; an explicit executor requires provenance verification.
`--dry-run` prints the underlying command without building, booting or creating
a job. Real runs attach by default; `--detach` is explicit.

Retain the printed job path. Reconnect with `mise run dw dev follow JOB`;
cancel with `mise run dw dev cancel JOB`, then follow to final status.

For tests without a registered scenario, read `darling-testing` and use the
supervised interface, with paths beneath the owned durable state root:

```bash
scripts/west-job.sh start --state-dir /absolute/owned/job -- mise run west test ...
scripts/west-job.sh follow --state-dir /absolute/owned/job
```

No manual nohup/setsid/PID/rc-file recipes, follow filters, sleep-polling loops,
or external timeout/kill wrappers. Observer timeout is not cancellation;
a detached handle is not completion. Use one-shot `status` only for recovery
following interruption. Do not start the next prefix-backed run until final rc
AND cleanup of the previous one. Separate lanes may run concurrently.

## 3. Run once and inspect evidence

- Bootstrap runs doctor workspace scope before build/deploy and mandatory runtime
  scope after guest smoke; `all` is the ordinary combined audit. Read current
  `darling-doctor --help`: `--prefix` is install prefix, `--extra-prefix` adds a
  runtime prefix. Do not present a scoped pass as a full audit or waive drift.
- On failure read retained doctor JSON, stdout/stderr, command, rc and timeout
  under the reported evidence archive. A restored prefix after rollback is a
  different state and cannot explain the failed deployment by itself.
- Runner `BUNDLE` is published at creation, stdout/stderr are registered live,
  and Homebrew build-log directories register automatically. `--activity-log`
  is for additional logs; `--forward-output` is post-execution replay, not live
  streaming. Log silence, observed phase and last-write age do not prove a hang.
- Require the workload's own readiness/completion oracle. A process or socket,
  an intermediate diagnostic or a timeout is not a successful guest operation.
  Use `darling-diagnostics` for artifact identity and interpretation.
- Native-tools preflight is not Homebrew from-source acceptance. Exact capture
  deliberately times out the payload: success requires verified guest Mach-O,
  matching structurally valid ELF core, registers and cleanup. Report diagnostic
  PASS separately from archive `exact_complete`; preserve unreadable-mapping
  warnings such as Linux vsyscall pages.

## 4. Finish, repair or retain a blocker

1. Request graceful shutdown through the run's prefix-scoped lifecycle owner;
   verify remaining processes and mounts even when shutdown exits zero.
2. Let that owner terminate verified leftovers. Never globally pkill/killall
   darlingserver, mldr, launchd or vchroot. Tie each process to the selected
   prefix/namespace; unknown ownership means stop and retain evidence.
3. Do not boot over half-dead state or retry in a kill loop. Preserve failure
   diagnostics and finish teardown before another attempt. `--keep-prefix-running`
   is only deliberate owned local iteration, never a cleanup workaround.
4. Missing tmp/CLT prerequisites: use `mise run west darling-prefix-repair
   --prefix PATH` (inspect `--check` first), not undocumented mkdir/ln repairs.
   Stale mounts: confirm no Darling processes remain, then use the same command
   with `--cleanup-mounts`. A remaining process or mount keeps the run failed.
5. For completed debug prefixes, use `darling-rootless-debug-cleanup --path PATH
   --dry-run` before removal; add `--sudo` only for a reported ownership failure.
   Never glob-delete `/tmp/darling-rootless-*`: source worktrees also use the name.
6. Deployment/restore belongs to the supported transaction. No in-place overwrite
   of executing binaries, guessed baseline reset or writes into another owner's
   prefix. Atomic rename does not waive lifecycle ownership. Runtime RED proof
   must back up/restore every declared artifact and verify fixed-runtime return.
7. Record final command status, guest-visible verdict, evidence paths and cleanup.
   Use `darling-durability` to retain results; do not reset unrelated prefixes.

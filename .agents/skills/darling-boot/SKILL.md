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
  install prefix, guest prefix, launcher or state root. Declare an absolute owned
  `DW_STATE_ROOT`; place explicit prefix, bundle, evidence and job paths beneath
  it. The variable does not relocate arbitrary explicit paths or every legacy
  default. Never create prefixes, lanes, jobs or evidence archives under `/tmp`.
  Use short socket paths: pathname AF_UNIX addresses must fit `sun_path`,
  including NUL. Do not bypass guards or change guest temporary-directory semantics.
- Establish that no other run owns the prefix. Use supported locks and
  prefix/process identity, not names alone. Each concurrent experiment needs its
  own lane: [state ownership and lane safety](../../../docs/environment.md#durable-state-and-cleanup).
  Never write the inert base; a job supervisor is not a prefix-ownership lock.
- Rootless is the launcher default and needs host support for unprivileged user,
  mount and PID namespaces. A namespace denial is a host prerequisite failure,
  not evidence of prefix corruption. Privileged/rootfull mode is retired and
  gated by `DARLING_FORCE_ROOTFULL_MODE=1`; do not change setuid permissions to
  repair an ordinary rootless installation.
- Managed commands export the selected guest prefix. On a necessary direct
  launcher invocation, set `DPREFIX` explicitly: the path to `bin/darling` alone
  does not select its guest prefix, and the launcher otherwise defaults to
  `~/.darling`. `DARLING_PREFIX` alone is not the launcher's selector. The
  launcher now publishes `DARLING_PREFIX` from the retained validated prefix FD
  on both cold and warm paths. Do not normalize the caller’s `--prefix` before
  the launcher’s retained-FD/no-follow/`..` checks; that can hide the spelling the
  guards must reject. For guest-execution proof use the explicit `shell /bin/bash -c '...'` route
  with a real guest Mach-O/file operation; a bare `/usr/bin/true` is currently
  resolved as a host path under `/Volumes/SystemRoot`, so its rc0 is not proof of
  guest execution.
- Guest C/C++ CTest fixtures compile inside Darling. Declare
  `guest-toolchain: darling-command-line-tools` where required and let the
  provisioner install reviewed CLT through the guest `installer`. Never commit
  CLT packages or snapshots; a runtime-only smoke does not prove a compiler/SDK.
- Catalog packages are checked for size and XAR boundaries, compressed-TOC SHA-1
  and pinned whole-file SHA-256 before installation. Catalog SHA-1 is NOT
  whole-file SHA-1. `DARLING_CLT_PACKAGE` instead selects the one pinned local
  CLT 13.2 package: its whole-file SHA-256 is checked both before staging and on
  the staged copy. It is not an arbitrary-package override. Neither path's local
  hash verification establishes publisher authentication; never weaken it.
- Derive SDK/runtime identity and compatibility from authoritative metadata and
  installed contents. The mirrored package set and selected CLT 13.2 are not
  interchangeable. No fabricated versions, links or package-manager bypasses;
  retain incompatibilities in the owning Bead.

## 2. Select the supported owner

Run from the manifest with the pinned `mise run west ...` / `mise run dw ...`
environment; add `$HOME/.local/bin` to PATH if needed. `--prefix` only selects
a path. Bootstrap an absent/empty owned prefix in a separate invocation before
any prefix-backed selection; see [bootstrap](../../../docs/test-infra.md#bootstrap-a-fresh-prefix-before-a-prefix-backed-run).

The canonical runtime-setup providers are `manifest-ring-baseline` (runtime
only, no CLT) and `manifest-ring-on` (runtime plus reviewed guest compiler/SDK).
These are runtime providers, not patch/source profiles. Both declare
`source-mode: manifest`: all managed project HEADs must match their manifest
revisions, the selected product modules must have no tracked modifications or
untracked source overrides, and the manifest repository must have no tracked
modifications. Preserve foreign changes and coordinate with their owner instead
of resetting, stashing or bypassing this gate. Legacy internals still exist;
their presence is not permission to use them for canonical setup.

For a long run, use the existing supervisor. Replace every example path with an
absolute owned path and use a fresh, nonexistent job directory per invocation:

```bash
export DW_STATE_ROOT=/absolute/owned/state
scripts/west-job.sh start --state-dir "$DW_STATE_ROOT/jobs/bootstrap" -- \
  mise run west test --prefix "$DW_STATE_ROOT/prefix" \
  --bootstrap-runtime-profile manifest-ring-baseline
scripts/west-job.sh follow --state-dir "$DW_STATE_ROOT/jobs/bootstrap"
```

Choose `manifest-ring-on` instead when guest compilation is required; these are
alternatives, not sequential setup steps. To select the reviewed local CLT 13.2,
export `DARLING_CLT_PACKAGE=/absolute/path/to/reviewed.pkg` **before** starting
that provisioning job. Use a fresh prefix if the older SDK layout conflicts;
do not remove its SDK directory to force an in-place upgrade. Do not combine
bootstrap with CTest selectors, `--list`, extra runtime providers or passthrough
arguments. Read `darling-testing` before the subsequent test selection.

`darling-bootstrap --prefix ABS --build-dir ABS` is a separate explicit,
already-configured-build route. Its plan checks build defines and a matching
`CMAKE_INSTALL_PREFIX`; it builds/deploys and smokes that tree, but does not
enforce the provider's full clean manifest-source gate, provision CLT or write
the retained provider marker. Do not substitute it for canonical provider setup
or infer source provenance from its smoke. Its prefix parent must already exist;
the prefix itself must not be a symlink.

Do not use named Homebrew dev scenarios as the manifest-native setup/workload
route: their current dispatch still selects legacy source/patch metadata.
There is no manifest-native Homebrew scenario in that facade. Named contexts
are local West configuration, not portable runtime artifacts; inspecting
`mise run dw dev run --help` does not establish canonical source selection.
Its `--dry-run` prints a command without building or creating a job, whereas
`west test --dry-run` is GC-only and is not a launch preview.

Retain the job path. Reconnect with `scripts/west-job.sh follow --state-dir JOB`;
cancel with `scripts/west-job.sh cancel --state-dir JOB`, then follow to final
status. Existing dev jobs also support `mise run dw dev follow JOB` / `cancel JOB`.
Cancellation first requests cooperative cleanup; its escalation fallback can
interrupt cleanup, so cancellation alone never proves an idle prefix.

No manual nohup/setsid/PID/rc-file recipes, follow filters, sleep-polling loops,
or external timeout/kill wrappers. Observer timeout is not cancellation; a
detached handle is not completion. Use one-shot `status` only for recovery
following interruption. Do not start the next prefix-backed run until final rc
AND prefix cleanup. Separate lanes may run concurrently.

## 3. Run once and inspect evidence

- Provider bootstrap runs doctor workspace scope before build/deploy and runtime
  scope after guest smoke, before optional CLT provisioning. A doctor pass is
  not the installer verdict or completion of the enclosing job. `all` combines
  workspace and runtime checks; scoped passes are not full audits.
- Read `mise run west darling-doctor --help`. For an ordinary current-runtime
  audit, pass the actual `--prefix` (install prefix baked into the launcher)
  and prefix-matched `--build-dir`; `--extra-prefix` compares an additional
  runtime prefix's critical closure. Defaults are not workspace discovery.
  Current receipt checks are distinct from historical baseline mode;
  `--no-baseline-file` selects historical mode too. Do not waive unexplained drift.
  Doctor does not boot by default; `--boot-smoke` is an explicit guest launch
  requiring ownership, not a read-only audit option.
- On provider-bootstrap failure, read retained doctor JSON, stdout/stderr,
  command, rc and timeout in the reported evidence archive. A restored prefix
  after rollback is a different state, not the failed deployment's evidence.
  The explicit-build bootstrap reports doctors in its command output instead;
  post-smoke doctor/receipt failure can leave its deployment retained.
- When a diagnostic runner is used, retain its printed `BUNDLE` path and verify
  executor provenance. Its stdout/stderr are registered for live job observation;
  `--activity-log` adds other logs. `--forward-output` is post-execution replay,
  not live streaming. Log silence, phase and last-write age do not prove a hang.
- Require the workload's own readiness/completion oracle. A process or socket,
  an intermediate diagnostic or a timeout is not successful guest execution.
  Use `darling-diagnostics` for artifact identity and interpretation.
- Native-tools preflight is not Homebrew from-source acceptance. Exact-capture
  diagnostic acceptance deliberately reaches a payload timeout, then requires
  captured guest Mach-O, a structurally valid core for that process, registers
  and prefix cleanup. Report its diagnostic PASS separately from archive
  `exact_complete`; preserve unreadable-mapping warnings.

## 4. Finish, repair or retain a blocker

1. Request graceful shutdown through the run's prefix-scoped lifecycle owner;
   verify remaining processes and mounts even when shutdown exits zero. After
   the previous owner has stopped, standalone cleanup is
   `mise run west test --prefix ABS --cleanup-prefix`, without test selectors or
   `--keep-prefix-running`. This stops/cleans runtime state; it does not delete
   the prefix tree.
2. Let that owner terminate verified leftovers. Never globally pkill/killall
   darlingserver, mldr, launchd or vchroot. Tie each process to the selected
   prefix/namespace; unknown ownership means stop and retain evidence.
3. Do not boot over half-dead state or retry in a kill loop. Preserve failure
   diagnostics and finish teardown before another attempt. `--keep-prefix-running`
   is only deliberate owned local iteration, never acceptance or a cleanup workaround.
4. For repairable tmp/CLT-link prerequisites, inspect
   `mise run west darling-prefix-repair --prefix ABS --check`, then repair only
   the owned prefix. Repair does not install a missing CLT package or manufacture
   its identity. For stale mounts, first stop the lifecycle owner, then inspect
   `--check --cleanup-mounts` before using `--cleanup-mounts`. That operation
   can unmount and terminate identified Darling mount holders; it is not just
   file repair. A remaining process or mount keeps cleanup incomplete.
5. For lanes use their supported verify/destroy lifecycle. The
   `darling-rootless-debug-cleanup` command currently accepts only recognized
   disposable trees directly under `/tmp`, not canonical durable-state prefixes.
   It is not a general deletion tool and does not justify new `/tmp` prefixes.
   For an existing legacy tree inspect `mise run west
   darling-rootless-debug-cleanup --path ABS --dry-run`; it refuses detected
   live processes/mounts. Use `--sudo` only for its reported ownership failure.
   Never glob-delete prefixes or source worktrees.
6. Select the deployment path deliberately. `--deploy-only` and the
   deploy-verify wrapper copy/hash components without lifecycle locking or
   rollback; they are not the full build/deploy transaction. Stop the owning
   runtime first and verify every intended path. No in-place overwrite of
   executing binaries, guessed baseline reset or writes into another owner's
   prefix. Runtime RED proof must back up/restore every declared artifact and
   verify fixed-runtime return. A receipt or rollback alone is not guest readiness.
7. Record final command status, guest-visible verdict, evidence paths and cleanup.
   Use `darling-durability` to retain results; do not reset unrelated prefixes.

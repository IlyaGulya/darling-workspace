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
- Rootless is the launcher default. Its current launch path does not create
  Linux user, mount or PID namespaces;
  unprivileged namespace creation is not an ordinary rootless prerequisite.
  Privileged/rootfull mode is gated by `DARLING_FORCE_ROOTFULL_MODE=1`; do not
  change setuid permissions or select that mode to repair a rootless launch.
  Attribute namespace denials only to the path that actually requested them.
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
- Guest C/C++ CTest fixtures compile inside Darling. Flat guest-C workloads derive
  compiler prerequisites; workload selection does not provision the compiler.
  Explicitly provision reviewed CLT through the guest `installer` using
  `west darling-bootstrap --runtime-root R --prefix P --no-build` with
  `--guest-toolchain darling-command-line-tools`, not a workload metadata field.
  Never commit CLT packages or snapshots; a runtime-only smoke does not prove a compiler/SDK.
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

Select an immutable normal product installation `R` separately from writable
guest state `P`: `R/bin/darling`, `R/bin/darlingserver` and
`R/libexec/darling` belong to the image; `P` is `DPREFIX`. The two paths must
be disjoint. Never discover a launcher in `P/bin` or copy system libraries
into `P`. Configure the build `B` with absolute `CMAKE_INSTALL_PREFIX=R`,
default `COMPONENTS=stock`, build ALL and perform the full ordinary install:
`mise run west darling-build --build-dir B --runtime-root R --install`.
Add `--with-test-data` only when the workload requires installed test assets.
New code needs a new image root; never overwrite a published image.

For a long bootstrap, use the existing supervisor with absolute owned paths
and a fresh, nonexistent job directory:

```bash
export DW_STATE_ROOT=/absolute/owned/state
scripts/west-job.sh start --state-dir "$DW_STATE_ROOT/jobs/bootstrap" -- \
  mise run west darling-bootstrap --runtime-root "$DW_STATE_ROOT/runtime" \
  --prefix "$DW_STATE_ROOT/prefix" --build-dir "$DW_STATE_ROOT/build"
scripts/west-job.sh follow --state-dir "$DW_STATE_ROOT/jobs/bootstrap"
```

The build must already be configured for that final runtime root. Bootstrap
can consume an existing verified image without rewriting it: omit `--build-dir`
and use `--no-build`. For a fresh image, `--no-build` with a build directory
skips compilation but still installs already-built files into the new root.
Optional `--guest-toolchain darling-command-line-tools` provisions real guest
CLT; `--smoke-timeout-seconds N` bounds smoke. Export the pinned
`DARLING_CLT_PACKAGE` before provisioning when using the reviewed local package.
Use a fresh prefix for incompatible SDK layouts, never remove SDKs to force
an upgrade. Bootstrap is setup, not a workload verdict.

Workspace lifecycle owners retain an exclusive persistent external per-prefix
sibling lease, `<P.parent>/.west-prefix-lease-<sha256(P.name)>`, through cleanup.
It is not the product's `P/.lifecycle.lock`. Never delete the lease file or
replace it with a parent-directory flock. Clone identity belongs to the product:
`DPREFIX=DEST R/bin/darling bind-clone BASE`; supported lane/fresh-prefix tools
invoke it, and workspace Python must not rewrite lifecycle records. Keep the
caller's raw prefix spelling for product safety checks.
Nested controllers may borrow only the owner's verified inherited locked
descriptor. Never export lease markers by hand; the marker alone is not a
lock, and actual runtime children must not retain the workspace lease.

Named contexts record runtime root and guest prefix separately; they are local
configuration, not portable runtime artifacts. Select flat workloads with
`west test --workload ID --source-root S --runtime-root R --prefix P`.
Read `darling-testing` first. `west test --dry-run` is GC-only, not a launch preview.

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

- Read `mise run west darling-doctor --help` and select explicit `--runtime-root R`
  and `--prefix P` for the requested audit. Defaults are not workspace discovery.
  A scoped doctor pass, install receipt or successful smoke is not the enclosing
  workload's verdict. Do not waive unexplained drift. `--boot-smoke` is an
  explicit guest launch requiring ownership, not a read-only audit.
- On bootstrap failure retain its command, JSON, stdout/stderr, rc, timeout,
  selected installation identity and cleanup result. A later repaired prefix
  is a different state, not evidence of the failed run.
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
   `mise run west test --runtime-root R --prefix P --cleanup-prefix`, without test selectors or
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
6. Select a complete immutable installation deliberately. Never overwrite
   executing binaries or publish changes into an existing image. Runtime RED
   proof uses separate bad/fixed normal installs and the same oracle, declared
   dependencies and lifecycle cleanup. Return explicitly to the fixed image.
   Rescue kill/repair can clean a failed run but cannot produce clean PASS;
   ordinary acceptance requires product shutdown and zero owned residue.
7. Record final command status, guest-visible verdict, evidence paths and cleanup.
   Use `darling-durability` to retain results; do not reset unrelated prefixes.

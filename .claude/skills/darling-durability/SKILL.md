---
name: darling-durability
description: >-
  Preserve Darling changes across checkout, integration regeneration, and handoff.
  Use before ending a changing session, before checkout/update/patch application,
  or when dirty source work is found. Preserve canonical fix branches and stage
  only explicitly owned manifest and handoff changes.
---

# Darling durability: preserve owned changes, then hand off

Treat this skill as guidance to verify against current repository rules and
commands, not as authority over them. Re-check the active West workspace and
manifest repository and derive the baseline from authoritative configuration.
Correct stale instructions when found.

## Source and activation

The canonical source is `.claude/skills/darling-durability/SKILL.md` in the
selected manifest repository. Resolve `skill://darling-durability` and workspace
skill aliases to their physical backing files; an active installation may be a
separate checkout. Synchronize separate active copies from the canonical source
and report that relationship. Installed guidance is not portable runtime
configuration. Do not search conversation archives for installation copies.

## What must survive

- Clean `fix/*` branches are canonical editable product source.
- Patch archives and their full source SHA/checksum metadata are portable inputs.
- The manifest repository owns workspace metadata, Beads, drafts, and handoff.
- `integration/*` branches and profile `west.lock.yml` files are generated,
  not a place to retain fixes. Do not edit generated branches or locks.
- Dirty worktrees are not made durable by `mise run west dw handoff` alone.
- The West-root sibling `darling-debug-runner` is an independent repository.
  Forest handoff does not retain its branches merely because the runner is used
  by `mise run dw dev run`. Preserve its owned commits on explicit local refs and,
  when needed for transfer, a separate bundle; verify the desired tips there.
- Named dev context values live in local West configuration:
  `dev.active-context` and `dev-<name>.{prefix,runtime-profile,executor,bundle-root}`.
  Record how to recreate the context on the receiving host, not copied
  host-specific prefixes or executor paths in generic source/skills.
- External diagnostic archives and job state are evidence, not source or an
  automatic handoff payload. Preserve their actual locations and any required
  transfer separately; a path in a handoff note does not transfer its bytes.

From the manifest repository, use `mise run dw ...` for the managed interface
and `mise run west ...` for advanced West commands. Both tasks select the pinned
mise environment for every command and forward arguments without an extra
separator. From the West root use `mise -C darling-workspace run dw ...` or
`mise -C darling-workspace run west ...`. Preserve advanced interfaces such as
`mise run west patch`, `mise run west pr`, and `mise run west dw handoff`;
do not invent unsupported managed verbs.

## Triage before changing repository state

1. Inspect relevant repository status and worktree/branch ownership, including
   the manifest repository and independent runner sibling. Distinguish source edits from expected
   submodule-pointer drift and generated files. Use the current harness's file
   and search tools instead of copied shell pipelines.
2. Classify each change as owned keeper, another person's work, or disposable
   experiment/evidence. Do not discard, stage, reformat, or commit someone else's
   changes as part of your task.
3. Before rebasing, switching, resetting, or regenerating an integration, preserve
   at-risk commits with an explicit local rescue ref or verified bundle, and back
   up owned uncommitted changes outside disposable worktrees. A branch reference
   alone does not preserve uncommitted files.
4. Put the actual fix on a clean `fix/*` worktree at its reviewed base, including
   required dependencies. Do not force-update a branch from a copied recipe or
   blindly assume every fix can be based on the manifest revision. Verify the
   intended replay and behavior; preserve the old identity until the replacement
   is durable.
5. Inventory runner source and owned uncommitted changes independently of the
   Darling forest. Retain an explicit runner ref before changing its checkout;
   record the actual runner source tip and executor provenance alongside the
   manifest/source tips. A release executable or target directory is not a
   durable replacement for source; derive runner identity from the repository.

## Commit and export narrowly

- Commit only the exact owned source paths on the canonical fix branch. Inspect
  pre-existing staged changes so the commit cannot absorb unrelated work.
- Refresh changed patch inputs with `mise run west patch export`, using the focused
  `--profile` and `--patch` selectors when applicable. Update the full source
  SHA and checksum together. Do not hand-edit archives or accept unrelated YAML
  formatting churn. Never edit generated locks to make verification pass.
- Validate through `mise run west patch export --check`,
  `mise run west patch verify`, and relevant behavior checks. Unified patch archives have significant blank
  context lines: exclude `patches/**/*.patch` from ordinary whitespace checks
  and validate the payload with patch tooling instead.
- Commit owned manifest changes with an explicit path list, separate from
  handoff-generated changes. NEVER use `git add -A`, broad directory staging,
  or a blanket reset/clean as a shortcut. If the index contains unrelated work,
  isolate your commit without altering that work.
- Keep generated logs, snapshots, test archives, and diagnostic evidence in the
  external diagnostic archive, not in product patches. An experiment does not
  become product source merely to make it durable.
- Keep reproducible configuration declarations in source; keep machine-local dev
  contexts, prefix contents, downloaded CLT caches, compiled executors, and
  diagnostic bundles outside product patches. Preserve reviewed toolchain
  integrity metadata in its authoritative source, not a copied cache stamp.
  For catalog-backed CLT packages, catalog SHA-1 covers the compressed XAR TOC;
  the reviewed SHA-256 covers the whole package. Derive SDK identity from
  authoritative metadata and installed contents, preserve compatibility gates,
  and record blockers in Beads. Cache presence or made-up SDK versions are not
  proof of provisioning.

## Finish or transfer managed runtime work

- For the supported `homebrew-prepare`, `homebrew-preflight`, `homebrew-source`,
  and `exact-capture` scenarios, use `mise run dw dev run` with a configured context;
  see darling-boot for ownership and lifecycle gates. Context setup is local;
  `mise run dw dev run <scenario> --dry-run` only prints the plan without build, job, prefix, or guest
  side effects. The default runtime is `homebrew-lz4-source`, executor build is
  incremental release in the sibling runner, and archive root is
  `<workspace-parent>/darling-debug`.
- Retain the printed `JOB` path. Reconnect using `mise run dw dev follow <state-dir>`;
  request cancellation using `mise run dw dev cancel <state-dir>` and follow to
  final status before declaring cleanup complete. For advanced West jobs use
  `scripts/west-job.sh start`/`follow` with a unique state directory and
  `mise run west ...` as the job command, not manual detachment or polling loops.
  An observer timeout does not cancel a job.
- Preserve automatic live-log output and the reported bundles, not only console
  excerpts. Log silence does not prove a hang. A transferred active job needs
  an explicit owner; otherwise finish/cancel it through its lifecycle authority
  before changing the runtime it uses. Never globally kill prefix processes.
- Preserve complete bootstrap doctor failure JSON, stdout/stderr, command,
  return code, timeout state, and archive path. Record the scope: bootstrap
  checks workspace drift before provisioning and runtime postconditions after
  deployment; a scoped pass is not a full doctor pass.
- Record diagnostic PASS separately from archive-wide `exact_complete`.
  Exact capture deliberately expects a payload timeout and checks the guest
  Mach-O image, matching valid core, registers, and prefix cleanup. Unreadable
  mappings such as `[vsyscall]` can leave `exact_complete=false` even when that
  diagnostic passes; retain the omission details rather than upgrading the claim.

## Refresh handoff without swallowing other work

After changing private branches or Beads, run `mise run west dw handoff`.
Before doing so, inspect the current handoff implementation and record the
pre-existing dirty/staged paths; handoff behavior and generated paths can change.

Afterward:

1. Identify which files the handoff operation actually changed. Do not assume a
   fixed list of bundle, state, manifest, or Beads paths is exhaustive.
2. Inspect those changes and stage only the exact handoff-owned paths or hunks.
   A file that already contained unrelated edits needs separation, not wholesale
   staging. Never use `git add -A`, even scoped to a handoff directory.
3. Commit the handoff changes separately when needed.
4. Use `git bundle list-heads <actual-bundle-path>` or the current handoff verifier
   to prove each owned canonical forest branch tip is retained. Separately
   verify the runner's explicit refs/bundle tips: a successful forest handoff is
   not evidence of runner preservation. Record source, runner, and manifest
   commit IDs plus remaining unrelated dirt, local configuration, external
   evidence locations, and blocked work.

A successful command exit is not proof that every desired branch reached a
bundle. Conversely, a checkout containing someone else's intentional changes
need not be made globally clean to finish your task.

## Publication and runtime boundaries

- Local durability does not authorize a push. Never publish branches, tags, or PR
  updates without explicit approval covering that exact action and destination.
  Fork approval does not authorize upstream changes. Preserve publication gates;
  do not silently reclassify blocked fixes.
- Handoff does not require booting Darling, redeploying artifacts, or resetting
  all prefixes to a remembered baseline. If this session used a runtime, verify
  only its owned prefix's lifecycle and declared restore obligations through the
  current tools. Do not globally kill processes or use copied binary hashes.
  Deployment and restoration remain the supported transaction's responsibility:
  no in-place overwrite of executing binaries, guessed baseline reset, or
  redeployment into another owner's prefix. Atomic file replacement does not
  bypass the lifecycle or restore gates.
- Preserve current working fixes even if their end-to-end proof is blocked; record
  the exact blocker and evidence. Do not call the feature complete merely because
  a commit or bundle exists.
- An explicit user stop pauses further mutation, including commits and handoff.
  Report the preserved state and what remains uncommitted; resume only when asked.

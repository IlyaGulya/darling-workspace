---
name: darling-durability
description: >-
  Preserve owned Darling changes before ending a changing session, checkout,
  update or handoff, and whenever dirty source is found. Protect canonical fix
  branches, unrelated user edits and independently owned tooling repositories.
---

# Darling durability

Canonical source: `.agents/skills/darling-durability/SKILL.md` in the manifest;
`.claude/skills` is only its compatibility alias. Resolve installed aliases to
physical files; preserve local additions when synchronizing genuinely separate
copies. Installed guidance and named contexts are not portable runtime artifacts.
Verify current CLI, implementation and repository ownership before acting;
preservation requirements below are not automatic tool guarantees.

## 1. Identify and protect ownership

- Inspect relevant status, staged changes and worktree/branch ownership. Separate
  owned keepers, user work and disposable experiments; distinguish source edits
  from generated state and submodule-pointer drift. Never discard, stage or
  reformat another owner's work.
- Canonical product fixes belong on clean `fix/*` branches at a reviewed base
  with required dependencies. Do not force-move refs or assume the manifest pin
  is always a valid fix base. Before checkout/rebase/reset, preserve at-risk
  commits with a rescue ref or verified bundle and uncommitted bytes outside
  disposable worktrees. A branch does not preserve uncommitted files.
- Workspace metadata, Beads, manifests and handoff belong in the manifest, never
  Darling source repositories. Keep test assets with their declared owner.
- Resolve the runner actually used. The workspace `dwdiag` shim prefers vendored
  `tools/darling-debug-runner`; a separately used sibling is independently owned.
  A forest handoff does not cover that sibling merely because a scenario invokes
  it. Retain the actual source tip and executor provenance, not only its binary.
- Keep host-local prefixes, contexts, CLT caches and diagnostic archives outside
  product source. Preserve configuration declarations and reviewed package
  integrity metadata, not invented cache stamps or SDK identities.

## 2. Finish active work through its owner

Retain the actual owner's job path and any published bundle/evidence paths;
their formats and retention differ. Existing dev jobs support `mise run dw dev
follow JOB` / `cancel JOB`; other supervised runs use `scripts/west-job.sh
follow|cancel --state-dir JOB`. Observer timeout is not cancellation, and a
cancel request alone is not cleanup evidence. An active job needs an explicit
receiving owner; otherwise follow/cancel through final rc and verified prefix
cleanup before changing the runtime. Never globally kill processes.
Read `darling-boot` for source selection, launch/cleanup and exact-capture rules.

Preserve full command, rc, timeout, stdout/stderr, structured doctor problems,
artifact identities and archive paths, not console excerpts alone. A scoped
doctor pass is not a full audit; rollback changes the prefix being inspected.
Diagnostic PASS and archive `exact_complete` are separate facts. Preserve
unreadable mappings/warnings rather than upgrading evidence completeness.

Keep at least one acceptance-evidence copy outside GC-managed roots with a
SHA-256 manifest. Copy required symlink targets too; PRESENT paths or directory
listings do not prove the bytes were transferred. Write checksums to a separate
file after finalizing evidence, exclude the checksum file itself, and verify with
`sha256sum -c`. Follow [state ownership and cleanup](../../../docs/environment.md#durable-state-and-cleanup).
If only a hash/verdict survives, say so and rerun the gate rather than citing a
destroyed archive.

## 3. Retain only accepted, owned changes

- Validate real behavior with `darling-testing`. Preserve a useful fix even when
  end-to-end verification is blocked, but record the exact blocker and never
  call it complete because a commit or bundle exists.
- Commit only explicit owned paths on their canonical branch. Inspect existing
  staged work; isolate your commit without altering it. No `git add -A`, broad
  directory staging or blanket reset/clean. Separate product/manifest commits
  from handoff-generated state; do not publish rejected implementations.
- Record chronology, evidence and remaining work in the owning Bead. Instructions
  and skills remain normative; no incident log or one-off acceptance transcript.

Durability means accepted component and manifest commits reachable from their
respective declared remotes, committed Beads state, and clean OR explicitly
reported dirty worktrees. This does not authorize pushing. Never push branches,
tags or updates without approval for that exact action and destination; this
hard fork has no upstream publication or pull requests.

## 4. Handoff only when portable archival is needed

`mise run west dw handoff` is optional for explicit local-only state needing
portable archival, not a prerequisite for every Beads edit or branch. Inspect
`--dry-run` or `--json` first: these plan without publishing, but can refuse an
incomplete source closure or unsafe/interrupted transaction. Do not initialize
unrelated submodules merely to satisfy it. Bundles retain selected committed
refs, not dirty/untracked files; `--allow-dirty` permits omission, not backup.
Preserve uncommitted bytes separately before any checkout/update. A rejected
branch needs no keeper bundle once reason, SHA and evidence are durably recorded.

Before handoff inspect the current implementation and existing dirty/staged
paths. Afterward identify exactly which files it changed; stage only those
owned paths/hunks and commit separately when needed. Use `git bundle list-heads`
or the supported verifier to prove desired forest tips are retained. Preserve
changed independent tooling in its own keeper bundle and verify its tip too.
A successful exit alone does not prove desired commits were included.

Record source/runner/manifest identities, remaining dirt, context recreation
instructions and evidence transfer locations. Named context keys include
`dev.active-context` and `dev-NAME.{prefix,runtime-profile,executor,bundle-root}`;
record how to recreate them rather than treating host paths as portable defaults.

## Completion boundary

Report exercised checks, owned changes/commits, uncommitted paths, evidence
locations and blockers. No boot/redeploy/reset is required merely for handoff;
verify only this session's owned runtime and restore obligations. Atomic file
replacement does not waive lifecycle or ownership checks. An explicit user stop
pauses all mutation, including commits and handoff; report preserved state.

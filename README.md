# Darling workspace control plane

Private West manifest and portable coordination state for development on
Darling without adding personal metadata to Darling or its upstream submodules.

This repository is the source of truth for the development workspace. A
checkout under `~/work/darling-dev/darling` is a working copy, not the durable
record of tasks, unpublished branches, or PR preparation.

## Ownership boundaries

- `west.yml`: active multi-repository workspace manifest.
- `west.lock.yml`: frozen upstream workspace revisions.
- `patches/`: reproducible local integration profiles with provenance.
- `handoff/`: Git bundles containing every local non-default branch.
- `.beads/issues.jsonl`: shared task graph for humans and agents.
- `pr-drafts/`: PR descriptions and review notes.
- `state/repos.tsv`: reproducible snapshot of checked-out commits and branches.
- `bin/dw`: workspace commands.

Darling's `.gitmodules` remains the upstream build contract. West provides the
developer control plane over the same repositories. The old `repo` XML
manifests remain temporarily as migration evidence and fallback.

## Setup on another machine

```bash
west init -m git@github.com:IlyaGulya/darling-workspace.git ~/work/darling-dev
cd ~/work/darling-dev
west update
west dw restore
west dw beads sync --import-only --rebuild
west patch verify --profile homebrew
west patch apply --profile homebrew
```

## Daily use

```bash
west status
west forall -c 'git log -1 --oneline'
west dw summary
west dw beads ready
west dw restore
west patch list --profile homebrew
west patch verify --profile homebrew
west patch apply --profile homebrew
west patch clean --profile homebrew
west darling-doctor            # verify manifest/build/deploy alignment BEFORE building or booting
west darling-build             # doctor-gated ninja build of dyld + closure (add --deploy to install)
west dw handoff
```

`west darling-doctor` is the guard against the drift that caused the perf#24c2c-pre
detours: it checks each project's working tree against its **West manifest** revision
(intentional drift is declared in `doctor-allow-drift.txt`), that the build dir's
`CMAKE_INSTALL_PREFIX` matches the prefix baked into the setuid launcher (a mismatched
build can never boot the prefix), and that the deployed dyld/mldr/darlingserver match the
known-good `deploy-baseline.md5`. Run it before any build/deploy/boot. `west darling-build`
runs the doctor as a pre-gate, refuses to build on failure (unless `--force`), and re-checks
after `--deploy`. Update `deploy-baseline.md5` when a legitimate rebuild changes what is
deployed.

Doctor output has three explicit modes. The default is a bounded summary with
at most eight problem/warning rows and an exact, shell-quoted command for the
complete view. That command preserves the current West launcher and writes
free-form options as `--option=value`, so leading-dash values replay safely.
`west darling-doctor --full` retains the verbose per-check diagnostics; `west
darling-doctor --json` writes only its complete machine result. Independent
sections continue after an operational error so the result remains complete.
The JSON envelope has `schema_version: 1`, `operation: "doctor"`, and `state`
equal to `healthy`, `problems`, or `operational_error`, plus typed per-check
results and summary counts. Exit status is 0 only for `healthy`, 1 for
diagnosed problems or an operational failure, and 2 for invalid command-line
usage.

## Claude Code guardrails (hooks + skills)

The workspace ships Claude Code automation that encodes the guardrails and
procedures learned the hard way (the perf#24c2c-pre detours). All of it is
tracked here so it survives `west init` on another machine.

- `hooks/pretooluse-build-gate.sh` — a `PreToolUse` (Bash) hook. Before any
  command that looks like a Darling build/deploy/boot (`ninja`, `west
  darling-build`, a copy into `libexec/darling`, `darling shell`, `shellspawn`,
  …) it runs `west darling-doctor` and **blocks** on failure. Catches #89
  (wrong build dir) and #90 (manifest↔worktree drift) automatically. Escape
  hatch: `--force`, `--skip-doctor`, or `DARLING_SKIP_DOCTOR=1`.
- `hooks/stop-durability-reminder.sh` — a `Stop` hook. On session end it warns
  if the manifest repo is uncommitted or a tracked worktree (dyld /
  darlingserver / xnu / superproject) is dirty, and blocks the stop once so the
  work isn't silently abandoned. It respects `stop_hook_active` (no loop).
- `.claude/skills/darling-boot/` — clean teardown + single boot protocol
  (kill orphan launchd, settle, one boot + poll) to avoid the perf#23a wedge.
- `.claude/skills/darling-durability/` — triage → rescue onto `fix/*` → commit
  manifest → `west dw handoff` → verify.

Activation is per-checkout and lives OUTSIDE the manifest repo (the workspace
root is not a git repo):

```bash
# from the workspace root (~/work/darling-dev)
mkdir -p .claude
ln -sfn darling-workspace/.claude/skills .claude/skills        # skills source of truth
cp darling-workspace/.claude/settings.sample.json .claude/settings.json
# (settings.json references the hook scripts by absolute path)
```

`west dw handoff` exports Beads, refreshes manifests, and creates Git bundles
for every local branch except an unchanged `main`/`master`. This includes
active topics, clean PR branches, and backup snapshots. The bootstrap flow
syncs `base.xml`, then restores all those branch refs from the bundles.

Uncommitted worktree changes cannot be handed off. `dw handoff` prints every
dirty repository so it can be committed or intentionally discarded first.

## Daily feature workflow

`west dev` is a thin, evidence-producing front end for the existing patch,
test, doctor, deployment, Beads, and handoff authorities:

```bash
west dev profiles
west dev profiles --kind runtime --json
source <(west dev profiles --completion bash)
west dev status --profile homebrew
west dev start --source /path/to/source --destination /path/to/authoring \
  --base <commit-or-ref> --branch fix/<topic> --bead <id> --module <name> \
  --evidence /path/outside/active/repos/start.json --dry-run
west dev check quick --profile homebrew \
  --evidence /path/outside/active/repos/quick.json
west dev check canonical --profile homebrew \
  --evidence /path/outside/active/repos/canonical.json
west dev check acceptance --profile homebrew --prefix /path/to/prefix \
  --build-dir /path/to/build --evidence /path/outside/active/repos/acceptance.json
west dev package --profile homebrew --receipt /path/to/acceptance.json \
  --output /path/to/review-package --evidence /path/to/package.json
west dev verify-package /path/to/review-package
```

`start` creates an independent exact-base clone without alternates, hardlinks,
or mutations to active West repositories. Its `--dry-run --json` form emits
the complete plan. `west dev recover-start --evidence <start.json>` recovers
an interrupted transaction. `acceptance` is an unnarrowed Homebrew runtime
tier and requires explicit `--prefix` and `--build-dir`. `package` requires a
committed, current-workspace acceptance receipt and its embedded acceptance
artifacts; `verify-package` revalidates a published package's complete closure
without changing it.

Human-mode checks report each step start/finish with elapsed time. Acceptance
copies only manifest-declared source refs into its disposable candidate, then
runs `patch verify`, the host materialized test, and the immutable oracle
concurrently across isolated candidate, active, and control repository sets.
Successful results for those three steps are checkpointed under the manifest
repository's Git common directory. The key binds the workspace commit and
tree, composed profile graph, profile manifest and patch bytes, lock-first
mapping and lock bytes, frozen manifest, executable and installed West package
content, the host compiler/build-tool content, and the exact non-secret
environment inherited by the parallel gate. A valid hit reuses only those
three results and the content-addressed oracle; candidate replay and
comparison, the candidate host tier, and the final guest/prefix smoke still
run. Corrupt checkpoints are recomputed and replaced. Symlinked or otherwise
unsafe checkpoint paths fail closed.

`profiles` discovers immediate `patches/*/patches.yml` manifests and the
CTest-owned `testkit/runtime-profiles.yml` catalog at invocation time. Its
default summary is bounded to eight rows and names the exact `--json` command
for the complete result; `--names --kind patch|runtime|all` is the stable
newline-delimited candidate protocol. Repeatable `--purpose` filters narrow
runtime discovery; bootstrap completion uses the two bootstrap-capable
purposes automatically and never advertises an ordinary runtime-only provider.
Because discovery is dynamic, adding a valid manifest needs no completion
regeneration. The sourced Bash helper
completes only values for `--profile`, `--with-runtime-profile`, and
`--bootstrap-runtime-profile`, including `--option=value`; it deliberately
does not provide general West command/option completion and does not complete
the unrelated `--prefix-profile` shortcut.

The review package contains the accepted receipt, exact manifest/profile/mapping
and generated-lock bytes, acceptance artifacts, recovery mboxes, Git object
bundles, `package-index.json`, and `SHA256SUMS`. Offline verification derives
the locked commit order, stable patch identities, and resulting trees from
those contents. The candidate commit is explicitly acceptance-attested; its
tree is independently replayed. A local package directory remains mutable by
its owner—the receipt is not a signature—so run `verify-package` immediately
before use and again after copying or transfer. Any missing, extra, changed, or
internally inconsistent payload is rejected.

Default `west dev` output is bounded: dirty repositories, active operations,
and planned mutations each show at most eight entries and report the omitted
count. Every status or dry-run summary prints an exact shell-quoted `--json`
command immediately after its header for full detail. The command preserves
the recorded or current West launcher and uses `--option=value` for arbitrary
option values. A completed `check` or `package` instead places an exact
`cat --` command for its durable evidence JSON there, so inspecting detail never
reruns a mutation. Add `--json` when another tool consumes an operation.
These results use `schema_version: 1` with an `operation` discriminator
(`profiles`, `status`, `start`, `check`, `package`, or `package-verify`) and an
operation-specific `state` such as `valid`, `healthy`, `degraded`,
`in_progress`, `planned`, `committed`, `failed`, `invalid`, or
`operational_error`. A successful `profiles` result has `state: "valid"`,
records the selected patch/runtime kind in `inputs.kind`, includes the full
ordered `profiles` array, and returns 0. A caught validation or operational
failure in JSON mode returns one action-specific error envelope and exits 1
without appending human diagnostics. Successful operations exit 0, recorded
command failures propagate their nonzero status, and invalid command-line
usage exits 2.

## Sharing code without fork noise

Share stable work as normal upstreamable commits and clean PR branches. Export
branches selected for local composition with:

```bash
./scripts/export_patches.py homebrew \
  --source-root /path/to/existing/darling
./scripts/export_patches.py homebrew --check \
  --source-root /path/to/existing/darling
```

`patches/<profile>/patches.yml` records the source branch and commit, Bead or
PR, historical archive checksum, and application order. `west patch apply`
validates schema-v2 locks and replays their immutable commits through native
Git in clean disposable transactions. It creates clean
`integration/<profile>` branches, records the top-level submodule pointers,
and writes a frozen profile lock. The checked-in patch archives are retained
for provenance and recovery review; they are not materialization inputs.

Patch diagnostics have the same bounded-default contract. `west patch status`,
`west patch check`, and `west patch explain` print a summary, then an exact
shell-quoted `--full` command, then at most eight problem rows; `explain` also
prints one recommended next command. `--full` emits every human-readable row
and `--json` emits the complete machine result. Their JSON envelopes use
`schema_version: 1` and operation discriminators `patch_status`,
`patch_check`, and `patch_explain`. `status --strict` exits 1 for missing,
mismatched, or locally unavailable integration state. `check` exits 1 for
invalid metadata, for missing behavioral coverage with `--strict`, or for
quality findings with `--strict-quality`. `explain` is read-only and exits 0
after classifying the requested profile, module, or series; planning or input
errors exit 1. Invalid command-line usage exits 2 for all three commands.

## Typed profile composition

A profile may declare `base-profile: <name>` in `patches.yml`. The corresponding
schema-v3 mapping and composition document bind that dependency, the frozen
manifest hash, every series boundary, and every final module tree. A standalone
canonical apply resolves the complete typed dependency graph and materializes
prerequisites automatically from immutable schema-v2 locks; it does not require
pre-existing `integration/<base>` refs and does not infer composition from
archive filenames.

For example, one command reconstructs Homebrew, then Perf, then Arch in the
declared order:

```bash
west patch apply --profile arch
```

Each successful layer publishes its own `integration/<profile>` refs and
generated `patches/<profile>/west.lock.yml`. The final generated lock carries
forward prerequisite-only module revisions. `west patch clean --profile
<name>` removes that layer while leaving separately materialized prerequisite
layers available. Any missing, reordered, or mismatched typed dependency fails
closed; there is no archive fallback.

## Patch profile invariants

- Canonical editable source: clean `fix/*` branch in the owning repository.
- Canonical materialization source: schema-v3 profile mappings/compositions,
  schema-v2 series locks, and their declared immutable refs/OIDs.
- Review/provenance artifacts: versioned patch archives and
  `patches/<profile>/patches.yml`; authoring exporters may refresh them, but
  they are not portable executable integration inputs.
- Generated local state: `integration/<profile>` branches and the profile
  `west.lock.yml`.
- Historical source: `backup/*` and preserved mega-branches.

Never edit or open a PR from `integration/<profile>`. Never edit patch files or
generated locks manually. Refresh patches with `scripts/export_patches.py`,
then run `west patch verify`. Source commits must be full 40-character SHAs.

`west patch clean` only operates when affected repositories are on the matching
integration branch or detached at `manifest-rev`, and refuses dirty worktrees.
`--force` is reserved for intentional recovery. The tracked profile lock is
updated only by a successful `west patch apply`.

## Pull request workflow

GitHub publication is a separate state machine over the same clean branches:

```bash
west pr list --profile homebrew
west pr dashboard --profile homebrew
west pr check --profile homebrew dar-q95.3
west pr publish-plan --profile homebrew dar-q95.3 --target fork
west pr fork-draft --profile homebrew dar-q95.3 --dry-run
west pr fork-draft --profile homebrew dar-q95.3
west pr sync --profile homebrew dar-q95.3
west pr upstream-draft --profile homebrew dar-q95.3
west pr update-body --profile homebrew dar-q95.3 --target fork
west pr ready --profile homebrew dar-q95.3 --target upstream
west pr open --profile homebrew dar-q95.3 --target upstream
```

A fork draft compares `fix/*` against `preupstream/<base>` inside the
`IlyaGulya` fork. `west pr fork-draft` updates that staging base from
`manifest-rev`, pushes the exact `source-commit`, and opens a draft without
notifying Darling maintainers. An upstream draft is a separate PR from the same
fork branch into `darlinghq`.

PR bodies are generated from the `## Title` and `## Body` sections in
`pr-drafts/*.md`. GitHub URLs and synchronized state are stored under each
patch's `github.fork` and `github.upstream` sections. Publishing is always
single-Bead and explicit; there is no bulk publish or automatic merge command.

One private manifest repository is intentional. Split Beads only if it needs
different access control or an independent lifecycle.

See `docs/branch-migration.md` for the branch workflow and
`docs/mega-branch-audit-2026-06-12.md` for the completed migration audit,
residual dispositions, and conditions for retiring the historical refs.

`west.yml` is the selected workspace backend and includes private workspace
tools such as `darling-debug-runner`. The old `repo` manifests are retained as
migration evidence and fallback only. See `docs/west-spike.md` for validation
results.

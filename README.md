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
- `bin/dw`: workspace command implementation; invoke through `mise run dw`.

Darling's `.gitmodules` defines the upstream build contract. West provides the
developer control plane over the same repositories. XML manifests under this
repository are migration evidence; use West for workspace setup.

## Setup on another machine

```bash
git clone git@github.com:IlyaGulya/darling-workspace.git ~/work/darling-dev/darling-workspace
cd ~/work/darling-dev
# Review darling-workspace/mise.toml before trusting its tools and tasks.
mise -C darling-workspace trust
mise -C darling-workspace install
mise -C darling-workspace run west init -l .
mise -C darling-workspace run west update
mise -C darling-workspace run west dw restore
mise -C darling-workspace run west dw beads sync --import-only --rebuild
mise -C darling-workspace run west patch verify --profile homebrew
mise -C darling-workspace run west patch apply --profile homebrew
```

## Daily use
Run commands from the manifest directory with `mise run dw ...` or
`mise run west ...`. Both tasks select the pinned environment and forward
arguments; West preserves the caller's directory. From the workspace root use
`mise -C darling-workspace run dw ...` or `run west ...`.
Use `--` after the task name for opaque arguments, including a literal `:::`;
for example, `mise run west -- config KEY VALUE`. Simple options can be passed
directly: `mise run west --version`.
Bare West names in prose identify underlying APIs, not additional dw verbs.


```bash
mise run west status
mise run west forall -c 'git log -1 --oneline'
mise run west dw summary
mise run west dw beads ready
mise run west dw restore
mise run west patch list --profile homebrew
mise run west patch verify --profile homebrew
mise run west patch apply --profile homebrew
mise run west patch clean --profile homebrew
mise run west darling-doctor --scope workspace
mise run west dw handoff
```

`west darling-doctor --scope workspace` checks project worktrees against the
**West manifest**
(intentional drift is declared in `doctor-allow-drift.txt`) without requiring an
already deployed runtime. `--scope runtime` checks build/deploy alignment and
prefix prerequisites; the default `all` runs both scopes. Bootstrap runs the
workspace scope before prefix mutation/build/deploy, then runtime scope after
guest readiness. Runtime doctor failures retain raw stdout/stderr and structured
problem rows in the runtime evidence archive's `diagnostics/` directory before
rollback; follow the artifact path reported by the failure handler.

For the four supported scenarios, use `mise run dw dev run` below.
Advanced `mise run west darling-build` is a
doctor-gated operator build interface, with `--deploy` for its supported install
path. Do not bypass a failing gate to make a run proceed or refresh
`deploy-baseline.md5` merely to silence drift. Preserve the reviewed deployment
identity and use the owning West deployment transaction. Never copy over or
delete binaries while the selected prefix is running: request owner cleanup,
establish that its processes have stopped, then deploy. Retain failure evidence.

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

The workspace tracks Claude Code guardrails and procedures so they are
available after `west init` on another machine.

- `hooks/pretooluse-build-gate.sh` — a `PreToolUse` (Bash) hook. Before any
  command that looks like a Darling build/deploy/boot (`ninja`, `west
  darling-build`, a copy into `libexec/darling`, `darling shell`, `shellspawn`,
  …) it runs `west darling-doctor` and **blocks** on failure. It checks build
  directory and manifest/worktree alignment. Resolve failures before proceeding.
- `hooks/stop-durability-reminder.sh` — a `Stop` hook. On session end it warns
  if the manifest repo is uncommitted, the independent runner is dirty, or a
  tracked source worktree is dirty, and blocks the stop once so the
  work isn't silently abandoned. It respects `stop_hook_active` (no loop).
- `.claude/skills/darling-boot/` — managed context/run/follow/cancel workflow,
  prefix-owned shutdown and ownership-checked cleanup.
- `.claude/skills/darling-durability/` — triage → rescue onto `fix/*` → commit
  manifest → `west dw handoff` → verify.

Activation is per-checkout and lives OUTSIDE the manifest repo (the workspace
root is not a git repo):

```bash
# from the workspace root
mise -C darling-workspace run dw setup-local
```

`setup-local` installs the source instruction template and Git excludes.
Install project hooks from `.claude/settings.sample.json`, replacing its
workspace paths. Resolve active skill locations and synchronize separate copies
from `.claude/skills/`. Preserve local additions when an installed instruction
file differs from its template.

`west dw handoff` exports Beads, refreshes manifests, and creates Git bundles
for every local branch except an unchanged `main`/`master`. This includes
active topics, clean PR branches, and backup snapshots. Initialize the workspace
with West, then use `mise run west dw restore` to restore bundled branch refs.

West supplies the complete project closure, including independently cloned
nested repositories; Git submodule initialization is not required for those
clones. Every declared path must be its own worktree, and populated
gitlinks outside that closure are rejected. Without a West closure, handoff
retains strict recursive submodule validation. Repository remote selection
prefers `origin`, otherwise requires exactly one configured remote.

Uncommitted worktree changes cannot be handed off. `west dw handoff` prints every
dirty repository so it can be committed or intentionally discarded first.

## Daily feature workflow

`west dev` is a thin, evidence-producing front end for the existing patch,
test, doctor, deployment, Beads, and handoff authorities:

From the manifest directory, `mise run dw dev ...` selects the pinned
environment through the dw proxy task. For runtime diagnostics:

```sh
mise run dw dev context homebrew --prefix /absolute/path/to/new-prefix
mise run dw dev run homebrew-prepare
mise run dw dev run homebrew-preflight
mise run dw dev run exact-capture
mise run dw dev run homebrew-source
```

Configure a context once with an absent or empty new prefix. Contexts are local
West settings; `dev context NAME` selects the active context, while
`dev run --context NAME` and explicit path options override it for one run.
The default runtime is `homebrew-lz4-source`, the checked-out diagnostic runner
is incrementally built in release mode unless `--executor` is supplied, and
archives default to the workspace parent's `darling-debug` directory.

Runs own a recorded job and attach live observation automatically. `--dry-run`
shows the underlying command without building, creating a prefix/job, or booting;
`--detach` starts without observing. Use the printed `mise run dw dev follow JOB`
or `mise run dw dev cancel JOB` commands to reconnect or request owner cleanup.
Runner streams and Homebrew build-log directories register automatically;
follow reports phase, log paths and last-write age, not a hang verdict from
silence. Advanced prefix-backed runs use `scripts/west-job.sh start` and
`follow` as documented in [test-infra.md](docs/test-infra.md).

Preparation, native-tools preflight and the guest exact-capture diagnostic have
separate acceptance criteria from a Homebrew source build. Validate SDK identity
against authoritative package metadata and preserve its provenance. Do not
invent version metadata or disable Homebrew's SDK checks to accept an
incompatible toolchain. Record scenario results and blockers in Beads and
diagnostic evidence.

The exact diagnostic intentionally times out its payload and can pass its
guest-image/core/cleanup oracle while the archive reports
`exact_complete=false` for unreadable mappings such as Linux vsyscall.

Advanced West commands are available (use `mise run dw dev` for these dev
commands, or `mise run west ...` for the underlying interfaces):

```bash
mise run west dev profiles
mise run west dev profiles --kind runtime --json
source <(mise run west dev profiles --completion bash)
mise run west dev status --profile homebrew
mise run west dev start --source /path/to/source --destination /path/to/authoring \
  --base <commit-or-ref> --branch fix/<topic> --bead <id> --module <name> \
  --evidence /path/outside/active/repos/start.json --dry-run
mise run west dev check quick --profile homebrew \
  --evidence /path/outside/active/repos/quick.json
mise run west dev check canonical --profile homebrew \
  --evidence /path/outside/active/repos/canonical.json
mise run west dev check acceptance --profile homebrew --prefix /path/to/prefix \
  --build-dir /path/to/build --evidence /path/outside/active/repos/acceptance.json
mise run west dev package --profile homebrew --receipt /path/to/acceptance.json \
  --output /path/to/review-package --evidence /path/to/package.json
mise run west dev verify-package /path/to/review-package
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
creates the independent control and candidate manifest clones concurrently,
then bootstraps the candidate from the active West forest as a Git path cache
with eight bounded update workers. The frozen manifest selects every
checkout by exact revision; active worktrees and refs are not reused.
Acceptance then copies only manifest-declared source refs into the candidate
and runs `patch verify`, the host materialized test, and the immutable oracle
concurrently across isolated candidate, active, and control repository sets.

Successful results for those three steps are checkpointed under the manifest
repository's Git common directory. The key binds the workspace commit and
tree, composed profile graph, profile manifest and patch bytes, lock-first
mapping and lock bytes, frozen manifest, executable and installed West package
content, the host compiler/build-tool content, and the exact non-secret
environment inherited by the parallel gate. After the first verified replay,
acceptance also publishes an immutable candidate cache containing only the
integration-object deltas, generated locks, and lock-first evidence. A valid
hit hydrates those objects into the fresh exact-base candidate instead of
replaying the stack. The guest tier then receives a shared-object clone with
independent refs, index, and worktree.

The candidate host tier and guest smoke run concurrently. Guest smoke selects
the initialization and prebuilt-Mach-O cases in one `west test` invocation so
prefix setup, locking, and shutdown happen once. Bootstrap runtime builds share
a private persistent ccache directory; the cache identity binds the resolved
Clang binaries, their content hashes, and version output, while debug/file
prefix maps keep disposable build paths out of object identity. Lock-first
rollback checks use an owned temporary root, so they cannot confuse another
tier's live worktree with a leak. Guest-wide GC is deferred until both tiers
finish, when a final sequential cleanup gate collects global garbage and
verifies no live West test jobs remain. Corrupt reusable content and symlinked
or otherwise unsafe cache paths fail closed.

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
tree is independently replayed. A local package directory is mutable by
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
PR, archive checksum, and application order. `west patch apply`
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
mise run west patch apply --profile arch
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
- Recovery source: `backup/*` and preserved topic branches.

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
mise run west pr list --profile homebrew
mise run west pr dashboard --profile homebrew
mise run west pr check --profile homebrew dar-q95.3
mise run west pr publish-plan --profile homebrew dar-q95.3 --target fork
mise run west pr fork-draft --profile homebrew dar-q95.3 --dry-run
mise run west pr fork-draft --profile homebrew dar-q95.3
mise run west pr sync --profile homebrew dar-q95.3
mise run west pr upstream-draft --profile homebrew dar-q95.3
mise run west pr update-body --profile homebrew dar-q95.3 --target fork
mise run west pr ready --profile homebrew dar-q95.3 --target upstream
mise run west pr open --profile homebrew dar-q95.3 --target upstream
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

The evidence reports `docs/branch-migration.md`,
`docs/mega-branch-audit-2026-06-12.md` and `docs/west-spike.md` preserve migration
decisions and observations; they are not operational runbooks.

`west.yml` is the workspace backend and includes workspace tools such as
`darling-debug-runner`.

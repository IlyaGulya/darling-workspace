# Darling workspace control plane

Private West manifest and portable coordination state for development on
Darling without adding personal metadata to Darling or its upstream submodules.

The canonical West workspace is `/home/ilyagulya/work/wr-fresh`; this manifest
owns coordination, not product source. See [environment](docs/environment.md)
before selecting source, build, installation or guest-state paths.

## Ownership boundaries

- `west.yml`: active multi-repository workspace manifest.
- `west.lock.yml`: derived frozen revision evidence, not a second source authority.
- `handoff/`: optional archives of selected committed refs, not dirty-file backups.
- `.beads/issues.jsonl`: shared task graph for humans and agents.
- `bin/dw`: workspace command implementation; invoke through `mise run dw`.

Darling's `.gitmodules` defines the upstream build contract. West provides the
developer control plane over the same repositories. XML manifests under this
repository are migration evidence; use West for workspace setup.

## Setup on another machine

```bash
git clone git@github.com:IlyaGulya/darling-workspace.git /home/ilyagulya/work/wr-fresh/darling-workspace
cd /home/ilyagulya/work/wr-fresh
# Review darling-workspace/mise.toml before trusting its tools and tasks.
mise -C darling-workspace trust
mise -C darling-workspace install
mise -C darling-workspace run west init -l .
mise -C darling-workspace run west update
mise -C darling-workspace run west dw restore
mise -C darling-workspace run west dw beads sync --import-only --rebuild
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
mise run west darling-doctor --scope workspace
mise run west dw handoff
```

`west darling-doctor --scope workspace` checks worktrees against the West
manifest; intentional drift is declared in `doctor-allow-drift.txt`.
`--scope runtime --runtime-root R --prefix P --build-dir B` checks selected
build configuration, installation integrity and guest-state prerequisites.
The default `all` combines scopes. Doctor does not boot unless `--boot-smoke`
is explicitly requested; a scoped pass is not guest acceptance.

Installation `R` and writable guest state `P` are disjoint. Configure an absolute
`CMAKE_INSTALL_PREFIX=R`, use default `COMPONENTS=stock`, build ALL and perform
the full normal install:

```sh
mise run west darling-build --build-dir "$B" --runtime-root "$R" --install
mise run west darling-bootstrap --runtime-root "$R" --prefix "$P" --no-build \
  --guest-toolchain darling-command-line-tools
```

Omit the toolchain option for runtime-only smoke. Alternatively bootstrap accepts
`--build-dir B` to build/install first. `--with-test-data` adds installed test
assets, not a partial runtime. The launcher is `R/bin/darling`; never discover
it in `P/bin` or copy system libraries into `P`. Publish a new root for new code;
never rewrite a published image. See [bootstrap](docs/test-infra.md#bootstrap-a-fresh-prefix-before-a-prefix-backed-run).

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

## Agent skills and local guardrails

Portable agent procedures live canonically in `.agents/skills/`.
The manifest's `.claude/skills -> ../.agents/skills` relative symlink exposes
the same procedures to Claude Code without maintaining duplicate copies.

- `hooks/pretooluse-build-gate.sh` — an optional `PreToolUse` (Bash) hook.
  It recognizes selected build/install/boot command patterns and blocks when
  its `west darling-doctor` invocation fails. Matching is not complete runtime
  or prefix discovery, and the hook fails open when mise or the workspace is
  unavailable. Resolve the explicit source/build/runtime configuration through
  managed entrypoints; the hook is not a lifecycle lock or acceptance gate.
- `hooks/stop-durability-reminder.sh` — a `Stop` hook. On session end it warns
  if the manifest repo is uncommitted, the independent runner is dirty, or a
  tracked source worktree is dirty, and blocks the stop once so the
  work isn't silently abandoned. It respects `stop_hook_active` (no loop).
- `.agents/skills/darling-boot/` — managed context/run/follow/cancel workflow,
  prefix-owned shutdown and ownership-checked cleanup.
- `.agents/skills/darling-diagnostics/` — guest tracing, artifact identity and
  deployment verification.
- `.agents/skills/darling-testing/` — test selection, verdicts and regression
  proof.
- `.agents/skills/darling-durability/` — dirty-work triage, source ownership and
  durable handoff.

Install local agent routing in the selected Darling checkout (the workspace
root itself is not a Git repository):

```bash
# from the workspace root
mise -C darling-workspace run dw setup-local
```

`setup-local` installs `source/AGENTS.md` only if the selected checkout has no
instruction file, preserving differing local instructions. It links that
checkout's `.agents/skills` to this manifest's canonical skills and installs
`.claude/skills -> ../.agents/skills`. Matching links are retained on repeat
invocation. Conflicting files, directories, dangling links or wrong links cause
a clear failure without replacement; reconcile those paths explicitly, keeping
any local additions. Custom instruction files, including existing instruction
symlinks, are never overwritten.

The command adds local Git excludes for instructions, Beads and the installed
skill paths through Git's resolved `info/exclude` (including gitfile-based
checkouts); it does not change the product's tracked `.gitignore`. Select a
non-default source checkout through `DW_DARLING_SRC` in the local
`workspace.env` or command environment. Install optional Claude Code hooks from
`.claude/settings.sample.json`, replacing its workspace paths. Skills remain
linked rather than copied; edit their canonical manifest sources.

`west dw handoff` exports Beads, refreshes manifests, and creates Git bundles
for every local branch except an unchanged `main`/`master`. This includes
active topics, clean work branches, and backup snapshots. Initialize the workspace
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

`west dev` is a thin, evidence-producing front end for the existing test,
doctor, deployment, Beads, and handoff authorities:

From the manifest directory, `mise run dw dev ...` selects the pinned
environment through the dw proxy task. For runtime diagnostics:

```sh
mise run dw dev context homebrew --runtime-root /absolute/path/to/runtime \
  --prefix /absolute/path/to/new-prefix
mise run dw dev run homebrew-prepare
mise run dw dev run homebrew-preflight
mise run dw dev run exact-capture
mise run dw dev run homebrew-source
```

Contexts are local West settings; `dev context NAME` selects the active context,
while `dev run --context NAME` and explicit paths override it for one run.
Select an ordinary immutable installation and a separately owned guest prefix.
Without `--executor`, the diagnostic runner may be built before execution.

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
mise run west dev --help
mise run west dev status
mise run west dev start --source /path/to/source --destination /path/to/authoring \
  --base <commit-or-ref> --branch fix/<topic> --bead <id> --module <name> \
  --evidence /path/outside/active/repos/start.json --dry-run
mise run west dev check quick \
  --evidence /path/outside/active/repos/quick.json
mise run west dev check canonical \
  --evidence /path/outside/active/repos/canonical.json
mise run west dev check acceptance --workload WORKLOAD_ID \
  --runtime-root /path/to/runtime --prefix /path/to/prefix \
  --source-root /path/to/source --build-dir /path/to/build \
  --evidence /path/outside/active/repos/acceptance.json
mise run west dev package --receipt /path/to/acceptance.json \
  --output /path/to/review-package --evidence /path/to/package.json
mise run west dev verify-package /path/to/review-package
```

`start` creates an independent exact-base clone without alternates, hardlinks,
or mutations to active West repositories. Its `--dry-run --json` form emits
the complete plan. `west dev recover-start --evidence <start.json>` recovers
an interrupted transaction. Acceptance requires explicit named `--workload`
values, `--runtime-root` and `--prefix`; repeat `--workload` for the complete
intended selection. Workloads come from flat `testkit/workloads.yml`, not source
profiles. Preserve their full oracles and report setup, runtime and native
reference evidence separately.

Source review is tracked-only: tracked unstaged and staged diffs are included;
untracked files are neither collected nor inventoried. Stage explicit new owned
files before review. There are no `--tracked-only` or `--stage-new-files` flags.

`package` requires an acceptance receipt. The package contains that receipt,
`source-review.json` and its snapshot, exact `manifest/west.yml`, per-owner
tracked working/staged diffs, `package-index.json` and `SHA256SUMS`. It does not
contain untracked bytes, recovery mboxes, portable Git object closure or the
runtime image; it is not an independently rebuildable source distribution.
`verify-package` checks the package without changing it. A receipt is not a
signature: verify again after transfer.

Installation identity in command results is a concise summary: receipt path and
SHA, artifact count, source revision/dirty state and provenance. The full schema-2
installation receipt inventories the complete actual image, including normal
install-hook files and actual CMake manifest hashes; verification rejects extra
files. A published image remains verifiable when its original build tree changes.

Default `west dev` output is bounded: dirty repositories, active operations,
and planned mutations each show at most eight entries and report the omitted
count. Every status or dry-run summary prints an exact shell-quoted `--json`
command immediately after its header for full detail. The command preserves
the recorded or current West launcher and uses `--option=value` for arbitrary
option values. A completed `check` or `package` instead places an exact
`cat --` command for its durable evidence JSON there, so inspecting detail never
reruns a mutation. Add `--json` when another tool consumes an operation.
These results use `schema_version: 1` with an `operation` discriminator
(`status`, `start`, `check`, `package`, or `package-verify`) and an
operation-specific `state`. A caught validation or operational
failure in JSON mode returns one action-specific error envelope and exits 1
without appending human diagnostics. Successful operations exit 0, recorded
command failures propagate their nonzero status, and invalid command-line
usage exits 2.

## Hard fork

This workspace is a hard fork: nothing is published upstream and no pull
request is opened. Clean `fix/*` branches in the owning repositories, together
with the manifest pins, are the source of truth; work stays local as Beads,
pruned branches, diagnostic evidence, and optional handoff bundles.

One private manifest repository is intentional. Split Beads only if it needs
different access control or an independent lifecycle.

The evidence reports `docs/branch-migration.md`,
`docs/mega-branch-audit-2026-06-12.md` and `docs/west-spike.md` preserve migration
decisions and observations; they are not operational runbooks.

`west.yml` is the workspace backend and includes workspace tools such as
`darling-debug-runner`.

# Darling workspace

Private coordination repository: manifests, Beads, local refs and agent workflow.
Darling product source belongs to the surrounding West workspace, not this repo.

## Canonical environment

- The canonical workspace is `/home/ilyagulya/work/wr-fresh`, with this manifest
  at `darling-workspace`. Do not create a second source authority or revive
  retired workspaces. Read [environment](docs/environment.md) before choosing
  source/build directories, prefixes, toolchains or state locations.
- Run source commands in the owning source repository. Run workspace orchestration
  from the manifest with `mise run dw ...` and West commands with `mise run west ...`.
  From the West root use `mise -C darling-workspace run dw|west ...`.
- `mise.toml` pins host tools. On a new checkout review it, then run `mise trust`
  and `mise install`. If tools are absent from PATH, check `$HOME/.local/bin`
  before installing anything; Beads needs `br` there on PATH too.
- Use `--` after a mise task name for opaque arguments or a literal `:::`.
  Use `mise exec -- uv ...` for isolated Python, never system installs/ensurepip.

## Ownership and safety

- Canonical product fixes belong on clean `fix/*` branches. Inspect ownership
  before editing dirty source; preserve unrelated changes, staged work and refs.
- This is a hard fork: no upstream publication or pull requests. Never push
  branches, tags or updates without approval for that exact action/destination.
- Keep process state in the owning Bead: `mise run west dw beads ...`;
  current CLI comments use `comments add ID TEXT` (also `--file`/`--message`);
  the older `comment ID TEXT` spelling is rejected. Never add Beads, agent state or workspace
  metadata to product-source commits. Stage explicit owned paths, not `git add -A`.
- Durable state belongs beneath the owned `DW_STATE_ROOT` or workspace state
  root, never `/tmp`. This includes prefixes, lanes, jobs and run evidence.
  Keep a checksummed acceptance-evidence copy outside GC-managed roots.
- One lifecycle owner per prefix: no overlapping runs, manual cleanup or deploy
  into another owner's runtime. Independent concurrent experiments need separate
  lanes. Never globally kill Darling processes or glob-delete prefixes/caches.
- Use managed run/follow/cancel and cleanup tools; observer timeout is not
  cancellation. Wait for final status AND prefix cleanup before reusing it.
  Inspect GC dry-run and ownership before removal; never run GC unattended.
- Before implementing a missing API, check this tree's vendored sources/submodules,
  then the component's newer upstream release, then sibling forks. VibeDarling is
  a read-only reference, never a build source. Record what was found and why an
  authoritative implementation cannot be reused beside any replacement.
- Do not fabricate SDK/runtime identities, bypass compatibility/integrity checks,
  or turn absent behavior into a successful fallback.

## Required task routing

Read the matching skill BEFORE acting; skills live in this manifest, not memory.
A tooling question also requires the relevant reference section and current help.

| Work | Required guidance |
|---|---|
| Guest launch, shellspawn, guest compilation, DCC, deploy or prefix repair | [darling-boot](.agents/skills/darling-boot/SKILL.md) |
| Tracing, instrumentation, artifact comparison, loader closure or fault capture | [darling-diagnostics](.agents/skills/darling-diagnostics/SKILL.md), then relevant [tooling](docs/tooling.md) / [debugger](docs/darling-debugger.md) sections |
| Adding/changing tests, runners, CMake/CTest, regression proof or acceptance claims | [darling-testing](.agents/skills/darling-testing/SKILL.md), then relevant [test infrastructure](docs/test-infra.md) sections |
| Dirty source, checkout/update, committing, handoff or ending a changing session | [darling-durability](.agents/skills/darling-durability/SKILL.md) |
| Concurrent experiments, state maintenance or deletion | [State ownership and lane safety](docs/environment.md#durable-state-and-cleanup), plus boot/durability as applicable |

- Use supported tools instead of rebuilding their functionality in ad-hoc shell.
  The complete diagnostic command roster is generated in `docs/tooling.md`, not here.
- Delegated agents do not automatically inherit this file. Every brief must tell
  them to read it and matching skills, or include the exact workspace, invocation,
  ownership and safety contract they need. Delegate independent work only.

## Evidence and completion

- Execute the changed behavior and inspect its result. Source/text matching is
  audit evidence, not behavioral coverage. Tests must catch consumer-visible
  behavior; do not pin incidental wording, command spellings or derived digests.
- Guest tests compile/run inside Darling, not Linux binaries through a launcher.
- A libsystem_kernel change requires a complete new normal installation, including dyld.
  Record the selected installed-image identity and the runtime that served each run.
- PASS needs the workload's completed oracle and cleanup. Log growth, silent
  probes, intermediate diagnostics and cached verdicts are not fresh acceptance.
  Preserve exact commands, verdicts, warnings, evidence locations and blockers.
- Handoff is optional portable archival, not a prerequisite for every session;
  it does not retain uncommitted bytes or automatically cover sibling tool repos.
  Never publish rejected experiments or initialize unrelated repos just for it.

## Maintaining instructions

- `.agents/skills/` is canonical; `.claude/skills -> ../.agents/skills` is its
  compatibility alias. Keep setup, active installations and references aligned.
- `mise run dw setup-local` links source-checkout skills and installs its template;
  it preserves differing instructions and refuses conflicting skill paths.
- Keep this file a concise core and task router. Skills contain operational
  workflows; `docs/` contains reference detail. Remove duplicates instead of
  copying them. Store chronology, incident narratives and one-off results in
  Beads or diagnostic evidence, not instructions or skills.

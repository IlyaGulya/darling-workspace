# Local Darling agent instructions

Workspace coordination belongs to the private manifest repository, not Darling
Git history. Resolve the actual West workspace and read its `darling-workspace/AGENTS.md`
before editing source. That file owns the complete current workflow; this template
is the source-checkout entry point, not a second policy implementation.

For a normal sibling layout, commands below run from the Darling source root.
For separate `fix/*` worktrees, resolve the manifest path from workspace
configuration before choosing command paths.

## Managed diagnostics

```sh
mise -C ../darling-workspace run dw dev context NAME --prefix /absolute/path/to/prefix
mise -C ../darling-workspace run dw dev run homebrew-prepare
mise -C ../darling-workspace run dw dev run homebrew-preflight
mise -C ../darling-workspace run dw dev run exact-capture
mise -C ../darling-workspace run dw dev run homebrew-source
```

Configure the context once. A new bootstrap prefix must be absent or empty.
`dev run` owns the pinned environment, default executor build, job observation
and normal West prefix lifecycle. Use `--dry-run` to inspect the command without
launching; `--context` and explicit run overrides do not rewrite configuration.
Use the printed `dev follow JOB` and `dev cancel JOB` commands for existing jobs.
Execute registered scenarios through initialized West command state and its
prefix-scoped lifecycle owner.

Other registered tests are available through `mise run dw test`. Advanced long
commands use the manifest's `scripts/west-job.sh start` and attached `follow`;
`status` is for recovery. Do not overlap runs using one prefix. Phase/log age is
an observation, not proof of a hang. Runner stdout/stderr and Homebrew log
directories are registered automatically; `--forward-output` is later replay.

A native-tools preflight pass is not a complete Homebrew source-build pass.
Derive SDK/runtime identities from authoritative metadata and validate their
compatibility; keep failing gates open without spoofing versions or disabling
package-manager checks. Exact-capture success verifies a deliberately timed-out
guest's image/core/registers and cleanup; archive completeness is independent.

## Issues and source ownership

The `dw` and `west` mise tasks own the pinned environment for every command.
Use the West task for Beads:

```sh
mise -C ../darling-workspace run west dw beads ready
mise -C ../darling-workspace run west dw beads show <id>
mise -C ../darling-workspace run west dw beads update <id> --status=in_progress
mise -C ../darling-workspace run west dw beads comment <id> "Evidence or blocker"
```

Do not run raw `br`/`bd` from source or create a second issue database. Optional
`bv --robot-*` triage belongs in the manifest directory; recheck issue state with
`west dw beads` before claiming. Never launch an unattended interactive `bv`.

- Edit canonical clean `fix/*` branches. Integration branches and profile locks
  are generated; do not edit or publish them.
- Export source changes through `west patch export`, refreshing full source SHA
  and checksum together. Preserve publication blockers and use focused profile
  verification. Follow the manifest's behavioral coverage and RED-proof rules;
  source-text matching or an unrelated startup failure is not regression proof.
- Use declared runtime providers for build/deploy/restore. For manual focused
  validation, use serialized `west darling-build`; never race raw Ninja builds.
  A libsystem_kernel behavior change also requires dyld deployment because dyld
  contains a static emulation path. Do not validate against remembered hashes.
- Keep `.beads/`, agent instructions, PR drafts and handoff state out of source
  commits. Stage exact owned paths; do not absorb unrelated work.

## Completion and durability

1. Record actual verdicts, diagnostic paths and unresolved blockers in the Bead.
   Preserve full bootstrap failure evidence before interpreting a restored prefix.
2. Commit owned source changes and refresh their portable patch metadata; commit
   owned manifest changes separately. No blanket staging or automatic push.
3. Run `mise -C ../darling-workspace run west dw handoff` after Bead/private-ref
   changes, then commit only the files that handoff actually changed.
4. Verify every changed canonical tip in its keeper bundle. A forest handoff may
   omit sibling tooling repositories; preserve those explicitly. Local contexts
   and diagnostic archives are not product patches.
5. No push or mutating PR operation without explicit approval for that action and
   destination. Fork approval never permits an upstream mutation.

The manifest's `mise run dw setup-local` installs this template only when no source
instruction file exists. It preserves differing installed copies: inspect local
additions and synchronize the installed file with the template explicitly.

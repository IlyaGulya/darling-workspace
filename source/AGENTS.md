# Local Darling agent instructions

This checkout is product source; workspace coordination belongs to the private
manifest repository. Resolve the actual West workspace and read its
`darling-workspace/AGENTS.md` before editing. For separate `fix/*` worktrees,
resolve the manifest from workspace configuration rather than assuming siblings.

## Workflow routes

- Run orchestration from the manifest with `mise run dw ...` and advanced West
  commands with `mise run west ...`; these select the pinned environment.
  In a normal sibling layout, use `mise -C ../darling-workspace run ...` here.
- Read the linked `.agents/skills/<name>/SKILL.md` before the matching work:
  `darling-boot` for guest launches and managed context/run/follow/cancel;
  `darling-diagnostics` for tracing, artifact identity and deploy verification;
  `darling-testing` for test selection, verdicts and regression proof;
  `darling-durability` for dirty source, checkout/update and handoff.
  `.claude/skills` is a compatibility link to the same canonical skills.
- Read the manifest's `docs/environment.md` before choosing build directories
  or prefixes and `docs/tooling.md` before low-level tooling. Do not overlap
  prefix-backed runs or bypass the owning lifecycle/deploy transaction.
- Keep issue state in the manifest's Beads database via
  `mise run west dw beads ...`; never create another database in source.

## Product-source safeguards

- Edit canonical clean `fix/*` branches, never generated integration branches.
  Before implementing a missing API, follow the manifest's authoritative-source
  reuse rules rather than inventing a replacement.
- Use a normal stock ALL build and complete ordinary install into immutable `R`;
  select writable guest state `P` separately (`--runtime-root R --prefix P`).
  Follow the boot skill for install/lease/bind-clone details; never deploy into
  `P`, discover `P/bin/darling` or overwrite a published image.
  A libsystem_kernel change also requires dyld in the same complete image.
- Do not spoof SDK/runtime identities or disable compatibility gates. A native
  preflight is not a full source-build pass; diagnostic success and archive
  completeness are independent. Follow the testing skill's coverage rules.
- Keep `.beads/`, agent instructions, skills and handoff state out of product
  commits. Stage exact owned paths; preserve unrelated work. Record actual
  verdicts, evidence locations and blockers in the owning Bead.
- Source review includes tracked working/staged diffs only. Stage explicit new
  owned files before review; untracked bytes are neither collected nor backed up.
- Commit source and manifest changes separately. Preserve dirty work and owned
  refs before changing checkouts; a handoff does not retain uncommitted bytes or
  automatically cover sibling tooling repositories and external archives.
- No push or publication without explicit approval for that action and
  destination. This is a hard fork: nothing goes upstream.

`mise run dw setup-local` links skills and installs this template only when no
source instruction file exists. It preserves differing instructions and fails
without replacing conflicting skill paths; reconcile local additions explicitly.

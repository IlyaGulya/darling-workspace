# Darling workspace

This repository contains private coordination state, not Darling source code.
It is the source of truth for workspace manifests, tasks, unpublished branch
refs, PR drafts, and agent handoff.

- Run source commands in the West workspace, not in this manifest repository.
- Use `west dw beads ...` for issue operations.
- Use `west patch apply|clean|list` for local integration profiles.
- Run `west dw handoff` before ending a session that changed Beads or private
  branches.
- Never add workspace metadata, PR drafts, agent state, or Beads files to the
  Darling source repositories.
- Do not push investigation branches unless explicitly requested.

# Darling workspace

This repository contains private coordination state, not Darling source code.

- Run source commands in the checkout reported by `bin/dw env`.
- Use `bin/dw beads ...` for issue operations.
- Run `bin/dw sync` before ending a session that changed Beads or repository
  positions.
- Never add workspace metadata, PR drafts, agent state, or Beads files to the
  Darling source repositories.
- Do not push investigation branches unless explicitly requested.


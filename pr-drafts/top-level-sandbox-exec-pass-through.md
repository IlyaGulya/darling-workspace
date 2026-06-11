# PR draft - top-level: ship sandbox-exec pass-through

- **beads:** dar-q95.21
- **repo:** darlinghq/darling
- **current commit:** `f85f220f0`
- **clean PR branch:** not split yet
- **files:** `src/sandbox/CMakeLists.txt`, `src/sandbox/sandbox-exec.sh`

## Title
sandbox: ship a sandbox-exec pass-through command

## Body
Homebrew invokes `sandbox-exec` during formula builds. Darling did not provide
the command, forcing `HOMEBREW_NO_SANDBOX=1` for source builds. Install a
minimal pass-through `sandbox-exec` command so build scripts can run through
the normal Homebrew sandbox entry point.

## Review Note

This is a compatibility shim, not a real sandbox implementation. Upstream review
should decide whether a pass-through command is acceptable, whether it should
warn, or whether unsupported policies should fail explicitly. Do not present
this as sandbox semantics; it only removes the "command missing" incompatibility
for build tools such as Homebrew.

## Tests

- Homebrew source-build path without `HOMEBREW_NO_SANDBOX`

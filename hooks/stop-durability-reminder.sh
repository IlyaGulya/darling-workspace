#!/usr/bin/env bash
# Claude Code Stop hook for the Darling workspace.
#
# When the session is about to stop, warn if there is undurable work:
#   - an uncommitted manifest repository;
#   - dirty worktrees in the source forest or independent runner.
# Follow the darling-durability skill for ownership and preservation rules.
#
# It BLOCKS the stop once (decision: block) with a reminder so the work isn't
# silently abandoned. It NEVER loops: if stop_hook_active is true (we already
# fired), it allows the stop immediately.
#
# Contract: reads event JSON on stdin; exit 0 with optional JSON on stdout.
# {"decision":"block","reason":"..."} keeps the session going with the reason
# shown to Claude; no decision = allow normal stop.

set -uo pipefail

MANIFEST="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="${DARLING_WORKSPACE:-$(dirname -- "$MANIFEST")}"
MANIFEST="$WORKSPACE/darling-workspace"

INPUT="$(cat)"

# Loop-breaker: if we already blocked once this stop cycle, let it stop.
active=""
if command -v jq >/dev/null 2>&1; then
  active="$(printf '%s' "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null)"
fi
[ "$active" = "true" ] && exit 0

# Fail-open if the workspace isn't here (wrong machine / not initialized).
[ -d "$MANIFEST/.git" ] || [ -f "$MANIFEST/.git" ] || exit 0

warnings=""

# 1. Manifest repo uncommitted?  (highest risk)
if [ -n "$(git -C "$MANIFEST" status --porcelain 2>/dev/null)" ]; then
  warnings="${warnings}  - manifest repo (darling-workspace) has uncommitted changes\n"
fi

# 2. Dirty worktrees in the repos we actually care about (fast, targeted — not a full west forall).
for rel in \
    "darling" \
    "darling-debug-runner" \
    "darling/src/external/dyld" \
    "darling/src/external/darlingserver" \
    "darling/src/external/xnu"; do
  repo="$WORKSPACE/$rel"
  [ -d "$repo/.git" ] || [ -f "$repo/.git" ] || continue
  # For the superproject, ignore submodule-pointer drift (expected, not a risk).
  if [ "$rel" = "darling" ]; then
    dirty="$(git -C "$repo" status --porcelain --ignore-submodules=all --untracked-files=no 2>/dev/null)"
  else
    dirty="$(git -C "$repo" status --porcelain 2>/dev/null)"
  fi
  [ -n "$dirty" ] && warnings="${warnings}  - dirty worktree: $rel\n"
done

# Nothing at risk → allow stop silently.
[ -z "$warnings" ] && exit 0

REASON="Undurable Darling work detected before stopping:\n${warnings}\nFollow the darling-durability skill: preserve owned source, commit exact paths, run mise run west dw handoff, and verify forest and runner keeper refs. Record any intentionally uncommitted work explicitly. Respect an explicit user stop."

# Emit a block decision (exit 0 + JSON). Prefer jq for safe JSON encoding.
if command -v jq >/dev/null 2>&1; then
  jq -cn --arg r "$REASON" '{decision:"block", reason:$r}'
else
  # Minimal manual JSON (reason kept simple); newlines already escaped as \n literals.
  printf '{"decision":"block","reason":"%s"}\n' "$(printf '%s' "$REASON" | sed 's/"/\\"/g')"
fi
exit 0

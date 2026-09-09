#!/usr/bin/env bash
# Claude Code PreToolUse hook (matcher: Bash) for the Darling workspace.
#
# Runs the pinned doctor before explicit Darling build/deploy/boot commands.
# Exit 2 blocks a command whose workspace/runtime doctor gate fails.
#
# Design:
#  - NARROW match: only fires on explicit build/deploy/boot patterns, never on
#    ordinary shell commands.
#  - ESCAPE HATCH: a command containing DARLING_SKIP_DOCTOR (env or literal) or
#    `--force`/`--skip-doctor` is allowed through (intentional override).
#  - FAIL-OPEN when mise or the West workspace is unavailable.
#
# Contract: reads event JSON on stdin; exit 0 = allow, exit 2 = block (stderr
# becomes Claude's feedback).

set -uo pipefail

MANIFEST="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="${DARLING_WORKSPACE:-$(dirname -- "$MANIFEST")}"

INPUT="$(cat)"

# Extract the bash command string. Prefer jq; fall back to python3.
CMD=""
if command -v jq >/dev/null 2>&1; then
  CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)"
fi
if [ -z "$CMD" ] && command -v python3 >/dev/null 2>&1; then
  CMD="$(printf '%s' "$INPUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)"
fi

# No command / couldn't parse → allow (fail-open; parsing is not our job to police).
[ -z "$CMD" ] && exit 0

# Explicit override → allow.
case "$CMD" in
  *DARLING_SKIP_DOCTOR*|*--skip-doctor*|*--force*) exit 0 ;;
esac

# Narrow build/deploy/boot detection. Keep this list tight to avoid false blocks.
#  - ninja / cmake --build in a darling build tree
#  - copying into a prefix's libexec/darling closure tree (a deploy)
#  - west darling-build with --deploy
#  - booting the guest (darling shell / darling shutdown-then-boot / shellspawn)
is_target=0
case "$CMD" in
  *"west darling-build"*)                 is_target=1 ;;
  *ninja*)                                is_target=1 ;;
  *"cmake --build"*)                      is_target=1 ;;
  *"libexec/darling"*cp*|*cp*"libexec/darling"*) is_target=1 ;;   # deploy into closure tree
  *"darling shell"*)                      is_target=1 ;;
  *"darling shutdown"*)                   is_target=1 ;;
  *shellspawn*)                           is_target=1 ;;
esac
[ "$is_target" -eq 0 ] && exit 0

# Guard requires the pinned task entry and a West workspace.
if ! command -v mise >/dev/null 2>&1; then exit 0; fi
if [ ! -d "$WORKSPACE/.west" ]; then exit 0; fi

DOCTOR_OUT="$(mise -C "$WORKSPACE/darling-workspace" run west darling-doctor 2>&1)"
DOCTOR_RC=$?

if [ "$DOCTOR_RC" -ne 0 ]; then
  {
    echo "BLOCKED by darling-doctor build/deploy/boot gate."
    echo "The command looks like a Darling build/deploy/boot, but"
    echo "\`mise run west darling-doctor\` reports a failed gate:"
    echo
    echo "$DOCTOR_OUT"
    echo
    echo "Inspect the complete report, resolve the configuration or deployment"
    echo "mismatch, and retry through the workspace's managed entrypoint."
  } >&2
  exit 2
fi

exit 0

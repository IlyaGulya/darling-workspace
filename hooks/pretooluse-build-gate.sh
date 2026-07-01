#!/usr/bin/env bash
# Claude Code PreToolUse hook (matcher: Bash) for the Darling workspace.
#
# Runs `west darling-doctor` BEFORE any command that looks like a Darling
# build / deploy / boot, and BLOCKS (exit 2) if the doctor fails. This catches
# the perf#24c2c-pre detours automatically:
#   #89  building in the wrong build dir (/usr/local instead of ~/work/darling-build)
#   #90  a project worktree drifted from its West manifest revision
# and a deploy that would diverge the prod baseline (dyld/mldr/dserver md5).
#
# Design:
#  - NARROW match: only fires on explicit build/deploy/boot patterns, never on
#    ordinary shell commands.
#  - ESCAPE HATCH: a command containing DARLING_SKIP_DOCTOR (env or literal) or
#    `--force`/`--skip-doctor` is allowed through (intentional override).
#  - FAIL-OPEN on infrastructure problems (no west / not in workspace): never
#    block work just because the guard itself can't run; only block on a real
#    doctor failure.
#
# Contract: reads event JSON on stdin; exit 0 = allow, exit 2 = block (stderr
# becomes Claude's feedback).

set -uo pipefail

WORKSPACE="${DARLING_WORKSPACE:-$HOME/work/darling-dev}"

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

# Guard can only run if west + workspace are present. Fail-open otherwise.
if ! command -v west >/dev/null 2>&1; then exit 0; fi
if [ ! -d "$WORKSPACE/.west" ]; then exit 0; fi

DOCTOR_OUT="$(cd "$WORKSPACE" && west darling-doctor 2>&1)"
DOCTOR_RC=$?

if [ "$DOCTOR_RC" -ne 0 ]; then
  {
    echo "BLOCKED by darling-doctor build/deploy/boot gate."
    echo "The command looks like a Darling build/deploy/boot, but"
    echo "\`west darling-doctor\` reports drift (this is exactly what caused #89/#90):"
    echo
    echo "$DOCTOR_OUT"
    echo
    echo "Fix the drift, or if this override is intentional, re-run the command"
    echo "with --force (or prefix DARLING_SKIP_DOCTOR=1)."
  } >&2
  exit 2
fi

exit 0

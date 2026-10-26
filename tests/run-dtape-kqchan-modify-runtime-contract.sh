#!/usr/bin/env bash
# Run the retained EV_ADD-on-existing-knote kqchan modify regression through
# west test's ordinary installed-runtime owner and structured guest-C runner.
# The reviewed guest CLT compiles the fixture inside Darling; no host product
# build, copied executable, private runtime, or independent cleanup is used.
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
runtime_root="${DARLING_RUNTIME_ROOT:-}"
prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
west_args=()
usage() {
	printf '%s\n' \
		"Usage: $0 --runtime-root ROOT --prefix PREFIX [--executor PATH]" \
		"       [--bundle-root DIR] [--runtime-evidence-root DIR] [--proof-scratch-root DIR] [--source-root DIR]" \
		"ROOT and PREFIX may instead be selected by DARLING_RUNTIME_ROOT and DPREFIX." \
		"Provision CLT first: west darling-bootstrap --no-build --build-dir BUILD --runtime-root ROOT --prefix PREFIX --guest-toolchain darling-command-line-tools."
}
while [ "$#" -gt 0 ]; do
	case "$1" in
		-h|--help) usage; exit 0 ;;
		--runtime-root|--prefix|--executor|--bundle-root|--runtime-evidence-root|--proof-scratch-root|--source-root)
			[ "$#" -ge 2 ] || { usage >&2; exit 2; }
			case "$1" in
				--runtime-root) runtime_root=$2 ;;
				--prefix) prefix=$2 ;;
				*) west_args+=("$1" "$2") ;;
			esac
			shift 2
			;;
		*) echo "unsupported argument: $1" >&2; usage >&2; exit 2 ;;
	esac
done
[ -n "$prefix" ] || { usage >&2; exit 2; }
DARLING_WORKSPACE_ROOT=$workspace_root
source "$workspace_root/scripts/darling-runtime.sh"
darling_resolve_runtime "$runtime_root" "$prefix"

mise="$(command -v mise || true)"
if [ -z "$mise" ] && [ -x "$HOME/.local/bin/mise" ]; then mise="$HOME/.local/bin/mise"; fi
[ -n "$mise" ] || { echo "mise is required for the canonical west test runner" >&2; exit 2; }
export DW_STATE_ROOT="${DW_STATE_ROOT:-${workspace_root%/*}/.west-state/dtape-kqchan-modify}"
export WEST_TEST_VERDICT_CACHE=off
exec "$mise" -C "$workspace_root" run west -- test \
	--runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$prefix" \
	--workload dtape_kqchan_modify_runtime "${west_args[@]}"

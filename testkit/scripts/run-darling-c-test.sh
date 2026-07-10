#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=darling-guest-shell.sh
source "$script_dir/darling-guest-shell.sh"

name=
source=
guest_cc="${DARLING_GUEST_CC:-/Library/Developer/CommandLineTools/usr/bin/clang}"
guest_cflags="${DARLING_GUEST_CFLAGS:--isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk}"
launcher="${DARLING_LAUNCHER:-}"
ok_marker=
args=()

while (($#)); do
	case "$1" in
		--name)
			name="$2"
			shift 2
			;;
		--source)
			source="$2"
			shift 2
			;;
		--launcher)
			launcher="$2"
			shift 2
			;;
		--cc)
			guest_cc="$2"
			shift 2
			;;
		--cflags)
			guest_cflags="$2"
			shift 2
			;;
		--ok-marker)
			ok_marker="$2"
			shift 2
			;;
		--)
			shift
			args=("$@")
			break
			;;
		*)
			echo "unknown argument: $1" >&2
			exit 2
			;;
	esac
done

if [[ -z "$name" || -z "$source" ]]; then
	echo "usage: run-darling-c-test.sh --name NAME --source PATH [--launcher PATH] [--ok-marker TEXT] [-- ARGS...]" >&2
	exit 2
fi
if [[ -z "$launcher" ]]; then
	echo "$name: DARLING_LAUNCHER is unset" >&2
	exit 2
fi
prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
if [[ -z "$prefix" ]]; then
	echo "$name: DPREFIX or DARLING_PREFIX is unset" >&2
	exit 2
fi
if [[ ! -f "$source" ]]; then
	echo "$name: source not found: $source" >&2
	exit 2
fi

safe_name="${name//[^A-Za-z0-9_.-]/_}"
run_id="${WEST_GUEST_C_FIXTURE_ID:-$$.$RANDOM}"
guest_src="/tmp/${safe_name}.${run_id}.c"
guest_bin="/tmp/${safe_name}.${run_id}"
output="$(mktemp "${TMPDIR:-/tmp}/west-ctest-guest-c.${safe_name}.XXXXXX")"
trap 'rm -f "$output"' EXIT

quoted_args=()
for arg in "${args[@]}"; do
	quoted_args+=("$(printf '%q' "$arg")")
done

set +e
darling_guest_shell "$launcher" "$prefix" "${DARLING_GUEST_TIMEOUT_SECONDS:-60}" "
set -euo pipefail
cleanup() {
  rm -f '$guest_src' '$guest_bin'
}
trap cleanup EXIT
cat > '$guest_src'
$guest_cc $guest_cflags -o '$guest_bin' '$guest_src'
'$guest_bin' ${quoted_args[*]}
" <"$source" >"$output" 2>&1
rc=$?
set -e
cat "$output"

if [ "$rc" -eq 0 ] && [ -n "$ok_marker" ]; then
	grep -F -x -q -- "$ok_marker" "$output"
fi

exit "$rc"

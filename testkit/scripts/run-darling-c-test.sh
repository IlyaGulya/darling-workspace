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
ok_marker_file=
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
		--ok-marker-file)
			ok_marker_file="$2"
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
	echo "usage: run-darling-c-test.sh --name NAME --source PATH [--launcher PATH] [--ok-marker TEXT|--ok-marker-file PATH] [-- ARGS...]" >&2
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
if [[ -n "$ok_marker" && -n "$ok_marker_file" ]]; then
	echo "$name: --ok-marker and --ok-marker-file are mutually exclusive" >&2
	exit 2
fi
if [[ -n "$ok_marker_file" ]]; then
	if [[ ! -f "$ok_marker_file" ]]; then
		echo "$name: ok marker file not found: $ok_marker_file" >&2
		exit 2
	fi
	ok_marker="$(<"$ok_marker_file")"
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

dump_file_sha() {
	local label="$1"
	local path="$2"
	if [[ -e "$path" ]]; then
		sha256sum "$path" 2>/dev/null | sed "s#^#WEST_GUEST_FILE_SHA256 $label #; s#  # #g" >&2 || true
	else
		printf 'WEST_GUEST_FILE_MISSING %s %s\n' "$label" "$path" >&2
	fi
}

dump_runtime_file_state() {
	dump_file_sha launcher "$launcher"
	dump_file_sha prefix_libsystem_kernel "$prefix/usr/lib/system/libsystem_kernel.dylib"
	dump_file_sha prefix_nested_libsystem_kernel \
		"$prefix/libexec/darling/usr/lib/system/libsystem_kernel.dylib"
}

timeout_seconds="${DARLING_GUEST_TIMEOUT_SECONDS:-60}"

cleanup_guest_artifacts() {
	darling_guest_shell "$launcher" "$prefix" 10 \
		"rm -f '$guest_src' '$guest_bin'" >/dev/null 2>&1 || true
}
trap cleanup_guest_artifacts EXIT

run_guest_stage() {
	local stage="$1"
	local script="$2"
	printf 'WEST_GUEST_STAGE=%s\n' "$stage" >>"$output"
	darling_guest_shell "$launcher" "$prefix" "$timeout_seconds" "$script" \
		>>"$output" 2>&1
}

set +e
run_guest_stage upload "cat > '$guest_src'" <"$source"
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
	set +e
	run_guest_stage compile "set +e; $guest_cc $guest_cflags -o '$guest_bin' '$guest_src'; rc=\$?; printf 'ORACLE_RC=%s\\n' \"\$rc\"; exit \"\$rc\""
	rc=$?
	set -e
fi
if [ "$rc" -eq 0 ]; then
	set +e
	run_guest_stage run "set +e; '$guest_bin' ${quoted_args[*]}; rc=\$?; printf 'ORACLE_RC=%s\\n' \"\$rc\"; exit \"\$rc\""
	rc=$?
	set -e
fi
cat "$output"

if [[ "$rc" -ne 0 ]]; then
	dump_runtime_file_state
fi

if [ "$rc" -eq 0 ] && [ -n "$ok_marker" ]; then
	grep -F -x -q -- "$ok_marker" "$output"
fi

exit "$rc"

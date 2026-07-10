#!/usr/bin/env bash
set -euo pipefail

marker=
marker_file=
while (($#)); do
	case "$1" in
		--marker)
			marker="$2"
			shift 2
			;;
		--marker-file)
			marker_file="$2"
			shift 2
			;;
		--)
			shift
			break
			;;
		*)
			echo "usage: expect-failure.sh --marker TEXT|--marker-file PATH -- COMMAND [ARGS...]" >&2
			exit 2
			;;
	esac
done

if [[ -n "$marker" && -n "$marker_file" ]]; then
	echo "--marker and --marker-file are mutually exclusive" >&2
	exit 2
fi
if [[ -n "$marker_file" ]]; then
	if [[ ! -f "$marker_file" ]]; then
		echo "marker file not found: $marker_file" >&2
		exit 2
	fi
	marker="$(<"$marker_file")"
fi
if [[ -z "$marker" || $# -eq 0 ]]; then
	echo "usage: expect-failure.sh --marker TEXT|--marker-file PATH -- COMMAND [ARGS...]" >&2
	exit 2
fi

output="$(mktemp "${TMPDIR:-/tmp}/west-ctest-red.XXXXXX")"
trap 'rm -f "$output"' EXIT

set +e
"$@" >"$output" 2>&1
rc=$?
set -e
cat "$output"

if [[ "$rc" -eq 0 ]]; then
	echo "RED command unexpectedly passed" >&2
	exit 1
fi
if ! grep -F -q -- "$marker" "$output"; then
	echo "RED command failed without expected marker: $marker" >&2
	exit 1
fi

echo "WEST_TEST_RED_OK: $marker"

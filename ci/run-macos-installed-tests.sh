#!/usr/bin/env bash
set -euo pipefail

bundle="${1:?installed compatibility bundle is required}"
manifest="$bundle/compat-install-manifest.tsv"
[ -f "$manifest" ] || {
	echo "missing compatibility manifest: $manifest" >&2
	exit 2
}

count=0
while IFS=$'\t' read -r name executable marker; do
	[ -n "$name" ] || continue
	path="$bundle/testcase/$executable"
	[ -x "$path" ] || {
		echo "$name: installed testcase is not executable: $path" >&2
		exit 1
	}
	output="$($path 2>&1)" || {
		printf '%s\n' "$output" >&2
		echo "$name: native macOS testcase failed" >&2
		exit 1
	}
	if [ -n "$marker" ] && ! grep -F -q -- "$marker" <<<"$output"; then
		printf '%s\n' "$output" >&2
		echo "$name: missing expected marker: $marker" >&2
		exit 1
	fi
	printf 'PASS macos/%s\n' "$name"
	count=$((count + 1))
done <"$manifest"

[ "$count" -gt 0 ] || {
	echo 'installed compatibility manifest contains no tests' >&2
	exit 1
}

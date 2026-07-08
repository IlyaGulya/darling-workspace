#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

fail() {
	printf 'FAIL: %s\n' "$*" >&2
	exit 1
}

guarded="$(
	west test --profile homebrew \
		--patch xnu/getattrlist-shared-packer.patch \
		--list
)"

printf '%s\n' "$guarded" | grep -q 'diag:guarded' ||
	fail 'guarded metadata did not resolve to diag:guarded'
printf '%s\n' "$guarded" | grep -q 'darling-debug-runner run ' ||
	fail 'guarded metadata was not wrapped in darling-debug-runner'

bare="$(
	west test --profile homebrew \
		--patch xnu/psynch-negative-errno.patch \
		--list
)"

printf '%s\n' "$bare" | grep -q 'diag:bare' ||
	fail 'host metadata did not resolve to diag:bare'
if printf '%s\n' "$bare" | grep -q 'darling-debug-runner run '; then
	fail 'bare host metadata was unexpectedly wrapped'
fi

printf 'PASS west-test-metadata-contract\n'

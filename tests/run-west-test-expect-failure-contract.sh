#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runner="$root/testkit/scripts/expect-failure.sh"

if ! "$runner" --marker 'expected failure' -- bash -c 'echo expected failure >&2; exit 7'; then
	echo 'expected marker and non-zero exit were rejected' >&2
	exit 1
fi
if "$runner" --marker 'expected failure' -- bash -c 'exit 0'; then
	echo 'successful command was accepted as RED' >&2
	exit 1
fi
if "$runner" --marker 'expected failure' -- bash -c 'echo unrelated >&2; exit 7'; then
	echo 'wrong failure reason was accepted as RED' >&2
	exit 1
fi

echo 'PASS west-test-expect-failure-contract'

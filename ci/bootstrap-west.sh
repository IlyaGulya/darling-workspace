#!/usr/bin/env bash
set -euo pipefail

manifest="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
top="$(dirname "$manifest")"

command -v west >/dev/null || {
	echo 'west is required; install it before bootstrapping the workspace' >&2
	exit 2
}

if ! west topdir >/dev/null 2>&1; then
	west init -l "$manifest" "$top"
fi
west update

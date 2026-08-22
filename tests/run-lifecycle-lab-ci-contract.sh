#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
owned_root="$(mktemp -d "${TMPDIR:-/tmp}/dlc.XXXXXX")"
cleanup() {
	rm -rf -- "$owned_root"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$owned_root"
python3 -B \
	"$repo/tests/west_test_contracts/lifecycle_lab_ci_contract.py" \
	"$repo" "$owned_root"

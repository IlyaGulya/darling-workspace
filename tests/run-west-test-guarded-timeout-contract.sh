#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec mise -C "$repo" exec -- uv run --project "$repo" --frozen -- python -B \
	"$repo/tests/west_test_contracts/guarded_timeout_contract.py"

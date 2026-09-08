#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec mise exec -- uv run --no-project --with west==1.5.0 python -B \
	"$repo/tests/west_test_contracts/guarded_timeout_contract.py"

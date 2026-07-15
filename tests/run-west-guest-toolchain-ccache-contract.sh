#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
exec env PYTHONDONTWRITEBYTECODE=1 python3 -B \
	"$repo/tests/west_test_contracts/guest_toolchain_ccache_contract.py"

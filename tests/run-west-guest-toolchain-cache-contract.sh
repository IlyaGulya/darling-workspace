#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$repo/west_commands" python3 -B \
	"$repo/tests/west_test_contracts/guest_toolchain_cache_contract.py"

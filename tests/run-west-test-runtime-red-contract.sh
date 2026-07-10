#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

export PYTHONDONTWRITEBYTECODE=1

python3 tests/west_test_contracts/runtime_red_contract.py
west test --bead dar-cps --list >/dev/null

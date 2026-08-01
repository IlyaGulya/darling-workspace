#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -B "$repo/tests/west_test_contracts/prefix_lock_diagnostic_contract.py"

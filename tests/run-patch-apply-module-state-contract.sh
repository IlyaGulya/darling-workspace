#!/usr/bin/env bash
set -euo pipefail

# Covers the complete-report rule for module state: apply and clean name every
# module that is out of state in one run, and collecting them mutates nothing.
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
python3 -B "$repo/tests/west_test_contracts/patch_apply_module_state_contract.py"

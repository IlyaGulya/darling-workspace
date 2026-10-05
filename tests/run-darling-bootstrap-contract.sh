#!/usr/bin/env bash
# Direct bootstrap entrypoint: proves the West-native prefix path reads an explicit deploy plan and never a
# source selection, that a malformed plan is refused instead of partially deployed, and that the plan's Ring
# defines are the ones the manifest-native Ring provider declares.
set -euo pipefail

cd "$(dirname "$0")/.."
exec env PYTHONDONTWRITEBYTECODE=1 python3 tests/west_test_contracts/darling_bootstrap_contract.py

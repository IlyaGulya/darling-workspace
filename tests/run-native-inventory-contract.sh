#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
tmp="$(mktemp -d "${TMPDIR:-/tmp}/west-native-inventory.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

mise exec -- uv run --no-project --with west==1.5.0 python -B \
  tests/west_test_contracts/native_inventory_contract.py
mise exec -- uv run --no-project --with west==1.5.0 python -B \
  scripts/audit-test-registration.py "$tmp/inventory"

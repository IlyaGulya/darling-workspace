#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
printf '%s\n' 'coverage-tier: source'
python3 -B "$repo/tests/west_test_contracts/namespace_writer_inventory_contract.py"

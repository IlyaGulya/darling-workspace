#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
# The contract parses YAML, so it needs an interpreter that has PyYAML; the plain
# "mise exec -- python3" form resolved an interpreter without it and the contract
# could not run at all. Use the same ephemeral west environment as the other
# YAML-consuming contracts.
exec mise exec -- uv run --no-project --with west==1.5.0 python -B \
	tests/west_test_contracts/perf_archive_forensic_contract.py

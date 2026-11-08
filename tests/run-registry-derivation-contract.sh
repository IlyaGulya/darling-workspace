#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
printf '%s\n' 'coverage-tier: source'
# Registry generators consume PyYAML from the locked host environment.
exec mise exec -- uv run --frozen -- python -B \
	tests/west_test_contracts/registry_derivation_contract.py

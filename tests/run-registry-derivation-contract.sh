#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
printf '%s\n' 'coverage-tier: source'
# The derivation contract drives the registry generators, and two of them parse
# YAML, so it needs an interpreter that has PyYAML: the plain "mise exec --
# python3" form resolves one without it and the generators die on import. Use
# the same ephemeral west environment as the other YAML-consuming contracts.
exec mise exec -- uv run --no-project --with west==1.5.0 python -B \
	tests/west_test_contracts/registry_derivation_contract.py

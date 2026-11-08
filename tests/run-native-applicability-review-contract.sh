#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
# The West fixture and production YAML normalizer use the locked host environment.
exec mise exec -- uv run --frozen -- python -B \
	tests/west_test_contracts/native_applicability_review_contract.py

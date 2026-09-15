#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
# The contract builds a West fixture workspace and normalizes YAML manifests with
# the production normalizer, so it runs in the same ephemeral west environment as
# the other YAML-consuming contracts.
exec mise exec -- uv run --no-project --with west==1.5.0 python -B \
	tests/west_test_contracts/native_applicability_review_contract.py

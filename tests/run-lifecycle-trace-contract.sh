#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
exec env PYTHONDONTWRITEBYTECODE=1 mise exec -- uv run --with jsonschema==4.23.0 -- python3 -B tests/west_test_contracts/lifecycle_trace_schema_contract.py

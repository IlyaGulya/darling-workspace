#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec env PYTHONDONTWRITEBYTECODE=1 python3 -B tests/west_test_contracts/metadata_invocation_identity_contract.py

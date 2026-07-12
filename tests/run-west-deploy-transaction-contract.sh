#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
python3 -B tests/west_test_contracts/deploy_transaction_contract.py

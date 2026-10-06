#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec env PYTHONDONTWRITEBYTECODE=1 python3 -u tests/west_test_contracts/truthful_nofile_contract.py

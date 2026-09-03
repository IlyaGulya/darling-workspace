#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec env PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/west_test_contracts/west_extension_help_contract.py"

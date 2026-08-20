#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 -B "$ROOT/tests/west_test_contracts/runtime_lower_binding_contract.py"

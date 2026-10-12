#!/usr/bin/env bash
set -euo pipefail

# Covers the rung-loop tooling: the analyzer's declaration source and wave sweep, the rung driver,
# and pin sync.  Every case is synthetic (a temporary tree, manifest, payload and superproject), so
# the host tier can run it: no Xcode tree, no prefix, no build directory and no llvm tools are
# touched.  It also imports the tools as modules, so PYTHONDONTWRITEBYTECODE keeps the checkout free
# of __pycache__.
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
python3 -B "$repo/tests/west_test_contracts/rung_tooling_contract.py"

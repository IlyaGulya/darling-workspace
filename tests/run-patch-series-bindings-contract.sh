#!/usr/bin/env bash
set -euo pipefail

# Covers the export's binding report: which lock, receipt row and compositions a
# series change leaves behind, and silence when they already agree.
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
python3 -B "$repo/tests/west_test_contracts/patch_series_bindings_contract.py"

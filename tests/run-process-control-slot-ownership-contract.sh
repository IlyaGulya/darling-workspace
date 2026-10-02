#!/usr/bin/env bash
# Thin contract entrypoint for the process-control slot ownership model (dar-b5pe).
#
# The model itself lives in tests/process_control_slot_ownership_model.py so this file stays a one-line dispatcher,
# the way the other host contracts in this directory are written. The contract is RED against the protocol in the
# tree today (case C: a late completion for generation A lands on generation B) and must be GREEN once abandonment
# is generation/ownership-safe.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

export PYTHONDONTWRITEBYTECODE=1
python3 tests/process_control_slot_ownership_model.py

#!/usr/bin/env bash
# Host-only bootstrap rejection/state/integrity proof; real guest acceptance runs separately.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -B "$repo/tests/west_test_contracts/darling_bootstrap_contract.py"

#!/usr/bin/env bash
# Real host compiler/CMake/immutable-install consumer proof; no guest runtime is simulated.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -B "$repo/tests/west_test_contracts/runtime_install_contract.py"

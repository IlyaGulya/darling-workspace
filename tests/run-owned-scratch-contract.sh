#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${DARLING_SCRATCH_CENSUS_HELPER:?provide the prebuilt darling-scratch-census helper}"
exec python3 -B "$repo/tests/west_test_contracts/owned_scratch_contract.py"

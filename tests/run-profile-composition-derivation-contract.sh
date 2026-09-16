#!/usr/bin/env bash
set -euo pipefail

# Covers the profile-composition derivation's decisions. The derivation itself
# replays every locked series and needs the immutable mirror, so it is not run
# here; the gate for a stale receipt is the profile materialization in the tier.
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
python3 -B "$repo/tests/west_test_contracts/profile_composition_derivation_contract.py"

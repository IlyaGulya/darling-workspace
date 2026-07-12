#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

export PYTHONDONTWRITEBYTECODE=1
python3 -B tests/west_test_contracts/source_worktree_contract.py

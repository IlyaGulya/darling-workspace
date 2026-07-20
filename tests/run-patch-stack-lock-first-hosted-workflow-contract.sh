#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -B "$repo/tests/west_test_contracts/patch_stack_lock_first_hosted_workflow_contract.py"

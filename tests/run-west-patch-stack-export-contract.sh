#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 -B "$repo/tests/west_test_contracts/patch_stack_export_contract.py"

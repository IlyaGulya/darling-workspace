#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_tmp="$(mktemp -d "${TMPDIR:-/tmp}/dar-4ush.6.XXXXXX")"
cleanup() {
    status=$?
    rm -rf -- "$task_tmp"
    exit "$status"
}
trap cleanup EXIT INT TERM

export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$task_tmp/tmp"
mkdir -p "$TMPDIR"
python3 -B "$repo/tests/west_test_contracts/patch_stack_mutation_contract.py" \
    --root "$task_tmp/run" \
    --result "$task_tmp/run/mutation-result.json"

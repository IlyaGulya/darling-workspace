#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 tests/west_test_contracts/resource_provider_contract.py
printf 'PASS west-test-resource-provider-contract\n'

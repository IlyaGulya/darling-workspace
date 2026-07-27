#!/usr/bin/env bash
set -euo pipefail
exec mise exec -- python3 -B tests/west_test_contracts/perf_archive_forensic_contract.py

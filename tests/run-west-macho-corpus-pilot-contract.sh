#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 -B \
	"$repo/tests/west_test_contracts/macho_corpus_pilot_contract.py"

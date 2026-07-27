#!/bin/sh
set -eu

: "${OBJC4_MACRO_CONTRACT_CANDIDATE:?set the reviewed objc4 candidate path}"
exec python3 -B "$(dirname "$0")/west_test_contracts/objc4_macro_contract.py" \
  --candidate "$OBJC4_MACRO_CONTRACT_CANDIDATE"

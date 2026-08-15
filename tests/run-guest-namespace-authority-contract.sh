#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/west_test_contracts/guest_namespace_authority_contract.py"

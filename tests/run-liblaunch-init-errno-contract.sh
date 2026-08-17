#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${DARLING_SOURCE:?set DARLING_SOURCE to the candidate Darling source tree}"
exec python3 -B "$SCRIPT_DIR/west_test_contracts/liblaunch_init_errno_contract.py"


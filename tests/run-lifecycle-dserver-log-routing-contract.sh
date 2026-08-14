#!/usr/bin/env bash
set -euo pipefail

workspace=${1:?workspace root required}
darlingserver=${2:?Darlingserver source root required}

exec python3 "$workspace/tests/west_test_contracts/lifecycle_dserver_log_routing_contract.py" \
	--workspace "$workspace" \
	--darlingserver "$darlingserver"

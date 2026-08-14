#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
: "${DARLING_LIFECYCLE_DEPLOYED_LAUNCHER:?set exact deployed launcher}"
: "${DARLING_LIFECYCLE_DEPLOYED_PREFIX:?set task-owned deployed prefix}"
: "${DARLING_LIFECYCLE_DEPLOYED_EVIDENCE:?set a new task-owned evidence directory}"
: "${DARLING_LIFECYCLE_DSERVER_SHA256:?set exact deployed Darlingserver SHA-256}"
: "${DARLING_LIFECYCLE_WORKSPACE_ROOT:?set exact workspace source root}"
: "${DARLING_LIFECYCLE_DSERVER_SOURCE_ROOT:?set exact Darlingserver source root}"

exec /usr/bin/python3 -B \
	"$root/tests/west_test_contracts/lifecycle_dserver_log_deployed_contract.py" \
	--launcher "$DARLING_LIFECYCLE_DEPLOYED_LAUNCHER" \
	--prefix "$DARLING_LIFECYCLE_DEPLOYED_PREFIX" \
	--evidence-dir "$DARLING_LIFECYCLE_DEPLOYED_EVIDENCE" \
	--expected-darlingserver-sha256 "$DARLING_LIFECYCLE_DSERVER_SHA256" \
	--workspace-root "$DARLING_LIFECYCLE_WORKSPACE_ROOT" \
	--darlingserver-root "$DARLING_LIFECYCLE_DSERVER_SOURCE_ROOT"

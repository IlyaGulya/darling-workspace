#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
: "${DARLING_LIFECYCLE_DEPLOYED_LAUNCHER:?set exact deployed launcher}"
: "${DARLING_LIFECYCLE_DEPLOYED_PREFIX:?set task-owned deployed prefix}"
: "${DARLING_LIFECYCLE_DEPLOYED_EVIDENCE:?set a new task-owned evidence directory}"
: "${DARLING_LIFECYCLE_SOURCE_IDENTITY:?set exact source identity JSON}"
: "${DARLING_LIFECYCLE_SOURCE_IDENTITY_SCHEMA:="$ROOT/schemas/lifecycle-cohort-deployed-source-v1.schema.json"}"
: "${DARLING_LIFECYCLE_PER_USER_PROBE:?set exact compiled per-user probe}"

exec "${PYTHON:-/usr/bin/python3}" -B \
	"$ROOT/tests/west_test_contracts/lifecycle_cohort_deployed_contract.py" \
	--launcher "$DARLING_LIFECYCLE_DEPLOYED_LAUNCHER" \
	--prefix "$DARLING_LIFECYCLE_DEPLOYED_PREFIX" \
	--evidence-dir "$DARLING_LIFECYCLE_DEPLOYED_EVIDENCE" \
	--source-identity "$DARLING_LIFECYCLE_SOURCE_IDENTITY" \
	--source-identity-schema "$DARLING_LIFECYCLE_SOURCE_IDENTITY_SCHEMA" \
	--per-user-probe "$DARLING_LIFECYCLE_PER_USER_PROBE" \
	--workspace-root "$ROOT"

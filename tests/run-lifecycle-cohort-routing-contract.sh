#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DARLING_ROOT=${DARLING_LIFECYCLE_DARLING_ROOT:-"$ROOT/../darling"}
DARLINGSERVER_ROOT=${DARLING_LIFECYCLE_DARLINGSERVER_ROOT:-"$DARLING_ROOT/../darlingserver"}
TASK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dar-4ush-7-cohort.XXXXXX")
trap 'rm -rf -- "$TASK_ROOT"' EXIT

export CARGO_TARGET_DIR="$TASK_ROOT/cargo-target"
cargo build --quiet --locked --release --lib --bin darling-lifecycle-controller-worker \
	--manifest-path "$ROOT/lifecycle/operation-boundary/Cargo.toml"

clang -std=c11 -Wall -Wextra -Werror \
	-DDARLING_LIFECYCLE_COHORT_TESTING=1 \
	-I"$ROOT/lifecycle/operation-boundary/include" \
	-I"$DARLING_ROOT/src/lifecycle" \
	"$ROOT/tests/fixtures/lifecycle-cohort-v1/cohort_client_harness.c" \
	"$DARLING_ROOT/src/lifecycle/lifecycle_cohort_client.c" \
	"$CARGO_TARGET_DIR/release/libdarling_lifecycle_operation_boundary.a" \
	-lpthread -ldl -lm -lrt \
	-o "$TASK_ROOT/cohort-client-harness"

ln -s "$TASK_ROOT/cohort-client-harness" "$TASK_ROOT/launchd"
COHORT_HARNESS_LAUNCHD_PREFIX="$TASK_ROOT/prefix" \
	COHORT_HARNESS_WORKER="$CARGO_TARGET_DIR/release/darling-lifecycle-controller-worker" \
	bash -c 'exec -a /sbin/launchd "$1"' cohort-launchd "$TASK_ROOT/launchd"

"${PYTHON:-/usr/bin/python3}" "$ROOT/tests/west_test_contracts/lifecycle_cohort_routing_contract.py" \
	--workspace-root "$ROOT" \
	--darling-root "$DARLING_ROOT" \
	--darlingserver-root "$DARLINGSERVER_ROOT"

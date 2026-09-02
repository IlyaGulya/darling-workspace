#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TASK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dar-4ush-7-owner-abi.XXXXXX")
trap 'rm -rf -- "$TASK_ROOT"' EXIT
export CARGO_TARGET_DIR="$TASK_ROOT/cargo-target"

cargo build --quiet --locked --release --lib --bin darling-lifecycle-controller-worker \
	--manifest-path "$ROOT/lifecycle/operation-boundary/Cargo.toml"
c++ -std=c++17 -Wall -Wextra -Werror \
	-I"$ROOT/lifecycle/operation-boundary/include" \
	"$ROOT/tests/fixtures/lifecycle-cohort-v1/runtime_lower_binding_abi.cpp" \
	"$CARGO_TARGET_DIR/release/libdarling_lifecycle_operation_boundary.a" \
	-lpthread -ldl -lm -lrt -o "$TASK_ROOT/runtime-lower-binding-abi"
python3 -B "$ROOT/tests/west_test_contracts/runtime_lower_binding_abi_contract.py" \
	--fixture "$TASK_ROOT/runtime-lower-binding-abi" \
	--worker "$CARGO_TARGET_DIR/release/darling-lifecycle-controller-worker" \
	--task-root "$TASK_ROOT/runtime"

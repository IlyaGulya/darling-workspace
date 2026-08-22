#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
cargo_target="${CARGO_TARGET_DIR:-$repo/lifecycle/operation-boundary/target}"
env CARGO_NET_OFFLINE=true cargo test --manifest-path lifecycle/operation-boundary/Cargo.toml
env CARGO_NET_OFFLINE=true cargo build --manifest-path lifecycle/operation-boundary/Cargo.toml
DARLING_LIFECYCLE_BOUNDARY_BIN="$cargo_target/debug/lifecycle-boundary" \
    python3 -B tests/west_test_contracts/lifecycle_operation_boundary_contract.py

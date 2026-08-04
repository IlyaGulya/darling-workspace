#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
env CARGO_NET_OFFLINE=true cargo test --manifest-path lifecycle/operation-boundary/Cargo.toml
env CARGO_NET_OFFLINE=true cargo build --manifest-path lifecycle/operation-boundary/Cargo.toml
DARLING_LIFECYCLE_BOUNDARY_BIN="$repo/lifecycle/operation-boundary/target/debug/lifecycle-boundary" \
    python3 -B tests/west_test_contracts/lifecycle_operation_boundary_contract.py

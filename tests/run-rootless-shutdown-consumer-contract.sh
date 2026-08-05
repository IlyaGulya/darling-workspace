#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env CARGO_NET_OFFLINE=true cargo build --manifest-path "$repo/lifecycle/operation-boundary/Cargo.toml" \
    >/dev/null
DARLING_LIFECYCLE_BOUNDARY_BIN="$repo/lifecycle/operation-boundary/target/debug/lifecycle-boundary" \
    python3 -B "$repo/tests/west_test_contracts/rootless_shutdown_consumer_contract.py"

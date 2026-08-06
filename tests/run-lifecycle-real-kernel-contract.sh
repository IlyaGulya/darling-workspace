#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lane_root="$(mktemp -d "${TMPDIR:-/tmp}/darling-lifecycle-real-kernel.XXXXXX")"
cleanup() {
    rm -rf -- "$lane_root"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$lane_root"
export DARLING_LIFECYCLE_REAL_KERNEL_ROOT="$lane_root"

# Build the already accepted .4 trace producer inside the owned lane root;
# no repository target or generated source state is left behind.
export CARGO_TARGET_DIR="$lane_root/cargo-target"
env CARGO_NET_OFFLINE=true cargo build \
    --manifest-path "$repo/lifecycle/operation-boundary/Cargo.toml" \
    --bin lifecycle-fuzz >/dev/null

export DARLING_LIFECYCLE_FUZZ_BIN="$CARGO_TARGET_DIR/debug/lifecycle-fuzz"
python3 -B "$repo/tests/west_test_contracts/lifecycle_real_kernel_contract.py"

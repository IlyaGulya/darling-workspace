#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
contract_tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/darling-lifecycle-fuzz-contract.XXXXXX")"
cleanup_contract_tmp() {
    rm -rf -- "$contract_tmp_root"
}
trap cleanup_contract_tmp EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$contract_tmp_root"
export DARLING_LIFECYCLE_FUZZ_TMPDIR="$contract_tmp_root"
cargo_target="${CARGO_TARGET_DIR:-$repo/lifecycle/operation-boundary/target}"
env CARGO_NET_OFFLINE=true cargo build --manifest-path lifecycle/operation-boundary/Cargo.toml --bin lifecycle-fuzz
DARLING_LIFECYCLE_FUZZ_BIN="$cargo_target/debug/lifecycle-fuzz" \
    python3 -B tests/west_test_contracts/lifecycle_fuzz_contract.py

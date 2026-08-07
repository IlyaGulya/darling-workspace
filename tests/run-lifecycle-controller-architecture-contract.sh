#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
env CARGO_NET_OFFLINE=true cargo fmt --manifest-path lifecycle/operation-boundary/Cargo.toml -- --check
env CARGO_NET_OFFLINE=true cargo test --manifest-path lifecycle/operation-boundary/Cargo.toml controller::tests
python3 -B tests/west_test_contracts/lifecycle_controller_architecture_contract.py
git diff --check -- ':!patches/**/*.patch'

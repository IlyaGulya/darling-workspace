#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
export CARGO_NET_OFFLINE=true
task_tmp="$(mktemp -d "${TMPDIR:-/tmp}/darling-quarantine-gc-contract.XXXXXX")"
trap 'rm -rf -- "$task_tmp"' EXIT
export TMPDIR="$task_tmp"

cargo fmt --manifest-path lifecycle/operation-boundary/Cargo.toml -- --check
cargo test --manifest-path lifecycle/operation-boundary/Cargo.toml \
  --lib quarantine_gc::tests -- --test-threads=1
git diff --check
printf '%s\n' 'LIFECYCLE_QUARANTINE_GC_CONTRACT=VALID'

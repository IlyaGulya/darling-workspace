#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

# Rust's sanitizer list deliberately has no `undefined` mode. The accepted
# bounded policy is ASan + TSan for the complete fuzz target and Miri for the
# filesystem-free decoder/reducer semantic core. This script is the Miri leg;
# it intentionally never runs explore_one under Miri.
if ! rustup component list --toolchain nightly | grep -q '^miri-.*(installed)$'; then
    printf '%s\n' 'UB_GATE_STATUS=BLOCKED_MIRI_UNAVAILABLE'
    printf '%s\n' 'UB_POLICY=ASAN_TSAN_MIRI_SEMANTIC_CORE'
    exit 2
fi

timeout 180s env CARGO_NET_OFFLINE=true MIRIFLAGS=-Zmiri-disable-isolation RUSTUP_TOOLCHAIN=nightly \
    cargo miri test --manifest-path lifecycle/operation-boundary/Cargo.toml \
    --lib fuzz::tests::miri_semantic_core -- --nocapture

printf '%s\n' 'MIRI_DECODER_REDUCER_CORPUS=PASS'
printf '%s\n' 'MIRI_HISTORICAL_BAD_ARMS=8/8'
printf '%s\n' 'MIRI_MINIMIZER=PASS'
printf '%s\n' 'MIRI_DETERMINISTIC_MUTATIONS=PASS'
printf '%s\n' 'UB_POLICY=ASAN_TSAN_MIRI_SEMANTIC_CORE'
printf '%s\n' 'UBSAN_STATUS=UNSUPPORTED_NO_UNDEFINED_SANITIZER'
printf '%s\n' 'UB_GATE_STATUS=PASS'

#!/usr/bin/env bash
# The vendored diagnostics tool's own tests: the instrument-registry census (a registered instrument whose
# emitter changed format must fail here, not silently empty a report) and the deploy-receipt verdict merge.
#
# WHY A CONTRACT: these tests existed and NOTHING ran them, so one of them was red (fourteen registered
# instruments had no sample line, and the census could not notice) -- a contract that nothing runs cannot fail.
set -euo pipefail

cd "$(dirname "$0")/.."
exec env CARGO_NET_OFFLINE=true cargo test --offline --locked \
  --manifest-path tools/darling-debug-runner/Cargo.toml

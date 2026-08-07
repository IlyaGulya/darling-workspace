#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
manifest="$repo_root/lifecycle/operation-boundary/Cargo.toml"

cargo test --manifest-path "$manifest" 'linux_backend::tests::' -- --nocapture
printf 'ROOTLESS_LINUX_BACKEND_VALID backend=rust fixture=task-owned cgroup=BOUND census=FIXED_POINT pidfd=PASS cleanup=QUARANTINE_RENAMEAT2\n'

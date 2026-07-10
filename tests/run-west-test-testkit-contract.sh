#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

bead="dar-dar6x4-perf-5dq.1"

list_by_bead="$(west test --bead "$bead" --list)"
printf '%s\n' "$list_by_bead" | grep -q 'host/mldr_thread_create_checkin_wait' ||
	{ printf '%s\n' "$list_by_bead" >&2; exit 1; }
printf '%s\n' "$list_by_bead" | grep -q 'host/mldr_thread_create_checkin_wait_spin_red' ||
	{ printf '%s\n' "$list_by_bead" >&2; exit 1; }

west test --bead "$bead"

list_by_submodule="$(west test --submodule darling --list)"
printf '%s\n' "$list_by_submodule" | grep -q 'host/mldr_thread_create_checkin_wait' ||
	{ printf '%s\n' "$list_by_submodule" >&2; exit 1; }
printf '%s\n' "$list_by_submodule" | grep -q 'host/mldr_thread_create_checkin_wait_spin_red' ||
	{ printf '%s\n' "$list_by_submodule" >&2; exit 1; }

printf 'PASS west-test-testkit-contract\n'

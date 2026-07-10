#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

export PYTHONDONTWRITEBYTECODE=1

tests/run-darling-c-test-contract.sh

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

list_guest="$(west test --bead dar-cps --env darling --list)"
printf '%s\n' "$list_guest" | grep -q 'darling/abort_with_payload_no_group_broadcast' ||
	{ printf '%s\n' "$list_guest" >&2; exit 1; }
dar_cps_json="$(ctest --test-dir testkit/build --show-only=json-v1 \
	-L 'bead:dar-cps' -L 'env:darling')"
printf '%s\n' "$dar_cps_json" | grep -q 'runtime-profile:homebrew' ||
	{ printf '%s\n' "$dar_cps_json" >&2; exit 1; }

list_select_guest="$(west test --bead dar-q95.3 --env darling --list)"
printf '%s\n' "$list_select_guest" | grep -q 'darling/select_fdset_guest' ||
	{ printf '%s\n' "$list_select_guest" >&2; exit 1; }

list_bzero_guest="$(west test --bead dar-q95.4 --env darling --list)"
printf '%s\n' "$list_bzero_guest" | grep -q 'darling/bzero_return_register_guest' ||
	{ printf '%s\n' "$list_bzero_guest" >&2; exit 1; }
bzero_json="$(ctest --test-dir testkit/build --show-only=json-v1 \
	-L 'bead:dar-q95.4' -L 'env:darling')"
printf '%s\n' "$bzero_json" | grep -q 'runtime-profile:homebrew-libplatform' ||
	{ printf '%s\n' "$bzero_json" >&2; exit 1; }

for bead in dar-q95.10 dar-q95.11 dar-q95.20 dar-gwn.6.4 dar-gwn.6 dar-gwn.1.6 dar-gyvb dar-6x4.1 dar-gwn.6.5; do
	guest_list="$(west test --bead "$bead" --env darling --list)"
	printf '%s\n' "$guest_list" | grep -q 'darling/' ||
		{ printf '%s\n' "$guest_list" >&2; exit 1; }
done

list_eunion="$(west test --bead dar-test-infra-sp5.8.4.4 --env host --list)"
printf '%s\n' "$list_eunion" | grep -q 'host/eunion_host_suite' ||
	{ printf '%s\n' "$list_eunion" >&2; exit 1; }
west test --bead dar-test-infra-sp5.8.4.4 --env host

printf 'PASS west-test-testkit-contract\n'

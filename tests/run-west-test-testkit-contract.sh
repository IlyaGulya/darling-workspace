#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

scratch="${DW_STATE_ROOT:-${repo%/*}/.west-state}/test-scratch"
mkdir -p "$scratch"
scope_build="$(mktemp -d "$scratch/testkit-contract.XXXXXX")"
finish() {
	local status="$1" command="$2"
	if (( status == 0 )); then
		rm -rf "$scope_build"
	else
		printf 'FAIL west-test-testkit-contract: rc=%s command=%s\nartifacts: %s\n' \
			"$status" "$command" "$scope_build" >&2
	fi
}
trap 'finish "$?" "$BASH_COMMAND"' EXIT

export PYTHONDONTWRITEBYTECODE=1

tests/run-darling-c-test-contract.sh
tests/run-west-guest-toolchain-contract.sh
tests/run-west-guest-toolchain-cache-contract.sh
tests/run-west-guest-toolchain-ccache-contract.sh
python3 tests/west_test_contracts/guest_macho_validation_contract.py
tests/run-west-guest-macho-contract.sh
tests/run-west-macho-corpus-batch-contract.sh

# Exercise West's real extension loader.  It does not import command modules
# like ordinary Python packages, so this catches loader-incompatible module
# declarations before any CTest discovery runs.
west test --help | grep -q -- '--runtime-root ROOT'
west test --help | grep -q -- '--source-root SOURCE'
west test --help | grep -q -- '--workload NAME'
west test --help | grep -q -- '--guest-macho-validation-group {homebrew}'
west test --help | grep -q -- '--guest-macho-evidence-dir DIR'

# The explicit source-bound host harness remains opt-in; ordinary guest
# selection consumes the installed image without enabling unrelated host targets.
cmake -S testkit -B "$scope_build" -G Ninja \
	-DDARLING_ENABLE_EUNION_HOST_SUITE=OFF >/dev/null
if ctest --test-dir "$scope_build" --show-only=json-v1 2>/dev/null |
	grep -F -q 'host/eunion_hardening_host_suite'; then
	printf 'E-UNION host suite leaked into the default guest build\n' >&2
	exit 1
fi


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

list_select_guest="$(west test --bead dar-q95.3 --env darling --list)"
printf '%s\n' "$list_select_guest" | grep -q 'darling/select_fdset_guest' ||
	{ printf '%s\n' "$list_select_guest" >&2; exit 1; }
list_select_prebuilt="$(west test --workload select_fdset_guest_prebuilt --env darling --list)"
printf '%s\n' "$list_select_prebuilt" | grep -q 'select_fdset_guest_prebuilt' ||
	{ printf '%s\n' "$list_select_prebuilt" >&2; exit 1; }
list_macho_homebrew="$(west test --env darling --guest-macho-validation-group homebrew --list)"
[ "$(printf '%s\n' "$list_macho_homebrew" | grep -F -c '_prebuilt')" -eq 13 ] ||
	{ printf '%s\n' "$list_macho_homebrew" >&2; exit 1; }
if west test --env darling --guest-macho-validation-group invalid --list >/dev/null 2>&1; then
	echo 'guest Mach-O selector accepted an invalid group' >&2
	exit 1
fi
list_getattrlist_guest="$(west test --bead dar-e1j --env darling --list)"
printf '%s\n' "$list_getattrlist_guest" | grep -q 'darling/getattrlist_name_objtype_guest' ||
	{ printf '%s\n' "$list_getattrlist_guest" >&2; exit 1; }

list_bzero_guest="$(west test --bead dar-q95.4 --env darling --list)"
printf '%s\n' "$list_bzero_guest" | grep -q 'darling/bzero_return_register_guest' ||
	{ printf '%s\n' "$list_bzero_guest" >&2; exit 1; }

list_sigignore_guest="$(west test --bead dar-cpuk --env darling --list)"
printf '%s\n' "$list_sigignore_guest" | grep -q 'darling/sigaction_ignore_pending' ||
	{ printf '%s\n' "$list_sigignore_guest" >&2; exit 1; }

list_handler_mask_guest="$(west test --bead dar-1c53 --env darling --list)"
printf '%s\n' "$list_handler_mask_guest" | grep -q 'darling/signal_handler_mask' ||
	{ printf '%s\n' "$list_handler_mask_guest" >&2; exit 1; }

for bead in dar-q95.10 dar-q95.11 dar-q95.20 dar-gwn.6.4 dar-gwn.6 dar-gwn.1.6 dar-nmda dar-6x4.1 dar-gwn.6.5; do
	guest_list="$(west test --bead "$bead" --env darling --list)"
	printf '%s\n' "$guest_list" | grep -q 'darling/' ||
		{ printf '%s\n' "$guest_list" >&2; exit 1; }
done

# The host suite compiles the explicitly selected current XNU checkout and
# keeps its real CTest registration and behavioral oracle.
eunion_metadata="$(west test --workload eunion_hardening_host_suite --env host --list)"
printf '%s\n' "$eunion_metadata" | grep -q 'host/eunion_hardening_host_suite' ||
	{ printf '%s\n' "$eunion_metadata" >&2; exit 1; }

printf 'PASS west-test-testkit-contract\n'

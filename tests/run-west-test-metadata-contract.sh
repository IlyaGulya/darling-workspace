#!/usr/bin/env bash
set -euo pipefail

display_has_debug_runner_run() {
	python3 tests/west_test_contracts/metadata_display_contract.py --match guarded
}

display_has_guarded_ctest() {
	local label=$1
	python3 tests/west_test_contracts/metadata_display_contract.py \
		--match guarded-ctest \
		--label "$label"
}

if [ "${1:-}" = "--metadata-display-contract-probe" ]; then
	case "${2:-}" in
	guarded)
		display_has_debug_runner_run
		;;
	bare)
		if display_has_debug_runner_run; then
			exit 1
		fi
		;;
	guarded-ctest)
		[ "$#" -eq 3 ] || exit 2
		display_has_guarded_ctest "$3"
		;;
	*)
		printf 'unknown metadata display contract probe: %s\n' "${2:-}" >&2
		exit 2
		;;
	esac
	exit 0
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
"$repo/scripts/west-job.sh" assert-no-live-west-test --state-root "${TMPDIR:-/tmp}"

if [ "${1:-}" = "--source-contract-probe" ]; then
	[ -n "${DARLING_SRC_ROOT:-}" ] || {
		echo "DARLING_SRC_ROOT is not set" >&2
		exit 1
	}
	[ -d "$DARLING_SRC_ROOT" ] || {
		echo "DARLING_SRC_ROOT is not a directory: $DARLING_SRC_ROOT" >&2
		exit 1
	}
	printf 'WEST_SOURCE_CONTRACT_SCRIPT_OK\n'
	exit 0
fi

if [ "${1:-}" = "--source-script-shebang-probe" ]; then
	set -o pipefail
	printf 'WEST_SOURCE_SCRIPT_SHEBANG_OK\n'
	exit 0
fi

if [ "${1:-}" = "--self-contract-probe" ]; then
	printf 'WEST_SELF_CONTRACT_SCRIPT_OK\n'
	exit 0
fi

export PYTHONDONTWRITEBYTECODE=1

if [[ "${CODEX_CI:-}" == "1" && "${WEST_JOB_ACTIVE:-}" != "1" ]]; then
	echo 'metadata contract requires scripts/west-job.sh in CODEX_CI' >&2
	exit 2
fi

if [ "${1:-}" = "--transport-gate-probe" ]; then
	printf 'WEST_METADATA_TRANSPORT_GATE_OK\n'
	exit 0
fi

# CTest and the structured runners remain the execution authority. These focused
# contracts exercise current-source workloads without constructing source profiles.
python3 -B tests/west_test_contracts/manifest_contract.py
python3 -B tests/west_test_contracts/selection_contract.py
python3 -B tests/west_test_contracts/metadata_display_contract.py
python3 -B tests/west_test_contracts/metadata_invocation_identity_contract.py
python3 -B tests/west_test_contracts/metadata_run_report_contract.py
python3 -B tests/west_test_contracts/metadata_workload_execution_contract.py
python3 -B tests/west_test_contracts/dispatch_contract.py
python3 -B tests/west_test_contracts/execution_contract.py
python3 -B tests/west_test_contracts/ctest_backend_contract.py
python3 -B tests/west_test_contracts/ctest_selection_argv_contract.py
python3 -B tests/west_test_contracts/ctest_lifecycle_contract.py
python3 -B tests/west_test_contracts/cmake_fixture_backend_contract.py
python3 -B tests/west_test_contracts/host_trace_failure_phase_contract.py
python3 -B tests/west_test_contracts/guest_trace_failure_phase_contract.py
python3 -B tests/west_test_contracts/runtime_evidence_contract.py
python3 -B tests/west_test_contracts/eunion_boot_contract.py
python3 -B tests/west_test_contracts/eunion_prereq_contract.py
printf 'PASS west-test-metadata-contract\n'

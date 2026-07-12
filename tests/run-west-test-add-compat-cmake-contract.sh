#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cat >"$tmp/CMakeLists.txt" <<CMAKE
cmake_minimum_required(VERSION 3.13)
project(add-compat-contract C)
include(CTest)
include("${repo}/testkit/cmake/AddCompatTest.cmake")
file(WRITE "\${CMAKE_CURRENT_BINARY_DIR}/guest.c" "#include <stdio.h>\\nint main(void) { puts(\"GUEST; ARG CONTRACT OK\"); return 0; }\\n")
file(WRITE "\${CMAKE_CURRENT_BINARY_DIR}/red.c" "#include <stdio.h>\\nint main(void) { fputs(\"EXPECTED RED; SYMPTOM\\\\n\", stderr); return 7; }\\n")
add_compat_test(
  NAME guest_arg_contract
  SOURCE "\${CMAKE_CURRENT_BINARY_DIR}/guest.c"
  ENVS darling
  BEAD dar-contract
  SUBMODULES darling
  FUZZ
  STRESS
  TIMEOUT 17
  OK_MARKER "GUEST; ARG CONTRACT OK"
  ARGS hello
)
add_compat_test(
  NAME guest_runtime_override_contract
  SOURCE "\${CMAKE_CURRENT_BINARY_DIR}/guest.c"
  ENVS darling
  RUNTIME_PROFILE specialist-runtime
)
add_compat_test(
  NAME macos_contract
  SOURCE "\${CMAKE_CURRENT_BINARY_DIR}/guest.c"
  ENVS macos
  BEAD dar-macos-contract
  DIAG bare
)
add_compat_test(
  NAME expected_failure_contract
  SOURCE "\${CMAKE_CURRENT_BINARY_DIR}/red.c"
  ENVS host
  DIAG bare
  EXPECT_FAILURE_MARKER "EXPECTED RED; SYMPTOM"
)
CMAKE

cmake -S "$tmp" -B "$tmp/build-shell" -G Ninja \
  -DDARLING_TEST_PREFIX=/tmp/darling-prefix-contract \
  -DDARLING_TEST_NO_OVERLAYFS=ON >/dev/null

ctest_file="$tmp/build-shell/CTestTestfile.cmake"
grep -q 'run-darling-c-test.sh.*guest_arg_contract.*guest.c.*hello' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
if grep -q -- '--launcher' "$ctest_file"; then
	cat "$ctest_file" >&2
	exit 1
fi
grep -q -- '--ok-marker-file.*guest_arg_contract.ok' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
test "$(cat "$tmp/build-shell/west-test-markers/guest_arg_contract.ok")" = 'GUEST; ARG CONTRACT OK' ||
	{ cat "$tmp/build-shell/west-test-markers/guest_arg_contract.ok" >&2; exit 1; }
grep -q 'DPREFIX=/tmp/darling-prefix-contract' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'DARLING_PREFIX=/tmp/darling-prefix-contract' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'DARLING_GUEST_TIMEOUT_SECONDS=17' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'DARLING_NOOVERLAYFS=1' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'fuzz:true' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'stress:true' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'runtime-profile:homebrew' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'runtime-profile:specialist-runtime' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'TIMEOUT "27"' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }

cmake -S "$tmp" -B "$tmp/build-guarded" -G Ninja \
	-DDARLING_TEST_PREFIX=/tmp/darling-prefix-contract \
	-DDARLING_TEST_EXECUTOR=/bin/echo >/dev/null

guarded_ctest_file="$tmp/build-guarded/CTestTestfile.cmake"
grep -q 'darling/guest_arg_contract.*"--timeout-seconds" "17"' "$guarded_ctest_file" ||
	{ cat "$guarded_ctest_file" >&2; exit 1; }
grep -q 'DARLING_GUEST_TIMEOUT_SECONDS=27' "$guarded_ctest_file" ||
	{ cat "$guarded_ctest_file" >&2; exit 1; }
grep -q 'capture-darling-prefix-timeout.sh' "$guarded_ctest_file" ||
	{ cat "$guarded_ctest_file" >&2; exit 1; }
grep -q 'TIMEOUT "27"' "$guarded_ctest_file" ||
	{ cat "$guarded_ctest_file" >&2; exit 1; }

cmake --build "$tmp/build-shell" >/dev/null
ctest --test-dir "$tmp/build-shell" -V \
	-R '^host/expected_failure_contract$' >"$tmp/red.out" 2>&1 ||
	{ cat "$tmp/red.out" >&2; exit 1; }
grep -q 'WEST_TEST_RED_OK: EXPECTED RED; SYMPTOM' "$tmp/red.out" ||
	{ cat "$tmp/red.out" >&2; exit 1; }

cmake -S "$tmp" -B "$tmp/build-missing" -G Ninja >/dev/null
cmake --build "$tmp/build-missing" >/dev/null
if ctest --test-dir "$tmp/build-missing" --output-on-failure -L bead:dar-contract \
	>"$tmp/missing.out" 2>&1; then
	cat "$tmp/missing.out" >&2
	exit 1
fi
grep -q 'DARLING_LAUNCHER is unset' "$tmp/missing.out" ||
	{ cat "$tmp/missing.out" >&2; exit 1; }

if ctest --test-dir "$tmp/build-shell" --output-on-failure -R '^macos/macos_contract$' \
	>"$tmp/macos.out" 2>&1; then
	cat "$tmp/macos.out" >&2
	exit 1
fi
grep -q 'needs a real macOS runner' "$tmp/macos.out" ||
	{ cat "$tmp/macos.out" >&2; exit 1; }

printf 'PASS west-test-add-compat-cmake-contract\n'

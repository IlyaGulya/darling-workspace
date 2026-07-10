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
file(WRITE "\${CMAKE_CURRENT_BINARY_DIR}/guest.c" "int main(void) { return 0; }\\n")
add_compat_test(
  NAME guest_arg_contract
  SOURCE "\${CMAKE_CURRENT_BINARY_DIR}/guest.c"
  ENVS darling
  BEAD dar-contract
  SUBMODULES darling
  DIAG bare
  ARGS hello
)
CMAKE

cmake -S "$tmp" -B "$tmp/build-shell" -G Ninja \
  '-DDARLING_SHELL=/bin/echo;darling-shell' \
  -DDARLING_TEST_PREFIX=/tmp/darling-prefix-contract >/dev/null

ctest_file="$tmp/build-shell/CTestTestfile.cmake"
grep -q '/bin/echo.*darling-shell.*guest_arg_contract.*hello' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'DPREFIX=/tmp/darling-prefix-contract' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }
grep -q 'DARLING_PREFIX=/tmp/darling-prefix-contract' "$ctest_file" ||
	{ cat "$ctest_file" >&2; exit 1; }

cmake -S "$tmp" -B "$tmp/build-missing" -G Ninja >/dev/null
cmake --build "$tmp/build-missing" >/dev/null
if ctest --test-dir "$tmp/build-missing" --output-on-failure -L bead:dar-contract \
	>"$tmp/missing.out" 2>&1; then
	cat "$tmp/missing.out" >&2
	exit 1
fi
grep -q 'DARLING_SHELL is unset' "$tmp/missing.out" ||
	{ cat "$tmp/missing.out" >&2; exit 1; }

printf 'PASS west-test-add-compat-cmake-contract\n'

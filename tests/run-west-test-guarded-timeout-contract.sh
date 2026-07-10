#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runner="${repo}/../darling-debug-runner/target/release/darling-debug-runner"
if [[ ! -x "$runner" ]]; then
	echo "SKIP west-test-guarded-timeout-contract: missing $runner"
	exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
bundle_root="$tmp/bundles"

cat >"$tmp/CMakeLists.txt" <<CMAKE
cmake_minimum_required(VERSION 3.13)
project(guarded-timeout-contract C)
include(CTest)
include("${repo}/testkit/cmake/AddCompatTest.cmake")
file(WRITE "\${CMAKE_CURRENT_BINARY_DIR}/hang.c"
"#include <unistd.h>\\nint main(void) { sleep(30); return 0; }\\n")
add_compat_test(
  NAME guarded_timeout_contract
  SOURCE "\${CMAKE_CURRENT_BINARY_DIR}/hang.c"
  ENVS host
  BEAD dar-contract
  DIAG guarded
  TIMEOUT 1
)
CMAKE

cmake -S "$tmp" -B "$tmp/build" -G Ninja \
	-DDARLING_TEST_EXECUTOR="$runner" \
	-DDARLING_TEST_BUNDLE_ROOT="$bundle_root" >/dev/null
cmake --build "$tmp/build" >/dev/null

if ctest --test-dir "$tmp/build" --output-on-failure -L bead:dar-contract \
	>"$tmp/ctest.out" 2>&1; then
	cat "$tmp/ctest.out" >&2
	exit 1
fi

bundle="$(find "$bundle_root" -mindepth 1 -maxdepth 1 -type d | head -1)"
if [[ -z "$bundle" ]]; then
	cat "$tmp/ctest.out" >&2
	echo "no debug bundle created under $bundle_root" >&2
	exit 1
fi

for artifact in command.txt pid.txt stdout.log stderr.log env.txt; do
	test -f "$bundle/$artifact" || { find "$bundle" -maxdepth 1 -type f -print >&2; exit 1; }
done

grep -q 'guarded_timeout_contract' "$bundle/command.txt" ||
	{ cat "$bundle/command.txt" >&2; exit 1; }
grep -q 'Timeout' "$tmp/ctest.out" ||
	{ cat "$tmp/ctest.out" >&2; exit 1; }

pid="$(cat "$bundle/pid.txt")"
if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
	cat "$tmp/ctest.out" >&2
	echo "timed-out debug-runner process is still alive: $pid" >&2
	exit 1
fi

printf 'PASS west-test-guarded-timeout-contract\n'

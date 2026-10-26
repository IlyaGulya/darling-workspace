#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
darling_root="${DARLING_SOURCE_ROOT:-$workspace_root/../darling}"
server_root="${DSERVER_SRC_ROOT:-$darling_root/src/external/darlingserver}"
work="$(mktemp -d "${TMPDIR:-/tmp}/dserver-runtime-mode-contract.XXXXXX")"

cleanup() {
	rm -rf -- "$work"
}
trap cleanup EXIT

c++ -std=c++17 -Wall -Wextra -Werror \
	-I"$server_root/include" \
	"$server_root/tests/runtime_mode_test.cpp" \
	"$server_root/src/runtime-mode.cpp" \
	-o "$work/runtime-mode-test"

test "$("$work/runtime-mode-test")" = "DSERVER_RUNTIME_MODE_CONTRACT_OK"


printf 'PASS rootless-runtime-server\n'

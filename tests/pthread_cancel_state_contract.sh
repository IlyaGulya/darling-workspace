#!/usr/bin/env bash
set -euo pipefail

src="${DSERVER_SRC_ROOT:?set DSERVER_SRC_ROOT}"
test -f "$src/duct-tape/internal-include/darlingserver/duct-tape/thread-cancel.h"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cc -std=c11 -Wall -Wextra -Werror \
	-I "$src/duct-tape/internal-include" \
	"$PWD/tests/pthread_cancel_state_contract.c" \
	-o "$tmp/pthread_cancel_state_contract"
"$tmp/pthread_cancel_state_contract"

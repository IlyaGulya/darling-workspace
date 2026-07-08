#!/usr/bin/env bash
set -euo pipefail

src="${DARLING_SRC_ROOT:?set DARLING_SRC_ROOT}"
test -f "$src/src/startup/mldr/stack_mapping.h"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cc -std=gnu11 -Wall -Wextra -Werror \
	-I "$src" \
	"$PWD/tests/mldr_stack_mapping_contract.c" \
	-o "$tmp/mldr_stack_mapping_contract"
"$tmp/mldr_stack_mapping_contract"

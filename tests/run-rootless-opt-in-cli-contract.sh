#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
darling_root="${DARLING_SOURCE_ROOT:-${DARLING_SRC_ROOT:-$workspace_root/../darling}}"
startup="$darling_root/src/startup"
work="$(mktemp -d "${TMPDIR:-/tmp}/darling-runtime-mode-contract.XXXXXX")"

cleanup() {
	rm -rf -- "$work"
}
trap cleanup EXIT

for source in \
	"$startup/runtime_mode.c" \
	"$startup/runtime_mode.h" \
	"$startup/runtime_credentials.c" \
	"$startup/runtime_credentials.h" \
	"$startup/runtime_mode_prefix.c" \
	"$startup/runtime_mode_prefix.h" \
	"$startup/tests/runtime_mode_test.c"
do
	test -f "$source"
done

cc -std=gnu11 -Wall -Wextra -Werror \
	-DDARLING_RUNTIME_PREFIX_LIFECYCLE_TESTING=1 \
	-I"$startup" \
	"$startup/runtime_credentials.c" \
	"$startup/runtime_mode.c" \
	"$startup/runtime_mode_prefix.c" \
	"$startup/tests/runtime_mode_test.c" \
	-o "$work/runtime-mode-test"

test "$("$work/runtime-mode-test")" = "DARLING_RUNTIME_MODE_CONTRACT_OK"
for focused_case in lock-race durability capability interruptions namespace
do
	DARLING_RUNTIME_PREFIX_TEST_CASE="$focused_case" \
		"$work/runtime-mode-test"
done

cat >"$work/capability-transfer-good.c" <<'EOF'
#include "runtime_mode_prefix.h"

int main(void)
{
	darling_runtime_prefix source = DARLING_RUNTIME_PREFIX_INITIALIZER;
	darling_runtime_prefix destination = DARLING_RUNTIME_PREFIX_INITIALIZER;
	char error[64] = {0};
	return darling_runtime_prefix_move(destination, source,
		error, sizeof(error));
}
EOF
cc -std=gnu11 -Wall -Wextra -Werror -c -I"$startup" \
	"$work/capability-transfer-good.c" -o "$work/capability-transfer-good.o"

cat >"$work/capability-direct-copy-bad.c" <<'EOF'
#include "runtime_mode_prefix.h"

int main(void)
{
	darling_runtime_prefix source = DARLING_RUNTIME_PREFIX_INITIALIZER;
	darling_runtime_prefix destination = source;
	(void)destination;
	return 0;
}
EOF
if cc -std=gnu11 -Wall -Wextra -Werror -c -I"$startup" \
	"$work/capability-direct-copy-bad.c" -o "$work/capability-direct-copy-bad.o" \
	>"$work/capability-direct-copy-bad.stdout" \
	2>"$work/capability-direct-copy-bad.stderr"
then
	printf 'assignment-resistant prefix capability accepted direct copy-initialization\n' >&2
	exit 1
fi


printf 'PASS rootless-opt-in-cli\n'

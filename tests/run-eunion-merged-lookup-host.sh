#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
XNU_ROOT=${XNU_SRC_ROOT:-"$ROOT/../darling/src/external/xnu"}
TASK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dar-awrp-eunion-host.XXXXXX")
trap 'rm -rf -- "$TASK_ROOT"' EXIT

mkdir -p "$TASK_ROOT/include/darling/emulation/linux_premigration"
ln -s "$XNU_ROOT/darling/src/libsystem_kernel/emulation/include/linux_premigration/vchroot_expand.h" \
	"$TASK_ROOT/include/darling/emulation/linux_premigration/vchroot_expand.h"

clang -std=c11 -Wall -Wextra \
	-Wno-unused-function -Wno-unused-variable -Wno-sign-compare \
	-I"$TASK_ROOT/include" \
	-I"$XNU_ROOT/darling/src/libsystem_kernel/emulation/src/linux_premigration" \
	"$ROOT/tests/eunion_merged_lookup_host.c" \
	-o "$TASK_ROOT/eunion-merged-lookup-host"

mkdir "$TASK_ROOT/root"
"$TASK_ROOT/eunion-merged-lookup-host" "$TASK_ROOT/root"

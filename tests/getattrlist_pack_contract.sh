#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
test -f "$emu/include/xnu_syscall/bsd/helper/xattr/getattrlist_pack.h"
test -f "$emu/include/conversion/xattr/getattrlist.h"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/conversion/xattr" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/helper/xattr"

cat > "$tmp/include/darling/emulation/conversion/xattr/getattrlist.h" <<H_EOF
#pragma once
#include "$emu/include/conversion/xattr/getattrlist.h"
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/helper/xattr/getattrlist_pack.h" <<H_EOF
#pragma once
#include "$emu/include/xnu_syscall/bsd/helper/xattr/getattrlist_pack.h"
H_EOF

cc -std=gnu11 -Wall -Wextra -Werror \
	-I "$tmp/include" \
	"$PWD/tests/getattrlist_pack_contract.c" \
	-o "$tmp/getattrlist_pack_contract"
"$tmp/getattrlist_pack_contract"

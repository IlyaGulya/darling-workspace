#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
impl="$emu/src/linux_premigration/signal/sigexc.c"
test -f "$impl"
. "$PWD/tests/sigexc_compile_env.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

prepare_sigexc_compile_env "$tmp" "$emu"

cc $(sigexc_contract_cflags) \
	-I "$tmp/include" \
	-c "$impl" \
	-o "$tmp/sigexc-default.o"
if nm -u "$tmp/sigexc-default.o" | grep -q ' __simple_kprintf$'; then
	echo "default sigexc build still references __simple_kprintf"
	exit 1
fi

cc $(sigexc_contract_cflags) \
	-DDEBUG_SIGEXC \
	-I "$tmp/include" \
	-c "$impl" \
	-o "$tmp/sigexc-debug.o"
if ! nm -u "$tmp/sigexc-debug.o" | grep -q ' __simple_kprintf$'; then
	echo "DEBUG_SIGEXC build no longer references __simple_kprintf"
	exit 1
fi

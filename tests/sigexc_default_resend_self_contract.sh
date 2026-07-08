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
	"$PWD/tests/sigexc_default_resend_self_contract.c" \
	"$impl" \
	-o "$tmp/sigexc_default_resend_self_contract"
"$tmp/sigexc_default_resend_self_contract"

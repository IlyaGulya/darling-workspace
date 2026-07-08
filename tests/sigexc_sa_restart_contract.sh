#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
sigaction_impl="$emu/src/xnu_syscall/bsd/impl/signal/sigaction.c"
sigexc_impl="$emu/src/linux_premigration/signal/sigexc.c"
test -f "$sigaction_impl"
test -f "$sigexc_impl"
. "$PWD/tests/sigexc_compile_env.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

prepare_sigexc_compile_env "$tmp" "$emu"

cc $(sigexc_contract_cflags) \
	-I "$tmp/include" \
	"$PWD/tests/sigexc_sa_restart_contract.c" \
	"$sigaction_impl" \
	"$sigexc_impl" \
	-o "$tmp/sigexc_sa_restart_contract"
"$tmp/sigexc_sa_restart_contract"

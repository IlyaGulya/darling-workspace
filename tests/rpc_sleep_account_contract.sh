#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
hdr="$emu/include/linux_premigration/resources/rpc-sleep-account.h"
impl="$emu/src/linux_premigration/resources/rpc-sleep-account.c"
test -f "$hdr"
test -f "$impl"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/linux_premigration/resources"

cat > "$tmp/include/darling/emulation/common/base.h" <<'H_EOF'
#pragma once
#ifdef __cplusplus
#define CPP_EXTERN_BEGIN extern "C" {
#define CPP_EXTERN_END }
#else
#define CPP_EXTERN_BEGIN
#define CPP_EXTERN_END
#endif
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
#pragma once
#define __NR_openat 257
#define __NR_getpid 39
#define __NR_write 1
#define __NR_close 3
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL0(n) test_linux_syscall((n))
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), ##__VA_ARGS__)
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/resources/rpc-sleep-account.h" <<H_EOF
#pragma once
#include "$hdr"
H_EOF

cc -std=gnu11 -Wall -Wextra -Werror \
	-DDARLING_RPC_SLEEP_ACCOUNT \
	-I "$tmp/include" \
	-c "$impl" \
	-o "$tmp/rpc-sleep-account.o"

cc -std=gnu11 -Wall -Wextra -Werror \
	-DDARLING_RPC_SLEEP_ACCOUNT \
	-I "$tmp/include" \
	"$PWD/tests/rpc_sleep_account_contract.c" \
	"$tmp/rpc-sleep-account.o" \
	-o "$tmp/rpc_sleep_account_contract"
"$tmp/rpc_sleep_account_contract"

cc -std=gnu11 -Wall -Wextra -Werror \
	-I "$tmp/include" \
	-c "$impl" \
	-o "$tmp/rpc-sleep-account-off.o"
if nm -g "$tmp/rpc-sleep-account-off.o" | grep -q '__darling_rpc_sleep'; then
	echo "OFF build leaked RPC sleep accounting symbols"
	exit 1
fi

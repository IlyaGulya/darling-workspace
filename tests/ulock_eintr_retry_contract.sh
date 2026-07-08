#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
wait_c="$emu/src/xnu_syscall/bsd/impl/psynch/ulock_wait.c"
wake_c="$emu/src/xnu_syscall/bsd/impl/psynch/ulock_wake.c"
test -f "$wait_c"
test -f "$wake_c"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/conversion" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/psynch"

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

cat > "$tmp/include/darling/emulation/conversion/errno.h" <<'H_EOF'
#pragma once
int errno_linux_to_bsd(int err);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/duct_errno.h" <<H_EOF
#pragma once
#include "$emu/include/conversion/duct_errno.h"
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
#pragma once
#define __NR_futex 202
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), __VA_ARGS__)
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/psynch/ulock_wait.h" <<H_EOF
#pragma once
#include "$emu/include/xnu_syscall/bsd/impl/psynch/ulock_wait.h"
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/psynch/ulock_wake.h" <<H_EOF
#pragma once
#include "$emu/include/xnu_syscall/bsd/impl/psynch/ulock_wake.h"
H_EOF

cc -std=gnu11 -Wall -Wextra -Werror -Wno-unused-parameter \
	-I "$tmp/include" \
	"$PWD/tests/ulock_eintr_retry_contract.c" \
	"$wait_c" \
	"$wake_c" \
	-o "$tmp/ulock_eintr_retry_contract"
"$tmp/ulock_eintr_retry_contract"

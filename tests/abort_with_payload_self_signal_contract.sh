#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
impl="$emu/src/xnu_syscall/bsd/impl/misc/abort_with_payload.c"
test -f "$impl"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/conversion/signal" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/misc" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal"

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

cat > "$tmp/include/darling/emulation/common/simple.h" <<'H_EOF'
#pragma once
void __simple_printf(const char* message, ...);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/signal/duct_signals.h" <<'H_EOF'
#pragma once
int signum_bsd_to_linux(int signum);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
#pragma once
#define __NR_getpid 39
#define __NR_gettid 186
#define __NR_tgkill 234
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL0(n) test_linux_syscall((n))
#define LINUX_SYSCALL1(n, a) test_linux_syscall((n), (long)(a))
#define LINUX_SYSCALL2(n, a, b) test_linux_syscall((n), (long)(a), (long)(b))
#define LINUX_SYSCALL3(n, a, b, c) test_linux_syscall((n), (long)(a), (long)(b), (long)(c))
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), ##__VA_ARGS__)
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/misc/abort_with_payload.h" <<H_EOF
#pragma once
#include "$emu/include/xnu_syscall/bsd/impl/misc/abort_with_payload.h"
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal/kill.h" <<'H_EOF'
#pragma once
long sys_kill(int pid, int sig, int posix);
H_EOF

cc -std=gnu11 -Wall -Wextra -Werror \
	-Wno-unused-parameter \
	-I "$tmp/include" \
	"$PWD/tests/abort_with_payload_self_signal_contract.c" \
	"$impl" \
	-o "$tmp/abort_with_payload_self_signal_contract"
"$tmp/abort_with_payload_self_signal_contract"

#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
fork_c="$emu/src/xnu_syscall/bsd/impl/process/fork.c"
test -f "$fork_c"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common/bsdthread" \
	"$tmp/include/darling/emulation/common/guarded" \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/conversion/signal" \
	"$tmp/include/darling/emulation/conversion" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/linux_premigration/resources" \
	"$tmp/include/darling/emulation/linux_premigration" \
	"$tmp/include/darling/emulation/other/mach" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/process" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd" \
	"$tmp/include/darlingserver"

cat > "$tmp/include/_libkernel_init.h" <<'H_EOF'
#pragma once
typedef struct { int unused; } _libkernel_functions_t;
H_EOF

cat > "$tmp/include/darling/emulation/common/base.h" <<'H_EOF'
#pragma once
#include <stdbool.h>
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

cat > "$tmp/include/darling/emulation/conversion/signal/duct_signals.h" <<'H_EOF'
#pragma once
#define LINUX_SIGCHLD 17
H_EOF

cat > "$tmp/include/darling/emulation/common/bsdthread/per_thread_wd.h" <<'H_EOF'
#pragma once
int get_perthread_wd(void);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/fchdir.h" <<'H_EOF'
#pragma once
long sys_fchdir(int fd);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
#pragma once
#define __NR_clone 56
#define __NR_fork 57
#define LINUX_ECHILD 10
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), __VA_ARGS__)
H_EOF

cat > "$tmp/include/darling/emulation/common/simple.h" <<'H_EOF'
#pragma once
void __simple_printf(const char* message, ...);
void __simple_abort(void);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/elfcalls_wrapper.h" <<'H_EOF'
#pragma once
void __mldr_postfork_child(void);
H_EOF

cat > "$tmp/include/darling/emulation/other/mach/lkm.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/close.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/common/guarded/table.h" <<'H_EOF'
#pragma once
typedef struct {
	void (*close)(int fd);
} guard_entry_options_t;
#define guard_flag_prevent_close 1
#define guard_flag_close_on_fork 2
void guard_table_postfork_child(void);
void guard_table_add(int fd, int flags, guard_entry_options_t* options);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/resources/dserver-ring.h" <<'H_EOF'
#pragma once
void __dserver_ring_postfork_reset(void);
H_EOF

cat > "$tmp/include/darlingserver/rpc.h" <<'H_EOF'
#pragma once
void __dserver_per_thread_socket_refresh(void);
int __dserver_process_lifetime_pipe_refresh(void);
int __dserver_per_thread_socket(void);
int __dserver_get_process_lifetime_pipe(void);
void __dserver_close_socket(int fd);
void __dserver_close_process_lifetime_pipe(int fd);
int dserver_rpc_checkin(int fork_child, void* stack_addr, int lifetime_pipe);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/process/fork.h" <<H_EOF
#pragma once
#include "$emu/include/xnu_syscall/bsd/impl/process/fork.h"
H_EOF

cc -std=gnu11 -Wall -Wextra -Werror \
	-I "$tmp/include" \
	"$PWD/tests/fork_postfork_child_contract.c" \
	"$fork_c" \
	-o "$tmp/fork_postfork_child_contract"
"$tmp/fork_postfork_child_contract"

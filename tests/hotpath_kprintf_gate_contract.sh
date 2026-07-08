#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
test -f "$emu/include/common/simple.h"
test -f "$emu/src/xnu_syscall/bsd/impl/process/execve.c"
test -f "$emu/src/xnu_syscall/bsd/helper/ioctl/filio.c"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common/bsdthread" \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/conversion/fcntl" \
	"$tmp/include/darling/emulation/conversion/ioctl" \
	"$tmp/include/darling/emulation/conversion" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/linux_premigration/resources" \
	"$tmp/include/darling/emulation/linux_premigration/signal" \
	"$tmp/include/darling/emulation/linux_premigration" \
	"$tmp/include/darling/emulation/other/mach" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/helper/ioctl" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/helper/misc" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/fcntl" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/ioctl" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/mman" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/process" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd" \
	"$tmp/include/darlingserver" \
	"$tmp/include/mach-o"

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

cat > "$tmp/include/darling/emulation/common/simple.h" <<H_EOF
#pragma once
#include "$emu/include/common/simple.h"
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
#pragma once
#include <stdint.h>
typedef unsigned long long linux_sigset_t;
#define __NR_rt_sigprocmask 14
#define __NR_rt_sigaction 13
#define __NR_mprotect 10
#define LINUX_SIG_BLOCK 0
#define LINUX_SIG_UNBLOCK 1
#define LINUX_SA_RESTORER 0x04000000
#define LINUX_SA_SIGINFO 4
#define LINUX_SA_RESTART 0x10000000
#define LINUX_SA_ONSTACK 0x08000000
#define LINUX_SIGSTOP 19
#define LINUX_SIGKILL 9
#define LINUX_SIGCHLD 17
#define SIGNAL_SIGEXC_SUSPEND 34
#define SIGNAL_S2C 35
#define LINUX_SYSCALL(n, ...) (-1)
struct linux_siginfo { int si_signo; int si_code; int si_pid; int si_uid; };
struct linux_gregset { unsigned long long gregs[23]; };
typedef void *linux_fpregset_t;
struct linux_ucontext { int unused; };
typedef void linux_sig_handler(int, struct linux_siginfo *, struct linux_ucontext *);
struct linux_sigaction {
	linux_sig_handler *sa_sigaction;
	linux_sigset_t sa_mask;
	unsigned long sa_flags;
	void (*sa_restorer)(void);
};
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/vchroot_expand.h" <<'H_EOF'
#pragma once
#define VCHROOT_FOLLOW 1
struct vchroot_expand_args {
	int flags;
	int dfd;
	char path[4096];
};
int vchroot_expand(struct vchroot_expand_args *args);
H_EOF

cat > "$tmp/include/darling/emulation/common/bsdthread/per_thread_wd.h" <<'H_EOF'
#pragma once
int get_perthread_wd(void);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/errno.h" <<'H_EOF'
#pragma once
int errno_linux_to_bsd(int ret);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/fcntl/open.h" <<'H_EOF'
#pragma once
#define BSD_O_RDONLY 0
H_EOF

cat > "$tmp/include/darling/emulation/conversion/ioctl/ioctl.h" <<'H_EOF'
#pragma once
#define IOCTL_HANDLED 1
#define IOCTL_PASS 0
#define BSD_FIODTYPE 0x4004667a
#define BSD_FIONBIO 0x8004667e
#define BSD_FIOASYNC 0x8004667d
#define BSD_FIOCLEX 0x20006601
#define BSD_FIONCLEX 0x20006602
#define BSD_FIONREAD 0x4004667f
#define BSD_FIOGETOWN 0x4004667b
#define BSD_FIOSETOWN 0x8004667c
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/helper/misc/fdpath.h" <<'H_EOF'
#pragma once
int fdpath(int fd, char *buf, unsigned long len);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/ioctl/ioctl.h" <<'H_EOF'
#pragma once
int __real_ioctl(int fd, unsigned long cmd, void *arg);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/fcntl/open.h" <<'H_EOF'
#pragma once
long sys_open(const char *path, int flags, int mode);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/fcntl/fcntl.h" <<'H_EOF'
#pragma once
long sys_fcntl(int fd, int cmd, long arg);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/read.h" <<'H_EOF'
#pragma once
long sys_read(int fd, void *buf, unsigned long size);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/close.h" <<'H_EOF'
#pragma once
long close_internal(int fd);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/readlink.h" <<'H_EOF'
#pragma once
long sys_readlink(const char *path, char *buf, unsigned long size);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/write.h" <<'H_EOF'
#pragma once
long sys_write(int fd, const void *buf, unsigned long size);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/signal/sigexc.h" <<'H_EOF'
#pragma once
void darling_sigexc_self(void);
void sigexc_setup1(void);
void sigexc_setup2(void);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/elfcalls_wrapper.h" <<'H_EOF'
#pragma once
int elf_calls_exec(const char *path, const char **argv, const char **envp);
H_EOF

cat > "$tmp/include/darlingserver/rpc.h" <<'H_EOF'
#pragma once
#include <stdint.h>
int dserver_rpc_mldr_path(char *path, uint64_t size, uint64_t *path_length);
int dserver_rpc_interrupt_exit(void);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/resources/dserver-ring.h" <<'H_EOF'
#pragma once
#include <stdint.h>
int __dserver_ring_mldr_path(uint64_t path, uint64_t size, uint64_t *path_length, int *t_code);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/resources/dserver-rpc-defs.h" <<'H_EOF'
#pragma once
struct linux_sockaddr_un {
	unsigned short sun_family;
	char sun_path[108];
};
H_EOF

cat > "$tmp/include/_libkernel_init.h" <<'H_EOF'
#pragma once
typedef struct { int unused; } _libkernel_functions_t;
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal/sigaction.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal/sigaltstack.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal/kill.h" <<'H_EOF'
#pragma once
long sys_kill(int pid, int sig);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/exit.h" <<'H_EOF'
#pragma once
void sys_exit(int code);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/mman/mman.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/other/mach/lkm.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/mach-o/loader.h" <<'H_EOF'
#pragma once
#define MH_MAGIC 0xfeedface
#define MH_CIGAM 0xcefaedfe
#define MH_MAGIC_64 0xfeedfacf
#define MH_CIGAM_64 0xcffaedfe
H_EOF

cat > "$tmp/include/mach-o/fat.h" <<'H_EOF'
#pragma once
#define FAT_MAGIC 0xcafebabe
#define FAT_CIGAM 0xbebafeca
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/helper/ioctl/filio.h" <<'H_EOF'
#pragma once
int handle_filio(int fd, int cmd, void *arg, int *retval);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/process/execve.h" <<'H_EOF'
#pragma once
long sys_execve(const char *fname, const char **argvp, const char **envp);
H_EOF

cat > "$tmp/macro_default.c" <<'C_EOF'
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <darling/emulation/common/simple.h>
static int kprintf_calls;
static int side_effects;
void __simple_kprintf(const char *format, ...) {
	(void)format;
	kprintf_calls++;
}
static int side_effect(void) {
	side_effects++;
	return 7;
}
int main(void) {
	hotpath_kdebug("value %d\n", side_effect());
	if (kprintf_calls != 0 || side_effects != 0) {
		fprintf(stderr, "default hotpath_kdebug evaluated: calls=%d side=%d\n", kprintf_calls, side_effects);
		return 1;
	}
	return 0;
}
C_EOF

cat > "$tmp/macro_debug.c" <<'C_EOF'
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#define DARLING_HOTPATH_KPRINTF_DEBUG 1
#include <darling/emulation/common/simple.h>
static int kprintf_calls;
static int side_effects;
void __simple_kprintf(const char *format, ...) {
	(void)format;
	kprintf_calls++;
}
static int side_effect(void) {
	side_effects++;
	return 7;
}
int main(void) {
	hotpath_kdebug("value %d\n", side_effect());
	if (kprintf_calls != 1 || side_effects != 1) {
		fprintf(stderr, "debug hotpath_kdebug did not call: calls=%d side=%d\n", kprintf_calls, side_effects);
		return 1;
	}
	return 0;
}
C_EOF

include_flags=(-I "$tmp/include" -I "$emu/include")
cc -std=gnu11 -Wall -Wextra "${include_flags[@]}" "$tmp/macro_default.c" -o "$tmp/macro-default"
"$tmp/macro-default"
cc -std=gnu11 -Wall -Wextra "${include_flags[@]}" "$tmp/macro_debug.c" -o "$tmp/macro-debug"
"$tmp/macro-debug"

cc -std=gnu11 -w "${include_flags[@]}" -c \
	"$emu/src/xnu_syscall/bsd/impl/process/execve.c" -o "$tmp/execve.o"
cc -std=gnu11 -w "${include_flags[@]}" -c \
	"$emu/src/xnu_syscall/bsd/helper/ioctl/filio.c" -o "$tmp/filio.o"
fail=0
for object in "$tmp/execve.o" "$tmp/filio.o"; do
	if nm -u "$object" | grep -q ' __simple_kprintf$'; then
		echo "$(basename "$object"): default build still references __simple_kprintf"
		fail=1
	fi
done

if [ "$fail" -ne 0 ]; then
	exit 1
fi

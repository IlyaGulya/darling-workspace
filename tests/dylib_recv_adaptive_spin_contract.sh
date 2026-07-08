#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
emu="$src/darling/src/libsystem_kernel/emulation"
hdr="$emu/include/linux_premigration/resources/dserver-rpc-defs.h"
test -f "$hdr"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/conversion/network" \
	"$tmp/include/darling/emulation/conversion" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/linux_premigration/resources" \
	"$tmp/include/darling/emulation/linux_premigration" \
	"$tmp/include/darling/emulation/other/mach" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/network" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd" \
	"$tmp/include/darlingserver" \
	"$tmp/include/sys/_types"

cat > "$tmp/include/sys/_types/_iovec_t.h" <<'H_EOF'
#pragma once
#include <stddef.h>
struct iovec {
	void* iov_base;
	size_t iov_len;
};
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/network/recvmsg.h" <<'H_EOF'
#pragma once
#include <stddef.h>
#include <sys/_types/_iovec_t.h>
#define LINUX_MSG_DONTWAIT 0x40
#define LINUX_SOL_SOCKET 1
#define LINUX_CMSG_SPACE(len) (sizeof(struct linux_cmsghdr) + (len))
#define LINUX_CMSG_LEN(len) (sizeof(struct linux_cmsghdr) + (len))
struct linux_cmsghdr {
	size_t cmsg_len;
	int cmsg_level;
	int cmsg_type;
	unsigned char cmsg_data[32];
};
struct linux_msghdr {
	void* msg_name;
	size_t msg_namelen;
	struct iovec* msg_iov;
	size_t msg_iovlen;
	void* msg_control;
	size_t msg_controllen;
	int msg_flags;
};
H_EOF

for header in \
	darling/emulation/xnu_syscall/bsd/impl/network/sendmsg.h \
	darling/emulation/xnu_syscall/bsd/impl/network/getsockopt.h \
	darling/emulation/xnu_syscall/bsd/impl/network/sendto.h \
	darling/emulation/conversion/duct_errno.h \
	darling/emulation/conversion/network/duct.h \
	darling/emulation/conversion/network/getsockopt.h \
	darling/emulation/other/mach/lkm.h \
	rtsig.h
do
	mkdir -p "$tmp/include/$(dirname "$header")"
	printf '#pragma once\n' > "$tmp/include/$header"
done

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
static inline void __simple_printf(const char* message, ...) { (void)message; }
static inline void __simple_abort(void) { __builtin_trap(); }
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/elfcalls_wrapper.h" <<'H_EOF'
#pragma once
static inline void* __dserver_socket_address(void) { return (void*)0; }
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/close.h" <<'H_EOF'
#pragma once
static inline long close_internal(int fd) { (void)fd; return 0; }
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal/sigprocmask.h" <<'H_EOF'
#pragma once
typedef unsigned long sigset_t;
#define SIG_BLOCK 0
#define SIG_SETMASK 2
static inline long sys_sigprocmask(int how, const sigset_t* set, sigset_t* oldset)
{
	(void)how;
	(void)set;
	if (oldset)
		*oldset = 0;
	return 0;
}
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
#pragma once
#define __NR_getpid 39
#define __NR_gettid 186
#define __NR_recvmsg 47
#define __NR_sendmsg 46
#define __NR_sendto 44
#define __NR_mmap 9
#define __NR_munmap 11
#define __NR_mprotect 10
#define __NR_msync 26
#define __NR_close 3
#define __NR_sched_yield 24
#define LINUX_EAGAIN 11
#define LINUX_EIO 5
#define LINUX_EBADMSG 74
#define LINUX_ECOMM 70
#define LINUX_EPIPE 32
#define LINUX_EINTR 4
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL0(n) test_linux_syscall((n))
#define LINUX_SYSCALL1(n, a) test_linux_syscall((n), (a))
#define LINUX_SYSCALL2(n, a, b) test_linux_syscall((n), (a), (b))
#define LINUX_SYSCALL3(n, a, b, c) test_linux_syscall((n), (a), (b), (c))
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), ##__VA_ARGS__)
H_EOF

cat > "$tmp/include/darlingserver/rpc-supplement.h" <<'H_EOF'
#pragma once
#include <stdint.h>
typedef struct {
	uint32_t call_number;
	int pid;
	int tid;
	int architecture;
	uint32_t s2c_number;
} dserver_s2c_callhdr_t;
enum {
	dserver_s2c_msgnum_mmap = 1,
	dserver_s2c_msgnum_munmap = 2,
	dserver_s2c_msgnum_mprotect = 3,
	dserver_s2c_msgnum_msync = 4,
};
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	uint64_t length;
	int protection;
	int flags;
	int fd;
	uint64_t offset;
} dserver_s2c_call_mmap_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	int errno_result;
} dserver_s2c_reply_mmap_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	uint64_t length;
} dserver_s2c_call_munmap_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_munmap_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	uint64_t length;
	int protection;
} dserver_s2c_call_mprotect_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_mprotect_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	uint64_t size;
	int sync_flags;
} dserver_s2c_call_msync_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_msync_t;
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/resources/dserver-rpc-defs.h" <<H_EOF
#pragma once
#include "$hdr"
H_EOF

build_and_run() {
	local name="$1"
	shift
	cc -std=gnu11 -Wall -Wextra -Werror \
		-Wno-unused-function -Wno-unused-label -Wno-sign-compare -Wno-int-conversion \
		-I "$tmp/include" \
		"$@" \
		"$PWD/tests/dylib_recv_adaptive_spin_contract.c" \
		-o "$tmp/$name"
	"$tmp/$name"
}

build_and_run recvspin-enabled -DDARLING_GUEST_RECVSPIN=3
build_and_run recvspin-disabled -DDARLING_GUEST_RECVSPIN=0

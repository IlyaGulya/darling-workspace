/*
This file is part of Darling.

Copyright (C) 2017 Lubos Dolezel

Darling is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Darling is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Darling.  If not, see <http://www.gnu.org/licenses/>.
*/

#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdarg.h>
// MEASURED (doc section 183): the loader replaces the guest's environment while a guest image is being set up,
// so a `getenv` consulted from inside long-running loader code stops answering -- the process sat in its reply loop
// for thirty seconds with every diagnostic suppressed because the gate read NULL. The decision is therefore taken
// ONCE, on the first call, and never re-read.
static int mldr_diag_enabled = -1;
static int mldr_diag_on(void) {
	if (mldr_diag_enabled < 0) {
		mldr_diag_enabled = (getenv("MLDR_COURIER_DIAG") != NULL) ? 1 : 0;
	}
	return mldr_diag_enabled;
}

static int mldr_diag_fd = -1; // set on first use: MLDR_DIAG_LOG when named, else fd 2
static void mldr_diagf(const char* fmt, ...); // see the definition
#include <stdbool.h>
#include <mach-o/loader.h>
#include <mach-o/fat.h>
#include <dlfcn.h>
#include <endian.h>
#include "commpage.h"
#include "loader.h"
// perf#30: select the ring definitions in the shared header BEFORE the first include of it, so the
// bootstrap can see both the process-control ABI and the ring control block from one include.
#define DSERVER_RING_TRANSPORT 1
#include <darlingserver/rpc.h>            // callnums first: the header derives the opcode hash from them
#include <darlingserver/rpc-supplement.h>
#include "glibc_fork_reset.h"
#include "signal_atomic.h"
#include "stack_mapping.h"
#include "elfcalls/threads.h"
#include <sys/resource.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <darlingserver/rpc.h>
#include <sys/ptrace.h>
#include <pthread.h>
#include <sys/utsname.h>

#ifndef PAGE_SIZE
#	define PAGE_SIZE	4096
#endif
#define PAGE_ALIGN(x) (x & ~(PAGE_SIZE-1))

static const char* dyld_path = INSTALL_PREFIX "/libexec/usr/lib/dyld";

struct sockaddr_un __dserver_socket_address_data = {
	.sun_family = AF_UNIX,
	.sun_path = "\0",
};

int __dserver_main_thread_socket_fd = -1;
int __dserver_process_lifetime_pipe_fd = -1;

// The idea of mldr is to load dyld_path into memory and set up the stack
// as described in dyldStartup.S.
// After that, we pass control over to dyld.
//
// Additionally, mldr providers access to native platforms libdl.so APIs (ELF loader).

#ifdef __x86_64__
static void load64(int fd, bool expect_dylinker, struct load_results* lr);
static void reexec32(char** argv);
#endif
static void load32(int fd, bool expect_dylinker, struct load_results* lr);
static void load_fat(int fd, cpu_type_t cpu, bool expect_dylinker, char** argv, struct load_results* lr);
static void load(const char* path, cpu_type_t cpu, bool expect_dylinker, char** argv, struct load_results* lr);
static int native_prot(int prot);
static void setup_space(struct load_results* lr, bool is_64_bit);
static void finish_space_after_checkin(struct load_results* lr);
static void mldr_do_pending_checkin(void);
static void process_special_env(struct load_results* lr);
static void start_thread(struct load_results* lr);
static bool is_kernel_at_least(int major, int minor);
static void* compatible_mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset);
#ifdef __x86_64__
static void setup_stack64(const char* filepath, struct load_results* lr);
#endif
static void setup_stack32(const char* filepath, struct load_results* lr);

// this is called when argv[0] specifies an interpreter and we need to "unexpand" it (i.e. convert it from a Linux path to a vchrooted path)
static void vchroot_unexpand_interpreter(struct load_results* lr);

// UUID of the main executable
uint8_t exe_uuid[16];

// globally visible for debugging/core-dumping purposes
// however, this should not be relied on; a pointer to this should passed around to whoever needs the load_results structure
__attribute__((used))
struct load_results mldr_load_results = {0};

static uint32_t stack_size = 0;

static const char* const skip_env_vars[] = {
	"__mldr_bprefs=",
	"__mldr_sockpath=",
	"__mldr_lifetime_pipe",
};

/* TEST-ONLY HOST FAULT WITNESS (DARLING_TEST_FAULT_WITNESS=1).
 * Armed in the LOADER, which is present in every guest process and is native host code, so it can use the
 * host's own sigaction/ucontext instead of hand-decoding a Linux frame from a Darwin guest. An earlier attempt
 * armed the same witness inside the guest workload: it announced itself and then never fired, and whole runs
 * now end with no result line at all, so the fatal signal may be landing on a guest process that is not the
 * workload. Written with raw write(2), formatted by hand, bounded, no malloc and no Darling RPC. It restores
 * the default action and returns, so the process still dies of the same fault afterwards. */
/* A RAW SYSCALL, USED FROM THE FAULT WITNESS. MEASURED: asking arch_prctl through libc's `syscall()` from inside
 * the witness made every fault line disappear -- a faulting thread whose thread pointer is NULL cannot survive a
 * wrapper that writes `errno`, so the handler died reporting the fault it was there to describe. Inline `syscall`
 * cannot touch TLS, which is what makes it usable while the TLS state itself is the question. */
static long fw_raw_syscall2(long n, long a, long b)
{
	long ret;
	__asm__ volatile("syscall" : "=a"(ret) : "a"(n), "D"(a), "S"(b) : "rcx", "r11", "memory");
	return ret;
}

static void fw_witness(int sig, siginfo_t* info, void* uctx)
{
	char buf[512];
	int n = 0;
	static const char* dig = "0123456789abcdef";
	static const char* p1 = "FAULT-WITNESS sig=";
	static const char* p2 = " host_pid=";
	static const char* p3 = " host_tid=";
	static const char* p4 = " si_code=";
	static const char* p5 = " si_addr=0x";
	static const char* p6 = " rip=0x";
	static const char* p7 = " rsp=0x";
	for (const char* p = p1; *p && n < 400; ++p) buf[n++] = *p;
	{ int v = sig; if (v < 0) { buf[n++] = '-'; v = -v; } char tmp[12]; int k = 0; if (v == 0) tmp[k++] = '0';
	  while (v > 0) { tmp[k++] = (char)('0' + v % 10); v /= 10; } while (k > 0) buf[n++] = tmp[--k]; }
	for (const char* p = p2; *p && n < 400; ++p) buf[n++] = *p;
	{ long v = (long)getpid(); char tmp[24]; int k = 0; if (v == 0) tmp[k++] = '0';
	  while (v > 0) { tmp[k++] = (char)('0' + v % 10); v /= 10; } while (k > 0) buf[n++] = tmp[--k]; }
	for (const char* p = p3; *p && n < 400; ++p) buf[n++] = *p;
	{ long v = (long)syscall(186 /* gettid */); char tmp[24]; int k = 0; if (v == 0) tmp[k++] = '0';
	  while (v > 0) { tmp[k++] = (char)('0' + v % 10); v /= 10; } while (k > 0) buf[n++] = tmp[--k]; }
	for (const char* p = p4; *p && n < 400; ++p) buf[n++] = *p;
	{ int v = info ? info->si_code : -1; if (v < 0) { buf[n++] = '-'; v = -v; } char tmp[12]; int k = 0;
	  if (v == 0) tmp[k++] = '0'; while (v > 0) { tmp[k++] = (char)('0' + v % 10); v /= 10; }
	  while (k > 0) buf[n++] = tmp[--k]; }
	for (const char* p = p5; *p && n < 400; ++p) buf[n++] = *p;
	{ unsigned long v = info ? (unsigned long)info->si_addr : 0; for (int i = 60; i >= 0; i -= 4) buf[n++] = dig[(v >> i) & 0xf]; }
	for (const char* p = p6; *p && n < 400; ++p) buf[n++] = *p;
	{ ucontext_t* uc = (ucontext_t*)uctx; unsigned long v = uc ? (unsigned long)uc->uc_mcontext.gregs[REG_RIP] : 0;
	  for (int i = 60; i >= 0; i -= 4) buf[n++] = dig[(v >> i) & 0xf]; }
	for (const char* p = p7; *p && n < 400; ++p) buf[n++] = *p;
	{ ucontext_t* uc = (ucontext_t*)uctx; unsigned long v = uc ? (unsigned long)uc->uc_mcontext.gregs[REG_RSP] : 0;
	  for (int i = 60; i >= 0; i -= 4) buf[n++] = dig[(v >> i) & 0xf]; }
	/* TLS BASE. si_addr=0x8 is the shape of a load through a NULL thread pointer plus an 8-byte offset, so whether
	 * the FAULTING thread has a thread pointer at all separates "this thread never had TLS" from "it had TLS and
	 * something else was NULL". arch_prctl(ARCH_GET_FS) is a syscall, so it is safe in a signal handler, and it
	 * answers for the thread that actually faulted. */
	{
		static const char* p8 = " fs=0x";
		for (const char* p = p8; *p && n < 440; ++p) buf[n++] = *p;
		unsigned long fs = 0;
		if (fw_raw_syscall2(158 /* arch_prctl */, 0x1003 /* ARCH_GET_FS */, (long)&fs) != 0) { fs = 0; }
		for (int i = 60; i >= 0; i -= 4) buf[n++] = dig[(fs >> i) & 0xf];
		fs = 0;
		if (fw_raw_syscall2(158 /* arch_prctl */, 0x1002 /* ARCH_GET_GS */, (long)&fs) != 0) { fs = 0; }
		static const char* p9 = " gs=0x";
		for (const char* p = p9; *p && n < 470; ++p) buf[n++] = *p;
		for (int i = 60; i >= 0; i -= 4) buf[n++] = dig[(fs >> i) & 0xf];
	}
	buf[n++] = '\n';
	(void)!write(2, buf, (size_t)n);
	struct sigaction dfl;
	memset(&dfl, 0, sizeof(dfl));
	dfl.sa_handler = SIG_DFL;
	sigaction(sig, &dfl, NULL);
}

static void* fw_rearm_loop(void* arg)
{
	/* RE-ARMING: the loader installs the witness at process start, and Darling's own install happens later (the
	 * guest image's sigaction/sigexc setup), which is why an armed witness that never fires is more likely to
	 * have been REPLACED than to have missed the fault. Re-installing in a loop under the same environment makes
	 * the witness the disposition at the moment of the fault; if it then fires, replacement is confirmed and the
	 * fault address is finally available. */
	(void)arg;
	for (;;) {
		struct sigaction sa;
		memset(&sa, 0, sizeof(sa));
		sa.sa_sigaction = fw_witness;
		sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
		sigemptyset(&sa.sa_mask);
		sigaction(SIGSEGV, &sa, NULL);
		sigaction(SIGBUS, &sa, NULL);
		usleep(20000);
	}
	return NULL;
}

static void fw_arm(void)
{
	pthread_t th;
	if (pthread_create(&th, NULL, fw_rearm_loop, NULL) == 0) {
		pthread_detach(th);
	}
	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_sigaction = fw_witness;
	sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGSEGV, &sa, NULL);
	sigaction(SIGBUS, &sa, NULL);
	(void)!write(2, "FAULT-WITNESS armed via loader\n", 31);
}

void* __mldr_main_stack_top = NULL;

static int kernel_major = -1;
static int kernel_minor = -1;

void __mldr_postfork_child(void);

// perf#30 STAGE 2: defined below; main seeds the process-global lane for the main thread.
void* __mldr_ring_lane_seed(void);
// perf#30 PROCESS-CONTROL PLANE / FD-COURIER: defined below; the bootstrap uses them earlier in the file.
const void* __mldr_fd_courier_address(void);
int __mldr_fd_courier_socket(void);
uint64_t __mldr_process_generation(void);
int __mldr_process_control_create(void);
int __mldr_process_control_request(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3);
static int __mldr_process_control_request_once(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3);
int __mldr_process_control_ready(void);
int __mldr_process_control_wait_ready(int timeout_ms);
void* __mldr_process_control_page(void);
// Forward declarations: both are defined far below, and the control-page setup (which runs early) needs them to
// try to adopt the process doorbell before the first plane publish.
int __mldr_ring_doorbell(int fd);
static int __mldr_fd_courier_recv_once(int sock);

// perf#30 PHASE-0 diagnostics: every line names the INVOCATION, not just the pid. MEASURED need: two
// execve images of one process share the pid, so a trace without the image cannot say whether two lines
// are one invocation's sequence or two invocations' interleaving -- and that ambiguity is exactly what the
// readiness question ran into.
static unsigned __mldr_diag_seq(void);
static unsigned __mldr_diag_seq_counter = 0;
static unsigned __mldr_diag_seq(void) {
	return ++__mldr_diag_seq_counter;
}

static const char* __mldr_diag_image(const char* arg0) {
	return (arg0 != NULL) ? arg0 : "(none)";
}

// Read-only view for diagnostics: the POINTER ONLY, never a call that may create the page.
static struct dserver_process_control* mldr_process_control_page_readonly(void);

// perf#30 LOADER CHECKIN ORDERING: the deferred checkin's context. Values only -- see the note at the
// recording site: the recording frame returns before the deferred call runs.
struct mldr_pending_checkin {
	int lifetime_read_fd;
	uintptr_t stack_hint;
	uint32_t arch_bits;
	bool needed;
};
static struct mldr_pending_checkin g_pending_checkin = { -1, 0, 0, false };

static struct mldr_ring_lane_record* mldr_ring_lane_rec = 0;
int __mldr_ring_call(uint32_t callnum, const void* req, uint32_t reqlen, void* rep, uint32_t replen);

// perf#30 (doc section 169): the second incarnation EXITS during the load of launchd, with no line of its own --
// the bounded waits would have reported and were not reached, so it is not a wait; and the loader's own failures
// all print, so this names the remaining possibility. Raw syscalls only (a handler cannot use buffered stdio and
// may run before anything is initialised), and every ABI register the caller can depend on is preserved.
static void mldr_signal_probe(int sig) {
	static const char pre[] = "[mldr-SIGNAL ";
	char digits[4];
	int n = 0, v = sig;
	// fd 2 AND fd 1: the hard measure of this section is that by the time the second incarnation is loading,
	// fd 2 is the launchd image's /dev/null -- so a signal probe that reported only there could not be seen at
	// exactly the moment it mattered.
	for (int which = 0; which < 2; ++which) {
		long fd = (which == 0) ? (mldr_diag_fd >= 0 ? mldr_diag_fd : 2) : 2;
		long rax = 1, rdi = fd, rsi = (long)pre, rdx = sizeof(pre) - 1;
		__asm__ volatile("syscall" : "+a"(rax), "+D"(rdi), "+S"(rsi), "+d"(rdx) : : "rcx", "r11", "memory");
	}
	if (v >= 100) { digits[n++] = (char)('0' + (v / 100) % 10); }
	if (v >= 10) { digits[n++] = (char)('0' + (v / 10) % 10); }
	digits[n++] = (char)('0' + v % 10);
	digits[n++] = '\n';
	for (int which = 0; which < 2; ++which) {
		long fd = (which == 0) ? (mldr_diag_fd >= 0 ? mldr_diag_fd : 2) : 2;
		long rax = 1, rdi = fd, rsi = (long)digits, rdx = n;
		__asm__ volatile("syscall" : "+a"(rax), "+D"(rdi), "+S"(rsi), "+d"(rdx) : : "rcx", "r11", "memory");
	}
	_exit(91);
}

static void mldr_install_signal_probe(void) {
	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = mldr_signal_probe;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGSEGV, &sa, NULL);
	sigaction(SIGSYS, &sa, NULL);
	sigaction(SIGILL, &sa, NULL);
	sigaction(SIGBUS, &sa, NULL);
	sigaction(SIGABRT, &sa, NULL);
	sigaction(SIGFPE, &sa, NULL);
}

int main(int argc, char** argv, char** envp)
{
	if (getenv("DARLING_TEST_FAULT_WITNESS") != NULL) { fw_arm(); }
	void** sp;
	int pushCount = 0;
	char *filename, *p = NULL;
	size_t arg_strings_total_size_after = 0;
	size_t orig_argv0_len = 0;
	const char* orig_argv1 = NULL;

	mldr_load_results.kernfd = -1;
	mldr_load_results.argc = argc;
	mldr_load_results.argv = argv;

	// Locate glibc's loader/stack-cache locks while still single-threaded, so the
	// fork child can reset them and avoid an inherited-held-lock deadlock (dar-gwn.5).
	__mldr_glibc_fork_reset_detect();

	while (envp[mldr_load_results.envc] != NULL) {
		++mldr_load_results.envc;
	}
	mldr_load_results.envp = envp;

	// sys_execve() passes the original file path appended to the mldr path in argv[0].
	if (argc > 0)
		p = strchr(argv[0], '!');

	if (argc <= 1)
	{
		if (p == NULL) {
			fprintf(stderr, "mldr is part of Darling. It is not to be executed directly.\n");
			return 1;
		}
		else
		{
			fprintf(stderr, "mldr: warning: Executing with no argv[0]. Continuing anyway, but this is probably a bug.\n");
		}
	}

	if (p != NULL)
	{
		filename = (char*) __builtin_alloca(strlen(argv[0])+1);
		strcpy(filename, p + 1);
	}
	else
	{
		filename = (char*) __builtin_alloca(strlen(argv[1])+1);
		strcpy(filename, argv[1]);
	}

	// allow any process to ptrace us
	// the only process we really care about being able to do this is the server,
	// but we can't just use the server's PID, since it lies outside our PID namespace.
	ptrace(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);

	process_special_env(&mldr_load_results);
#ifdef __i386__

	mldr_install_signal_probe();

	load(filename, CPU_TYPE_X86, false, argv, &mldr_load_results); // accept i386 only
#else
	load(filename, 0, false, argv, &mldr_load_results);
#endif

	// this was previously necessary when we were loading the binary from the LKM
	// (presumably because the break was detected incorrectly)
	// but this shouldn't be necessary for loading Mach-O's from userspace (the heap space should already be set up properly).
	// see https://github.com/darlinghq/darling/issues/469 for the issue this originally fixed in the LKM
#if 0
	if (prctl(PR_SET_MM, PR_SET_MM_BRK, PAGE_ALIGN(mldr_load_results.vm_addr_max), 0, 0) < 0) {
		fprintf(stderr, "Failed to set BRK value\n");
		return 1;
	}

	if (prctl(PR_SET_MM, PR_SET_MM_START_BRK, PAGE_ALIGN(mldr_load_results.vm_addr_max), 0, 0) < 0) {
		fprintf(stderr, "Failed to set BRK start\n");
		return 1;
	}
#endif

	// adjust argv (remove mldr's argv[0])
	// NOTE: this code assumes that the current argv array points to contiguous strings.
	//       this is not necessarily true, although AFAIK this is always true on Linux.
	// also note: we do it this way (moving the string contents in addition to the pointers)
	//            so that Linux sees our modified argv array without having to use PR_SET_MM_ARG_START
	//            and PR_SET_MM_ARG_END (since those require CAP_SYS_RESOURCE)

	--mldr_load_results.argc;

	orig_argv0_len = strlen(mldr_load_results.argv[0]) + 1;
	orig_argv1 = mldr_load_results.argv[1];

	for (size_t i = 0; i < mldr_load_results.argc; ++i) {
		mldr_load_results.argv[i] = mldr_load_results.argv[0] + arg_strings_total_size_after;
		arg_strings_total_size_after += strlen(mldr_load_results.argv[i + 1]) + 1;
	}
	mldr_load_results.argv[mldr_load_results.argc] = NULL;

	memmove(mldr_load_results.argv[0], orig_argv1, arg_strings_total_size_after);
	memset(mldr_load_results.argv[0] + arg_strings_total_size_after, 0, orig_argv0_len);

	if (p == NULL) {
		vchroot_unexpand_interpreter(&mldr_load_results);
	}

	// adjust envp (remove special mldr variables)
	// NOTE: same as for argv; here we assume the envp strings are contiguous
	for (size_t i = 0; i < mldr_load_results.envc; ++i) {
		if (!mldr_load_results.envp[i]) {
			mldr_load_results.envc = i;
			break;
		}

		size_t len = strlen(mldr_load_results.envp[i]) + 1;

		#define ENV_VAR_MATCHES(_name) \
			(len > sizeof(_name) - 1 && strncmp(mldr_load_results.envp[i], _name, sizeof(_name) - 1) == 0)

		// Don't pass these special env vars down to userland
		if (
			ENV_VAR_MATCHES("__mldr_bprefs=")   ||
			ENV_VAR_MATCHES("__mldr_sockpath=") ||
			ENV_VAR_MATCHES("__mldr_runtime_mode=")
		) {
			size_t len_after = 0;
			const char* orig_envp_i_plus_one = mldr_load_results.envp[i + 1];

			--mldr_load_results.envc;

			for (size_t j = i; j < mldr_load_results.envc; ++j) {
				mldr_load_results.envp[j] = mldr_load_results.envp[i] + len_after;
				len_after += strlen(mldr_load_results.envp[j + 1]) + 1;
			}
			mldr_load_results.envp[mldr_load_results.envc] = NULL;

			memmove(mldr_load_results.envp[i], orig_envp_i_plus_one, len_after);
			memset(mldr_load_results.envp[i] + len_after, 0, len);

			// we have to check this index again because it now points to a different string
			--i;
			continue;
		}
		// If we were passed __mldr_DYLD_ROOT_PATH, it is a special case of DYLD_ROOT_PATH needing to be set,
		// so we remove the prefix, so dyld reads it as DYLD_ROOT_PATH
		else if (ENV_VAR_MATCHES("__mldr_DYLD_ROOT_PATH=")) {
			const char* env_p = mldr_load_results.envp[i];
			env_p += (sizeof("__mldr_") - 1);
			size_t len_remaining = strlen(env_p) + 1;
			memmove(mldr_load_results.envp[i], env_p, len_remaining);
		}
	}

	if (mldr_load_results._32on64)
		setup_stack32(filename, &mldr_load_results);
	else
#ifdef __x86_64__
		setup_stack64(filename, &mldr_load_results);
#elif __aarch64__
	#error TODO: aarch64
#else
		abort();
#endif



	// MEASURED (round 48, RED): routing this write through the process-control plane, with the plane
	// established BEFORE it, stalls the rootless shellspawn handshake ("did not become ready within
	// 30000ms") even though the server DID service the plane for that pid (region + `request op=1` logged
	// for the shellspawn pid). So the loader's first writes must precede the plane's establishment, the
	// same family as the two round-27/28 negatives for the lane. It stays on the datagram path.
	// perf#30 PHASE-0 (round 49f): the process-control plane is established BEFORE the first bootstrap
	// write, and the guest waits on TRANSPORT readiness -- the server sets it the moment it has mapped
	// the region, which it does from the courier's SO_PEERCRED pid alone, with no Process and no Thread.
	// MEASURED in round 48: establishing the plane here WITH a synchronous semantic PING was RED (the
	// shellspawn handshake never completed) because the PING's answer depends on the normal Process
	// state. What changed is the dependency, not the position: this waits for a transport
	// acknowledgement, which the courier path can give before any semantic registration exists.

	// perf#30 (doc section 63): REVERTED. Establishing the transport above `load()` DOES make the page
	// available at the checkin site -- MEASURED: `checkin-route ... ready=1 page=0x7aded64ac000` -- and the
	// boot then FAILS (`HELLO=0`, shellspawn never ready), exactly as it does when the establishment is
	// moved inside setup_space (section 61). Two different earlier positions, the same RED, with the page
	// demonstrably present in both: so the RED is caused by the page route for THIS checkin, not by a
	// missing page and not by the position of the establishment. The original comment was right and is now
	// measured rather than assumed: this checkin's result must be visible to the process's other traffic,
	// and the page is serviced on the server's own pass. It stays on the datagram path.
	{
		int created = __mldr_process_control_create();
		if (created == 0) {
			__mldr_process_control_wait_ready(200);
			// perf#28d: the plane's probe now carries the incarnation's GENERATION in payload[3]. MEASURED
			// reason: the server learned the generation only from the courier drain, so the LOADER's own
			// first attach -- which precedes its own courier traffic -- was stamped generation 0, the guest
			// refused the doorbell bundle as stale, and the one-time delivery could not be enabled. The
			// page is established before that attach, so this is the one place that can teach it in time.
			(void)__mldr_process_control_request(DSERVER_PROCESS_CONTROL_OP_PING, 0, 0, 0, __mldr_process_generation());
			if (mldr_diag_on()) {
				mldr_diagf("[mldr-ctl] ready pid=%d state=%d page=%p sz=%zu off=%zu\n", (int)getpid(),
				__mldr_process_control_ready(), mldr_process_control_page_readonly(),
				sizeof(struct dserver_process_control),
				(size_t)((char*)&((struct dserver_process_control*)0)->transport_ready - (char*)0));
				mldr_diagf("[mldr-ctl]   ready-image=%s\n", __mldr_diag_image(argv[0]));
			}


		} else if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] create failed pid=%d tid=%d\n", (int)getpid(), (int)gettid());
		}
	}

	// perf#30 LOADER CHECKIN ORDERING (doc section 165): the ORDER the checkin needs. The load/parse work is
	// done, the plane is established and transport-ready, and now -- BEFORE anything that must observe the
	// registration -- the checkin goes through the management plane, and only then does the suffix that
	// depends on it (the vchroot path) run. The datagram route is gone from this path entirely.
	mldr_bootstrap_before_dylinker_load(&mldr_load_results);

	// The first two bootstrap writes now travel on the ONE ordered shared channel when it is ready. They
	// move TOGETHER: a split stream (one on the page, one on a socket) is exactly the cross-transport
	// ordering race this plane exists to remove.
	int status;
	if (__mldr_process_control_ready()) {
		status = __mldr_process_control_request(DSERVER_PROCESS_CONTROL_OP_SET_DYLD_INFO,
			mldr_load_results.dyld_all_image_location, mldr_load_results.dyld_all_image_size, 0, 0);
	} else {
		status = dserver_rpc_set_dyld_info(mldr_load_results.dyld_all_image_location, mldr_load_results.dyld_all_image_size);
	}
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] seq=%u after-dyld pid=%d status=%d ready=%d image=%s\n", __mldr_diag_seq(), (int)getpid(), status, __mldr_process_control_ready(), __mldr_diag_image(argv[0]));
	}
	if (status < 0) {
		fprintf(stderr, "Failed to tell darlingserver about our dyld info\n");
		exit(1);
	}

	int execPathStatus;
	if (__mldr_process_control_ready()) {
#if defined(__x86_64__)
		uint64_t archForPlane = mldr_load_results._32on64 ? 1u : 2u;
#else
		uint64_t archForPlane = 4u;
#endif
		execPathStatus = __mldr_process_control_request(DSERVER_PROCESS_CONTROL_OP_SET_EXECUTABLE_PATH,
			(uint64_t)(uintptr_t)filename, 0, strlen(filename), archForPlane);
	} else {
		execPathStatus = dserver_rpc_set_executable_path(filename, strlen(filename));
	}
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] seq=%u after-execpath pid=%d status=%d ready=%d image=%s\n", __mldr_diag_seq(), (int)getpid(), execPathStatus, __mldr_process_control_ready(), __mldr_diag_image(argv[0]));
	}
	if (execPathStatus < 0) {
		fprintf(stderr, "Failed to tell darlingserver about our executable path\n");
		exit(1);
	}


	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] seq=%u before-threadself pid=%d ready=%d image=%s\n", __mldr_diag_seq(), (int)getpid(), __mldr_process_control_ready(), __mldr_diag_image(argv[0]));
	}
	uint32_t main_thread_port = 0;
	if (__mldr_process_control_ready()) {
		// perf#30 (doc section 228): the trap's home is the management PLANE, not a datagram. It is asked here,
		// BEFORE __darling_thread_initialize_main consumes the name below, and at this point no server-side
		// Thread exists yet (the checkin that creates it is deferred until after the process-control
		// establishment), so the plane op registers the thread itself through Call::registerPeerForMessage and
		// answers the trap from the thread-parameterized duct-tape helper. The datagram below is kept ONLY for
		// the pre-page window, where there is no plane to publish on; there is deliberately no fallback after a
		// plane failure -- a failure here is fatal and loud, because falling back would hide a broken route.
#if defined(__x86_64__)
		uint64_t archForTrap = mldr_load_results._32on64 ? 1u : 2u;
#else
		uint64_t archForTrap = 4u;
#endif
		/* A BOUNDED RETRY, BECAUSE THIS REQUEST IS AN IDEMPOTENT READ AND ITS FAILURE KILLS THE BOOT (dar-rcnr).
		 * MEASURED: a run in ten died here with "[mldr-ctl] thread-self-bootstrap failed status=-2 port=0" followed
		 * by "Failed to get main thread port from the process control plane", which the launcher turns into
		 * "Rootless shellspawn did not become ready within 30000ms" -- so an unobservable race looked like a broken
		 * boot. -2 is the plane helper's OVERTAKEN REPLY: the request was published and answered, but by the time
		 * this thread read the slot the sequence belonged to another publisher (measured directly on the checkout
		 * path as pub=5 seen=17). Asking again is safe HERE and not in general: this operation reads the main
		 * thread's port for the calling thread and registers it if absent, so repeating it cannot undo anything,
		 * and the fatal path below still fires loudly if three attempts cannot answer. The general question --
		 * whether the plane should hand a reply to its own publisher instead of leaving it to a sequence
		 * comparison -- is dar-rcnr/dar-b5pe and is not settled by this retry. */
		int trapStatus = -1;
		struct dserver_process_control* trapPage = __mldr_process_control_page();
		for (int attempt = 1; attempt <= 3 && main_thread_port == 0; ++attempt) {
			trapStatus = __mldr_process_control_request(DSERVER_PROCESS_CONTROL_OP_THREAD_SELF_BOOTSTRAP,
				(uint64_t)(unsigned)gettid(), archForTrap, 0, 0);
			if (trapStatus == 0 && trapPage != NULL) {
				main_thread_port = (uint32_t)(trapPage->reply_payload[0] & 0xffffffffu);
			}
			if (main_thread_port == 0 && attempt < 3) {
				mldr_diagf("[mldr-ctl] thread-self-bootstrap retry attempt=%d status=%d\n", attempt, trapStatus);
			}
		}
		if (main_thread_port == 0) {
			mldr_diagf("[mldr-ctl] thread-self-bootstrap failed pid=%d tid=%d status=%d port=%u\n",
				(int)getpid(), (int)gettid(), trapStatus, main_thread_port);
			fprintf(stderr, "Failed to get main thread port from the process control plane\n");
			exit(1);
		}
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] thread-self-bootstrap plane pid=%d tid=%d port=%u\n",
				(int)getpid(), (int)gettid(), main_thread_port);
		}
	} else if (dserver_rpc_explicit_thread_self_trap(mldr_load_results.kernfd,
			&main_thread_port) < 0) {
		fprintf(stderr, "Failed to get main thread port from darlingserver\n");
		exit(1);
	}

	// perf#30: the lane is seeded AFTER the loader's own bootstrap writes and before the loaded image
	// starts. MEASURED (two variants, both RED): those writes must precede the FIRST lane attach --
	// publishing them on the lane (publish-only, and then with a transport acknowledgement) breaks the
	// workload. They are pre-attach process bootstrap, not a migration gap.
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] seq=%u after-threadself pid=%d ready=%d image=%s\n", __mldr_diag_seq(), (int)getpid(), __mldr_process_control_ready(), __mldr_diag_image(argv[0]));
	}
	{
		void* seeded = __mldr_ring_lane_seed();
		fprintf(stderr, "[mldr-seed] seeded pid=%d map=%p\n", (int)getpid(), seeded);
	}
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] seq=%u after-seed pid=%d ready=%d image=%s\n", __mldr_diag_seq(), (int)getpid(), __mldr_process_control_ready(), __mldr_diag_image(argv[0]));
	}

	if (__darling_thread_initialize_main((void*)mldr_load_results.stack_top,
			mldr_load_results.stack_size, main_thread_port) < 0) {
		fprintf(stderr, "Failed to initialize main thread TSD\n");
		exit(1);
	}
	__mldr_main_stack_top = (void*)mldr_load_results.stack_top;


	start_thread(&mldr_load_results);

	__builtin_unreachable();
}

void load(const char* path, cpu_type_t forced_arch, bool expect_dylinker, char** argv, struct load_results* lr)
{
	int fd;
	uint32_t magic;

	fd = open(path, O_RDONLY);
	if (fd == -1)
	{
		fprintf(stderr, "Cannot open %s: %s\n", path, strerror(errno));
		exit(1);
	}

	// We need to read argv[1] and detect whether it's a 32 or 64-bit application.
	// Then load the appropriate version of dyld from the fat file.
	// In case the to-be-executed executable contains both, we prefer the 64-bit version,
	// unless a special property has been passed to sys_posix_spawn() to force the 32-bit
	// version. See posix_spawnattr_setbinpref_np().

	if (read(fd, &magic, sizeof(magic)) != sizeof(magic))
	{
		fprintf(stderr, "Cannot read the file header of %s.\n", path);
		exit(1);
	}

	if (magic == MH_MAGIC_64 || magic == MH_CIGAM_64)
	{
#ifdef __x86_64__
		lseek(fd, 0, SEEK_SET);
		load64(fd, expect_dylinker, lr);
#else
		abort();
#endif
	}
	else if (magic == MH_MAGIC || magic == MH_CIGAM)
	{
#if !__x86_64__
		lseek(fd, 0, SEEK_SET);
		load32(fd, expect_dylinker, lr);
#else
		// Re-run self as mldr32
		reexec32(argv);
#endif
	}
	else if (magic == FAT_MAGIC || magic == FAT_CIGAM)
	{
		lseek(fd, 0, SEEK_SET);
		load_fat(fd, forced_arch, expect_dylinker, argv, lr);
	}
	else
	{
		fprintf(stderr, "Unknown file format: %s.\n", path);
		exit(1);
	}

	close(fd);
}

static void load_fat(int fd, cpu_type_t forced_arch, bool expect_dylinker, char** argv, struct load_results* lr) {
	struct fat_header fhdr;
	struct fat_arch best_arch = {0};
	int bpref_index = -1;

	best_arch.cputype = CPU_TYPE_ANY;

	if (read(fd, &fhdr, sizeof(fhdr)) != sizeof(fhdr))
	{
		fprintf(stderr, "Cannot read fat file header.\n");
		exit(1);
	}

	const bool swap = fhdr.magic == FAT_CIGAM;

#define SWAP32(x) x = __bswap_32(x)

	if (swap)
		SWAP32(fhdr.nfat_arch);

	uint32_t i;
	for (i = 0; i < fhdr.nfat_arch; i++)
	{
		struct fat_arch arch;

		if (read(fd, &arch, sizeof(arch)) != sizeof(arch))
		{
			fprintf(stderr, "Cannot read fat_arch header.\n");
			exit(1);
		}

		if (swap)
		{
			SWAP32(arch.cputype);
			SWAP32(arch.cpusubtype);
			SWAP32(arch.offset);
			SWAP32(arch.size);
			SWAP32(arch.align);
		}

		if (!forced_arch)
		{
			int j;
			for (j = 0; j < 4; j++)
			{
				if (lr->bprefs[j] && arch.cputype == lr->bprefs[j])
				{
					if (bpref_index == -1 || bpref_index > j)
					{
						best_arch = arch;
						bpref_index = j;
						break;
					}
				}
			}

			if (bpref_index == -1)
			{
#if defined(__x86_64__)
				if (arch.cputype == CPU_TYPE_X86_64)
					best_arch = arch;
				else if (best_arch.cputype == CPU_TYPE_ANY && arch.cputype == CPU_TYPE_X86)
					best_arch = arch;
#elif defined(__i386__)
				if (arch.cputype == CPU_TYPE_X86)
					best_arch = arch;
#elif defined (__aarch64__)
	#error TODO: arm
#else
	#error Unsupported CPU architecture
#endif
			}
		}
		else
		{
			if (arch.cputype == forced_arch)
				best_arch = arch;
		}
	}

	if (best_arch.cputype == CPU_TYPE_ANY)
	{
		fprintf(stderr, "No supported architecture found in fat binary.\n");
		exit(1);
	}

	if (lseek(fd, best_arch.offset, SEEK_SET) == -1)
	{
		fprintf(stderr, "Cannot seek to selected arch in fat binary.\n");
		exit(1);
	}

	if (best_arch.cputype & CPU_ARCH_ABI64) {
#ifdef __x86_64__
		load64(fd, expect_dylinker, lr);
#elif __aarch64__
	#error TODO: aarch64
#else
		abort();
#endif
	} else {
#if !__x86_64__
		load32(fd, expect_dylinker, lr);
#else
		// Re-run self as mldr32
		reexec32(argv);
#endif
	}
};

#ifdef __x86_64__
#define GEN_64BIT
#include "loader.c"
#include "stack.c"
#undef GEN_64BIT
#endif

#define GEN_32BIT
#include "loader.c"
#include "stack.c"
#undef GEN_32BIT

int native_prot(int prot)
{
	int protOut = 0;

	if (prot & VM_PROT_READ)
		protOut |= PROT_READ;
	if (prot & VM_PROT_WRITE)
		protOut |= PROT_WRITE;
	if (prot & VM_PROT_EXECUTE)
		protOut |= PROT_EXEC;

	return protOut;
}

static void reexec32(char** argv)
{
	char selfpath[1024];
	ssize_t len;

	len = readlink("/proc/self/exe", selfpath, sizeof(selfpath)-3);
	if (len == -1)
	{
		perror("Cannot readlink /proc/self/exe");
		abort();
	}

	selfpath[len] = '\0';
	strcat(selfpath, "32");

	execv(selfpath, argv);

	perror("Cannot re-execute as 32-bit process");
	abort();
}

// Given that there's no proper way of passing special parameters to the binary loader
// via execve(), we must do this via env variables
static void process_special_env(struct load_results* lr) {
	const char* str;
	static char root_path[4096];

	lr->bprefs[0] = lr->bprefs[1] = lr->bprefs[2] = lr->bprefs[3] = 0;
	str = getenv("__mldr_bprefs");

	if (str != NULL) {
		sscanf(str, "%x,%x,%x,%x", &lr->bprefs[0], &lr->bprefs[1], &lr->bprefs[2], &lr->bprefs[3]);
	}

	str = getenv("__mldr_sockpath");

	if (str != NULL) {
		if (strlen(str) > sizeof(__dserver_socket_address_data.sun_path) - 1) {
			fprintf(stderr, "darlingserver socket path is too long\n");
			exit(1);
		}
		strncpy(__dserver_socket_address_data.sun_path, str, sizeof(__dserver_socket_address_data.sun_path) - 1);
		__dserver_socket_address_data.sun_path[sizeof(__dserver_socket_address_data.sun_path) - 1] = '\0';

		lr->socket_path = __dserver_socket_address_data.sun_path;
	}

	lr->lifetime_pipe = -1;
	str = getenv("__mldr_lifetime_pipe");

	if (str != NULL) {
		sscanf(str, "%i", &lr->lifetime_pipe);
	}

	str = getenv("__mldr_DYLD_ROOT_PATH");
	if (str == NULL) {
		// perf#30 (doc section 202): the loader REWRITES the same variable for the guest -- it strips the
		// `__mldr_` prefix in place a few lines above this function's caller, so the value survives in the
		// environment under the guest's name while the loader's own name is gone. MEASURED: the second image
		// read null and loaded the unprefixed guest path for /usr/lib/dyld, while the first image (which still
		// had the loader's name) worked. Both names are therefore read, and the guest's is the one that
		// survives.
		str = getenv("DYLD_ROOT_PATH");
	}

	if (str != NULL && lr->root_path == NULL) {
		strncpy(root_path, str, sizeof(root_path) - 1);
		root_path[sizeof(root_path) - 1] = '\0';
		lr->root_path = root_path;
		lr->root_path_length = strlen(lr->root_path);
	}

	// THE ROOT MUST SURVIVE INTO EVERY IMAGE, NOT ONLY THE FIRST. MEASURED: the loader renames its private
	// __mldr_DYLD_ROOT_PATH to the guest's DYLD_ROOT_PATH before the image starts (the block that strips the
	// `__mldr_` prefix), dyld then CONSUMES that name, and the next image of the process -- and every child it
	// spawns -- reads neither name and builds the unprefixed path `/usr/lib/dyld`, which does not exist. The
	// observable failure is 'Cannot open /usr/lib/dyld' with no root in front of it, and the boot stops there.
	// Re-publishing BOTH names here gives every later image the root through the name it looks for, and
	// vchroot's rename becomes an optimisation instead of the only thing holding the invariant.
	if (lr->root_path != NULL) {
		// NARROWED (measured): only the PRIVATE name is re-published. Setting the public DYLD_ROOT_PATH here
		// as well changed the failure instead of curing it -- the guest log dropped from 673 lines to five and
		// the boot stopped one stage earlier -- so the public name is left to the loader's own rewrite, which
		// has always produced it from the private one for the image it is about to run.
		setenv("__mldr_DYLD_ROOT_PATH", lr->root_path, 1);
	}

	str = getenv("__mldr_rootless_pid1");
	if (str != NULL) {
		fprintf(stderr,
			"obsolete __mldr_rootless_pid1 marker crossed the typed runtime boundary\n");
		exit(1);
	}
	if (getenv("DARLING_ROOTLESS") != NULL ||
		getenv("DARLING_NOOVERLAYFS") != NULL ||
		getenv("DARLING_EUNION") != NULL) {
		fprintf(stderr,
			"legacy runtime flags crossed the launcher compatibility boundary\n");
		exit(1);
	}

	str = getenv("__mldr_runtime_mode");
	if (str != NULL) {
		lr->init_runtime_mode = darling_runtime_mode_parse(str);
		if (lr->init_runtime_mode == DARLING_RUNTIME_MODE_INVALID ||
			(darling_runtime_mode_uses_eunion(lr->init_runtime_mode) &&
				DARLING_RUNTIME_EUNION_CAPABLE == 0)) {
			fprintf(stderr, "invalid typed runtime mode for launchd bootstrap\n");
			exit(1);
		}
	}
};

static void unset_special_env() {
	unsetenv("__mldr_bprefs");
	unsetenv("__mldr_sockpath");
	unsetenv("__mldr_lifetime_pipe");
	unsetenv("__mldr_rootless_pid1");
	unsetenv("__mldr_runtime_mode");
};

typedef struct socket_bitmap {
	pthread_mutex_t mutex;
	/**
	 * This is always next lowest available index.
	 * If this is equal to #bit_length, then the bitmap is full.
	 */
	size_t next_index;
	uint8_t* bits;
	size_t bit_length;
	int highest;
} socket_bitmap_t;

static socket_bitmap_t socket_bitmap = {
	.mutex = PTHREAD_MUTEX_INITIALIZER,
	.next_index = 0,
	.bits = NULL,
	.bit_length = 0,
	.highest = -1,
};

// Shared by dyld and libsystem_kernel through elfcalls. The loader owns these
// descriptors; guest image-local lane tables own only their mappings.
// perf#28 (ONE doorbell): the canonical process-wide Ring wake descriptor, or -1. Recorded inside the
// same registry/lock as the per-lane fds so fork preparation covers it, but tracked separately because
// its lifetime is the PROCESS's, not a lane's: a lane release must never close it, and the whole
// transport keeps using it until the process exits.
// perf#30 (doc section 179): monotonic milliseconds, so a bootstrap position can be chosen against the
// launcher's deadline instead of against a pass number. The loader is a Linux binary, so this is the host clock.
// perf#30 (doc section 180): the loader's boot diagnostics go to fd 2 AND fd 1, because by the time the second
// incarnation is loading, fd 2 is the launchd image's /dev/null (doc section 150) -- and a diagnostic the observer
// cannot read is the instrument-that-cannot-answer class this work keeps recording. Raw `write` through the syscall
// boundary, so a closed or replaced fd 2 costs nothing, nothing is buffered, and no stdio state is touched.
// MEASURED: writing the same diagnostic to BOTH fd 2 and fd 1 made the guest's progress depend on whether the
// SERVER was logging. The reason is the instrument, not the transport: guest and server share one pipe for the run
// log, the server's logging fills it, and a blocking raw `write` from inside the publish path then parks the loader
// exactly where the observer sees "the second incarnation stops before `planeloop BEGIN`". A diagnostic that can
// block is a diagnostic that changes what it measures.
//
// The sink is therefore a FILE when the harness names one (MLDR_DIAG_LOG): regular-file writes do not block on a
// pipe, and the file survives the launchd image installing /dev/null as its stderr. With no path named the
// behaviour is the earlier one -- fd 2 -- so nothing that ran before changes.
// MEASURED (doc section 188): `vsnprintf` was itself the blocker. The loop's progress stopped exactly at the
// print that follows `iter=21000`, the process was observed parked in a futex whose pc is inside libc, and the
// loader forks while other threads exist -- which leaves libc locks held in the child. A diagnostic that takes a
// libc lock is a diagnostic that can deadlock the thing it measures, and this file's own rule for pre-runtime code
// already says so: raw syscalls, no libc. So the formatting is done here, into a caller-provided buffer, with no
// stdio, no locale and no lock.
static char* mldr_diag_append(char* out, char* end, const char* text, int len) {
	for (int i = 0; i < len && out < end && text[i] != '\0'; ++i) {
		*out++ = text[i];
	}
	return out;
}

static char* mldr_diag_ulong(char* out, char* end, unsigned long long v, int base, int width, int pad_zero) {
	char tmp[24];
	int n = 0;
	if (v == 0) {
		tmp[n++] = '0';
	}
	while (v != 0 && n < (int)sizeof(tmp)) {
		unsigned d = (unsigned)(v % (unsigned long long)base);
		tmp[n++] = (char)(d < 10 ? ('0' + d) : ('a' + (d - 10)));
		v /= (unsigned long long)base;
	}
	while (n < width && n < (int)sizeof(tmp)) {
		tmp[n++] = pad_zero ? '0' : ' ';
	}
	while (n > 0 && out < end) {
		*out++ = tmp[--n];
	}
	return out;
}

static void mldr_diagf(const char* fmt, ...) {
	char buf[768];
	char* out = buf;
	char* end = buf + sizeof(buf) - 1;
	va_list ap;
	va_start(ap, fmt);
	for (const char* f = fmt; *f != '\0' && out < end; ++f) {
		if (*f != '%') {
			*out++ = *f;
			continue;
		}
		++f;
		int pad = 0;
		while (*f == '0') {
			pad = 1;
			++f;
		}
		int width = 0;
		while (*f >= '0' && *f <= '9') {
			width = width * 10 + (*f - '0');
			++f;
		}
		int lcount = 0;
		while (*f == 'l') {
			++lcount;
			++f;
		}
		if (*f == 'z') { ++f; }   // size_t: unsigned long on every target this runs on
		switch (*f) {
			case 's': out = mldr_diag_append(out, end, va_arg(ap, const char*), 4096); break;
			case 'c': *out++ = (char)va_arg(ap, int); break;
			case 'd': case 'i': {
				long long v = (lcount >= 2) ? va_arg(ap, long long) : (lcount == 1 ? va_arg(ap, long) : va_arg(ap, int));
				if (v < 0) { *out++ = '-'; v = -v; }
				out = mldr_diag_ulong(out, end, (unsigned long long)v, 10, width, pad);
				break;
			}
			case 'u': case 'x': case 'X': {
				unsigned long long v = (lcount >= 2) ? va_arg(ap, unsigned long long)
					: (lcount == 1 ? va_arg(ap, unsigned long) : va_arg(ap, unsigned int));
				out = mldr_diag_ulong(out, end, v, (*f == 'u') ? 10 : 16, width, pad);
				break;
			}
			case 'p': {
				out = mldr_diag_append(out, end, "0x", 2);
				out = mldr_diag_ulong(out, end, (unsigned long long)(unsigned long)va_arg(ap, void*), 16, 0, 0);
				break;
			}
			case '%': *out++ = '%'; break;
			default: *out++ = '%'; *out++ = *f; break;
		}
	}
	va_end(ap);
	*out = '\0';
	int n = (int)(out - buf);
	if (mldr_diag_fd < 0) {
		const char* path = getenv("MLDR_DIAG_LOG");
		mldr_diag_fd = 2;
		if (path != NULL && *path != '\0') {
			int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_NONBLOCK, 0644);
			if (fd >= 0) {
				mldr_diag_fd = fd;
			}
		}
	}
	long a = 1, d = mldr_diag_fd, s = (long)buf, l = n;
	__asm__ volatile("syscall" : "+a"(a), "+D"(d), "+S"(s), "+d"(l) : : "rcx", "r11", "memory");
}

static unsigned long long mldr_diag_now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (unsigned long long)ts.tv_sec * 1000ull + (unsigned long long)(ts.tv_nsec / 1000000);
}

static int ring_doorbell_fd = -1;
// perf#30 STAGE 2: the descriptor the SEED adopted for the main-thread lane. Kept separately because the
// fork-child reset above clears ring_doorbell_fd even though the doorbell's lifetime is the PROCESS's.
static int ring_seed_doorbell_fd = -1;

// perf#30 (PROCESS-GLOBAL LANE DIRECTORY): ONE logical Ring lane incarnation per Linux host tid, seen
// by every guest image. The images of a process share one Linux address space and the loader stays
// resident, so the directory needs no new shared-memory object: it is ordinary loader BSS whose ADDRESS
// is handed to every image through one appended elfcall. The record is transport-neutral (no emulation
// types) and is a CONTROL structure only -- the payload lane stays a per-thread SPSC ring.
//
// State machine: 0 EMPTY -> 1 ATTACHING (one image arbitrates) -> 2 ACTIVE. The publishing image stores
// mapping/mapping_size/generation/slot BEFORE the ACTIVE store (release), so an adopting image that
// observes ACTIVE also observes a fully initialized mapping.
enum {
	MLDR_RING_LANE_EMPTY = 0,
	MLDR_RING_LANE_ATTACHING = 1,
	MLDR_RING_LANE_ACTIVE = 2,
};

struct mldr_ring_lane_record {
	volatile int32_t  host_tid;      // 0 == free
	volatile uint32_t state;
	volatile uint32_t generation;
	volatile uint32_t slot_index;
	volatile int32_t  owner_image;   // diagnostic: 0 mldr, 1 dyld, 2 kernel
	volatile void*    mapping;       // guest VA, valid in EVERY image of this process
	volatile uint64_t mapping_size;
	volatile uint32_t next_seq;      // the incarnation's wire sequence (shared by every image)
	volatile uint32_t creator_image; // diagnostic: who created it
};

#define MLDR_RING_LANE_SLOTS 1024
// The runtime's GR_SLOT_SIZE/GR_SLOT_COUNT. These are part of the control-block contract with the server,
// so they must match emulation/.../dserver-ring.c exactly.
// (the transport switch is defined at the top of this file, before the first include of the header)
#include <limits.h>
#include <darlingserver/rpc-supplement.h> // dserver_ring_shm_t/dserver_ring_t + the SHARED SPSC helpers
#include <sys/syscall.h>

#define MLDR_GR_SLOT_SIZE 128u
#define MLDR_GR_SLOT_COUNT 8u
static struct mldr_ring_lane_record mldr_ring_lanes[MLDR_RING_LANE_SLOTS];

void* __mldr_ring_lane_registry(void) {
	return (void*)&mldr_ring_lanes[0];
}

int __mldr_ring_lane_slots(void) {
	return MLDR_RING_LANE_SLOTS;
}

static int* ring_fds;
static size_t ring_fd_count;
static size_t ring_fd_capacity;
static __thread sigset_t fork_signal_mask;

static int socket_bitmap_get_locked(socket_bitmap_t* bitmap) {
	int fd = -1;
	bool updated = false;

	if (bitmap->highest == -1) {
		// we need to initialize this bitmap
		struct rlimit limit;

		if (getrlimit(RLIMIT_NOFILE, &limit) < 0) {
			goto out;
		}

		if (limit.rlim_cur == RLIM_INFINITY) {
			// just default to 1024
			limit.rlim_cur = 1024;
		}

		bitmap->highest = limit.rlim_cur - 1;
	}

	// Never reserve a standard descriptor, even with a reduced host limit.
	if (bitmap->highest < 3 || bitmap->next_index > (size_t)(bitmap->highest - 3)) {
		goto out;
	}

	if (bitmap->next_index >= bitmap->bit_length) {
		// we need to grow the bitmap

		if ((bitmap->bit_length % 8) == 0) {
			// we need to allocate an additional byte

			void* ptr = realloc(bitmap->bits, (bitmap->bit_length / 8) + 1);
			if (!ptr) {
				goto out;
			}

			bitmap->bits = ptr;

			bitmap->bits[bitmap->bit_length / 8] = 0;
		} else {
			// we just need to increment the bit length
		}

		++bitmap->bit_length;
	}

	fd = bitmap->highest - bitmap->next_index;

	bitmap->bits[bitmap->next_index / 8] |= 1 << (bitmap->next_index % 8);

	// update the next available index
	for (size_t i = bitmap->next_index + 1; i < bitmap->bit_length; ++i) {
		size_t byte = i / 8;
		uint8_t bit = i % 8;

		if (bit == 0) {
			// check the entire byte at once so we can avoid unnecessary iteration
			if (bitmap->bits[byte] == 0xff) {
				// this byte is full, skip it
				i += 7;
				continue;
			}
		}

		if ((bitmap->bits[byte] & (1 << bit)) == 0) {
			// this index is unused
			bitmap->next_index = i;
			updated = true;
			break;
		}
	}

	if (!updated) {
		// all of our entries are currently in-use
		bitmap->next_index = bitmap->bit_length;
	}

out:
	return fd;
};

static int socket_bitmap_get(socket_bitmap_t* bitmap) {
	sigset_t saved;
	mldr_block_async_signals(&saved);
	pthread_mutex_lock(&bitmap->mutex);
	int fd = socket_bitmap_get_locked(bitmap);
	pthread_mutex_unlock(&bitmap->mutex);
	mldr_restore_signals(&saved);
	return fd;
}

static void socket_bitmap_put_locked(socket_bitmap_t* bitmap, int socket) {
	size_t index;

	index = bitmap->highest - socket;

	bitmap->bits[index / 8] &= ~(1 << (index % 8));

	if (index < bitmap->next_index) {
		bitmap->next_index = index;
	}

	if (index == bitmap->bit_length - 1) {
		// we can shrink the bitmap
		size_t old_byte_size = (bitmap->bit_length + 7) / 8;
		size_t new_byte_size = old_byte_size;

		while (bitmap->bit_length > 0) {
			size_t index = bitmap->bit_length - 1;

			if ((bitmap->bit_length % 8) == 0) {
				// check the entire byte at once to avoid unnecessary iteration
				if (bitmap->bits[(bitmap->bit_length / 8) - 1] == 0) {
					// remove this entire byte
					bitmap->bit_length -= 8;
					continue;
				}
			}

			if ((bitmap->bits[index / 8] & (1 << (index % 8))) == 0) {
				// this bit is in-use, so we can't shrink any further
				break;
			}

			--bitmap->bit_length;
		}

		new_byte_size = (bitmap->bit_length + 7) / 8;

		if (old_byte_size != new_byte_size) {
			// we can free one or more bytes from the bitmap
			void* ptr = realloc(bitmap->bits, new_byte_size);
			if (!ptr) {
				goto out;
			}

			bitmap->bits = ptr;
		}
	}

out:
	return;
};

static void socket_bitmap_put(socket_bitmap_t* bitmap, int socket) {
	sigset_t saved;
	mldr_block_async_signals(&saved);
	pthread_mutex_lock(&bitmap->mutex);
	socket_bitmap_put_locked(bitmap, socket);
	pthread_mutex_unlock(&bitmap->mutex);
	mldr_restore_signals(&saved);
}

int __mldr_adopt_ring_fd(int source) {
	int result = -1;
	sigset_t saved;
	mldr_block_async_signals(&saved);
	pthread_mutex_lock(&socket_bitmap.mutex);
	if (ring_fd_count == ring_fd_capacity) {
		size_t capacity = ring_fd_capacity ? ring_fd_capacity * 2 : 16;
		if (capacity < ring_fd_capacity || capacity > SIZE_MAX / sizeof(*ring_fds)) {
			goto out;
		}
		int* resized = realloc(ring_fds, capacity * sizeof(*ring_fds));
		if (!resized) {
			goto out;
		}
		ring_fds = resized;
		ring_fd_capacity = capacity;
	}
	int reserved = socket_bitmap_get_locked(&socket_bitmap);
	if (reserved < 0) {
		goto out;
	}
	// Unlike dup2, F_DUPFD_CLOEXEC cannot overwrite an application descriptor
	// that occupies or races to acquire our preferred number. A collision is
	// a clean attach failure; the caller retains and closes its source FD.
	int duplicate = fcntl(source, F_DUPFD_CLOEXEC, reserved);
	if (duplicate != reserved) {
		if (duplicate >= 0) {
			close(duplicate);
		}
		socket_bitmap_put_locked(&socket_bitmap, reserved);
		goto out;
	}
	ring_fds[ring_fd_count++] = duplicate;
	result = duplicate;
out:
	pthread_mutex_unlock(&socket_bitmap.mutex);
	mldr_restore_signals(&saved);
	return result;
}

// perf#28 (ONE doorbell): adopt the process-wide Ring wake descriptor. The server duplicates its
// single doorbell on EVERY lane attach (the attach ABI still answers with a usable wake fd), so this
// runs once per lane and must be idempotent: the first call adopts + records, every later call closes
// the duplicate it was handed and returns the SAME descriptor. That is what makes the guest's Ring
// wake-fd count one per Linux process instead of one per lane -- and because the recording lives in
// the shared loader, one per process rather than one per guest image.
// perf#30 STAGE 2 (mldr is the FIRST OWNER of the process-global lane). The loader runs before either
// guest image, on the process's main thread -- the SAME host tid the kernel image will later use -- so
// creating and publishing the incarnation HERE is what makes cross-image adoption happen NATURALLY: a
// later image finds an ACTIVE record for its own tid and borrows it instead of attaching its own.
//
// WHERE this is called from matters and is the whole lesson of the first attempt: NOT from a generated
// RPC hook (that runs INSIDE a wrapper, so calling another generated RPC from there re-enters the
// loader's own RPC machinery and breaks boot). It is called from the main bootstrap, immediately after
// two generated RPCs have completed -- outside every wrapper, with the UDS client proven live and the
// loaded image's code not yet running.
//
// The geometry below mirrors the runtime's per-thread attach field for field, but the RING is not
// re-implemented: the producer/consumer/ordering helpers are the SHARED inline functions from
// <darlingserver/rpc-supplement.h>, and the negotiation is the SAME generated `dserver_rpc_ring_attach`
// client the runtime uses.
// perf#30 FD-COURIER (PRODUCT, dar-gwn.7.7.5): the abstract address of the ONE process-scoped
// SCM_RIGHTS descriptor channel. The loader owns it for the same reason it owns the lane directory:
// process-level transport state belongs to the image that spans the whole process. Every other image
// reaches the address through the elfcalls table instead of re-deriving the prefix, and the server
// derives the same name from the same prefix by replacing its metrics service prefix -- so neither
// side ever sends the name.
// perf#30 PROCESS-CONTROL PLANE (loader side): the process-shared management page. The loader creates
// it, initializes it and hands the backing descriptor to the server over the process courier -- the page
// carries SEMANTIC commands (bootstrap/teardown ones), which is exactly what the courier must NOT carry,
// so the two channels stay honest: courier = descriptors, control page = rare commands, lanes = hot RPC.
static struct dserver_process_control* g_process_control_page = NULL;
static int __mldr_process_control_memfd = -1;

// The loader's own courier send (the images' hook is a stub; the loader owns the connection).
static int __mldr_fd_courier_send_envelope(int fd, uint64_t token, uint32_t kind) {
	int sock = __mldr_fd_courier_socket();
	if (sock < 0 || fd < 0) {
		return -1;
	}
	struct dserver_fd_courier_message envelope;
	envelope.process_generation = __mldr_process_generation();
	envelope.token = token;
	envelope.kind = kind;
	envelope.fd_count = 1;
	char control[CMSG_SPACE(sizeof(int))];
	memset(control, 0, sizeof(control));
	struct iovec iov = { &envelope, sizeof(envelope) };
	struct msghdr msg;
	memset(&msg, 0, sizeof(msg));
	msg.msg_iov = &iov;
	msg.msg_iovlen = 1;
	msg.msg_control = control;
	msg.msg_controllen = sizeof(control);
	struct cmsghdr* cmsg = CMSG_FIRSTHDR(&msg);
	cmsg->cmsg_level = SOL_SOCKET;
	cmsg->cmsg_type = SCM_RIGHTS;
	cmsg->cmsg_len = CMSG_LEN(sizeof(int));
	memcpy(CMSG_DATA(cmsg), &fd, sizeof(int));
	// perf#30 R1 COURIER PURITY: the courier's OWN traffic is recorded too. The generated-RPC hook above does NOT see
	// it (the courier builds its own message), so without this the census would call a blind spot "0 fd-bearing
	// packets". Same four facts: direction, SCM_RIGHTS presence, fd count, payload bytes.
	{
		static unsigned g_courier_send_n = 0;
		if (__atomic_fetch_add(&g_courier_send_n, 1, __ATOMIC_RELAXED) < 4096) {
			int fdcnt = 0; int scm = 0;
			for (struct cmsghdr* c = CMSG_FIRSTHDR(&msg); c != NULL; c = CMSG_NXTHDR(&msg, c)) {
				if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
					scm = 1; fdcnt += (int)((c->cmsg_len - CMSG_LEN(0)) / sizeof(int));
				}
			}
			size_t bytes = 0;
			for (size_t i = 0; i < (size_t)msg.msg_iovlen; ++i) { bytes += msg.msg_iov[i].iov_len; }
			fprintf(stderr, "[courier-send] n=%u scm=%d fdcnt=%d payload=%zu direction=guest->server\n",
				g_courier_send_n, scm, fdcnt, bytes);
			fflush(stderr);
		}
	}
	ssize_t sent = sendmsg(sock, &msg, 0);
	return (sent == (ssize_t)sizeof(envelope)) ? 0 : -1;
}

void* __mldr_process_control_page(void); // the accessor every image reaches through the elfcalls table

static uint64_t __mldr_fd_courier_next_token = 0;

// Send one descriptor and return the token that names it (0 on failure, which makes the caller keep the
// whole operation on the legacy transport -- never a split).
uint64_t __mldr_fd_courier_send_token(int fd, uint32_t kind) {
	// perf#30 IDENTITY: same reasoning as the guest image's sender -- the pid is part of the token, because a
	// token that only mixes generation and a per-process counter collides across processes.
	uint64_t token = (__mldr_process_generation() * 0x9E3779B97F4A7C15ull)
		^ ((uint64_t)(unsigned)getpid() * 0xC2B2AE3D27D4EB4Full)
		^ (++__mldr_fd_courier_next_token);
	if (__mldr_fd_courier_send_envelope(fd, token, kind) != 0) {
		return 0;
	}
	return token;
}

int __mldr_process_control_create(void) {
	if (__mldr_process_control_memfd >= 0) {
		// perf#30 (doc section 187): WHICH page. Two execve images share one pid and one address space, so
		// "the page" is not a single thing unless this says so -- and the whole stale-page question is whether
		// the loader and the guest image are holding the same one.
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] create-REUSE pid=%d page=%p memfd=%d\n", (int)getpid(),
				(void*)g_process_control_page, __mldr_process_control_memfd);
		}
		return 0;
	}
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] create-NEW pid=%d\n", (int)getpid());
	}
	size_t size = sizeof(struct dserver_process_control);
	int memfd = (int)syscall(SYS_memfd_create, "dserver-ctl", 0x1u);
	if (memfd < 0) {
		return -1;
	}
	if (ftruncate(memfd, (off_t)size) != 0) {
		close(memfd);
		return -1;
	}
	void* map = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, 0);
	if (map == MAP_FAILED) {
		close(memfd);
		return -1;
	}
	struct dserver_process_control* page = (struct dserver_process_control*)map;
	memset(page, 0, size);
	page->version = DSERVER_PROCESS_CONTROL_VERSION;
	page->request_state = DSERVER_PROCESS_CONTROL_IDLE;
	__atomic_store_n(&page->reply_state, DSERVER_PROCESS_CONTROL_IDLE, __ATOMIC_RELEASE);
	if (__mldr_fd_courier_send_envelope(memfd, 0x1000u, DSERVER_FD_COURIER_KIND_PROCESS_CONTROL) != 0) {
		munmap(map, size);
		close(memfd);
		return -1;
	}
	page->owner_pid = (int32_t)getpid();
	g_process_control_page = page;
	__mldr_process_control_memfd = memfd;
	// perf#30 (violation A, directive sections 2-4): the doorbell must exist BEFORE the first management request.
	// The server sends it while it registers this very region (addressed to the loader connection), and this bounded
	// attempt is what takes it into the loader's hands before the PING below. Bounded on purpose: a server that does
	// not answer must not stall the boot -- the loader then publishes without a doorbell exactly as before, and the
	// channel census says so instead of the failure being invisible. Reachable by env OR file flag, for the same
	// reason as the server side.
	{
		int attempts = 0;
		int adoptedEarly = 0;
		for (attempts = 0; attempts < 3000; ++attempts) {
			int courierSock = __mldr_fd_courier_socket();
			if (courierSock < 0 || __mldr_ring_doorbell(-1) >= 0) {
				break;
			}
			if (__mldr_fd_courier_recv_once(courierSock) == 0) {
				struct timespec bellTs = { 0, 1000000 };
				nanosleep(&bellTs, NULL);
			}
		}
		adoptedEarly = (__mldr_ring_doorbell(-1) >= 0);
		// The bound is a BOOTSTRAP wait, not an idle poll: the successful case breaks out on the first millisecond, so
		// it costs nothing when the descriptor is there. MEASURED why it is larger than it looks: the server's own log
		// timestamps its first send at ~0.24 s and ~0.5 s after the page, while the previous 200 x 1 ms window expired
		// just before it -- a race, not a missing send (64 sends in the server log, 0 drops and 0 adoptions in the
		// loader's window). 3 s bounds the wait; the doorbell itself is still the only thing that wakes the server.
		// DECISIVE and GUEST-SIDE on purpose. The server's own diagnostics do not appear in the run log at all
		// (measured), so "did the server send it in time" can only be answered here: this line separates "the server
		// had not sent yet within the window" from "the descriptor never arrived at any point".
		fprintf(stderr, "[plane-doorbell] drain attempts=%d adopted=%d\n", attempts, adoptedEarly);
		fflush(stderr);
	}
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] page t=%llu pid=%d size=%zu sent=1 page=%p memfd=%d\n",
			mldr_diag_now_ms(), (int)getpid(), size, (void*)page, memfd);
	}
	return 0;
}

// The process-global accessor: create the page on first use in ANY image (the first caller is the
// loader's bootstrap) and hand the SAME page to every later caller. This is what keeps one page per
// process while images keep their own statics.
// perf#30 PHASE-0: TRANSPORT readiness, published by the server when it maps the region -- a fact about
// the transport, not about any Process or Thread. The wait is bounded so a server that never maps the page
// degrades to the legacy path instead of hanging the boot.
int __mldr_process_control_ready(void) {
	if (g_process_control_page == NULL) {
		return 0;
	}
	return __atomic_load_n(&g_process_control_page->transport_ready, __ATOMIC_ACQUIRE) != 0;
}

int __mldr_process_control_wait_ready(int timeout_ms) {
	if (g_process_control_page == NULL) {
		return 0;
	}
	for (int elapsed = 0; elapsed < timeout_ms; ++elapsed) {
		if (__atomic_load_n(&g_process_control_page->transport_ready, __ATOMIC_ACQUIRE) != 0) {
			return 1;
		}
		struct timespec ts;
		ts.tv_sec = 0;
		ts.tv_nsec = 1000000;
		nanosleep(&ts, NULL);
	}
	return __atomic_load_n(&g_process_control_page->transport_ready, __ATOMIC_ACQUIRE) != 0;
}

static struct dserver_process_control* mldr_process_control_page_readonly(void) {
	return g_process_control_page;
}

void* __mldr_process_control_page(void) {
	// A page belongs to ONE process incarnation. MEASURED: a forked child inherits the parent's page pointer
	// (it is a static in the image), so without this check the child published into the PARENT's page and the
	// server answered there -- the child's caller then waited for a reply that was never addressed to it,
	// which is the third-attach hang. A page whose owner is not this process is abandoned here (its mapping
	// stays with the process that owns it) and a fresh one is created for this incarnation.
	if (g_process_control_page != NULL && g_process_control_page->owner_pid != (int32_t)getpid()) {
		g_process_control_page = NULL;
		__mldr_process_control_memfd = -1;
	}
	if (g_process_control_page == NULL) {
		if (__mldr_process_control_create() != 0) {
			return NULL;
		}
	}
	return g_process_control_page;
}

// One request through the page: publish, wait for the server's answer on the same page, read the status.
// The wait is a bounded spin plus a futex sleep -- never a datagram, never a lane.
static int __mldr_process_control_request_once(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3) {
	if (g_process_control_page == NULL) {
		return -1;
	}
	struct dserver_process_control* page = g_process_control_page;
	static uint32_t seq = 0;
	// perf#30 MAILBOX CLAIM: ONE outstanding request per page is the model, so the slot is CLAIMED with a
	// compare-exchange instead of published over. MEASURED: this function is the publisher for every op
	// except the guest attach, and publishing over another request overwrites `request_seq` -- the server
	// then answers a sequence the waiting caller is not holding, and that caller waits forever. That is
	// exactly how a page-route attach lost its answer to a concurrent kqchan request and wedged the boot.
	if (mldr_diag_on()) {
		// perf#30 (doc section 192): PUBLISH THE PAGE ADDRESS where an outside observer can read it. A guest that
		// is blocked in its first wait cannot print, so "did the server's completion reach this page" has to be
		// answered by reading the page from outside -- and the address cannot be guessed across runs (ASLR).
		{
			const char* hint = getenv("MLDR_PAGE_HINT");
			if (hint != NULL && *hint != '\0') {
				char hb[64];
				int n = 0;
				unsigned long long v = (unsigned long long)(unsigned long)page;
				char tmp[20];
				int tn = 0;
				if (v == 0) { tmp[tn++] = '0'; }
				while (v) { unsigned d = (unsigned)(v & 0xf); tmp[tn++] = (char)(d < 10 ? '0' + d : 'a' + (d - 10)); v >>= 4; }
				hb[n++] = '0';
				hb[n++] = 'x';
				while (tn > 0) { hb[n++] = tmp[--tn]; }
				hb[n++] = '\n';
				int fd = open(hint, O_WRONLY | O_CREAT | O_TRUNC | O_NONBLOCK, 0644);
				if (fd >= 0) {
					long a = 1, d2 = fd, sp3 = (long)hb, l2 = n;
					__asm__ volatile("syscall" : "+a"(a), "+D"(d2), "+S"(sp3), "+d"(l2) : : "rcx", "r11", "memory");
					close(fd);
				}
			}
		}
		// (dev, ino) of the memfd, so the page this side is about to use can be compared with the region the
		// server reports IN THE SAME RUN -- an address cannot cross address spaces and an inode number alone is
		// unique only within a filesystem (doc section 181).
		struct stat st_;
		unsigned long long dev_ = 0, ino_ = 0;
		if (__mldr_process_control_memfd >= 0 && fstat(__mldr_process_control_memfd, &st_) == 0) {
			dev_ = (unsigned long long)st_.st_dev;
			ino_ = (unsigned long long)st_.st_ino;
		}
		mldr_diagf("[mldr-ctl] request pid=%d op=%u page=%p memfd=%d global=%p dev=%llu ino=%llu\n",
			(int)getpid(), op, (void*)page, __mldr_process_control_memfd, (void*)g_process_control_page, dev_, ino_);
	}
	// perf#30 (doc section 195): a slot left at DONE by the SERVER's completion store (which races this side's
	// RELEASE and can therefore land after it) is CLAIMABLE. MEASURED: with only IDLE claimable, one lost race
	// wedged the slot at DONE forever, every later request was refused with -3, and the guest fell back to the
	// datagram route -- seen as `seq=3 after-dyld status=-3` plus "Failed to tell darlingserver about our dyld
	// info". The publisher reads its ANSWER from reply_state, never from this flag, so reusing a completed slot
	// is exactly what "the publisher owns the slot until it has read its answer" already means.
	int slot = 0;
	for (int t = 0; t < 2000 && !slot; ++t) {
		uint32_t expect = (t % 2 == 0) ? DSERVER_PROCESS_CONTROL_IDLE : DSERVER_PROCESS_CONTROL_DONE;
		if (__atomic_compare_exchange_n(&page->request_state, &expect,
		        DSERVER_PROCESS_CONTROL_PENDING, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
			slot = 1;
			break;
		}
		struct timespec ts = {0, 1000000L};
		nanosleep(&ts, NULL);
	}
	if (!slot) {
		// perf#30 (doc section 232): the loader's own refusal had NO diagnostic either. A -3 here means another
		// request of this process is holding the slot, and that is the fact a stalled bootstrap needs: the op and
		// sequence it is holding are printed with the state, gated like every other loader diagnostic.
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] plane-noslot op=%u state=%u holder_op=%u holder_seq=%u pid=%d\n", op,
				__atomic_load_n(&page->request_state, __ATOMIC_ACQUIRE),
				__atomic_load_n(&page->request_op, __ATOMIC_ACQUIRE),
				__atomic_load_n(&page->request_seq, __ATOMIC_ACQUIRE), (int)getpid());
		}
		return -3;  // no mailbox slot: the caller falls back to its datagram route
	}
	// ATOMIC (doc section 70): the shared publisher is called from several threads of one process, and a
	// plain ++ can hand two of them the same sequence -- the server then answers one, and the other refuses
	// the result and repeats its operation on the datagram. MEASURED: 19 of 19 socket-creating threads had
	// their page request serviced with status 0, which leaves this collision as the remaining cause.
	uint32_t mine = __atomic_add_fetch(&seq, 1, __ATOMIC_RELAXED);
	__atomic_store_n(&page->reply_state, DSERVER_PROCESS_CONTROL_IDLE, __ATOMIC_RELEASE);
	page->reply_payload[0] = 0;   // doc 91: never inherit an earlier request's answer
	page->reply_payload[1] = 0;
	page->request_op = op;
	page->request_seq = mine;
	page->request_payload[0] = p0;
	page->request_payload[1] = p1;
	page->request_payload[2] = p2;
	page->request_payload[3] = p3;
	// Wake the server on the channel it already owns: the PROCESS doorbell. The management page is
	// shared memory, so there is no event of its own, and the server must not poll for one on the hot
	// path -- ringing the doorbell is what turns "a request appeared" into "the loop runs a pass",
	// exactly as a lane publish does. (A lost ring costs latency, never correctness: the server also
	// services the page on every pass it makes for any other reason.)
	if (ring_doorbell_fd >= 0) {
		uint64_t one = 1;
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] planewake BEGIN pid=%d fd=%d op=%u seq=%u\n", (int)getpid(),
				ring_doorbell_fd, op, mine);
		}
		(void)!write(ring_doorbell_fd, &one, sizeof(one));
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] planewake DONE pid=%d\n", (int)getpid());
		}
	} else {
		// MEASURED (round 49f): at this point in the bootstrap the process doorbell does NOT exist yet --
		// it arrives with the first lane attach, which is exactly what this plane is meant to precede. With
		// no wake the server stayed in epoll, never ran a pass, and the guest waited for a reply that was
		// never produced. The courier connection exists this early (it is what delivered the page), and the
		// server already watches it, so a ONE-BYTE message on it is the wake: it carries no descriptor and
		// no semantics, and the server reads it as "run a pass".
		int courier = __mldr_fd_courier_socket();
		if (courier >= 0) {
char wake = 0;
						/* perf#30 R1 COURIER PURITY: this used to be a ZERO-FD DATAGRAM (27 per boot, measured). The server now
						   notices a plane publish on its own bounded epoll timeout, and the process doorbell (an eventfd, not
						   AF_UNIX) is used when this process already holds one. No packet leaves here. */
						{ extern int __mldr_ring_doorbell(int fd); int db = __mldr_ring_doorbell(-1);
						  int rang = 0;
						  if (db >= 0) { uint64_t one = 1; rang = (write(db, &one, sizeof(one)) == (ssize_t)sizeof(one)); }
						  /* perf#30 (user directive: an instrument must not make two different facts look the same). Two defects
						     fixed here: (1) the label said `via=doorbell-or-server-poll`, which cannot be told apart, and that is
						     precisely the question violation A turns on -- a publish is either woken by the doorbell or only found
						     later by the server's bounded poll, and the loader KNOWS which, because it rings only when it holds
						     the descriptor; (2) the line ended in a LITERAL backslash-n rather than a newline, so every
						     [plane-wake] record collapsed into ONE log line -- any line-based count of them undercounts, which is
						     exactly how a reader would conclude the wake path was quiet. */
						  { static unsigned g_plane_wake_n = 0; if (__atomic_fetch_add(&g_plane_wake_n, 1, __ATOMIC_RELAXED) < 16) {
							fprintf(stderr, "[plane-wake] n=%u via=%s scm=0 fdcnt=0 payload=0 db=%d\n", g_plane_wake_n, rang ? "doorbell" : "none", db); fflush(stderr); } } }
						(void)wake;
		}
	}
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] planeloop BEGIN pid=%d op=%u mine=%u state=%u\n", (int)getpid(), op, mine,
			__atomic_load_n(&page->request_state, __ATOMIC_ACQUIRE));
	}
	// perf#30 (doc section 187): UNCONDITIONAL, ungated progress. Every reading so far has been an inference
	// from which prints appeared, and two of those prints were themselves measured to be unreliable (a blocking
	// sink, an environment that is replaced mid-boot). This counter has no gate at all: it is written to the
	// diagnostic file directly, so "the loop advanced this far and no further" is read, not deduced.
	unsigned long long iterations = 0;
	int spins = 0;
	int claimed = 0;
	// perf#30 LOADER CHECKIN ORDERING (doc section 168): BOUNDED, and it says why it gave up.
	//
	// This loop had no bound while every other wait in this file does. The consequence was measured: the
	// second incarnation (the exec of launchd) publishes and then hangs here forever, so the boot goes silent
	// with no launchd at all and -- worse for diagnosis -- the guest can say NOTHING about where it stopped,
	// because the diagnostic that would report it comes after this loop. An unbounded wait whose failure is
	// invisible is the "instrument that cannot answer" class this work keeps recording.
	//
	// The bound is generous (about ten seconds of futex waits) because this is a bootstrap path, and on expiry
	// the request is abandoned and the caller's datagram route runs, exactly as for a refused slot.
	int waited_ms = 0;
	// perf#30 (doc section 191): ATOMIC ON EVERY ITERATION. MEASURED: this condition and the `break` below were
	// plain loads, so the compiler may keep the value in a register and the loop then never observes the reply
	// the server writes -- which is exactly the shape of the stall (the loop keeps spinning with `waited=0`), and
	// the completion is written by ANOTHER PROCESS, so no compiler may cache it. The complaints this project
	// already records about hoisting are the same defect seen from the other side.
	while (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) != DSERVER_PROCESS_CONTROL_DONE) {
		// CLAIMED is ownership transfer, not completion: from here the caller waits for the server's
		// transaction to finish rather than giving up and duplicating the operation elsewhere.
		if (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) {
			claimed = 1;
		}
		if ((++iterations % 1000) == 0 && mldr_diag_fd >= 0) {
			mldr_diagf("[mldr-ctl] iter=%llu waited=%d reply=%u seq=%u\n", iterations, waited_ms,
				__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE), page->reply_seq);
		}
		if (++spins < 20000) {
			continue; // the server services the page on its next loop pass
		}
		if (waited_ms > 10000) {
			if (mldr_diag_on()) {
				// perf#30 (doc section 177): the page's IDENTITY, not its address. Addresses cannot be compared
				// across address spaces; the memfd inode can, and the server prints the same field for the
				// region it holds. Equal inodes mean one memory and the defect is in the read; different
				// inodes mean the guest waits on a page the server does not hold.
				{
					struct stat _st;
					unsigned long long _ino = 0;
					if (__mldr_process_control_memfd >= 0 && fstat(__mldr_process_control_memfd, &_st) == 0) {
						_ino = (unsigned long long)_st.st_ino;
					}
					mldr_diagf("[mldr-ctl] plane-page-identity pid=%d memfd=%d ino=%llu\n",
						(int)getpid(), __mldr_process_control_memfd, _ino);
				}
				mldr_diagf("[mldr-ctl] plane-request TIMEOUT pid=%d op=%u seq=%u state=%u claimed=%d"
					" transport_ready=%d page=%p image=%s\n", (int)getpid(), op, mine,
					__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE), claimed,
					__atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE), (void*)page,
					__mldr_diag_image(mldr_load_results.argv != NULL ? mldr_load_results.argv[0] : NULL));
			}
			return -4; // abandoned: the caller falls back to its datagram route
		}
		waited_ms += claimed ? 2 : 1;
		// perf#30 (doc section 189): ONE BYTE per completed sleep, with no formatting, no gate and no condition
		// beyond the fd being open. Every other reading in this stretch was an inference from formatted prints,
		// and two of those print paths were themselves measured to be the blocker. A byte count cannot lie about
		// how many times the loop came round.
		if (mldr_diag_fd >= 0) {
			char dot = '.';
			long a = 1, d = mldr_diag_fd, sp2 = (long)&dot, l = 1;
			__asm__ volatile("syscall" : "+a"(a), "+D"(d), "+S"(sp2), "+d"(l) : : "rcx", "r11", "memory");
		}
		// perf#30 (doc section 181): a heartbeat INSIDE the loop, with the identity that actually identifies the
		// memory -- (dev, ino) of the memfd, not the inode number alone, which is unique only within a
		// filesystem and was the defect that hid this contradiction. It also prints the values the guest reads,
		// so "the server wrote DONE and the guest reads 0" and "the guest reads DONE and does not proceed" are
		// distinguishable in one run instead of in the next three.
		if ((waited_ms % 1000) == 0 && mldr_diag_on()) {
			struct stat _st;
			unsigned long long _dev = 0, _ino = 0;
			if (__mldr_process_control_memfd >= 0 && fstat(__mldr_process_control_memfd, &_st) == 0) {
				_dev = (unsigned long long)_st.st_dev;
				_ino = (unsigned long long)_st.st_ino;
			}
			mldr_diagf("[mldr-ctl] planeloop-heartbeat waited=%d reply_state=%u reply_seq=%u request_state=%u"
				" mine=%u payload0=%llx payload1=%llx memfd=%d dev=%llu ino=%llu\n", waited_ms,
				__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE), page->reply_seq,
				__atomic_load_n(&page->request_state, __ATOMIC_ACQUIRE), mine,
				(unsigned long long)page->reply_payload[0], (unsigned long long)page->reply_payload[1],
				__mldr_process_control_memfd, _dev, _ino);
		}
		// ORDER: read the futex word, THEN the state, THEN wait on the value read. This is the canonical
		// sequence that makes a lost wake impossible: if the server bumped the word before we read it, the
		// state check below already sees DONE; if it bumps after, the value differs and FUTEX_WAIT returns
		// EAGAIN instead of sleeping.
		uint32_t seen = __atomic_load_n(&page->futex, __ATOMIC_ACQUIRE);
		if (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_DONE) {
			break;
		}
		{
			struct timespec ts = {0, claimed ? 2000000L : 1000000L};
			// perf#30 (doc section 183): the two lines that distinguish "the wait never returns" from "it
			// returns and something after it parks". MEASURED: the loop prints its heartbeat at waited=1000
			// and then nothing for the rest of the run, with the process observed parked in futex -- and those
			// two facts together cannot both be explained by a bounded wait that is known to be bounded.
			if ((waited_ms % 1000) == 0 && mldr_diag_on()) {
				mldr_diagf("[mldr-ctl] futex-wait BEGIN waited=%d seen=%u timeout_ns=%ld\n", waited_ms,
					seen, (long)ts.tv_nsec);
			}
			// perf#30 (doc section 185): the PRIMITIVE changed, not another reading. MEASURED: a FUTEX_WAIT on
			// the page's own word, with a timespec the kernel reads as one millisecond, did not return for
			// ~27 s of observation, while the same word and value are exactly what the arguments said. The
			// kernel can always time out a clock_nanosleep, so the polling case uses one; the futex wake stays
			// as an optimisation whose loss costs latency only.
			// perf#30 (doc section 190): NO TIMEOUT. MEASURED: the loop completed exactly 995 slept iterations
			// and then the last sleep never returned, with the tracer showing the same thread in the same wait
			// across the whole observation window -- and the earlier futex anomaly was the same shape (a
			// one-millisecond relative timeout that did not expire). A relative timeout is evaluated against
			// the process's clock, and this process's clock is the GUEST's: when nothing else is pending, guest
			// timers do not advance, so a timeout-based wait can park indefinitely while a WAKE always works.
			// The server writes reply_state and FUTEX_WAKEs the page on completion, so the wait is woken by the
			// event it is waiting for; the timeout was never the mechanism, it was the fallback.
			long fret = syscall(SYS_futex, &page->futex, FUTEX_WAIT, (int)seen, NULL, NULL, 0);
			if ((waited_ms % 1000) == 0 && mldr_diag_on()) {
				mldr_diagf("[mldr-ctl] futex-wait DONE waited=%d ret=%ld futex=%u reply=%u\n", waited_ms,
					fret, page->futex, __atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE));
			}
		}
	}
	// The publisher owns the slot until it has read its own answer (doc section 67) -- and the answer must be
	// READ BEFORE the release (doc section 71): releasing first lets the next thread claim the slot and
	// overwrite reply_seq/reply_status before this thread reads them, which makes it refuse a foreign sequence
	// and repeat the operation on the datagram. That defect was measured as the surviving cause of the
	// per-thread RPC socket creations.
	uint32_t seenSeq = page->reply_seq;
	int32_t seenStatus = page->reply_status;
	{ uint32_t _st = __atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE); if (_st == DSERVER_PROCESS_CONTROL_PENDING) { static const char _m[] = "[release-drops-pending] site=mldr.c:1347\n"; long _a = 1, _d = 2, _s = (long)_m, _n = sizeof(_m) - 1; __asm__ volatile("syscall" : "+a"(_a), "+D"(_d), "+S"(_s), "+d"(_n) : : "rcx", "r11", "memory"); } }
	/* TRACED EXPERIMENT (dar-b5pe): a give-up that would take back a slot the server owns announces itself and leaves
	 * the slot alone. Only the LOADER is changed here, so a boot failure that persists attributes to this site rather
	 * than to the release macro as a whole. */
	if (__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) {
		static int __mldr_held_1 = 0;
		if (!__mldr_held_1) {
			__mldr_held_1 = 1;
			fprintf(stderr, "[mldr-release-held-claimed] site=mldr.c:1896 tid=%d op=%u state=%u rstate=%u\n",
				(int)syscall(SYS_gettid),
				(unsigned)__atomic_load_n(&(page)->request_op, __ATOMIC_ACQUIRE),
				(unsigned)__atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE),
				(unsigned)__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE));
			fflush(stderr);
		}
	} else {
		uint32_t __mldr_expect_1 = DSERVER_PROCESS_CONTROL_PENDING;
		(void)__atomic_compare_exchange_n(&(page)->request_state, &__mldr_expect_1,
			DSERVER_PROCESS_CONTROL_IDLE, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
	}
	if (seenSeq != mine) {
		return -2; // a stale answer: never treated as success
	}
	return seenStatus;
}

/* A LOADER REQUEST THAT DID NOT GET ITS OWN ANSWER IS RETRIED, BOUNDED (dar-4cp9). MEASURED root cause of the
 * residual boot stall: __mldr_process_control_request_once returns -2 when the reply sequence is not the one it
 * published (its wait ran out with the slot still PENDING, printed as release-drops-pending), and the loader's
 * caller treats ANY negative as fatal -- `Failed to tell darlingserver about our executable path` and exit(1),
 * which is precisely the 15-line boot failure whose guest disappears with no denial and no signal. A retry is the
 * same remedy used for the guest-side and server-side plane paths and is safe for the same reason: the request
 * map is keyed by (pid, seq), so a new sequence is a NEW transaction. Only -2 is retried -- a real server status
 * is an answer and is returned unchanged -- and the attempts are bounded at three. */
int __mldr_process_control_request(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3) {
	int rc = -1;
	for (int attempt = 0; attempt < 3; ++attempt) {
		rc = __mldr_process_control_request_once(op, p0, p1, p2, p3);
		if (rc != -2) {
			return rc;
		}
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] request-retry op=%u attempt=%d\n", (unsigned)op, attempt + 1);
		}
	}
	return rc;
}

// perf#28d (round 49p): the loader RECEIVES descriptors as well as sending them -- the process doorbell
// arrives on the same connection. MEASURED: with the previous stub (`return -1`) the generated wrapper's
// reply-token branch resolved every arriving doorbell to -1 and never called the real receive, which is why
// the third one-time attempt sent a correct bundle and the loader still saw wake=-1.
#define MLDR_FD_COURIER_PENDING_MAX 16
struct mldr_courier_pending {
	uint64_t token;
	int fd;
};
static struct mldr_courier_pending mldr_courier_pending[MLDR_FD_COURIER_PENDING_MAX];

static int __mldr_fd_courier_lookup(uint64_t token) {
	for (int i = 0; i < MLDR_FD_COURIER_PENDING_MAX; ++i) {
		if (mldr_courier_pending[i].token == token && mldr_courier_pending[i].token != 0) {
			int fd = mldr_courier_pending[i].fd;
			mldr_courier_pending[i].fd = -1;
			mldr_courier_pending[i].token = 0;
			return fd;
		}
	}
	return -1;
}

static void __mldr_fd_courier_store(uint64_t token, int fd) {
	if (token == 0 || fd < 0) {
		if (fd >= 0) {
			close(fd);
		}
		return;
	}
	for (int i = 0; i < MLDR_FD_COURIER_PENDING_MAX; ++i) {
		if (mldr_courier_pending[i].token == 0) {
			mldr_courier_pending[i].token = token;
			mldr_courier_pending[i].fd = fd;
			return;
		}
	}
	close(fd); // a full registry closes rather than leaks; the loader never needs more than a handful
}

// One non-blocking receive of one bundle. Returns 1 when a message was consumed, 0 when nothing is queued.
static int __mldr_fd_courier_recv_once(int sock) {
	struct dserver_fd_courier_message envelope;
	char control[CMSG_SPACE(sizeof(int))];
	memset(control, 0, sizeof(control));
	struct iovec iov = { &envelope, sizeof(envelope) };
	struct msghdr msg;
	memset(&msg, 0, sizeof(msg));
	msg.msg_iov = &iov;
	msg.msg_iovlen = 1;
	msg.msg_control = control;
	msg.msg_controllen = sizeof(control);
	ssize_t got = recvmsg(sock, &msg, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
	if (got <= 0) {
		return 0;
	}
	if (got != (ssize_t)sizeof(envelope)) {
		// perf#30 (violation A): this branch DISCARDED a received descriptor in silence, and a silent discard is
		// indistinguishable from a bundle that never arrived -- which is why the server could be measured sending the
		// doorbell (52 records in its own log) while the loader's windows expired empty. Name it, with the length that
		// did not match, so the next run decides between "arrives malformed" and "never arrives on this connection".
		fprintf(stderr, "[fd-courier-drop] got=%zd expect=%zu flags=0x%x\n", got, sizeof(envelope), (unsigned)msg.msg_flags);
		fflush(stderr);
		for (struct cmsghdr* c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
			if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
				uint32_t n = (uint32_t)((c->cmsg_len - CMSG_LEN(0)) / sizeof(int));
				int* came = (int*)CMSG_DATA(c);
				for (uint32_t i = 0; i < n; ++i) {
					if (came[i] >= 0) {
						close(came[i]);
					}
				}
			}
		}
		return 1;
	}
	if (msg.msg_flags & MSG_CTRUNC) {
		// Same reason as the length branch: a truncated control message drops the descriptor, and it used to do so
		// without a word.
		fprintf(stderr, "[fd-courier-drop] truncated control message flags=0x%x kind=%u\n", (unsigned)msg.msg_flags, envelope.kind);
		fflush(stderr);
		return 1;
	}
	int fd = -1;
	for (struct cmsghdr* c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
		if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
			uint32_t n = (uint32_t)((c->cmsg_len - CMSG_LEN(0)) / sizeof(int));
			int* came = (int*)CMSG_DATA(c);
			if (n >= 1) {
				fd = came[0];
				for (uint32_t i = 1; i < n; ++i) {
					close(came[i]);
				}
			}
		}
	}
	// perf#30 (violation A): a process-doorbell envelope is ADOPTED here rather than stored under a token the loader
	// cannot know. Nothing sends this kind to the loader today, so the branch is inert on the default path; with the
	// early-delivery hatch it is what makes `__mldr_ring_doorbell(-1)` non-negative BEFORE the first plane publish.
	// MEASURED without it: 36-42 of a boot's publishes reported `via=none`, i.e. the bounded poll was the only thing
	// waking the server.
	if (envelope.kind == DSERVER_FD_COURIER_KIND_PROCESS_DOORBELL && fd >= 0) {
		// ADOPT **and** STORE. Storing alone leaves the loader without a doorbell (measured: 36-42 publishes per boot
		// reporting `via=none`), but adopting alone BREAKS THE BOOT: the server already sends a PROCESS_DOORBELL
		// bundle on the lane attach, whose token the attach reply carries in reply_payload[1], and a later
		// `__mldr_fd_courier_receive(token)` then finds nothing -- MEASURED as a stall after 6 plane publishes with
		// no crash. So the descriptor is adopted here and a DUP is stored under the token, which keeps both
		// consumers working.
		int forToken = dup(fd);
		if (forToken >= 0) {
			__mldr_fd_courier_store(envelope.token, forToken);
		}
		int adopted = __mldr_ring_doorbell(fd);
		fprintf(stderr, "[plane-doorbell] adopted=%d fd=%d stored_dup=%d token=%llu\n", adopted, fd, forToken, (unsigned long long)envelope.token);
		fflush(stderr);
		return 1;
	}
	__mldr_fd_courier_store(envelope.token, fd);
	return 1;
}

int __mldr_fd_courier_receive(uint64_t token) {
	if (token == 0) {
		return -1;
	}
	int sock = __mldr_fd_courier_socket();
	if (sock < 0) {
		return -1;
	}
	int fd = __mldr_fd_courier_lookup(token);
	if (fd >= 0) {
		return fd;
	}
	// The server sends the descriptor BEFORE it publishes the reply, so by the time a reply names a token
	// the bundle is normally queued; a bounded retry covers the in-flight window without hanging a boot.
	// SHORT by design: MEASURED (round 49q), a generous retry (200 x 0.5 ms) put 100 ms on the path of any
	// call whose token never arrives, and the 16-thread stress workload stopped completing because of it.
	// The server sends the descriptor BEFORE it publishes the reply, so a token that is not queued yet is
	// in flight for microseconds, not milliseconds.
	for (int i = 0; i < 4; ++i) {
		if (__mldr_fd_courier_recv_once(sock) == 1) {
			fd = __mldr_fd_courier_lookup(token);
			if (fd >= 0) {
				return fd;
			}
		}
		struct timespec ts;
		ts.tv_sec = 0;
		ts.tv_nsec = 250000;
		nanosleep(&ts, NULL);
	}
	return -1;
}

static struct sockaddr_un __mldr_fd_courier_address_data;
static int __mldr_fd_courier_address_ready = 0;

// perf#30 FD-COURIER: the process-level incarnation identity. Computed ONCE per process and handed to
// every image through the elfcalls table: a per-image value would make the second image in a process
// look like a stale incarnation to the server, which keys descriptors by (pid, generation).
const void* __mldr_fd_courier_address(void);

// perf#30 FD-COURIER: the process's SINGLE courier connection. Opened on first use and reused for every
// descriptor transfer in this process, whichever image asks: process-level transport state belongs to
// the loader, and a per-image socket would give the server several connections (and several identities)
// for one process.
static int __mldr_fd_courier_socket_cached = -2; // -2 untried, -1 unusable

void __mldr_fd_courier_reset(void); // the accessor the images reach through the elfcalls table

int __mldr_fd_courier_socket(void) {
	if (__mldr_fd_courier_socket_cached != -2) {
		return __mldr_fd_courier_socket_cached;
	}
	__mldr_fd_courier_socket_cached = -1;
	const void* address = __mldr_fd_courier_address();
	if (address == NULL) {
		return __mldr_fd_courier_socket_cached;
	}
	int sock = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
	if (sock < 0) {
		return __mldr_fd_courier_socket_cached;
	}
	// Abstract name: sun_path[0] is the NUL and the name follows. The length must cover exactly the
	// name -- a full-size sockaddr_un appends the struct's zero padding and the kernel refuses.
	const struct sockaddr_un* addr = (const struct sockaddr_un*)address;
	socklen_t len = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + strlen(&addr->sun_path[1]));
	if (connect(sock, (const struct sockaddr*)addr, len) != 0) {
		close(sock);
		return __mldr_fd_courier_socket_cached;
	}
	__mldr_fd_courier_socket_cached = sock;
	// perf#30 (violation A): an fd NUMBER is not an identity across processes, so the connection names itself by its
	// socket inode, which both sides can print and compare. MEASURED need: the server's bundle log says it sends to a
	// connection it believes is the loader's (isLoader=1) while the loader reads its own socket and receives nothing
	// for three seconds -- only a shared identity can say whether those are the same connection.
	{
		struct stat connStat;
		if (fstat(sock, &connStat) == 0) {
			fprintf(stderr, "[fd-courier-conn] sock=%d ino=%llu\n", sock, (unsigned long long)connStat.st_ino);
			fflush(stderr);
		}
	}
	return __mldr_fd_courier_socket_cached;
}

void __mldr_fd_courier_reset(void) {
	if (__mldr_fd_courier_socket_cached >= 0) {
		close(__mldr_fd_courier_socket_cached);
	}
	// FORGET the number, do not merely close it: a closed number can be reused by the next file the
	// process opens, and a stale cached number is how an image ends up sending on someone else's file.
	__mldr_fd_courier_socket_cached = -2;
}

uint64_t __mldr_process_generation(void) {
	static uint64_t generation = 0;
	if (generation == 0) {
		struct timespec ts;
		if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0) {
			generation = (((uint64_t)ts.tv_sec) << 32) ^ (uint64_t)ts.tv_nsec;
		}
		if (generation == 0) {
			generation = 1;
		}
	}
	return generation;
}

const void* __mldr_fd_courier_address(void) {
	if (!__mldr_fd_courier_address_ready) {
		const char* prefix = getenv("DARLING_PREFIX");
		if (prefix == NULL) {
			prefix = getenv("DPREFIX");
		}
		if (prefix == NULL) {
			return NULL;
		}
		char real[PATH_MAX];
		if (realpath(prefix, real) == NULL) {
			return NULL;
		}
		char name[256];
		int n = snprintf(name, sizeof(name), "darlingserver-fdcourier:%s", real);
		if (n <= 0 || (size_t)n >= sizeof(name) - 1) {
			return NULL;
		}
		if (mldr_diag_on()) {
			fprintf(stderr, "[mldr-courier] pid=%d prefix=%s name=%s\n", (int)getpid(), real, name);
		}
		memset(&__mldr_fd_courier_address_data, 0, sizeof(__mldr_fd_courier_address_data));
		__mldr_fd_courier_address_data.sun_family = AF_UNIX;
		// ABSTRACT name: sun_path[0] is the NUL, the name follows. Binding a pathname socket here
		// would make every connect fail silently, which is exactly how the first attempt behaved.
		__mldr_fd_courier_address_data.sun_path[0] = '\0';
		memcpy(&__mldr_fd_courier_address_data.sun_path[0] + 1, name, (size_t)n + 1);
		__mldr_fd_courier_address_ready = 1;
	}
	return &__mldr_fd_courier_address_data;
}

int __mldr_ring_doorbell(int fd); // defined below
static int mldr_ring_lane_state = 0; // 0 none, 1 attaching, 2 published, -1 failed
static void* mldr_ring_lane_map = 0;
static uint64_t mldr_ring_lane_size = 0;

int __mldr_ring_lane_ready(void) { return mldr_ring_lane_state == 2; }

void* __mldr_ring_lane_seed(void) {
	if (getenv("MLDR_SEED_OFF") != NULL) { return 0; }
	if (mldr_ring_lane_state != 0) {
		return mldr_ring_lane_map;
	}
	mldr_ring_lane_state = 1;
	uint64_t hdr = sizeof(dserver_ring_shm_t);
	uint64_t ring_span = sizeof(dserver_ring_t) + (uint64_t)MLDR_GR_SLOT_COUNT * MLDR_GR_SLOT_SIZE;
	uint64_t c2s_off = (hdr + 63u) & ~63ull;
	uint64_t s2c_off = (c2s_off + ring_span + 63u) & ~63ull;
	uint64_t total = (s2c_off + ring_span + 63u) & ~63ull;
	int tid = (int)gettid();
	int memfd = (int)syscall(__NR_memfd_create, "dring", 0x1u);
	if (memfd < 0) { mldr_ring_lane_state = -1; return 0; }
	if (ftruncate(memfd, (off_t)total) < 0) { close(memfd); mldr_ring_lane_state = -1; return 0; }
	void* map = mmap(0, total, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, 0);
	if (map == MAP_FAILED) { close(memfd); mldr_ring_lane_state = -1; return 0; }
	dserver_ring_shm_t* cb = (dserver_ring_shm_t*)map;
	cb->magic = DSERVER_RING_MAGIC;
	cb->abi_version = DSERVER_RING_ABI_VERSION;
	cb->slot_size = (uint16_t)MLDR_GR_SLOT_SIZE;
	cb->slot_count = MLDR_GR_SLOT_COUNT;
	cb->arena_off = 0;
	cb->arena_size = 0;
	cb->c2s_ring_off = (uint32_t)c2s_off;
	cb->s2c_ring_off = (uint32_t)s2c_off;
	cb->total_size = (uint32_t)total;
	cb->guest_tid = (int32_t)tid;
	cb->c2s_opcode_hash = dserver_ring_c2s_opcode_hash();
	cb->server_state = DSERVER_RING_SRV_SLEEPING_EPOLL;
	cb->s2c_waiters = 0;
	uint32_t reject = 0;
	int wake_fd = -1;
	fprintf(stderr, "[mldr-seed] attach pid=%d memfd=%d size=%llu\n", (int)getpid(), memfd, (unsigned long long)total);
	int rc = 0;
	if (getenv("MLDR_SEED_NO_ATTACH") != NULL) {
		rc = -1; // bisection: skip the negotiate entirely
	} else {
		// perf#30 ATTACH_LANE on the page for the EARLIEST attach too. MEASURED: the loader's seed went
		// straight to the datagram, which is the whole of the remaining `ring_attach uds` (9 per run) --
		// every later attach already rides the page. The page exists by now (it is created before the
		// seed) and its transport is established, so the same two-half shape applies: descriptor on the
		// process courier, semantics and completion on the page.
		int viaPage = 0;
		struct dserver_process_control* page = (struct dserver_process_control*)__mldr_process_control_page();
		if (page != NULL && __atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) != 0) {
			uint64_t token = __mldr_fd_courier_send_token(memfd, DSERVER_FD_COURIER_KIND_LANE_BACKING);
			if (token != 0) {
				int slot = 0;
				for (int t = 0; t < 2000 && !slot; ++t) {
					uint32_t expect = DSERVER_PROCESS_CONTROL_IDLE;
					if (__atomic_compare_exchange_n(&page->request_state, &expect,
					        DSERVER_PROCESS_CONTROL_PENDING, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
						slot = 1;
						break;
					}
					struct timespec ts = {0, 1000000L};
					nanosleep(&ts, NULL);
				}
				if (slot) {
					static uint32_t seed_seq = 0;
					uint32_t mine = __atomic_add_fetch(&seed_seq, 1, __ATOMIC_RELAXED);  // atomic: doc section 70
					__atomic_store_n(&page->reply_state, DSERVER_PROCESS_CONTROL_IDLE, __ATOMIC_RELEASE);
					page->request_op = DSERVER_PROCESS_CONTROL_OP_ATTACH_LANE;
					page->request_seq = mine;
					page->request_payload[0] = (uint64_t)tid;
					page->request_payload[1] = total;
					page->request_payload[2] = token;
					page->request_payload[3] = 1;
					__atomic_store_n(&page->request_state, DSERVER_PROCESS_CONTROL_PENDING, __ATOMIC_RELEASE);
					{
						int courier = __mldr_fd_courier_socket();
						if (courier >= 0) {
char wake = 0;
						/* perf#30 R1 COURIER PURITY: this used to be a ZERO-FD DATAGRAM (27 per boot, measured). The server now
						   notices a plane publish on its own bounded epoll timeout, and the process doorbell (an eventfd, not
						   AF_UNIX) is used when this process already holds one. No packet leaves here. */
						{ extern int __mldr_ring_doorbell(int fd); int db = __mldr_ring_doorbell(-1);
						  int rang = 0;
						  if (db >= 0) { uint64_t one = 1; rang = (write(db, &one, sizeof(one)) == (ssize_t)sizeof(one)); }
						  /* perf#30 (user directive: an instrument must not make two different facts look the same). Two defects
						     fixed here: (1) the label said `via=doorbell-or-server-poll`, which cannot be told apart, and that is
						     precisely the question violation A turns on -- a publish is either woken by the doorbell or only found
						     later by the server's bounded poll, and the loader KNOWS which, because it rings only when it holds
						     the descriptor; (2) the line ended in a LITERAL backslash-n rather than a newline, so every
						     [plane-wake] record collapsed into ONE log line -- any line-based count of them undercounts, which is
						     exactly how a reader would conclude the wake path was quiet. */
						  { static unsigned g_plane_wake_n = 0; if (__atomic_fetch_add(&g_plane_wake_n, 1, __ATOMIC_RELAXED) < 16) {
							fprintf(stderr, "[plane-wake] n=%u via=%s scm=0 fdcnt=0 payload=0 db=%d\n", g_plane_wake_n, rang ? "doorbell" : "none", db); fflush(stderr); } } }
						(void)wake;
						}
					}
					int spins = 0, claimed = 0;
					while (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) != DSERVER_PROCESS_CONTROL_DONE) {
						if (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) {
							claimed = 1;
						}
						if (++spins < 20000) { continue; }
						{
							uint32_t seen = page->futex;
							struct timespec ts = {0, claimed ? 2000000L : 1000000L};
							if (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_DONE) { break; }
							syscall(SYS_futex, &page->futex, FUTEX_WAIT, (int)seen, &ts, NULL, 0);
						}
					}
					// read before release (doc 71): releasing first lets the next thread overwrite the fields.
					uint32_t seenState = __atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE);
					uint32_t seenSeq = page->reply_seq;
					int32_t seenStatus = page->reply_status;
	{ uint32_t _st = __atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE); if (_st == DSERVER_PROCESS_CONTROL_PENDING) { static const char _m[] = "[release-drops-pending] site=mldr.c:1675\n"; long _a = 1, _d = 2, _s = (long)_m, _n = sizeof(_m) - 1; __asm__ volatile("syscall" : "+a"(_a), "+D"(_d), "+S"(_s), "+d"(_n) : : "rcx", "r11", "memory"); } }
					/* TRACED EXPERIMENT (dar-b5pe), the second loader site: same rule, same marker. */
					if (__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) {
						static int __mldr_held_2 = 0;
						if (!__mldr_held_2) {
							__mldr_held_2 = 1;
							fprintf(stderr, "[mldr-release-held-claimed] site=mldr.c:2303 tid=%d op=%u state=%u rstate=%u\n",
								(int)syscall(SYS_gettid),
								(unsigned)__atomic_load_n(&(page)->request_op, __ATOMIC_ACQUIRE),
								(unsigned)__atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE),
								(unsigned)__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE));
							fflush(stderr);
						}
					} else {
						uint32_t __mldr_expect_2 = DSERVER_PROCESS_CONTROL_PENDING;
						(void)__atomic_compare_exchange_n(&(page)->request_state, &__mldr_expect_2,
							DSERVER_PROCESS_CONTROL_IDLE, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
					}
					if (seenState == DSERVER_PROCESS_CONTROL_DONE && seenSeq == mine) {
						rc = page->reply_status;
						reject = (uint32_t)page->reply_payload[0];
						if (page->reply_payload[1] != 0) {
							wake_fd = __mldr_fd_courier_receive(page->reply_payload[1]);
						}
						viaPage = 1;
					} else if (claimed) {
						// the server owns it; do not duplicate the attach on the datagram
						rc = -1;
						viaPage = 1;
					}
				}
			}
		}
		if (!viaPage) {
			rc = dserver_rpc_ring_attach(memfd, total, &reject, &wake_fd);
		}
	}
	fprintf(stderr, "[mldr-seed] attach-rc pid=%d rc=%d reject=%u wake=%d\n", (int)getpid(), rc, reject, wake_fd);
	if (getenv("MLDR_SEED_NO_ATTACH") != NULL) {
		// keep the mapping + a registry record so the rest of the seed still runs
		mldr_ring_lane_map = map;
		mldr_ring_lane_size = total;
		mldr_ring_lane_state = 2;
		return map;
	}
	close(memfd); // the mapping holds the reference
	if (rc != 0 || reject != dserver_ring_ok || wake_fd < 0) {
		munmap(map, total);
		if (wake_fd >= 0) close(wake_fd);
		mldr_ring_lane_state = -1;
		return 0;
	}
	// EXPERIMENT C: do NOT adopt the process doorbell here. The doorbell is the loader's canonical fd;
	// adopting it this early is a candidate cause of the boot break (A/B showed the harm is not the
	// adoption of the lane by a later image, so the remaining suspect is this step). The lane keeps a
	// non-owning reference semantics either way -- the FIRST RUNTIME ATTACH adopts the doorbell exactly
	// as it does today.
	fprintf(stderr, "[mldr-seed] doorbell-in pid=%d wake=%d mode=%s\n", (int)getpid(), wake_fd, getenv("MLDR_SEED_KEEP_DOORBELL") ? "adopt" : "close");
	// The seed ALWAYS adopts the canonical doorbell: experiment C showed closing it changes nothing about
	// the boot break, while leaving it closed left every later image (and therefore every borrowed view)
	// with doorbell=-1 and no way to wake the server.
	int doorbell = __mldr_ring_doorbell(wake_fd);
	fprintf(stderr, "[mldr-seed] doorbell-out pid=%d fd=%d\n", (int)getpid(), doorbell);
	if (doorbell < 0) { munmap(map, total); mldr_ring_lane_state = -1; return 0; }
	for (uint32_t i = 0; i < MLDR_RING_LANE_SLOTS; ++i) {
		uint32_t expected = 0;
		if (__atomic_compare_exchange_n(&mldr_ring_lanes[i].state, &expected, 1u, 0,
		                                __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) {
			mldr_ring_lanes[i].host_tid = tid;
			mldr_ring_lanes[i].owner_image = 0; // 0 == mldr
			mldr_ring_lanes[i].generation = 1;
			mldr_ring_lanes[i].slot_index = 0;
			mldr_ring_lanes[i].mapping = map;
			mldr_ring_lanes[i].mapping_size = total;
			mldr_ring_lanes[i].next_seq = 1;
			// adoption-bisection mode, published for the guest images (they must not scan the env this
			// early): 0x100 | mode with mode 0=off, 1=create-view-only, 2=full adoption.
			{
				const char* m = getenv("DARLING_GUEST_ADOPT_MODE");
				uint32_t mode = 2;
				if (m && m[0] == 'o') mode = 0;
				else if (m && m[0] == 'v') mode = 1;
				mldr_ring_lanes[i].creator_image = 0x100u | mode;
			}
			// BOOT-SAFE DEFAULT: the record is left ATTACHING (published to nobody). Publishing it ACTIVE makes a
	// later image ADOPT this lane, and that adoption path is currently the one component that still breaks
	// boot (measured: ATTACHING/no-adoption arms boot GREEN, ACTIVE arm RED). The loader itself uses the
	// lane in both cases -- so mldr-as-first-owner works and adoption is isolated behind this switch until
	// its remaining defect is fixed.
	// DEFAULT ON: the loader's incarnation is published so the guest images ADOPT it (proved in PRODUCT:
	// boot GREEN, proc_adopted>0 in every process, no second attach, ring_attach count drops). The
	// diagnostic override MLDR_SEED_NO_ADOPT restores the pre-Stage-2 behaviour.
	if (getenv("MLDR_SEED_NO_ADOPT") == NULL) {
		__atomic_store_n(&mldr_ring_lanes[i].state, 2u, __ATOMIC_RELEASE); // publish LAST
	}
			mldr_ring_lane_map = map;
			mldr_ring_lane_size = total;
			mldr_ring_lane_rec = &mldr_ring_lanes[i];
			mldr_ring_lane_state = 2;
			return map;
		}
	}
	munmap(map, total);
	mldr_ring_lane_state = -1;
	return 0;
}

// perf#30 STAGE 2 (attempt E): the loader must USE the lane it creates. The bisection proved the boot
// break is the server-side ring_attach itself (OFF/NO-ATTACH arms GREEN, FULL arm RED), i.e. the server
// binds this thread to the lane at attach time, so a lane that is attached and then never used is not a
// neutral state. This performs one ordinary, no-argument RPC over the seeded lane, using the SHARED SPSC
// helpers and the shared wire convention (slot header + payload; reply = reply-hdr + body).
int __mldr_ring_call(uint32_t callnum, const void* req, uint32_t reqlen, void* rep, uint32_t replen) {
	if (mldr_ring_lane_state != 2 || !mldr_ring_lane_rec) {
		return -1;
	}
	dserver_ring_shm_t* cb = (dserver_ring_shm_t*)mldr_ring_lane_map;
	dserver_ring_t* c2s = (dserver_ring_t*)((char*)mldr_ring_lane_map + cb->c2s_ring_off);
	dserver_ring_t* s2c = (dserver_ring_t*)((char*)mldr_ring_lane_map + cb->s2c_ring_off);
	dserver_ring_slot_t* slot = dserver_ring_producer_begin(c2s, cb->slot_size, cb->slot_count);
	if (!slot) {
		return -1;
	}
	uint32_t seq = __atomic_fetch_add(&mldr_ring_lane_rec->next_seq, 1u, __ATOMIC_RELAXED);
	slot->callnum = callnum;
	slot->seq = seq;
	slot->length = reqlen;
	slot->arena_off = 0;
	slot->arena_len = 0;
	slot->flags = 0;
	if (reqlen) {
		memcpy((char*)slot + sizeof(dserver_ring_slot_t), req, reqlen);
	}
	dserver_ring_producer_publish(c2s);
	if (dserver_ring_guest_should_doorbell(cb)) {
		uint64_t one = 1;
		int fd = __mldr_ring_doorbell(-1);
		if (fd >= 0) {
			(void)write(fd, &one, sizeof(one));
		}
	}
	for (long spin = 0; spin < 200000000L; ++spin) {
		dserver_ring_slot_t* rs = dserver_ring_consumer_begin(s2c, cb->slot_size, cb->slot_count);
		if (!rs) {
			continue;
		}
		if (rs->seq != seq || rs->callnum != callnum) {
			dserver_ring_consumer_advance(s2c);
			continue;
		}
		dserver_ring_reply_hdr_t* hdr = (dserver_ring_reply_hdr_t*)((char*)rs + sizeof(dserver_ring_slot_t));
		uint32_t body_len = rs->length >= sizeof(dserver_ring_reply_hdr_t) ? rs->length - (uint32_t)sizeof(dserver_ring_reply_hdr_t) : 0;
		if (body_len > replen) {
			body_len = replen;
		}
		if (rep && body_len) {
			memcpy(rep, (char*)hdr + sizeof(dserver_ring_reply_hdr_t), body_len);
		}
		int code = hdr->code;
		dserver_ring_consumer_advance(s2c);
		return code;
	}
	return -2; // published and no reply: the caller must NOT retry this call over UDS
}


int __mldr_ring_doorbell(int received) {
	if (received < 0) {
		if (ring_doorbell_fd >= 0) {
			return ring_doorbell_fd;
		}
		return ring_seed_doorbell_fd; // the seed's doorbell survives a variable reset (fd stays open)
	}
	if (ring_doorbell_fd >= 0) {
		close(received); // another lane's dup of the SAME object: the canonical fd is already held
		return ring_doorbell_fd;
	}
	int adopted = __mldr_adopt_ring_fd(received);
	close(received); // adopt duplicated it into our internal range; this one is ours to close
	if (adopted < 0) {
		return -1;
	}
	ring_doorbell_fd = adopted;
	return adopted;
}

bool __mldr_fd_is_internal(int fd) {
	bool owned = false;
	sigset_t saved;
	mldr_block_async_signals(&saved);
	pthread_mutex_lock(&socket_bitmap.mutex);
	if (fd >= 0 && fd <= socket_bitmap.highest) {
		size_t index = socket_bitmap.highest - fd;
		owned = index < socket_bitmap.bit_length &&
			(socket_bitmap.bits[index / 8] & (1U << (index % 8))) != 0;
	}
	pthread_mutex_unlock(&socket_bitmap.mutex);
	mldr_restore_signals(&saved);
	return owned;
}

// perf#27 (lane lifecycle): release an adopted ring descriptor. The guest lane that owned it is being
// torn down, so the loader must FORGET the number as well as close it -- leaving it in ring_fds[] would
// make a later __mldr_postfork_child close a number the application may have since reused for something
// else. Called from the guest's thread-exit path via the `dserver_release_ring_fd` elfcall.
void __mldr_release_ring_fd(int fd) {
	if (fd < 0) {
		return;
	}
	sigset_t saved;
	mldr_block_async_signals(&saved);
	pthread_mutex_lock(&socket_bitmap.mutex);
	int found = 0;
	for (size_t i = 0; i < ring_fd_count; ++i) {
		if (ring_fds[i] == fd) {
			socket_bitmap_put_locked(&socket_bitmap, fd);
			ring_fds[i] = ring_fds[--ring_fd_count];
			found = 1;
			break;
		}
	}
	pthread_mutex_unlock(&socket_bitmap.mutex);
	mldr_restore_signals(&saved);
	// close outside the lock: an fd teardown may take the address-space lock in the kernel and we do not
	// want to hold the fork-blocking registry mutex across it.
	close(fd);
	(void)found;
}

void __mldr_prefork_prepare(void) {
	// Do not fork between installing an inherited ring FD and recording its
	// ownership. The parent releases this lock on success and failure alike.
	// A guest signal handler may call close/fcntl and reenter this registry.
	mldr_block_async_signals(&fork_signal_mask);
	pthread_mutex_lock(&socket_bitmap.mutex);
}

void __mldr_postfork_parent(void) {
	pthread_mutex_unlock(&socket_bitmap.mutex);
	mldr_restore_signals(&fork_signal_mask);
}

void __mldr_postfork_child(void) {
	// perf#30 (directive sections 2/3, doc section 206): the child inherits a descriptor NUMBER whose file
	// description belongs to the parent and which guard_table_postfork_child is about to close, so the loader's
	// cached per-thread socket is invalidated here -- WITHOUT creating one. The child's transport is the process
	// Ring, which it re-adopts; a socket, if one is ever needed again, is created lazily by the call that needs it.
	{
		extern void __darling_thread_rpc_socket_invalidate(void);
		__darling_thread_rpc_socket_invalidate();
	}
	socket_bitmap.mutex = (pthread_mutex_t)PTHREAD_MUTEX_INITIALIZER;
	__mldr_glibc_fork_reset_child();
	// Close both guest images' inherited ring descriptors before any child
	// RPC socket can reuse their numbers. Image-local reset must not close
	// these saved numbers a second time.
	for (size_t i = 0; i < ring_fd_count; ++i) {
		close(ring_fds[i]);
		socket_bitmap_put(&socket_bitmap, ring_fds[i]);
	}
	ring_fd_count = 0;
	// The child does not inherit the parent's doorbell: the descriptor above was just closed and the
	// child's first lane attach adopts its own. Writing a stale number here would be a wake for a
	// descriptor the child no longer owns.
	ring_doorbell_fd = -1;
	mldr_restore_signals(&fork_signal_mask);
}

int __mldr_create_rpc_socket(void) {
	int pre_fd = -1;
	int fd = -1;

	pre_fd = socket(AF_UNIX, SOCK_DGRAM, 0);
	if (pre_fd < 0) {
		goto err_out;
	}

	fd = socket_bitmap_get(&socket_bitmap);
	if (fd < 0) {
		goto err_out;
	}

	if (dup2(pre_fd, fd) < 0) {
		// we have to put it away ourselves here because `fd` is not yet valid, so we can't close() it in the error handler
		socket_bitmap_put(&socket_bitmap, fd);
		fd = -1;
		goto err_out;
	}

	close(pre_fd);
	pre_fd = -1;

	// `fd` now contains the socket with the desired FD number returned by `socket_bitmap_get`

	int fd_flags = fcntl(fd, F_GETFD);
	if (fd_flags < 0) {
		goto err_out;
	}
	if (fcntl(fd, F_SETFD, fd_flags | FD_CLOEXEC) < 0) {
		goto err_out;
	}

	sa_family_t family = AF_UNIX;
	if (bind(fd, (const struct sockaddr*)&family, sizeof(family)) < 0) {
		goto err_out;
	}

out:
	return fd;

err_out:
	if (fd >= 0) {
		socket_bitmap_put(&socket_bitmap, fd);
		close(fd);
	}

	if (pre_fd >= 0) {
		close(pre_fd);
	}

	return -1;
};

void __mldr_close_rpc_socket(int socket) {
	close(socket);
	socket_bitmap_put(&socket_bitmap, socket);
};

int __mldr_create_process_lifetime_pipe(int* fds) {
	// These pipes are not required for Linux 5.3 or newer,
	// we already have pidfd_open.
	if (is_kernel_at_least(5, 3)) {
		fds[0] = fds[1] = -1;
		return 0;
	}

	int pre_fds[2];
	if (pipe(pre_fds) == -1) {
		goto err_out;
	}

	for (int i = 0; i < 2; ++i) {
		fds[i] = socket_bitmap_get(&socket_bitmap);
		if (fds[i] < 0) {
			goto err_out;
		}

		if (dup2(pre_fds[i], fds[i]) < 0) {
			socket_bitmap_put(&socket_bitmap, fds[i]);
			fds[i] = -1;
			goto err_out;
		}

		close(pre_fds[i]);
		pre_fds[i] = -1;
	}

	return 0;

err_out:
	for (int i = 0; i < 2; ++i) {
		if (fds[i] >= 0) {
			socket_bitmap_put(&socket_bitmap, fds[i]);
			close(fds[i]);
		}

		if (pre_fds[i] >= 0) {
			close(pre_fds[i]);
		}
	}

	return -1;
}

void __mldr_close_process_lifetime_pipe(int fd) {
	if (fd != -1) {
		close(fd);
		socket_bitmap_put(&socket_bitmap, fd);
	}
}

static void setup_space(struct load_results* lr, bool is_64_bit) {
	commpage_setup(is_64_bit);

	// Place the guest's main stack just below the commpage. Using the default
	// (native) stack top would put the stack just *above* the commpage and
	// eventually collide with it, so we allocate our own stack region instead.
	unsigned long preferred_top = commpage_address(
#if __x86_64__
		true
#elif __i386__
		false
#else
	#error Unsupported architecture
#endif
	);

	struct rlimit limit;
	getrlimit(RLIMIT_STACK, &limit);
	// Reserve the permitted stack range, not just its first 64 KiB. A successful
	// MAP_GROWSDOWN mapping can still be unable to expand when another mapping
	// lies below it (including after the kernel-chosen EEXIST fallback). Pages
	// remain demand-paged; an unlimited stack starts with Darwin's 8 MiB default.
	unsigned long size = limit.rlim_cur == RLIM_INFINITY
			? 8UL * 1024 * 1024 : limit.rlim_cur;

	// dar-stackmmap-eexist-9j9: the preferred fixed range [preferred_top - size,
	// preferred_top) sits just below the commpage. For some guests the process's
	// own native Linux [stack] tops out exactly at the commpage base and extends
	// down across this range, so the fixed mapping overlaps the live native stack
	// and MAP_FIXED_NOREPLACE returns EEXIST. (Captured live: native [stack]
	// 0x7fffffde0000-0x7fffffe00000 fully covering the wanted range.) The guest
	// used to exit(1) on EEXIST -- killing whatever was being launched (e.g. a
	// clang/test under `make check`) and failing the build.
	//
	// We must NOT force this with MAP_FIXED: that would unmap the running native
	// stack. But the guest stack does not have to live at this exact address --
	// lr->stack_top is only used as the initial %rsp and reported via
	// KERN_USRSTACK, both of which work with any valid address. So: try the
	// preferred spot first (keeps the historical layout in the common case), and
	// if it is occupied, let the kernel pick any free region for the stack.
	unsigned long stack_top = 0;
	void* stack = mldr_map_guest_stack(compatible_mmap, preferred_top, size, &stack_top);
	if (stack == MAP_FAILED) {
		fprintf(stderr, "Failed to allocate stack of %lu bytes: %d (%s)\n", size, errno, strerror(errno));
		exit(1);
	}

	// stack_top is the high end of whatever region we actually got.
	lr->stack_top = stack_top;

	unset_special_env();

	lr->kernfd = __mldr_create_rpc_socket();
	if (lr->kernfd < 0) {
		fprintf(stderr, "Failed to create socket\n");
		exit(1);
	}

	__dserver_main_thread_socket_fd = lr->kernfd;

	int lifetime_pipe[2];

	// this process is created using exec from another Darling process.
	// darlingserver should already have the read pipe, so we don't need
	// to check that in.
	if (lr->lifetime_pipe != -1) {
		lifetime_pipe[1] = socket_bitmap_get(&socket_bitmap);

		if (lr->lifetime_pipe != lifetime_pipe[1]) {
			// move the existing pipe to a higher fd number, and invalidate
			// the old fd to prevent interfering with fds provided by
			// socket_bitmap_get
			if (dup2(lr->lifetime_pipe, lifetime_pipe[1]) == -1) {
				fprintf(stderr, "Failed to dup process lifetime pipe: %d (%s)\n", errno, strerror(errno));
				exit(1);
			}
			close(lr->lifetime_pipe);
		}

		lifetime_pipe[0] = -1;
	} else {
		if (__mldr_create_process_lifetime_pipe(lifetime_pipe) == -1) {
			fprintf(stderr, "Failed to create process lifetime pipe: %d (%s)\n", errno, strerror(errno));
			exit(1);
		}
	}

	lr->lifetime_pipe = lifetime_pipe[1];

	// store the write end of the pipe; the read end is sent to darlingserver.
	__dserver_process_lifetime_pipe_fd = lifetime_pipe[1];

	int dummy_stack_variable;
	// perf#30 PROCESS-CONTROL PLANE: the plane CAN carry this checkin -- MEASURED, it succeeds through it
	// (status=0; the synthesized call resolves its thread and registers its process) -- but the boot does
	// not proceed afterwards, and what is missing is ORDERING, not correctness: the page is serviced when
	// the server runs its own loop pass, while everything around the checkin (the exec handshake, the
	// first RPCs) travels in socket order. That is the same class the four earlier checkin attempts hit,
	// so the call stays on the datagram path until the plane has an ordering story; the op, the reply
	// suppression, the accept-before-drain fix and the diagnostics stay in the tree for that work.
	// perf#30 PROCESS-CONTROL PLANE: the plane CAN carry this checkin -- MEASURED, status=0, with the
	// synthesized call resolving its thread and registering its process -- but the boot does not proceed
	// afterwards, and neither servicing the page after the socket work nor refreshing the thread's reply
	// address changed that. The missing piece is therefore still open, and the call stays on the datagram
	// path: the op, the reply suppression, the accept-before-drain fix and the diagnostics stay in the
	// tree for that work.
	// perf#30 LOADER CHECKIN ORDERING (the untried option, doc section 165): this checkin does NOT happen
	// here. `setup_space` runs inside `load()`, which is called by `main` BEFORE the process-control plane is
	// established, so at this point the page can never be transport-ready and the route necessarily falls
	// back to the datagram -- which is the whole of the remaining per-thread RPC socket traffic
	// (`reason=checkin` x2).
	//
	// Moving the ESTABLISHMENT earlier was measured RED twice (two different positions, same outcome), so the
	// fix is the other direction: keep everything here that does not need a registered process, record what
	// the deferred checkin needs, and let `main` perform it AFTER the establishment and BEFORE the dependent
	// bootstrap traffic. The dependent suffix (the vchroot-path lookup, which needs the process this checkin
	// registers) moves with it, as `finish_space_after_checkin`.
	//
	// The inputs are recorded as VALUES, never as a pointer into this frame: this function returns before the
	// deferred checkin runs, so `&dummy_stack_variable` would dangle.
	g_pending_checkin.lifetime_read_fd = lifetime_pipe[0];
	g_pending_checkin.stack_hint = (uintptr_t)(void*)&dummy_stack_variable;
	g_pending_checkin.arch_bits =
#if defined(__x86_64__)
		mldr_load_results._32on64 ? 1u : 2u;
#else
		4u;
#endif
	g_pending_checkin.needed = true;
}

// The suffix of `setup_space` that DEPENDS on a successful checkin: the vchroot path is served by the server
// for a process that this checkin registers, so it must run after it. Nothing else in `setup_space` depends on
// the checkin, and nothing here is needed by the pre-checkin work -- which is why the split is exactly here.
// perf#30 (doc section 2 02): the vchroot root is a property of the PROCESS, discovered once (from the
// environment by the first image, or by querying the server), and every later image of the same process needs it
// before its dylinker load. MEASURED: the second incarnation asked the server and got an EMPTY path -- the server
// answers from `process->vchrootPath()`, which the guest image sets later (op 8 is the setter), so the loader asked
// a question whose answer did not exist yet and then built the unprefixed guest path for /usr/lib/dyld. Remembering
// the value removes the ordering cycle instead of rearranging it.
static char g_mldr_root_path[4096];
static size_t g_mldr_root_path_length = 0;

static void finish_space_after_checkin(struct load_results* lr) {
	if (lr->root_path && lr->root_path_length > 0 && g_mldr_root_path_length == 0) {
		size_t n = lr->root_path_length;
		if (n < sizeof(g_mldr_root_path)) {
			memcpy(g_mldr_root_path, lr->root_path, n);
			g_mldr_root_path[n] = '\0';
			g_mldr_root_path_length = n;
		}
	}
	if (!lr->root_path && g_mldr_root_path_length > 0) {
		lr->root_path = g_mldr_root_path;
		lr->root_path_length = g_mldr_root_path_length;
	}
	// perf#30 (doc section 202): PUBLISH the discovered root into the ENVIRONMENT. A loader static cannot carry it
	// across guest images -- the loader's state is re-established per image (measured: the page and the sequence
	// counter both restart) -- while the environment does survive, and this is the very variable the loader already
	// reads for an image whose root it does not know. MEASURED: the second incarnation got root=(null) and built
	// the unprefixed guest path for /usr/lib/dyld, because nothing carried the first image's discovery forward.
	if (lr->root_path && lr->root_path_length > 0) {
		setenv("__mldr_DYLD_ROOT_PATH", lr->root_path, 1);
	}
	if (!lr->root_path) {
		static char vchroot_buffer[4096];
		uint64_t vchroot_path_length = 0;
		size_t _vchroot_probe_len = 0;

		// perf#30 (doc section 202, directive section 12): the vchroot lookup goes over the MANAGEMENT PLANE when
		// it is available. MEASURED: this call used the RPC path, which needs the thread's socket -- and the
		// second incarnation therefore got no root at all (root=(null)), so the dylinker load below built an
		// unprefixed guest path and `open("/usr/lib/dyld")` failed with ENOENT. The plane's vchroot op takes the
		// buffer's ADDRESS and size and runs the ordinary call, so the semantics are the same one implementation;
		// only the transport differs, which is exactly what this migration is for.
		int code;
		if (__mldr_process_control_ready()) {
			// The architecture byte is part of the plane's envelope for this op, and MEASURED: sending 0
			// (dserver_rpc_architecture_invalid) made the server answer -EINVAL, because the enum is
			// invalid=0, i386=1, x86_64=2. Derived from the pointer size so the 32-bit pass reports i386.
			const uint64_t arch = (sizeof(void*) == 8) ? 2u : 1u;
			code = (int)__mldr_process_control_request(DSERVER_PROCESS_CONTROL_OP_VCHROOT_PATH,
				(uint64_t)(uintptr_t)vchroot_buffer, (uint64_t)sizeof(vchroot_buffer), 0, arch);
			if (code >= 0) {
				// perf#30 (doc section 202): the LENGTH may arrive as this op's status, as the ordinary call's
				// return, or not at all -- and the path itself is written into this buffer by the server through
				// the process's memory interface, which is the channel the call's own comment names. MEASURED:
				// relying on the status alone left the root unset (root=(null)) and the dylinker load below
				// opened the unprefixed guest path. The buffer is the source of truth: it is NUL-terminated by
				// the server, so its length is measurable here.
				_vchroot_probe_len = 0;
				while (_vchroot_probe_len < sizeof(vchroot_buffer) && vchroot_buffer[_vchroot_probe_len] != '\0') {
					++_vchroot_probe_len;
				}
				vchroot_path_length = (_vchroot_probe_len > 0) ? (uint64_t)_vchroot_probe_len : (uint64_t)code;
			}
		} else {
			code = dserver_rpc_vchroot_path(vchroot_buffer, sizeof(vchroot_buffer), &vchroot_path_length);
		}
		if (code < 0) {
			fprintf(stderr, "Failed to retrieve vchroot path from darlingserver: %d\n", code);
			exit(1);
		}

		if (vchroot_path_length >= sizeof(vchroot_buffer)) {
			fprintf(stderr, "Vchroot path is too large for buffer\n");
			exit(1);
		} else if (vchroot_path_length > 0) {
			lr->root_path = vchroot_buffer;
			lr->root_path_length = vchroot_path_length;
			// remember it for the process's later images
			if (vchroot_path_length < sizeof(g_mldr_root_path)) {
				memcpy(g_mldr_root_path, vchroot_buffer, vchroot_path_length);
				g_mldr_root_path[vchroot_path_length] = '\0';
				g_mldr_root_path_length = vchroot_path_length;
			}
		}
	}
}

// perf#30 LOADER CHECKIN ORDERING (doc section 165): the ONE place that performs establishment -> checkin ->
// vchroot, at the first point where all three are possible. Called from the dylinker load (which needs the
// registration) and again from `main` (which needs the vchroot path before the dependent bootstrap traffic); the
// guard makes the second call a no-op, so the order is established once and cannot drift.
static bool g_bootstrap_done = false;

void mldr_bootstrap_before_dylinker_load(struct load_results* lr) {
	if (g_bootstrap_done) {
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] bootstrap SKIPPED pid=%d (already done)\n", (int)getpid());
		}
		return;
	}
	g_bootstrap_done = true;
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] bootstrap-root t=%llu pid=%d root=%s rootlen=%zu\n", mldr_diag_now_ms(),
			(int)getpid(), (lr && lr->root_path) ? lr->root_path : "(null)",
			(lr && lr->root_path) ? strlen(lr->root_path) : (size_t)0);
	}
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] bootstrap BEGIN t=%llu pid=%d pending=%d image=%s\n", mldr_diag_now_ms(), (int)getpid(),
			g_pending_checkin.needed ? 1 : 0,
			__mldr_diag_image(mldr_load_results.argv != NULL ? mldr_load_results.argv[0] : NULL));
	}
	// Establish the process-control plane here: this is EARLIER than `main`'s block (which comes after `load()`
	// returns) and LATER than the two positions measured RED (above `load()`, and inside setup_space), because
	// the outer image is already mapped at this point.
	if (__mldr_process_control_create() == 0) {
		// MEASURED: the PING here STALLS the second incarnation. The hook is reached with a page that was just
		// created and sent, and the PING waits for a completion that does not arrive for it -- so the
		// establishment never finishes, the deferred checkin is never reached, and the boot goes silent with no
		// launchd at all (`[mldr-ctl] bootstrap BEGIN ... pending=1` and then nothing).
		//
		// The PING's purpose is perf#28d's generation teaching before the first attach. It is NOT required for
		// the checkin, and the checkin is what the boot needs here: it registers the process and its tid travels
		// on it. So the wait for transport readiness stays (bounded) and the PING moves back to `main`'s block,
		// which runs after `load()` -- where it has always been observed to complete.
		__mldr_process_control_wait_ready(200);
	}
	// ORDER: the semantic checkin first (it registers the process), then the suffix that needs the registration.
	mldr_do_pending_checkin();
	finish_space_after_checkin(lr);
}

// The deferred checkin, performed by `main` after the process-control establishment and before any dependent
// bootstrap traffic. The SEMANTIC half travels on the management plane; the lifetime descriptor's half travels
// on the process courier with its token in the payload, exactly as before -- an ordinary RPC socket is not
// involved in either half.
static void mldr_do_pending_checkin(void) {
	if (!g_pending_checkin.needed) {
		// A silent no-op here is indistinguishable from "the checkin ran and failed", which is exactly the
		// class of unanswerable instrument this work keeps recording. Say which it is.
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] deferred-checkin SKIPPED pid=%d (no pending context: setup_space did not run,"
				" which is the expect_dylinker path)\n", (int)getpid());
		}
		return;
	}
	int checkin_status = -1;
	uint64_t checkin_token = 0;
	// perf#30 (doc section 171): the second incarnation stops somewhere between creating/sending its page and
	// publishing its checkin, and the only work on that path which can block is the descriptor send -- a
	// blocking `sendmsg` on the courier socket, done BEFORE the publish, which is exactly why the server logs no
	// request for it. These two lines make the two halves of that step distinguishable in one run.
	if (g_pending_checkin.lifetime_read_fd >= 0) {
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] checkin-token BEGIN pid=%d fd=%d\n", (int)getpid(),
				g_pending_checkin.lifetime_read_fd);
		}
		checkin_token = __mldr_fd_courier_send_token(g_pending_checkin.lifetime_read_fd,
			DSERVER_FD_COURIER_KIND_CHECKIN_FD);
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] checkin-token DONE pid=%d fd=%d token=%llx\n", (int)getpid(),
				g_pending_checkin.lifetime_read_fd, (unsigned long long)checkin_token);
		}
	}
	// ORDERING, not just position: the guest waits for the COMPLETION before returning, so the registration
	// this call performs is visible before the bootstrap traffic that follows it is sent.
	if (__mldr_process_control_ready()) {
		if (mldr_diag_on()) {
			mldr_diagf("[mldr-ctl] checkin-publish BEGIN t=%llu pid=%d tid=%d token=%llx stack=%llx\n",
				mldr_diag_now_ms(), (int)getpid(), (int)gettid(), (unsigned long long)checkin_token,
				(unsigned long long)g_pending_checkin.stack_hint);
		}
		checkin_status = __mldr_process_control_request(DSERVER_PROCESS_CONTROL_OP_CHECKIN,
			(uint64_t)g_pending_checkin.arch_bits | ((uint64_t)0u << 8),
			(uint64_t)gettid(),
			checkin_token,
			(uint64_t)g_pending_checkin.stack_hint);
	} else {
		// The establishment is guaranteed to precede this point, so this path is a genuine transport failure
		// rather than the expected route. Kept because losing a boot entirely is worse than one socket.
		checkin_status = dserver_rpc_checkin(false, (void*)g_pending_checkin.stack_hint,
			g_pending_checkin.lifetime_read_fd);
	}
	if (mldr_diag_on()) {
		mldr_diagf("[mldr-ctl] deferred-checkin t=%llu pid=%d seq=%u status=%d ready=%d image=%s\n",
			mldr_diag_now_ms(), (int)getpid(), __mldr_diag_seq(), checkin_status, __mldr_process_control_ready(),
			__mldr_diag_image(mldr_load_results.argv != NULL ? mldr_load_results.argv[0] : NULL));
		// perf#30 (doc section 176): the SAME line on fd 1. MEASURED: the server completes this exact checkin
		// (`checkin-reply status=0`) and the loader's own next line never appears -- because the incarnation
		// that is being loaded (launchd) installs /dev/null as its stderr (doc section 150), so fd 2 stops
		// being the run log's stream. A diagnostic on a stream the observer does not hold is the
		// instrument-that-cannot-answer class again, and this is the cheap way to show it: one line, two fds.
		fprintf(stdout, "[mldr-ctl] deferred-checkin-stdout pid=%d seq=%u status=%d ready=%d\n",
			(int)getpid(), __mldr_diag_seq(), checkin_status, __mldr_process_control_ready());
		fflush(stdout);
	}
	if (checkin_status < 0) {
		fprintf(stderr, "Failed to checkin with darlingserver\n");
		exit(1);
	}
	// keep our write end while closing the unused read end.
	__mldr_close_process_lifetime_pipe(g_pending_checkin.lifetime_read_fd);
	g_pending_checkin.needed = false;
}

static void start_thread(struct load_results* lr) {
#ifdef __x86_64__
	__asm__ volatile(
		"mov %1, %%rsp\n"
		"jmpq *%0"
		::
		"m"(lr->entry_point),
		"r"(lr->stack_top)
		:
	);
#elif defined(__i386__)
	__asm__ volatile(
		"mov %1, %%esp\n"
		"jmp *%0"
		::
		"m"(lr->entry_point),
		"r"(lr->stack_top)
		:
	);
#elif defined(__arm__)
	__asm__ volatile(
		"mov sp, %1\n"
		"bx %0"
		::
		"r"(lr->entry_point),
		"r"(lr->stack_top)
		:
	);
#else
#       error Unsupported platform!
#endif
};

static bool is_kernel_at_least(int major, int minor) {
	if (kernel_major == -1) {
		struct utsname uname_info;
		if (uname(&uname_info) == -1) {
			return false;
		}
		kernel_major = 0;
		kernel_minor = 0;
		size_t pos = 0;
		while (uname_info.release[pos] != '\0' && uname_info.release[pos] != '.') {
			kernel_major = kernel_major * 10 + uname_info.release[pos] - '0';
			++pos;
		}
		++pos;
		while (uname_info.release[pos] != '\0' && uname_info.release[pos] != '.') {
			kernel_minor = kernel_minor * 10 + uname_info.release[pos] - '0';
			++pos;
		}
	}

	if (major != kernel_major) {
		return kernel_major > major;
	}

	return kernel_minor >= minor;
}

void* compatible_mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset) {
	// MAP_FIXED_NOREPLACE is not supported on WSL1 (Linux < 4.17).
	bool fixed_noreplace_hack = false;
	if ((flags & MAP_FIXED_NOREPLACE) && !is_kernel_at_least(4, 17)) {
		flags &= ~MAP_FIXED_NOREPLACE;
		fixed_noreplace_hack = true;
	}
	void* result = mmap(addr, length, prot, flags, fd, offset);
	// MAP_GROWSDOWN is not supported on WSL1. See https://github.com/microsoft/WSL/issues/8095.
	if ((result == (void*)MAP_FAILED) && (flags & MAP_GROWSDOWN) && (errno == EOPNOTSUPP)) {
		result = mmap(addr, length, prot, (flags & ~MAP_GROWSDOWN), fd, offset);
	}
	if (fixed_noreplace_hack) {
		if (result != addr && result != (void*)MAP_FAILED) {
			errno = ESRCH;
			munmap(addr, length);
			return MAP_FAILED;
		}
	}
	return result;
}

static void vchroot_unexpand_interpreter(struct load_results* lr) {
	static char unexpanded[4096];
	size_t length;

	if (lr->root_path) {
		length = strlen(lr->argv[0]);

		if (strncmp(lr->argv[0], lr->root_path, lr->root_path_length) == 0) {
			memmove(unexpanded, lr->argv[0] + lr->root_path_length, length - lr->root_path_length + 1);
		} else {
			// FIXME: potential buffer overflow
			memmove(unexpanded + sizeof(SYSTEM_ROOT) - 1, lr->argv[0], length + 1);
			memcpy(unexpanded, SYSTEM_ROOT, sizeof(SYSTEM_ROOT) - 1);
		}

		lr->argv[0] = unexpanded;
	}
};

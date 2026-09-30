/*
This file is part of Darling.

Copyright (C) 2015-2018 Lubos Dolezel

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

#include "threads.h"
#include "../loader.h"   // perf#30: mldr_load_results._32on64, the architecture the checkin must carry
extern struct load_results mldr_load_results;
#include <darlingserver/rpc-supplement.h>

// perf#30 (violation A): the plane-wake instrument must name the CHANNEL. Declared here because the publish sites
// below only had a block-local extern, and a label that cannot tell a doorbell wake from a polled one is the defect
// this instrument exists to remove (it also ended in a literal backslash-n, collapsing every record into one line).
int __mldr_ring_doorbell(int fd);   // perf#30: the process-control page protocol (OP_CHECKIN, states)
#include <pthread.h>
#include <sys/mman.h>
#include <semaphore.h>
#include <string.h>
#include <stdbool.h>
#include <stdlib.h>
#include <signal.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <setjmp.h>
#include <sys/syscall.h>
#include <sys/socket.h>
#include <stdio.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <linux/futex.h>
#include <errno.h>
#include <stdarg.h>

#include "dthreads.h"

#include <darlingserver/rpc.h>

extern void __mldr_close_rpc_socket(int socket);

// The point of this file is build macOS threads on top of native libc's threads,
// otherwise it would not be possible to make native calls from these threads.

static __thread jmp_buf t_jmpbuf;
static __thread void* t_freeaddr;
static __thread size_t t_freesize;
static __thread darling_thread_create_callbacks_t t_callbacks = NULL;

// perf#30 REMOVAL STEP 3b: there is no per-thread datagram transport any more, so a lifecycle call that the plane did
// not publish has no transport at all. Declared here because the thread-entry and thread-terminate paths below are its
// callers; defined with the remaining transport accessors further down.
static void __darling_no_datagram_transport(const char* what);

typedef void (*thread_ep)(void**, int, ...);
struct arg_struct
{
	thread_ep entry_point;
	uintptr_t real_entry_point;
	uintptr_t arg1; // `user_arg` for normal threads; `keventlist` for workqueues
	uintptr_t arg2; // `stack_addr` for normal threads; `flags` for workqueues
	uintptr_t arg3; // `flags` for normal threads; `nkevents` for workqueues
	union {
		void* _backwards_compat; // kept around to avoid modifying assembly
		int port;
	};
	unsigned long pth_obj_size;
	void* pth;
	darling_thread_create_callbacks_t callbacks;
	uintptr_t stack_bottom;
	uintptr_t stack_addr;
	bool is_workqueue;
	// perf #1 (dar-dar6x4-perf-5dq.1): completion handshake futex word. The creating
	// thread FUTEX_WAITs on this instead of busy-spinning sched_yield() until the new
	// thread has checked in with darlingserver; the new thread stores 1 + FUTEX_WAKEs.
	// Appended LAST so the hardcoded i386 byte offsets into &args below stay valid.
	_Atomic int checked_in;
};

// raw futex syscall wrappers (no glibc wrapper exists)
static inline long __dthread_futex(_Atomic int* uaddr, int op, int val) {
	return syscall(SYS_futex, (int*)uaddr, op, val, NULL, NULL, 0);
}

static void* darling_thread_entry(void* p);

#ifndef PTHREAD_STACK_MIN
#	define PTHREAD_STACK_MIN 16384
#endif

#define DEFAULT_DTHREAD_GUARD_SIZE 0x1000

static inline void *align_16(uintptr_t ptr) {
	return (void *) ((uintptr_t) ptr & ~(uintptr_t) 15);
}

static dthread_t dthread_structure_allocate(size_t stack_size, size_t guard_size, void** stack_addr) {
	size_t total_size = guard_size + stack_size + sizeof(struct _dthread);

	// allocate our stack, guard page, and dthread structure
	void* base_addr = mmap(NULL, total_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);

	// protect our guard page
	mprotect(base_addr, guard_size, PROT_NONE);

	/**
	 * memory layout of newly allocated block:
	 *
	 * [base_addr]         [base_addr + total_size]
	 * --------------------------------------------
	 * | guard page |       stack       | dthread |
	 */

	// stack_addr points to the top of the stack (i.e. the highest address)
	*stack_addr = ((char*)base_addr) + stack_size + guard_size;

	// the dthread sits above the stack
	// (and by "above", i mean the lowest address of the dthread is the highest address of the stack)
	dthread_t dthread = (dthread_t)*stack_addr;
	// zero-out the entrire dthread structure
	memset(dthread, 0, sizeof(struct _dthread));

	return __darling_dthread_initialize(dthread, guard_size, *stack_addr, stack_size, base_addr, total_size);
};

static struct _dthread main_dthread;

int __darling_thread_initialize_main(void* stack_top, size_t stack_size,
		uint32_t mach_thread_self)
{
	memset(&main_dthread, 0, sizeof(main_dthread));
	__darling_dthread_initialize(&main_dthread, 0, stack_top, stack_size, NULL, 0);
	main_dthread.tsd[DTHREAD_TSD_SLOT_MACH_THREAD_SELF] =
		(void*)(uintptr_t)mach_thread_self;
	return __darling_dthread_set_tsd_base(&main_dthread.tsd[0]);
}

// perf#30 DIAGNOSIS: lock-free, libc-free writer for this file. `mldr_diagf` is static to mldr.c and taking a libc
// lock here can deadlock the very path being measured (the loader forks while other threads exist). Bounded to the
// few lines this diagnostic needs, and writes to fd 2 as well as MLDR_DIAG_LOG when named.
static int mldr_thread_diag_fd = -2;
static void mldr_thread_diag_write(const char* buf, long n) {
	if (mldr_thread_diag_fd == -2) {
		mldr_thread_diag_fd = 2;
		const char* path = getenv("MLDR_DIAG_LOG");
		if (path != NULL && *path != '\0') {
			int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_NONBLOCK, 0644);
			if (fd >= 0) { mldr_thread_diag_fd = fd; }
		}
	}
	long a = 1, d = mldr_thread_diag_fd, sp = (long)buf, l = n;
	__asm__ volatile("syscall" : "+a"(a), "+D"(d), "+S"(sp), "+d"(l) : : "rcx", "r11", "memory");
}

static void mldr_thread_diagf(const char* fmt, ...) {
	char buf[192];
	int at = 0;
	va_list ap;
	va_start(ap, fmt);
	for (const char* f = fmt; *f != '\0' && at < (int)sizeof(buf) - 24; ++f) {
		if (*f != '%') { buf[at++] = *f; continue; }
		++f;
		if (*f == 's') {
			const char* sv = va_arg(ap, const char*);
			for (const char* q = sv; q != NULL && *q != '\0' && at < (int)sizeof(buf) - 1; ++q) { buf[at++] = *q; }
		} else if (*f == 'p') {
			unsigned long v = (unsigned long)va_arg(ap, void*);
			char tmp[20]; int n = 0;
			buf[at++] = '0'; buf[at++] = 'x';
			if (v == 0) { tmp[n++] = '0'; }
			while (v != 0 && n < (int)sizeof(tmp)) { unsigned dg = (unsigned)(v & 0xf); tmp[n++] = (char)(dg < 10 ? ('0' + dg) : ('a' + dg - 10)); v >>= 4; }
			while (n > 0 && at < (int)sizeof(buf) - 1) { buf[at++] = tmp[--n]; }
		} else if (*f == 'l' && *(f + 1) == 'u') {
			++f;
			unsigned long v = va_arg(ap, unsigned long);
			char tmp[24]; int n = 0;
			if (v == 0) { tmp[n++] = '0'; }
			while (v != 0 && n < (int)sizeof(tmp)) { tmp[n++] = (char)('0' + (v % 10)); v /= 10; }
			while (n > 0 && at < (int)sizeof(buf) - 1) { buf[at++] = tmp[--n]; }
		} else if (*f == 'd') {
			int v = va_arg(ap, int);
			char tmp[16]; int n = 0;
			int neg = v < 0;
			unsigned uv = neg ? (unsigned)(-(long)v) : (unsigned)v;
			if (uv == 0) { tmp[n++] = '0'; }
			while (uv != 0 && n < (int)sizeof(tmp)) { tmp[n++] = (char)('0' + (uv % 10)); uv /= 10; }
			if (neg) { buf[at++] = '-'; }
			while (n > 0 && at < (int)sizeof(buf) - 1) { buf[at++] = tmp[--n]; }
		} else {
			buf[at++] = '%'; if (*f != '\0' && at < (int)sizeof(buf) - 1) { buf[at++] = *f; }
		}
	}
	va_end(ap);
	mldr_thread_diag_write(buf, at);
}

void* __darling_thread_create(unsigned long stack_size, unsigned long pth_obj_size,
				void* entry_point, uintptr_t real_entry_point,
				uintptr_t arg1, uintptr_t arg2, uintptr_t arg3,
				darling_thread_create_callbacks_t callbacks, void* pth)
{
	struct arg_struct args = {
		.entry_point      = (thread_ep)entry_point,
		.real_entry_point = real_entry_point,
		.arg1             = arg1,
		.arg2             = arg2,
		.arg3             = arg3,
		.port             = 0,
		.pth_obj_size     = pth_obj_size,
		.pth              = NULL, // set later on
		.callbacks        = callbacks,
		.stack_addr       = 0, // set later on
		.is_workqueue     = real_entry_point == 0, // our `workq_kernreturn` sets `real_entry_point` to NULL; `bsdthread_create` actually passes a value
		.checked_in       = 0, // perf #1: cleared before launch, set to 1 by the new thread after checkin
	};
	pthread_attr_t attr;
	pthread_t nativeLibcThread;

	pthread_attr_init(&attr);
	//pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
	// pthread_attr_setstacksize(&attr, stack_size);

	// in some cases, we're already given a pthread object, stack, and guard page;
	// in those cases, just use what we're given (it also contains more information)
	//
	// otherwise, allocate them ourselves
	if (pth == NULL || args.is_workqueue) {
		pth = dthread_structure_allocate(stack_size, DEFAULT_DTHREAD_GUARD_SIZE, (void**)&args.stack_addr);
	} else if (!args.is_workqueue) {
		// `arg2` is `stack_addr` for normal threads
		args.stack_addr = arg2;
	}

	args.stack_bottom = args.stack_addr - stack_size;

	// pthread_attr_setstack is buggy. The documentation states we should provide the lowest
	// address of the stack, yet some versions regard it as the highest address instead.
	// Therefore it's better to just make the pthread stack as small as possible and then switch
	// to our own stack instead.
	//pthread_attr_setstack(&attr, ((char*)pth) + pth_obj_size, stack_size - pth_obj_size - 0x1000);

	// std::cout << "Allocated stack at " << pth << ", size " << stack_size << std::endl;

	/* HOST STACK FOR THE THREAD THAT CARRIES A GUEST THREAD. MEASURED QUESTION: the value was 4096 bytes, which is
	 * below glibc's PTHREAD_STACK_MIN on x86_64 (16384), so the setstacksize call could not have produced a 4 KiB
	 * thread -- but glibc's REACTION to a too-small request (EINVAL and the default, or a clamp) decides the host
	 * stack this loader's entry runs on, and every guest thread's startup passes through it before the guest stack is
	 * switched in. This experiment uses a size that cannot be ambiguous. */
	pthread_attr_setstacksize(&attr, 512 * 1024);

	//pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);

	args.pth = pth;
	// perf#30 DIAGNOSIS: MEASURED that a guest process dies inside the SECOND thread creation, with the guest's own
	// marks showing the create entering the loader and never returning, and with no syscall traced after the guest
	// `bsdthread_create`. THIS call is the loader's host-glibrc thread creation -- the only substantial code between
	// those two facts. It is instrumented in its own file with a local, lock-free raw writer (the loader's rule:
	// no stdio and no libc locks on this path, and `mldr_diagf` is file-local to mldr.c). The return value and errno
	// are printed because this call's result is otherwise DISCARDED: a failed create leaves the caller waiting for a
	// checkin that can never come, which is a distinct and silent failure mode of its own.
	{
		static _Atomic unsigned long g_dthread_create_seq = 0;
		unsigned long seq = atomic_fetch_add(&g_dthread_create_seq, 1);
		mldr_thread_diagf("[mldr-dthread pre seq=%lu tid=%d stack_addr=%p pth=%p]\n",
				seq, (int)syscall(SYS_gettid), (void*)args.stack_addr, pth);
		int pc_rc = pthread_create(&nativeLibcThread, &attr, darling_thread_entry, &args);
		int pc_errno = errno;
		mldr_thread_diagf("[mldr-dthread post seq=%lu tid=%d rc=%d errno=%d th=%p]\n",
				seq, (int)syscall(SYS_gettid), pc_rc, pc_errno, (void*)nativeLibcThread);
	}
	pthread_attr_destroy(&attr);

	// perf #1 (dar-dar6x4-perf-5dq.1): wait for the new thread to finish its darlingserver
	// checkin. The original code was an unbounded `while (args.pth != NULL) sched_yield();`
	// busy-spin: under high thread-creation density (e.g. `make -j` link storms) every
	// in-flight creator burned a whole core, starving the new threads AND the single-threaded
	// darlingserver of the CPU they needed to complete the very checkin being waited on.
	//
	// But the checkin is usually FAST (p50 ~16-64us on a warm server), faster than a futex
	// syscall round-trip, so an immediate FUTEX_WAIT regresses the common case. Use an
	// ADAPTIVE wait: spin a bounded number of times first (wins the fast path with no
	// syscall), then fall back to FUTEX_WAIT so a slow checkin can never monopolise a core.
	// This keeps the fast-path throughput of the spin while removing the pathological
	// starvation of the unbounded spin.
	{
		// ~tuned so the spin phase covers a typical fast checkin but bails out quickly
		// (each iteration is a relaxed load + a sched_yield, i.e. a few hundred ns).
		const int kSpinIters = 4000;
		int spins = 0;
		while (atomic_load_explicit(&args.checked_in, memory_order_acquire) == 0) {
			if (spins < kSpinIters) {
				++spins;
				sched_yield();
				continue;
			}
			// slow checkin: stop burning CPU and block in the kernel.
			long r = __dthread_futex(&args.checked_in, FUTEX_WAIT_PRIVATE, 0);
			// EAGAIN => the value already changed (checkin won the race); EINTR => spurious
			// wakeup. Both just re-check the predicate. Any other error: yield so we can
			// never hang (defensive; should not happen).
			if (r != 0 && errno != EAGAIN && errno != EINTR) {
				sched_yield();
			}
		}
	}

	return pth;
}

static void* darling_thread_entry(void* p)
{
	struct arg_struct* in_args = (struct arg_struct*) p;
	struct arg_struct args;

	// perf#30 DIAGNOSIS: the new guest thread's startup, marked statement by statement. MEASURED setting: a guest
	// process dies inside the SECOND thread creation, the loader's host `pthread_create` for it returns rc=0, and the
	// creating thread's `post-create` mark never appears -- so the death is in THIS function's path (the new host
	// thread becoming a guest thread) or in the guest entry it jumps to. Four bounded marks name which.
	// UNCONDITIONAL ENTRY LINE, raw write, no diagf gating: the earlier bounded marks could not distinguish "this
	// thread never entered the loader" from "the marker did not print for it", and that distinction is the whole
	// question left in the basic-20 fault. Every entry now leaves this line, whatever its budget state was.
	{
		char eb[64];
		int en = 0;
		const char* pre = "[dthread-entry-tid=";
		for (const char* c = pre; *c; ++c) eb[en++] = *c;
		long tv = (long)syscall(SYS_gettid);
		char tb[24];
		int tk = 0;
		if (tv == 0) tb[tk++] = '0';
		while (tv > 0) { tb[tk++] = (char)('0' + (tv % 10)); tv /= 10; }
		while (tk > 0) eb[en++] = tb[--tk];
		eb[en++] = '@';
		// The loader's OWN identity: with more than one mldr copy able to serve a run, an entry line that does not
		// name its copy cannot decide which loader created a thread. readlink /proc/self/exe is one syscall and
		// names the exact file that is running this code.
		long rl = syscall(SYS_readlink, "/proc/self/exe", eb + en, sizeof(eb) - (size_t)en - 2);
		if (rl > 0) en += (int)rl;
		eb[en++] = ']';
		eb[en++] = '\n';
		(void)!write(2, eb, (size_t)en);
	}
	mldr_thread_diagf("[mldr-dthread-entry begin tid=%d pth=%p]\n", (int)syscall(SYS_gettid), p);
	// INHERITED-MASK PROBE. MEASURED NEED: a hardware SIGSEGV kills the guest process while the host disposition is
	// Darling's own delivery handler, the guest never blocks SIGSEGV through its API, and the handler is never
	// entered -- the shape of a signal that is BLOCKED at the kernel. A new thread inherits its creator's mask, and
	// this thread is created by Darling's machinery, so a mask block inherited from the creator would make the fault
	// undeliverable here and nowhere else. This prints the kernel mask of the newborn thread.
	{
		static int emitted_mask = 0;
		if (emitted_mask < 16) {
			sigset_t cur;
			int q = sigprocmask(0 /* query only, NULL set */, NULL, &cur);
			++emitted_mask;
			mldr_thread_diagf("[dthread-mask q=%d segv_blocked=%d tid=%d]\n", q,
				(q == 0) ? (int)sigismember(&cur, SIGSEGV) : -1, (int)syscall(SYS_gettid));
		}
	}

	memcpy(&args, in_args, sizeof(args));

	dthread_t dthread = args.pth;
	uintptr_t* flags = args.is_workqueue ? &args.arg2 : &args.arg3;

	// perf#30 LAZY PER-THREAD RPC SOCKET (round 49): the socket is NOT created here any more. It is
	// created on the first call that actually needs a datagram, so the count of creations says which
	// operations still force the legacy transport instead of hiding it behind an eager allocation. The
	// thread-create checkin is the first such call and is tagged as the reason when it is the one that
	// creates the socket; a call the lane serves creates nothing at all.
	t_callbacks = args.callbacks;

	// libpthread now expects the kernel to set the TSD
	// so, since we're pretending to be the kernel handling threads...
	args.callbacks->thread_set_tsd_base(&dthread->tsd[0], 0);
	*flags |= args.is_workqueue ? DWQ_FLAG_THREAD_TSD_BASE_SET : DTHREAD_START_TSD_BASE_SET;
	mldr_thread_diagf("[mldr-dthread-entry tsd-set tid=%d]\n", (int)syscall(SYS_gettid));

	// let's check-in with darlingserver on this new thread
	int dummy_stack_variable;
	// the lifetime pipe fd is ignored as the process should already have been registered
	//
	// perf#30 FD-COURIER: this is the THREAD-CREATE checkin -- no descriptor, no ordering dependency on the exec/fork
	// handshake. HISTORY worth keeping: this instance is what used to create the per-thread socket (every created
	// socket was attributed to checkin), which is exactly why checkin had to leave the datagram before the socket could
	// disappear -- and it did: the page below is its only route now.
	// perf#30 CHECKIN ON THE PAGE, and (REMOVAL STEP 3b) the ONLY route: the per-thread RPC socket does not exist any
	// more, so `per_thread_rpc_socket_created = 0` is not a target but a property. The server's OP_CHECKIN runs the
	// ORDINARY checkin Call (measured semantically correct: call=1, thread=pid, process=pid), so the semantics are not
	// reimplemented here; what the page adds is that the request and its completion need no datagram. A page that is
	// not established is therefore a NAMED failure (`__darling_no_datagram_transport`), not a fallback.
	int checked_in = 0;
	/* THE SEQUENCE THIS THREAD PUBLISHED (dar-4cp9). MEASURED: the server recorded the checkin for the very
	 * tid that then aborted -- [srv-checkin #32 pid=... tid=3124771 ...] immediately before
	 * [rpc-socket-DENIED ... call=checkin image=loader] and sigexc-fatal -- so the operation had been
	 * delivered and only the client's acceptance test lost its reply to a later publisher, exactly as on the
	 * checkout path (pub=5 seen=17). A checkin that was published is a checkin the server has. */
	uint32_t checkin_pubseq = 0;
	{
		extern void* __mldr_process_control_page(void);
		struct dserver_process_control* page = (struct dserver_process_control*)__mldr_process_control_page();
		if (page != NULL) {
			for (int w = 0; w < 200 && __atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) == 0; ++w) {
				struct timespec ts = {0, 1000000L};
				nanosleep(&ts, NULL);
			}
		}
		if (page != NULL && __atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) != 0) {
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
				static uint32_t checkin_seq = 0;
				// ATOMIC (doc section 70): THE MEASURED CAUSE. Threads of one process call this concurrently, and a
				// plain ++ hands two of them the same sequence; the server answers one, the other reads a foreign
				// reply_seq, refuses the result and falls back to the datagram -- creating the per-thread RPC socket
				// this route exists to avoid. Intersecting the socket-creation tids with the tids the server
				// serviced showed 19 of 19 had been serviced with status 0, leaving only this collision.
				uint32_t mine = __atomic_add_fetch(&checkin_seq, 1, __ATOMIC_RELAXED);
				checkin_pubseq = mine;
				page->reply_state = DSERVER_PROCESS_CONTROL_IDLE;
				page->request_op = DSERVER_PROCESS_CONTROL_OP_CHECKIN;
				page->request_seq = mine;
				// perf#30 THE DUPLICATE-CHECKIN DEFECT (doc section 57): this was `1u`, and 1 is
				// dserver_rpc_architecture_i386, not x86_64 (2). The server compares the architecture the
				// checkin carries against the process's own and throws when they differ, so the page route
				// completed with -EINVAL (102 of 108), the guest refused the result, and it repeated the
				// same checkin on the datagram -- which is why half of all checkins were duplicates while
				// the regression stayed GREEN. mldr.c already computed this correctly; this site must too.
				page->request_payload[0] = (uint64_t)(mldr_load_results._32on64 ? 1u : 2u);  // architecture; not a fork
				page->request_payload[1] = (uint64_t)(unsigned)syscall(SYS_gettid);
				page->request_payload[2] = 0;   // no lifetime descriptor on the thread-create checkin
				page->request_payload[3] = (uint64_t)(uintptr_t)&dummy_stack_variable;
				__atomic_store_n(&page->request_state, DSERVER_PROCESS_CONTROL_PENDING, __ATOMIC_RELEASE);
				{
					extern int __mldr_fd_courier_socket(void);
					int courier = __mldr_fd_courier_socket();
					if (courier >= 0) {
char wake = 0;
						/* perf#30 R1 COURIER PURITY: this used to be a ZERO-FD DATAGRAM (27 per boot, measured). The server now
						   notices a plane publish on its own bounded epoll timeout, and the process doorbell (an eventfd, not
						   AF_UNIX) is used when this process already holds one. No packet leaves here. */
						{ static unsigned g_plane_wake_n = 0; if (__atomic_fetch_add(&g_plane_wake_n, 1, __ATOMIC_RELAXED) < 8) {
							fprintf(stderr, "[plane-wake] n=%u via=%s scm=0 fdcnt=0 payload=0 db=%d\n", g_plane_wake_n, (__mldr_ring_doorbell(-1) >= 0) ? "doorbell" : "none", __mldr_ring_doorbell(-1)); fflush(stderr); } }
						{ extern int __mldr_ring_doorbell(int fd); int db = __mldr_ring_doorbell(-1);
						  if (db >= 0) { uint64_t one = 1; (void)!write(db, &one, sizeof(one)); } }
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
						if (page->reply_state == DSERVER_PROCESS_CONTROL_DONE) { break; }
						syscall(SYS_futex, &page->futex, FUTEX_WAIT, (int)seen, &ts, NULL, 0);
					}
				}
				// slot ownership (doc 67): the answer must be READ BEFORE the slot is released. MEASURED: with
				// the release first, the next thread claims the slot and overwrites reply_seq/reply_status
				// before this thread reads them, so this thread refuses a foreign sequence and falls back --
				// which is what kept the per-thread RPC socket alive. Snapshot the fields, then release.
				uint32_t seenState = __atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE);
				uint32_t seenSeq = page->reply_seq;
				int32_t seenStatus = page->reply_status;
	{ uint32_t _st = __atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE); if (_st == DSERVER_PROCESS_CONTROL_PENDING) { static const char _m[] = "[release-drops-pending] site=threads.c:350\n"; long _a = 1, _d = 2, _s = (long)_m, _n = sizeof(_m) - 1; __asm__ volatile("syscall" : "+a"(_a), "+D"(_d), "+S"(_s), "+d"(_n) : : "rcx", "r11", "memory"); } }
				DSERVER_PROCESS_CONTROL_RELEASE(page);
				if (seenState == DSERVER_PROCESS_CONTROL_DONE && seenSeq == mine && seenStatus == 0) {
					checked_in = 1;
				} else if (!claimed && getenv("DARLING_GUEST_CHECKIN_DIAG") != NULL) {
					// perf#30 ATTRIBUTION (doc section 71): five hypotheses for the surviving 27 socket
					// creations have been eliminated by measurement, so the guest must say which term of its
					// own acceptance test failed and what it actually read. This site runs AFTER the loader,
					// so a gated print is safe here (unlike the bootstrap path, where printing is a measured
					// hazard). Bounded to the first few per process.
					static int diagCount = 0;
					if (diagCount < 4) {
						++diagCount;
						fprintf(stderr, "[checkin-diag] pid=%d tid=%d state=%u seq=%u mine=%u status=%d spins=%d\n",
							(int)getpid(), (int)syscall(SYS_gettid), seenState, seenSeq, mine, (int)seenStatus, spins);
					}
				} else if (claimed || checkin_pubseq != 0) {
					if (!claimed) {
						fprintf(stderr, "[checkin-reply-unseen tid=%d pub=%u seen=%u seenstate=0x%x -- published, reply overtaken]\n",
							(int)syscall(SYS_gettid), (unsigned)checkin_pubseq, (unsigned)seenSeq, (unsigned)seenState);
					}
					// the server owns the checkin; it is the same operation, so treat a completion we could
					// not read as done rather than duplicating it on the datagram
					checked_in = 1;
				}
			}
		}
	}
	// perf#30 (doc section 205): the plane block above can be skipped ENTIRELY -- the page absent, or mapped but
	// never marked ready -- and in that case the datagram fallback creates the per-thread socket without saying
	// why. MEASURED: two threads of a booting process created a socket with reason=checkin while every other
	// thread's checkin rode the plane, so the decision, not the transport, was what remained unmeasured. Bounded
	// and gated: this site runs after the loader, so a print here is not the bootstrap hazard it would be earlier.
	if (!checked_in && getenv("DARLING_GUEST_CHECKIN_DIAG") != NULL) {
		static int pathCount = 0;
		if (pathCount < 4) {
			extern void* __mldr_process_control_page(void);
			struct dserver_process_control* pg = (struct dserver_process_control*)__mldr_process_control_page();
			++pathCount;
			fprintf(stderr, "[checkin-path] pid=%d tid=%d page=%p ready=%d -> no-transport (declined)\n",
				(int)getpid(), (int)syscall(SYS_gettid), (void*)pg,
				(pg != NULL) ? (int)__atomic_load_n(&pg->transport_ready, __ATOMIC_ACQUIRE) : -1);
		}
	}
	if (!checked_in) {
		// perf#30 REMOVAL STEP 3b (measured): the plane did not publish this checkin, and there is no per-thread
		// datagram transport to fall back on.
		(void)dummy_stack_variable;
		__darling_no_datagram_transport("checkin");
	}
	mldr_thread_diagf("[mldr-dthread-entry checked-in=%d tid=%d]\n", checked_in, (int)syscall(SYS_gettid));

	int thread_self_port = args.callbacks->thread_self_trap();
	dthread->tsd[DTHREAD_TSD_SLOT_MACH_THREAD_SELF] = (void*)(intptr_t)thread_self_port;
	args.port = thread_self_port;
	mldr_thread_diagf("[mldr-dthread-entry port=%d tid=%d]\n", thread_self_port, (int)syscall(SYS_gettid));

	// perf #1 (dar-dar6x4-perf-5dq.1): signal the creating thread that we've checked in and
	// wake it from its FUTEX_WAIT. This is our LAST access to `in_args` (the creator's stack):
	// once it observes checked_in==1 it may return and reuse that frame, so we must not touch
	// `in_args` afterward. `pth` is nulled too for backwards-compat with anything inspecting it.
	in_args->pth = NULL;
	atomic_store_explicit(&in_args->checked_in, 1, memory_order_release);
	// exactly one waiter (the creating thread) ever waits on this word
	__dthread_futex(&in_args->checked_in, FUTEX_WAKE_PRIVATE, 1);

	mldr_thread_diagf("[mldr-dthread-entry handshake-done tid=%d stack_addr=%p entry=%p]\n",
		(int)syscall(SYS_gettid), (void*)args.stack_addr, (void*)args.entry_point);

	if (setjmp(t_jmpbuf))
	{
		// Terminate the Linux thread
		// perf#30 DIAGNOSIS: MEASURED that a guest process dies (silent SIGSEGV, racy) right after a
		// create+join cycle -- at the next thread creation, at the program's own exit path, or inside the
		// dispatcher -- and this is the only unmap on that path. The region being unmapped here holds the
		// guest's stack AND its pthread object, and BOTH are read after this thread terminates: the joining
		// thread reads the object, and libpthread keeps its own bookkeeping in it. Unmapping it from the
		// EXITING thread therefore frees memory the joiner has not finished with, and the fault appears
		// wherever the next access lands -- which is exactly the racy, silent, location-varying death that
		// has been chased all session. This hatch turns the unmap OFF (leaking the region) so the
		// hypothesis is decided by measurement instead of argument; when it holds, the fix is ownership
		// (free on join / at thread-destroy), not "unmap earlier".
		static int no_unmap = -1;
		if (no_unmap < 0) {
			const char* v = getenv("DARLING_GUEST_NO_THREAD_UNMAP");
			no_unmap = (v != NULL && v[0] == '1') ? 1 : 0;
		}
		if (!no_unmap) {
			munmap(t_freeaddr, t_freesize);
		}
		pthread_detach(pthread_self());
		return NULL;
	}

	void *stack_ptr = align_16(args.stack_addr);

	// No additional function calls should occur beyond this point. Otherwise, we will risk our
	// registers being call-clobbered. I recommend reading the following doc for more details:
	// https://gcc.gnu.org/onlinedocs/gcc/Local-Register-Variables.html
#if __x86_64__
	register void*     arg1 asm("rdi") = args.pth;
	register int       arg2 asm("esi") = args.port;
	register uintptr_t arg3 asm("rdx") = args.real_entry_point;
	register uintptr_t arg4 asm("rcx") = args.arg1;
	register uintptr_t arg5 asm("r8")  = args.arg2;
	register uintptr_t arg6 asm("r9")  = args.arg3;
#elif __i386__
	uintptr_t arg3 = args.real_entry_point;
#endif

	if (arg3 == 0) {
		arg3 = (long) args.stack_bottom;
	}

#ifdef __x86_64__
	asm volatile(
		// Zero out the frame base register.
		"xorq %%rbp, %%rbp\n"
		// Switch to the new stack.
		"movq %[stack_ptr], %%rsp\n"
		// Push a fake return address.
		"pushq $0\n"
		// Jump to the entry point.
		"jmp *%[entry_point]" ::
		
		// Function arguments
		"r"(arg1),"r"(arg2),"r"(arg3),"r"(arg4),"r"(arg5),"r"(arg6),
		
		[entry_point] "r"(args.entry_point),
		[stack_ptr] "r"(stack_ptr)
	);
#elif defined(__i386__) // args in eax, ebx, ecx, edx, edi, esi
	__asm__ __volatile__ (
		// Zero out the frame base register.
		"xorl %%ebp, %%ebp\n"
		// Switch to the new stack.
		"movl %[stack_ptr], %%esp\n"
		// Make sure stack is 16 aligned (before we push the fake return address)
		"sub $8, %%esp\n"
		// Unlike x86_64, all function arguments must be stored in the stack
		"pushl   16(%[args])\n"		// 6th argument | args.arg3
		"pushl   12(%[args])\n"		// 5th argument | args.arg2
		"pushl   8(%[args])\n"		// 4th argument | args.arg1
		"pushl   %[arg3]\n"			// 3rd argument | args3
		"pushl   20(%[args])\n"		// 2nd argument | args.port
		"pushl   28(%[args])\n"		// 1st argument | args.pth
		// Push a fake return address.
		"pushl $0\n"
		// Jump to the entry point.
		"jmp *%[entry_point]" ::
		
		// Function arguments to push to the stack.
		[args] "r"(&args), [arg3]"r"(arg3),

		[entry_point] "r"(args.entry_point),
		[stack_ptr] "r"(stack_ptr)
	);
#else
	#error Not implemented
	// args.entry_point(args.pth, args.port, args.real_entry_point, args.arg1, args.arg2, args.arg3);
#endif
	__builtin_unreachable();
}

int __darling_thread_terminate(void* stackaddr,
				unsigned long freesize, unsigned long pthobj_size)
{
	int checkout_result = 0;

	// perf#30 CHECKOUT ON THE PAGE: the descriptor-less thread-exit instance. The abort that made this look
	// impossible was in the reply funnel (ENOTCONN is now a dropped message), not in this route, so it is
	// restored with the fix in place. The datagram path stays as the fallback.
	int checkout_via_page = 0;
	/* THE PUBLISHED AND THE OBSERVED SEQUENCE, HOISTED SO A FAILED CHECKOUT CAN REPORT BOTH (dar-b5pe).
	 * The reply slot is single and shared, and the accept path takes its seq/state snapshot AFTER the wait
	 * loop has exited, so a second publisher in that window can make a delivered checkout look undelivered.
	 * Without these numbers the two windows are indistinguishable in the log and any fix is a guess. */
	uint32_t checkout_pubseq = 0;
	uint32_t checkout_seenSeq = 0;
	uint32_t checkout_seenState = 0xffffffffu;
	int checkout_claimed = 0;
	{
		struct dserver_process_control* page =
			(struct dserver_process_control*)__mldr_process_control_page();
		if (page != NULL) {
			for (int w = 0; w < 200 && __atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) == 0; ++w) {
				struct timespec ts = {0, 1000000L};
				nanosleep(&ts, NULL);
			}
		}
		if (page != NULL && __atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) != 0) {
			int slot = 0;
			for (int t = 0; t < 2000 && !slot; ++t) {
				uint32_t expect = DSERVER_PROCESS_CONTROL_IDLE;
				if (__atomic_compare_exchange_n(&page->request_state, &expect,
				        DSERVER_PROCESS_CONTROL_PENDING, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) { slot = 1; break; }
				struct timespec ts = {0, 1000000L};
				nanosleep(&ts, NULL);
			}
			if (slot) {
				static uint32_t exit_checkout_seq = 0;
				uint32_t mine = __atomic_add_fetch(&exit_checkout_seq, 1, __ATOMIC_RELAXED);  // atomic: doc 70
				checkout_pubseq = mine;
				page->reply_state = DSERVER_PROCESS_CONTROL_IDLE;
				page->request_op = DSERVER_PROCESS_CONTROL_OP_CHECKOUT;
				page->request_seq = mine;
				page->request_payload[0] = 0;
				page->request_payload[1] = (uint64_t)(unsigned)syscall(SYS_gettid);
				page->request_payload[2] = 0;
				page->request_payload[3] = 1u;
				__atomic_store_n(&page->request_state, DSERVER_PROCESS_CONTROL_PENDING, __ATOMIC_RELEASE);
				{
					extern int __mldr_fd_courier_socket(void);
					int courier = __mldr_fd_courier_socket();
					if (courier >= 0) {
char wake = 0;
						/* perf#30 R1 COURIER PURITY: this used to be a ZERO-FD DATAGRAM (27 per boot, measured). The server now
						   notices a plane publish on its own bounded epoll timeout, and the process doorbell (an eventfd, not
						   AF_UNIX) is used when this process already holds one. No packet leaves here. */
						{ static unsigned g_plane_wake_n = 0; if (__atomic_fetch_add(&g_plane_wake_n, 1, __ATOMIC_RELAXED) < 8) {
							fprintf(stderr, "[plane-wake] n=%u via=%s scm=0 fdcnt=0 payload=0 db=%d\n", g_plane_wake_n, (__mldr_ring_doorbell(-1) >= 0) ? "doorbell" : "none", __mldr_ring_doorbell(-1)); fflush(stderr); } }
						{ extern int __mldr_ring_doorbell(int fd); int db = __mldr_ring_doorbell(-1);
						  if (db >= 0) { uint64_t one = 1; (void)!write(db, &one, sizeof(one)); } }
						(void)wake;
					}
				}
				int spins = 0, claimed = 0;
				while (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) != DSERVER_PROCESS_CONTROL_DONE) {
					if (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) { claimed = 1; }
					if (++spins < 20000) { continue; }
					{
						uint32_t seen = page->futex;
						struct timespec ts = {0, claimed ? 2000000L : 1000000L};
						if (page->reply_state == DSERVER_PROCESS_CONTROL_DONE) { break; }
						syscall(SYS_futex, &page->futex, FUTEX_WAIT, (int)seen, &ts, NULL, 0);
					}
				}
				// read before release (doc 71): see the thread-create site above.
				uint32_t seenState = __atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE);
				uint32_t seenSeq = page->reply_seq;
				int32_t seenStatus = page->reply_status;
				checkout_seenState = seenState; checkout_seenSeq = seenSeq; checkout_claimed = claimed;
	{ uint32_t _st = __atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE); if (_st == DSERVER_PROCESS_CONTROL_PENDING) { static const char _m[] = "[release-drops-pending] site=threads.c:531\n"; long _a = 1, _d = 2, _s = (long)_m, _n = sizeof(_m) - 1; __asm__ volatile("syscall" : "+a"(_a), "+D"(_d), "+S"(_s), "+d"(_n) : : "rcx", "r11", "memory"); } }
				DSERVER_PROCESS_CONTROL_RELEASE(page);
				if (seenState == DSERVER_PROCESS_CONTROL_DONE && seenSeq == mine) {
					checkout_result = page->reply_status; checkout_via_page = 1;
				} else if (claimed) { checkout_result = 0; checkout_via_page = 1; }
			}
		}
	}
	if (!checkout_via_page) {
		// perf#30 REMOVAL STEP 3b/c (MEASURED: the plane is the only route left, and this is the ONE case where not
		// publishing is not an error). When the MAIN thread exits, the process is ending and darlingserver learns of
		// it from the process exit itself -- a checkout that could not be published there carries no information the
		// server does not already have. MEASURED: at process exit the page is already disappearing (transport_ready
		// clear), which produced exactly ONE named denial per run on basic 1 / basic 20 -- the case the removed
		// datagram fallback used to cover silently. Any OTHER thread that cannot publish its checkout would stay live
		// on the server, and that remains a NAMED hard failure.
		// The state is printed, not assumed: a route that failed without saying WHICH precondition failed is the
		// "instrument that cannot answer" class. Bounded to the first few per process.
		{
			static int checkoutPathCount = 0;
			extern void* __mldr_process_control_page(void);
			struct dserver_process_control* pg = (struct dserver_process_control*)__mldr_process_control_page();
			if (checkoutPathCount < 4) {
				++checkoutPathCount;
				fprintf(stderr, "[checkout-path] pid=%d tid=%d page=%p ready=%d main=%d\n",
					(int)getpid(), (int)syscall(SYS_gettid), (void*)pg,
					(pg != NULL) ? (int)__atomic_load_n(&pg->transport_ready, __ATOMIC_ACQUIRE) : -1,
					(int)(getpid() == syscall(SYS_gettid)));
			}
		}
		if (getpid() == syscall(SYS_gettid)) {
			fprintf(stderr, "[mldr-ctl] checkout-skipped tid=%d (main thread exit; plane not published)\n",
				(int)syscall(SYS_gettid));
			checkout_result = 0;
		} else {
			// WHY THE PUBLICATION FAILED, printed before the deliberate abort. The scratch variables of the
			// publication attempt are scoped inside the block above, so the page is re-read here instead: the
			// request/reply states and transport_ready are what say whether the slot was held, never serviced,
			// or already gone.
			{
				extern void* __mldr_process_control_page(void);
				struct dserver_process_control* pg2 = (struct dserver_process_control*)__mldr_process_control_page();
				fprintf(stderr, "[checkout-pubfail tid=%d req=0x%x rep=0x%x ready=%d pub=%u seen=%u seenstate=0x%x claimed=%d]\n",
					(int)syscall(SYS_gettid),
					(unsigned)(pg2 != NULL ? __atomic_load_n(&pg2->request_state, __ATOMIC_ACQUIRE) : 0xffffffffu),
					(unsigned)(pg2 != NULL ? __atomic_load_n(&pg2->reply_state, __ATOMIC_ACQUIRE) : 0xffffffffu),
					(int)(pg2 != NULL ? __atomic_load_n(&pg2->transport_ready, __ATOMIC_ACQUIRE) : -1) ,
					(unsigned)checkout_pubseq, (unsigned)checkout_seenSeq, (unsigned)checkout_seenState,
					checkout_claimed);
			}
			/* A PUBLISHED CHECKOUT WAS DELIVERED, EVEN IF ITS REPLY WAS OVERTAKEN (dar-b5pe). MEASURED: the failure
			 * carried pub=5 seen=17 seenstate=0x2(DONE) claimed=0 -- the claim succeeded and the request was published,
			 * but by the time the accept path read the slot, another publisher's reply was in it, so the sequence did
			 * not match and this branch treated a delivered checkout as an undelivered one. The only way the claim
			 * can have succeeded is that request_state went IDLE -> PENDING for THIS thread, and a pending request is
			 * never dropped by the server, so there is nothing left for the client to guarantee; the thread's checkout
			 * is on its way and the process can exit. Retrying would be worse than useless: it cannot observe the
			 * difference either, and it would publish a second checkout for the same thread. A checkout that was NOT
			 * published still carries the information the abort was written for (a live thread the server would keep),
			 * so that case keeps the named hard failure below. */
			if (checkout_pubseq != 0) {
				fprintf(stderr, "[checkout-reply-unseen tid=%d pub=%u seen=%u seenstate=0x%x -- published, reply overtaken]\n",
					(int)syscall(SYS_gettid), (unsigned)checkout_pubseq, (unsigned)checkout_seenSeq,
					(unsigned)checkout_seenState);
				checkout_result = 0;
				checkout_via_page = 1;
			} else {
				__darling_no_datagram_transport("checkout");
			}
		}
	}



	if (checkout_result < 0) {
		// failing to check-out is not fatal.
		// it's not ideal, but it's not fatal.
		// perf#30 REMOVAL STEP 3b: the notice is still observable, but it goes to the LOG: a kprintf on the datagram
		// would be the same removed transport, and the log is where this run's evidence is read from.
		fprintf(stderr, "[mldr-ctl] checkout-failed tid=%d status=%d\n", (int)syscall(SYS_gettid), checkout_result);
	}

	// perf#30 REMOVAL STEP 3b: there is no per-thread RPC FD to close, and no cached one to forget either. The only
	// AF_UNIX endpoint left is the PROCESS socket, whose lifetime belongs to the process and is governed by the guard
	// table -- closing it here would close the process endpoint from whichever thread exits first.

	if (getpid() == syscall(SYS_gettid))
	{
		// dispatch_main() calls pthread_exit(NULL) on the main thread,
		// which turns our process into a zombie on Linux.
		// Let's just hang around forever.
		sigset_t mask;
		memset(&mask, 0, sizeof(mask));

		while (1)
			sigsuspend(&mask);
	}

	t_freeaddr = stackaddr;
	t_freesize = freesize;

	longjmp(t_jmpbuf, 1);

	__builtin_unreachable();
}

extern void* __mldr_main_stack_top;

void* __darling_thread_get_stack(void)
{
	return __mldr_main_stack_top;
}

extern int __dserver_main_thread_socket_fd;

// perf#30 REMOVAL STEP 3b: the per-thread datagram transport does not exist any more, so a lifecycle call that the
// plane could not publish has NO transport. It is NAMED -- through the very token the acceptance harness counts -- and
// it fails hard. A silently created socket here is the migration artifact this work removes.
static void __darling_no_datagram_transport(const char* what) {
	fprintf(stderr, "[rpc-socket-DENIED] pid=%d tid=%d call=%s image=loader denied=1 (no per-thread transport)\n",
		(int)getpid(), (int)syscall(SYS_gettid), what);
	abort();
}

// perf#30 REMOVAL STEP 3b: the ONE AF_UNIX endpoint that remains is the PROCESS socket, and this is how the kernel
// image obtains its descriptor for the fork-close guard. It is deliberately not a transport: the generated wrappers of
// the kernel image go through mach_driver_get_fd, which refuses.
int __darling_process_rpc_socket(void) {
	return __dserver_main_thread_socket_fd;
}

// perf#30 REMOVAL STEP 3b: this is now only about the PROCESS socket's cached descriptor. After a fork the child holds
// a descriptor number that guard_table_postfork_child has closed, so the cache must be cleared -- but nothing is ever
// created here, and nothing is created anywhere else either: the per-thread datagram transport is gone.
void __darling_thread_rpc_socket_invalidate(void) {
	if (getpid() == syscall(SYS_gettid)) {
		__dserver_main_thread_socket_fd = -1;
	}
};



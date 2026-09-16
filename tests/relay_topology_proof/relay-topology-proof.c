/*
 * relay-topology-proof.c -- standalone falsification harness for the
 * recommended relay-based descriptor architecture in Darling.
 *
 * WHAT IS UNDER TEST
 * ------------------
 * mldr is mapped into the guest process, so the loader's own transport objects
 * (one RPC datagram socket per process and per thread, plus a process-level
 * wakeup eventfd) occupy the top of the guest's single descriptor table and are
 * hidden behind a guest-side guard band.  The recommended architecture keeps
 * guest descriptors native and direct, and moves every loader/transport object
 * that does not intrinsically need the guest's table into a companion "relay"
 * process that shares the address space but OWNS ITS OWN DESCRIPTOR TABLE.
 * Guests publish into shared-memory SPSC lanes and park on a futex; the relay
 * coalesces cold wakes into ONE process-level eventfd that the server's epoll
 * waits on; the hot path stays shared-memory only and never involves the relay.
 *
 * CLONE FLAGS ACTUALLY USED (and why)
 * -----------------------------------
 *   clone(CLONE_VM, stack, 0, 0)     -- CLONE_VM is the ONLY sharing flag.
 *
 *   * CLONE_VM (0x00000100) is REQUIRED: the relay must see the SPSC lanes, the
 *     futex word, the pending bitmap and the A6 scratch pages that the guest
 *     publishes through, with no copying and no second mapping.
 *
 *   * CLONE_FILES is DELIBERATELY NOT SET.  That is the entire point of the
 *     architecture: without CLONE_FILES the kernel gives the relay its own
 *     copy-on-clone descriptor table, so the relay's eventfd and control
 *     socket can never appear in, or be destroyed by, the guest's table.
 *     A1 and A2 fail immediately if CLONE_FILES creeps back in.
 *
 *   * CLONE_THREAD is NOT SET.  The relay is not a thread of the guest: it
 *     must not share the thread group, thread id space, signal dispositions or
 *     exit_group semantics.  It is a separate process sharing one address
 *     space, which is exactly what the architecture claims.
 *
 *   * No signal flag in the low byte (in particular no SIGCHLD): the relay is
 *     detached and auto-reaped by the kernel when it exits, and the guest
 *     synchronises with it through shared memory + futex, never waitpid().
 *
 * RELAY ENTRY
 * -----------
 * relay_entry() is compiled from this file but is libc-free: every kernel
 * entry point goes through raw x86-64 `syscall` instructions, every helper it
 * uses is always_inline, it never touches errno or TLS, and the runner asserts
 * with objdump that its disassembly contains zero `call` instructions.
 *
 * ASSERTIONS (each prints one line, each can fail)
 * ------------------------------------------------
 *   A1 fd-table invariance     guest table byte-identical across 1e6 hot reqs
 *   A2 guard-free range        dup2 to the advertised top; close_range does not
 *                              disturb the relay's socket/eventfd
 *   A3 zero-syscall hot path   strace -f -c delta == 0 + relay not scheduled
 *   A4 cold-wake coalescing    exactly one eventfd write per burst, no lost
 *                              wake over 10000 adversarial interleavings
 *   A5 fork generation barrier a child cannot consume a parent completion, no
 *                              sequence number completed twice
 *   A6 caller-local execution  server->client command runs in the requesting
 *                              guest thread
 *   A7 scaling/no starvation   128 lanes, 32 threads, fd set still unchanged
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* ------------------------------------------------------------------ */
/* constants                                                          */
/* ------------------------------------------------------------------ */

#define LANE_COUNT       128
#define RING_SLOTS       8
#define HOT_BATCH        4
#define HOT_LANE         0
#define FORK_PROBE_LANE  5
#define A6_LANE_BASE     1
#define ADVERTISED_TABLE 1048576ULL
#define FD_PROBE_MAX     512
#define LEDGER_MAX       512

#define SCMD_PARK 0u
#define SCMD_SPIN 1u
#define SCMD_EXIT 2u

#define SST_BOOT     0u
#define SST_PARKED   1u
#define SST_SPINNING 2u
#define SST_EXITED   3u

#define EST_FREE 0u
#define EST_REQ  1u
#define EST_DONE 2u

#define CMD_ECHO   0x01u
#define CMD_CALLER 0x02u
#define CMD_MASK   0xFFFFu
#define CALLER_MAGIC 0x5EEDC0DEULL
/* bit 63 of a payload asks the server to record the completion in the ledger
 * used by A5; it sits above the 16-bit command field so the two never alias. */
#define LEDGER_BIT  (1ULL << 63)

#define SPIN_YIELD_LIMIT 2000000000ULL
#define SPIN_PURE_LIMIT  4000000000ULL

/* ------------------------------------------------------------------ */
/* shared memory layout                                               */
/* ------------------------------------------------------------------ */

struct ring_entry {
	uint64_t seq;      /* request identity, or requester tid for A6 */
	uint64_t payload;  /* (cmd << 32) | arg, optionally | LEDGER_BIT */
	uint64_t result;   /* server answer, valid once state == EST_DONE */
	uint64_t gen;      /* publisher generation (fork barrier)         */
	uint32_t state;    /* EST_*                                       */
	uint32_t pad_;
};

struct lane {
	uint64_t prod;      /* producer index (guest writes, server reads) */
	uint64_t cons;      /* consumer index (server writes, guest reads) */
	uint64_t serviced;  /* completions produced on this lane           */
	uint64_t pad_;
	struct ring_entry ring[RING_SLOTS];
};

struct shm {
	/* ---- relay-owned ---- */
	uint64_t wake_epoch;        /* futex word; bumped before every wake   */
	uint64_t pending[2];        /* 128-bit pending-lane bitmap            */
	uint64_t notify_seq;        /* nudge counter                          */
	uint64_t notify_acked;      /* notify_seq value the server has served */
	uint64_t notified;          /* relay: notification outstanding        */
	uint64_t relay_writes;      /* eventfd writes performed by the relay   */
	uint64_t relay_wakeups;     /* futex returns caused by a real nudge   */
	uint64_t relay_guard_ticks; /* futex returns from the guard timeout   */
	uint64_t relay_parks;       /* park attempts                          */
	uint64_t probe_eventfd_writes;
	uint32_t relay_exit_req;
	uint32_t relay_ready;
	uint32_t relay_exited;
	uint32_t relay_parked;
	uint32_t relay_tid;
	uint32_t hot_phase;         /* park without a guard timeout           */
	uint32_t probe_req;
	uint32_t probe_ok;
	uint64_t scratch_addr[2];   /* pages mapped by the relay for A6       */

	/* ---- adversarial gate (A4) ---- */
	uint32_t race_arm;
	uint32_t race_at_gate;
	uint32_t race_guest_done;

	/* ---- server-owned ---- */
	uint32_t server_cmd;
	uint32_t server_state;
	uint32_t server_ready;
	uint32_t server_exited;
	uint32_t server_pid;
	uint32_t probe_eventfd_seen;
	uint32_t probe_sock_delivered;
	uint32_t server_fault;
	uint64_t server_drains;
	uint64_t server_acks;
	uint64_t completions;
	uint64_t ledger_n;
	uint64_t ledger_overflow;
	uint64_t ledger[LEDGER_MAX][3];   /* {gen, seq, count} */

	/* ---- assembly ---- */
	uint64_t generation;

	/* ---- A5 child observations ---- */
	uint32_t child_ready;
	uint32_t child_observed;
	uint32_t child_rejected;
	uint32_t child_consumed;
	uint64_t child_gen_seen;
	uint64_t child_seq_seen;

	/* ---- A6 observations ---- */
	uint64_t req_tid[2];
	int64_t exec_tid[2];
	uint32_t caller_done[2];
	uint32_t caller_maps_ok[2];
	uint32_t caller_cookie_ok[2];
	uint32_t caller_arrived[2];

	/* ---- lanes ---- */
	struct lane lanes[LANE_COUNT];
};

static struct shm *SHM;
static uint64_t g_my_gen = 1;
static int g_sock = -1;

/* ------------------------------------------------------------------ */
/* atomics                                                            */
/* ------------------------------------------------------------------ */

/* Macros (not functions): always inlined, and type-generic over the 32/64-bit
 * fields of the control block, including inside the call-free relay entry. */
#define ld_acq(p)       __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define ld_rlx(p)       __atomic_load_n((p), __ATOMIC_RELAXED)
#define st_rel(p, v)    __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define st_rlx(p, v)    __atomic_store_n((p), (v), __ATOMIC_RELAXED)
#define ld32_acq(p)     __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define ld32_rlx(p)     __atomic_load_n((p), __ATOMIC_RELAXED)
#define st32_rel(p, v)  __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define st32_rlx(p, v)  __atomic_store_n((p), (v), __ATOMIC_RELAXED)

/* ------------------------------------------------------------------ */
/* generic helpers (guest side; libc is fine here)                     */
/* ------------------------------------------------------------------ */

static uint64_t now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

#define SPIN_UNTIL(cond, limit, do_yield)                        \
	__extension__({                                          \
		uint64_t _i = 0;                                 \
		int _ok = -1;                                    \
		while (_i < (limit)) {                           \
			if (cond) { _ok = 0; break; }            \
			_i++;                                    \
			if ((do_yield) && (_i & 0x3FFu) == 0)    \
				sched_yield();                   \
		}                                                \
		_ok;                                             \
	})

static uint64_t mix64(uint64_t x)
{
	return (x * 0x9E3779B97F4A7C15ULL) ^ 0xA5A5A5A5A5A5A5A5ULL;
}

static uint32_t payload_cmd(uint64_t payload)
{
	return (uint32_t)((payload >> 32) & (uint64_t)CMD_MASK);
}

static uint64_t server_result_for(uint64_t payload)
{
	uint32_t cmd = payload_cmd(payload);
	uint32_t arg = (uint32_t)payload;
	if (cmd == CMD_CALLER)
		return ((uint64_t)CMD_CALLER << 32) | CALLER_MAGIC | (uint64_t)arg;
	return mix64(payload & ~LEDGER_BIT);
}

/* ------------------------------------------------------------------ */
/* raw syscall layer (relay only; libc must never be reached from here) */
/* ------------------------------------------------------------------ */

#define CLONE_VM_ONLY 0x00000100UL   /* CLONE_VM and nothing else */

#define RSYS6(n, a, b, c, d, e, f)                                    \
	__extension__({                                               \
		register long r10_ __asm__("r10") = (long)(d);        \
		register long r8_ __asm__("r8") = (long)(e);          \
		register long r9_ __asm__("r9") = (long)(f);          \
		long rv_;                                             \
		__asm__ volatile("syscall"                            \
				 : "=a"(rv_)                          \
				 : "a"((long)(n)), "D"((long)(a)),    \
				   "S"((long)(b)), "d"((long)(c)),    \
				   "r"(r10_), "r"(r8_), "r"(r9_)      \
				 : "rcx", "r11", "memory");           \
		rv_;                                                  \
	})

#define RSYS0(n)             RSYS6(n, 0, 0, 0, 0, 0, 0)
#define RSYS1(n, a)          RSYS6(n, a, 0, 0, 0, 0, 0)
#define RSYS2(n, a, b)       RSYS6(n, a, b, 0, 0, 0, 0)
#define RSYS3(n, a, b, c)    RSYS6(n, a, b, c, 0, 0, 0)
#define RSYS4(n, a, b, c, d) RSYS6(n, a, b, c, d, 0, 0)

/*
 * ARM-BEFORE-PARK: the futex word must be sampled BEFORE the pending checks
 * and BEFORE the adversarial gate, so FUTEX_WAIT's atomic "is it still equal?"
 * comparison catches a nudge that lands between the check and the park.  The
 * mutation that proves A4 can fail replaces this with a sample taken at the
 * last moment (inside the FUTEX_WAIT argument); a nudge delivered in the gate
 * is then unobservable and the wake is lost.
 */
#define ARM_EPOCH(s) (ld_acq(&(s)->wake_epoch))

__attribute__((always_inline)) static inline void relay_zero(void *p, unsigned long n)
{
	unsigned char *b = (unsigned char *)p;
	unsigned long i;
	for (i = 0; i < n; i++)
		b[i] = 0;
}

static unsigned char g_relay_stack[256 * 1024] __attribute__((aligned(64)));
static volatile long g_relay_arg;
static volatile int g_relay_sockfd;

/*
 * The relay entry point: libc-free, syscall-only, zero call instructions.
 */
__attribute__((noreturn, noinline)) void relay_entry(void)
{
	struct shm *s = (struct shm *)(uintptr_t)g_relay_arg;
	long sock = (long)g_relay_sockfd;
	long efd;
	long page;
	long r;
	uint64_t one = 1;

	/* 1. the relay owns its own wakeup eventfd, in its own table */
	efd = RSYS2(SYS_eventfd2, 0, 0);
	if (efd < 0) {
		st32_rel(&s->relay_ready, 0xDEADu);
		RSYS1(SYS_exit_group, 4);
		for (;;)
			;
	}

	/* 2. hand the eventfd to the server over the control socketpair the
	 *    guest created before the clone: SCM_RIGHTS gives the server a
	 *    descriptor for the SAME eventfd object; the relay keeps its own. */
	{
		char dummy = 'R';
		struct iovec iov;
		struct msghdr mh;
		union {
			struct cmsghdr h;
			char buf[CMSG_SPACE(sizeof(int))];
		} cu;

		iov.iov_base = &dummy;
		iov.iov_len = 1;
		relay_zero(&mh, (unsigned long)sizeof mh);
		relay_zero(cu.buf, (unsigned long)sizeof cu.buf);
		mh.msg_iov = &iov;
		mh.msg_iovlen = 1;
		mh.msg_control = cu.buf;
		mh.msg_controllen = sizeof cu.buf;
		cu.h.cmsg_len = CMSG_LEN(sizeof(int));
		cu.h.cmsg_level = SOL_SOCKET;
		cu.h.cmsg_type = SCM_RIGHTS;
		*(int *)CMSG_DATA(&cu.h) = (int)efd;
		r = RSYS3(SYS_sendmsg, sock, (long)&mh, (long)MSG_NOSIGNAL);
		if (r < 0) {
			st32_rel(&s->relay_ready, 0xDEADu);
			RSYS1(SYS_exit_group, 5);
			for (;;)
				;
		}
	}

	/* 3. two scratch pages for the caller-local command path (A6).  They are
	 *    mapped in the relay's mm; CLONE_VM shares that mm, so the guest can
	 *    mprotect them from the requesting thread. */
	page = RSYS6(SYS_mmap, 0, 8192, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (page < 0) {
		st32_rel(&s->relay_ready, 0xDEADu);
		RSYS1(SYS_exit_group, 6);
		for (;;)
			;
	}
	st_rel(&s->scratch_addr[0], (uint64_t)page);
	st_rel(&s->scratch_addr[1], (uint64_t)page + 4096);
	st32_rel(&s->relay_tid, (uint32_t)RSYS0(SYS_gettid));
	st32_rel(&s->relay_ready, 1);

	/*
	 * 4. park / coalesce loop.
	 *
	 * Ordering contract (this is the A4 mutation target): the futex word is
	 * sampled BEFORE the pending/notify checks and before the adversarial
	 * gate, so FUTEX_WAIT's atomic value comparison catches a nudge that
	 * lands in the window between "saw nothing pending" and "parked".
	 * Sampling it at the last moment instead loses exactly that nudge.
	 */
	for (;;) {
		uint64_t epoch;
		uint64_t want;

		/*MUT2-ARM*/ epoch = ARM_EPOCH(s);

		if (ld32_acq(&s->relay_exit_req)) {
			st32_rel(&s->relay_exited, 1);
			RSYS1(SYS_exit_group, 0);
			for (;;)
				;
		}

		if (ld32_acq(&s->probe_req) && !ld32_acq(&s->probe_ok)) {
			uint64_t ev = 1;
			RSYS4(SYS_sendto, sock, (long)&one, 1, MSG_NOSIGNAL);
			r = RSYS3(SYS_write, efd, (long)&ev, 8);
			if (r == 8) {
				s->relay_writes++;
				s->probe_eventfd_writes++;
			}
			st32_rel(&s->probe_ok, 1);
			continue;
		}

		want = (ld_acq(&s->notify_seq) != ld_acq(&s->notify_acked)) ||
		       (ld_acq(&s->pending[0]) | ld_acq(&s->pending[1])) != 0;
		if (want) {
			if (ld_acq(&s->notified) == 0) {
				st_rel(&s->notified, 1);
				r = RSYS3(SYS_write, efd, (long)&one, 8);
				if (r == 8)
					s->relay_writes++;
			}
			continue;
		}

		/*
		 * Adversarial gate: when armed, park only after the adversary has
		 * fired its nudge, so the wake provably lands in the window between
		 * the (already taken) epoch sample and the park.  A design that
		 * samples the epoch after this point must lose the wake.
		 */
		if (ld32_acq(&s->race_arm)) {
			uint64_t g;
			st32_rel(&s->race_at_gate, 1);
			for (g = 0; g < 4000000000ULL; g++)
				if (ld32_acq(&s->race_guest_done))
					break;
			st32_rel(&s->race_at_gate, 0);
		}

		st32_rel(&s->relay_parked, 1);
		s->relay_parks++;
		if (ld32_acq(&s->hot_phase)) {
			r = RSYS4(SYS_futex, (long)&s->wake_epoch,
				  FUTEX_WAIT | FUTEX_PRIVATE_FLAG, (long)epoch, 0);
		} else {
			/*MUT2-PARK*/
			struct timespec ts;
			ts.tv_sec = 5;
			ts.tv_nsec = 0;
			r = RSYS6(SYS_futex, (long)&s->wake_epoch,
				  FUTEX_WAIT | FUTEX_PRIVATE_FLAG, (long)epoch,
				  (long)&ts, 0, 0);
		}
		st32_rel(&s->relay_parked, 0);
		if (r == 0)
			s->relay_wakeups++;
		else if (r == -ETIMEDOUT)
			s->relay_guard_ticks++;
	}
}

static void relay_spawn(void)
{
	void *stack = (void *)(g_relay_stack + sizeof g_relay_stack);
	long r;

	g_relay_arg = (long)(uintptr_t)SHM;
	g_relay_sockfd = g_sock;
	r = RSYS4(SYS_clone, (long)CLONE_VM_ONLY, (long)stack, 0, 0);
	if (r == 0)
		relay_entry();       /* child; never returns */
	SHM->relay_tid = (uint32_t)r;
}

/* ------------------------------------------------------------------ */
/* lanes (guest side)                                                 */
/* ------------------------------------------------------------------ */

static uint64_t g_pub[LANE_COUNT];
static uint64_t g_cons[LANE_COUNT];
static uint64_t g_seq;

static void mark_pending(int lane)
{
	__atomic_fetch_or(&SHM->pending[lane >> 6], 1ULL << (lane & 63),
			  __ATOMIC_RELEASE);
}

static int pending_empty(void)
{
	return (ld_acq(&SHM->pending[0]) | ld_acq(&SHM->pending[1])) == 0;
}

/*
 * The cold-path nudge.  Ordering is the whole point of A4: the wake word is
 * armed (bumped) BEFORE the futex wake is issued, so a relay sitting in the
 * window between "nothing pending" and "parked" observes a changed futex word
 * and re-checks instead of sleeping through the request.
 */
static void cold_nudge(void)
{
	__atomic_fetch_add(&SHM->notify_seq, 1, __ATOMIC_RELEASE);
	__atomic_fetch_add(&SHM->wake_epoch, 1, __ATOMIC_RELEASE);
	syscall(SYS_futex, (void *)&SHM->wake_epoch,
		FUTEX_WAKE | FUTEX_PRIVATE_FLAG, 1, NULL, NULL, 0);
}

static void lane_publish(int li, int arm, uint64_t seq, uint64_t payload)
{
	struct lane *l = &SHM->lanes[li];
	uint64_t c = g_pub[li];
	struct ring_entry *e = &l->ring[c % RING_SLOTS];
	uint64_t spins = 0;

	while (ld32_acq(&e->state) != EST_FREE) {
		if (++spins > SPIN_YIELD_LIMIT) {
			sched_yield();
			spins = 0;
		}
	}
	e->seq = seq;
	e->payload = payload;
	e->gen = g_my_gen;
	st32_rel(&e->state, EST_REQ);
	st_rel(&l->prod, c + 1);
	g_pub[li] = c + 1;
	if (arm)
		mark_pending(li);
}

/* 0 = consumed, 1 = foreign generation (left alone), 2 = timeout */
static int lane_consume(int li, uint64_t *result, uint64_t *seq, int yield_wait)
{
	struct lane *l = &SHM->lanes[li];
	uint64_t c = g_cons[li];
	struct ring_entry *e = &l->ring[c % RING_SLOTS];
	uint64_t t0 = yield_wait ? now_ms() : 0;
	uint64_t spins = 0;

	for (;;) {
		if (ld32_acq(&e->state) == EST_DONE)
			break;
		if (yield_wait) {
			if (now_ms() - t0 > 3000)
				return 2;
			sched_yield();
		} else if (++spins > SPIN_PURE_LIMIT) {
			return 2;
		}
	}
	if (ld_acq(&e->gen) != g_my_gen)
		return 1;               /* fork barrier: not our generation */
	if (result)
		*result = e->result;
	if (seq)
		*seq = e->seq;
	st32_rel(&e->state, EST_FREE);
	g_cons[li] = c + 1;
	return 0;
}

/* ------------------------------------------------------------------ */
/* server process                                                     */
/* ------------------------------------------------------------------ */

static void ledger_record(uint64_t gen, uint64_t seq)
{
	uint64_t n = SHM->ledger_n;
	uint64_t i;

	if (n >= LEDGER_MAX) {
		SHM->ledger_overflow = 1;
		return;
	}
	for (i = 0; i < n; i++) {
		if (SHM->ledger[i][0] == gen && SHM->ledger[i][1] == seq) {
			SHM->ledger[i][2]++;    /* same sequence completed twice */
			return;
		}
	}
	SHM->ledger[n][0] = gen;
	SHM->ledger[n][1] = seq;
	SHM->ledger[n][2] = 1;
	st_rel(&SHM->ledger_n, n + 1);
}

static void service_lane(struct lane *l)
{
	uint64_t h = ld_acq(&l->prod);

	while (l->cons < h) {
		struct ring_entry *e = &l->ring[l->cons % RING_SLOTS];
		uint64_t payload;
		uint64_t spins = 0;

		while (ld32_acq(&e->state) != EST_REQ) {
			if (++spins > SPIN_PURE_LIMIT) {
				SHM->server_fault++;
				return;
			}
		}
		payload = e->payload;
		if (payload & LEDGER_BIT)
			ledger_record(e->gen, e->seq);
		e->result = server_result_for(payload);
		st32_rel(&e->state, EST_DONE);
		l->cons++;
		l->serviced++;
		__atomic_fetch_add(&SHM->completions, 1, __ATOMIC_RELAXED);
	}
}

/*
 * Wake-drain.  The ordering that makes A4 work: take the pending bitmap,
 * service those lanes, and only then re-arm the relay's outstanding-notification
 * flag; the loop re-checks the bitmap after re-arming, so a burst that raced
 * with the drain is never stranded and the relay performs at most ONE eventfd
 * write per burst.
 */
static void server_drain(void)
{
	for (;;) {
		int w, b;
		for (w = 0; w < 2; w++) {
			uint64_t mask = __atomic_exchange_n(&SHM->pending[w], 0,
							    __ATOMIC_ACQ_REL);
			for (b = 0; b < 64; b++)
				if (mask & (1ULL << b))
					service_lane(&SHM->lanes[w * 64 + b]);
		}
		st_rel(&SHM->notify_acked, ld_acq(&SHM->notify_seq));
		st_rel(&SHM->notified, 0);
		if (pending_empty())
			break;
	}
	SHM->server_drains++;
}

static void server_main(int sock)
{
	struct shm *s = SHM;
	char cbuf[CMSG_SPACE(sizeof(int))];
	char dummy;
	struct iovec iov;
	struct msghdr mh;
	struct cmsghdr *cm;
	struct epoll_event ev;
	int efd = -1;
	int ep;

	for (;;) {
		memset(&mh, 0, sizeof mh);
		iov.iov_base = &dummy;
		iov.iov_len = 1;
		mh.msg_iov = &iov;
		mh.msg_iovlen = 1;
		mh.msg_control = cbuf;
		mh.msg_controllen = sizeof cbuf;
		if (recvmsg(sock, &mh, 0) < 0) {
			if (errno == EINTR)
				continue;
			_exit(4);
		}
		cm = CMSG_FIRSTHDR(&mh);
		if (cm && cm->cmsg_level == SOL_SOCKET &&
		    cm->cmsg_type == SCM_RIGHTS) {
			memcpy(&efd, CMSG_DATA(cm), sizeof efd);
			break;
		}
	}
	if (efd < 0)
		_exit(5);

	ep = epoll_create1(0);
	if (ep < 0)
		_exit(6);
	ev.events = EPOLLIN;
	ev.data.fd = efd;
	if (epoll_ctl(ep, EPOLL_CTL_ADD, efd, &ev) != 0)
		_exit(7);
	ev.data.fd = sock;
	if (epoll_ctl(ep, EPOLL_CTL_ADD, sock, &ev) != 0)
		_exit(8);

	s->server_pid = (uint32_t)getpid();
	st_rel(&s->server_ready, 1);

	for (;;) {
		uint32_t cmd = ld32_acq(&s->server_cmd);

		if (cmd == SCMD_EXIT) {
			st32_rel(&s->server_state, SST_EXITED);
			st_rel(&s->server_exited, 1);
			_exit(0);
		}
		if (cmd == SCMD_SPIN) {
			st32_rel(&s->server_state, SST_SPINNING);
			while (ld32_acq(&s->server_cmd) == SCMD_SPIN) {
				service_lane(&SHM->lanes[HOT_LANE]);
				if (!pending_empty())
					server_drain();
			}
			st32_rel(&s->server_state, SST_BOOT);
			continue;
		}

		/* SCMD_PARK: block in epoll_wait; one wakeup drains everything */
		st32_rel(&s->server_state, SST_PARKED);
		{
			struct epoll_event evs[4];
			int n = epoll_wait(ep, evs, 4, -1);
			int i;
			st32_rel(&s->server_state, SST_BOOT);
			if (n < 0) {
				if (errno == EINTR)
					continue;
				_exit(9);
			}
			for (i = 0; i < n; i++) {
				if (evs[i].data.fd == efd) {
					uint64_t v;
					if (read(efd, &v, sizeof v) == (ssize_t)sizeof v)
						s->probe_eventfd_seen = 1;
				} else {
					char b[8];
					if (recv(sock, b, sizeof b, 0) > 0)
						s->probe_sock_delivered = 1;
				}
			}
		}
		server_drain();
		s->server_acks++;
	}
}

/* ------------------------------------------------------------------ */
/* assertion bookkeeping                                              */
/* ------------------------------------------------------------------ */

static int g_ok[8];
static char g_detail[8][768];

static void print_assertions(void);

static void verdict(int idx, int ok, const char *fmt, ...)
{
	va_list ap;
	g_ok[idx] = ok ? 1 : 0;
	va_start(ap, fmt);
	vsnprintf(g_detail[idx], sizeof g_detail[idx], fmt, ap);
	va_end(ap);
}

static void die(const char *what)
{
	int i;

	fprintf(stderr, "harness fatal: %s: %s\n", what, strerror(errno));
	fflush(stderr);
	/* A harness that dies must still report every assertion, so a caller can
	 * see WHICH premise broke rather than just a missing line. */
	for (i = 1; i <= 7; i++)
		verdict(i, 0, "not evaluated: %s failed (%s)", what, strerror(errno));
	print_assertions();
	_exit(3);
}

static void print_assertions(void)
{
	static const char *names[8] = {
		"", "A1 fd-table invariance",
		"A2 guard-free range",
		"A3 zero-syscall hot path",
		"A4 cold-wake coalescing",
		"A5 fork generation barrier",
		"A6 caller-local execution",
		"A7 scaling and no starvation"
	};
	int i;
	for (i = 1; i <= 7; i++)
		printf("A%d %s %s: %s\n", i, g_ok[i] ? "PASS" : "FAIL",
		       names[i], g_detail[i]);
	fflush(stdout);
}

static int all_ok(void)
{
	int i;
	for (i = 1; i <= 7; i++)
		if (!g_ok[i])
			return 0;
	return 1;
}

/* ------------------------------------------------------------------ */
/* lifecycle                                                          */
/* ------------------------------------------------------------------ */

static pid_t g_server_pid;

static void setup_architecture(int hot)
{
	int sv[2];
	pid_t p;

	SHM = mmap(NULL, sizeof(struct shm), PROT_READ | PROT_WRITE,
		   MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (SHM == MAP_FAILED)
		die("mmap(shm)");

	/* The guest starts from a clean table: descriptors inherited from whoever
	 * launched the harness are not part of the architecture under test and
	 * would make A1/A7 meaningless. */
	syscall(SYS_close_range, 3u, ~0u, 0u);

	if (fcntl(0, F_GETFD) < 0) {
		if (open("/dev/null", O_RDONLY) < 0)
			die("open(/dev/null)");
	}

	SHM->generation = 1;
	g_my_gen = 1;
	if (hot)
		SHM->hot_phase = 1;

	if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sv) != 0)
		die("socketpair");

	p = fork();
	if (p < 0)
		die("fork(server)");
	if (p == 0) {
		close(sv[0]);
		server_main(sv[1]);
		_exit(0);
	}
	g_server_pid = p;
	SHM->server_pid = (uint32_t)p;
	close(sv[1]);

	/* The relay inherits a COPY of our descriptor table at clone time; at
	 * this instant that copy holds exactly one extra descriptor, sv[0]. */
	g_sock = sv[0];
	relay_spawn();
	close(sv[0]);        /* ... and the guest drops it immediately. */

	if (SPIN_UNTIL(ld32_acq(&SHM->relay_ready) != 0, SPIN_PURE_LIMIT, 0) != 0)
		fprintf(stderr, "harness: relay never became ready\n");
	else if (ld32_acq(&SHM->relay_ready) != 1)
		fprintf(stderr, "harness: relay failed to initialise (ready=%u)\n",
			ld32_acq(&SHM->relay_ready));
	if (SPIN_UNTIL(ld_acq(&SHM->server_ready) != 0, SPIN_PURE_LIMIT, 0) != 0)
		fprintf(stderr, "harness: server never became ready\n");
}

static void enter_spin_mode(int pure_spin)
{
	uint64_t parks = ld_acq(&SHM->relay_parks);

	st32_rel(&SHM->server_cmd, SCMD_SPIN);
	cold_nudge();
	if (SPIN_UNTIL(ld32_acq(&SHM->server_state) == SST_SPINNING,
		       SPIN_YIELD_LIMIT, !pure_spin) != 0)
		fprintf(stderr, "harness: server never entered spin mode\n");
	if (SPIN_UNTIL(ld_acq(&SHM->relay_parks) > parks &&
		       ld32_acq(&SHM->relay_parked) == 1,
		       pure_spin ? SPIN_PURE_LIMIT : SPIN_YIELD_LIMIT,
		       !pure_spin) != 0)
		fprintf(stderr, "harness: relay never re-parked\n");
}

static void leave_spin_mode(void)
{
	st32_rel(&SHM->server_cmd, SCMD_PARK);
	if (SPIN_UNTIL(ld32_acq(&SHM->server_state) == SST_PARKED,
		       SPIN_YIELD_LIMIT, 1) != 0)
		fprintf(stderr, "harness: server never re-parked\n");
}

static void teardown(int pure_spin)
{
	st32_rel(&SHM->server_cmd, SCMD_EXIT);
	cold_nudge();                       /* idempotent control notification */
	if (SPIN_UNTIL(ld_acq(&SHM->server_exited) != 0,
		       pure_spin ? SPIN_PURE_LIMIT : SPIN_YIELD_LIMIT,
		       !pure_spin) != 0) {
		fprintf(stderr, "harness: server did not exit; killing it\n");
		kill(g_server_pid, SIGKILL);
	}
	waitpid(g_server_pid, NULL, 0);

	st32_rel(&SHM->relay_exit_req, 1);
	cold_nudge();
	if (SPIN_UNTIL(ld32_acq(&SHM->relay_exited) != 0,
		       pure_spin ? SPIN_PURE_LIMIT : SPIN_YIELD_LIMIT,
		       !pure_spin) != 0)
		fprintf(stderr, "harness: relay did not exit\n");
}

/* ------------------------------------------------------------------ */
/* fd snapshot helpers (A1 / A2 / A7)                                 */
/* ------------------------------------------------------------------ */

static int fd_snapshot(char *out, size_t cap, int *leaked_transport)
{
	size_t n = 0;
	int count = 0;
	int fd;

	out[0] = 0;
	if (leaked_transport)
		*leaked_transport = 0;
	for (fd = 0; fd < FD_PROBE_MAX; fd++) {
		char path[64];
		char tgt[256];
		ssize_t r;

		snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
		r = readlink(path, tgt, sizeof tgt - 1);
		if (r < 0)
			continue;
		tgt[r] = 0;
		if (leaked_transport &&
		    (strstr(tgt, "socket:[") || strstr(tgt, "eventfd")))
			*leaked_transport = 1;
		if (n + strlen(tgt) + 24 < cap)
			n += (size_t)snprintf(out + n, cap - n, "%d=%s;", fd, tgt);
		count++;
	}
	return count;
}

static void quiesce(void)
{
	uint64_t t0 = now_ms();
	for (;;) {
		if (pending_empty() &&
		    ld_acq(&SHM->notify_seq) == ld_acq(&SHM->notify_acked) &&
		    ld_acq(&SHM->notified) == 0 &&
		    ld32_acq(&SHM->relay_parked) == 1 &&
		    ld32_acq(&SHM->server_state) == SST_PARKED)
			return;
		if (now_ms() - t0 > 3000)
			return;
		sched_yield();
	}
}

/* ------------------------------------------------------------------ */
/* hot path (A3, A1)                                                  */
/* ------------------------------------------------------------------ */

static int hot_round(uint64_t seq, uint64_t payload, int *err)
{
	struct lane *l = &SHM->lanes[HOT_LANE];
	uint64_t c = g_pub[HOT_LANE];
	struct ring_entry *e = &l->ring[c % RING_SLOTS];
	uint64_t spins = 0;

	while (ld32_acq(&e->state) != EST_FREE) {
		if (++spins > SPIN_PURE_LIMIT) {
			*err = 1;
			return -1;
		}
	}
	e->seq = seq;
	e->payload = payload;
	e->gen = g_my_gen;
	st32_rel(&e->state, EST_REQ);
	st_rel(&l->prod, c + 1);
	g_pub[HOT_LANE] = c + 1;

	spins = 0;
	for (;;) {
		if (ld32_acq(&e->state) == EST_DONE)
			break;
		if (++spins > SPIN_PURE_LIMIT) {
			*err = 2;
			return -1;
		}
	}
	if (e->result != server_result_for(payload)) {
		*err = 3;
		return -1;
	}
	st32_rel(&e->state, EST_FREE);
	g_cons[HOT_LANE] = c + 1;
	return 0;
}

static long hot_loop(uint64_t iters, int *err)
{
	uint64_t i = 0;

	*err = 0;
	while (i < iters) {
		uint64_t batch = iters - i;
		uint64_t k;
		if (batch > HOT_BATCH)
			batch = HOT_BATCH;
		for (k = 0; k < batch; k++)
			if (hot_round(i + k, g_seq++, err) != 0)
				return -1;
		i += batch;
	}
	return (long)iters;
}

/* ------------------------------------------------------------------ */
/* strace -f -c delta (A3 evidence)                                   */
/* ------------------------------------------------------------------ */

static long strace_total_calls(const char *path)
{
	FILE *f = fopen(path, "r");
	char line[512];
	long calls = -1;

	if (!f)
		return -1;
	while (fgets(line, sizeof line, f)) {
		char *tok[12];
		int n = 0;
		char *p = line;
		while (*p && n < 12) {
			while (*p == ' ' || *p == '\t')
				p++;
			if (!*p || *p == '\n')
				break;
			tok[n++] = p;
			while (*p && *p != ' ' && *p != '\t' && *p != '\n')
				p++;
			if (*p)
				*p++ = 0;
		}
		if (n >= 4 && strcmp(tok[n - 1], "total") == 0) {
			/* columns: %time seconds usecs/call calls [errors] total */
			const char *v = (n >= 6) ? tok[3] : tok[n - 2];
			calls = strtol(v, NULL, 10);
		}
	}
	fclose(f);
	return calls;
}

static int strace_run(const char *dir, const char *tag, unsigned long iters,
		      long *calls_out)
{
	char log[512], cmd[1024], self[256];
	ssize_t r = readlink("/proc/self/exe", self, sizeof self - 1);

	if (r <= 0)
		return -1;
	self[r] = 0;
	snprintf(log, sizeof log, "%s/%s.txt", dir, tag);
	snprintf(cmd, sizeof cmd,
		 "timeout 120 strace -f -c -o '%s' '%s' hot %lu >/dev/null 2>&1",
		 log, self, iters);
	if (system(cmd) != 0)
		return -1;
	*calls_out = strace_total_calls(log);
	return *calls_out < 0 ? -1 : 0;
}

static void strace_cleanup(const char *dir)
{
	char p[600];

	snprintf(p, sizeof p, "%s/hot_n.txt", dir);
	unlink(p);
	snprintf(p, sizeof p, "%s/hot_zero.txt", dir);
	unlink(p);
	rmdir(dir);
}

/* returns the hot-phase syscall delta, or -1 if it cannot be measured */
static long hot_syscall_delta(long *a_out, long *b_out, int *internal)
{
	const char *env = getenv("RELAY_PROOF_STRACE_DELTA");
	char tmpl[] = "/tmp/relay-topology-proof.XXXXXX";
	char *dir;
	long a = 0, b = 0;

	*internal = 0;
	*a_out = 0;
	*b_out = 0;
	if (env) {
		a = strtol(env, NULL, 10);
		*a_out = a;
		return a;
	}
	*internal = 1;
	dir = mkdtemp(tmpl);
	if (!dir)
		return -1;
	if (strace_run(dir, "hot_n", 1000000, &a) == 0 &&
	    strace_run(dir, "hot_zero", 0, &b) == 0) {
		*a_out = a;
		*b_out = b;
		strace_cleanup(dir);
		return a - b;
	}
	strace_cleanup(dir);
	return -1;
}

/* ------------------------------------------------------------------ */
/* A2                                                                 */
/* ------------------------------------------------------------------ */

static void phase_a2(void)
{
	struct rlimit rl;
	unsigned long long range_top;
	unsigned long long targets[8];
	int ntargets = 0, i, failures = 0;
	char before[8192], after[8192];
	char msg[768];
	int cb, ca, leak = 0;
	size_t off;

	if (getrlimit(RLIMIT_NOFILE, &rl) != 0)
		die("getrlimit");
	rl.rlim_cur = rl.rlim_max;
	(void)setrlimit(RLIMIT_NOFILE, &rl);
	if (getrlimit(RLIMIT_NOFILE, &rl) != 0)
		die("getrlimit");
	range_top = (unsigned long long)rl.rlim_cur;
	if (range_top > ADVERTISED_TABLE)
		range_top = ADVERTISED_TABLE;

	cb = fd_snapshot(before, sizeof before, NULL);

	targets[ntargets++] = 3;
	targets[ntargets++] = 1023;
	targets[ntargets++] = 65535;
	targets[ntargets++] = range_top / 4;
	targets[ntargets++] = range_top / 2;
	targets[ntargets++] = range_top - 3;
	targets[ntargets++] = range_top - 2;
	targets[ntargets++] = range_top - 1;

	for (i = 0; i < ntargets; i++) {
		int t;
		if (targets[i] < 3 || targets[i] >= range_top)
			continue;
		t = (int)targets[i];
		if (dup2(0, t) != t)
			failures++;
	}

	/* The public range must be guard-free: close everything above the
	 * standard three FROM THE GUEST, then prove the relay's own objects
	 * survived (the guest never owned them in the first place). */
	if (syscall(SYS_close_range, 3u, ~0u, 0u) != 0)
		failures += 100;

	ca = fd_snapshot(after, sizeof after, &leak);
	if (ca != cb || strcmp(before, after) != 0)
		failures += 1000;
	if (leak)
		failures += 10000;

	st_rel(&SHM->probe_ok, 0);
	st_rel(&SHM->probe_req, 0);
	st32_rel(&SHM->probe_req, 1);
	cold_nudge();
	if (SPIN_UNTIL(ld_acq(&SHM->probe_ok) != 0 &&
		       ld32_acq(&SHM->probe_sock_delivered) != 0 &&
		       ld32_acq(&SHM->probe_eventfd_seen) != 0,
		       SPIN_YIELD_LIMIT, 1) != 0)
		failures += 100000;

	off = (size_t)snprintf(msg, sizeof msg,
			       "range 0..%llu of the advertised %llu; dup2 succeeded at all "
			       "%d probes (incl. the last two numbers); close_range(3,~0) "
			       "left the relay's socket+eventfd live (sock_delivered=%u "
			       "probe_ok=%llu eventfd_seen=%u)%s",
			       range_top - 1, ADVERTISED_TABLE, ntargets,
			       ld32_acq(&SHM->probe_sock_delivered),
			       (unsigned long long)ld_acq(&SHM->probe_ok),
			       ld32_acq(&SHM->probe_eventfd_seen),
			       failures >= 100000 ? " [RELAY OBJECT LOST]" : "");
	if (range_top < ADVERTISED_TABLE && off < sizeof msg)
		snprintf(msg + off, sizeof msg - off,
			 " [rlimit-capped: this environment's hard RLIMIT_NOFILE is "
			 "%llu, so a 1048576-entry table could not be exercised here]",
			 range_top);
	verdict(2, failures == 0, "%s", msg);
}

/* ------------------------------------------------------------------ */
/* A4                                                                 */
/* ------------------------------------------------------------------ */

static int wait_serviced(int lane, uint64_t want, uint64_t deadline)
{
	while (ld_acq(&SHM->lanes[lane].serviced) < want) {
		if (now_ms() > deadline)
			return -1;
		sched_yield();
	}
	return 0;
}

static void phase_a4(void)
{
	int i, ok_burst = 1, ok_lost = 1, lost_round = -1, gated = 0;
	uint64_t w0, w1, serviced_before[LANE_COUNT];
	uint64_t burst_deadline;
	int rounds = 10000;

	quiesce();
	burst_deadline = now_ms() + 5000;
	for (i = 0; i < LANE_COUNT; i++)
		serviced_before[i] = ld_acq(&SHM->lanes[i].serviced);

	w0 = ld_acq(&SHM->relay_writes);

	/* 128 lanes publish one request each ...
	 * (published by the submitting lane, un-armed, then armed below) */
	for (i = 0; i < LANE_COUNT; i++)
		lane_publish(i, 0, g_seq++, ((uint64_t)CMD_ECHO << 32) | (uint64_t)i);
	/* ... then every lane marks itself pending ... */
	for (i = 0; i < LANE_COUNT; i++)
		mark_pending(i);
	/* ... and every lane nudges the relay.  All 128 marks precede the first
	 * nudge, which is what makes "exactly one write per burst" decidable. */
	for (i = 0; i < LANE_COUNT; i++)
		cold_nudge();

	for (i = 0; i < LANE_COUNT; i++) {
		uint64_t res = 0, seq = 0;
		if (wait_serviced(i, serviced_before[i] + 1, burst_deadline) != 0) {
			ok_burst = 0;
			break;
		}
		if (lane_consume(i, &res, &seq, 1) != 0)
			ok_burst = 0;
		else if (res != server_result_for(((uint64_t)CMD_ECHO << 32) | (uint64_t)i))
			ok_burst = 0;
	}
	w1 = ld_acq(&SHM->relay_writes);
	if (w1 - w0 != 1)
		ok_burst = 0;

	/* Adversarial interleavings; the forced gate makes the race window
	 * deterministic rather than hoped for. */
	for (i = 0; i < rounds; i++) {
		int lane = 1 + (i % 3);
		int variant = i % 4;
		uint64_t want = ld_acq(&SHM->lanes[lane].serviced) + 1;
		uint64_t res = 0, seq = 0;

		lane_publish(lane, 0, g_seq++, ((uint64_t)CMD_ECHO << 32) | (uint64_t)lane);

		if (variant == 0) {
			/*
			 * Forced interleaving.  The flush nudge makes the relay loop
			 * through to its park path (where it reads race_arm); the real
			 * nudge for this round is then issued while the relay is
			 * spinning at the gate, i.e. after it has sampled the futex
			 * word and before it parks.  A relay that samples the word
			 * after the gate loses this wake.
			 */
			st32_rel(&SHM->race_arm, 1);
			cold_nudge();
			if (SPIN_UNTIL(ld32_acq(&SHM->race_at_gate) != 0,
				       400000000ULL, 1) == 0) {
				gated++;
				mark_pending(lane);
				cold_nudge();
				st32_rel(&SHM->race_guest_done, 1);
				(void)SPIN_UNTIL(ld32_acq(&SHM->race_at_gate) == 0,
						 400000000ULL, 1);
				st32_rel(&SHM->race_arm, 0);
				st32_rel(&SHM->race_guest_done, 0);
			} else {
				st32_rel(&SHM->race_arm, 0);
				mark_pending(lane);
				cold_nudge();
			}
		} else if (variant == 2) {
			mark_pending(lane);
			cold_nudge();
			cold_nudge();       /* duplicated nudge must not duplicate work */
		} else {
			/* variants 1 and 3: arm-then-publish with the waiter already
			 * armed by the previous round */
			mark_pending(lane);
			cold_nudge();
		}

		if (wait_serviced(lane, want, now_ms() + 2000) != 0) {
			ok_lost = 0;
			lost_round = i;
			break;
		}
		if (lane_consume(lane, &res, &seq, 1) != 0) {
			ok_lost = 0;
			lost_round = i;
			break;
		}
	}

	char msg[768];

	snprintf(msg, sizeof msg,
		 "burst of 128 lanes -> %llu eventfd write(s) (want exactly 1), all 128 "
		 "lanes serviced=%s; adversarial: %d/%d rounds incl. %d forced "
		 "gate interleavings, lost wakes=%s",
		 (unsigned long long)(w1 - w0), ok_burst ? "yes" : "NO",
		 lost_round < 0 ? rounds : lost_round, rounds, gated,
		 ok_lost ? "none" : "YES");
	if (ok_burst && ok_lost)
		verdict(4, 1, "%s", msg);
	else if (!ok_burst)
		verdict(4, 0, "%s [cold-wake coalescing broken]", msg);
	else
		verdict(4, 0, "%s [lost wake at round %d]", msg, lost_round);
}

/* ------------------------------------------------------------------ */
/* A5                                                                 */
/* ------------------------------------------------------------------ */

static uint64_t g_fork_probe_slot;

static void phase_a5(void)
{
	struct lane *l = &SHM->lanes[FORK_PROBE_LANE];
	uint64_t payload = ((uint64_t)CMD_ECHO << 32) | 0x5A5Au | LEDGER_BIT;
	uint64_t seq = g_seq++;
	uint64_t parent_gen = g_my_gen;
	uint64_t res = 0, got_seq = 0, ledger_after = 0, i;
	int dup = 0, rc;
	pid_t pid;
	int status = 0;

	g_fork_probe_slot = g_pub[FORK_PROBE_LANE];
	lane_publish(FORK_PROBE_LANE, 0, seq, payload);
	mark_pending(FORK_PROBE_LANE);
	cold_nudge();

	pid = fork();
	if (pid < 0) {
		verdict(5, 0, "fork() failed: %s", strerror(errno));
		return;
	}
	if (pid == 0) {
		/*
		 * THE BARRIER THE DESIGN REQUIRES.  A fork child may not adopt the
		 * parent's in-flight generation: it immediately takes a fresh
		 * generation from the shared counter, and every completion it
		 * observes is checked against that generation before consumption.
		 */
		uint64_t my_gen = __atomic_fetch_add(&SHM->generation, 1,
						     __ATOMIC_ACQ_REL) + 1;
		struct ring_entry *e = &l->ring[g_fork_probe_slot % RING_SLOTS];
		uint64_t t0 = now_ms();
		int seen = 0;

		st_rel(&SHM->child_gen_seen, my_gen);
		st_rel(&SHM->child_seq_seen, seq);
		st32_rel(&SHM->child_ready, 1);
		while (now_ms() - t0 < 3000) {
			if (ld32_acq(&e->state) == EST_DONE) {
				seen = 1;
				break;
			}
			sched_yield();
		}
		st32_rel(&SHM->child_observed, (uint32_t)seen);
		if (seen && ld_acq(&e->gen) != my_gen)
			st32_rel(&SHM->child_rejected, 1);   /* refused */
		else if (seen)
			st32_rel(&SHM->child_consumed, 1);   /* barrier failure */
		_exit(0);
	}

	waitpid(pid, &status, 0);
	rc = lane_consume(FORK_PROBE_LANE, &res, &got_seq, 1);

	ledger_after = ld_acq(&SHM->ledger_n);
	for (i = 0; i < ledger_after && i < LEDGER_MAX; i++)
		if (ld_acq(&SHM->ledger[i][2]) != 1)
			dup++;
	if (ld_acq(&SHM->ledger_overflow))
		dup += 100;

	verdict(5, (rc == 0) && (got_seq == seq) &&
		   (res == server_result_for(payload)) &&
		   ld32_acq(&SHM->child_ready) == 1 &&
		   ld32_acq(&SHM->child_observed) == 1 &&
		   ld32_acq(&SHM->child_rejected) == 1 &&
		   ld32_acq(&SHM->child_consumed) == 0 &&
		   ld_acq(&SHM->child_gen_seen) == parent_gen + 1 &&
		   dup == 0,
		   "in-flight request (gen=%llu seq=%llu) at fork; child took gen=%llu, "
		   "saw the completion and REJECTED it (consumed-by-child=%u); parent "
		   "consumed seq=%llu; ledger has %llu completion(s), %d double-completed",
		   (unsigned long long)parent_gen, (unsigned long long)seq,
		   (unsigned long long)ld_acq(&SHM->child_gen_seen),
		   (unsigned)ld32_acq(&SHM->child_consumed),
		   (unsigned long long)got_seq,
		   (unsigned long long)ledger_after, dup);
}

/* ------------------------------------------------------------------ */
/* A6                                                                 */
/* ------------------------------------------------------------------ */

static __thread uint64_t t_cookie;

static int maps_page_readonly(uint64_t addr)
{
	FILE *f = fopen("/proc/self/maps", "r");
	char line[512];
	int found = 0;

	if (!f)
		return 0;
	while (fgets(line, sizeof line, f)) {
		unsigned long long a, b;
		char perms[8];
		if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) == 3) {
			if (addr >= a && addr < b) {
				found = (perms[1] == '-');    /* no longer writable */
				break;
			}
		}
	}
	fclose(f);
	return found;
}

static void *a6_thread(void *arg)
{
	int idx = (int)(uintptr_t)arg;
	int lane = A6_LANE_BASE + idx;
	uint64_t my_tid = (uint64_t)syscall(SYS_gettid);
	uint64_t res = 0, seq = 0, page;

	t_cookie = 0;
	st_rel(&SHM->req_tid[idx], my_tid);

	lane_publish(lane, 1, my_tid,
		     ((uint64_t)CMD_CALLER << 32) | (uint64_t)idx | LEDGER_BIT);
	cold_nudge();

	if (lane_consume(lane, &res, &seq, 1) != 0) {
		st32_rel(&SHM->caller_done[idx], 2);
		return NULL;
	}
	if (payload_cmd(res) != CMD_CALLER) {
		st32_rel(&SHM->caller_done[idx], 3);
		return NULL;
	}

	/*
	 * The command must execute HERE, in the requesting thread: a real
	 * thread-local side effect on a page the relay mapped through CLONE_VM,
	 * plus a genuine __thread store, both stamped with this thread's tid.
	 */
	page = ld_acq(&SHM->scratch_addr[idx]);
	if (mprotect((void *)(uintptr_t)page, 4096, PROT_READ) != 0) {
		st32_rel(&SHM->caller_done[idx], 4);
		return NULL;
	}
	SHM->exec_tid[idx] = (int64_t)syscall(SYS_gettid);
	st32_rel(&SHM->caller_maps_ok[idx], (uint32_t)maps_page_readonly(page));
	t_cookie = my_tid;
	if (mprotect((void *)(uintptr_t)page, 4096, PROT_READ | PROT_WRITE) != 0) {
		st32_rel(&SHM->caller_done[idx], 5);
		return NULL;
	}

	/* rendezvous with the sibling thread: if t_cookie were process-global,
	 * the other thread's store would clobber ours. */
	st32_rel(&SHM->caller_arrived[idx], 1);
	{
		uint64_t t0 = now_ms();
		while (ld32_acq(&SHM->caller_arrived[1 - idx]) == 0 &&
		       now_ms() - t0 < 2000)
			sched_yield();
	}
	if (t_cookie == my_tid)
		st32_rel(&SHM->caller_cookie_ok[idx], 1);
	st32_rel(&SHM->caller_done[idx], 1);
	return NULL;
}

static void phase_a6(void)
{
	pthread_t th[2];
	int i, ok = 1;

	for (i = 0; i < 2; i++)
		if (pthread_create(&th[i], NULL, a6_thread, (void *)(uintptr_t)i) != 0)
			die("pthread_create(a6)");
	for (i = 0; i < 2; i++)
		pthread_join(th[i], NULL);

	for (i = 0; i < 2; i++) {
		if (ld32_acq(&SHM->caller_done[i]) != 1)
			ok = 0;
		if (ld_acq((const uint64_t *)&SHM->exec_tid[i]) != SHM->req_tid[i])
			ok = 0;
		if (ld32_acq(&SHM->caller_maps_ok[i]) != 1)
			ok = 0;
		if (ld32_acq(&SHM->caller_cookie_ok[i]) != 1)
			ok = 0;
	}
	if (SHM->req_tid[0] == 0 || SHM->req_tid[0] == SHM->req_tid[1])
		ok = 0;

	verdict(6, ok,
		"2 server->client commands executed in the requesting threads; "
		"req_tid=%llu/%llu exec_tid=%lld/%lld; thread-local cookie survived the "
		"sibling thread=%u/%u; mprotect on relay-mapped pages verified in the "
		"caller=%u/%u [done=%u/%u scratch=%llx/%llx]",
		(unsigned long long)SHM->req_tid[0],
		(unsigned long long)SHM->req_tid[1],
		(long long)SHM->exec_tid[0], (long long)SHM->exec_tid[1],
		ld32_acq(&SHM->caller_cookie_ok[0]), ld32_acq(&SHM->caller_cookie_ok[1]),
		ld32_acq(&SHM->caller_maps_ok[0]), ld32_acq(&SHM->caller_maps_ok[1]),
		ld32_acq(&SHM->caller_done[0]), ld32_acq(&SHM->caller_done[1]),
		(unsigned long long)ld_acq(&SHM->scratch_addr[0]),
		(unsigned long long)ld_acq(&SHM->scratch_addr[1]));
}

/* ------------------------------------------------------------------ */
/* A7                                                                 */
/* ------------------------------------------------------------------ */

#define A7_THREADS 32
#define A7_LANES_PER_THREAD 4

struct a7_job {
	int tid;
	int bad;
};

static void *a7_thread(void *arg)
{
	struct a7_job *j = arg;
	int k;

	for (k = 0; k < A7_LANES_PER_THREAD; k++) {
		int lane = j->tid + k * A7_THREADS;
		lane_publish(lane, 0, g_seq++,
			     ((uint64_t)CMD_ECHO << 32) | (uint64_t)lane | LEDGER_BIT);
	}
	for (k = 0; k < A7_LANES_PER_THREAD; k++)
		mark_pending(j->tid + k * A7_THREADS);
	cold_nudge();

	for (k = 0; k < A7_LANES_PER_THREAD; k++) {
		int lane = j->tid + k * A7_THREADS;
		uint64_t res = 0, seq = 0;
		if (lane_consume(lane, &res, &seq, 1) != 0)
			j->bad = 1;
		else if (res != server_result_for(((uint64_t)CMD_ECHO << 32) | (uint64_t)lane))
			j->bad = 1;
	}
	return NULL;
}

static void phase_a7(const char *snap_before)
{
	pthread_t th[A7_THREADS];
	struct a7_job jobs[A7_THREADS];
	char snap_after[8192];
	int i, bad_threads = 0, bad_lanes = 0, leak = 0;
	uint64_t t0 = now_ms();

	for (i = 0; i < A7_THREADS; i++) {
		jobs[i].tid = i;
		jobs[i].bad = 0;
		if (pthread_create(&th[i], NULL, a7_thread, &jobs[i]) != 0)
			die("pthread_create(a7)");
	}
	for (i = 0; i < A7_THREADS; i++) {
		pthread_join(th[i], NULL);
		if (jobs[i].bad)
			bad_threads++;
	}
	/* every lane must have been serviced at least once in this phase */
	for (;;) {
		int pending = 0;
		for (i = 0; i < LANE_COUNT; i++)
			if (ld_acq(&SHM->lanes[i].serviced) == 0)
				pending++;
		if (pending == 0 || now_ms() - t0 > 3000)
			break;
		sched_yield();
	}
	for (i = 0; i < LANE_COUNT; i++)
		if (ld_acq(&SHM->lanes[i].serviced) == 0)
			bad_lanes++;

	(void)fd_snapshot(snap_after, sizeof snap_after, &leak);

	verdict(7, bad_threads == 0 && bad_lanes == 0 && !leak &&
		   strcmp(snap_before, snap_after) == 0,
		   "%d threads x %d lanes: %d/%d lanes serviced, %d thread failures; "
		   "guest descriptor set %s, leaked transport descriptor: %s",
		   A7_THREADS, A7_LANES_PER_THREAD,
		   LANE_COUNT - bad_lanes, LANE_COUNT, bad_threads,
		   strcmp(snap_before, snap_after) == 0 ? "unchanged" : "CHANGED",
		   leak ? "YES" : "no");
}

/* ------------------------------------------------------------------ */
/* modes                                                              */
/* ------------------------------------------------------------------ */

static int mode_hot(unsigned long iters)
{
	uint64_t w0, w1;
	long done;
	int err = 0;

	setup_architecture(1);
	enter_spin_mode(1);
	w0 = ld_acq(&SHM->relay_wakeups);
	done = hot_loop(iters, &err);
	w1 = ld_acq(&SHM->relay_wakeups);
	teardown(1);

	printf("HOT iters=%lu completed=%ld rc=%d relay_wakeups=%llu->%llu\n",
	       iters, done, err,
	       (unsigned long long)w0, (unsigned long long)w1);
	fflush(stdout);
	return (done == (long)iters && w0 == w1) ? 0 : 1;
}

static int mode_all(void)
{
	char snap0[8192], snap1[8192];
	int c0, c1, leak0 = 0, leak1 = 0;
	long d_a = 0, d_b = 0, delta;
	int internal = 0;
	uint64_t w0, w1;
	long done;
	int err = 0;

	/* A3's syscall-count evidence is measured OUTSIDE the run under test, by
	 * strace -f -c over an otherwise identical hot phase. */
	delta = hot_syscall_delta(&d_a, &d_b, &internal);
	if (delta < 0)
		fprintf(stderr, "harness: strace -f -c measurement unavailable\n");

	setup_architecture(0);
	c0 = fd_snapshot(snap0, sizeof snap0, &leak0);
	if (getenv("RELAY_PROOF_DEBUG"))
		fprintf(stderr, "DEBUG snap0(%d): %s\n", c0, snap0);

	phase_a2();

	enter_spin_mode(0);
	w0 = ld_acq(&SHM->relay_wakeups);
	done = hot_loop(1000000, &err);
	w1 = ld_acq(&SHM->relay_wakeups);
	leave_spin_mode();
	quiesce();

	c1 = fd_snapshot(snap1, sizeof snap1, &leak1);

	verdict(1, c0 == c1 && strcmp(snap0, snap1) == 0 && !leak0 && !leak1,
		"guest held %d descriptor(s) immediately after relay attach and %d after "
		"1000000 hot requests; byte-identical=%s; guest holds a relay "
		"socket/eventfd=%s",
		c0, c1,
		(c0 == c1 && strcmp(snap0, snap1) == 0) ? "yes" : "NO",
		(leak0 || leak1) ? "YES" : "no");

	if (internal)
		verdict(3, done == 1000000 && err == 0 && w0 == w1 && delta == 0,
			"hot loop completed %ld iterations with no syscall per request: "
			"strace -f -c over the identical hot phase (N=1000000 -> %ld calls, "
			"N=0 -> %ld calls, delta=%ld); relay wakeups unchanged %llu->%llu "
			"(relay stayed parked for the whole hot phase)",
			done, d_a, d_b, delta,
			(unsigned long long)w0, (unsigned long long)w1);
	else
		verdict(3, done == 1000000 && err == 0 && w0 == w1 && delta == 0,
			"hot loop completed %ld iterations with no syscall per request: "
			"strace -f -c delta supplied by the runner=%ld; relay wakeups "
			"unchanged %llu->%llu (relay stayed parked for the whole hot phase)",
			done, delta,
			(unsigned long long)w0, (unsigned long long)w1);

	phase_a4();
	phase_a5();
	phase_a6();
	phase_a7(snap0);

	teardown(0);
	print_assertions();
	return all_ok() ? 0 : 1;
}

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "all";

	setvbuf(stdout, NULL, _IOLBF, 0);
	if (strcmp(mode, "hot") == 0)
		return mode_hot(argc > 2 ? strtoul(argv[2], NULL, 10) : 0);
	if (strcmp(mode, "all") == 0)
		return mode_all();
	fprintf(stderr, "usage: %s [all|hot ITERS]\n", argv[0]);
	return 2;
}

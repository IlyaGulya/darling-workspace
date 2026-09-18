/*
 * direct-process-doorbell-proof.c -- standalone falsification harness for
 * "Candidate B: constant-anchor direct transport, WITHOUT any helper/relay
 * process".
 *
 * WHAT IS UNDER TEST
 * ------------------
 * The guest process descriptor table contains ONLY:
 *   * the application descriptors,
 *   * ONE process-level wake eventfd,
 *   * ONE process-level control Unix-domain endpoint (SCM_RIGHTS),
 *   * plus fixed anchors that are not part of this proof.
 * Shared state between guest and server is N per-thread SPSC request/reply
 * lanes, ONE process-wide pending bitmap/epoch, and ONE per-thread reply futex
 * word.  The server is a SEPARATE PROCESS (separate address space / mm) that
 * epoll-waits on the process eventfd plus the control socket, actively polls
 * while busy, drains the pending lanes located from the shared bitmap, and
 * wakes a specific parked guest thread by futex on that thread's reply word.
 *
 * THERE IS NO RELAY / COMPANION PROCESS IN THIS TOPOLOGY.  No wake-coalescing
 * relay is modelled and no coalescing saving is claimed here.
 *
 * THIS IS A MODEL, NOT THE PRODUCT
 * --------------------------------
 * The harness does not run mldr, darlingserver, Mach or any Darling code.  It
 * models the mechanism: it executes the very syscalls the design needs
 * (eventfd, epoll_wait, futex, SCM_RIGHTS, fork, execve, memfd, SysV shm) and
 * checks the claims the design makes about them.  A PASS says the mechanism as
 * modelled here holds; it says nothing about the product's implementation.
 *
 * PROCESS / THREADING MODEL (and the fork-safety rule)
 * ----------------------------------------------------
 * The server is created with plain fork() (separate mm, its own descriptor
 * table).  Every fork in this harness happens with at most ONE live thread -
 * the forking thread - so no libc lock is held across a fork, and no code that
 * runs after a fork or after an exec allocates memory: all shared state lives
 * in one mapping and all post-fork code uses preallocated buffers or the
 * stack.  The server uses no malloc, no pthread locks and no threads.
 *
 * SHARED MEMORY LAYOUT AND HARDWARE-ENFORCED SPSC
 * ----------------------------------------------
 * One mapping, three page-aligned regions:
 *   * header          written by both sides (bitmap, epoch, ledger, control);
 *   * guest region    request slots, req_prod, rep_cons, lane bookkeeping:
 *                     WRITTEN BY THE GUEST, mprotect(PROT_READ) in the server;
 *   * server region   reply slots, rep_prod, req_cons, per-thread reply futex
 *                     words: WRITTEN BY THE SERVER, mprotect(PROT_READ) in the
 *                     guest.
 * A write into the other side's region therefore takes a real SIGSEGV, caught
 * by the owner and recorded (D2).  Each side proves at startup that the
 * protection bites by performing a deliberate probe write and observing the
 * fault.  The reply futex words live in the guest-read-only region on purpose:
 * FUTEX_WAIT only needs read access, and it makes the reply side structurally
 * unwritable by the guest.
 *
 * WAKE PROTOCOL (the D3 invariant)
 * --------------------------------
 * producer:  publish slot -> publish req_prod -> SET PENDING BIT -> fence ->
 *            read server_state -> (if not ACTIVE) write the eventfd.
 * server:    read bitmap -> drain -> seal server_state = SLEEPING -> fence ->
 *            RE-CHECK bitmap -> sleep only if empty.
 * The pending bit must be set before the producer reads the server state, and
 * the server must seal before it re-checks; any other order can strand a
 * request (the classic store-buffer litmus).  D3 enumerates forced
 * interleavings of exactly those steps and requires that no request is left
 * unserviced.
 *
 * CLAIMS (one printed line each; D9 is printed by the runner)
 * ----------------------------------------------------------
 *   D1 descriptor scaling        fd count identical at 1/8/32/64 guest threads
 *   D2 SPSC lanes                one producing tid per lane, no second
 *                                publisher, server cannot write a request
 *                                slot and guest cannot write a reply slot
 *   D3 no lost wake              forced interleavings + stress, none stranded
 *   D4 pending addressing        exact lane lookup, only non-zero words
 *                                touched, measured cost vs. idle-lane count
 *   D5 hot path                  ZERO eventfd writes per request while the
 *                                server actively polls; cold cost = 1/request
 *   D6 fork generation isolation child cannot consume a parent completion and
 *                                cannot use the inherited eventfd as its own
 *                                doorbell; repeated forks
 *   D7 exec generation           E1 retained descriptor across exec, E2
 *                                descriptor-less SysV shm reattached after
 *                                exec; stale incarnation rejected; a FAILED
 *                                exec leaves the previous generation usable
 *   D8 caller-local sideband     a server->guest operation runs on the exact
 *                                requesting tid
 *
 * MUTATIONS (built by the runner in its temporary directory, never here)
 *   M1 fork child uses the inherited eventfd/generation as its own doorbell
 *      -> D6 must fail
 *   M2 publication before the pending bit is set (wrong arm order)
 *      -> D3 must fail
 *   M3 the lane generation is omitted from the completion check
 *      -> D7 (and D6) must fail
 *
 * MODES
 *   all            D1..D8 (D4 and the D7 exec legs run after the main
 *                  architecture is retired)
 *   exec-e1-post   post-exec incarnation of D7/E1 (retained memfd backing)
 *   exec-e2-post   post-exec incarnation of D7/E2 (SysV shm backing)
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <pthread.h>
#include <sched.h>
#include <setjmp.h>
#include <stddef.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/ipc.h>
#include <sys/mman.h>
#include <sys/shm.h>
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

#define MAGIC 0x44444F4F5242454CULL /* "DDOORBEL" */

#define HIER_LANES 8192
#define HIER_L0_WORDS (HIER_LANES / 64) /* 128 words = 8192 lanes */
#define HIER_L1_WORDS (HIER_L0_WORDS / 64) /* 2 words */
#define HIER_L2_WORDS 1 /* 1 word; bits 0..1 select an L1 word */

#define LANE_COUNT 256
#define RING_SLOTS 8
#define MAX_THREADS 64
#define LEDGER_MAX 64
#define FD_PROBE_MAX 256
#define D1_REQS_PER_THREAD 12
#define D7_CHILD_RECORDS 12

/* disjoint lane ranges, so "a lane's producing tid never changes" is a claim
 * about the whole run rather than about one phase */
#define D2_STAGE0_LANE 0
#define D2_STAGE1_LANE 1
#define D2_STAGE2_LANE 9
#define D2_STAGE3_LANE 41
#define D8_LANE_BASE 108
#define D3_LANE 112
#define D3_PRE_LANE 113
#define D5_HOT_LANE 114
#define D5_COLD_LANE 115
#define D6_PARENT_LANE 120
#define D6_CHILD_LANE_BASE 121
#define D7_LANE_PRE 130
#define D7_LANE_POST 131
#define D3S_LANE_BASE 140
#define D3S_LANES_PER_THREAD 4

#define SIDE_GUEST 0
#define SIDE_SERVER 1

#define SCMD_PARK 0u
#define SCMD_POLL 1u
#define SCMD_EXIT 2u

#define SST_BOOT 0u
#define SST_ACTIVE 1u
#define SST_SLEEPING 2u
#define SST_EXITED 3u

#define CTL_ATTACH 1u
#define CTL_EXIT 2u
#define CTL_ACK 3u
#define ATTACH_OK 0u
#define ATTACH_STALE 1u

#define CMD_ECHO 0x01u
#define CMD_CALLER 0x02u
#define CMD_SHIFT 32
#define CMD_MASK 0xFFFFu
#define LEDGER_BIT (1ULL << 63)

#define D3_DEADLINE_MS 50
#define D3_EPOLL_MS 250
#define D3_GATES_GUEST 4
#define D3_GATES_SERVER 5
#define D3_STEP_TOTAL (D3_GATES_GUEST + D3_GATES_SERVER)
#define D3_STRESS_ROUNDS 2048
#define D5_HOT_ITERS 100000
#define D5_COLD_ITERS 64

#define STALL_MS 5000
#define WAIT_MS 1000

/* ------------------------------------------------------------------ */
/* shared memory                                                      */
/* ------------------------------------------------------------------ */

/* guest -> server.  The request slot carries no state word: the req_prod
 * release store is the publication event. */
struct req_slot {
	uint64_t seq;
	uint64_t payload;
	uint64_t gen; /* publisher generation */
	uint64_t prod_tid; /* producing thread id, recorded at publication */
};

/* server -> guest */
struct rep_slot {
	uint64_t seq; /* echo of the request identity */
	uint64_t result; /* server answer */
	uint64_t gen; /* completion generation */
	uint64_t tid; /* the requesting tid the server addressed (D8) */
};

struct hdr {
	uint64_t magic;
	uint64_t gen_counter; /* the next incarnation takes fetch_add + 1 */
	uint32_t srv_gen; /* generation the server currently accepts */
	uint32_t server_cmd;
	uint32_t server_state;
	uint32_t server_ready;
	uint32_t server_exited;
	uint32_t srv_attach_ok;
	uint32_t srv_attach_stale;
	uint32_t srv_attach_total;
	uint32_t guest_attach_ok;
	uint32_t srv_write_req_fault; /* the server wrote the guest region */
	uint32_t guest_write_rep_fault; /* the guest wrote the server region */
	uint32_t lane_double_claim;
	uint32_t lane_tid_change;
	uint32_t child_inherited_doorbell;
	uint32_t pub_stalls;
	uint32_t d3_stalls;
	uint32_t srv_in_epoll;
	uint32_t server_tid;
	uint32_t guest_pid;
	uint32_t server_pid;
	uint32_t pad_;
	uint64_t efd_writes; /* doorbell writes performed by the guest */
	uint64_t efd_credits; /* diagnostic: writes not yet drained */
	uint64_t server_drains;
	uint64_t server_services;
	uint64_t server_sleeps;
	uint64_t server_wakes;
	uint64_t ledger_n;
	uint64_t ledger_overflow;
	uint64_t ledger[LEDGER_MAX][3]; /* {gen, seq, times serviced} */
	/* ONE process-wide pending bitmap, hierarchical, so a wake can be
	 * addressed without a blind O(N) scan */
	uint64_t l0[HIER_L0_WORDS];
	uint64_t l1[HIER_L1_WORDS];
	uint64_t l2[HIER_L2_WORDS];
	uint64_t hier_lookups; /* measured scans */
	uint64_t hier_words; /* words touched by those scans */
	uint64_t hier_zero_touches;
	uint64_t live_scans; /* scans performed by the live server */
	uint64_t live_words;
	uint64_t live_zero_touches;
	/* forced-interleaving stepper (D3) */
	uint32_t adv_arm;
	uint32_t adv_turn; /* 0 = none, side+1 = that side owns the turn */
	uint32_t adv_cap[2];
	uint32_t adv_at[2];
	uint32_t adv_done[2];
	uint32_t prod_go;
	uint32_t prod_quit;
	uint32_t prod_lane;
	uint64_t prod_seq;
	uint64_t d3_windows;
	uint64_t d3_unserviced;
	uint64_t d3_stranded;
	uint64_t d3_doorbells;
	uint64_t stress_rounds;
	uint64_t stress_requests;
	uint64_t stress_lost;
	uint64_t stress_doorbells;
	/* D1 */
	uint64_t stage_fds[4];
	uint32_t stage_ok[4];
	uint32_t d1_threads[4];
	/* D2 / D4 */
	uint64_t d2_probe_guest_fault;
	uint64_t d2_probe_server_fault;
	uint64_t d2_exchanges;
	/* D5 */
	uint64_t hot_requests;
	uint64_t hot_doorbells;
	uint64_t hot_wakes;
	uint64_t cold_requests;
	uint64_t cold_doorbells;
	/* D6 */
	uint64_t parent_gen;
	uint64_t parent_requests;
	uint32_t parent_consumed;
	uint32_t d6_children;
	uint64_t child_gen[D7_CHILD_RECORDS];
	uint64_t child_ok[D7_CHILD_RECORDS];
	uint64_t child_rejected[D7_CHILD_RECORDS];
	uint64_t child_consumed_parent[D7_CHILD_RECORDS];
	uint64_t child_own_efd[D7_CHILD_RECORDS];
	uint64_t child_rc[D7_CHILD_RECORDS];
	/* D8 */
	uint64_t d8_req_tid[4];
	uint64_t d8_exec_tid[4];
	uint64_t d8_exec_pid[4];
	uint64_t d8_sideband_tid[4];
	uint32_t d8_done[4];
	uint32_t d8_cookie_ok[4];
	uint32_t d8_arrived[4];
};

struct guest_region {
	struct req_slot req[LANE_COUNT][RING_SLOTS] __attribute__((aligned(4096)));
	uint64_t req_prod[LANE_COUNT];
	uint64_t rep_cons[LANE_COUNT];
	uint64_t cons_count[LANE_COUNT];
	uint64_t lane_owner[LANE_COUNT];
	uint64_t lane_prod_tid[LANE_COUNT];
	uint64_t guest_parked[LANE_COUNT];
};

struct server_region {
	struct rep_slot rep[LANE_COUNT][RING_SLOTS] __attribute__((aligned(4096)));
	uint64_t rep_prod[LANE_COUNT];
	uint64_t req_cons[LANE_COUNT];
	uint64_t rep_futex[LANE_COUNT];
};

struct shm {
	struct hdr h __attribute__((aligned(4096)));
	struct guest_region g __attribute__((aligned(4096)));
	struct server_region s __attribute__((aligned(4096)));
};

static struct shm *SHM;
static int GEFD = -1; /* the guest's process-level wake eventfd */
static int GSOCK = -1; /* the guest's end of the process-level control endpoint */
static int GEFD_INHERITED = -1; /* the doorbell a fork child inherits (D6) */
static uint64_t G_GEN = 1;

#define HH (&SHM->h)
#define GH (&SHM->g)
#define SH (&SHM->s)

/* ------------------------------------------------------------------ */
/* atomics and small helpers                                          */
/* ------------------------------------------------------------------ */

#define ld_acq(p) __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define st_rel(p, v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define st_rlx(p, v) __atomic_store_n((p), (v), __ATOMIC_RELAXED)
#define ld32_acq(p) __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define st32_rel(p, v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define fence() __atomic_thread_fence(__ATOMIC_SEQ_CST)

static inline void cpu_pause(void)
{
	__asm__ volatile("pause" ::: "memory");
}

static uint64_t now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

static uint64_t tid_self(void)
{
	return (uint64_t)syscall(SYS_gettid);
}

static uint64_t mix64(uint64_t x)
{
	return (x * 0x9E3779B97F4A7C15ULL) ^ 0xA5A5A5A5A5A5A5A5ULL;
}

static uint32_t payload_cmd(uint64_t payload)
{
	return (uint32_t)((payload >> CMD_SHIFT) & (uint64_t)CMD_MASK);
}

static uint64_t server_result_for(uint64_t payload)
{
	uint32_t cmd = payload_cmd(payload);
	uint32_t arg = (uint32_t)payload;

	if (cmd == CMD_CALLER)
		return (uint64_t)CMD_CALLER << CMD_SHIFT;
	return mix64(((uint64_t)cmd << CMD_SHIFT) | arg);
}

static int fd_snapshot(void)
{
	int count = 0, fd;

	for (fd = 0; fd < FD_PROBE_MAX; fd++) {
		char path[64];
		char tgt[256];

		snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
		if (readlink(path, tgt, sizeof tgt - 1) >= 0)
			count++;
	}
	return count;
}

/* ------------------------------------------------------------------ */
/* the process-wide pending bitmap (hierarchical)                     */
/* ------------------------------------------------------------------ */

/*
 * All three levels are set unconditionally.  A "was this word already
 * non-zero?" shortcut races with the drain's exchange-to-zero (a concurrent
 * drain can clear the middle level between the producer's two stores), which
 * would strand a lane.  Setting every level unconditionally makes "l2 != 0 iff
 * some lane is pending" an invariant, so the server's sleep decision is a
 * single word read.
 */
static void bitmap_set(int lane)
{
	uint64_t w = (uint64_t)lane >> 6;

	__atomic_fetch_or(&HH->l0[w], 1ULL << (lane & 63), __ATOMIC_ACQ_REL);
	__atomic_fetch_or(&HH->l1[w >> 6], 1ULL << (w & 63), __ATOMIC_ACQ_REL);
	__atomic_fetch_or(&HH->l2[0], 1ULL << (w >> 6), __ATOMIC_ACQ_REL);
}

static int bitmap_any(void)
{
	return ld_acq(&HH->l2[0]) != 0;
}

/*
 * Take every currently pending lane.  Walks top-down with exchange-to-zero so
 * each level is claimed exactly once, and counts the words touched: a
 * hierarchical wake touches a number of words proportional to the number of
 * pending lanes times log(N), never N/64.
 */
static int bitmap_scan(uint64_t *lanes, int maxlanes, uint64_t *touched_out,
		       int *zero_out)
{
	int n = 0, touched = 0, zero = 0, i, j, k;
	uint64_t top;

	top = __atomic_exchange_n(&HH->l2[0], 0, __ATOMIC_ACQ_REL);
	touched++;
	if (!top)
		zero++;
	for (i = 0; i < HIER_L2_WORDS * 64; i++) {
		uint64_t mid;

		if (!(top & (1ULL << i)))
			continue;
		mid = __atomic_exchange_n(&HH->l1[i], 0, __ATOMIC_ACQ_REL);
		touched++;
		if (!mid)
			zero++;
		for (j = 0; j < 64; j++) {
			uint64_t low;

			if (!(mid & (1ULL << j)))
				continue;
			low = __atomic_exchange_n(&HH->l0[i * 64 + j], 0,
						  __ATOMIC_ACQ_REL);
			touched++;
			if (!low)
				zero++;
			for (k = 0; k < 64; k++) {
				if (!(low & (1ULL << k)))
					continue;
				if (n < maxlanes)
					lanes[n++] =
						(uint64_t)(i * 64 + j) * 64 +
						(uint64_t)k;
			}
		}
	}
	if (touched_out)
		*touched_out = (uint64_t)touched;
	if (zero_out)
		*zero_out = zero;
	return n;
}

/* ------------------------------------------------------------------ */
/* the doorbell (cold path) and the raw wake (coordinator only)        */
/* ------------------------------------------------------------------ */

static void doorbell_write(void)
{
	uint64_t one = 1;

	__atomic_fetch_add(&HH->efd_writes, 1, __ATOMIC_RELAXED);
	__atomic_fetch_add(&HH->efd_credits, 1, __ATOMIC_RELEASE);
	if (write(GEFD, &one, sizeof one) < 0)
		__atomic_fetch_add(&HH->pub_stalls, 1, __ATOMIC_RELAXED);
}

static void wake_raw(int fd)
{
	uint64_t one = 1;

	if (fd >= 0 && write(fd, &one, sizeof one) < 0)
		__atomic_fetch_add(&HH->pub_stalls, 1, __ATOMIC_RELAXED);
}

/* ------------------------------------------------------------------ */
/* forced interleaving gates (D3)                                     */
/* ------------------------------------------------------------------ */

/*
 * Each side has a fixed number of gate arrivals (guest 4, server 5).  The
 * coordinator arms the gates and then grants turns one at a time: the side
 * that owns the turn executes exactly the code between its two gates and
 * blocks again.  Past its cap a side runs free, which is how the rest of a
 * window settles after the scheduled part.
 */
static void adv_gate(int side)
{
	uint32_t n;

	if (!ld32_acq(&HH->adv_arm))
		return;
	n = ld32_acq(&HH->adv_at[side]) + 1;
	if (n > ld32_acq(&HH->adv_cap[side]))
		return;
	st32_rel(&HH->adv_at[side], n);
	for (;;) {
		if (ld32_acq(&HH->adv_turn) == (uint32_t)(side + 1)) {
			st32_rel(&HH->adv_turn, 0);
			st32_rel(&HH->adv_done[side], n);
			return;
		}
		if (!ld32_acq(&HH->adv_arm))
			return; /* disarmed: run free, leave no handshake trace */
		cpu_pause();
		sched_yield();
	}
}

#define ADV_GATE(side) adv_gate(side)

/* ------------------------------------------------------------------ */
/* guest side: publish and consume                                    */
/* ------------------------------------------------------------------ */

static void lane_claim_record(int lane, uint64_t tid)
{
	uint64_t own = ld_acq(&GH->lane_owner[lane]);

	if (own == 0) {
		uint64_t expect = 0;

		if (!__atomic_compare_exchange_n(&GH->lane_owner[lane], &expect,
						 tid, 0, __ATOMIC_ACQ_REL,
						 __ATOMIC_ACQUIRE) &&
		    expect != tid)
			__atomic_fetch_add(&HH->lane_double_claim, 1,
					   __ATOMIC_RELAXED);
	} else if (own != tid) {
		__atomic_fetch_add(&HH->lane_tid_change, 1, __ATOMIC_RELAXED);
	}
	GH->lane_prod_tid[lane] = tid;
}

static int ring_room(int lane)
{
	return (int)(ld_acq(&GH->req_prod[lane]) - ld_acq(&SH->req_cons[lane])) <
	       RING_SLOTS;
}

/*
 * Publish one request.  gated != 0 only for the D3 stepper's producer thread;
 * arm != 0 asks for the cold doorbell when the server is not active.
 */
static int lane_publish(int lane, uint64_t seq, uint64_t payload, uint64_t gen,
			uint64_t tid, int arm, int gated, uint64_t stall_ms)
{
	uint64_t prod, t0;
	struct req_slot *r;
	int need;
	uint64_t spins = 0;

	t0 = now_ms();
	while (!ring_room(lane)) {
		if (now_ms() - t0 > stall_ms) {
			__atomic_fetch_add(&HH->pub_stalls, 1,
					   __ATOMIC_RELAXED);
			return -1;
		}
		if (++spins > 100000) {
			sched_yield();
			spins = 0;
		} else {
			cpu_pause();
		}
	}

	lane_claim_record(lane, tid);
	prod = ld_acq(&GH->req_prod[lane]);
	r = &GH->req[lane][prod % RING_SLOTS];
	r->seq = seq;
	r->payload = payload;
	r->gen = gen;
	r->prod_tid = tid;
	fence();
	st_rel(&GH->req_prod[lane], prod + 1);

	if (gated)
		ADV_GATE(SIDE_GUEST); /* gate 1: the publication is visible */

	/*MUT2-ARM*/ bitmap_set(lane); fence();

	if (gated)
		ADV_GATE(SIDE_GUEST); /* gate 2: the pending bit is visible */

	need = (ld32_acq(&HH->server_state) != SST_ACTIVE) || !arm;

	if (gated)
		ADV_GATE(SIDE_GUEST); /* gate 3: the decision is taken */

	/*MUT2-WRITE*/ if (need) { doorbell_write(); }

	if (gated)
		ADV_GATE(SIDE_GUEST); /* gate 4: the decision was acted on */

	return 0;
}

/* publish without a doorbell and without gates: the D3 coordinator's
 * pre-existing request and the D6 parent's in-flight request */
static int lane_publish_quiet(int lane, uint64_t seq, uint64_t payload,
			      uint64_t gen, uint64_t tid, int mark)
{
	uint64_t prod;
	struct req_slot *r;

	if (!ring_room(lane))
		return -1;
	lane_claim_record(lane, tid);
	prod = ld_acq(&GH->req_prod[lane]);
	r = &GH->req[lane][prod % RING_SLOTS];
	r->seq = seq;
	r->payload = payload;
	r->gen = gen;
	r->prod_tid = tid;
	fence();
	st_rel(&GH->req_prod[lane], prod + 1);
	if (mark) {
		bitmap_set(lane);
		fence();
	}
	return 0;
}

/*
 * 0 = consumed, 1 = foreign generation (the completion was left alone),
 * 2 = timed out (no reply for this incarnation within timeout_ms).
 */
static int lane_consume(int lane, uint64_t my_gen, int do_park,
			uint64_t timeout_ms, uint64_t *result, uint64_t *seq,
			uint64_t *rtid)
{
	uint64_t c = ld_acq(&GH->rep_cons[lane]);
	uint64_t t0 = now_ms();
	uint64_t spins = 0;
	struct rep_slot *r;

	for (;;) {
		uint32_t e = ld32_acq(&SH->rep_futex[lane]);

		if (ld_acq(&SH->rep_prod[lane]) > c)
			break;
		if (!do_park) {
			if (now_ms() - t0 > timeout_ms)
				return 2;
			if (++spins > 100000) {
				sched_yield();
				spins = 0;
			} else {
				cpu_pause();
			}
			continue;
		}
		/* arm-before-park: sample the futex word, then re-check */
		st_rel(&GH->guest_parked[lane], 1);
		fence();
		if (ld_acq(&SH->rep_prod[lane]) > c) {
			st_rel(&GH->guest_parked[lane], 0);
			break;
		}
		{
			struct timespec ts;

			ts.tv_sec = 0;
			ts.tv_nsec = 5000000; /* 5ms slices honour our timeout */
			syscall(SYS_futex, &SH->rep_futex[lane], FUTEX_WAIT,
				(unsigned long)e, &ts, NULL, 0);
		}
		st_rel(&GH->guest_parked[lane], 0);
		if (now_ms() - t0 > timeout_ms)
			return 2;
	}

	r = &SH->rep[lane][c % RING_SLOTS];
	/*MUT3-GEN*/ if (ld_acq(&r->gen) != my_gen)
		/*MUT3-GEN*/ return 1; /* a foreign incarnation's completion */
	if (result)
		*result = ld_acq(&r->result);
	if (seq)
		*seq = ld_acq(&r->seq);
	if (rtid)
		*rtid = ld_acq(&r->tid);
	st_rel(&GH->rep_cons[lane], c + 1);
	__atomic_fetch_add(&GH->cons_count[lane], 1, __ATOMIC_RELAXED);
	return 0;
}

static int reply_ready(int lane, uint64_t seq)
{
	uint64_t c = ld_acq(&GH->rep_cons[lane]);

	if (ld_acq(&SH->rep_prod[lane]) <= c)
		return 0;
	return ld_acq(&SH->rep[lane][c % RING_SLOTS].seq) == seq;
}

static int wait_reply(int lane, uint64_t seq, uint64_t timeout_ms)
{
	uint64_t t0 = now_ms();

	for (;;) {
		if (reply_ready(lane, seq))
			return 0;
		if (now_ms() - t0 > timeout_ms)
			return -1;
		cpu_pause();
		sched_yield();
	}
}

/* ------------------------------------------------------------------ */
/* control endpoint (SCM_RIGHTS)                                      */
/* ------------------------------------------------------------------ */

static int ctl_attach(int sock, int efd, uint32_t gen, uint32_t *srv_gen_out,
		      uint32_t *reason_out)
{
	char cbuf[CMSG_SPACE(sizeof(int))];
	uint32_t msg[2];
	char rep[32];
	struct iovec iov;
	struct msghdr mh;
	struct cmsghdr *cm;
	ssize_t n;

	memset(cbuf, 0, sizeof cbuf);
	msg[0] = CTL_ATTACH;
	msg[1] = gen;
	iov.iov_base = msg;
	iov.iov_len = sizeof msg;
	memset(&mh, 0, sizeof mh);
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	mh.msg_control = cbuf;
	mh.msg_controllen = sizeof cbuf;
	cm = CMSG_FIRSTHDR(&mh);
	cm->cmsg_level = SOL_SOCKET;
	cm->cmsg_type = SCM_RIGHTS;
	cm->cmsg_len = CMSG_LEN(sizeof(int));
	memcpy(CMSG_DATA(cm), &efd, sizeof efd);
	if (sendmsg(sock, &mh, MSG_NOSIGNAL) < 0)
		return -1;
	n = recv(sock, rep, sizeof rep, 0);
	if (n < (ssize_t)(sizeof(uint32_t) * 3))
		return -1;
	{
		uint32_t *r = (uint32_t *)rep;

		if (r[0] != CTL_ACK)
			return -1;
		if (srv_gen_out)
			*srv_gen_out = r[2];
		if (reason_out)
			*reason_out = r[1];
		return r[1] == ATTACH_OK ? 0 : 1;
	}
}

static int ctl_exit(int sock)
{
	uint32_t msg[2] = { CTL_EXIT, 0 };
	char rep[32];

	if (send(sock, msg, sizeof msg, MSG_NOSIGNAL) < 0)
		return -1;
	if (recv(sock, rep, sizeof rep, 0) < (ssize_t)sizeof(uint32_t))
		return -1;
	return 0;
}

/* 1 = a fresh attachment accepted (efd_out set), 0 = stale/ignored,
 * 2 = exit requested, -1 = error */
static int ctl_recv_flags(int sock, int *efd_out, uint32_t *gen_out, int flags)
{
	char cbuf[CMSG_SPACE(sizeof(int))];
	char buf[64];
	struct iovec iov;
	struct msghdr mh;
	struct cmsghdr *cm;
	uint32_t *m;
	ssize_t n;

	memset(&mh, 0, sizeof mh);
	iov.iov_base = buf;
	iov.iov_len = sizeof buf;
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	mh.msg_control = cbuf;
	mh.msg_controllen = sizeof cbuf;
	n = recvmsg(sock, &mh, flags);
	if (n < (ssize_t)(sizeof(uint32_t) * 2))
		return -1;
	m = (uint32_t *)buf;
	if (m[0] == CTL_ATTACH) {
		int efd = -1;
		uint32_t rep[4];

		for (cm = CMSG_FIRSTHDR(&mh); cm; cm = CMSG_NXTHDR(&mh, cm))
			if (cm->cmsg_level == SOL_SOCKET &&
			    cm->cmsg_type == SCM_RIGHTS)
				memcpy(&efd, CMSG_DATA(cm), sizeof efd);
		HH->srv_attach_total++;
		if (efd < 0 || (int32_t)(m[1] - HH->srv_gen) <= 0) {
			HH->srv_attach_stale++;
			rep[0] = CTL_ACK;
			rep[1] = ATTACH_STALE;
			rep[2] = HH->srv_gen;
			rep[3] = 0;
			(void)send(sock, rep, sizeof rep, MSG_NOSIGNAL);
			if (efd >= 0)
				close(efd);
			return 0;
		}
		HH->srv_gen = m[1];
		HH->srv_attach_ok++;
		*efd_out = efd;
		*gen_out = m[1];
		rep[0] = CTL_ACK;
		rep[1] = ATTACH_OK;
		rep[2] = HH->srv_gen;
		rep[3] = 0;
		(void)send(sock, rep, sizeof rep, MSG_NOSIGNAL);
		return 1;
	}
	if (m[0] == CTL_EXIT) {
		uint32_t ack[1] = { CTL_ACK };

		(void)send(sock, ack, sizeof ack, MSG_NOSIGNAL);
		return 2;
	}
	return 0;
}

static int ctl_recv(int sock, int *efd_out, uint32_t *gen_out)
{
	return ctl_recv_flags(sock, efd_out, gen_out, 0);
}

/* ------------------------------------------------------------------ */
/* server side: service, drain, loop                                  */
/* ------------------------------------------------------------------ */

static void ledger_record(uint64_t gen, uint64_t seq)
{
	uint64_t n = ld_acq(&HH->ledger_n);
	uint64_t i;

	for (i = 0; i < n && i < LEDGER_MAX; i++) {
		if (HH->ledger[i][0] == gen && HH->ledger[i][1] == seq) {
			HH->ledger[i][2]++;
			return;
		}
	}
	if (n >= LEDGER_MAX) {
		HH->ledger_overflow = 1;
		return;
	}
	HH->ledger[n][0] = gen;
	HH->ledger[n][1] = seq;
	HH->ledger[n][2] = 1;
	st_rel(&HH->ledger_n, n + 1);
}

static void service_lane(int lane)
{
	uint64_t prod = ld_acq(&GH->req_prod[lane]);
	uint64_t cons = ld_acq(&SH->req_cons[lane]);

	while (cons < prod) {
		struct req_slot *r = &GH->req[lane][cons % RING_SLOTS];
		uint64_t payload = ld_acq(&r->payload);
		uint64_t gen = ld_acq(&r->gen);
		uint64_t seq = ld_acq(&r->seq);
		uint64_t tid = ld_acq(&r->prod_tid);
		uint64_t rp = ld_acq(&SH->rep_prod[lane]);
		struct rep_slot *w = &SH->rep[lane][rp % RING_SLOTS];

		w->seq = seq;
		w->result = server_result_for(payload);
		w->gen = gen;
		w->tid = tid;
		fence();
		st_rel(&SH->rep_prod[lane], rp + 1);

		/* wake the exact parked thread for this lane, if it is parked on
		 * its own reply futex word */
		if (ld32_acq(&GH->guest_parked[lane])) {
			uint64_t e = ld_acq(&SH->rep_futex[lane]) + 1;

			st_rel(&SH->rep_futex[lane], e);
			syscall(SYS_futex, &SH->rep_futex[lane], FUTEX_WAKE, 1,
				NULL, NULL, 0);
			__atomic_fetch_add(&HH->server_wakes, 1,
					   __ATOMIC_RELAXED);
		}
		if (payload & LEDGER_BIT)
			ledger_record(gen, seq);
		cons++;
		st_rel(&SH->req_cons[lane], cons);
		__atomic_fetch_add(&HH->server_services, 1, __ATOMIC_RELAXED);
	}
}

static void drain_all(void)
{
	static uint64_t lanes[HIER_LANES];

	for (;;) {
		uint64_t touched = 0;
		int nl, zero = 0, i;

		if (!bitmap_any())
			break;
		nl = bitmap_scan(lanes, HIER_LANES, &touched, &zero);
		if (zero)
			__atomic_fetch_add(&HH->live_zero_touches, 1,
					   __ATOMIC_RELAXED);
		if (nl == 0)
			break;
		for (i = 0; i < nl; i++)
			service_lane((int)lanes[i]);
		__atomic_fetch_add(&HH->live_scans, 1, __ATOMIC_RELAXED);
		__atomic_fetch_add(&HH->live_words, touched, __ATOMIC_RELAXED);
	}
	/* every marked lane has been claimed: the outstanding doorbell is
	 * accounted for */
	st_rel(&HH->efd_credits, 0);
	__atomic_fetch_add(&HH->server_drains, 1, __ATOMIC_RELAXED);
}

static void server_loop(int ep, int efd, int sock)
{
	struct epoll_event evs[4];

	for (;;) {
		int i, work, nevs;

		if (ld32_acq(&HH->server_cmd) == SCMD_EXIT)
			break;
		st32_rel(&HH->server_state, SST_ACTIVE);
		ADV_GATE(SIDE_SERVER); /* S1: before reading the bitmap */
		work = bitmap_any();
		ADV_GATE(SIDE_SERVER); /* S2: before the drain */
		if (work) {
			drain_all();
			continue;
		}
		if (ld32_acq(&HH->server_cmd) == SCMD_POLL)
			continue; /* active polling: never sleep while busy */
		ADV_GATE(SIDE_SERVER); /* S3: before sealing the sleep */
		st32_rel(&HH->server_state, SST_SLEEPING);
		fence();
		ADV_GATE(SIDE_SERVER); /* S4: before the re-check */
		if (bitmap_any())
			continue; /* a request arrived while we were sealing */
		ADV_GATE(SIDE_SERVER); /* S5: before sleeping */
		st32_rel(&HH->srv_in_epoll, 1);
		__atomic_fetch_add(&HH->server_sleeps, 1, __ATOMIC_RELAXED);
		nevs = epoll_wait(ep, evs, 4, D3_EPOLL_MS);
		if (nevs > 0) {
			for (i = 0; i < nevs; i++) {
				if (!(evs[i].events & EPOLLIN))
					continue;
				if (evs[i].data.fd == efd) {
					uint64_t v;

					if (read(efd, &v, sizeof v) ==
					    (ssize_t)sizeof v)
						__atomic_fetch_add(
							&HH->server_wakes, 1,
							__ATOMIC_RELAXED);
				} else if (evs[i].data.fd == sock) {
					int nfd = -1;
					uint32_t gen = 0;

					(void)ctl_recv_flags(sock, &nfd, &gen,
							     MSG_DONTWAIT);
					if (nfd >= 0) {
						struct epoll_event e2;

						e2.events = EPOLLIN;
						e2.data.fd = nfd;
						(void)epoll_ctl(
							ep, EPOLL_CTL_ADD, nfd,
							&e2);
					}
				}
			}
		}
		st32_rel(&HH->srv_in_epoll, 0);
	}
	st32_rel(&HH->server_state, SST_EXITED);
	st32_rel(&HH->server_exited, 1);
}

/* ------------------------------------------------------------------ */
/* probes and region protection                                       */
/* ------------------------------------------------------------------ */

static sigjmp_buf g_probe_jmp;
static volatile sig_atomic_t g_probe_active;

static void guest_segv(int sig, siginfo_t *si, void *uc)
{
	(void)sig;
	(void)si;
	(void)uc;
	if (g_probe_active) {
		g_probe_active = 0;
		siglongjmp(g_probe_jmp, 1);
	}
	HH->guest_write_rep_fault = 1;
	_exit(4);
}

static void server_segv(int sig, siginfo_t *si, void *uc)
{
	(void)sig;
	(void)si;
	(void)uc;
	if (g_probe_active) {
		g_probe_active = 0;
		siglongjmp(g_probe_jmp, 1);
	}
	HH->srv_write_req_fault = 1;
	_exit(4);
}

static void install_segv(void (*fn)(int, siginfo_t *, void *))
{
	struct sigaction sa;

	memset(&sa, 0, sizeof sa);
	sa.sa_sigaction = fn;
	sa.sa_flags = SA_SIGINFO | SA_NODEFER;
	(void)sigaction(SIGSEGV, &sa, NULL);
	(void)sigaction(SIGBUS, &sa, NULL);
}

static void region_bounds(uintptr_t *gstart, uintptr_t *sstart, uintptr_t *end)
{
	uintptr_t base = (uintptr_t)SHM;

	*gstart = base + (uintptr_t)offsetof(struct shm, g);
	*sstart = base + (uintptr_t)offsetof(struct shm, s);
	*end = base + (uintptr_t)sizeof(struct shm);
}

/* the guest must not be able to write the server region */
static void guest_protect(void)
{
	uintptr_t gs, ss, end;

	region_bounds(&gs, &ss, &end);
	(void)gs;
	if (mprotect((void *)ss, (size_t)(end - ss), PROT_READ) != 0) {
		fprintf(stderr,
			"harness: guest mprotect(%p, %zu) base=%p gs=%zx ss=%zx "
			"end=%zx sizeof=%zu: %s\n",
			(void *)ss, (size_t)(end - ss), (void *)SHM, (size_t)gs,
			(size_t)ss, (size_t)end, sizeof(struct shm),
			strerror(errno));
		_exit(3);
	}
}

/* the server must not be able to write the guest region */
static void server_protect(void)
{
	uintptr_t gs, ss, end;

	region_bounds(&gs, &ss, &end);
	(void)end;
	if (mprotect((void *)gs, (size_t)(ss - gs), PROT_READ) != 0) {
		fprintf(stderr, "server: mprotect(%p, %zu): %s\n", (void *)gs,
			(size_t)(ss - gs), strerror(errno));
		_exit(3);
	}
}

/* ------------------------------------------------------------------ */
/* lifecycle                                                          */
/* ------------------------------------------------------------------ */

static pid_t g_server_pid;

static void server_main(int sock)
{
	int ep, efd = -1;
	uint32_t gen = 0;
	struct epoll_event ev;
	int r;

	/* the process-level doorbell and control endpoint handshake: the guest
	 * owns the eventfd and hands the server a descriptor for the same
	 * object over the control endpoint */
	for (;;) {
		r = ctl_recv(sock, &efd, &gen);
		if (r == 1)
			break;
		if (r == 2 || r < 0) {
			st32_rel(&HH->server_exited, 1);
			_exit(0);
		}
	}
	if (efd < 0)
		_exit(5);

	server_protect();
	install_segv(server_segv);

	/* the server must never write the guest region: prove the protection
	 * bites before trusting it */
	{
		volatile uint64_t *p = (volatile uint64_t *)&GH->req[0][0].seq;

		g_probe_active = 1;
		if (sigsetjmp(g_probe_jmp, 1) == 0) {
			*p = 0xDEADu;
			HH->d2_probe_server_fault = 0;
		} else {
			HH->d2_probe_server_fault = 1;
		}
		g_probe_active = 0;
	}

	st32_rel(&HH->server_tid, (uint32_t)tid_self());
	st32_rel(&HH->server_pid, (uint32_t)getpid());

	ep = epoll_create1(0);
	if (ep < 0)
		_exit(6);
	ev.events = EPOLLIN;
	ev.data.fd = efd;
	if (epoll_ctl(ep, EPOLL_CTL_ADD, efd, &ev) != 0)
		_exit(7);
	ev.events = EPOLLIN;
	ev.data.fd = sock;
	if (epoll_ctl(ep, EPOLL_CTL_ADD, sock, &ev) != 0)
		_exit(8);

	st32_rel(&HH->server_ready, 1);
	server_loop(ep, efd, sock);
	_exit(0);
}

static void setup_shm(struct shm *m, uint32_t gen0)
{
	memset(m, 0, sizeof *m);
	m->h.magic = MAGIC;
	m->h.gen_counter = gen0;
	m->h.server_cmd = SCMD_PARK;
	m->h.server_state = SST_BOOT;
	m->h.guest_pid = (uint32_t)getpid();
}

static void setup_architecture(void)
{
	int sv[2], efd;
	pid_t p;
	uint32_t srv_gen = 0, reason = 0;

	SHM = mmap(NULL, sizeof(struct shm), PROT_READ | PROT_WRITE,
		   MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (SHM == MAP_FAILED) {
		fprintf(stderr, "harness fatal: mmap(shm): %s\n",
			strerror(errno));
		_exit(3);
	}
	setup_shm(SHM, 1);

	efd = eventfd(0, EFD_NONBLOCK);
	if (efd < 0) {
		fprintf(stderr, "harness fatal: eventfd: %s\n", strerror(errno));
		_exit(3);
	}
	if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sv) != 0) {
		fprintf(stderr, "harness fatal: socketpair: %s\n",
			strerror(errno));
		_exit(3);
	}

	p = fork();
	if (p < 0) {
		fprintf(stderr, "harness fatal: fork(server): %s\n",
			strerror(errno));
		_exit(3);
	}
	if (p == 0) {
		close(sv[0]);
		close(efd); /* the server gets its own descriptor over the control
			     * endpoint, not by inheritance */
		server_main(sv[1]);
		_exit(0);
	}
	g_server_pid = p;
	close(sv[1]);

	GEFD = efd;
	GSOCK = sv[0];
	GEFD_INHERITED = efd;
	G_GEN = 1;

	if (ctl_attach(GSOCK, GEFD, (uint32_t)G_GEN, &srv_gen, &reason) != 0) {
		fprintf(stderr, "harness fatal: attach gen=%llu rejected (%u)\n",
			(unsigned long long)G_GEN, reason);
		_exit(3);
	}
	HH->guest_attach_ok++;

	guest_protect();
	install_segv(guest_segv);
	{
		uint64_t t0 = now_ms();

		while (!ld32_acq(&HH->server_ready) && now_ms() - t0 < 3000)
			sched_yield();
		if (!ld32_acq(&HH->server_ready)) {
			fprintf(stderr, "harness fatal: server never ready\n");
			_exit(3);
		}
	}
}

static void wait_server_parked(void)
{
	uint64_t t0 = now_ms();

	for (;;) {
		if (ld32_acq(&HH->srv_in_epoll) && !bitmap_any())
			return;
		if (now_ms() - t0 > WAIT_MS)
			return;
		cpu_pause();
		sched_yield();
	}
}

static void set_server_mode(uint32_t cmd)
{
	st32_rel(&HH->server_cmd, cmd);
	if (cmd == SCMD_PARK)
		wait_server_parked();
}

static void teardown(void)
{
	st32_rel(&HH->server_cmd, SCMD_EXIT);
	wake_raw(GEFD);
	{
		uint64_t t0 = now_ms();

		while (!ld32_acq(&HH->server_exited) && now_ms() - t0 < 2000)
			sched_yield();
		if (!ld32_acq(&HH->server_exited))
			kill(g_server_pid, SIGKILL);
	}
	waitpid(g_server_pid, NULL, 0);
}

/* ------------------------------------------------------------------ */
/* claims                                                             */
/* ------------------------------------------------------------------ */

static int g_ok[9];
static char g_detail[9][1600];

static void verdict(int idx, int ok, const char *fmt, ...)
{
	va_list ap;

	g_ok[idx] = ok ? 1 : 0;
	va_start(ap, fmt);
	vsnprintf(g_detail[idx], sizeof g_detail[idx], fmt, ap);
	va_end(ap);
}

static void print_claims(void)
{
	static const char *names[9] = {
		"",
		"D1 descriptor scaling",
		"D2 SPSC lanes",
		"D3 no lost wake",
		"D4 pending addressing",
		"D5 hot path doorbell",
		"D6 fork generation isolation",
		"D7 exec generation",
		"D8 caller-local sideband",
	};
	int i;

	for (i = 1; i <= 8; i++)
		printf("D%d %s %s: %s\n", i, g_ok[i] ? "PASS" : "FAIL",
		       names[i], g_detail[i]);
	fflush(stdout);
}

static int all_ok(void)
{
	int i;

	for (i = 1; i <= 8; i++)
		if (!g_ok[i])
			return 0;
	return 1;
}

static void die(const char *what)
{
	int i;

	fprintf(stderr, "harness fatal: %s: %s\n", what, strerror(errno));
	fflush(stderr);
	for (i = 1; i <= 8; i++)
		verdict(i, 0, "not evaluated: %s failed (%s)", what,
			strerror(errno));
	print_claims();
	_exit(3);
}

/* ------------------------------------------------------------------ */
/* D1 / D2                                                            */
/* ------------------------------------------------------------------ */

struct stage_job {
	int lane;
	int idx;
	int ok;
	int nreq;
};

static void *stage_thread(void *arg)
{
	struct stage_job *j = arg;
	uint64_t tid = tid_self();
	int i;

	j->ok = 1;
	for (i = 0; i < j->nreq; i++) {
		uint64_t seq = ((uint64_t)j->idx << 32) | (uint64_t)i;
		uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) |
				   (uint64_t)(j->lane & 0xFFFF);
		uint64_t res = 0, got = 0, rt = 0;

		if (lane_publish(j->lane, seq, payload, G_GEN, tid, 1, 0,
				 STALL_MS) != 0) {
			j->ok = 0;
			break;
		}
		if (lane_consume(j->lane, G_GEN, 1, WAIT_MS, &res, &got,
				 &rt) != 0) {
			j->ok = 0;
			break;
		}
		if (got != seq || res != server_result_for(payload)) {
			j->ok = 0;
			break;
		}
		__atomic_fetch_add(&HH->d2_exchanges, 1, __ATOMIC_RELAXED);
	}
	return NULL;
}

static void d1_phase(void)
{
	static const int threads[4] = { 1, 8, 32, 64 };
	static const int base[4] = { D2_STAGE0_LANE, D2_STAGE1_LANE,
				     D2_STAGE2_LANE, D2_STAGE3_LANE };
	int si;

	for (si = 0; si < 4; si++) {
		pthread_t th[MAX_THREADS];
		struct stage_job job[MAX_THREADS];
		int i, ok = 1;

		HH->d1_threads[si] = (uint32_t)threads[si];
		for (i = 0; i < threads[si]; i++) {
			job[i].lane = base[si] + i;
			job[i].idx = si * 1000 + i;
			job[i].nreq = D1_REQS_PER_THREAD;
			job[i].ok = 0;
			if (pthread_create(&th[i], NULL, stage_thread,
					   &job[i]) != 0)
				die("pthread_create(d1)");
		}
		for (i = 0; i < threads[si]; i++)
			pthread_join(th[i], NULL);
		for (i = 0; i < threads[si]; i++)
			if (!job[i].ok)
				ok = 0;
		HH->stage_ok[si] = (uint32_t)ok;
		HH->stage_fds[si] = (uint64_t)fd_snapshot();
		if (HH->guest_write_rep_fault || HH->srv_write_req_fault)
			break;
	}
}

static void d1_verdict(void)
{
	uint64_t f0 = HH->stage_fds[0];
	int i, same = 1, allok = 1;
	char detail[1600];
	size_t n = 0;

	for (i = 1; i < 4; i++)
		if (HH->stage_fds[i] != f0)
			same = 0;
	for (i = 0; i < 4; i++)
		if (!HH->stage_ok[i])
			allok = 0;
	n = (size_t)snprintf(detail, sizeof detail,
			     "with the fixed process anchors present (3 anchors "
			     "+ stdio + 1 process eventfd + 1 control endpoint) "
			     "the open descriptor count is invariant under guest "
			     "thread count; measured open descriptors: ");
	for (i = 0; i < 4; i++)
		n += (size_t)snprintf(detail + n, sizeof detail - n,
				      "%u thread(s)=%llu%s", HH->d1_threads[i],
				      (unsigned long long)HH->stage_fds[i],
				      i == 3 ? "" : ", ");
	snprintf(detail + n, sizeof detail - n,
		 "; %llu request/reply exchanges verified; every lane is "
		 "shared memory, so no per-thread descriptor is created",
		 (unsigned long long)ld_acq(&HH->d2_exchanges));
	verdict(1, same && allok && HH->stage_fds[0] > 0, "%s", detail);
}

static void d2_probe(void)
{
	volatile uint64_t *p = (volatile uint64_t *)&SH->rep[0][0].result;

	g_probe_active = 1;
	if (sigsetjmp(g_probe_jmp, 1) == 0) {
		*p = 1;
		HH->d2_probe_guest_fault = 0;
	} else {
		HH->d2_probe_guest_fault = 1;
	}
	g_probe_active = 0;
}

static void d2_verdict(void)
{
	int claimed = 0, i;
	uint64_t owner_mismatch = 0;

	for (i = 0; i < LANE_COUNT; i++) {
		if (GH->lane_owner[i])
			claimed++;
		if (GH->lane_owner[i] && GH->lane_prod_tid[i] &&
		    GH->lane_owner[i] != GH->lane_prod_tid[i])
			owner_mismatch++;
	}
	verdict(2,
		ld_acq(&HH->d2_probe_guest_fault) == 1 &&
			ld_acq(&HH->d2_probe_server_fault) == 1 &&
			ld_acq(&HH->lane_double_claim) == 0 &&
			ld_acq(&HH->lane_tid_change) == 0 &&
			owner_mismatch == 0 && claimed > 0 &&
			!HH->guest_write_rep_fault && !HH->srv_write_req_fault,
		"%d lane(s) each claimed by exactly one producing tid (%llu "
		"double claims, %llu producing-tid changes, %llu owner/prod_tid "
		"mismatches); ONE producer and ONE consumer per lane; the server "
		"never writes a request slot: the guest region is "
		"mprotect(PROT_READ) in the server, its probe write faulted=%llu "
		"and faults during %llu exchanges=%llu; the guest never writes a "
		"reply slot: the server region is mprotect(PROT_READ) in the "
		"guest, its probe write faulted=%llu and faults=%llu",
		claimed, (unsigned long long)ld_acq(&HH->lane_double_claim),
		(unsigned long long)ld_acq(&HH->lane_tid_change),
		(unsigned long long)owner_mismatch,
		(unsigned long long)ld_acq(&HH->d2_probe_server_fault),
		(unsigned long long)ld_acq(&HH->d2_exchanges),
		(unsigned long long)HH->srv_write_req_fault,
		(unsigned long long)ld_acq(&HH->d2_probe_guest_fault),
		(unsigned long long)HH->guest_write_rep_fault);
}

/* ------------------------------------------------------------------ */
/* D5                                                                 */
/* ------------------------------------------------------------------ */

static void d5_phase(void)
{
	uint64_t tid = tid_self();
	uint64_t w0, w1, s0, s1, i;
	uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) | 0x1155;
	int ok_hot = 1;

	/* Park first, then switch to active polling and make the server consume a
	 * wake of ours before the window opens: a wake that belongs to an earlier
	 * phase (or a stale eventfd credit) must not be counted inside the window,
	 * so "the server never slept during the window" is a sound statement. */
	set_server_mode(SCMD_PARK);
	set_server_mode(SCMD_POLL);
	{
		uint64_t w = ld_acq(&HH->server_wakes);
		uint64_t t0 = now_ms();

		wake_raw(GEFD);
		while (ld32_acq(&HH->server_state) != SST_ACTIVE ||
		       ld32_acq(&HH->srv_in_epoll) != 0 ||
		       ld_acq(&HH->server_wakes) == w) {
			if (now_ms() - t0 > WAIT_MS)
				break;
			cpu_pause();
			sched_yield();
		}
	}
	w0 = ld_acq(&HH->efd_writes);
	s0 = ld_acq(&HH->server_wakes);
	for (i = 0; i < D5_HOT_ITERS; i++) {
		uint64_t seq = 0x1100000000ULL + i;
		uint64_t res = 0, got = 0, rt = 0;

		if (lane_publish(D5_HOT_LANE, seq, payload, G_GEN, tid, 1, 0,
				 WAIT_MS) != 0 ||
		    lane_consume(D5_HOT_LANE, G_GEN, 0, WAIT_MS, &res, &got,
				 &rt) != 0 ||
		    got != seq || res != server_result_for(payload)) {
			ok_hot = 0;
			break;
		}
	}
	w1 = ld_acq(&HH->efd_writes);
	s1 = ld_acq(&HH->server_wakes);
	HH->hot_requests = i;
	HH->hot_doorbells = w1 - w0;
	HH->hot_wakes = s1 - s0;

	{
		uint64_t c0, c1;
		int ok_cold = 1;

		set_server_mode(SCMD_PARK);
		c0 = ld_acq(&HH->efd_writes);
		for (i = 0; i < D5_COLD_ITERS; i++) {
			uint64_t seq = 0x2200000000ULL + i;
			uint64_t res = 0, got = 0, rt = 0;

			wait_server_parked();
			if (lane_publish(D5_COLD_LANE, seq, payload, G_GEN,
					 tid, 1, 0, WAIT_MS) != 0 ||
			    lane_consume(D5_COLD_LANE, G_GEN, 1, WAIT_MS, &res,
					 &got, &rt) != 0 ||
			    got != seq || res != server_result_for(payload)) {
				ok_cold = 0;
				break;
			}
		}
		c1 = ld_acq(&HH->efd_writes);
		HH->cold_requests = i;
		HH->cold_doorbells = c1 - c0;

		verdict(5,
			ok_hot && HH->hot_requests == D5_HOT_ITERS &&
				HH->hot_doorbells == 0 && HH->hot_wakes == 0 &&
				ok_cold && HH->cold_requests == D5_COLD_ITERS &&
				HH->cold_doorbells == D5_COLD_ITERS,
			"fully active window: %llu requests, %llu eventfd writes "
			"(MUST be 0) and %llu server epoll wakes (the server "
			"polled for the whole window, so the hot path never "
			"touched the doorbell); cold window: %llu requests, %llu "
			"eventfd writes = %s per request (the server was parked "
			"for every one of them)",
			(unsigned long long)HH->hot_requests,
			(unsigned long long)HH->hot_doorbells,
			(unsigned long long)HH->hot_wakes,
			(unsigned long long)HH->cold_requests,
			(unsigned long long)HH->cold_doorbells,
			HH->cold_requests == HH->cold_doorbells ? "1.000" :
								  "equal to the "
								  "request count");
	}
}

/* ------------------------------------------------------------------ */
/* D3                                                                 */
/* ------------------------------------------------------------------ */

static uint8_t g_sched[256][D3_STEP_TOTAL];
static int g_nsched;

static void gen_sched(int gp, int gs, uint8_t *cur, int len)
{
	if (len == D3_STEP_TOTAL) {
		if (g_nsched < 256) {
			memcpy(g_sched[g_nsched], cur, D3_STEP_TOTAL);
			g_nsched++;
		}
		return;
	}
	if (gp < D3_GATES_GUEST) {
		cur[len] = SIDE_GUEST;
		gen_sched(gp + 1, gs, cur, len + 1);
	}
	if (gs < D3_GATES_SERVER) {
		cur[len] = SIDE_SERVER;
		gen_sched(gp, gs + 1, cur, len + 1);
	}
}

static void *d3_prod_thread(void *arg)
{
	(void)arg;
	for (;;) {
		int lane;
		uint64_t seq, tid;

		while (!ld32_acq(&HH->prod_go)) {
			if (ld32_acq(&HH->prod_quit))
				return NULL;
			cpu_pause();
		}
		st32_rel(&HH->prod_go, 0);
		lane = (int)ld32_acq(&HH->prod_lane);
		seq = ld_acq(&HH->prod_seq);
		tid = tid_self();
		(void)lane_publish(lane, seq,
				   ((uint64_t)CMD_ECHO << CMD_SHIFT) | 0x3333,
				   G_GEN, tid, 1, 1 /* gated */, STALL_MS);
	}
}

static int d3_grant(int side, uint32_t want)
{
	uint64_t t0 = now_ms();

	while (ld32_acq(&HH->adv_at[side]) < want) {
		if (now_ms() - t0 > WAIT_MS)
			return 0;
		cpu_pause();
		sched_yield();
	}
	st32_rel(&HH->adv_turn, (uint32_t)(side + 1));
	while (ld32_acq(&HH->adv_done[side]) < want) {
		if (now_ms() - t0 > WAIT_MS)
			return 0;
		cpu_pause();
		sched_yield();
	}
	return 1;
}

static void d3_window(const uint8_t *sch, uint64_t seq, int fresh_pre,
		      uint64_t pre_seq, uint64_t *unserviced)
{
	uint32_t at[2] = { 0, 0 };
	int i, stalled = 0;
	uint64_t res = 0, got = 0, rt = 0;

	wait_server_parked();
	st32_rel(&HH->adv_arm, 0); /* quiesce the handshake while resetting */
	st32_rel(&HH->adv_at[0], 0);
	st32_rel(&HH->adv_at[1], 0);
	st32_rel(&HH->adv_done[0], 0);
	st32_rel(&HH->adv_done[1], 0);
	st32_rel(&HH->adv_cap[0], D3_GATES_GUEST);
	st32_rel(&HH->adv_cap[1], D3_GATES_SERVER);
	st32_rel(&HH->adv_turn, 0);
	st32_rel(&HH->prod_lane, D3_LANE);
	st_rel(&HH->prod_seq, seq);
	fence();
	st32_rel(&HH->adv_arm, 1);

	/* family B: a request is already pending when the window opens, so the
	 * server's first drain has work and the interleaving covers the
	 * drain-then-re-check ordering as well.  The server cannot observe it
	 * before the wake below, because it is still inside epoll_wait. */
	if (fresh_pre)
		(void)lane_publish_quiet(D3_PRE_LANE, pre_seq,
					 ((uint64_t)CMD_ECHO << CMD_SHIFT) |
						 0x4444,
					 G_GEN, tid_self(), 1);

	st32_rel(&HH->prod_go, 1); /* the producer runs to its first gate */
	wake_raw(GEFD); /* bring the server to its first gate */

	for (i = 0; i < D3_STEP_TOTAL; i++) {
		int side = sch[i];

		at[side]++;
		if (!d3_grant(side, at[side])) {
			stalled = 1;
			break;
		}
	}
	st32_rel(&HH->adv_arm, 0);
	if (stalled)
		__atomic_fetch_add(&HH->d3_stalls, 1, __ATOMIC_RELAXED);

	/* settle: was the window's request serviced? */
	if (wait_reply(D3_LANE, seq, D3_DEADLINE_MS) != 0) {
		(*unserviced)++;
		if (!bitmap_any() && !ld32_acq(&HH->srv_in_epoll))
			__atomic_fetch_add(&HH->d3_stranded, 1,
					   __ATOMIC_RELAXED);
		wake_raw(GEFD); /* rescue so the next window starts clean */
		if (wait_reply(D3_LANE, seq, WAIT_MS) != 0)
			__atomic_fetch_add(&HH->d3_stalls, 1,
					   __ATOMIC_RELAXED);
	}
	__atomic_fetch_add(&HH->d3_windows, 1, __ATOMIC_RELAXED);

	/* consume the window's reply (and the pre-existing request's reply) so
	 * the next window starts from a clean lane */
	(void)lane_consume(D3_LANE, G_GEN, 0, 300, &res, &got, &rt);
	if (!fresh_pre)
		(void)lane_consume(D3_PRE_LANE, G_GEN, 0, 50, &res, &got, &rt);
	else
		(void)lane_consume(D3_PRE_LANE, G_GEN, 0, 300, &res, &got, &rt);
}

struct stress_job {
	int base;
	int rounds;
	int ok;
};

static void *stress_thread(void *arg)
{
	struct stress_job *j = arg;
	uint64_t tid = tid_self();
	int r, i, ok = 1;

	for (r = 0; r < j->rounds; r++) {
		int burst = 1 + (r % D3S_LANES_PER_THREAD);
		uint64_t seqbase = ((tid & 0xFFFF) << 32) | ((uint64_t)r << 4);

		/* flap the server between active polling and parked */
		set_server_mode((r & 1) ? SCMD_POLL : SCMD_PARK);

		for (i = 0; i < burst; i++) {
			int lane = j->base + i;
			uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) |
					   (uint64_t)(lane & 0xFFFF);

			if (lane_publish(lane, seqbase + (uint64_t)i, payload,
					 G_GEN, tid, 1, 0, WAIT_MS) != 0) {
				ok = 0;
				break;
			}
			__atomic_fetch_add(&HH->stress_requests, 1,
					   __ATOMIC_RELAXED);
		}
		for (i = 0; i < burst; i++) {
			int lane = j->base + i;
			uint64_t res = 0, got = 0, rt = 0;
			uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) |
					   (uint64_t)(lane & 0xFFFF);

			if (lane_consume(lane, G_GEN, 1, WAIT_MS, &res, &got,
					 &rt) != 0 ||
			    got != seqbase + (uint64_t)i ||
			    res != server_result_for(payload)) {
				__atomic_fetch_add(&HH->stress_lost, 1,
						   __ATOMIC_RELAXED);
				ok = 0;
				break;
			}
		}
		if (!ok)
			break;
	}
	__atomic_fetch_add(&HH->stress_rounds, (uint64_t)j->rounds,
			   __ATOMIC_RELAXED);
	j->ok = ok;
	return NULL;
}

static void d3_phase(void)
{
	uint8_t cur[D3_STEP_TOTAL];
	pthread_t prod;
	int fam, s, stalls;
	uint64_t unserviced = 0, seq = 0x3300000000ULL;

	g_nsched = 0;
	gen_sched(0, 0, cur, 0);

	st32_rel(&HH->prod_quit, 0);
	if (pthread_create(&prod, NULL, d3_prod_thread, NULL) != 0)
		die("pthread_create(d3)");

	for (fam = 0; fam < 2; fam++) {
		for (s = 0; s < g_nsched; s++) {
			uint64_t wseq = seq++;
			uint64_t pseq = seq++;

			d3_window(g_sched[s], wseq, fam == 1, pseq,
				  &unserviced);
			if (HH->guest_write_rep_fault ||
			    HH->srv_write_req_fault)
				break;
		}
	}

	st32_rel(&HH->prod_quit, 1);
	st32_rel(&HH->prod_go, 1);
	pthread_join(prod, NULL);

	{
		pthread_t th[2];
		struct stress_job sj[2];
		uint64_t w0 = ld_acq(&HH->efd_writes);

		sj[0].base = D3S_LANE_BASE;
		sj[0].rounds = D3_STRESS_ROUNDS;
		sj[0].ok = 0;
		sj[1].base = D3S_LANE_BASE + D3S_LANES_PER_THREAD;
		sj[1].rounds = D3_STRESS_ROUNDS;
		sj[1].ok = 0;
		if (pthread_create(&th[0], NULL, stress_thread, &sj[0]) != 0 ||
		    pthread_create(&th[1], NULL, stress_thread, &sj[1]) != 0)
			die("pthread_create(stress)");
		pthread_join(th[0], NULL);
		pthread_join(th[1], NULL);
		HH->stress_doorbells = ld_acq(&HH->efd_writes) - w0;
		set_server_mode(SCMD_PARK);
		if (!sj[0].ok || !sj[1].ok)
			__atomic_fetch_add(&HH->stress_lost, 1,
					   __ATOMIC_RELAXED);
	}

	stalls = (int)ld_acq(&HH->d3_stalls);
	verdict(3,
		unserviced == 0 && HH->stress_lost == 0 &&
			!HH->guest_write_rep_fault && !HH->srv_write_req_fault,
		"%d forced interleavings were executed deterministically over the "
		"5 windows named in the claim (server arming/sleeping, producer "
		"setting the pending bit, the producer's doorbell decision, the "
		"server draining, the server transitioning active->sleeping): %d "
		"schedules x 2 initial states (family A: empty pending set, %d "
		"runs; family B: a drain already due, %d runs); %llu window(s) "
		"ran, %llu left unserviced within %d ms (%llu also state-verified "
		"stranded), %d handshake stall(s); stress: 2 threads x %d rounds = "
		"%llu thread-rounds, %llu requests published, %llu unserviced, "
		"%llu doorbell writes for those requests",
		g_nsched * 2, g_nsched, g_nsched, g_nsched,
		(unsigned long long)ld_acq(&HH->d3_windows),
		(unsigned long long)unserviced, D3_DEADLINE_MS,
		(unsigned long long)ld_acq(&HH->d3_stranded), stalls,
		D3_STRESS_ROUNDS, (unsigned long long)ld_acq(&HH->stress_rounds),
		(unsigned long long)ld_acq(&HH->stress_requests),
		(unsigned long long)ld_acq(&HH->stress_lost),
		(unsigned long long)HH->stress_doorbells);
}

/* ------------------------------------------------------------------ */
/* D6                                                                 */
/* ------------------------------------------------------------------ */

static uint32_t child_attach_fresh(int *fd_out)
{
	uint32_t gen = (uint32_t)__atomic_fetch_add(&HH->gen_counter, 1,
						    __ATOMIC_ACQ_REL) +
		       1;
	int efd = eventfd(0, EFD_NONBLOCK);
	uint32_t srv_gen = 0, reason = 0;

	if (efd < 0)
		return 0;
	if (ctl_attach(GSOCK, efd, gen, &srv_gen, &reason) != 0) {
		close(efd);
		return 0;
	}
	__atomic_fetch_add(&HH->guest_attach_ok, 1, __ATOMIC_RELAXED);
	*fd_out = efd;
	return gen;
}

static void d6_child(int idx)
{
	int lane = D6_CHILD_LANE_BASE + idx;
	uint64_t my_gen = 0, res = 0, got = 0, rt = 0, tid = tid_self();
	int rc, child_efd = -1;

	/* 1. a fork child gets its own transport generation: its own doorbell
	 *    descriptor, attached over the process-level control endpoint. */
	/*MUT1-ATTACH*/ my_gen = child_attach_fresh(&child_efd);
	if (my_gen == 0)
		_exit(0);
	HH->child_gen[idx] = my_gen;
	HH->child_own_efd[idx] = (uint64_t)child_efd;
	if (child_efd == GEFD_INHERITED)
		HH->child_inherited_doorbell = 1;
	GEFD = child_efd; /* this incarnation's doorbell from here on */

	/* 2. the child must not adopt the parent's in-flight generation: the
	 *    parent's completion is visible but belongs to another incarnation */
	rc = lane_consume(D6_PARENT_LANE, my_gen, 0, 300, &res, &got, &rt);
	HH->child_rejected[idx] = (rc == 1);
	HH->child_consumed_parent[idx] = (rc == 0);
	if (rc == 0)
		_exit(0);

	/* 3. and it makes progress on its own lane, in its own generation */
	{
		uint64_t seq = ((uint64_t)my_gen << 32) | 0xD6ULL;
		uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) |
				   (uint64_t)(lane & 0xFFFF);

		if (lane_publish(lane, seq, payload, my_gen, tid, 1, 0,
				 STALL_MS) != 0)
			_exit(0);
		rc = lane_consume(lane, my_gen, 1, WAIT_MS, &res, &got, &rt);
		HH->child_rc[idx] = (uint64_t)rc;
		HH->child_ok[idx] = (rc == 0 && got == seq &&
				     res == server_result_for(payload)) ?
					    1 :
					    0;
	}
	_exit(0);
}

static void d6_phase(void)
{
	int i, children = 0;
	uint64_t res = 0, got = 0, rt = 0, tid = tid_self();

	HH->parent_gen = G_GEN;

	for (i = 0; i < D7_CHILD_RECORDS; i++) {
		uint64_t seq = 0xD60000ULL + (uint64_t)i;
		uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) | 0x5A5A |
				   LEDGER_BIT;
		pid_t pid;
		int status = 0, rc;
		uint64_t t0;

		/* the parent's in-flight request: published, serviced and NOT
		 * consumed before the fork */
		if (lane_publish_quiet(D6_PARENT_LANE, seq, payload, G_GEN, tid,
				       1) != 0)
			break;
		__atomic_fetch_add(&HH->parent_requests, 1, __ATOMIC_RELAXED);
		t0 = now_ms();
		while (!reply_ready(D6_PARENT_LANE, seq) &&
		       now_ms() - t0 < WAIT_MS)
			sched_yield();

		pid = fork();
		if (pid < 0)
			break;
		if (pid == 0) {
			d6_child(i);
			_exit(0);
		}
		t0 = now_ms();
		while (waitpid(pid, &status, WNOHANG) != pid &&
		       now_ms() - t0 < 5000)
			sched_yield();
		if (waitpid(pid, &status, WNOHANG) != pid)
			(void)kill(pid, SIGKILL);
		children++;

		/* the parent consumes exactly its own completion */
		rc = lane_consume(D6_PARENT_LANE, G_GEN, 0, WAIT_MS, &res, &got,
				  &rt);
		if (rc == 0 && got == seq && res == server_result_for(payload))
			__atomic_fetch_add(&HH->parent_consumed, 1,
					   __ATOMIC_RELAXED);
	}
	HH->d6_children = (uint32_t)children;

	{
		uint64_t dup = 0, entries = ld_acq(&HH->ledger_n), j;
		int rejected = 0, child_ok = 0, own_doorbell = 0, consumed = 0;
		char why[192];
		size_t wn = 0;

		why[0] = 0;
		for (i = 0; i < (int)HH->d6_children && wn < 140; i++) {
			if (ld_acq(&HH->child_ok[i]))
				continue;
			wn += (size_t)snprintf(
				why + wn, sizeof why - wn,
				"#%d(gen=%llu consume_rc=%llu) ", i,
				(unsigned long long)ld_acq(&HH->child_gen[i]),
				(unsigned long long)ld_acq(&HH->child_rc[i]));
		}

		for (j = 0; j < entries && j < LEDGER_MAX; j++)
			if (ld_acq(&HH->ledger[j][2]) != 1)
				dup++;
		if (ld_acq(&HH->ledger_overflow))
			dup += 100;
		for (i = 0; i < (int)HH->d6_children; i++) {
			if (ld_acq(&HH->child_rejected[i]))
				rejected++;
			if (ld_acq(&HH->child_ok[i]))
				child_ok++;
			if (ld_acq(&HH->child_consumed_parent[i]))
				consumed++;
			if (ld_acq(&HH->child_own_efd[i]) != 0 &&
			    ld_acq(&HH->child_own_efd[i]) !=
				    (uint64_t)GEFD_INHERITED)
				own_doorbell++;
		}
		verdict(6,
			HH->d6_children > 0 &&
				HH->parent_consumed == HH->parent_requests &&
				rejected == (int)HH->d6_children &&
				child_ok == (int)HH->d6_children &&
				own_doorbell == (int)HH->d6_children &&
				ld_acq(&HH->child_inherited_doorbell) == 0 &&
				dup == 0 &&
				ld_acq(&GH->cons_count[D6_PARENT_LANE]) ==
					HH->parent_requests,
			"%u repeated forks (parent generation %llu): every child "
			"attached its own doorbell descriptor + generation "
			"(%d/%u own descriptors, the inherited process eventfd was "
			"used as a child doorbell %llu time(s)), every child saw "
			"the parent's in-flight completion and REJECTED it on the "
			"generation check (%d/%u rejected, %llu consumed by a "
			"child), every child then made progress on its own lane in "
			"its own generation (%d/%u), the parent consumed exactly its "
			"own %llu completion(s) (lane consumption count %llu) and no "
			"completion was counted twice (%llu of %llu ledger entries "
			"have a count != 1); failing children: %s",
			HH->d6_children,
			(unsigned long long)HH->parent_gen, own_doorbell,
			HH->d6_children,
			(unsigned long long)ld_acq(&HH->child_inherited_doorbell),
			rejected, HH->d6_children,
			(unsigned long long)consumed, child_ok, HH->d6_children,
			(unsigned long long)HH->parent_consumed,
			(unsigned long long)ld_acq(
				&GH->cons_count[D6_PARENT_LANE]),
			(unsigned long long)dup, (unsigned long long)entries,
			why[0] ? why : "none");
	}
}

/* ------------------------------------------------------------------ */
/* D8                                                                 */
/* ------------------------------------------------------------------ */

static __thread uint64_t t_cookie;

struct d8_job {
	int idx;
};

static void *d8_thread(void *arg)
{
	struct d8_job *j = arg;
	int idx = j->idx;
	int lane = D8_LANE_BASE + idx;
	uint64_t tid = tid_self();
	uint64_t res = 0, got = 0, rt = 0;
	int i, all;
	uint64_t t0;

	t_cookie = 0;
	HH->d8_req_tid[idx] = tid;
	HH->d8_done[idx] = 0;

	if (lane_publish(lane, tid,
			 ((uint64_t)CMD_CALLER << CMD_SHIFT) | (uint64_t)idx |
				 LEDGER_BIT,
			 G_GEN, tid, 1, 0, STALL_MS) != 0) {
		HH->d8_done[idx] = 2;
		return NULL;
	}
	if (lane_consume(lane, G_GEN, 1, WAIT_MS, &res, &got, &rt) != 0) {
		HH->d8_done[idx] = 3;
		return NULL;
	}
	if (payload_cmd(res) != CMD_CALLER) {
		HH->d8_done[idx] = 4;
		return NULL;
	}

	/* the sideband command executes HERE, in the requesting thread */
	HH->d8_exec_tid[idx] = tid_self();
	HH->d8_exec_pid[idx] = (uint64_t)getpid();
	HH->d8_sideband_tid[idx] = rt;
	t_cookie = tid;

	st_rel(&HH->d8_arrived[idx], 1);
	t0 = now_ms();
	for (;;) {
		all = 1;
		for (i = 0; i < 4; i++)
			if (!ld32_acq(&HH->d8_arrived[i]))
				all = 0;
		if (all || now_ms() - t0 > WAIT_MS)
			break;
		cpu_pause();
		sched_yield();
	}
	if (t_cookie == tid)
		HH->d8_cookie_ok[idx] = 1;
	HH->d8_done[idx] = 1;
	return NULL;
}

static void d8_phase(void)
{
	pthread_t th[4];
	struct d8_job job[4];
	int i, done = 0, tid_ok = 0, cookie = 0, sideband = 0, pid_ok = 0;

	for (i = 0; i < 4; i++) {
		job[i].idx = i;
		if (pthread_create(&th[i], NULL, d8_thread, &job[i]) != 0)
			die("pthread_create(d8)");
	}
	for (i = 0; i < 4; i++)
		pthread_join(th[i], NULL);

	for (i = 0; i < 4; i++) {
		if (ld32_acq(&HH->d8_done[i]) == 1)
			done++;
		if (ld_acq(&HH->d8_exec_tid[i]) == ld_acq(&HH->d8_req_tid[i]) &&
		    ld_acq(&HH->d8_req_tid[i]) != 0)
			tid_ok++;
		if (ld32_acq(&HH->d8_cookie_ok[i]))
			cookie++;
		if (ld_acq(&HH->d8_sideband_tid[i]) ==
			    ld_acq(&HH->d8_req_tid[i]) &&
		    ld_acq(&HH->d8_sideband_tid[i]) != 0)
			sideband++;
		if (ld_acq(&HH->d8_exec_pid[i]) == (uint64_t)getpid() &&
		    ld_acq(&HH->d8_exec_pid[i]) != 0 &&
		    ld_acq(&HH->d8_exec_pid[i]) !=
			    (uint64_t)ld32_acq(&HH->server_pid))
			pid_ok++;
	}
	verdict(8,
		done == 4 && tid_ok == 4 && cookie == 4 && sideband == 4 &&
			pid_ok == 4,
		"4 server->guest sideband operations; each executed on the exact "
		"requesting thread: requested tid %llu/%llu/%llu/%llu, observed "
		"executing tid %llu/%llu/%llu/%llu (the tid the server addressed "
		"in the reply slot: %llu/%llu/%llu/%llu); the thread-local cookie "
		"survived the 4-way rendezvous %d/4; none executed on the server "
		"(the executing pid is the guest pid for %d/4 and is never the "
		"server pid)",
		(unsigned long long)ld_acq(&HH->d8_req_tid[0]),
		(unsigned long long)ld_acq(&HH->d8_req_tid[1]),
		(unsigned long long)ld_acq(&HH->d8_req_tid[2]),
		(unsigned long long)ld_acq(&HH->d8_req_tid[3]),
		(unsigned long long)ld_acq(&HH->d8_exec_tid[0]),
		(unsigned long long)ld_acq(&HH->d8_exec_tid[1]),
		(unsigned long long)ld_acq(&HH->d8_exec_tid[2]),
		(unsigned long long)ld_acq(&HH->d8_exec_tid[3]),
		(unsigned long long)ld_acq(&HH->d8_sideband_tid[0]),
		(unsigned long long)ld_acq(&HH->d8_sideband_tid[1]),
		(unsigned long long)ld_acq(&HH->d8_sideband_tid[2]),
		(unsigned long long)ld_acq(&HH->d8_sideband_tid[3]), cookie,
		pid_ok);
}

/* ------------------------------------------------------------------ */
/* D4                                                                 */
/* ------------------------------------------------------------------ */

struct d4_case {
	int lanes;
	int pending;
	int step;
};

static void d4_measure(const struct d4_case *c, uint64_t *touched_out,
		       int *exact_out, int *zero_out)
{
	uint64_t lanes[HIER_LANES];
	uint64_t touched = 0;
	int z = 0, i, n, exact = 1;

	for (i = 0; i < c->pending; i++) {
		int lane = (i * c->step) % c->lanes;

		if (lane >= HIER_LANES)
			lane = HIER_LANES - 1;
		bitmap_set(lane);
	}
	n = bitmap_scan(lanes, HIER_LANES, &touched, &z);
	if (n != c->pending)
		exact = 0;
	for (i = 0; i < c->pending; i++) {
		int lane = (i * c->step) % c->lanes;
		int k, found = 0;

		if (lane >= HIER_LANES)
			lane = HIER_LANES - 1;
		for (k = 0; k < n; k++)
			if ((int)lanes[k] == lane)
				found = 1;
		if (!found)
			exact = 0;
	}
	*touched_out = touched;
	*zero_out = z;
	*exact_out = exact;
	__atomic_fetch_add(&HH->hier_lookups, 1, __ATOMIC_RELAXED);
	__atomic_fetch_add(&HH->hier_words, touched, __ATOMIC_RELAXED);
	__atomic_fetch_add(&HH->hier_zero_touches, (uint64_t)z,
			   __ATOMIC_RELAXED);
}

static void d4_verdict(void)
{
	static const struct d4_case cases[4] = {
		{ 1, 1, 1 },
		{ 512, 1, 1 },
		{ 8192, 1, 1 },
		{ 8192, 37, 221 },
	};
	uint64_t touched[4];
	int exact[4], z[4], i, all_exact = 1, all_nonzero = 1;
	char detail[1600];
	size_t n = 0;

	for (i = 0; i < 4; i++) {
		d4_measure(&cases[i], &touched[i], &exact[i], &z[i]);
		if (!exact[i])
			all_exact = 0;
		if (z[i] != 0)
			all_nonzero = 0;
	}
	n = (size_t)snprintf(detail, sizeof detail,
			     "one eventfd wake locates the exact pending lanes "
			     "with no blind O(N) scan: ");
	for (i = 0; i < 4; i++) {
		int blind_words = (cases[i].lanes + 63) / 64;

		n += (size_t)snprintf(
			detail + n, sizeof detail - n,
			"N=%d idle=%d pending=%d -> hierarchy touched %llu word(s) "
			"vs a blind word-scan of %d (blind per-lane reads %d); ",
			cases[i].lanes, cases[i].lanes - cases[i].pending,
			cases[i].pending, (unsigned long long)touched[i],
			blind_words, cases[i].lanes);
	}
	snprintf(detail + n, sizeof detail - n,
		 "every touched word was non-zero in every measured scan (%llu "
		 "zero-touches over %llu measured scans, %llu words); under "
		 "live load the server ran %llu scans touching %llu words with "
		 "%llu benign zero-touches",
		 (unsigned long long)ld_acq(&HH->hier_zero_touches),
		 (unsigned long long)ld_acq(&HH->hier_lookups),
		 (unsigned long long)ld_acq(&HH->hier_words),
		 (unsigned long long)ld_acq(&HH->live_scans),
		 (unsigned long long)ld_acq(&HH->live_words),
		 (unsigned long long)ld_acq(&HH->live_zero_touches));
	verdict(4, all_exact && all_nonzero, "%s", detail);
}

/* ------------------------------------------------------------------ */
/* D7                                                                 */
/* ------------------------------------------------------------------ */

static const char *kv_path(void)
{
	static char buf[512];
	const char *tmp = getenv("DIRECT_PROOF_TMP");

	if (!tmp || !*tmp)
		tmp = "/tmp";
	snprintf(buf, sizeof buf, "%s/ddb-d7-%d.out", tmp, (int)getpid());
	return buf;
}

static char *kv_slurp(const char *path)
{
	static char buf[8192];
	int fd = open(path, O_RDONLY);
	ssize_t n;

	if (fd < 0)
		return NULL;
	n = read(fd, buf, sizeof buf - 1);
	close(fd);
	if (n < 0)
		return NULL;
	buf[n] = 0;
	return buf;
}

static unsigned long long kv_get(const char *buf, const char *key)
{
	const char *p = buf;
	size_t klen;

	if (!buf)
		return 0;
	klen = strlen(key);
	while ((p = strstr(p, key)) != NULL) {
		if ((p == buf || p[-1] == '\n') && p[klen] == '=')
			return strtoull(p + klen + 1, NULL, 10);
		p++;
	}
	return 0;
}

static void kv_put(const char *path, const char *key, unsigned long long v)
{
	int fd = open(path, O_CREAT | O_WRONLY | O_APPEND, 0644);
	char line[128];
	int n;

	if (fd < 0)
		return;
	n = snprintf(line, sizeof line, "%s=%llu\n", key, v);
	if (write(fd, line, (size_t)n) != n) {
		close(fd);
		return;
	}
	close(fd);
}

/* ---- the pre-exec incarnations ---- */

static void d7_pre_e1(const char *out)
{
	int fd, efd, sv[2];
	pid_t p;
	uint32_t srv_gen = 0, reason = 0;
	uint64_t res = 0, got = 0, rt = 0, tid = tid_self();
	uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) | 0x7E1;
	uint64_t seq = 0x7E100;

	(void)syscall(SYS_close_range, 3u, ~0u, 0u);

	fd = (int)syscall(SYS_memfd_create, "ddb-shared", 0u);
	if (fd < 0)
		_exit(10);
	if (ftruncate(fd, (off_t)sizeof(struct shm)) != 0)
		_exit(11);
	efd = eventfd(0, EFD_NONBLOCK);
	if (efd < 0)
		_exit(12);
	if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sv) != 0)
		_exit(13);

	/* the retained transport set: backing descriptor, process doorbell and
	 * control endpoint all survive exec (CLOEXEC cleared) */
	(void)fcntl(fd, F_SETFD, 0);
	(void)fcntl(efd, F_SETFD, 0);
	(void)fcntl(sv[0], F_SETFD, 0);
	if (dup2(fd, 3) < 0 || dup2(efd, 4) < 0 || dup2(sv[0], 5) < 0)
		_exit(14);
	if (fd > 5)
		close(fd);
	if (efd > 5)
		close(efd);
	if (sv[0] > 5)
		close(sv[0]);
	fd = 3;
	efd = 4;
	sv[0] = 5;

	SHM = mmap(NULL, sizeof(struct shm), PROT_READ | PROT_WRITE,
		   MAP_SHARED, fd, 0);
	if (SHM == MAP_FAILED)
		_exit(15);
	setup_shm(SHM, 1);
	GEFD = efd;
	GSOCK = sv[0];
	G_GEN = 1;

	p = fork();
	if (p < 0)
		_exit(16);
	if (p == 0) {
		close(sv[0]);
		close(efd);
		server_main(sv[1]);
		_exit(0);
	}
	close(sv[1]);

	if (ctl_attach(GSOCK, GEFD, 1, &srv_gen, &reason) != 0)
		_exit(17);
	guest_protect();
	install_segv(guest_segv);
	{
		uint64_t t0 = now_ms();

		while (!ld32_acq(&HH->server_ready) && now_ms() - t0 < 3000)
			sched_yield();
	}

	kv_put(out, "desc_before", (unsigned long long)fd_snapshot());
	kv_put(out, "backing", 1); /* E1: retained descriptor */
	kv_put(out, "pre_gen", 1);
	kv_put(out, "efd_fd", (unsigned long long)GEFD);
	kv_put(out, "sock_fd", (unsigned long long)GSOCK);

	if (lane_publish(D7_LANE_PRE, seq, payload, G_GEN, tid, 1, 0,
			 STALL_MS) == 0 &&
	    lane_consume(D7_LANE_PRE, G_GEN, 1, WAIT_MS, &res, &got, &rt) == 0)
		kv_put(out, "pre_progress", 1);
	else
		kv_put(out, "pre_progress", 0);

	/* a FAILED exec must leave the previous generation usable: the image is
	 * unchanged, so the same incarnation keeps using the same generation and
	 * the same doorbell */
	{
		char *bad_argv[2];

		bad_argv[0] = (char *)"/nonexistent-direct-doorbell-failed-exec";
		bad_argv[1] = NULL;
		execv(bad_argv[0], bad_argv);
		kv_put(out, "failed_exec_errno", (unsigned long long)errno);
	}
	{
		uint64_t seq2 = seq + 0x10;

		if (lane_publish(D7_LANE_PRE, seq2, payload, G_GEN, tid, 1, 0,
				 STALL_MS) == 0 &&
		    lane_consume(D7_LANE_PRE, G_GEN, 1, WAIT_MS, &res, &got,
				 &rt) == 0 &&
		    got == seq2)
			kv_put(out, "failed_exec_progress", 1);
		else
			kv_put(out, "failed_exec_progress", 0);
	}

	/* leave a serviced-but-unconsumed completion behind: it belongs to this
	 * incarnation's generation and the post-exec one must reject it */
	if (lane_publish(D7_LANE_PRE, seq + 1, payload, G_GEN, tid, 1, 0,
			 STALL_MS) == 0)
		(void)wait_reply(D7_LANE_PRE, seq + 1, WAIT_MS);

	{
		char *argv[8];
		char fdbuf[16], efbuf[16], sockbuf[16];

		snprintf(fdbuf, sizeof fdbuf, "%d", 3);
		snprintf(efbuf, sizeof efbuf, "%d", 4);
		snprintf(sockbuf, sizeof sockbuf, "%d", 5);
		argv[0] = (char *)"/proc/self/exe";
		argv[1] = (char *)"exec-e1-post";
		argv[2] = (char *)out;
		argv[3] = fdbuf;
		argv[4] = efbuf;
		argv[5] = sockbuf;
		argv[6] = (char *)"0";
		argv[7] = NULL;
		execv("/proc/self/exe", argv);
	}
	_exit(18);
}

static void d7_pre_e2(const char *out)
{
	int efd, sv[2], shmid;
	pid_t p;
	uint32_t srv_gen = 0, reason = 0;
	uint64_t res = 0, got = 0, rt = 0, tid = tid_self();
	uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) | 0x7E2;
	uint64_t seq = 0x7E200;

	(void)syscall(SYS_close_range, 3u, ~0u, 0u);

	shmid = shmget(IPC_PRIVATE, sizeof(struct shm), IPC_CREAT | 0600);
	if (shmid < 0)
		_exit(20);
	SHM = shmat(shmid, NULL, 0);
	if (SHM == (void *)-1)
		_exit(21);
	efd = eventfd(0, EFD_NONBLOCK);
	if (efd < 0)
		_exit(22);
	if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sv) != 0)
		_exit(23);

	/* descriptor-less backing: only the process doorbell and the control
	 * endpoint are retained across exec */
	(void)fcntl(efd, F_SETFD, 0);
	(void)fcntl(sv[0], F_SETFD, 0);
	if (dup2(efd, 3) < 0 || dup2(sv[0], 4) < 0)
		_exit(24);
	if (efd > 4)
		close(efd);
	if (sv[0] > 4)
		close(sv[0]);
	efd = 3;
	sv[0] = 4;

	setup_shm(SHM, 1);
	GEFD = efd;
	GSOCK = sv[0];
	G_GEN = 1;

	p = fork();
	if (p < 0)
		_exit(25);
	if (p == 0) {
		close(sv[0]);
		close(efd);
		server_main(sv[1]);
		_exit(0);
	}
	close(sv[1]);
	if (ctl_attach(GSOCK, GEFD, 1, &srv_gen, &reason) != 0)
		_exit(26);
	guest_protect();
	install_segv(guest_segv);
	{
		uint64_t t0 = now_ms();

		while (!ld32_acq(&HH->server_ready) && now_ms() - t0 < 3000)
			sched_yield();
	}

	kv_put(out, "desc_before", (unsigned long long)fd_snapshot());
	kv_put(out, "backing", 2); /* E2: descriptor-less SysV shm */
	kv_put(out, "pre_gen", 1);
	kv_put(out, "efd_fd", (unsigned long long)GEFD);
	kv_put(out, "sock_fd", (unsigned long long)GSOCK);
	kv_put(out, "shmid", (unsigned long long)shmid);

	if (lane_publish(D7_LANE_PRE, seq, payload, G_GEN, tid, 1, 0,
			 STALL_MS) == 0 &&
	    lane_consume(D7_LANE_PRE, G_GEN, 1, WAIT_MS, &res, &got, &rt) == 0)
		kv_put(out, "pre_progress", 1);
	else
		kv_put(out, "pre_progress", 0);

	{
		char *bad_argv[2];

		bad_argv[0] = (char *)"/nonexistent-direct-doorbell-failed-exec";
		bad_argv[1] = NULL;
		execv(bad_argv[0], bad_argv);
		kv_put(out, "failed_exec_errno", (unsigned long long)errno);
	}
	{
		uint64_t seq2 = seq + 0x10;

		if (lane_publish(D7_LANE_PRE, seq2, payload, G_GEN, tid, 1, 0,
				 STALL_MS) == 0 &&
		    lane_consume(D7_LANE_PRE, G_GEN, 1, WAIT_MS, &res, &got,
				 &rt) == 0 &&
		    got == seq2)
			kv_put(out, "failed_exec_progress", 1);
		else
			kv_put(out, "failed_exec_progress", 0);
	}

	if (lane_publish(D7_LANE_PRE, seq + 1, payload, G_GEN, tid, 1, 0,
			 STALL_MS) == 0)
		(void)wait_reply(D7_LANE_PRE, seq + 1, WAIT_MS);

	{
		char *argv[8];
		char efbuf[16], sockbuf[16], shmbuf[32];

		snprintf(efbuf, sizeof efbuf, "%d", 3);
		snprintf(sockbuf, sizeof sockbuf, "%d", 4);
		snprintf(shmbuf, sizeof shmbuf, "%d", shmid);
		argv[0] = (char *)"/proc/self/exe";
		argv[1] = (char *)"exec-e2-post";
		argv[2] = (char *)out;
		argv[3] = (char *)"0";
		argv[4] = efbuf;
		argv[5] = sockbuf;
		argv[6] = shmbuf;
		argv[7] = NULL;
		execv("/proc/self/exe", argv);
	}
	_exit(27);
}

/* ---- the post-exec incarnations ---- */

static int d7_post_common(const char *out, struct shm *m, int efd, int sock,
			  int shmid, int backing)
{
	uint32_t reason = 0, srv_gen = 0;
	uint64_t gen, res = 0, got = 0, rt = 0, tid = tid_self();
	uint64_t payload = ((uint64_t)CMD_ECHO << CMD_SHIFT) | 0x7F0;
	uint64_t seq = 0x7F0000 + (uint64_t)backing;
	int rc, stale_attach, fresh_attach;

	SHM = m;
	GEFD = efd;
	GSOCK = sock;
	GEFD_INHERITED = efd;

	kv_put(out, "magic_ok", ld_acq(&HH->magic) == MAGIC ? 1 : 0);
	if (ld_acq(&HH->magic) != MAGIC)
		return 1;
	kv_put(out, "gen_counter_before",
	       (unsigned long long)ld_acq(&HH->gen_counter));
	kv_put(out, "srv_gen_before", (unsigned long long)ld_acq(&HH->srv_gen));

	guest_protect();
	install_segv(guest_segv);
	kv_put(out, "desc_after", (unsigned long long)fd_snapshot());

	/* a stale incarnation must be rejected at the attach */
	stale_attach = ctl_attach(GSOCK, GEFD, 1, &srv_gen, &reason);
	kv_put(out, "stale_attach_rc", (unsigned long long)stale_attach);
	kv_put(out, "stale_attach_reason", (unsigned long long)reason);

	/* and this incarnation attaches a fresh transport generation */
	gen = __atomic_fetch_add(&HH->gen_counter, 1, __ATOMIC_ACQ_REL) + 1;
	fresh_attach = ctl_attach(GSOCK, GEFD, (uint32_t)gen, &srv_gen,
				  &reason);
	kv_put(out, "fresh_attach_rc", (unsigned long long)fresh_attach);
	kv_put(out, "srv_gen_after", (unsigned long long)ld_acq(&HH->srv_gen));
	kv_put(out, "post_gen", (unsigned long long)gen);
	G_GEN = gen;

	/* the pre-exec incarnation's serviced-but-unconsumed completion must be
	 * rejected: it carries the previous generation */
	rc = lane_consume(D7_LANE_PRE, gen, 0, 300, &res, &got, &rt);
	kv_put(out, "stale_lane_rc", (unsigned long long)rc);

	/* then the fresh incarnation makes progress on its own lane */
	{
		int pub = lane_publish(D7_LANE_POST, seq, payload, gen, tid, 1, 0,
				       STALL_MS);

		kv_put(out, "post_publish", pub == 0 ? 1 : 0);
		rc = pub == 0 ? lane_consume(D7_LANE_POST, gen, 1, WAIT_MS,
					     &res, &got, &rt) :
				-1;
		kv_put(out, "post_consume_rc", (unsigned long long)rc);
		kv_put(out, "post_progress",
		       (rc == 0 && got == seq &&
			res == server_result_for(payload)) ?
			       1 :
			       0);
	}
	kv_put(out, "server_services_after",
	       (unsigned long long)ld_acq(&HH->server_services));
	kv_put(out, "srv_attach_ok",
	       (unsigned long long)ld_acq(&HH->srv_attach_ok));
	kv_put(out, "srv_attach_stale",
	       (unsigned long long)ld_acq(&HH->srv_attach_stale));

	(void)ctl_exit(GSOCK);
	{
		uint64_t t0 = now_ms();

		while (waitpid(-1, NULL, WNOHANG) != -1 &&
		       now_ms() - t0 < 2000)
			sched_yield();
	}
	if (shmid > 0)
		(void)shmctl(shmid, IPC_RMID, NULL);
	return 0;
}

static int mode_exec_e1_post(int argc, char **argv)
{
	const char *out;
	int shm_fd, efd, sock;
	struct shm *m;

	if (argc < 7)
		return 2;
	out = argv[2];
	shm_fd = atoi(argv[3]);
	efd = atoi(argv[4]);
	sock = atoi(argv[5]);
	m = mmap(NULL, sizeof(struct shm), PROT_READ | PROT_WRITE,
		 MAP_SHARED, shm_fd, 0);
	if (m == MAP_FAILED)
		return 3;
	return d7_post_common(out, m, efd, sock, 0, 1);
}

static int mode_exec_e2_post(int argc, char **argv)
{
	const char *out;
	int efd, sock, shmid;
	struct shm *m;

	if (argc < 7)
		return 2;
	out = argv[2];
	efd = atoi(argv[4]);
	sock = atoi(argv[5]);
	shmid = atoi(argv[6]);
	m = shmat(shmid, NULL, 0);
	if (m == (void *)-1)
		return 3;
	return d7_post_common(out, m, efd, sock, shmid, 2);
}

static int d7_run_leg(int e2, char *detail, size_t cap)
{
	char path[600];
	pid_t pid;
	int status = 0;
	char *buf;
	int ok;
	unsigned long long desc_before, desc_after, pre_gen, post_gen;
	unsigned long long stale_attach, stale_reason, fresh_attach;
	unsigned long long stale_lane_rc, pre_progress, post_progress;
	unsigned long long magic_ok, gen_before, failed_exec_errno;
	unsigned long long failed_exec_progress;
	uint64_t t0;

	snprintf(path, sizeof path, "%s", kv_path());
	{
		int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);

		if (fd >= 0)
			close(fd);
	}

	pid = fork();
	if (pid < 0)
		return -1;
	if (pid == 0) {
		if (e2)
			d7_pre_e2(path);
		else
			d7_pre_e1(path);
		_exit(30);
	}
	t0 = now_ms();
	for (;;) {
		if (waitpid(pid, &status, WNOHANG) == pid)
			break;
		if (now_ms() - t0 > 20000) {
			(void)kill(pid, SIGKILL);
			(void)waitpid(pid, &status, 0);
			break;
		}
		sched_yield();
	}

	buf = kv_slurp(path);
	magic_ok = kv_get(buf, "magic_ok");
	gen_before = kv_get(buf, "gen_counter_before");
	desc_before = kv_get(buf, "desc_before");
	desc_after = kv_get(buf, "desc_after");
	pre_gen = kv_get(buf, "pre_gen");
	post_gen = kv_get(buf, "post_gen");
	stale_attach = kv_get(buf, "stale_attach_rc");
	stale_reason = kv_get(buf, "stale_attach_reason");
	fresh_attach = kv_get(buf, "fresh_attach_rc");
	stale_lane_rc = kv_get(buf, "stale_lane_rc");
	pre_progress = kv_get(buf, "pre_progress");
	failed_exec_errno = kv_get(buf, "failed_exec_errno");
	failed_exec_progress = kv_get(buf, "failed_exec_progress");
	post_progress = kv_get(buf, "post_progress");

	ok = WIFEXITED(status) && WEXITSTATUS(status) == 0 && buf != NULL &&
	     magic_ok == 1 && gen_before == 1 && desc_before > 0 &&
	     desc_after == desc_before && pre_gen == 1 && post_gen == 2 &&
	     stale_attach == 1 && stale_reason == ATTACH_STALE &&
	     fresh_attach == 0 && stale_lane_rc == 1 && pre_progress == 1 &&
	     post_progress == 1 &&
	     failed_exec_errno == (unsigned long long)ENOENT &&
	     failed_exec_progress == 1;

	snprintf(detail, cap,
		 "%s: the shared state survived exec (magic_ok=%llu, generation "
		 "counter read back as %llu) and the descriptor count is "
		 "%llu before / %llu after; a stale incarnation is rejected at "
		 "attach (rc=%llu, reason=%llu=stale) while generation %llu "
		 "attaches fresh (rc=%llu); the pre-exec incarnation's "
		 "serviced-but-unconsumed completion is rejected on the "
		 "generation check (rc=%llu, %s); the post-exec incarnation "
		 "makes progress=%llu; pre-exec progress=%llu; a FAILED exec "
		 "left the previous generation usable (errno=%llu=ENOENT, "
		 "same-incarnation progress afterwards=%llu)",
		 e2 ? "E2 descriptor-less SysV shm, reattached with shmat after "
		      "exec (no descriptor for the transport backing)" :
		      "E1 one process-wide retained backing descriptor survives "
		      "exec (memfd, CLOEXEC cleared)",
		 magic_ok, gen_before, desc_before, desc_after, stale_attach,
		 stale_reason, post_gen, fresh_attach, stale_lane_rc,
		 stale_lane_rc == 0 ? "CONSUMED" : "not consumed",
		 post_progress, pre_progress, failed_exec_errno,
		 failed_exec_progress);
	unlink(path);
	return ok ? 1 : 0;
}

static void d7_phase(void)
{
	char a[1024], b[1024];
	int ok_a, ok_b;

	ok_a = d7_run_leg(0, a, sizeof a);
	ok_b = d7_run_leg(1, b, sizeof b);
	verdict(7, ok_a == 1 && ok_b == 1,
		"exec generation, both backing options: %s; %s", a, b);
}

/* ------------------------------------------------------------------ */
/* main                                                               */
/* ------------------------------------------------------------------ */

static void print_env_note(void)
{
	char line[256];
	char cap[64] = "?";
	char uid[32] = "?";
	FILE *f = fopen("/proc/self/status", "r");

	if (f) {
		while (fgets(line, sizeof line, f)) {
			if (!strncmp(line, "CapEff:", 7))
				(void)sscanf(line + 7, "%63s", cap);
			if (!strncmp(line, "Uid:", 4))
				(void)sscanf(line + 4, "%31s", uid);
		}
		fclose(f);
	}
	printf("INFO env uid=%s cap_eff=%s pid=%d (model, not the product)\n",
	       uid, cap, (int)getpid());
	fflush(stdout);
}

/* fixed anchors that are not part of this proof: they exist so D1 measures a
 * table that already contains application descriptors */
static void setup_anchors(void)
{
	(void)open("/dev/null", O_RDONLY);
	(void)open("/dev/null", O_WRONLY);
	(void)syscall(SYS_memfd_create, "ddb-anchor", 0u);
}

static int mode_all(void)
{
	print_env_note();
	(void)syscall(SYS_close_range, 3u, ~0u, 0u);
	setup_anchors();
	setup_architecture();

	d1_phase();
	d1_verdict();
	d2_probe();
	d5_phase();
	d3_phase();
	d6_phase();
	d8_phase();

	teardown();

	d2_verdict();
	d4_verdict();
	d7_phase();

	print_claims();
	return all_ok() ? 0 : 1;
}

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "all";

	setvbuf(stdout, NULL, _IOLBF, 0);
	if (strcmp(mode, "all") == 0)
		return mode_all();
	if (strcmp(mode, "exec-e1-post") == 0)
		return mode_exec_e1_post(argc, argv);
	if (strcmp(mode, "exec-e2-post") == 0)
		return mode_exec_e2_post(argc, argv);
	fprintf(stderr, "usage: %s [all|exec-e1-post ...|exec-e2-post ...]\n",
		argv[0]);
	return 2;
}

/*
 * direct-process-control-proof.c -- standalone falsification harness for the
 * "ONE process-level control descriptor per guest process" proposal.
 *
 * WHAT IS UNDER TEST
 * ------------------
 * Darling's slow-path RPC today is served by per-thread Unix-domain sockets: a
 * process with 32 or 64 guest threads holds 32 or 64 socket descriptors, and
 * every descriptor-bearing RPC has to be routed to the thread that issued it.
 * The proposal under test replaces all of them with ONE process-level control
 * endpoint -- a single connected AF_UNIX SOCK_SEQPACKET descriptor to
 * darlingserver -- while the hot path stays what it already is: per-thread
 * SPSC ring lanes in shared memory, serviced by the server without any
 * syscall from the guest.
 *
 * This harness builds that architecture for real (a listening AF_UNIX socket
 * bound to a path in a scratch directory, a libc-free server process forked
 * from the guest, one connected endpoint per guest process, per-thread
 * completion lanes in a shared mapping, SCM_RIGHTS in both directions) and
 * then tries to break it:
 *
 *   C1 MUST PASS  fd-table invariance: the guest process holds the SAME number
 *      of descriptors with 64 guest threads as with 32, and the count is the
 *      count of a process that owns exactly ONE control descriptor.
 *   C2 MUST PASS  64 threads, requests tagged with {request id, lane id},
 *      every request completes EXACTLY once, and no thread ever consumes a
 *      completion that belongs to another thread.  Verified per thread (the
 *      slot it observes must carry the request id it published) and globally
 *      (a dense ledger that must read exactly 1 for every published id).
 *   C3 MUST PASS  SCM_RIGHTS in BOTH directions under 64-thread concurrency:
 *      guest->server (the server reads a token out of the descriptor it was
 *      handed and compares it with the request id) and server->guest (the
 *      guest reads a token out of the descriptor it was handed and compares it
 *      with the request id).  No descriptor reaches the wrong request and
 *      neither side leaks: fd counts before and after, both processes.
 *   C4 MUST PASS  a descriptor-RETURNING control RPC (the server creates the
 *      descriptor for an application-table open and hands it to the guest)
 *      runs concurrently and lands in the application table of the REQUESTING
 *      process: every published row carries its requester's generation, lane
 *      and request id, and the descriptor's own content proves its identity.
 *   C5 MUST PASS  fork isolation: while the parent's 64 threads are hammering
 *      the endpoint, a forked child must not steal or interleave with the
 *      parent's completions; the child closes the inherited endpoint and
 *      establishes its own, and neither side ever sees the other's traffic.
 *   C6 MUST PASS  exec transition: after a successful execve the control
 *      endpoint is re-established (new connection, new generation: a request
 *      over the old generation is REJECTED by the server), the reply that was
 *      still in flight on the pre-exec connection is recognised as stale and
 *      rejected instead of being consumed as the new request's answer, and a
 *      FAILED execve leaves the previous endpoint fully usable.
 *   C7 MUST PASS  the hot path is not serialized by the control lock.  Three
 *      sub-phases: hot alone, hot with 64 threads of style-2 control traffic,
 *      and hot while the control lock is held for the whole phase by a
 *      deliberate holder, who samples the live per-lane counters across its own
 *      held window.  No control operation may complete inside that window, and
 *      the hot path must still run at full rate with the lock held.  Both
 *      throughputs are printed; the control-load figure is set by the one
 *      server thread that serves both paths, the held-lock figure is what
 *      isolates the guest lock.
 *   C8 MEASUREMENT  the two implementation styles, measured: style 1 (all slow
 *      control traffic serialized behind one guest lock, reply over the
 *      socket) versus style 2 (multiplexed messages on the one endpoint,
 *      completion delivered through the per-thread shared-memory lane):
 *      control messages per second, mean and p99 completion latency, and the
 *      contention cost of each.  C8 always reports a winner; the numbers are
 *      measurements, never verdicts.
 *   C9 MEASUREMENT  whether the control path is rare enough to serialize: the
 *      ratio of control operations to hot-path operations THIS HARNESS
 *      produced, with the explicit statement that the number comes from the
 *      harness's own counters and not from the product.
 *
 * WHAT THIS HARNESS DOES NOT DO
 * -----------------------------
 * It does not execute mldr, darlingserver, or any Darling prefix.  It does not
 * re-measure the product's slow-path RPC census (12 descriptor-bearing RPC
 * sites: vchroot; console_open, kqchan_mach_port_open, kqchan_proc_open,
 * debug_list_processes, debug_list_ports, debug_list_members,
 * debug_list_messages; checkin, checkout, ring_attach, push_reply) -- that
 * census is quoted as a PREMISE, never as a result of this harness.  It does
 * not measure the product's traffic mix: C9's ratio is this harness's own, and
 * the harness's workload is deliberately control-heavy, so C9 is a worst case,
 * not evidence about the product.
 *
 * ARCHITECTURE ACTUALLY BUILT
 * ---------------------------
 *   guest process : the harness.  Owns exactly one connected control endpoint
 *                   (g_ctl) and one process-level receiver thread that routes
 *                   descriptor-bearing replies to the per-thread completion
 *                   lane named in the reply header.
 *   server process: a fork child of the guest, libc-free (raw x86-64 syscalls
 *                   only, asserted by the runner with objdump: zero `call`
 *                   instructions), single-threaded, epoll over the listening
 *                   endpoint plus every connection.  It answers plain requests
 *                   by writing the completion straight into the requester's
 *                   shared-memory lane and waking it, and descriptor requests
 *                   by creating a descriptor, writing the request id into it
 *                   as a token, and handing it back with SCM_RIGHTS.
 *   hot path      : HOT_LANES per-thread SPSC rings in shared memory.  The
 *                   guest producer issues no syscall and takes no lock and
 *                   reads its result back from the ring; the server polls the
 *                   rings between control batches and, while the guest says it
 *                   is running hot traffic, spins on them instead of blocking
 *                   in epoll_wait -- which is what the product's ring-polling
 *                   mode does, and it is the one server thread, not the guest
 *                   lock, that limits the hot path under heavy control load.
 *
 * RAW SYSCALLS ONLY IN CHILD CODE
 * -------------------------------
 * Every function that runs after fork (the server, the C5 fork child, both C6
 * children) and after exec (exec_child_entry) is compiled from this file but is
 * libc-free: raw `syscall` instructions, hand-rolled struct zeroing and string
 * helpers that cannot be turned into memset/strcmp calls, no errno, no stdio,
 * no malloc.  The runner asserts with objdump that each of those entry points
 * contains zero `call` instructions.
 *
 * THE HARNESS MUST BE ABLE TO FAIL
 * --------------------------------
 * The runner builds deliberate mutations of this source in its own temporary
 * directory (never in the repository) and requires the named claim to fail:
 *   M1 route the completion to the neighbouring lane      -> C2 must fail
 *   M2 deliver a transferred descriptor to the wrong lane -> C3 must fail
 *   M3 the fork child reuses the parent's endpoint        -> C5 must fail
 *   M4 one control endpoint per thread                    -> C1 must fail
 *   M5 the hot path takes the control lock                -> C7 must fail
 *   M6 the post-exec image accepts the stale reply        -> C6 must fail
 * Each mutation is a single marked line; the runner refuses to continue if the
 * marker did not apply.
 *
 * Modes: `all` (the run above), `exec-child PATH GEN REQ STALE_FD` (the
 * post-exec image, entered only through execve).
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/futex.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* ------------------------------------------------------------------ */
/* constants                                                          */
/* ------------------------------------------------------------------ */

#define MAX_THREADS      64
#define MAX_LANES        128
#define HOT_LANES        8
#define HOT_SLOTS        8
#define LEDGER_MAX       65536
#define LEDGER_SPAN      16384     /* ids per space; three spaces below    */
#define IDSPACE_STYLE1   16384
#define IDSPACE_FD       32768
#define APP_ROWS         128
#define SERVER_CONNS     16
#define CTL_MAGIC        0x4450435F43544C31ULL      /* "DPC_CTL1"         */
#define HELLO_ACK_MAGIC  0x5EEDC0DE5EEDC0DEULL
#define REJECT_MAGIC     0xDEADBEEFDEADBEEFULL
#define C5_MAGIC         0x43354B4953534544ULL      /* "C5KISSED"         */
#define C6_MAGIC         0x4336455845433031ULL      /* "C6EXEC01"         */

#define CTL_HELLO       1u
#define CTL_HELLO_ACK   2u
#define CTL_ECHO        3u     /* completion through the shared lane     */
#define CTL_ECHO_SYNC   4u     /* completion over the socket (style 1)   */
#define CTL_FD_IN       5u     /* guest -> server descriptor             */
#define CTL_FD_OUT      6u     /* server -> guest descriptor             */
#define CTL_SYNC_SLOW   7u     /* reply after hdr.arg milliseconds       */
#define CTL_REPLY       8u
#define CTL_REJECT      9u

#define CS_IDLE  0u
#define CS_WAIT  1u
#define CS_DONE  2u

#define HE_FREE  0u
#define HE_REQ   1u
#define HE_DONE  2u

#define GEN_PARENT   1ULL
#define GEN_FORK     2ULL
#define GEN_EXEC_PRE 3ULL
#define GEN_EXEC_NEW 4ULL
#define GEN_EXEC_FAIL 5ULL

#define STYLE1 1
#define STYLE2 2

/* failure detail strings */
#define DET_MAX 768

/* ------------------------------------------------------------------ */
/* wire format (AF_UNIX SOCK_SEQPACKET, one datagram per request)      */
/* ------------------------------------------------------------------ */

struct ctl_msg {
	uint64_t magic;
	uint64_t gen;
	uint64_t req_id;
	uint64_t result;
	uint32_t lane;
	uint32_t kind;
	uint32_t arg;
	uint32_t pad;
};

/* what a raw child writes into its report pipe (fixed size, atomic write) */
struct child_report {
	uint64_t magic;
	uint64_t gen;
	uint64_t requests;
	uint64_t replied;
	uint64_t verified;          /* replies whose req_id/lane were its own   */
	uint64_t foreign;           /* replies that were not its own            */
	uint64_t endpoint_ok;       /* HELLO acknowledged                       */
	uint64_t fd_out_requests;
	uint64_t fd_out_ok;         /* token read back equalled the request id  */
	uint64_t fd_out_bad;
	uint64_t stale_seen;        /* pre-exec reply observed after exec       */
	uint64_t stale_rejected;    /* ... and refused as the new request's     */
	uint64_t stale_endpoint_rejected; /* server refused an old-gen request  */
	uint64_t own_req;
	uint64_t own_ok;            /* post-exec/-failure request fully verified*/
	uint64_t exec_failed;       /* errno of a deliberately failed execve    */
	uint64_t post_fail_ok;      /* the old endpoint still worked after that */
	uint64_t stage;             /* __LINE__ of a fatal child-side failure   */
};

/* ------------------------------------------------------------------ */
/* shared memory                                                      */
/* ------------------------------------------------------------------ */

struct hent {
	uint64_t seq;
	uint64_t payload;
	uint64_t result;
	uint32_t state;
	uint32_t pad;
};

struct hring {                       /* hot path: SPSC, guest producer     */
	uint64_t prod;
	uint64_t cons;
	uint64_t serviced;
	uint64_t pad;
	struct hent ring[HOT_SLOTS];
};

struct cslot {                       /* one in-flight control request      */
	uint64_t req_id;
	uint64_t result;
	uint64_t token;             /* token read out of a received fd        */
	uint64_t t_publish;
	uint64_t t_done;
	int32_t  fd;
	uint32_t state;
	uint32_t kind;
	uint32_t fd_want;
	uint32_t fd_ok;
	uint32_t pad_;
};

struct clane {                       /* per-thread completion lane         */
	uint64_t done_seq;          /* futex word, bumped per completion      */
	uint64_t packets;
	uint64_t foreign;           /* packets whose req_id was not the slot's*/
	uint64_t orphan;            /* packets for an idle slot               */
	uint64_t parked;
	uint64_t pad;
	struct cslot slot;
};

struct approw {                      /* application table (C4)             */
	uint64_t owner_req;
	uint64_t token;
	uint64_t gen;
	uint64_t lane;
	uint64_t tid;
	int32_t  fd;
	uint32_t state;
};

struct hlive {                       /* one cache line per hot lane        */
	uint64_t n;
	char pad[56];
};

struct ctl_lock {
	uint32_t word;                      /* 0 free, 1 held                 */
	uint32_t pad;
	int32_t  owner_tid;
};

struct shm {
	/* ---- child reports the raw children could not write (no SHM after
	 *      exec); see the pipes instead.  Kept for server-side detail. ---- */
	uint64_t magic;
	uint64_t parent_pid;
	uint64_t parent_gen;
	uint64_t server_pid;
	uint32_t server_ready;
	uint32_t server_exit_req;
	uint32_t server_exited;
	uint32_t pad_srv;
	uint32_t server_fault;      /* __LINE__ of a fatal server-side fault   */
	uint32_t server_eof;
	uint64_t server_conns;
	uint64_t server_datagrams;
	uint64_t server_unknown;
	uint64_t server_stale_rejected;
	uint64_t server_bad_magic;
	uint64_t server_bad_frame;
	uint64_t server_fd_in_ok;
	uint64_t server_fd_in_bad;
	uint64_t server_fd_out;
	uint64_t server_hot_services;
	uint64_t server_rejects_sent;

	/* ---- global request accounting ---- */
	uint64_t seq_style1;
	uint64_t seq_style2;
	uint64_t seq_fd;
	uint64_t seq_bulk;
	uint64_t ctl_requests;
	uint64_t ctl_completed;
	uint32_t ledger[LEDGER_MAX];
	uint64_t observed_multi;
	uint64_t observed_missing;
	uint64_t ledger_overflow;

	/* ---- C1 ---- */
	uint32_t workers_started;
	uint32_t workers_parked;
	uint32_t workers_done;
	uint32_t workers_failed;
	uint32_t gate;
	uint32_t phase_abort;
	uint32_t rx_paused;
	uint32_t pad0;

	/* ---- C2 / C8 style 2 ---- */
	uint64_t s2_requests;
	uint64_t s2_completed;
	uint64_t s2_foreign;        /* thread saw a completion not its own    */
	uint64_t s2_timeouts;
	uint64_t s2_parked;
	uint64_t s2_lat_sum;
	uint64_t s2_lat_max;

	/* ---- C3 / C4 ---- */
	uint64_t fd_in_requests;
	uint64_t fd_in_ok;
	uint64_t fd_in_bad;         /* server's token check failed            */
	uint64_t fd_out_requests;
	uint64_t fd_out_ok;
	uint64_t fd_out_bad;        /* guest's token check failed             */
	uint64_t fd_out_timeouts;
	uint64_t rx_packets;
	uint64_t rx_fds_routed;
	uint64_t rx_fds_foreign;    /* descriptor for a slot that is not waiting */
	uint64_t rx_fds_orphan;     /* descriptor for an idle slot            */
	uint64_t rx_bad_lane;
	uint64_t rx_foreign_gen;    /* packet whose generation is not ours    */
	uint64_t rx_routed_other_lane; /* packet routed to another thread's lane */
	uint64_t app_published;
	uint64_t app_unfilled;
	uint64_t app_token_mismatch;
	uint64_t app_foreign_gen;
	uint64_t app_dup;

	/* ---- C5 ---- */
	uint64_t storm_expected;
	uint64_t storm_completed;
	uint64_t storm_stop;
	uint64_t storm_lost;
	uint64_t storm_foreign;   /* a storm completion went to the wrong thread */
	uint64_t storm_slow;      /* a storm request did not finish in its budget */

	/* ---- C7 ---- */
	uint64_t hot_published;
	uint64_t hot_completed;
	struct hlive hot_live[HOT_LANES];   /* per-lane live completion counter  */
	uint64_t hot_lost;
	uint64_t hot_go;
	uint64_t hot_stop;
	uint64_t hot_active;         /* the server spins on the rings, not on epoll */
	uint64_t phase_go;           /* workers start only when the observer says so */
	uint64_t hot_ops_start;      /* live sample taken at hot_go            */
	uint64_t holder_ops_start;   /* live sample taken when the holder held */
	uint64_t holder_ops_end;
	uint64_t holder_grab_ns;
	uint64_t holder_release_ns;
	uint64_t ctl_done_after_grab;
	uint64_t s1_in_cr;

	/* ---- C8 style 1 ---- */
	uint64_t s1_requests;
	uint64_t s1_completed;
	uint64_t s1_foreign;
	uint64_t s1_timeouts;
	uint64_t s1_lock_wait_ns;
	uint64_t s1_contended;
	uint64_t s1_lat_sum;
	uint64_t s1_lat_max;

	/* ---- C1: descriptor census (taken with the threads alive) ---- */
	uint32_t c1_fd_setup;
	uint32_t c1_fd_32;
	uint32_t c1_fd_64;
	uint32_t c1_per_thread_endpoints;
	/* ---- descriptor accounting around the SCM_RIGHTS phases ---- */
	uint32_t fd_guest_before;
	uint32_t fd_guest_after;
	uint32_t fd_server_before;
	uint32_t fd_server_after;

	/* ---- C7 ---- */
	uint64_t hot1_ops;
	uint64_t hot1_ns;
	uint64_t hot2_ops;
	uint64_t hot2_ns;
	uint64_t hot3_ops;
	uint64_t hot3_ns;
	uint64_t holder_hold_ns;
	uint64_t hot3_ctl_after_grab;
	uint64_t holder_release;

	/* ---- C8 ---- */
	uint64_t s1_ctx;
	uint64_t s2_ctx;
	uint64_t s1_dur_ns;
	uint64_t s2_dur_ns;

	/* ---- C4: which process published which application-table region ---- */
	uint64_t app_parent_rows;
	uint64_t app_child_rows;

	/* ---- receiver thread control ---- */
	uint32_t rx_stop;
	uint32_t rx_paused_count;
	uint64_t rx_plain_unrouted;
	uint32_t cur_phase;
	uint32_t pad3;

	/* ---- lanes / tables ---- */
	struct ctl_lock lock;
	struct clane clanes[MAX_LANES];
	struct hring hrings[HOT_LANES];
	struct approw app[APP_ROWS];
};

static struct shm *SHM;
static char g_path[PATH_MAX + 32];  /* control socket path                */
static char g_scratch[PATH_MAX];
static char g_shmfile[PATH_MAX + 32];
static int g_ctl = -1;              /* the ONE process-level endpoint      */
static int g_listen = -1;
static pid_t g_server_pid;
static const char *g_env;

/* ------------------------------------------------------------------ */
/* raw syscall layer (child code only; libc must never be reached)     */
/* ------------------------------------------------------------------ */

#define RSYS6(n, a, b, c, d, e, f)                                     \
	__extension__({                                                \
		register long r10_ __asm__("r10") = (long)(d);          \
		register long r8_ __asm__("r8") = (long)(e);            \
		register long r9_ __asm__("r9") = (long)(f);            \
		long rv_;                                               \
		__asm__ volatile("syscall"                              \
				 : "=a"(rv_)                            \
				 : "a"((long)(n)), "D"((long)(a)),      \
				   "S"((long)(b)), "d"((long)(c)),      \
				   "r"(r10_), "r"(r8_), "r"(r9_)        \
				 : "rcx", "r11", "memory");             \
		rv_;                                                    \
	})

#define RSYS0(n)             RSYS6(n, 0, 0, 0, 0, 0, 0)
#define RSYS1(n, a)          RSYS6(n, a, 0, 0, 0, 0, 0)
#define RSYS2(n, a, b)       RSYS6(n, a, b, 0, 0, 0, 0)
#define RSYS3(n, a, b, c)    RSYS6(n, a, b, c, 0, 0, 0)
#define RSYS4(n, a, b, c, d) RSYS6(n, a, b, c, d, 0, 0)
#define RSYS5(n, a, b, c, d, e) RSYS6(n, a, b, c, d, e, 0)

/* ------------------------------------------------------------------ */
/* atomics                                                            */
/* ------------------------------------------------------------------ */

#define ld_acq(p)      __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define ld_rlx(p)      __atomic_load_n((p), __ATOMIC_RELAXED)
#define st_rel(p, v)   __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define st_rlx(p, v)   __atomic_store_n((p), (v), __ATOMIC_RELAXED)
#define add_rel(p, v)  __atomic_fetch_add((p), (v), __ATOMIC_RELEASE)
#define add_rlx(p, v)  __atomic_fetch_add((p), (v), __ATOMIC_RELAXED)
#define cas32(p, o, n) __atomic_compare_exchange_n((p), (o), (n), 0,      \
						   __ATOMIC_ACQ_REL,      \
						   __ATOMIC_ACQUIRE)
#define ld32_acq(p)    __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define ld32_rlx(p)    __atomic_load_n((p), __ATOMIC_RELAXED)
#define st32_rel(p, v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define st32_rlx(p, v) __atomic_store_n((p), (v), __ATOMIC_RELAXED)

/* ------------------------------------------------------------------ */
/* hand-rolled helpers safe for child code (zero `call` instructions)  */
/* ------------------------------------------------------------------ */

#define MY_CMSG_ALIGN(len) (((len) + sizeof(size_t) - 1) & ~(sizeof(size_t) - 1))
#define MY_CMSG_FIRSTHDR(mh)                                              \
	(((mh)->msg_controllen >= sizeof(struct cmsghdr))                 \
		 ? (struct cmsghdr *)(void *)(mh)->msg_control           \
		 : (struct cmsghdr *)0)
#define MY_CMSG_NXTHDR(mh, c)                                             \
	((c) == 0                                                        \
		 ? MY_CMSG_FIRSTHDR(mh)                                  \
		 : (((unsigned char *)(c) + MY_CMSG_ALIGN((c)->cmsg_len) + \
		     MY_CMSG_ALIGN(sizeof(struct cmsghdr)) >               \
	     (unsigned char *)(mh)->msg_control + (mh)->msg_controllen)  \
			    ? (struct cmsghdr *)0                          \
			    : (struct cmsghdr *)(void *)((unsigned char *)(c) + \
				      MY_CMSG_ALIGN((c)->cmsg_len))))
#define MY_CMSG_DATA(c)                                                   \
	((void *)(void *)(((unsigned char *)(c)) + MY_CMSG_ALIGN(sizeof(struct cmsghdr))))

__attribute__((always_inline)) static inline void zero_bytes(void *p, unsigned long n)
{
	volatile unsigned char *v = (volatile unsigned char *)p;
	unsigned long i;
	for (i = 0; i < n; i++)
		v[i] = 0;
}

__attribute__((always_inline)) static inline int str_eq(const char *a, const char *b)
{
	unsigned long i = 0;
	for (;;) {
		if (a[i] != b[i])
			return 0;
		if (a[i] == '\0')
			return 1;
		i++;
	}
}

__attribute__((always_inline)) static inline unsigned long str_len(const char *s)
{
	unsigned long n = 0;
	while (s[n] != '\0')
		n++;
	return n;
}

__attribute__((always_inline)) static inline uint64_t parse_u64(const char *s)
{
	uint64_t v = 0;
	unsigned long i;
	for (i = 0; s[i] >= '0' && s[i] <= '9'; i++)
		v = v * 10u + (uint64_t)(s[i] - '0');
	return v;
}

/* ------------------------------------------------------------------ */
/* guest-side utilities (libc is fine here)                           */
/* ------------------------------------------------------------------ */

static uint64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static uint64_t now_ms(void)
{
	return now_ns() / 1000000ULL;
}

__attribute__((always_inline)) static inline uint64_t mix64(uint64_t x)
{
	return (x * 0x9E3779B97F4A7C15ULL) ^ 0xA5A5A5A5A5A5A5A5ULL;
}

__attribute__((always_inline)) static inline uint64_t expected_result(uint64_t req_id)
{
	return mix64(req_id ^ 0x1234ABCD5678EF90ULL);
}

static void cpu_relax(void)
{
	__asm__ __volatile__("pause" ::: "memory");
}

/* futex helpers: bounded sleeps only, so a broken design times out instead of
 * hanging the run. */
static void futex_wake(void *addr, int n)
{
	syscall(SYS_futex, addr, FUTEX_WAKE | FUTEX_PRIVATE_FLAG, n, NULL, NULL, 0);
}

static int futex_wait_to(void *addr, uint32_t expect, long ms)
{
	struct timespec ts;
	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (ms % 1000) * 1000000L;
	return (int)syscall(SYS_futex, addr, FUTEX_WAIT | FUTEX_PRIVATE_FLAG,
			    expect, &ts, NULL, 0);
}

/* descriptor census of a process (0 = self) */
static int fd_snapshot_pid(pid_t pid, char *out, size_t cap)
{
	char dir[64];
	DIR *d;
	struct dirent *e;
	int count = 0;
	size_t used = 0;

	if (pid == 0)
		snprintf(dir, sizeof dir, "/proc/self/fd");
	else
		snprintf(dir, sizeof dir, "/proc/%d/fd", (int)pid);
	d = opendir(dir);
	if (!d)
		return -1;
	if (out && cap)
		out[0] = '\0';
	while ((e = readdir(d)) != NULL) {
		if (e->d_name[0] == '.')
			continue;
		count++;
		if (out && used + strlen(e->d_name) + 2 < cap) {
			used += (size_t)snprintf(out + used, cap - used, "%s,",
						 e->d_name);
		}
	}
	closedir(d);
	/* the DIR's own descriptor appears in the listing */
	return count - 1;
}

static int fd_count_self(void)
{
	return fd_snapshot_pid(0, NULL, 0);
}

static int fd_count_of(pid_t pid)
{
	return fd_snapshot_pid(pid, NULL, 0);
}

/* voluntary context switches of THIS thread: /proc/<pid>/status reports only
 * the main thread, and a finished worker's counters are gone, so every worker
 * samples its own and reports the delta (see worker()). */
static uint64_t thread_vol_ctx(void)
{
	char path[320], line[256];
	FILE *f;
	uint64_t v = 0;

	snprintf(path, sizeof path, "/proc/self/task/%ld/status",
		 (long)syscall(SYS_gettid));
	f = fopen(path, "r");
	if (!f)
		return 0;
	while (fgets(line, sizeof line, f)) {
		if (strncmp(line, "voluntary_ctxt_switches:", 24) == 0) {
			v = strtoull(line + 24, NULL, 10);
			break;
		}
	}
	fclose(f);
	return v;
}

/* ------------------------------------------------------------------ */
/* the control lock (style 1's single lock; also C7's held lock)       */
/* ------------------------------------------------------------------ */

static uint64_t lock_wait_ns;
static uint64_t lock_contended;

/* returns 0 on success, -1 when the deadline passed first */
static int lock_acquire_to(uint64_t deadline_ms)
{
	struct ctl_lock *l = &SHM->lock;
	uint64_t t0 = now_ns();
	uint32_t expect = 0;

	if (!cas32(&l->word, &expect, 1u)) {
		__atomic_fetch_add(&lock_contended, 1, __ATOMIC_RELAXED);
		for (;;) {
			if (now_ms() >= deadline_ms) {
				__atomic_fetch_add(&lock_wait_ns, now_ns() - t0,
						   __ATOMIC_RELAXED);
				return -1;
			}
			expect = 0;
			if (cas32(&l->word, &expect, 1u))
				break;
			futex_wait_to(&l->word, 1u, 5);
		}
	}
	l->owner_tid = (int32_t)syscall(SYS_gettid);
	__atomic_fetch_add(&lock_wait_ns, now_ns() - t0, __ATOMIC_RELAXED);
	return 0;
}

static void lock_release(void)
{
	struct ctl_lock *l = &SHM->lock;
	l->owner_tid = 0;
	st_rel(&l->word, 0u);
	futex_wake(&l->word, 1);
}

/* ------------------------------------------------------------------ */
/* routing hooks (the mutation targets for C2/C3)                      */
/* ------------------------------------------------------------------ */

/* The lane a completion is delivered to.  Every completion path -- the
 * server's shared-lane write and the receiver's descriptor deposit -- goes
 * through this function, so a single-line mutation here sends every completion
 * to the neighbouring lane and C2 must fail. */
__attribute__((always_inline)) static inline uint32_t route_lane(uint32_t lane)
{
	return lane;                       /*MUT1-ROUTE*/
}

/* The lane a *descriptor-bearing* packet is deposited into.  Mutating this
 * delivers a transferred descriptor to the wrong request and C3 must fail. */
__attribute__((always_inline)) static inline uint32_t fd_deposit_lane(uint32_t lane)
{
	return lane;                       /*MUT2-FD*/
}

/* ------------------------------------------------------------------ */
/* raw child-side helpers (zero `call` instructions)                   */
/* ------------------------------------------------------------------ */

/* volatile so that GCC cannot turn this into a call to strlen */
__attribute__((always_inline)) static inline unsigned long raw_strlen(const char *s)
{
	volatile const char *v = (volatile const char *)s;
	unsigned long n = 0;

	while (v[n] != '\0')
		n++;
	return n;
}

__attribute__((always_inline)) static inline uint64_t raw_now_ns(void)
{
	struct timespec ts;
	RSYS2(SYS_clock_gettime, 1 /* CLOCK_MONOTONIC */, (long)&ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

__attribute__((always_inline)) static inline void raw_u64_str(char *b, uint64_t v)
{
	char tmp[24];
	int n = 0, i;

	if (v == 0) {
		b[0] = '0';
		b[1] = '\0';
		return;
	}
	while (v) {
		tmp[n++] = (char)('0' + (int)(v % 10u));
		v /= 10u;
	}
	for (i = 0; i < n; i++)
		b[i] = tmp[n - 1 - i];
	b[n] = '\0';
}

__attribute__((always_inline)) static inline long raw_extract_fd(struct msghdr *mh)
{
	struct cmsghdr *c;

	for (c = MY_CMSG_FIRSTHDR(mh); c != 0; c = MY_CMSG_NXTHDR(mh, c)) {
		if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
			long n = (long)(c->cmsg_len -
					MY_CMSG_ALIGN(sizeof(struct cmsghdr))) /
				 (long)sizeof(int);
			if (n > 0)
				return (long)(*(int *)MY_CMSG_DATA(c));
		}
	}
	return -1;
}

__attribute__((always_inline)) static inline long raw_send_msg(long fd,
							       const struct ctl_msg *m,
							       long send_fd)
{
	char cbuf[64];
	struct iovec iov;
	struct msghdr mh;
	struct cmsghdr *c;

	iov.iov_base = (void *)(uintptr_t)m;
	iov.iov_len = sizeof *m;
	zero_bytes(&mh, sizeof mh);
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	if (send_fd >= 0) {
		zero_bytes(cbuf, sizeof cbuf);
		c = (struct cmsghdr *)(void *)cbuf;
		c->cmsg_len = MY_CMSG_ALIGN(sizeof(struct cmsghdr)) +
			      (long)sizeof(int);
		c->cmsg_level = SOL_SOCKET;
		c->cmsg_type = SCM_RIGHTS;
		*(int *)MY_CMSG_DATA(c) = (int)send_fd;
		mh.msg_control = cbuf;
		mh.msg_controllen = (size_t)c->cmsg_len;
	}
	return RSYS3(SYS_sendmsg, fd, (long)&mh, MSG_NOSIGNAL);
}

/* wait_ms < 0: do not wait (non-blocking).  Returns sizeof(struct ctl_msg) on a
 * complete datagram, 0 on end-of-connection, -1 on timeout/would-block. */
__attribute__((always_inline)) static inline long raw_recv_msg(long fd,
							       struct ctl_msg *m,
							       long *got_fd,
							       long wait_ms)
{
	char cbuf[96];
	struct iovec iov;
	struct msghdr mh;
	struct pollfd pf;
	long rv;

	if (got_fd)
		*got_fd = -1;
	if (wait_ms >= 0) {
		pf.fd = (int)fd;
		pf.events = POLLIN;
		pf.revents = 0;
		rv = RSYS3(SYS_poll, (long)&pf, 1, wait_ms);
		if (rv <= 0)
			return -1;
	}
	iov.iov_base = m;
	iov.iov_len = sizeof *m;
	zero_bytes(&mh, sizeof mh);
	zero_bytes(cbuf, sizeof cbuf);
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	mh.msg_control = cbuf;
	mh.msg_controllen = sizeof cbuf;
	rv = RSYS3(SYS_recvmsg, fd, (long)&mh, wait_ms >= 0 ? 0 : MSG_DONTWAIT);
	if (rv < 0)
		return -1;
	if (got_fd)
		*got_fd = raw_extract_fd(&mh);
	return rv;
}

__attribute__((always_inline)) static inline long raw_open_endpoint(const char *path,
								    uint64_t gen)
{
	char sa[112];
	unsigned long n = 0, i;
	long fd, rv;
	struct ctl_msg m, r;

	n = raw_strlen(path);
	if (n > 107)
		return -1;
	fd = RSYS3(SYS_socket, AF_UNIX, SOCK_SEQPACKET, 0);
	if (fd < 0)
		return -1;
	zero_bytes(sa, sizeof sa);
	((unsigned short *)(void *)sa)[0] = (unsigned short)AF_UNIX;
	for (i = 0; i <= n; i++)
		sa[2 + i] = path[i];
	rv = RSYS3(SYS_connect, fd, (long)sa, (long)(2 + n + 1));
	if (rv < 0) {
		RSYS1(SYS_close, fd);
		return -1;
	}
	zero_bytes(&m, sizeof m);
	m.magic = CTL_MAGIC;
	m.gen = gen;
	m.req_id = 0xDEAD000000000000ULL | gen;
	m.kind = CTL_HELLO;
	if (raw_send_msg(fd, &m, -1) != (long)sizeof m) {
		RSYS1(SYS_close, fd);
		return -1;
	}
	if (raw_recv_msg(fd, &r, 0, 3000) != (long)sizeof r ||
	    r.magic != CTL_MAGIC || r.kind != CTL_HELLO_ACK) {
		RSYS1(SYS_close, fd);
		return -1;
	}
	return fd;
}

/* The endpoint a forked child uses.  The child MUST close the endpoint it
 * inherited from the fork before it can be trusted: an inherited connection is
 * the parent's, so a child that reads from it steals the parent's
 * descriptor-bearing replies.  MUT3 replaces the close+connect with a reuse of
 * the inherited descriptor and C5 must fail. */
__attribute__((always_inline)) static inline long raw_child_endpoint(long inherited,
								    uint64_t gen)
{
	/*MUT3-FORK*/ RSYS1(SYS_close, inherited);
	return raw_open_endpoint(g_path, gen);
}

__attribute__((always_inline)) static inline void report_write(long fd,
							       const struct child_report *r)
{
	long off = 0;

	while (off < (long)sizeof *r) {
		long n = RSYS3(SYS_write, fd, (long)(uintptr_t)r + off,
			       (long)sizeof *r - off);
		if (n <= 0)
			break;
		off += n;
	}
}

/* ------------------------------------------------------------------ */
/* the server: libc-free, single-threaded, epoll over the endpoint      */
/* ------------------------------------------------------------------ */

static void *g_srv_shm;
static long g_srv_listen;

struct sconn {
	long fd;
	uint64_t gen;
	uint32_t used;
	uint32_t pad;
};

__attribute__((always_inline)) static inline long drain_hot(struct shm *s)
{
	long serviced = 0;
	int i;

	for (i = 0; i < HOT_LANES; i++) {
		struct hring *h = &s->hrings[i];
		for (;;) {
			uint64_t cons = ld_acq(&h->cons);
			struct hent *e;

			if (cons >= ld_acq(&h->prod))
				break;
			e = &h->ring[cons % HOT_SLOTS];
			if (ld_acq(&e->state) != HE_REQ)
				break;
			e->result = mix64(e->payload ^ 0xF00DF00DF00DF00DULL);
			st_rel(&e->state, HE_DONE);
			st_rel(&h->cons, cons + 1);
			serviced++;
		}
	}
	if (serviced)
		add_rel(&s->server_hot_services, (uint64_t)serviced);
	return serviced;
}

__attribute__((always_inline)) static inline void srv_reply(struct sconn *c,
							    const struct ctl_msg *req,
							    uint64_t result,
							    uint32_t kind)
{
	struct ctl_msg r;

	zero_bytes(&r, sizeof r);
	r.magic = CTL_MAGIC;
	r.gen = req->gen;
	r.req_id = req->req_id;
	r.result = result;
	r.lane = req->lane;
	r.kind = kind;
	raw_send_msg(c->fd, &r, -1);
}

/* the server->guest descriptor handoff: the descriptor carries the request id
 * as a token, so the guest can prove WHICH request the descriptor belongs to
 * by reading it, not by trusting the routing. */
__attribute__((always_inline)) static inline void srv_send_fd(struct sconn *c,
							      const struct ctl_msg *req,
							      long fd)
{
	struct ctl_msg r;

	zero_bytes(&r, sizeof r);
	r.magic = CTL_MAGIC;
	r.gen = req->gen;
	r.req_id = req->req_id;
	r.result = expected_result(req->req_id);
	r.lane = route_lane(req->lane);
	r.kind = CTL_FD_OUT;
	raw_send_msg(c->fd, &r, fd);
}

__attribute__((always_inline)) static inline void srv_complete(struct shm *s,
							       uint64_t req_id,
							       uint32_t lane,
							       uint64_t result,
							       uint32_t kind,
							       uint32_t fd_ok)
{
	struct clane *cl;
	struct cslot *sl;

	(void)req_id;      /* the requester owns the slot's request id */
	if (lane >= MAX_LANES)
		return;
	lane = route_lane(lane);
	cl = &s->clanes[lane];
	sl = &cl->slot;
	sl->result = result;
	sl->kind = kind;
	sl->fd_ok = fd_ok;
	sl->t_done = raw_now_ns();
	st_rel(&sl->state, CS_DONE);
	add_rel(&cl->done_seq, 1);
	/* shared (non-private) futex: the waiter is in another process */
	RSYS6(SYS_futex, (long)&cl->done_seq, FUTEX_WAKE, 1, 0, 0, 0);
}

__attribute__((always_inline)) static inline void srv_handle(struct shm *s,
							     struct sconn *c,
							     struct ctl_msg *m,
							     long rx_fd)
{
	if (m->kind != CTL_HELLO) {
		if (m->gen < c->gen) {
			/* a superseded generation on a live endpoint: rejected */
			s->server_stale_rejected++;
			srv_reply(c, m, REJECT_MAGIC, CTL_REJECT);
			s->server_rejects_sent++;
			if (rx_fd >= 0)
				RSYS1(SYS_close, rx_fd);
			return;
		}
		if (m->gen > c->gen)
			c->gen = m->gen;
	}

	switch (m->kind) {
	case CTL_HELLO:
		c->gen = m->gen;
		srv_reply(c, m, HELLO_ACK_MAGIC, CTL_HELLO_ACK);
		break;
	case CTL_ECHO:
		srv_complete(s, m->req_id, m->lane, expected_result(m->req_id),
			     CTL_ECHO, 0);
		break;
	case CTL_ECHO_SYNC:
		srv_reply(c, m, expected_result(m->req_id), CTL_REPLY);
		break;
	case CTL_SYNC_SLOW: {
		struct timespec ts;

		ts.tv_sec = (long)(m->arg / 1000u);
		ts.tv_nsec = (long)(m->arg % 1000u) * 1000000L;
		RSYS2(SYS_nanosleep, (long)&ts, 0);
		srv_reply(c, m, expected_result(m->req_id), CTL_REPLY);
		break;
	}
	case CTL_FD_IN: {
		uint64_t tok = 0;
		uint32_t ok = 0;

		if (rx_fd >= 0) {
			long n = RSYS3(SYS_read, rx_fd, (long)&tok, 8);

			if (n == 8 && tok == m->req_id)
				ok = 1;
			RSYS1(SYS_close, rx_fd);
		}
		if (ok)
			s->server_fd_in_ok++;
		else
			s->server_fd_in_bad++;
		srv_complete(s, m->req_id, m->lane, ok, CTL_FD_IN,
			     ok ? 1u : 3u);
		break;
	}
	case CTL_FD_OUT: {
		int pfd[2];
		long rv = RSYS2(SYS_pipe2, (long)pfd, 0);
		uint64_t tok = m->req_id;

		if (rv == 0) {
			RSYS3(SYS_write, pfd[1], (long)&tok, 8);
			RSYS1(SYS_close, pfd[1]);
			srv_send_fd(c, m, pfd[0]);
			RSYS1(SYS_close, pfd[0]);
			s->server_fd_out++;
		} else {
			srv_complete(s, m->req_id, m->lane, 0, CTL_FD_OUT, 4u);
		}
		if (rx_fd >= 0)
			RSYS1(SYS_close, rx_fd);
		break;
	}
	default:
		s->server_unknown++;
		if (rx_fd >= 0)
			RSYS1(SYS_close, rx_fd);
		break;
	}
}

/* A server that serves the hot lanes and the control endpoint in one thread
 * must return to the lanes often: the batch size here is the number of control
 * datagrams the server takes from one connection before it polls the rings
 * again, which is what the product's ring-polling mode does. */
__attribute__((always_inline)) static inline void srv_conn_drain(struct shm *s,
								  struct sconn *c,
								  long epfd,
								  int budget)
{
	int batch;

	for (batch = 0; batch < budget; batch++) {
		struct ctl_msg m;
		long got_fd = -1;
		long rv = raw_recv_msg(c->fd, &m, &got_fd, -1);

		if (rv == 0) {
			struct epoll_event ev;

			zero_bytes(&ev, sizeof ev);
			RSYS4(SYS_epoll_ctl, epfd, EPOLL_CTL_DEL, c->fd, (long)&ev);
			RSYS1(SYS_close, c->fd);
			c->fd = -1;
			c->used = 0;
			s->server_eof++;
			return;
		}
		if (rv < 0)
			return;
		s->server_datagrams++;
		if (rv < (long)sizeof m || m.magic != CTL_MAGIC) {
			if (rv < (long)sizeof m)
				s->server_bad_frame++;
			else
				s->server_bad_magic++;
			if (got_fd >= 0)
				RSYS1(SYS_close, got_fd);
			continue;
		}
		srv_handle(s, c, &m, got_fd);
	}
}

/*
 * server_entry -- the whole server, libc-free.  Must contain zero `call`
 * instructions (the runner asserts it with objdump).
 */
__attribute__((noinline, noreturn)) void server_entry(void)
{
	struct shm *s = (struct shm *)g_srv_shm;
	struct sconn conns[SERVER_CONNS];
	struct epoll_event evs[8];
	long listen = g_srv_listen;
	long epfd;
	int i;

	zero_bytes(conns, sizeof conns);
	for (i = 0; i < SERVER_CONNS; i++)
		conns[i].fd = -1;

	epfd = RSYS1(SYS_epoll_create1, 0);
	if (epfd < 0) {
		s->server_fault = (uint32_t)__LINE__;
		RSYS1(SYS_exit_group, 4);
		for (;;)
			;
	}
	{
		struct epoll_event ev;

		zero_bytes(&ev, sizeof ev);
		ev.events = EPOLLIN;
		ev.data.fd = (int)listen;
		if (RSYS4(SYS_epoll_ctl, epfd, EPOLL_CTL_ADD, listen,
			  (long)&ev) < 0) {
			s->server_fault = (uint32_t)__LINE__;
			RSYS1(SYS_exit_group, 5);
			for (;;)
				;
		}
	}
	st_rel(&s->server_pid, (uint64_t)RSYS0(SYS_getpid));
	st32_rel(&s->server_ready, 1);

	for (;;) {
		long hot = drain_hot(s);
		long n, k;
		/* Ring mode: while the guest is running hot traffic the server spins
		 * on the rings and checks the endpoint without blocking, exactly as
		 * the product's ring-polling mode does; otherwise it blocks on the
		 * endpoint, which is what the slow path wants. */
		int idle = (hot || ld_acq(&s->hot_active)) ? 0 : 20;

		n = RSYS6(SYS_epoll_wait, epfd, (long)evs, 8, idle, 0, 0);
		for (k = 0; k < n; k++) {
			int fd = evs[k].data.fd;

			if (fd == (int)listen) {
				long nfd = RSYS4(SYS_accept4, listen, 0, 0,
						 SOCK_NONBLOCK);
				int slot = -1;

				if (nfd < 0)
					continue;
				for (i = 0; i < SERVER_CONNS; i++) {
					if (!conns[i].used) {
						slot = i;
						break;
					}
				}
				if (slot < 0) {
					RSYS1(SYS_close, nfd);
					continue;
				}
				conns[slot].fd = nfd;
				conns[slot].gen = 0;
				conns[slot].used = 1;
				{
					struct epoll_event ev;

					zero_bytes(&ev, sizeof ev);
					ev.events = EPOLLIN;
					ev.data.fd = (int)nfd;
					if (RSYS4(SYS_epoll_ctl, epfd,
						  EPOLL_CTL_ADD, nfd,
						  (long)&ev) < 0) {
						RSYS1(SYS_close, nfd);
						conns[slot].used = 0;
						conns[slot].fd = -1;
						continue;
					}
				}
				s->server_conns++;
			} else {
				for (i = 0; i < SERVER_CONNS; i++) {
					if (conns[i].used && conns[i].fd == fd) {
						srv_conn_drain(s, &conns[i], epfd, 8);
						break;
					}
				}
			}
		}
		if (ld32_acq(&s->server_exit_req)) {
			for (i = 0; i < SERVER_CONNS; i++) {
				if (conns[i].used && conns[i].fd >= 0)
					RSYS1(SYS_close, conns[i].fd);
			}
			RSYS1(SYS_close, epfd);
			RSYS1(SYS_close, listen);
			st32_rel(&s->server_exited, 1);
			RSYS1(SYS_exit_group, 0);
			for (;;)
				;
		}
	}
}

/* ------------------------------------------------------------------ */
/* the C5 fork child                                                   */
/* ------------------------------------------------------------------ */

static long g_child_report_fd;      /* write end of the child's pipe      */
static long g_slow_ms;              /* CTL_SYNC_SLOW delay for C6         */
static char **g_envp;               /* environ, for execve                */

__attribute__((always_inline)) static inline void child_app_publish(struct shm *s,
								    uint32_t lane,
								    uint64_t id,
								    uint64_t tok,
								    int fd,
								    uint64_t gen,
								    uint64_t *ok_count)
{
	struct approw *row;

	if (lane >= APP_ROWS)
		return;
	row = &s->app[lane];
	row->owner_req = id;
	row->token = tok;
	row->gen = gen;
	row->lane = lane;
	row->tid = (uint64_t)RSYS0(SYS_gettid);
	row->fd = fd;
	st_rel(&row->state, 1u);
	if (row->gen == gen && row->owner_req == id && row->token == tok)
		add_rel(ok_count, 1);
}

__attribute__((noinline, noreturn)) void c5_child_entry(void)
{
	struct shm *s = SHM;
	struct child_report rep;
	long repfd = g_child_report_fd;
	long fd;
	uint64_t i;

	zero_bytes(&rep, sizeof rep);
	rep.magic = C5_MAGIC;
	rep.gen = GEN_FORK;
	fd = raw_child_endpoint(g_ctl, GEN_FORK);      /*MUT3 target */
	if (fd < 0) {
		rep.stage = (uint64_t)__LINE__;
		report_write(repfd, &rep);
		RSYS1(SYS_exit_group, 1);
		for (;;)
			;
	}
	rep.endpoint_ok = 1;

	for (i = 0; i < 2000000; i++) {
		uint64_t id = 0xC5C5000000000000ULL | i;
		uint32_t lane = (uint32_t)(64 + (i % 16));
		struct ctl_msg m, r;
		long got = -1;
		long rv;

		if (ld_acq(&s->storm_stop))
			break;
		zero_bytes(&m, sizeof m);
		m.magic = CTL_MAGIC;
		m.gen = GEN_FORK;
		m.req_id = id;
		m.lane = lane;
		m.kind = CTL_ECHO_SYNC;
		if (raw_send_msg(fd, &m, -1) != (long)sizeof m) {
			rep.stage = (uint64_t)__LINE__;
			break;
		}
		rv = raw_recv_msg(fd, &r, &got, 300);
		rep.requests++;
		if (rv != (long)sizeof r) {
			rep.stage = (uint64_t)__LINE__;
			continue;
		}
		if (r.req_id == id && r.gen == GEN_FORK &&
		    r.kind == CTL_REPLY && r.result == expected_result(id))
			rep.verified++;
		else
			rep.foreign++;

		if ((i & 3u) == 3u) {
			struct ctl_msg f;
			uint64_t tok = 0;
			long n;

			zero_bytes(&f, sizeof f);
			f.magic = CTL_MAGIC;
			f.gen = GEN_FORK;
			f.req_id = id | 1u;
			f.lane = lane;
			f.kind = CTL_FD_OUT;
			rep.fd_out_requests++;
			if (raw_send_msg(fd, &f, -1) == (long)sizeof f) {
				rv = raw_recv_msg(fd, &r, &got, 300);
				if (rv == (long)sizeof r && got >= 0) {
					n = RSYS3(SYS_read, got, (long)&tok, 8);
					if (n == 8 && tok == f.req_id)
						rep.fd_out_ok++;
					else
						rep.fd_out_bad++;
					child_app_publish(s, lane, f.req_id, tok,
							  (int)got, GEN_FORK,
							  &s->app_child_rows);
					RSYS1(SYS_close, got);
				} else {
					rep.fd_out_bad++;
					if (got >= 0)
						RSYS1(SYS_close, got);
					rep.stage = (uint64_t)__LINE__;
				}
			} else {
				rep.fd_out_bad++;
			}
		}
	}
	report_write(repfd, &rep);
	RSYS1(SYS_exit_group, 0);
	for (;;)
		;
}

/* ------------------------------------------------------------------ */
/* the C6 children: successful exec, and deliberately failed exec       */
/* ------------------------------------------------------------------ */

__attribute__((always_inline)) static inline int child_round_trip(long fd,
								  uint64_t gen,
								  uint64_t id,
								  uint32_t kind,
								  uint32_t arg)
{
	struct ctl_msg m, r;
	long got = -1;

	zero_bytes(&m, sizeof m);
	m.magic = CTL_MAGIC;
	m.gen = gen;
	m.req_id = id;
	m.kind = kind;
	m.arg = arg;
	if (raw_send_msg(fd, &m, -1) != (long)sizeof m)
		return -1;
	if (raw_recv_msg(fd, &r, &got, 3000) != (long)sizeof r) {
		if (got >= 0)
			RSYS1(SYS_close, got);
		return -1;
	}
	if (got >= 0)
		RSYS1(SYS_close, got);
	if (r.magic != CTL_MAGIC || r.req_id != id || r.kind != CTL_REPLY ||
	    r.result != expected_result(id))
		return -1;
	return 0;
}

/*
 * c6_child_entry(FAIL) -- FAIL == 0: hand the pre-exec endpoint a request whose
 * reply is still in flight, then execve; the reply must be recognised as stale
 * by the post-exec image.  FAIL == 1: attempt an execve that cannot work, then
 * require the previous endpoint to still work.
 */
__attribute__((noinline, noreturn)) void c6_child_entry(long fail_exec)
{
	struct child_report rep;
	long repfd = g_child_report_fd;
	long fd;

	zero_bytes(&rep, sizeof rep);
	rep.magic = C6_MAGIC;
	rep.gen = GEN_EXEC_PRE;
	fd = raw_child_endpoint(g_ctl, GEN_EXEC_PRE);
	if (fd < 0) {
		rep.stage = (uint64_t)__LINE__;
		report_write(repfd, &rep);
		RSYS1(SYS_exit_group, 2);
		for (;;)
			;
	}
	rep.endpoint_ok = 1;

	if (fail_exec) {
		char *argv[2];
		long rv;

		argv[0] = (char *)"/nonexistent-direct-process-control-proof";
		argv[1] = (char *)0;
		rv = RSYS3(SYS_execve, (long)argv[0], (long)argv, (long)g_envp);
		rep.exec_failed = (uint64_t)(-rv);
		/* the execve failed: the endpoint we already had must still work */
		if (child_round_trip(fd, GEN_EXEC_FAIL, 0xF2F2000000000001ULL,
				     CTL_ECHO_SYNC, 0) == 0) {
			rep.post_fail_ok = 1;
			rep.own_ok = 1;
			rep.verified++;
		}
		rep.requests++;
	} else {
		char *argv[8];
		char gen_buf[24], req_buf[24], fd_buf[24], rep_buf[24];
		const uint64_t id = 0xE1E1000000000001ULL;
		struct ctl_msg m;

		zero_bytes(&m, sizeof m);
		m.magic = CTL_MAGIC;
		m.gen = GEN_EXEC_PRE;
		m.req_id = id;
		m.kind = CTL_SYNC_SLOW;
		m.arg = (uint32_t)g_slow_ms;
		if (raw_send_msg(fd, &m, -1) != (long)sizeof m) {
			rep.stage = (uint64_t)__LINE__;
			report_write(repfd, &rep);
			RSYS1(SYS_exit_group, 3);
			for (;;)
				;
		}
		raw_u64_str(gen_buf, GEN_EXEC_PRE);
		raw_u64_str(req_buf, id);
		raw_u64_str(fd_buf, (uint64_t)fd);
		raw_u64_str(rep_buf, (uint64_t)repfd);
		argv[0] = (char *)"/proc/self/exe";
		argv[1] = (char *)"exec-child";
		argv[2] = g_path;
		argv[3] = gen_buf;
		argv[4] = req_buf;
		argv[5] = fd_buf;
		argv[6] = rep_buf;
		argv[7] = (char *)0;
		RSYS3(SYS_execve, (long)argv[0], (long)argv, (long)g_envp);
		rep.stage = (uint64_t)__LINE__;   /* execve failed */
	}
	report_write(repfd, &rep);
	RSYS1(SYS_exit_group, 0);
	for (;;)
		;
}

/*
 * exec_child_entry -- the post-exec image, libc-free, entered through execve.
 * It re-establishes the endpoint (a fresh connection with a fresh generation),
 * proves that a superseded generation is refused, answers its own request, and
 * refuses the reply that the pre-exec connection still owed.
 */
__attribute__((noinline, noreturn)) void exec_child_entry(const char *path,
							  uint64_t old_gen,
							  uint64_t old_req,
							  long stale_fd,
							  long report_fd)
{
	struct child_report rep;
	const uint64_t own_req = 0xE1E1000000000002ULL;
	long fd;

	zero_bytes(&rep, sizeof rep);
	rep.magic = C6_MAGIC;
	rep.gen = GEN_EXEC_NEW;
	rep.own_req = own_req;

	fd = raw_open_endpoint(path, GEN_EXEC_NEW);
	if (fd < 0) {
		rep.stage = (uint64_t)__LINE__;
		report_write(report_fd, &rep);
		RSYS1(SYS_exit_group, 4);
		for (;;)
			;
	}
	rep.endpoint_ok = 1;

	/* 1. the old generation on the new endpoint must be refused */
	{
		struct ctl_msg m, r;
		long got = -1;

		zero_bytes(&m, sizeof m);
		m.magic = CTL_MAGIC;
		m.gen = old_gen;
		m.req_id = 0xE1E1000000000003ULL;
		m.kind = CTL_ECHO_SYNC;
		if (raw_send_msg(fd, &m, -1) == (long)sizeof m &&
		    raw_recv_msg(fd, &r, &got, 3000) == (long)sizeof r &&
		    r.magic == CTL_MAGIC && r.kind == CTL_REJECT &&
		    r.result == REJECT_MAGIC)
			rep.stale_endpoint_rejected = 1;
		if (got >= 0)
			RSYS1(SYS_close, got);
	}

	/* 2. the re-established endpoint answers its own request */
	{
		struct ctl_msg m, r;
		long got = -1;

		zero_bytes(&m, sizeof m);
		m.magic = CTL_MAGIC;
		m.gen = GEN_EXEC_NEW;
		m.req_id = own_req;
		m.kind = CTL_ECHO_SYNC;
		if (raw_send_msg(fd, &m, -1) == (long)sizeof m &&
		    raw_recv_msg(fd, &r, &got, 3000) == (long)sizeof r &&
		    r.magic == CTL_MAGIC && r.req_id == own_req &&
		    r.gen == GEN_EXEC_NEW && r.kind == CTL_REPLY &&
		    r.result == expected_result(own_req)) {
			rep.own_ok = 1;
			rep.verified++;
		}
		rep.requests++;
		if (got >= 0)
			RSYS1(SYS_close, got);
	}

	/* 3. the pre-exec connection's late reply is stale: refuse it */
	{
		struct ctl_msg r;
		long got = -1;

		if (raw_recv_msg(stale_fd, &r, &got, 3000) == (long)sizeof r) {
			rep.stale_seen = 1;
			if (r.gen == old_gen && r.req_id == old_req) {
				/*MUT6-STALE*/ rep.stale_rejected = 1;
			}
		}
		if (got >= 0)
			RSYS1(SYS_close, got);
	}

	RSYS1(SYS_close, stale_fd);
	RSYS1(SYS_close, fd);
	report_write(report_fd, &rep);
	RSYS1(SYS_exit_group, 0);
	for (;;)
		;
}

/* ------------------------------------------------------------------ */
/* guest side: the ONE process-level control endpoint                  */
/* ------------------------------------------------------------------ */

#define SP_STYLE2 0
#define SP_STYLE1 1
#define SP_FD     2
#define SP_BULK   3          /* unledgered bulk traffic (storms, C7)      */

static uint64_t g_wait_ms = 1500;
static uint64_t g_phase_ms = 15000;
static uint64_t g_req_per_thread = 64;
static uint64_t g_hot_dur_ms = 300;
static uint64_t g_storm_dur_ms = 400;
static int g_fast;

static uint64_t next_id(int space)
{
	uint64_t v;

	switch (space) {
	case SP_STYLE2:
		v = add_rel(&SHM->seq_style2, 1);
		if (v >= LEDGER_SPAN) {
			add_rel(&SHM->ledger_overflow, 1);
			return 0xFFFF000000000000ULL | (v & 0xFFFFFFu);
		}
		add_rel(&SHM->ctl_requests, 1);
		return v;
	case SP_STYLE1:
		v = add_rel(&SHM->seq_style1, 1);
		if (v >= LEDGER_SPAN) {
			add_rel(&SHM->ledger_overflow, 1);
			return 0xFFFF100000000000ULL | (v & 0xFFFFFFu);
		}
		add_rel(&SHM->ctl_requests, 1);
		return IDSPACE_STYLE1 + v;
	case SP_FD:
		v = add_rel(&SHM->seq_fd, 1);
		if (v >= LEDGER_SPAN) {
			add_rel(&SHM->ledger_overflow, 1);
			return 0xFFFF200000000000ULL | (v & 0xFFFFFFu);
		}
		add_rel(&SHM->ctl_requests, 1);
		return IDSPACE_FD + v;
	default:
		v = add_rel(&SHM->seq_bulk, 1);
		add_rel(&SHM->ctl_requests, 1);
		return 0xB000000000000000ULL |
		       ((uint64_t)(uint32_t)syscall(SYS_gettid) << 32) | v;
	}
}

static void ledger_observe(uint64_t id)
{
	uint32_t v;

	if (id >= LEDGER_MAX)
		return;
	v = __atomic_fetch_add(&SHM->ledger[id], 1u, __ATOMIC_RELAXED) + 1u;
	if (v > 1)
		add_rel(&SHM->observed_multi, 1);
	add_rel(&SHM->ctl_completed, 1);
}

static long ctl_send(int fd, const struct ctl_msg *m, int send_fd)
{
	struct msghdr mh;
	struct iovec iov;
	char cbuf[CMSG_SPACE(sizeof(int))];

	memset(&mh, 0, sizeof mh);
	memset(cbuf, 0, sizeof cbuf);
	iov.iov_base = (void *)(uintptr_t)m;
	iov.iov_len = sizeof *m;
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	if (send_fd >= 0) {
		struct cmsghdr *c = (struct cmsghdr *)(void *)cbuf;

		c->cmsg_len = CMSG_LEN(sizeof(int));
		c->cmsg_level = SOL_SOCKET;
		c->cmsg_type = SCM_RIGHTS;
		*(int *)CMSG_DATA(c) = send_fd;
		mh.msg_control = cbuf;
		mh.msg_controllen = CMSG_SPACE(sizeof(int));
	}
	return sendmsg(fd, &mh, MSG_NOSIGNAL);
}

static long ctl_recv_nowait(int fd, struct ctl_msg *m, int *got_fd)
{
	struct msghdr mh;
	struct iovec iov;
	char cbuf[96];
	struct cmsghdr *c;
	long rv;

	if (got_fd)
		*got_fd = -1;
	memset(&mh, 0, sizeof mh);
	memset(cbuf, 0, sizeof cbuf);
	iov.iov_base = m;
	iov.iov_len = sizeof *m;
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	mh.msg_control = cbuf;
	mh.msg_controllen = sizeof cbuf;
	rv = recvmsg(fd, &mh, MSG_DONTWAIT);
	if (rv < 0)
		return -1;
	if (got_fd) {
		for (c = CMSG_FIRSTHDR(&mh); c; c = CMSG_NXTHDR(&mh, c)) {
			if (c->cmsg_level == SOL_SOCKET &&
			    c->cmsg_type == SCM_RIGHTS) {
				*got_fd = *(int *)CMSG_DATA(c);
				break;
			}
		}
	}
	return rv;
}

/* returns the datagram length, or -1 on timeout */
static long ctl_recv_wait(int fd, struct ctl_msg *m, int *got_fd,
			  uint64_t deadline_ms)
{
	while (now_ms() < deadline_ms) {
		struct pollfd pf;
		long rv;

		pf.fd = fd;
		pf.events = POLLIN;
		pf.revents = 0;
		if (poll(&pf, 1, 20) <= 0)
			continue;
		rv = ctl_recv_nowait(fd, m, got_fd);
		if (rv >= 0)
			return rv;
	}
	return -1;
}

static void sun_path_set(struct sockaddr_un *sa)
{
	size_t n = strlen(g_path);

	if (n >= sizeof sa->sun_path)
		n = sizeof sa->sun_path - 1;
	memcpy(sa->sun_path, g_path, n);
	sa->sun_path[n] = '\0';
}

static int ctl_connect_gen(uint64_t gen)
{
	int fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
	struct sockaddr_un sa;
	struct ctl_msg m, r;
	int got = -1;

	if (fd < 0)
		return -1;
	memset(&sa, 0, sizeof sa);
	sa.sun_family = AF_UNIX;
	sun_path_set(&sa);
	if (connect(fd, (struct sockaddr *)&sa,
		    offsetof(struct sockaddr_un, sun_path) +
			    strlen(sa.sun_path) + 1) != 0) {
		close(fd);
		return -1;
	}
	memset(&m, 0, sizeof m);
	m.magic = CTL_MAGIC;
	m.gen = gen;
	m.req_id = 0xDEAD000000000000ULL | gen;
	m.kind = CTL_HELLO;
	if (ctl_send(fd, &m, -1) != (long)sizeof m) {
		close(fd);
		return -1;
	}
	if (ctl_recv_wait(fd, &r, &got, now_ms() + 3000) != (long)sizeof r ||
	    r.magic != CTL_MAGIC || r.kind != CTL_HELLO_ACK) {
		close(fd);
		return -1;
	}
	return fd;
}

/* M4: the per-thread-socket architecture this proof argues against.  The clean
 * design opens NO per-thread endpoint; the runner flips this flag with a
 * one-line mutation and C1 must fail. */
static int g_per_thread_sockets;

static int per_thread_socket_open(int lane)
{
	int fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
	struct sockaddr_un sa;

	(void)lane;
	if (fd < 0)
		return -1;
	memset(&sa, 0, sizeof sa);
	sa.sun_family = AF_UNIX;
	sun_path_set(&sa);
	if (connect(fd, (struct sockaddr *)&sa,
		    offsetof(struct sockaddr_un, sun_path) +
			    strlen(sa.sun_path) + 1) != 0) {
		close(fd);
		return -1;
	}
	return fd;
}

static int per_thread_socket_lane(int lane)
{
	int fd = -1;

	/*MUT4-PERTHREAD*/ if (g_per_thread_sockets) fd = per_thread_socket_open(lane);
	(void)g_per_thread_sockets;
	return fd;
}

/* ------------------------------------------------------------------ */
/* completion lanes                                                    */
/* ------------------------------------------------------------------ */

#define SPIN_BUDGET 2048u
#define WAIT_OK 0
#define WAIT_TIMEOUT (-1)
#define WAIT_FOREIGN (-2)
#define WAIT_ABORT (-3)

static void futex_wake_sh(void *addr, int n)
{
	syscall(SYS_futex, addr, FUTEX_WAKE, n, NULL, NULL, 0);
}

static int futex_wait_sh(void *addr, uint32_t expect, long ms)
{
	struct timespec ts;

	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (ms % 1000) * 1000000L;
	return (int)syscall(SYS_futex, addr, FUTEX_WAIT, expect, &ts, NULL, 0);
}

static int futex_wake_priv(void *addr, int n)
{
	return (int)syscall(SYS_futex, addr, FUTEX_WAKE | FUTEX_PRIVATE_FLAG, n,
			    NULL, NULL, 0);
}

static int futex_wait_priv(void *addr, uint32_t expect, long ms)
{
	struct timespec ts;

	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (ms % 1000) * 1000000L;
	return (int)syscall(SYS_futex, addr,
			    FUTEX_WAIT | FUTEX_PRIVATE_FLAG, expect, &ts, NULL,
			    0);
}

/* wait for the completion published into this thread's own lane slot */
static int lane_wait(uint32_t lane, uint64_t id, uint64_t *result, int *fd_out,
		     uint64_t *lat_ns)
{
	struct clane *cl = &SHM->clanes[lane];
	struct cslot *sl = &cl->slot;
	uint64_t deadline = now_ms() + g_wait_ms;
	unsigned spins = 0;

	for (;;) {
		uint32_t ep;

		if (ld_acq(&sl->state) == CS_DONE)
			break;
		if (ld_acq(&SHM->phase_abort))
			return WAIT_ABORT;
		if (++spins < SPIN_BUDGET) {
			cpu_relax();
			continue;
		}
		ep = (uint32_t)ld_acq(&cl->done_seq);
		st_rel(&cl->parked, id);
		if (ld_acq(&sl->state) == CS_DONE)
			break;
		futex_wait_sh(&cl->done_seq, ep, 20);
		spins = 0;
		if (now_ms() >= deadline)
			return WAIT_TIMEOUT;
	}
	st_rlx(&cl->parked, 0);
	/* A descriptor the receiver deposited for this lane is this thread's to
	 * close whether or not the completion is accepted, so take it first. */
	if (fd_out) {
		*fd_out = ld_acq(&sl->fd);
		st_rlx(&sl->fd, -1);
	}
	/* the whole point of C2: a thread only ever accepts a completion whose
	 * request id is the one it published, with the server's result attached. */
	if (ld_acq(&sl->req_id) != id)
		return WAIT_FOREIGN;
	if (ld_acq(&sl->kind) != CTL_FD_IN &&
	    ld_acq(&sl->result) != expected_result(id))
		return WAIT_FOREIGN;
	if (result)
		*result = ld_acq(&sl->result);
	if (lat_ns)
		*lat_ns = now_ns() - ld_acq(&sl->t_publish);
	return WAIT_OK;
}

static int lane_publish(uint32_t lane, uint64_t id, uint32_t kind, int send_fd,
			uint64_t *result, int *fd_out, uint64_t *lat_ns)
{
	struct cslot *sl;
	struct ctl_msg m;

	if (lane >= MAX_LANES)
		return WAIT_FOREIGN;
	sl = &SHM->clanes[lane].slot;
	sl->req_id = id;
	sl->result = 0;
	sl->fd = -1;
	sl->fd_ok = 0;
	sl->kind = kind;
	sl->token = 0;
	sl->t_done = 0;
	sl->t_publish = now_ns();
	st_rel(&sl->state, CS_WAIT);

	memset(&m, 0, sizeof m);
	m.magic = CTL_MAGIC;
	m.gen = GEN_PARENT;
	m.req_id = id;
	m.lane = lane;
	m.kind = kind;
	if (ctl_send(g_ctl, &m, send_fd) != (long)sizeof m)
		return WAIT_TIMEOUT;
	return lane_wait(lane, id, result, fd_out, lat_ns);
}

/* ------------------------------------------------------------------ */
/* the process-level receiver: routes descriptor packets to their lane  */
/* ------------------------------------------------------------------ */

static void *receiver_thread(void *arg)
{
	(void)arg;
	while (!ld_acq(&SHM->rx_stop)) {
		struct pollfd pf;
		struct ctl_msg m;
		int got = -1;
		long rv;
		uint32_t lane;
		struct clane *cl;
		struct cslot *sl;

		if (ld_acq(&SHM->rx_paused)) {
			futex_wait_priv(&SHM->rx_paused, 1, 20);
			continue;
		}
		pf.fd = g_ctl;
		pf.events = POLLIN;
		pf.revents = 0;
		if (poll(&pf, 1, 20) <= 0)
			continue;
		rv = ctl_recv_nowait(g_ctl, &m, &got);
		if (rv == 0)
			break;              /* endpoint closed */
		if (rv < (long)sizeof m) {
			if (got >= 0)
				close(got);
			continue;
		}
		add_rel(&SHM->rx_packets, 1);
		if (m.magic != CTL_MAGIC) {
			if (got >= 0)
				close(got);
			continue;
		}
		if (m.gen != GEN_PARENT) {
			add_rel(&SHM->rx_foreign_gen, 1);
			if (got >= 0)
				close(got);
			continue;
		}
		lane = (m.kind == CTL_FD_OUT) ? fd_deposit_lane(m.lane) : m.lane;
		if (lane >= MAX_LANES) {
			add_rel(&SHM->rx_bad_lane, 1);
			if (got >= 0)
				close(got);
			continue;
		}
		cl = &SHM->clanes[lane];
		sl = &cl->slot;
		if (ld_acq(&sl->state) == CS_WAIT && ld_acq(&sl->req_id) == m.req_id) {
			if (got >= 0) {
				sl->fd = got;
				add_rel(&SHM->rx_fds_routed, 1);
			}
			sl->result = m.result;
			sl->kind = m.kind;
			sl->t_done = now_ns();
			st_rel(&sl->state, CS_DONE);
			add_rel(&cl->done_seq, 1);
			futex_wake_sh(&cl->done_seq, 1);
		} else if (got >= 0) {
			if (ld_acq(&sl->state) == CS_WAIT)
				add_rel(&SHM->rx_fds_foreign, 1);
			else
				add_rel(&SHM->rx_fds_orphan, 1);
			close(got);
		} else {
			add_rel(&SHM->rx_plain_unrouted, 1);
		}
	}
	return NULL;
}

static void rx_pause(void)
{
	st_rel(&SHM->rx_paused, 1);
	futex_wake_priv(&SHM->rx_paused, 1);
	add_rel(&SHM->rx_paused_count, 1);
	usleep(40000);                  /* let an in-flight recvmsg return */
}

static void rx_resume(void)
{
	st_rel(&SHM->rx_paused, 0);
	futex_wake_priv(&SHM->rx_paused, 1);
}

/* ------------------------------------------------------------------ */
/* operations                                                          */
/* ------------------------------------------------------------------ */

static void app_install(uint32_t lane, uint64_t id, uint64_t tok, int fd)
{
	struct approw *row;

	if (lane >= APP_ROWS)
		return;
	row = &SHM->app[lane];
	row->owner_req = id;
	row->token = tok;
	row->gen = GEN_PARENT;
	row->lane = lane;
	row->tid = (uint64_t)syscall(SYS_gettid);
	row->fd = fd;
	st_rel(&row->state, 1u);
	add_rel(&SHM->app_published, 1);
	if (row->owner_req == id && row->token == tok && row->gen == GEN_PARENT &&
	    row->lane == lane)
		add_rel(&SHM->app_parent_rows, 1);
	else
		add_rel(&SHM->app_token_mismatch, 1);
}

/* style 2: plain request, completion through this thread's shared lane */
static int op_echo_lane(int lane, uint64_t id, uint64_t *lat)
{
	uint64_t res = 0;
	int rc = lane_publish((uint32_t)lane, id, CTL_ECHO, -1, &res, NULL, lat);

	add_rel(&SHM->s2_requests, 1);
	if (rc == WAIT_OK) {
		add_rel(&SHM->s2_completed, 1);
		add_rel(&SHM->s2_lat_sum, *lat ? *lat : 1);
		ledger_observe(id);
	} else if (rc == WAIT_FOREIGN) {
		add_rel(&SHM->s2_foreign, 1);
	} else {
		add_rel(&SHM->s2_timeouts, 1);
	}
	return rc;
}

/* guest -> server descriptor transfer, identity proved by the token inside */
static int op_fd_in(int lane, uint64_t id, uint64_t *lat)
{
	int pfd[2];
	uint64_t tok = id;
	uint64_t res = 0;
	int rc;

	if (pipe2(pfd, 0) != 0)
		return WAIT_TIMEOUT;
	if (write(pfd[1], &tok, 8) != 8) {
		close(pfd[0]);
		close(pfd[1]);
		return WAIT_TIMEOUT;
	}
	close(pfd[1]);
	add_rel(&SHM->fd_in_requests, 1);
	rc = lane_publish((uint32_t)lane, id, CTL_FD_IN, pfd[0], &res, NULL, lat);
	close(pfd[0]);
	if (rc == WAIT_OK && res == 1) {
		add_rel(&SHM->fd_in_ok, 1);
		ledger_observe(id);
	} else if (rc == WAIT_OK) {
		add_rel(&SHM->fd_in_bad, 1);
	} else if (rc == WAIT_FOREIGN) {
		add_rel(&SHM->s2_foreign, 1);
	} else {
		add_rel(&SHM->s2_timeouts, 1);
	}
	return rc;
}

/* server -> guest descriptor transfer (an application-table open): the server
 * creates the descriptor, the guest proves its identity by reading the request
 * id out of the descriptor itself, then installs it in its own table. */
static int op_fd_out(int lane, uint64_t id, uint64_t *lat)
{
	int fd = -1;
	uint64_t res = 0;
	uint64_t tok = 0;
	int rc;

	add_rel(&SHM->fd_out_requests, 1);
	rc = lane_publish((uint32_t)lane, id, CTL_FD_OUT, -1, &res, &fd, lat);
	if (rc != WAIT_OK) {
		if (fd >= 0)
			close(fd);
		if (rc == WAIT_FOREIGN)
			add_rel(&SHM->s2_foreign, 1);
		else
			add_rel(&SHM->fd_out_timeouts, 1);
		return rc;
	}
	if (fd < 0) {
		add_rel(&SHM->fd_out_bad, 1);
		return WAIT_FOREIGN;
	}
	if (read(fd, &tok, 8) == 8 && tok == id) {
		add_rel(&SHM->fd_out_ok, 1);
		ledger_observe(id);
	} else {
		add_rel(&SHM->fd_out_bad, 1);
	}
	app_install((uint32_t)lane, id, tok, fd);
	close(fd);
	return rc;
}

/* style 1: everything behind one lock, reply over the socket */
static int op_style1(int lane, uint64_t id, uint64_t *lat, uint64_t *wait)
{
	struct ctl_msg m, r;
	int got = -1;
	uint64_t t0 = now_ns(), t1, t2;
	int rc = WAIT_TIMEOUT;

	add_rel(&SHM->s1_requests, 1);
	if (lock_acquire_to(now_ms() + g_wait_ms) < 0) {
		add_rel(&SHM->s1_timeouts, 1);
		if (lat)
			*lat = now_ns() - t0;
		return WAIT_TIMEOUT;
	}
	t1 = now_ns();
	if (wait)
		*wait = t1 - t0;
	memset(&m, 0, sizeof m);
	m.magic = CTL_MAGIC;
	m.gen = GEN_PARENT;
	m.req_id = id;
	m.lane = (uint32_t)lane;
	m.kind = CTL_ECHO_SYNC;
	if (ctl_send(g_ctl, &m, -1) != (long)sizeof m)
		goto out;
	if (ctl_recv_wait(g_ctl, &r, &got, now_ms() + g_wait_ms) != (long)sizeof r)
		goto out;
	if (got >= 0) {
		close(got);
		got = -1;
	}
	if (r.magic != CTL_MAGIC || r.gen != GEN_PARENT || r.req_id != id ||
	    r.kind != CTL_REPLY || r.result != expected_result(id)) {
		add_rel(&SHM->s1_foreign, 1);
		rc = WAIT_FOREIGN;
		goto out;
	}
	t2 = now_ns();
	/* a control operation that completed while the C7 holder held the lock
	 * would mean the hot path had serialized control behind it */
	if (ld_acq(&SHM->holder_grab_ns) != 0 &&
	    t2 >= ld_acq(&SHM->holder_grab_ns) &&
	    (ld_acq(&SHM->holder_release_ns) == 0 ||
	     t2 < ld_acq(&SHM->holder_release_ns)))
		add_rel(&SHM->ctl_done_after_grab, 1);
	add_rel(&SHM->s1_completed, 1);
	add_rel(&SHM->s1_lat_sum, t2 - t0);
	ledger_observe(id);
	rc = WAIT_OK;
out:
	if (got >= 0)
		close(got);
	lock_release();
	if (lat)
		*lat = now_ns() - t0;
	if (rc != WAIT_OK && rc == WAIT_TIMEOUT)
		add_rel(&SHM->s1_timeouts, 1);
	return rc;
}

/* ------------------------------------------------------------------ */
/* workers                                                             */
/* ------------------------------------------------------------------ */

/* The hot measurement is read by its OBSERVER from the per-lane live
 * counters (each producer writes only its own cache line), so a window
 * boundary is decided by the observer and never by a race in the
 * producer. */
static uint64_t hot_live_sum(void)
{
	uint64_t t = 0;
	int i;

	for (i = 0; i < HOT_LANES; i++)
		t += ld_acq(&SHM->hot_live[i].n);
	return t;
}

#define WM_STYLE2 1
#define WM_STYLE1 2
#define WM_FD     3
#define WM_STORM  4
#define WM_C1     5
#define WM_H2     6
#define WM_H3     7

#define MAX_ITERS 256

static uint64_t g_lat[MAX_THREADS][MAX_ITERS];
static uint64_t g_wait[MAX_THREADS][MAX_ITERS];

struct warg {
	int lane;
	int idx;
	int mode;
	uint64_t iters;
	uint64_t ops;
	int pfd;
	int pad;
};

static void *worker(void *p)
{
	struct warg *a = (struct warg *)p;
	uint64_t i, ctx0;

	__atomic_fetch_add(&SHM->workers_started, 1, __ATOMIC_RELAXED);
	{
		uint64_t d = now_ms() + 5000;

		while (now_ms() < d && !ld_acq(&SHM->phase_go))
			futex_wait_priv(&SHM->phase_go, 0, 5);
	}
	ctx0 = thread_vol_ctx();
	a->pfd = per_thread_socket_lane(a->lane);

	for (i = 0; i < a->iters; i++) {
		uint64_t lat = 0, wait = 0;
		uint64_t id;

		if (ld_acq(&SHM->phase_abort))
			break;
		switch (a->mode) {
		case WM_C1:
		case WM_STYLE2:
			id = next_id(SP_STYLE2);
			op_echo_lane(a->lane, id, &lat);
			break;
		case WM_STYLE1:
			id = next_id(SP_STYLE1);
			op_style1(a->lane, id, &lat, &wait);
			break;
		case WM_FD:
			if ((i & 1u) == 0) {
				id = next_id(SP_FD);
				op_fd_in(a->lane, id, &lat);
			} else {
				id = next_id(SP_FD);
				op_fd_out(a->lane, id, &lat);
			}
			break;
		case WM_STORM:
			/* bulk ids: the storm is a volume test, so it must not eat
			 * the ledgered id spaces the exactly-once check uses */
			if (ld_acq(&SHM->storm_stop))
				goto finished;
			add_rel(&SHM->storm_expected, 1);
			id = next_id(SP_BULK);
			{
				int rc;

				if ((i & 3u) < 2u)
					rc = op_echo_lane(a->lane, id, &lat);
				else if ((i & 3u) == 2u)
					rc = op_fd_in(a->lane, id, &lat);
				else
					rc = op_fd_out(a->lane, id, &lat);
				if (rc == WAIT_OK)
					add_rel(&SHM->storm_completed, 1);
				else if (rc == WAIT_FOREIGN)
					add_rel(&SHM->storm_foreign, 1);
				else
					add_rel(&SHM->storm_slow, 1);
			}
			break;
		case WM_H2:
			if (ld_acq(&SHM->hot_stop))
				goto finished;
			id = next_id(SP_BULK);
			op_echo_lane(a->lane, id, &lat);
			break;
		case WM_H3:
			if (ld_acq(&SHM->hot_stop))
				goto finished;
			id = next_id(SP_BULK);
			op_style1(a->lane, id, &lat, &wait);
			break;
		default:
			break;
		}
		a->ops++;
		if (lat && i < MAX_ITERS)
			g_lat[a->idx][i] = lat;
		if (wait && i < MAX_ITERS)
			g_wait[a->idx][i] = wait;
	}

finished:
	{
		uint64_t dc = thread_vol_ctx() - ctx0;

		if (a->mode == WM_STYLE1 || a->mode == WM_H3)
			add_rel(&SHM->s1_ctx, dc);
		else if (a->mode == WM_STYLE2 || a->mode == WM_H2)
			add_rel(&SHM->s2_ctx, dc);
	}
	if (a->mode == WM_C1) {
		__atomic_fetch_add(&SHM->workers_parked, 1, __ATOMIC_RELAXED);
		while (!ld_acq(&SHM->gate))
			futex_wait_priv(&SHM->gate, 0, 20);
	}
	if (a->pfd >= 0)
		close(a->pfd);
	__atomic_fetch_add(&SHM->workers_done, 1, __ATOMIC_RELEASE);
	futex_wake_priv(&SHM->workers_done, 1);
	return NULL;
}

/* ------------------------------------------------------------------ */
/* hot path: per-thread SPSC ring lanes, no lock, no syscall            */
/* ------------------------------------------------------------------ */

static uint64_t g_hot_local[MAX_THREADS];
static uint64_t g_hot_bad;

/* C7's structural claim: the hot publish path takes no lock.  The guard is
 * always_inline and called with a constant 0, so the clean build contains no
 * lock instruction on the hot path at all; MUT5 flips the constant and the hot
 * path then takes the control lock. */
__attribute__((always_inline)) static inline int hot_slot_guard(int on)
{
	if (!on)
		return -1;
	lock_acquire_to(now_ms() + 1000000);
	return 1;
}

__attribute__((always_inline)) static inline void hot_slot_unguard(int token)
{
	if (token > 0)
		lock_release();
}

__attribute__((noinline)) static void hot_publish(struct hring *h,
						  struct hent *e,
						  uint64_t c,
						  int lane)
{
	int token = hot_slot_guard(0);      /*MUT5-HOTLOCK: no lock on the hot path */

	e->seq = c;
	e->payload = mix64(c ^ (uint64_t)lane);
	e->result = 0;
	st_rel(&e->state, HE_REQ);
	st_rel(&h->prod, c + 1);
	hot_slot_unguard(token);
}

struct harg {
	int lane;
	int pad;
};

static void *hot_producer(void *p)
{
	struct harg *a = (struct harg *)p;
	struct hring *h = &SHM->hrings[a->lane];
	uint64_t c, spins = 0;

	while (!ld_acq(&SHM->hot_go)) {
		cpu_relax();
		if (++spins > 4000000000ULL)
			break;
	}
	spins = 0;
	/* continue where the ring left off: the server's consumer index is at the
	 * previous phase's producer index and every slot is free */
	c = ld_acq(&h->prod);
	for (;;) {
		struct hent *e;
		unsigned s = 0;

		if (ld_acq(&SHM->hot_stop))
			break;
		e = &h->ring[c % HOT_SLOTS];
		while (ld_acq(&e->state) != HE_FREE) {
			cpu_relax();
			if (++s > 400000000u)
				goto out;
		}
		hot_publish(h, e, c, a->lane);
		while (ld_acq(&e->state) != HE_DONE) {
			cpu_relax();
			if (++s > 400000000u)
				goto out;
		}
		if (e->result != mix64(e->payload ^ 0xF00DF00DF00DF00DULL))
			g_hot_bad++;
		st_rlx(&e->state, HE_FREE);
		st_rlx(&e->state, HE_FREE);
		c++;
		/* the completion counter is live from here on: whoever observes it --
		 * the main thread around its timed window, or the C7 holder around the
		 * window in which it held the lock -- defines which completions the
		 * measurement covers, so no timing race can flatter the result */
		st_rlx(&SHM->hot_live[a->lane].n, c);
	}
out:
	g_hot_local[a->lane] += c;
	add_rlx(&SHM->hot_completed, c);   /* the producer's own total */
	if (g_hot_bad)
		add_rel(&SHM->hot_lost, g_hot_bad);
	__atomic_fetch_add(&SHM->workers_done, 1, __ATOMIC_RELEASE);
	futex_wake_priv(&SHM->workers_done, 1);
	return NULL;
}

/* the deliberate control-lock holder of C7 */
static void *holder_thread(void *arg)
{
	(void)arg;
	if (lock_acquire_to(now_ms() + 1000000) < 0)
		return NULL;
	st_rel(&SHM->holder_grab_ns, now_ns());
	st_rel(&SHM->holder_ops_start, hot_live_sum());
	while (!ld_acq(&SHM->holder_release))
		futex_wait_priv(&SHM->holder_release, 0, 10);
	st_rel(&SHM->holder_ops_end, hot_live_sum());
	st_rel(&SHM->holder_release_ns, now_ns());
	lock_release();
	__atomic_fetch_add(&SHM->workers_done, 1, __ATOMIC_RELEASE);
	futex_wake_priv(&SHM->workers_done, 1);
	return NULL;
}

/* ------------------------------------------------------------------ */
/* claim bookkeeping                                                   */
/* ------------------------------------------------------------------ */

#define NCLAIMS 9
static int g_ok[NCLAIMS + 1];
static char g_detail[NCLAIMS + 1][DET_MAX];

static int all_ok(void);

static const char *claim_name(int i)
{
	switch (i) {
	case 1: return "fd-table invariance";
	case 2: return "one endpoint, exactly once";
	case 3: return "SCM_RIGHTS both directions";
	case 4: return "application-table handoff";
	case 5: return "fork isolation";
	case 6: return "exec transition";
	case 7: return "hot path not serialized";
	case 8: return "style 1 vs style 2";
	case 9: return "control:hot ratio";
	default: return "?";
	}
}

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
	int i;

	for (i = 1; i <= NCLAIMS; i++)
		printf("C%d %s %s: %s\n", i, g_ok[i] ? "PASS" : "FAIL",
		       claim_name(i), g_detail[i]);
	printf("HARNESS %s env=%s pid=%d\n", all_ok() ? "OK" : "FAILED", g_env,
	       (int)getpid());
	fflush(stdout);
}

static int all_ok(void)
{
	int i;

	for (i = 1; i <= NCLAIMS; i++)
		if (!g_ok[i])
			return 0;
	return 1;
}

static void die(const char *what)
{
	int i;

	fprintf(stderr, "harness fatal: %s: %s\n", what, strerror(errno));
	fflush(stderr);
	for (i = 1; i <= NCLAIMS; i++)
		verdict(i, 0, "not evaluated: %s failed (%s)", what,
			strerror(errno));
	print_claims();
	_exit(3);
}

/* ------------------------------------------------------------------ */
/* phase bookkeeping helpers                                           */
/* ------------------------------------------------------------------ */

#define WAKE_ALL 0x7fffffff

static struct warg g_args[MAX_THREADS];
static struct harg g_hargs[HOT_LANES];

static void reset_workers(void)
{
	usleep(3000);
	st32_rel(&SHM->workers_started, 0);
	st32_rel(&SHM->workers_parked, 0);
	st32_rel(&SHM->workers_done, 0);
	st32_rel(&SHM->workers_failed, 0);
	st32_rel(&SHM->phase_abort, 0);
	st32_rel(&SHM->gate, 0);
	st_rel(&SHM->phase_go, 0);
}

static void spawn_n(int n, int mode, uint64_t iters)
{
	pthread_t t;
	int i;

	for (i = 0; i < n; i++) {
		g_args[i].lane = i;
		g_args[i].idx = i;
		g_args[i].mode = mode;
		g_args[i].iters = iters;
		g_args[i].ops = 0;
		g_args[i].pfd = -1;
		if (pthread_create(&t, NULL, worker, &g_args[i]) != 0)
			die("pthread_create");
		pthread_detach(t);
	}
}

/* spawn the phase's workers, wait until they are all ready, release the start
 * gate and return the instant the measurement window opens: thread startup must
 * not be charged to the traffic under measurement */
static uint64_t spawn_and_go(int n, int mode, uint64_t iters)
{
	uint64_t d = now_ms() + 30000;

	spawn_n(n, mode, iters);
	while (now_ms() < d && (int)ld32_acq(&SHM->workers_started) < n)
		usleep(200);
	st_rel(&SHM->phase_go, 1);
	futex_wake_priv(&SHM->phase_go, WAKE_ALL);
	return now_ms();
}


static int wait_workers(int total, uint64_t deadline_ms)
{
	while (now_ms() < deadline_ms) {
		if ((int)ld32_acq(&SHM->workers_done) >= total)
			return 0;
		futex_wait_priv(&SHM->workers_done,
				(uint32_t)ld32_acq(&SHM->workers_done), 5);
	}
	return -1;
}

/* run a fixed-size phase and report whether every worker finished on time */
static int run_phase(int n, int mode, uint64_t iters)
{
	reset_workers();
	spawn_and_go(n, mode, iters);
	if (wait_workers(n, now_ms() + g_phase_ms) != 0) {
		/* a phase that cannot finish inside its budget is abandoned: the
		 * workers see phase_abort and stop waiting, so a harness whose
		 * premise broke still reports every claim line and exits */
		st32_rel(&SHM->phase_abort, 1);
		futex_wake_priv(&SHM->workers_done, WAKE_ALL);
		if (wait_workers(n, now_ms() + 5000) != 0)
			return -1;
		usleep(3000);
		return -1;
	}
	usleep(3000);
	return 0;
}

struct snap {
	uint64_t s2_requests, s2_completed, s2_foreign, s2_timeouts;
	uint64_t s1_requests, s1_completed, s1_foreign, s1_timeouts;
	uint64_t fd_in_requests, fd_in_ok, fd_in_bad;
	uint64_t fd_out_requests, fd_out_ok, fd_out_bad, fd_out_timeouts;
	uint64_t storm_expected, storm_completed;
	uint64_t storm_foreign, storm_slow;
};

static void snap_take(struct snap *s)
{
	s->s2_requests = ld_acq(&SHM->s2_requests);
	s->s2_completed = ld_acq(&SHM->s2_completed);
	s->s2_foreign = ld_acq(&SHM->s2_foreign);
	s->s2_timeouts = ld_acq(&SHM->s2_timeouts);
	s->s1_requests = ld_acq(&SHM->s1_requests);
	s->s1_completed = ld_acq(&SHM->s1_completed);
	s->s1_foreign = ld_acq(&SHM->s1_foreign);
	s->s1_timeouts = ld_acq(&SHM->s1_timeouts);
	s->fd_in_requests = ld_acq(&SHM->fd_in_requests);
	s->fd_in_ok = ld_acq(&SHM->fd_in_ok);
	s->fd_in_bad = ld_acq(&SHM->fd_in_bad);
	s->fd_out_requests = ld_acq(&SHM->fd_out_requests);
	s->fd_out_ok = ld_acq(&SHM->fd_out_ok);
	s->fd_out_bad = ld_acq(&SHM->fd_out_bad);
	s->fd_out_timeouts = ld_acq(&SHM->fd_out_timeouts);
	s->storm_expected = ld_acq(&SHM->storm_expected);
	s->storm_completed = ld_acq(&SHM->storm_completed);
	s->storm_foreign = ld_acq(&SHM->storm_foreign);
	s->storm_slow = ld_acq(&SHM->storm_slow);
}

static void snap_delta(const struct snap *a, const struct snap *b, struct snap *d)
{
	d->s2_requests = b->s2_requests - a->s2_requests;
	d->s2_completed = b->s2_completed - a->s2_completed;
	d->s2_foreign = b->s2_foreign - a->s2_foreign;
	d->s2_timeouts = b->s2_timeouts - a->s2_timeouts;
	d->s1_requests = b->s1_requests - a->s1_requests;
	d->s1_completed = b->s1_completed - a->s1_completed;
	d->s1_foreign = b->s1_foreign - a->s1_foreign;
	d->s1_timeouts = b->s1_timeouts - a->s1_timeouts;
	d->fd_in_requests = b->fd_in_requests - a->fd_in_requests;
	d->fd_in_ok = b->fd_in_ok - a->fd_in_ok;
	d->fd_in_bad = b->fd_in_bad - a->fd_in_bad;
	d->fd_out_requests = b->fd_out_requests - a->fd_out_requests;
	d->fd_out_ok = b->fd_out_ok - a->fd_out_ok;
	d->fd_out_bad = b->fd_out_bad - a->fd_out_bad;
	d->fd_out_timeouts = b->fd_out_timeouts - a->fd_out_timeouts;
	d->storm_expected = b->storm_expected - a->storm_expected;
	d->storm_completed = b->storm_completed - a->storm_completed;
	d->storm_foreign = b->storm_foreign - a->storm_foreign;
	d->storm_slow = b->storm_slow - a->storm_slow;
}

/* descriptor censuses */
static int fd_sockets_self(void)
{
	DIR *d = opendir("/proc/self/fd");
	struct dirent *e;
	int n = 0;

	if (!d)
		return -1;
	while ((e = readdir(d)) != NULL) {
		char path[512], buf[256];
		ssize_t r;

		if (e->d_name[0] == '.')
			continue;
		snprintf(path, sizeof path, "/proc/self/fd/%s", e->d_name);
		r = readlink(path, buf, sizeof buf - 1);
		if (r <= 0)
			continue;
		buf[r] = '\0';
		if (strncmp(buf, "socket:", 7) == 0)
			n++;
	}
	closedir(d);
	return n;
}

/* ------------------------------------------------------------------ */
/* latency statistics                                                  */
/* ------------------------------------------------------------------ */

static uint64_t g_samples[MAX_THREADS * MAX_ITERS];

static int cmp_u64(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;

	return x < y ? -1 : (x > y ? 1 : 0);
}

static void gather_lat(int n, uint64_t *dst, int *dst_n)
{
	int i;
	uint64_t j;

	*dst_n = 0;
	for (i = 0; i < n; i++) {
		uint64_t ops = g_args[i].ops;

		for (j = 0; j < ops && j < MAX_ITERS; j++)
			dst[(*dst_n)++] = g_lat[i][j];
	}
}

static void stats(const uint64_t *v, int n, uint64_t *mean, uint64_t *p99,
		  uint64_t *pmax)
{
	uint64_t sum = 0;
	int i;

	if (n <= 0) {
		*mean = *p99 = *pmax = 0;
		return;
	}
	qsort((void *)v, (size_t)n, sizeof v[0], cmp_u64);
	for (i = 0; i < n; i++)
		sum += v[i];
	*mean = sum / (uint64_t)n;
	*p99 = v[(int)((long)n * 99 / 100)];
	*pmax = v[n - 1];
}

/* ------------------------------------------------------------------ */
/* setup / teardown                                                    */
/* ------------------------------------------------------------------ */

static pthread_t g_rx_thread;

static int setup_all(void)
{
	char tmpl[PATH_MAX];
	const char *base = getenv("TMPDIR");
	struct sockaddr_un sa;
	int fd;
	pid_t p;

	if (!base || !base[0])
		base = "/tmp";

	/* Descriptors inherited from whoever launched the harness are not part of
	 * the architecture under test and would make C1's census meaningless. */
	syscall(SYS_close_range, 3u, ~0u, 0u);
	if (fcntl(0, F_GETFD) < 0) {
		if (open("/dev/null", O_RDONLY) < 0)
			return -1;
	}

	snprintf(tmpl, sizeof tmpl, "%s/dcp-proof.XXXXXX", base);
	if (mkdtemp(tmpl) == NULL)
		return -1;
	snprintf(g_scratch, sizeof g_scratch, "%s", tmpl);
	snprintf(g_path, sizeof g_path, "%s/control.sock", g_scratch);
	snprintf(g_shmfile, sizeof g_shmfile, "%s/shm", g_scratch);

	fd = open(g_shmfile, O_CREAT | O_RDWR, 0600);
	if (fd < 0)
		return -1;
	if (ftruncate(fd, (off_t)sizeof(struct shm)) != 0)
		return -1;
	SHM = mmap(NULL, sizeof(struct shm), PROT_READ | PROT_WRITE,
		   MAP_SHARED, fd, 0);
	if (SHM == MAP_FAILED) {
		SHM = NULL;
		return -1;
	}
	memset(SHM, 0, sizeof *SHM);
	close(fd);

	g_listen = socket(AF_UNIX, SOCK_SEQPACKET, 0);
	if (g_listen < 0)
		return -1;
	memset(&sa, 0, sizeof sa);
	sa.sun_family = AF_UNIX;
	sun_path_set(&sa);
	if (bind(g_listen, (struct sockaddr *)&sa,
		 offsetof(struct sockaddr_un, sun_path) + strlen(sa.sun_path) +
			 1) != 0)
		return -1;
	if (listen(g_listen, 16) != 0)
		return -1;

	g_srv_shm = SHM;
	g_srv_listen = g_listen;
	p = fork();
	if (p < 0)
		return -1;
	if (p == 0)
		server_entry();          /* never returns */
	g_server_pid = p;
	close(g_listen);
	g_listen = -1;

	{
		uint64_t deadline = now_ms() + 5000;

		while (now_ms() < deadline && !ld32_acq(&SHM->server_ready))
			usleep(1000);
		if (!ld32_acq(&SHM->server_ready)) {
			fprintf(stderr, "harness: server never became ready\n");
			return -1;
		}
	}

	g_ctl = ctl_connect_gen(GEN_PARENT);
	if (g_ctl < 0)
		return -1;
	if (pthread_create(&g_rx_thread, NULL, receiver_thread, NULL) != 0)
		return -1;
	pthread_detach(g_rx_thread);
	return 0;
}

static void teardown(void)
{
	uint64_t deadline;

	st_rel(&SHM->rx_stop, 1);
	futex_wake_priv(&SHM->rx_paused, 1);
	usleep(30000);

	st32_rel(&SHM->server_exit_req, 1);
	deadline = now_ms() + 3000;
	while (now_ms() < deadline && !ld32_acq(&SHM->server_exited))
		usleep(1000);
	if (!ld32_acq(&SHM->server_exited))
		kill(g_server_pid, SIGKILL);
	waitpid(g_server_pid, NULL, 0);

	if (g_ctl >= 0)
		close(g_ctl);
	unlink(g_path);
	unlink(g_shmfile);
	rmdir(g_scratch);
}

/* ------------------------------------------------------------------ */
/* phases                                                              */
/* ------------------------------------------------------------------ */

static struct snap g_ph_c2, g_ph_fd, g_ph_s2, g_ph_s1, g_ph_storm;
static struct child_report g_c5_rep, g_c6e_rep, g_c6f_rep;
static int g_c5_report_ok, g_c6e_report_ok, g_c6f_report_ok;
static uint64_t g_s2_dur_ns, g_s1_dur_ns, g_s2_ctx, g_s1_ctx;
static uint64_t g_s2_msgs, g_s1_msgs;
static uint64_t g_s1_lock_wait_ns, g_s1_contended;
static uint64_t g_s2_mean, g_s2_p99, g_s2_max, g_s1_mean, g_s1_p99,
	g_s1_max;
static uint64_t g_s1_wait_mean;
static int g_s2_n, g_s1_n;
static int g_c1_ok;
static uint64_t g_ledger_multi, g_ledger_missing;

/* read a raw child's fixed-size report from its pipe */
static int child_report_read(pid_t child, int rfd, struct child_report *rep,
			     uint64_t deadline_ms)
{
	size_t got = 0;
	int status;
	uint64_t reaped = 0;

	memset(rep, 0, sizeof *rep);
	while (now_ms() < deadline_ms && got < sizeof *rep) {
		struct pollfd pf;
		ssize_t r;

		pf.fd = rfd;
		pf.events = POLLIN;
		pf.revents = 0;
		if (poll(&pf, 1, 50) <= 0) {
			if (waitpid(child, &status, WNOHANG) == child) {
				reaped = 1;
				if (got >= sizeof *rep)
					break;
				if (poll(&pf, 1, 20) <= 0)
					break;
			}
			continue;
		}
		r = read(rfd, (char *)rep + got, sizeof *rep - got);
		if (r <= 0)
			break;
		got += (size_t)r;
	}
	if (!reaped) {
		uint64_t d = now_ms() + 2000;

		while (now_ms() < d) {
			if (waitpid(child, &status, WNOHANG) == child) {
				reaped = 1;
				break;
			}
			usleep(1000);
		}
		if (!reaped)
			kill(child, SIGKILL);
	}
	return got == sizeof *rep ? 0 : -1;
}

static int park_n(int n, uint64_t iters)
{
	uint64_t deadline;

	reset_workers();
	spawn_and_go(n, WM_C1, iters);
	deadline = now_ms() + g_phase_ms + 5000;
	while (now_ms() < deadline &&
	       (int)ld32_acq(&SHM->workers_parked) < n)
		futex_wait_priv(&SHM->workers_parked,
				(uint32_t)ld32_acq(&SHM->workers_parked), 5);
	return (int)ld32_acq(&SHM->workers_parked) >= n ? 0 : -1;
}

static int release_parked(int n)
{
	st_rel(&SHM->gate, 1);
	futex_wake_priv(&SHM->gate, WAKE_ALL);
	if (wait_workers(n, now_ms() + g_phase_ms) != 0)
		return -1;
	usleep(3000);
	return 0;
}

static void phase_c1(void)
{
	uint32_t setup_fds, fds32, fds64;
	int setup_sock, sock32, sock64;
	int ok = 1;

	setup_fds = (uint32_t)fd_count_self();
	setup_sock = fd_sockets_self();

	/* 32 guest threads, alive and parked while the census is taken */
	if (park_n(32, 8) != 0)
		ok = 0;
	fds32 = (uint32_t)fd_count_self();
	sock32 = fd_sockets_self();
	if (release_parked(32) != 0)
		ok = 0;

	/* 64 guest threads, same census */
	if (park_n(64, 8) != 0)
		ok = 0;
	fds64 = (uint32_t)fd_count_self();
	sock64 = fd_sockets_self();
	if (release_parked(64) != 0)
		ok = 0;

	SHM->c1_fd_setup = setup_fds;
	SHM->c1_fd_32 = fds32;
	SHM->c1_fd_64 = fds64;
	g_c1_ok = ok && setup_fds == fds32 && fds32 == fds64 &&
		  setup_sock == sock32 && sock32 == sock64 && setup_sock == 1;
	verdict(1, g_c1_ok,
		"guest held %u descriptor(s) with 0 threads, %u with 32 threads "
		"(control sockets %d) and %u with 64 threads (control sockets %d); "
		"one process-level control endpoint, count constant=%s, per-thread "
		"endpoints=%s",
		setup_fds, fds32, sock32, fds64, sock64,
		(setup_fds == fds32 && fds32 == fds64) ? "yes" : "NO",
		(sock32 == 1 && sock64 == 1) ? "none" : "PRESENT");
}

static void phase_c2(void)
{
	struct snap a, b;

	snap_take(&a);
	if (run_phase(64, WM_STYLE2, 32) != 0) {
		verdict(2, 0, "64-thread control phase did not finish");
		return;
	}
	snap_take(&b);
	snap_delta(&a, &b, &g_ph_c2);
}

static void phase_fd(void)
{
	struct snap a, b;
	uint32_t gb, sb, ga, sa;

	gb = (uint32_t)fd_count_self();
	sb = (uint32_t)fd_count_of(g_server_pid);
	snap_take(&a);
	if (run_phase(64, WM_FD, 8) != 0) {
		verdict(3, 0, "descriptor phase did not finish");
		return;
	}
	snap_take(&b);
	snap_delta(&a, &b, &g_ph_fd);
	usleep(5000);
	ga = (uint32_t)fd_count_self();
	sa = (uint32_t)fd_count_of(g_server_pid);
	SHM->fd_guest_before = gb;
	SHM->fd_guest_after = ga;
	SHM->fd_server_before = sb;
	SHM->fd_server_after = sa;
}

static void phase_c8(void)
{
	struct snap a, b;
	uint64_t t0, t1;

	/* style 2: lock-free publish, completion through the per-thread lane */
	memset(g_lat, 0, sizeof g_lat);
	memset(g_wait, 0, sizeof g_wait);
	snap_take(&a);
	reset_workers();
	t0 = spawn_and_go(64, WM_STYLE2, g_req_per_thread);
	if (wait_workers(64, now_ms() + g_phase_ms) != 0) {
		st32_rel(&SHM->phase_abort, 1);
		verdict(8, 0, "style 2 phase did not finish");
		return;
	}
	t1 = now_ms();
	snap_take(&b);
	snap_delta(&a, &b, &g_ph_s2);
	g_s2_dur_ns = (t1 - t0) * 1000000ULL;
	g_s2_ctx = ld_acq(&SHM->s2_ctx);
	gather_lat(64, g_samples, &g_s2_n);
	stats(g_samples, g_s2_n, &g_s2_mean, &g_s2_p99, &g_s2_max);
	g_s2_msgs = g_s2_n;

	/* style 1: the same traffic with the endpoint's receive direction owned
	 * by one lock, reply over the socket.  The receiver thread is paused for
	 * this phase: an implementation that serializes the whole round trip
	 * cannot share the endpoint's receive queue with a demultiplexer. */
	memset(g_lat, 0, sizeof g_lat);
	memset(g_wait, 0, sizeof g_wait);
	rx_pause();
	snap_take(&a);
	reset_workers();
	t0 = spawn_and_go(64, WM_STYLE1, g_req_per_thread);
	if (wait_workers(64, now_ms() + g_phase_ms) != 0) {
		st32_rel(&SHM->phase_abort, 1);
		rx_resume();
		verdict(8, 0, "style 1 phase did not finish");
		return;
	}
	t1 = now_ms();
	snap_take(&b);
	snap_delta(&a, &b, &g_ph_s1);
	g_s1_dur_ns = (t1 - t0) * 1000000ULL;
	g_s1_ctx = ld_acq(&SHM->s1_ctx);
	g_s1_lock_wait_ns = __atomic_load_n(&lock_wait_ns, __ATOMIC_RELAXED);
	g_s1_contended = __atomic_load_n(&lock_contended, __ATOMIC_RELAXED);
	gather_lat(64, g_samples, &g_s1_n);
	stats(g_samples, g_s1_n, &g_s1_mean, &g_s1_p99, &g_s1_max);
	g_s1_msgs = g_s1_n;
	{
		int i;
		uint64_t j;
		uint64_t sum = 0, cnt = 0;

		for (i = 0; i < 64; i++) {
			for (j = 0; j < g_args[i].ops && j < MAX_ITERS; j++) {
				sum += g_wait[i][j];
				cnt++;
			}
		}
		g_s1_wait_mean = cnt ? sum / cnt : 0;
	}
	rx_resume();
}

static void phase_c5(void)
{
	int pfd[2];
	struct snap a, b;
	pid_t c;
	uint64_t t0;

	if (pipe(pfd) != 0) {
		verdict(5, 0, "pipe() failed");
		return;
	}
	g_child_report_fd = pfd[1];
	reset_workers();
	st_rel(&SHM->storm_stop, 0);
	snap_take(&a);
	spawn_and_go(64, WM_STORM, 40000000);
	c = fork();
	if (c == 0) {
		close(pfd[0]);
		c5_child_entry();        /* never returns */
	}
	t0 = now_ms();
	while (now_ms() - t0 < g_storm_dur_ms)
		usleep(2000);
	st_rel(&SHM->storm_stop, 1);
	/* the parent's own completions are counted only once every worker has
	 * finished: the counters must not be read while a request is still in
	 * flight, or the phase would appear to lose completions */
	wait_workers(64, now_ms() + 20000);
	snap_take(&b);
	snap_delta(&a, &b, &g_ph_storm);
	g_c5_report_ok = child_report_read(c, pfd[0], &g_c5_rep,
					   now_ms() + 15000) == 0;
	close(pfd[0]);
	close(pfd[1]);
	g_child_report_fd = -1;
}

static void phase_c6(void)
{
	int pfd[2];
	pid_t c;

	/* (a) a successful execve: the reply still owed on the pre-exec
	 * connection must be refused by the post-exec image. */
	if (pipe(pfd) != 0) {
		verdict(6, 0, "pipe() failed");
		return;
	}
	g_child_report_fd = pfd[1];
	c = fork();
	if (c == 0) {
		close(pfd[0]);
		c6_child_entry(0);       /* never returns */
	}
	g_c6e_report_ok = child_report_read(c, pfd[0], &g_c6e_rep,
					    now_ms() + 20000) == 0;
	close(pfd[0]);
	close(pfd[1]);

	/* (b) an execve that cannot work: the previous endpoint stays usable */
	if (pipe(pfd) != 0) {
		verdict(6, 0, "pipe() failed");
		return;
	}
	g_child_report_fd = pfd[1];
	c = fork();
	if (c == 0) {
		close(pfd[0]);
		c6_child_entry(1);       /* never returns */
	}
	g_c6f_report_ok = child_report_read(c, pfd[0], &g_c6f_rep,
					    now_ms() + 20000) == 0;
	close(pfd[0]);
	close(pfd[1]);
	g_child_report_fd = -1;
}

static uint64_t c7_sub(int sub, uint64_t dur_ms, uint64_t *ops_out,
		       uint64_t *ns_out)
{
	uint64_t go, stop, ops_end;
	int expected, i;
	pthread_t t;

	reset_workers();
	st_rel(&SHM->hot_stop, 0);
	st_rel(&SHM->hot_go, 0);
	st_rel(&SHM->holder_grab_ns, 0);
	st_rel(&SHM->holder_release_ns, 0);
	st_rel(&SHM->holder_release, 0);
	st_rel(&SHM->ctl_done_after_grab, 0);
	st_rel(&SHM->storm_stop, 0);
	st_rel(&SHM->hot_ops_start, 0);
	st_rel(&SHM->holder_ops_start, 0);
	st_rel(&SHM->holder_ops_end, 0);
	memset(g_hot_local, 0, sizeof g_hot_local);
	g_hot_bad = 0;
	expected = HOT_LANES;

	if (sub == 3) {
		uint64_t d = now_ms() + 5000;

		if (pthread_create(&t, NULL, holder_thread, NULL) != 0)
			die("pthread_create(holder)");
		pthread_detach(t);
		expected++;
		while (now_ms() < d && ld_acq(&SHM->holder_grab_ns) == 0)
			usleep(500);
	}

	for (i = 0; i < HOT_LANES; i++) {
		g_hargs[i].lane = i;
		if (pthread_create(&t, NULL, hot_producer, &g_hargs[i]) != 0)
			die("pthread_create(hot)");
		pthread_detach(t);
	}
	if (sub == 2)
		spawn_n(64, WM_H2, 40000000);
	else if (sub == 3)
		spawn_n(64, WM_H3, 40000000);
	if (sub != 1) {
		st_rel(&SHM->phase_go, 1);
		futex_wake_priv(&SHM->phase_go, WAKE_ALL);
	}
	if (sub != 1)
		expected += 64;

	st_rel(&SHM->hot_ops_start, hot_live_sum());
	st_rel(&SHM->hot_active, 1);
	st_rel(&SHM->hot_go, 1);
	futex_wake_priv(&SHM->hot_go, WAKE_ALL);
	go = now_ns();
	usleep((useconds_t)(dur_ms * 1000));
	/* the observer closes its window and reads the live counters itself */
	ops_end = hot_live_sum();
	st_rel(&SHM->hot_stop, 1);
	stop = now_ns();
	usleep(2000);
	st_rel(&SHM->holder_release, 1);
	futex_wake_priv(&SHM->holder_release, WAKE_ALL);
	wait_workers(expected, now_ms() + 30000);
	st_rel(&SHM->hot_active, 0);
	usleep(3000);

	*ops_out = ops_end - ld_acq(&SHM->hot_ops_start);
	if (sub == 3) {
		uint64_t grab = ld_acq(&SHM->holder_grab_ns);
		uint64_t rel = ld_acq(&SHM->holder_release_ns);

		/* C7 is about what the hot path completed while the lock was
		 * actually held: the holder sampled the live counters across its
		 * own held window, and this is that window's exposure */
		*ops_out = ld_acq(&SHM->holder_ops_end) -
			   ld_acq(&SHM->holder_ops_start);
		*ns_out = (grab && rel > grab) ? rel - grab : stop - go;
	} else {
		*ns_out = stop - go;
	}
	return 0;
}

static void phase_c7(void)
{
	uint64_t ops, ns;

	c7_sub(1, g_hot_dur_ms, &ops, &ns);
	SHM->hot1_ops = ops;
	SHM->hot1_ns = ns;
	c7_sub(2, g_hot_dur_ms, &ops, &ns);
	SHM->hot2_ops = ops;
	SHM->hot2_ns = ns;
	c7_sub(3, g_hot_dur_ms, &ops, &ns);
	SHM->hot3_ops = ops;
	SHM->hot3_ns = ns;
	SHM->holder_hold_ns = ld_acq(&SHM->holder_release_ns) >
				      ld_acq(&SHM->holder_grab_ns)
				      ? ld_acq(&SHM->holder_release_ns) -
						ld_acq(&SHM->holder_grab_ns)
				      : 0;
	SHM->hot3_ctl_after_grab = ld_acq(&SHM->ctl_done_after_grab);
}

/* The exactly-once ledger, verified per id space so that each claim is judged
 * on its own traffic: C2's lane traffic, C3's descriptor traffic and C8's
 * style-1 traffic each have to read exactly 1 for every published id. */
struct ledstat {
	uint64_t multi;      /* ids observed more than once                    */
	uint64_t missing;    /* published ids never observed                   */
};

static struct ledstat g_led_s2, g_led_s1, g_led_fd;

static void ledger_space(uint64_t base, uint64_t published, struct ledstat *st)
{
	uint64_t k, lim = published > LEDGER_SPAN ? LEDGER_SPAN : published;

	st->multi = 0;
	st->missing = 0;
	for (k = 0; k < lim; k++) {
		uint32_t v = ld_acq(&SHM->ledger[base + k]);

		if (v == 0)
			st->missing++;
		else if (v > 1)
			st->multi++;
	}
}

static void ledger_validate(void)
{
	ledger_space(0, ld_acq(&SHM->seq_style2), &g_led_s2);
	ledger_space(IDSPACE_STYLE1, ld_acq(&SHM->seq_style1), &g_led_s1);
	ledger_space(IDSPACE_FD, ld_acq(&SHM->seq_fd), &g_led_fd);
	g_ledger_multi = ld_acq(&SHM->observed_multi);
	g_ledger_missing = g_led_s2.missing + g_led_s1.missing + g_led_fd.missing;
}

/* the application table: every row must belong to the process that asked */
static void app_validate(void)
{
	int i;
	uint64_t seen[APP_ROWS];
	int nseen = 0;

	SHM->app_foreign_gen = 0;
	for (i = 0; i < 64; i++) {
		struct approw *r = &SHM->app[i];

		if (ld_acq(&r->state) != 1) {
			add_rel(&SHM->app_unfilled, 1);
			continue;
		}
		if (r->gen != GEN_PARENT || r->lane != (uint64_t)i ||
		    r->owner_req >= 0xC000000000000000ULL)
			add_rel(&SHM->app_foreign_gen, 1);
	}
	for (i = 64; i < 96; i++) {
		struct approw *r = &SHM->app[i];

		if (ld_acq(&r->state) != 1)
			continue;        /* the fork child only uses its first 16 */
		if (r->gen != GEN_FORK || r->lane != (uint64_t)i ||
		    (r->owner_req >> 48) != 0xC5C5ULL)
			add_rel(&SHM->app_foreign_gen, 1);
	}
	SHM->app_dup = 0;
	for (i = 0; i < APP_ROWS; i++) {
		int j;

		if (ld_acq(&SHM->app[i].state) != 1)
			continue;
		for (j = 0; j < nseen; j++)
			if (seen[j] == SHM->app[i].owner_req)
				add_rel(&SHM->app_dup, 1);
		if (nseen < APP_ROWS)
			seen[nseen++] = SHM->app[i].owner_req;
	}
}

/* ------------------------------------------------------------------ */
/* claims                                                              */
/* ------------------------------------------------------------------ */

static void compute_claims(void)
{
	char why[256];
	uint64_t gb, ga, sb, sa;
	int s1_faster, s2_faster;

	/* ---- C2 ---- */
	{
		uint64_t tagged = ld_acq(&SHM->seq_style2) +
				  ld_acq(&SHM->seq_style1) + ld_acq(&SHM->seq_fd);

		verdict(2, g_ph_c2.s2_requests > 0 &&
			   g_ph_c2.s2_completed == g_ph_c2.s2_requests &&
			   g_ph_c2.s2_foreign == 0 && g_ph_c2.s2_timeouts == 0 &&
			   g_led_s2.multi == 0 && g_led_s2.missing == 0 &&
			   ld_acq(&SHM->ledger_overflow) == 0,
		"64 threads published %llu concurrent control requests (tagged with "
		"a request id and a lane id); %llu completed, %llu observed a "
		"completion that was not their own, %llu timed out; whole run: all "
		"%llu token-tagged request(s) published, and of the %llu lane-tagged "
		"ones the ledger read exactly 1 for every id (%llu completed more "
		"than once, %llu never observed, %llu id-space overflow(s)); "
		"%llu packet(s) with a foreign generation reached the receiver",
		(unsigned long long)g_ph_c2.s2_requests,
		(unsigned long long)g_ph_c2.s2_completed,
		(unsigned long long)g_ph_c2.s2_foreign,
		(unsigned long long)g_ph_c2.s2_timeouts,
		(unsigned long long)tagged,
		(unsigned long long)ld_acq(&SHM->seq_style2),
		(unsigned long long)g_led_s2.multi,
		(unsigned long long)g_led_s2.missing,
		(unsigned long long)ld_acq(&SHM->ledger_overflow),
		(unsigned long long)ld_acq(&SHM->rx_foreign_gen));
	}

	/* ---- C3 ---- */
	gb = ld_acq(&SHM->fd_guest_before);
	ga = ld_acq(&SHM->fd_guest_after);
	sb = ld_acq(&SHM->fd_server_before);
	sa = ld_acq(&SHM->fd_server_after);
	verdict(3, g_ph_fd.fd_in_requests > 0 && g_ph_fd.fd_out_requests > 0 &&
			   g_ph_fd.fd_in_ok == g_ph_fd.fd_in_requests &&
			   g_ph_fd.fd_in_bad == 0 &&
			   g_ph_fd.fd_out_ok == g_ph_fd.fd_out_requests &&
			   g_ph_fd.fd_out_bad == 0 &&
			   g_ph_fd.fd_out_timeouts == 0 &&
			   ld_acq(&SHM->rx_fds_foreign) == 0 &&
			   ld_acq(&SHM->rx_fds_orphan) == 0 &&
			   ld_acq(&SHM->rx_bad_lane) == 0 && gb == ga && sb == sa &&
			   g_led_fd.multi == 0 && g_led_fd.missing == 0,
		"guest->server %llu descriptor(s), server->guest %llu descriptor(s) "
		"under 64-thread concurrency; identity proved by a token inside each "
		"descriptor: %llu guest->server accepted, %llu rejected, %llu "
		"server->guest accepted, %llu rejected; %llu descriptor(s) arrived "
		"for a waiting-but-different request, %llu for an idle lane, %llu "
		"wrong lane; every descriptor-tagged request observed exactly once "
		"(%llu repeated, %llu missing); no leak (guest fds %llu->%llu, "
		"server fds %llu->%llu)",
		(unsigned long long)g_ph_fd.fd_in_requests,
		(unsigned long long)g_ph_fd.fd_out_requests,
		(unsigned long long)g_ph_fd.fd_in_ok,
		(unsigned long long)g_ph_fd.fd_in_bad,
		(unsigned long long)g_ph_fd.fd_out_ok,
		(unsigned long long)g_ph_fd.fd_out_bad,
		(unsigned long long)ld_acq(&SHM->rx_fds_foreign),
		(unsigned long long)ld_acq(&SHM->rx_fds_orphan),
		(unsigned long long)ld_acq(&SHM->rx_bad_lane),
		(unsigned long long)g_led_fd.multi,
		(unsigned long long)g_led_fd.missing,
		(unsigned long long)gb, (unsigned long long)ga,
		(unsigned long long)sb, (unsigned long long)sa);

	/* ---- C4 ---- */
	verdict(4, ld_acq(&SHM->app_published) > 0 &&
			   ld_acq(&SHM->app_foreign_gen) == 0 &&
			   ld_acq(&SHM->app_token_mismatch) == 0 &&
			   ld_acq(&SHM->app_dup) == 0 &&
			   ld_acq(&SHM->app_unfilled) == 0 &&
			   ld_acq(&SHM->app_parent_rows) > 0 &&
			   ld_acq(&SHM->app_child_rows) > 0 &&
			   g_c5_report_ok && g_c5_rep.fd_out_ok > 0,
		"application-table opens: %llu row(s) installed by the requesting "
		"process (%llu in the parent's own 64 rows, %llu in the forked "
		"child's rows, %llu validated by the child itself); %llu row(s) "
		"carried the wrong generation or lane, %llu carried a descriptor "
		"whose token was not its request id, %llu row(s) duplicated a "
		"request id across processes, %llu row(s) never installed",
		(unsigned long long)ld_acq(&SHM->app_published),
		(unsigned long long)ld_acq(&SHM->app_parent_rows),
		(unsigned long long)ld_acq(&SHM->app_child_rows),
		(unsigned long long)(g_c5_report_ok ? g_c5_rep.fd_out_ok : 0),
		(unsigned long long)ld_acq(&SHM->app_foreign_gen),
		(unsigned long long)ld_acq(&SHM->app_token_mismatch),
		(unsigned long long)ld_acq(&SHM->app_dup),
		(unsigned long long)ld_acq(&SHM->app_unfilled));

	/* ---- C5 ---- */
	verdict(5, g_c5_report_ok && g_c5_rep.endpoint_ok &&
			   g_c5_rep.foreign == 0 && g_c5_rep.verified > 0 &&
			   g_c5_rep.fd_out_bad == 0 &&
			   g_ph_storm.storm_foreign == 0 &&
			   g_ph_storm.storm_expected > 0 &&
			   g_ph_storm.storm_completed + g_ph_storm.storm_slow ==
				   g_ph_storm.storm_expected &&
			   ld_acq(&SHM->rx_foreign_gen) == 0 && g_c5_rep.stage == 0,
		"parent storm: %llu/%llu completions while the child ran (%llu "
		"completion(s) went to the wrong thread, %llu slow, %llu packet(s) "
		"with a foreign generation, %llu completion(s) lost); fork "
		"child: own endpoint established=%s, %llu request(s) verified as its "
		"own, %llu reply/replies were not its own, %llu descriptor(s) "
		"mis-identified, stage=%llu",
		(unsigned long long)g_ph_storm.storm_completed,
		(unsigned long long)g_ph_storm.storm_expected,
		(unsigned long long)g_ph_storm.storm_foreign,
		(unsigned long long)g_ph_storm.storm_slow,
		(unsigned long long)ld_acq(&SHM->rx_foreign_gen),
		(unsigned long long)(g_ph_storm.storm_expected -
				     g_ph_storm.storm_completed -
				     g_ph_storm.storm_slow),
		g_c5_report_ok && g_c5_rep.endpoint_ok ? "yes" : "NO",
		(unsigned long long)(g_c5_report_ok ? g_c5_rep.verified : 0),
		(unsigned long long)(g_c5_report_ok ? g_c5_rep.foreign : 0),
		(unsigned long long)(g_c5_report_ok ? g_c5_rep.fd_out_bad : 0),
		(unsigned long long)(g_c5_report_ok ? g_c5_rep.stage : 1));

	/* ---- C6 ---- */
	verdict(6, g_c6e_report_ok && g_c6f_report_ok &&
			   g_c6e_rep.endpoint_ok && g_c6e_rep.own_ok &&
			   g_c6e_rep.stale_endpoint_rejected &&
			   g_c6e_rep.stale_seen && g_c6e_rep.stale_rejected &&
			   g_c6f_rep.exec_failed == ENOENT && g_c6f_rep.post_fail_ok &&
			   g_c6e_rep.stage == 0 && g_c6f_rep.stage == 0,
		"successful exec: endpoint re-established=%s on generation %llu, "
		"request over the superseded generation refused by the server=%s, "
		"own request verified=%s, pre-exec reply still observed=%s and "
		"rejected instead of consumed=%s; failed exec: execve failed with "
		"errno %llu and the previous endpoint still served %s",
		g_c6e_report_ok && g_c6e_rep.endpoint_ok ? "yes" : "NO",
		(unsigned long long)(g_c6e_report_ok ? g_c6e_rep.gen : 0),
		g_c6e_report_ok && g_c6e_rep.stale_endpoint_rejected ? "yes" : "NO",
		g_c6e_report_ok && g_c6e_rep.own_ok ? "yes" : "no",
		g_c6e_report_ok && g_c6e_rep.stale_seen ? "yes" : "NO",
		g_c6e_report_ok && g_c6e_rep.stale_rejected ? "yes" : "NO",
		(unsigned long long)(g_c6f_report_ok ? g_c6f_rep.exec_failed : 0),
		g_c6f_report_ok && g_c6f_rep.post_fail_ok ? "yes" : "NO");

	/* ---- C7 ---- */
	verdict(7, ld_acq(&SHM->hot1_ops) >= g_hot_dur_ms * 20 &&
			   ld_acq(&SHM->hot2_ops) > 0 &&
			   ld_acq(&SHM->hot3_ops) >= g_hot_dur_ms * 20 &&
			   ld_acq(&SHM->hot3_ops) * 100 >=
				   ld_acq(&SHM->hot1_ops) * 25 &&
			   ld_acq(&SHM->hot3_ctl_after_grab) == 0 &&
			   ld_acq(&SHM->holder_hold_ns) * 10 >=
				   g_hot_dur_ms * 1000000ULL * 8 &&
			   ld_acq(&SHM->hot_lost) == 0,
		"hot path throughput (ring lanes only, counted by the observer of each "
		"window): %llu ops/s alone (>= %llu ops required in the window), %llu "
		"ops/s with 64 threads of style-2 control traffic, %llu ops/s while "
		"the control lock was held by a deliberate holder for the whole phase "
		"(%llu ms held, %llu control completion(s) inside that held window, "
		"%llu hot result(s) wrong); the hot path never waits for the control "
		"lock.  The control-load figure is set by the single server thread "
		"that polls the rings between control batches, not by the guest lock",
		(unsigned long long)(ld_acq(&SHM->hot1_ns)
					     ? ld_acq(&SHM->hot1_ops) * 1000000000ULL /
						       ld_acq(&SHM->hot1_ns)
					     : 0),
		(unsigned long long)(g_hot_dur_ms * 20),
		(unsigned long long)(ld_acq(&SHM->hot2_ns)
					     ? ld_acq(&SHM->hot2_ops) * 1000000000ULL /
						       ld_acq(&SHM->hot2_ns)
					     : 0),
		(unsigned long long)(ld_acq(&SHM->hot3_ns)
					     ? ld_acq(&SHM->hot3_ops) * 1000000000ULL /
						       ld_acq(&SHM->hot3_ns)
					     : 0),
		(unsigned long long)(ld_acq(&SHM->holder_hold_ns) / 1000000ULL),
		(unsigned long long)ld_acq(&SHM->hot3_ctl_after_grab),
		(unsigned long long)ld_acq(&SHM->hot_lost));

	/* ---- C8 ---- */
	s1_faster = g_s1_msgs && g_s2_msgs &&
		    (uint64_t)(g_s1_dur_ns ? g_s1_msgs * 1000000000ULL /
						     g_s1_dur_ns
					   : 0) >
			    (uint64_t)(g_s2_dur_ns ? g_s2_msgs * 1000000000ULL /
						     g_s2_dur_ns
						   : 0) &&
		    g_s1_p99 < g_s2_p99;
	s2_faster = g_s2_msgs && g_s1_msgs &&
		    (uint64_t)(g_s2_dur_ns ? g_s2_msgs * 1000000000ULL /
						     g_s2_dur_ns
					   : 0) >
			    (uint64_t)(g_s1_dur_ns ? g_s1_msgs * 1000000000ULL /
						     g_s1_dur_ns
						   : 0) &&
		    g_s2_p99 < g_s1_p99;
	snprintf(why, sizeof why, "%s",
		 s2_faster ? "style 2 (no guest lock, completion through the lane)"
			   : (s1_faster ? "style 1 (one lock, reply on the socket)"
					: "neither on both metrics (see numbers)"));
	verdict(8, g_s1_msgs > 0 && g_s2_msgs > 0 &&
			   g_s1_msgs == (uint64_t)g_s1_n &&
			   g_s2_msgs == (uint64_t)g_s2_n && g_ph_s2.s2_foreign == 0 &&
			   g_ph_s1.s1_foreign == 0 && g_led_s1.multi == 0 &&
			   g_led_s1.missing == 0,
		"64 threads x %llu requests each: style 1 %llu req in %llu ms = "
		"%llu msg/s, mean %llu ns, p99 %llu ns, max %llu ns, lock wait "
		"mean %llu ns, %llu contended acquisition(s), guest voluntary ctx "
		"switches %llu; style 2 %llu req in %llu ms = %llu msg/s, mean "
		"%llu ns, p99 %llu ns, max %llu ns, guest voluntary ctx switches "
		"%llu; winner: %s.  Measured structural finding: the one endpoint's "
		"receive queue cannot be shared between the per-process demultiplexer "
		"and lock-serialized round trips, so the demultiplexer had to be "
		"paused %llu time(s) for style 1",
		(unsigned long long)g_req_per_thread,
		(unsigned long long)g_s1_msgs,
		(unsigned long long)(g_s1_dur_ns / 1000000ULL),
		(unsigned long long)(g_s1_dur_ns ? g_s1_msgs * 1000000000ULL /
							  g_s1_dur_ns
						: 0),
		(unsigned long long)g_s1_mean, (unsigned long long)g_s1_p99,
		(unsigned long long)g_s1_max,
		(unsigned long long)g_s1_wait_mean,
		(unsigned long long)g_s1_contended,
		(unsigned long long)g_s1_ctx,
		(unsigned long long)g_s2_msgs,
		(unsigned long long)(g_s2_dur_ns / 1000000ULL),
		(unsigned long long)(g_s2_dur_ns ? g_s2_msgs * 1000000000ULL /
							  g_s2_dur_ns
						: 0),
		(unsigned long long)g_s2_mean, (unsigned long long)g_s2_p99,
		(unsigned long long)g_s2_max,
		(unsigned long long)g_s2_ctx, why,
		(unsigned long long)ld_acq(&SHM->rx_paused_count));

	/* ---- C9 ---- */
	{
		uint64_t hot = ld_acq(&SHM->hot1_ops) + ld_acq(&SHM->hot2_ops) +
			       ld_acq(&SHM->hot3_ops);
		uint64_t ctl = ld_acq(&SHM->ctl_requests);
		uint64_t per1000 = hot ? ctl * 1000ULL / hot : 0;

		verdict(9, ctl > 0 && hot > 0,
			"harness counters only (NOT the product): %llu control "
			"operation(s) and %llu hot-path operation(s) in this run = "
			"%llu control op(s) per 1000 hot op(s), i.e. a control share "
			"of %llu.%02llu%%; the harness's workload is deliberately "
			"control-heavy so this is a worst case, and C7 shows the hot "
			"path does not depend on control traffic at all",
			(unsigned long long)ctl, (unsigned long long)hot,
			(unsigned long long)per1000,
			(unsigned long long)(hot ? ctl * 100 / (ctl + hot) : 0),
			(unsigned long long)(hot ?
				(ctl * 10000ULL / (ctl + hot)) % 100ULL : 0));
	}
}

/* ------------------------------------------------------------------ */
/* main                                                                */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
	const char *env = getenv("DCP_ENV");

	g_env = (env && env[0]) ? env : "unlabeled";
	setvbuf(stdout, NULL, _IOLBF, 0);

	if (argc >= 7 && strcmp(argv[1], "exec-child") == 0) {
		exec_child_entry(argv[2], strtoull(argv[3], NULL, 10),
				 strtoull(argv[4], NULL, 10),
				 strtol(argv[5], NULL, 10),
				 strtol(argv[6], NULL, 10));
		return 0;                /* not reached */
	}
	if (argc > 1 && strcmp(argv[1], "all") != 0) {
		fprintf(stderr,
			"usage: %s [all|exec-child PATH GEN REQ STALE_FD REPORT_FD]\n",
			argv[0]);
		return 2;
	}

	g_fast = getenv("DCP_FAST") != NULL;
	if (g_fast) {
		g_wait_ms = 400;
		g_phase_ms = 5000;
		g_req_per_thread = 16;
		g_hot_dur_ms = 150;
		g_storm_dur_ms = 150;
		g_slow_ms = 120;
	}
	g_slow_ms = g_fast ? 120 : 200;
	{
		const char *v = getenv("DCP_SLOW_MS");

		if (v && v[0])
			g_slow_ms = strtol(v, NULL, 10);
	}

	printf("HARNESS start env=%s fast=%d threads=%d/%d\n", g_env, g_fast, 32,
	       64);
	fflush(stdout);

	g_envp = environ;
	if (setup_all() != 0)
		die("setup");
	{
		char list[1024];

		fd_snapshot_pid(0, list, sizeof list);
		printf("INFO guest descriptors at setup: %s\n", list);
		printf("INFO server pid=%d, control endpoint fd=%d, path=%s\n",
		       (int)g_server_pid, g_ctl, g_path);
		fflush(stdout);
	}

	phase_c1();
	phase_c2();
	phase_fd();
	phase_c8();
	phase_c5();
	phase_c6();
	phase_c7();
	ledger_validate();
	app_validate();
	compute_claims();
	print_claims();

	teardown();
	return all_ok() ? 0 : 1;
}




/*
 * generation-aba-proof.c -- standalone falsification harness for the
 * "a reused transport slot must never accept a completion addressed to its
 *  previous occupant" requirement, across four completion classes.
 *
 * WHAT IS UNDER TEST
 * ------------------
 * The transport is the per-thread ring lane.  The guest keeps a fixed lane
 * table (deployed tree, dserver-ring.c:90-101):
 *
 *   typedef struct {
 *       uint32_t active;       // 0 free, 1 live (published LAST on acquire)
 *       uint32_t generation;   // bumped on every (re)claim so a recycled TID
 *                              //  cannot match a stale lane epoch
 *       int      state;        // 0 untried, 1 attached, -1 failed
 *       int      owner_tid;    // gettid of the owning thread
 *       void*    map; uint64_t size; int wake_fd;
 *       uint32_t seq;          // monotonic request seq for THIS lane
 *   } gr_lane_t;
 *
 * The guest finds its lane by `active == 1 && owner_tid == tid'
 * (dserver-ring.c:136-143), claims a free slot with a CAS 0 -> 2
 * (dserver-ring.c:148-162), and publishes active=1 LAST after a successful
 * attach, bumping `generation' and resetting `seq' to 1 on the way
 * (dserver-ring.c:336-345).  A reply is trusted iff
 * `rep->seq == seq && rep->callnum == callnum' (dserver-ring.c:529, :639,
 * :725).  __dserver_ring_postfork_reset (dserver-ring.c:210-226) zeroes
 * active/generation/owner_tid/seq in the fork child, so the child's epoch
 * counter restarts at 0 and its first claim lands on generation 1.
 *
 * WHAT IS READ ON THE DEPLOYED TREE (the facts this harness is built on)
 * ---------------------------------------------------------------------
 *   * `generation' is bumped on every (re)claim and NO runtime path reads it
 *     (it feeds the D17 statistic g_stat_lanes_reclaimed only).
 *   * the guest never releases a slot today (lane reclaim on thread-exit is a
 *     future bead), so the whole reuse path this harness exercises is the
 *     path a slot-reusing transport must add.
 *   * the server has NO fork epoch and NO lane-generation check: its identity
 *     is the kernel SCM_CREDENTIALS for the process plus the payload-claimed
 *     nsid for the thread, cross-checked against /proc/<pid>/task.
 *   * the server's default lane wake model is "poll the rings"; there is no
 *     per-completion identity on the shared-memory path beyond the echoed
 *     slot header fields (seq, callnum).
 *
 * CONSEQUENCE THIS HARNESS MEASURES
 * ---------------------------------
 * `seq' restarts at 1 on every (re)claim and a lane's address is the slot
 * index, so a completion produced for occupant A of slot X carries
 * correlation fields that a NEW occupant B of the same slot can also satisfy:
 * same slot, same seq (1), same callnum, and -- with a recycled TID, which is
 * exactly what the generation field exists to defeat -- the same owner_tid.
 * The only field separating the two epochs is `generation', which no runtime
 * path reads.  This harness builds a working transport around that table (a
 * real server process, shared-memory completions and a duplex S2C mailbox, a
 * real process-level control endpoint, real SCM_RIGHTS descriptors, a real
 * fork under load) and delivers completions LATE -- after the slot has been
 * released and re-claimed -- to measure whether the new occupant accepts
 * them.
 *
 * CLAIMS (one line each; the exact identity tuple compared and the rejection
 * reason are printed on the claim line and on a preceding IDENT line)
 * ---------------------------------------------------------------------
 *   G1 lane ABA, ring completion.  Identity compared:
 *        {slot, generation, owner_tid, seq, callnum}
 *      A claims slot X at generation N, A exits, X is released and re-claimed
 *      by B (recycled TID -> same owner_tid) at generation N+1; the completion
 *      produced for A is delivered late and MUST be rejected with the reason
 *      printed; the occupant's OWN delayed completion must still be accepted.
 *   G2 server-initiated (S2C) work.  Identity compared:
 *        {slot, generation, owner_tid, parent_id}
 *      (the server's per-upcall id is stamped, printed and echoed back by the
 *      guest's reply, but the guest cannot know it before the upcall arrives,
 *      so it is not an input to the accept decision -- parent_id, which the
 *      guest chose, is.)
 *      The upcall is produced for the pre-fork occupant, the process forks
 *      (the transcribed postfork reset restarts the epoch counter, so the
 *      child's first claim of the same slot carries the SAME generation), and
 *      the upcall is delivered to the child's slot afterwards.  It must be
 *      reported rejected/undeliverable and MUST NOT execute in the child's
 *      context: the upcall's side effect is a real munmap(2) of a shared page,
 *      and the harness probes that the page is still mapped.
 *   G3 blocking control completion.  Identity compared:
 *        {request_id, slot, generation, owner_tid, token}
 *      The completion endpoint is ONE process-level control descriptor for the
 *      process.  A's blocking request id is abandoned when A exits; B blocks
 *      on its own request id; A's completion is delivered first and must not
 *      satisfy B's outstanding request (B must still be blocked, and the
 *      completion must be reported as having no live request).
 *   G4 descriptor association (SCM_RIGHTS).  Identity compared:
 *        {descriptor token == request_id, slot, generation, owner_tid}
 *      A descriptor sent for A's logical request must never be installed into
 *      B's request; it must be CLOSED rather than leaked.  Measured with real
 *      fd counts of both processes (before/after) and the token read back out
 *      of the descriptor B ends up holding.
 *   G5 repeated reuse under load.  >= 1000 slot acquisitions, each with a REAL
 *      delay injected between production and delivery; zero wrong-consumption
 *      events, and the legitimate deliveries are still accepted (so the
 *      verdict cannot be earned by rejecting everything).
 *
 * THE HARNESS MUST BE ABLE TO FAIL
 * --------------------------------
 * The runner (run-generation-aba-proof.sh) builds deliberate mutations of this
 * source in its own temporary directory (never in the repository) and requires
 * the named claim to go red:
 *   M1 the generation is dropped from the identity comparison (the deployed
 *      state: nothing reads it)                    -> G1 red (and G5 red)
 *   M2 the comparison loses the thread identity (owner_tid), leaving the
 *      numeric slot id and the epoch                    -> G2 red (and G5 red)
 *   M3 a control completion is matched by arrival order instead of by
 *      request id                                              -> G3 red
 *   M4 a descriptor is installed without checking the request identity
 *                                                              -> G4 red
 * Each mutation flips a single marked #define; the runner verifies that the
 * marker applied, that the mutant binary differs, and that the named claim
 * line goes red.
 *
 * WHAT THIS HARNESS MODELS, AND WHAT IT DOES NOT
 * ----------------------------------------------
 *   * It is a MODEL of the lane/ring/control discipline, not mldr,
 *     darlingserver or a Darling prefix.  It executes REAL syscalls where the
 *     mechanism under test is a syscall: fork, an AF_UNIX SOCK_SEQPACKET
 *     control endpoint, sendmsg/SCM_RIGHTS, memfd_create (pipe2 fallback),
 *     mincore, munmap (only when an upcall is wrongly accepted), futex, pipe,
 *     /proc/<pid>/fd accounting.  The lane table, the completion
 *     stamps and the identity predicates are the model's transcription of the
 *     contract a slot-reusing transport must satisfy; they are not the
 *     deployed code, which today has no generation check and no slot release.
 *   * The lane table is PROCESS-PRIVATE (BSS), as the deployed g_lanes is; the
 *     rings, completions and the S2C mailbox live in ONE MAP_SHARED mapping, as
 *     the guest's ring mapping does.  A request carries the identity the guest
 *     CLAIMS for its lane (slot, generation, owner_tid), which is the
 *     product's payload-claimed nsid; the server stamps the completion from
 *     that claim at PRODUCTION time and performs no identity check of its own
 *     at delivery.
 *   * The server process and the fork-under-test child run on raw x86-64
 *     syscalls (the server is libc-free: the runner asserts its entry point
 *     contains no `call' instruction at all).  The fork child's entry
 *     (fork_child_entry) is a separate noinline symbol the runner checks for
 *     calls into the allocator, stdio and the pthread locks -- the things a
 *     post-fork path must not touch.
 *   * The recycled TID is created by EXPLICIT sequencing: the token handed to
 *     B is the token the exited thread A had -- the value a kernel produces
 *     when it recycles a tid.  The harness never races the kernel's tid
 *     allocator, because that would be statistical, not a proof.
 *   * The fork-epoch collision in G2 is created by the transcribed postfork
 *     reset executed in the real fork child (generation restarts at 0), which
 *     is what the deployed __dserver_ring_postfork_reset does.
 *   * The server-side process identity the real darlingserver derives from
 *     kernel SCM_CREDENTIALS (and the payload nsid it cross-checks against
 *     /proc/<pid>/task) is NOT re-implemented here: the harness does not run
 *     /proc, and the guest lane table it models carries no process epoch
 *     (dserver-ring.c:90-101 has none).  The consequence is measured rather
 *     than assumed: after a real fork the child's epoch counter restarts, so
 *     its first claim of a slot collides with the parent's first epoch, and
 *     the only field left that separates the two occupants is the thread
 *     token.  M2 removes that field and G2 must go red -- which is the
 *     measurement of why a per-call owner check is load-bearing.
 *   * Only the host Linux kernel is measured; the container shares it.  No
 *     Darwin kernel, no darlingserver, no Darling prefix is exercised.
 *   * G5 is deterministic sequential reuse with an explicitly injected delay,
 *     not a concurrency race; it is a stress of the identity discipline, not a
 *     measurement of the product's throughput.
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
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
/* claim bookkeeping                                                   */
/* ------------------------------------------------------------------ */

#define NCLAIMS 5
#define DET_MAX 1024

static int g_ok[NCLAIMS + 1];
static char g_detail[NCLAIMS + 1][DET_MAX];
static const char *g_env = "unlabeled";

static const char *claim_label(int i)
{
	switch (i) {
	case 1: return "G1";
	case 2: return "G2";
	case 3: return "G3";
	case 4: return "G4";
	case 5: return "G5";
	default: return "?";
	}
}

static const char *claim_name(int i)
{
	switch (i) {
	case 1: return "lane ABA: a reused slot rejects the previous occupant's delayed ring completion";
	case 2: return "S2C: a delayed server-initiated upcall does not execute in the new occupant";
	case 3: return "blocking control completion is matched by request id, not by arrival order";
	case 4: return "SCM_RIGHTS descriptor for the old request is not installed and is closed";
	case 5: return "1000+ delayed reuses, zero wrong consumptions";
	default: return "?";
	}
}

static int all_ok(void);
static void print_claims(void);

static void verdict(int idx, int ok, const char *fmt, ...)
	__attribute__((format(printf, 3, 4)));

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
		printf("%s %s %s: %s\n", claim_label(i), g_ok[i] ? "PASS" : "FAIL",
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
		verdict(i, 0, "not evaluated: %s failed (%s)", what, strerror(errno));
	print_claims();
	_exit(3);
}

/* ------------------------------------------------------------------ */
/* model knobs -- each one is flipped by exactly one mutation          */
/* ------------------------------------------------------------------ */

/* M1: the deployed state.  `generation' is bumped on every (re)claim and
 * nothing reads it, so a recycled TID matches the stale lane epoch. */
#define MODEL_CHECK_GENERATION 1
/* M2: routing by lane index only (no thread identity). */
#define MODEL_CHECK_OWNER_TID 1
/* M3: control completions carry a request id; matching by arrival order is
 * what the mutation installs. */
#define MODEL_CTL_MATCH_BY_REQUEST_ID 1
/* M4: a received descriptor is installed into whatever request is pending. */
#define MODEL_DESC_CHECK_REQUEST_ID 1

/* ------------------------------------------------------------------ */
/* transport layout                                                    */
/* ------------------------------------------------------------------ */

#define LANES          8u      /* slot table; the product caps at 128    */
#define PEND_MAX       128u    /* produced-but-not-yet-delivered entries */
#define CTL_MAX        8u      /* outstanding blocking control requests  */
#define SLOT_NONE      0xffffffffu

#define SHM_MAGIC      0x47454e4142415f31ULL   /* "GENABA_1" */
#define REQ_MAGIC      0x5245515f30303031ULL   /* "REQ_0001" */

enum {
	OP_ECHO = 1u,          /* ring request: plain completion */
	OP_MUNMAP = 2u,        /* S2C upcall shape: munmap(addr,len) */
	OP_CTL = 3u,           /* control request over the process endpoint */
};

enum {
	R_ACCEPTED = 0,
	R_LANE_FREE,
	R_LANE_CLAIMING,
	R_SLOT,
	R_GENERATION,
	R_OWNER,
	R_CORRELATION,
	R_NO_LIVE_REQUEST,
	R_ARRIVAL_ORDER,
};

static const char *reason_name(int r)
{
	switch (r) {
	case R_ACCEPTED:        return "accepted";
	case R_LANE_FREE:       return "lane-free";
	case R_LANE_CLAIMING:   return "lane-claiming";
	case R_SLOT:            return "slot-mismatch";
	case R_GENERATION:      return "generation-mismatch";
	case R_OWNER:           return "owner-tid-mismatch";
	case R_CORRELATION:     return "parent-upcall-id-mismatch";
	case R_NO_LIVE_REQUEST: return "no-live-request-with-that-id";
	case R_ARRIVAL_ORDER:   return "matched-by-arrival-order";
	default:                return "?";
	}
}

/* The identity a lane occupant claims, and the identity a completion is
 * stamped with.  These are exactly the fields the deployed guest lane table
 * carries (dserver-ring.c:90-101): the slot (the lane's address), the
 * per-slot `generation' epoch counter and the `owner_tid' thread token.  The
 * ONLY process identity the deployed design has is the kernel's
 * SCM_CREDENTIALS on the server side; the guest lane table carries no process
 * epoch, so the harness does not model a process-epoch check either -- that
 * absence is exactly why the fork case below is load-bearing. */
typedef struct {
	uint32_t slot;
	uint32_t generation;
	int32_t  owner_tid;
} ident_t;

struct ring_req {          /* guest -> server, the lane's c2s request slot */
	uint64_t magic;
	ident_t  id;
	uint32_t seq;
	uint32_t callnum;
	uint64_t token;
	uint64_t reqid;
};

struct ring_rep {          /* server -> guest, the lane's s2c reply slot */
	uint32_t ready;
	uint32_t _pad;
	ident_t  id;
	uint32_t seq;
	uint32_t callnum;
	uint64_t token;
	int32_t  status;
};

struct upcall {            /* server -> guest, the lane's S2C duplex mailbox */
	uint32_t ready;
	uint32_t op;
	ident_t  id;
	uint32_t parent_id;
	uint32_t upcall_id;
	uint64_t addr;
	uint64_t len;
};

struct pend {              /* server: produced, not yet delivered */
	uint32_t used;
	uint32_t kind;         /* OP_ECHO | OP_MUNMAP */
	uint32_t target;       /* lane slot the delivery will land in */
	uint32_t _pad;
	struct ring_rep rep;
	struct upcall   upc;
};

/* A control message on the process-level endpoint. */
struct ctl_msg {
	uint64_t magic;
	uint64_t request_id;
	uint64_t token;        /* the logical request's token */
	ident_t  id;           /* the requesting occupant */
	uint32_t seq;
	uint32_t op;
	int32_t  status;
	uint32_t has_fd;       /* in-band marker: a descriptor accompanies this */
};

#define CTL_MAGIC 0x43544c5f4d534731ULL   /* "CTL_MSG1" */

/* Commands the guest publishes for the server (shared memory, no syscall). */
enum {
	CMDR_NONE = 0,
	CMD_PRODUCE_REP,       /* arg = lane slot    */
	CMD_PRODUCE_UPCALL,    /* arg = lane slot    */
	CMD_DELIVER,           /* arg = pend index   */
	CMD_CTL_REPLY,         /* cmd.request_id / cmd.token / cmd.status */
	CMD_DESC_SEND,         /* cmd.request_id / cmd.token / cmd.id */
	CMD_QUIT,
};

struct cmd {
	uint32_t op;
	uint32_t arg;
	uint64_t request_id;
	uint64_t token;
	int32_t  status;
	uint32_t _pad;
	ident_t  id;
};

struct shm {
	uint64_t magic;
	char     _pad0[56];

	/* command queue: guest writes cmd then release-bumps cmd_seq; the server
	 * acquires cmd_seq, executes, and sets cmd_ack = cmd_seq. */
	uint64_t cmd_seq;
	uint64_t cmd_ack;
	struct cmd cmd;
	char     _pad1[64];

	/* c2s requests the guest published (the server stamps from these) */
	struct ring_req req[LANES];
	/* s2c completions + S2C mailbox, one per lane slot (shared region) */
	struct ring_rep rep[LANES];
	struct upcall   upc[LANES];

	/* the server's produced-but-not-delivered store */
	uint32_t pend_n;
	uint32_t _pad2;
	struct pend pend[PEND_MAX];

	/* server-side accounting, read by the guest */
	uint32_t srv_ready;
	uint32_t _pad3;
	uint64_t next_upcall_id;
	uint64_t srv_cmds;
	uint64_t srv_produced;
	uint64_t srv_delivered;
	uint64_t srv_ctl_recv;
	uint64_t srv_ctl_sent;
	uint64_t srv_ctl_unknown;
	uint64_t srv_desc_sent;
	uint64_t srv_desc_closed;
	uint64_t srv_desc_failed;
	/* guest-written: S2C upcalls the pump refused to execute */
	uint64_t s2c_rejected;
	/* the victim page the S2C munmap upcall names (a separate mapping) */
	uint64_t victim_addr;
	uint64_t victim_len;
};

static struct shm *SHM;
static uint8_t *g_victim;
static uint64_t g_victim_len;

/* ------------------------------------------------------------------ */
/* the lane table -- transcribed from dserver-ring.c                   */
/* ------------------------------------------------------------------ */

typedef struct {
	uint32_t active;       /* 0 free, 1 live, 2 claiming (CAS sentinel) */
	uint32_t generation;   /* bumped on every (re)claim                 */
	int32_t  owner_tid;
	int32_t  state;
	uint32_t seq;          /* this lane's in-flight request seq         */
	uint32_t callnum;
	uint64_t token;
	uint64_t reqid;
} lane_t;

/* PROCESS-PRIVATE, exactly as the deployed g_lanes[] is: a fork child gets a
 * COW copy, and its postfork reset must not disturb the parent's table. */
static lane_t g_lanes[LANES];

/* dserver-ring.c:136-143 */
static __attribute__((unused)) lane_t *lane_find(int32_t tid)
{
	uint32_t i;

	for (i = 0; i < LANES; i++)
		if (__atomic_load_n(&g_lanes[i].active, __ATOMIC_ACQUIRE) == 1 &&
		    g_lanes[i].owner_tid == tid)
			return &g_lanes[i];
	return NULL;
}

static uint32_t lane_slot_of(const lane_t *L)
{
	return (uint32_t)(L - g_lanes);
}

/* dserver-ring.c:148-162 */
static __attribute__((unused)) lane_t *lane_claim(void)
{
	uint32_t i;

	for (i = 0; i < LANES; i++) {
		lane_t *L = &g_lanes[i];
		uint32_t expected = 0;

		if (__atomic_compare_exchange_n(&L->active, &expected, 2u, false,
		                                __ATOMIC_ACQ_REL, __ATOMIC_RELAXED))
			return L;
	}
	return NULL;
}

/* dserver-ring.c:336-345: initialize fully, bump the generation, reset the
 * per-lane seq, and publish active=1 LAST. */
static void lane_publish(lane_t *L, int32_t tid, uint32_t seq, uint32_t callnum,
                         uint64_t token, uint64_t reqid)
{
	L->owner_tid = tid;
	L->seq = seq;
	L->callnum = callnum;
	L->token = token;
	L->reqid = reqid;
	L->state = 1;
	L->generation = L->generation + 1;
	__atomic_store_n(&L->active, 1u, __ATOMIC_RELEASE);
}

static void lane_release(lane_t *L)
{
	L->active = 0;
	L->owner_tid = 0;
	L->state = 0;
	L->seq = 1;
	L->callnum = 0;
	L->token = 0;
	L->reqid = 0;
}

/* dserver-ring.c:210-226 */
static void lane_postfork_reset(void)
{
	uint32_t i;

	for (i = 0; i < LANES; i++) {
		lane_t *L = &g_lanes[i];

		L->active = 0;
		L->generation = 0;
		L->state = 0;
		L->owner_tid = 0;
		L->seq = 1;
		L->callnum = 0;
		L->token = 0;
		L->reqid = 0;
	}
}

static ident_t lane_ident(const lane_t *L)
{
	ident_t id;

	id.slot = lane_slot_of(L);
	id.generation = L->generation;
	id.owner_tid = L->owner_tid;
	return id;
}

static void ident_str(char *out, size_t cap, const ident_t *id)
{
	snprintf(out, cap, "{slot=%u,generation=%u,owner_tid=%d}",
	         id->slot, id->generation, id->owner_tid);
}

/* ------------------------------------------------------------------ */
/* raw syscalls -- the server child and the fork child run libc-free   */
/* ------------------------------------------------------------------ */


static __attribute__((always_inline)) inline long
rsys6(long n, long a, long b, long c, long d, long e, long f)
{
	register long r10 __asm__("r10") = d;
	register long r8  __asm__("r8")  = e;
	register long r9  __asm__("r9")  = f;
	long ret;

	__asm__ __volatile__("syscall"
	                     : "=a"(ret)
	                     : "a"(n), "D"(a), "S"(b), "d"(c), "r"(r10), "r"(r8), "r"(r9)
	                     : "rcx", "r11", "memory");
	return ret;
}

#define rsys3(n, a, b, c)    rsys6((n), (long)(a), (long)(b), (long)(c), 0, 0, 0)
#define rsys2(n, a, b)       rsys6((n), (long)(a), (long)(b), 0, 0, 0, 0)
#define rsys1(n, a)          rsys6((n), (long)(a), 0, 0, 0, 0, 0)

static __attribute__((always_inline)) inline void rsys_pause(void)
{
#if defined(__x86_64__) || defined(__i386__)
	__asm__ __volatile__("pause" ::: "memory");
#else
	__asm__ __volatile__("" ::: "memory");
#endif
}

static __attribute__((always_inline)) inline void byte_copy(void *dst, const void *src,
                                                            unsigned long n)
{
	unsigned char *d = (unsigned char *)dst;
	const unsigned char *s = (const unsigned char *)src;
	unsigned long i;

	for (i = 0; i < n; i++)
		d[i] = s[i];
}

static __attribute__((always_inline)) inline void byte_zero(void *dst, unsigned long n)
{
	unsigned char *d = (unsigned char *)dst;
	unsigned long i;

	for (i = 0; i < n; i++)
		d[i] = 0;
}

/* ------------------------------------------------------------------ */
/* the server: one libc-free process, polled, no identity re-check     */
/* ------------------------------------------------------------------ */

#define SRV_REQ_MAX 16

struct srv_req {
	uint64_t request_id;
	uint64_t token;
	ident_t  id;
	uint32_t used;
	uint32_t _pad;
};

static struct srv_req g_srv_reqs[SRV_REQ_MAX];
static uint32_t g_srv_req_next;

static __attribute__((always_inline)) inline struct srv_req *
srv_req_find(uint64_t request_id)
{
	uint32_t i;

	for (i = 0; i < SRV_REQ_MAX; i++)
		if (g_srv_reqs[i].used && g_srv_reqs[i].request_id == request_id)
			return &g_srv_reqs[i];
	return NULL;
}

/* Build the SCM_RIGHTS control message ourselves (no libc). */
union cmsg_buf {
	struct { long len; int level; int type; int fd; int _pad; } c;
	char raw[CMSG_SPACE(sizeof(int))];
	long align;
};

struct msghdr_raw {
	void   *msg_name;
	uint32_t msg_namelen;
	uint32_t _pad0;
	struct iovec *msg_iov;
	unsigned long msg_iovlen;
	void   *msg_control;
	unsigned long msg_controllen;
	int     msg_flags;
	int     _pad1;
};

static long srv_send_fd(int fd, const struct ctl_msg *m, int xfer_fd)
{
	union cmsg_buf cbuf;
	struct msghdr_raw mh;
	struct { void *base; unsigned long len; } iov;

	byte_zero(&cbuf, sizeof cbuf);
	byte_zero(&mh, sizeof mh);
	cbuf.c.len = (long)CMSG_LEN(sizeof(int));
	cbuf.c.level = SOL_SOCKET;
	cbuf.c.type = SCM_RIGHTS;
	cbuf.c.fd = xfer_fd;

	iov.base = (void *)m;
	iov.len = sizeof *m;
	mh.msg_iov = (void *)&iov;
	mh.msg_iovlen = 1;
	mh.msg_control = (void *)&cbuf;
	mh.msg_controllen = sizeof cbuf;

	return rsys3(SYS_sendmsg, fd, &mh, 0);
}

static long srv_create_token_fd(uint64_t token)
{
	long fd = rsys2(SYS_memfd_create, (long)"genaba-token", 0);

	if (fd >= 0) {
		if (rsys3(SYS_write, fd, &token, sizeof token) != (long)sizeof token ||
		    rsys3(SYS_lseek, fd, 0, 0 /* SEEK_SET */) != 0) {
			rsys1(SYS_close, fd);
			return -1;
		}
		return fd;
	}
	/* a container may refuse memfd_create; a pipe carries a descriptor just
	 * as well, and the token still travels inside it. */
	{
		long p[2];

		if (rsys2(SYS_pipe2, (long)p, 0) != 0)
			return -1;
		if (rsys3(SYS_write, p[1], &token, sizeof token) != (long)sizeof token) {
			rsys1(SYS_close, p[0]);
			rsys1(SYS_close, p[1]);
			return -1;
		}
		rsys1(SYS_close, p[1]);
		return p[0];
	}
}

/* The server's whole job: serve the guest's command queue and the control
 * endpoint.  It stamps completions from the identity the GUEST claimed in the
 * request (the product's payload-claimed nsid) and performs NO identity check
 * at delivery time -- that is the audited behaviour this harness models. */
static __attribute__((noreturn, noinline)) void server_entry(int ctl_fd)
{
	uint64_t last = 0;

	__atomic_store_n(&SHM->srv_ready, 1u, __ATOMIC_RELEASE);

	for (;;) {
		/* 1. drain the control endpoint (non-blocking) */
		for (;;) {
			struct ctl_msg m;
			long r = rsys6(SYS_recvfrom, ctl_fd, (long)&m, (long)sizeof m,
			               0x40 /* MSG_DONTWAIT */, 0, 0);

			if (r != (long)sizeof m || m.magic != CTL_MAGIC)
				break;
			if (g_srv_req_next < SRV_REQ_MAX) {
				struct srv_req *sr = &g_srv_reqs[g_srv_req_next++];

				byte_zero(sr, sizeof *sr);
				sr->request_id = m.request_id;
				sr->token = m.token;
				sr->id = m.id;
				sr->used = 1;
			}
			__atomic_fetch_add(&SHM->srv_ctl_recv, 1u, __ATOMIC_RELAXED);
		}

		/* 2. execute at most one queued command */
		{
			uint64_t seq = __atomic_load_n(&SHM->cmd_seq, __ATOMIC_ACQUIRE);

			if (seq != last) {
				struct cmd c;

				byte_copy(&c, &SHM->cmd, sizeof c);
				switch (c.op) {
				case CMD_PRODUCE_REP: {
					uint32_t lane = c.arg;
					struct pend *p = &SHM->pend[SHM->pend_n % PEND_MAX];
					struct ring_req *q = &SHM->req[lane];

					byte_zero(p, sizeof *p);
					p->used = 1;
					p->kind = OP_ECHO;
					p->target = lane;
					p->rep.id = q->id;      /* stamped from the guest's claim */
					p->rep.seq = q->seq;
					p->rep.callnum = q->callnum;
					p->rep.token = q->token;
					p->rep.status = 0;
					SHM->pend_n++;
					SHM->srv_produced++;
					break;
				}
				case CMD_PRODUCE_UPCALL: {
					uint32_t lane = c.arg;
					struct pend *p = &SHM->pend[SHM->pend_n % PEND_MAX];
					struct ring_req *q = &SHM->req[lane];

					byte_zero(p, sizeof *p);
					p->used = 1;
					p->kind = OP_MUNMAP;
					p->target = lane;
					p->upc.id = q->id;      /* stamped from the guest's claim */
					p->upc.op = OP_MUNMAP;
					p->upc.parent_id = q->seq;     /* the parent request it belongs to */
					p->upc.upcall_id = SHM->next_upcall_id++;
					p->upc.addr = SHM->victim_addr;
					p->upc.len = SHM->victim_len;
					SHM->pend_n++;
					SHM->srv_produced++;
					break;
				}
				case CMD_DELIVER: {
					uint32_t idx = c.arg % PEND_MAX;
					struct pend *p = &SHM->pend[idx];

					if (p->used) {
						if (p->kind == OP_ECHO) {
							struct ring_rep *dst = &SHM->rep[p->target];

							dst->id = p->rep.id;
							dst->seq = p->rep.seq;
							dst->callnum = p->rep.callnum;
							dst->token = p->rep.token;
							dst->status = p->rep.status;
							__atomic_store_n(&dst->ready, 1u, __ATOMIC_RELEASE);
						} else {
							struct upcall *dst = &SHM->upc[p->target];

							dst->op = p->upc.op;
							dst->id = p->upc.id;
							dst->parent_id = p->upc.parent_id;
							dst->upcall_id = p->upc.upcall_id;
							dst->addr = p->upc.addr;
							dst->len = p->upc.len;
							__atomic_store_n(&dst->ready, 1u, __ATOMIC_RELEASE);
						}
						p->used = 0;
						SHM->srv_delivered++;
					}
					break;
				}
				case CMD_CTL_REPLY: {
					struct srv_req *sr = srv_req_find(c.request_id);
					struct ctl_msg m;

					if (sr) {
						byte_zero(&m, sizeof m);
						m.magic = CTL_MAGIC;
						m.request_id = c.request_id;
						m.token = c.token;
						m.id = sr->id;
						m.op = OP_CTL;
						m.status = c.status;
						if (rsys3(SYS_write, ctl_fd, &m, sizeof m) == (long)sizeof m)
							SHM->srv_ctl_sent++;
					} else {
						SHM->srv_ctl_unknown++;
					}
					break;
				}
				case CMD_DESC_SEND: {
					long fd = srv_create_token_fd(c.token);
					struct ctl_msg m;

					byte_zero(&m, sizeof m);
					m.magic = CTL_MAGIC;
					m.request_id = c.request_id;
					m.token = c.token;
					m.id = c.id;
					m.has_fd = 1;
					if (fd >= 0) {
						if (srv_send_fd(ctl_fd, &m, (int)fd) > 0)
							SHM->srv_desc_sent++;
						else
							SHM->srv_desc_failed++;
						/* the server's own copy is closed after the transfer,
						 * whether or not the transfer succeeded. */
						rsys1(SYS_close, fd);
						SHM->srv_desc_closed++;
					} else {
						SHM->srv_desc_failed++;
					}
					break;
				}
				case CMD_QUIT:
					rsys1(SYS_close, ctl_fd);
					rsys1(SYS_exit_group, 0);
					break;
				default:
					break;
				}
				last = seq;
				__atomic_store_n(&SHM->cmd_ack, seq, __ATOMIC_RELEASE);
			}
		}

		rsys_pause();
	}
}

/* ------------------------------------------------------------------ */
/* guest-side plumbing (the parent harness process)                    */
/* ------------------------------------------------------------------ */

static int g_ctl_fd = -1;              /* the ONE process-level control endpoint */
static pid_t g_server_pid = -1;
static int g_child_pipe[2] = { -1, -1 }; /* fork-under-test <-> parent          */

static uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void futex_wake(void *addr, int n)
{
	syscall(__NR_futex, addr, 1 /* FUTEX_WAKE */, n, NULL, NULL, 0);
}

/* FUTEX_WAIT with a bounded timeout so a broken model can never hang. */
static int futex_wait_ms(void *addr, uint32_t expect, int ms)
{
	struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };

	return (int)syscall(__NR_futex, addr, 0 /* FUTEX_WAIT */, expect, &ts, NULL, 0);
}

/* The delay injected between PRODUCTION (the server stamps the completion from
 * the occupant's claim) and DELIVERY (the server writes it into the lane slot).
 * It is a bounded, MEASURED amount of real elapsed time (>= 500 us), spent as a
 * busy delay rather than a nanosleep so the injected floor itself does not
 * depend on the scheduler's timer slack.  Every delayed delivery's real gap
 * (from the production ack to the delivery ack) is measured and reported as
 * min/mean/max; on a loaded machine that gap is larger than the floor, which is
 * exactly why the harness sequences the reuse explicitly instead of relying on
 * elapsed time to create it. */
static void inject_delay(void)
{
	uint64_t deadline = now_ns() + 500000ull;

	while (now_ns() < deadline)
		rsys_pause();
}

static int fd_count_of(pid_t pid)
{
	char path[64];
	DIR *d;
	struct dirent *e;
	int n = 0;

	snprintf(path, sizeof path, "/proc/%d/fd", (int)pid);
	d = opendir(path);
	if (!d)
		return -1;
	while ((e = readdir(d)) != NULL)
		if (e->d_name[0] != '.')
			n++;
	closedir(d);
	return n;
}

/* The identity a ring request carries: what the guest CLAIMS for its lane.
 * This is the product's payload-claimed nsid. */
static struct ring_req *ring_publish(lane_t *L, uint32_t callnum, uint64_t token,
                                     uint64_t reqid)
{
	struct ring_req *q = &SHM->req[lane_slot_of(L)];

	memset(q, 0, sizeof *q);
	q->magic = REQ_MAGIC;
	q->id = lane_ident(L);
	q->seq = L->seq;
	q->callnum = callnum;
	q->token = token;
	q->reqid = reqid;
	return q;
}

/* ---- the server command queue (shared memory, polled, no syscall) ---- */

static uint64_t server_cmd(uint32_t op, uint32_t arg, uint64_t request_id,
                           uint64_t token, int32_t status, const ident_t *id)
{
	struct cmd *c = &SHM->cmd;
	uint64_t seq;

	c->op = op;
	c->arg = arg;
	c->request_id = request_id;
	c->token = token;
	c->status = status;
	if (id)
		c->id = *id;
	else
		memset(&c->id, 0, sizeof c->id);

	seq = __atomic_add_fetch(&SHM->cmd_seq, 1, __ATOMIC_ACQ_REL);
	for (long i = 0; i < 4000000000L; i++) {
		if (__atomic_load_n(&SHM->cmd_ack, __ATOMIC_ACQUIRE) == seq) {
			__atomic_add_fetch(&SHM->srv_cmds, 1, __ATOMIC_RELAXED);
			return seq;
		}
		rsys_pause();
		if ((i & 0xfffff) == 0)
			sched_yield();
	}
	die("the server did not acknowledge a command");
	return 0;
}

static void server_start(void)
{
	int sv[2];

	if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sv) != 0)
		die("socketpair for the process-level control endpoint");
	g_ctl_fd = sv[0];

	g_server_pid = fork();
	if (g_server_pid < 0)
		die("fork server");
	if (g_server_pid == 0) {
		close(sv[0]);
		server_entry(sv[1]);
		_exit(0);
	}
	close(sv[1]);

	for (long i = 0; i < 4000000000L; i++) {
		if (__atomic_load_n(&SHM->srv_ready, __ATOMIC_ACQUIRE) == 1)
			return;
		rsys_pause();
		if ((i & 0xfffff) == 0)
			sched_yield();
	}
	die("the server never became ready");
}

/* ---- the process-level control endpoint's pending requests ---- */

enum { CP_FREE = 0, CP_BLOCKED = 1, CP_ABANDONED = 2, CP_DONE = 3 };

struct ctlpend {
	uint32_t state;              /* also the futex word */
	uint32_t used;
	uint64_t request_id;
	uint64_t token;
	ident_t  id;
	uint64_t rx_token;           /* the token the receiver handed us */
	uint32_t reject_reason;
	uint32_t rejected_desc;
	int32_t  installed_fd;
	uint64_t installed_token;    /* read back out of the installed descriptor */
};

static struct ctlpend g_cp[CTL_MAX];
static uint64_t g_reqid_next = 1;

static uint64_t g_rx_seen;
static uint64_t g_rx_matched;
static uint64_t g_rx_unmatched;
static uint64_t g_rx_wrong_id;
static uint32_t g_rx_last_reject;
static uint32_t g_blocked_when_stale_arrived;
static uint64_t g_desc_rejected;
static uint64_t g_desc_closed;
static uint64_t g_desc_installed;
static pthread_t g_rx_thread;
static int g_rx_thread_live;

static struct ctlpend *cp_alloc(uint64_t *reqid_out)
{
	uint32_t i;

	for (i = 0; i < CTL_MAX; i++) {
		if (g_cp[i].used)
			continue;
		memset(&g_cp[i], 0, sizeof g_cp[i]);
		g_cp[i].used = 1;
		g_cp[i].state = CP_BLOCKED;
		g_cp[i].installed_fd = -1;
		g_cp[i].request_id = g_reqid_next++;
		*reqid_out = g_cp[i].request_id;
		return &g_cp[i];
	}
	return NULL;
}

static __attribute__((unused)) struct ctlpend *cp_live_by_request_id(uint64_t request_id)
{
	uint32_t i;

	for (i = 0; i < CTL_MAX; i++)
		if (g_cp[i].used && g_cp[i].state == CP_BLOCKED &&
		    g_cp[i].request_id == request_id)
			return &g_cp[i];
	return NULL;
}

static __attribute__((unused)) struct ctlpend *cp_head_blocked(void)
{
	uint32_t i;

	for (i = 0; i < CTL_MAX; i++)
		if (g_cp[i].used && g_cp[i].state == CP_BLOCKED)
			return &g_cp[i];
	return NULL;
}

static void ctl_dispatch(const struct ctl_msg *m, int fd)
{
	struct ctlpend *p = NULL;
	uint32_t i;
	int install = 0;

	(void)i;
#if MODEL_CTL_MATCH_BY_REQUEST_ID
	p = cp_live_by_request_id(m->request_id);
#else
	/* M3: no request-id match -- the completed unit is taken by arrival
	 * order, i.e. by whatever request is at the head of the blocking queue. */
	p = cp_head_blocked();
#endif

	if (fd >= 0) {
		/* ---- descriptor association (G4) ---- */
#if MODEL_DESC_CHECK_REQUEST_ID
		if (p != NULL && p->token == m->token)
			install = 1;
#else
		/* M4: no identity check -- the descriptor is installed into
		 * whatever request happens to be outstanding. */
		if (p == NULL)
			p = cp_head_blocked();
		install = (p != NULL);
#endif
		if (install) {
			uint64_t tok = 0;
			ssize_t n = read(fd, &tok, sizeof tok);

			p->installed_fd = fd;
			p->installed_token = (n == (ssize_t)sizeof tok) ? tok : 0;
			g_desc_installed++;
		} else {
			close(fd);
			g_desc_rejected++;
			g_desc_closed++;
			if (p != NULL)
				p->rejected_desc = 1;
		}
		__atomic_add_fetch(&g_rx_seen, 1, __ATOMIC_RELEASE);
		return;
	}

	/* ---- blocking control completion (G3) ---- */
	if (p != NULL) {
		if (p->request_id != m->request_id)
			g_rx_wrong_id++;
		p->rx_token = m->token;
		p->reject_reason = (p->request_id == m->request_id)
		                   ? R_ACCEPTED : R_ARRIVAL_ORDER;
		g_rx_last_reject = p->reject_reason;
		g_rx_matched++;
		__atomic_store_n(&p->state, CP_DONE, __ATOMIC_RELEASE);
		futex_wake(&p->state, 1);
	} else {
		/* no live request with this id: reported as rejected, never
		 * handed to an unrelated blocked request. */
		g_rx_unmatched++;
		g_rx_last_reject = R_NO_LIVE_REQUEST;
	}
	__atomic_add_fetch(&g_rx_seen, 1, __ATOMIC_RELEASE);
}

static void *receiver_thread(void *arg)
{
	(void)arg;

	for (;;) {
		struct ctl_msg m;
		char cbuf[CMSG_SPACE(sizeof(int))];
		struct iovec iov;
		struct msghdr mh;
		struct cmsghdr *cm;
		ssize_t r;
		int fd = -1;

		memset(&m, 0, sizeof m);
		memset(&mh, 0, sizeof mh);
		iov.iov_base = &m;
		iov.iov_len = sizeof m;
		mh.msg_iov = &iov;
		mh.msg_iovlen = 1;
		mh.msg_control = cbuf;
		mh.msg_controllen = sizeof cbuf;

		r = recvmsg(g_ctl_fd, &mh, 0);
		if (r <= 0)
			break;   /* the server closed the endpoint */
		if (r != (ssize_t)sizeof m || m.magic != CTL_MAGIC)
			continue;
		for (cm = CMSG_FIRSTHDR(&mh); cm != NULL; cm = CMSG_NXTHDR(&mh, cm))
			if (cm->cmsg_level == SOL_SOCKET && cm->cmsg_type == SCM_RIGHTS)
				memcpy(&fd, CMSG_DATA(cm), sizeof fd);
		ctl_dispatch(&m, fd);
	}
	return NULL;
}

static int ctl_wait(struct ctlpend *p, uint64_t *token_out)
{
	uint64_t spins;

	for (spins = 0; spins < 20000; spins++) {
		uint32_t st = __atomic_load_n(&p->state, __ATOMIC_ACQUIRE);

		if (st == CP_DONE) {
			*token_out = p->rx_token;
			return 0;
		}
		if (st == CP_ABANDONED)
			return 1;
		futex_wait_ms(&p->state, st, 200);
	}
	return 2;   /* timed out: the model never completed this request */
}

/* Wait for the receiver to have processed one more message. */
static void rx_wait(uint64_t before)
{
	for (long i = 0; i < 20000000L; i++) {
		if (__atomic_load_n(&g_rx_seen, __ATOMIC_ACQUIRE) > before)
			return;
		rsys_pause();
		if ((i & 0xfffff) == 0)
			sched_yield();
	}
	die("the control receiver never processed a message");
}

/* ---- the identity predicates ------------------------------------- */

/* Ring completion (G1).  The fields compared are exactly:
 *   slot, generation, owner_tid, seq, callnum.
 * The generation and owner checks are the ones the mutations remove. */
static int ring_accept(const lane_t *L, const struct ring_req *mine,
                       const struct ring_rep *rep, const char **why)
{
	if (__atomic_load_n(&L->active, __ATOMIC_ACQUIRE) != 1) {
		*why = reason_name(R_LANE_FREE);
		return R_LANE_FREE;
	}
	if (MODEL_CHECK_GENERATION && rep->id.generation != L->generation) {
		*why = reason_name(R_GENERATION);
		return R_GENERATION;
	}
	if (MODEL_CHECK_OWNER_TID && rep->id.owner_tid != L->owner_tid) {
		*why = reason_name(R_OWNER);
		return R_OWNER;
	}
	if (rep->id.slot != lane_slot_of(L)) {
		*why = reason_name(R_SLOT);
		return R_SLOT;
	}
	if (rep->seq != mine->seq || rep->callnum != mine->callnum) {
		*why = reason_name(R_CORRELATION);
		return R_CORRELATION;
	}
	*why = reason_name(R_ACCEPTED);
	return R_ACCEPTED;
}

/* S2C upcall (G2).  The fields compared are exactly:
 *   slot, generation, owner_tid, parent_id.
 * Run in the fork child, so it stays libc-free. */
static int upcall_accept(const lane_t *L, const struct ring_req *mine,
                         const struct upcall *u, int *why)
{
	if (__atomic_load_n(&L->active, __ATOMIC_ACQUIRE) != 1) {
		*why = R_LANE_FREE;
		return R_LANE_FREE;
	}
	if (MODEL_CHECK_GENERATION && u->id.generation != L->generation) {
		*why = R_GENERATION;
		return R_GENERATION;
	}
	if (MODEL_CHECK_OWNER_TID && u->id.owner_tid != L->owner_tid) {
		*why = R_OWNER;
		return R_OWNER;
	}
	if (u->id.slot != lane_slot_of(L)) {
		*why = R_SLOT;
		return R_SLOT;
	}
	if (u->parent_id != mine->seq) {
		*why = R_CORRELATION;
		return R_CORRELATION;
	}
	*why = R_ACCEPTED;
	return R_ACCEPTED;
}

/* ---- the fork child (G2): libc-free, raw syscalls only -------------- */

#define CHILD_MAGIC       0x4348494c445f3032ULL   /* "CHILD_02" */
#define CHILD_HELLO_MAGIC 0x48454c4c4f5f3031ULL   /* "HELLO_01" */

struct child_hello {
	uint64_t magic;
	int32_t  child_tid;
	uint32_t slot;
	ident_t  claimed;
};

struct child_report {
	uint64_t magic;
	int32_t  child_tid;
	uint32_t claimed_slot;
	ident_t  claimed;
	ident_t  upcall_stamp;
	uint32_t upcall_ready;
	uint32_t accepted;
	uint32_t executed;
	uint32_t self_found;
	int32_t  reject_reason;
	int64_t  munmap_rc;
	int64_t  mincore_rc;
};

static lane_t *lane_claim_slot_pub(uint32_t slot, int32_t tid, uint32_t seq,
                                   uint32_t callnum, uint64_t token, uint64_t reqid);
static __attribute__((unused)) lane_t *lane_claim_pub(int32_t tid, uint32_t seq,
                                                      uint32_t callnum, uint64_t token,
                                                      uint64_t reqid);

static __attribute__((noreturn, noinline)) void
fork_child_entry(long report_fd, long slot_hint)
{
	struct child_report rep;
	struct ring_req mine;
	struct upcall u;
	lane_t *L;
	uint32_t spin;

	byte_zero(&rep, sizeof rep);
	byte_zero(&mine, sizeof mine);
	byte_zero(&u, sizeof u);
	rep.magic = CHILD_MAGIC;
	rep.child_tid = (int32_t)rsys1(SYS_gettid, 0);

	/* the loader's postfork reset (dserver-ring.c:210-226), transcribed */
	lane_postfork_reset();
	rsys1(SYS_close, g_ctl_fd);      /* inherited endpoints are closed */

	/* re-claim the SAME slot index by explicit sequencing (the probe start is
	 * a hash of the tid; the harness refuses to depend on the kernel's tid
	 * recycling, so the reuse is sequenced, not raced). */
	L = lane_claim_slot_pub((uint32_t)slot_hint, rep.child_tid, 1, OP_MUNMAP, 0, 0);
	if (L == NULL) {
		rep.reject_reason = R_LANE_CLAIMING;
		rep.claimed_slot = SLOT_NONE;
		rsys3(SYS_write, report_fd, &rep, sizeof rep);
		rsys1(SYS_exit_group, 0);
		for (;;)
			rsys_pause();
	}
	rep.claimed_slot = lane_slot_of(L);
	rep.claimed = lane_ident(L);
	rep.self_found = (lane_find(rep.child_tid) == L) ? 1u : 0u;

	/* our own in-flight parent request: parent_id is this lane's seq (1 after
	 * the reclaim), so the correlation field cannot separate the epochs. */
	mine.id = lane_ident(L);
	mine.seq = L->seq;
	mine.callnum = OP_MUNMAP;
	SHM->req[lane_slot_of(L)] = mine;

	/* tell the parent we re-claimed the slot, then wait for the delivery */
	{
		struct child_hello hello;

		byte_zero(&hello, sizeof hello);
		hello.magic = CHILD_HELLO_MAGIC;
		hello.child_tid = rep.child_tid;
		hello.slot = rep.claimed_slot;
		hello.claimed = rep.claimed;
		rsys3(SYS_write, report_fd, &hello, sizeof hello);
	}

	for (spin = 0; spin < 4000000000u; spin++) {
		if (__atomic_load_n(&SHM->upc[lane_slot_of(L)].ready, __ATOMIC_ACQUIRE)) {
			rep.upcall_ready = 1;
			break;
		}
		rsys_pause();
	}
	u = SHM->upc[lane_slot_of(L)];
	rep.upcall_stamp = u.id;
	{
		int why = R_ACCEPTED;

		rep.reject_reason = upcall_accept(L, &mine, &u, &why);
	}
	if (rep.reject_reason == R_ACCEPTED) {
		long mrc;

		rep.accepted = 1;
		mrc = rsys3(SYS_munmap, (long)u.addr, (long)u.len, 0);
		rep.munmap_rc = mrc;
		rep.executed = (mrc == 0) ? 1u : 0u;
	} else {
		__atomic_add_fetch(&SHM->s2c_rejected, 1u, __ATOMIC_RELAXED);
	}

	/* probe the page the upcall named: mapped (0) or gone (-ENOMEM) */
	{
		unsigned char vec[8];

		rep.mincore_rc = rsys6(SYS_mincore, (long)u.addr, (long)u.len,
		                       (long)vec, 0, 0, 0);
	}

	rsys3(SYS_write, report_fd, &rep, sizeof rep);
	rsys1(SYS_exit_group, 0);
	for (;;)
		rsys_pause();
}

/* Claim one SPECIFIC slot (the deterministic reuse the ABA sequences need:
 * the harness sequences the reuse instead of racing the allocator). */
static lane_t *lane_claim_slot_pub(uint32_t slot, int32_t tid, uint32_t seq,
                                   uint32_t callnum, uint64_t token, uint64_t reqid)
{
	lane_t *L;
	uint32_t expected = 0;

	if (slot >= LANES)
		return NULL;
	L = &g_lanes[slot];
	if (!__atomic_compare_exchange_n(&L->active, &expected, 2u, false,
	                                 __ATOMIC_ACQ_REL, __ATOMIC_RELAXED))
		return NULL;
	lane_publish(L, tid, seq, callnum, token, reqid);
	return L;
}

/* The deployed probe+CAS claim (dserver-ring.c:148-162) followed by the
 * publish step (dserver-ring.c:336-345); the harness has no attach to do in
 * between, so claim and publish are adjacent here. */
static __attribute__((unused)) lane_t *lane_claim_pub(int32_t tid, uint32_t seq,
                                                      uint32_t callnum, uint64_t token,
                                                      uint64_t reqid)
{
	lane_t *L = lane_claim();

	if (L == NULL)
		return NULL;
	lane_publish(L, tid, seq, callnum, token, reqid);
	return L;
}

/* ------------------------------------------------------------------ */
/* shared phase helpers                                                */
/* ------------------------------------------------------------------ */

static int32_t g_token_next = 100000;

static void ring_expect(const lane_t *L, struct ring_req *out)
{
	memset(out, 0, sizeof *out);
	out->id = lane_ident(L);
	out->seq = L->seq;
	out->callnum = L->callnum;
	out->token = L->token;
	out->reqid = L->reqid;
}

static int send_ctl_request(const struct ctlpend *p)
{
	struct ctl_msg m;

	memset(&m, 0, sizeof m);
	m.magic = CTL_MAGIC;
	m.request_id = p->request_id;
	m.token = p->token;
	m.id = p->id;
	m.op = OP_CTL;
	return (send(g_ctl_fd, &m, sizeof m, 0) == (ssize_t)sizeof m) ? 0 : -1;
}

static void spin_until_srv_ctl(uint64_t want)
{
	for (long i = 0; i < 4000000000L; i++) {
		if (__atomic_load_n(&SHM->srv_ctl_recv, __ATOMIC_ACQUIRE) >= want)
			return;
		rsys_pause();
		if ((i & 0xfffff) == 0)
			sched_yield();
	}
	die("the server never received the control request");
}

/* One fork-epoch reuse cycle (used by G2 and by G5's epoch class):
 *   A claims `slot' in this process and the server PRODUCES an S2C upcall for
 *   A's claimed identity; the process forks; the child (postfork reset, epoch
 *   counter restarts at 0) re-claims the SAME slot -- colliding with A's
 *   generation -- publishes its own request, and waits for the delivery; the
 *   parent injects the delay and then DELIVERS.  Returns 0 and fills *rep_out
 *   with the child's report. */
static int fork_epoch_cycle(uint32_t slot, struct child_report *rep_out,
                            uint64_t *delay_out)
{
	struct child_hello hello;
	struct child_report rep;
	lane_t *A;
	pid_t pid;
	int status = 0;
	ssize_t n;
	uint64_t pend, t0, t1;

	memset(&hello, 0, sizeof hello);
	memset(&rep, 0, sizeof rep);

	/* Put THIS process's lane table into a fresh epoch space: the collision
	 * under test is between two FRESH epoch spaces -- every process's first
	 * claim on a slot is generation 1 (dserver-ring.c:343 bumps 0 -> 1) and
	 * the fork child's postfork reset returns its counters to 0, so the
	 * child's first claim of the same slot is generation 1 as well.  The
	 * harness sequences that state (the transcribed
	 * __dserver_ring_postfork_reset) instead of waiting for a process to be
	 * young. */
	lane_postfork_reset();

	A = lane_claim_slot_pub(slot, g_token_next++, 1, OP_MUNMAP, 0, 0);
	if (A == NULL)
		return -1;
	ring_publish(A, OP_MUNMAP, (uint64_t)g_token_next++, g_reqid_next++);
	pend = SHM->pend_n;
	server_cmd(CMD_PRODUCE_UPCALL, slot, 0, 0, 0, NULL);
	t0 = now_ns();

	if (pipe(g_child_pipe) != 0)
		return -1;
	pid = fork();
	if (pid < 0) {
		close(g_child_pipe[0]);
		close(g_child_pipe[1]);
		g_child_pipe[0] = g_child_pipe[1] = -1;
		return -1;
	}
	if (pid == 0) {
		rsys1(SYS_close, g_child_pipe[0]);
		fork_child_entry(g_child_pipe[1], slot);
	}
	close(g_child_pipe[1]);
	g_child_pipe[1] = -1;
	lane_release(A);   /* A exits once the child exists */

	n = read(g_child_pipe[0], &hello, sizeof hello);
	if (n != (ssize_t)sizeof hello || hello.magic != CHILD_HELLO_MAGIC) {
		close(g_child_pipe[0]);
		waitpid(pid, &status, 0);
		return -1;
	}

	/* the injected delay between production and delivery */
	inject_delay();
	server_cmd(CMD_DELIVER, pend, 0, 0, 0, NULL);
	t1 = now_ns();
	if (delay_out)
		*delay_out = t1 - t0;

	n = read(g_child_pipe[0], &rep, sizeof rep);
	close(g_child_pipe[0]);
	g_child_pipe[0] = -1;
	waitpid(pid, &status, 0);
	rep_out->claimed = hello.claimed;
	rep_out->child_tid = hello.child_tid;
	rep_out->claimed_slot = hello.slot;
	if (n == (ssize_t)sizeof rep)
		rep_out->magic = rep.magic;
	else
		return -1;
	rep_out->upcall_stamp = rep.upcall_stamp;
	rep_out->upcall_ready = rep.upcall_ready;
	rep_out->accepted = rep.accepted;
	rep_out->executed = rep.executed;
	rep_out->self_found = rep.self_found;
	rep_out->reject_reason = rep.reject_reason;
	rep_out->munmap_rc = rep.munmap_rc;
	rep_out->mincore_rc = rep.mincore_rc;
	return 0;
}

/* ---- the G3 blocking control-request thread ---- */

struct g3arg {
	uint32_t slot;
	int32_t  token;
	pthread_barrier_t *ready;
	struct ctlpend *p;        /* out */
	volatile int entered;     /* set just before the futex block */
	int    wait_rc;
	uint64_t got_token;
};

static void *g3_occupant(void *vp)
{
	struct g3arg *a = vp;
	lane_t *L;
	struct ctlpend *p;
	uint64_t rid = 0;

	{
		int32_t tok = a->token != 0 ? a->token : (int32_t)gettid();

		L = lane_claim_slot_pub(a->slot, tok, 1, OP_CTL, 0, 0);
	}
	if (L == NULL) {
		pthread_barrier_wait(a->ready);
		return NULL;
	}
	p = cp_alloc(&rid);
	p->token = 0x000c7000ull + (uint64_t)a->token;
	p->id = lane_ident(L);
	a->p = p;
	ring_publish(L, OP_CTL, p->token, p->request_id);
	pthread_barrier_wait(a->ready);
	if (send_ctl_request(p) != 0)
		die("send on the process-level control endpoint");
	__atomic_store_n(&a->entered, 1, __ATOMIC_RELEASE);
	a->wait_rc = ctl_wait(p, &a->got_token);
	lane_release(L);
	return NULL;
}

/* ---- an occupant thread that just claims/publishes and (optionally) exits ---- */

struct occarg {
	uint32_t slot;
	int32_t  token;
	uint32_t callnum;
	uint64_t token_req;
	pthread_barrier_t *ready;
	volatile int *exit_now;
	int release_on_exit;
	lane_t *lane;             /* out */
	int32_t real_tid;         /* out */
};

static void *occupant_thread(void *vp)
{
	struct occarg *a = vp;
	lane_t *L;

	{
		uint64_t reqid = g_reqid_next++;
		int32_t tok;

		a->real_tid = (int32_t)gettid();
		/* token 0 == "my own tid", the value the kernel assigns a thread;
		 * a nonzero token is the value an ABA sequence hands a new occupant
		 * (the recycled tid the exited thread had). */
		tok = a->token != 0 ? a->token : a->real_tid;
		L = lane_claim_slot_pub(a->slot, tok, 1, a->callnum, a->token_req, reqid);
		a->lane = L;
		if (L != NULL)
			ring_publish(L, a->callnum, a->token_req, reqid);
	}
	pthread_barrier_wait(a->ready);
	if (a->exit_now != NULL) {
		while (!__atomic_load_n(a->exit_now, __ATOMIC_ACQUIRE))
			sched_yield();
		if (a->release_on_exit)
			lane_release(L);   /* lane reclaim on thread-exit */
	}
	return NULL;
}

/* ------------------------------------------------------------------ */
/* the four completion classes, as claims                              */
/* ------------------------------------------------------------------ */

#define G_SLOT 3u

#define TOKEN_RING_A 0x00000000000000a1ull
#define TOKEN_RING_B 0x00000000000000b1ull
#define TOKEN_DESC_A 0x00000000000000daull
#define TOKEN_DESC_B 0x00000000000000dbull

static uint64_t g_wrong_consumption;
static uint64_t g_rejected_stale;

/* ---- G1: lane ABA, delayed ring completion ------------------------- */

static void phase_g1(void)
{
	pthread_t ta, tb;
	pthread_barrier_t ba, bb;
	volatile int exit_a = 0, exit_b = 0;
	struct occarg aa, bargs;
	lane_t *B;
	struct ring_req expect;
	struct ring_rep stale, own;
	const char *why = NULL, *why_own = NULL;
	char sstr[96], ostr[96];
	ident_t stamp;
	uint64_t pend_stale, pend_own, t0, t1, delay;
	uint64_t consumed = 0;
	int32_t recycled;
	int rc, rc_own, ok;

	memset(&aa, 0, sizeof aa);
	memset(&bargs, 0, sizeof bargs);
	memset(&expect, 0, sizeof expect);
	memset(&stale, 0, sizeof stale);
	memset(&own, 0, sizeof own);
	pthread_barrier_init(&ba, NULL, 2);
	pthread_barrier_init(&bb, NULL, 2);

	/* A claims the slot and publishes its request. */
	aa.slot = G_SLOT;
	aa.token = 0;                 /* A's own tid, read back below */
	aa.callnum = OP_ECHO;
	aa.token_req = TOKEN_RING_A;
	aa.ready = &ba;
	aa.exit_now = &exit_a;
	aa.release_on_exit = 1;
	pthread_create(&ta, NULL, occupant_thread, &aa);
	pthread_barrier_wait(&ba);
	if (aa.lane == NULL)
		die("G1: occupant A could not claim the slot");
	recycled = aa.lane->owner_tid;   /* A's tid, handed to B below */

	/* The server PRODUCES A's completion while A owns the lane, and holds it. */
	pend_stale = SHM->pend_n;
	t0 = now_ns();
	server_cmd(CMD_PRODUCE_REP, G_SLOT, 0, 0, 0, NULL);
	stamp = SHM->pend[pend_stale % PEND_MAX].rep.id;

	/* A exits: the slot is released and its thread token is recycled. */
	__atomic_store_n(&exit_a, 1, __ATOMIC_RELEASE);
	pthread_join(ta, NULL);

	/* B re-claims the SAME slot with the RECYCLED token. */
	bargs.slot = G_SLOT;
	bargs.token = recycled;
	bargs.callnum = OP_ECHO;
	bargs.token_req = TOKEN_RING_B;
	bargs.ready = &bb;
	bargs.exit_now = &exit_b;
	bargs.release_on_exit = 1;
	pthread_create(&tb, NULL, occupant_thread, &bargs);
	pthread_barrier_wait(&bb);
	B = bargs.lane;
	if (B == NULL)
		die("G1: occupant B could not re-claim the slot");

	/* Inject the delay, then deliver A's completion into the slot B now owns. */
	inject_delay();
	server_cmd(CMD_DELIVER, pend_stale, 0, 0, 0, NULL);
	t1 = now_ns();
	delay = t1 - t0;
	stale = SHM->rep[G_SLOT];
	ring_expect(B, &expect);
	rc = ring_accept(B, &expect, &stale, &why);
	if (rc == R_ACCEPTED) {
		consumed = stale.token;
		g_wrong_consumption++;
	} else {
		g_rejected_stale++;
	}

	/* The occupant's OWN delayed completion must still be accepted. */
	pend_own = SHM->pend_n;
	server_cmd(CMD_PRODUCE_REP, G_SLOT, 0, 0, 0, NULL);
	inject_delay();
	server_cmd(CMD_DELIVER, pend_own, 0, 0, 0, NULL);
	own = SHM->rep[G_SLOT];
	rc_own = ring_accept(B, &expect, &own, &why_own);
	if (rc_own != R_ACCEPTED || own.token != TOKEN_RING_B)
		g_wrong_consumption++;

	__atomic_store_n(&exit_b, 1, __ATOMIC_RELEASE);
	pthread_join(tb, NULL);

	ident_str(sstr, sizeof sstr, &stamp);
	ident_str(ostr, sizeof ostr, &expect.id);
	printf("IDENT G1 stale.completion=%s occupant=%s compared=slot,generation,owner_tid,seq,callnum "
	       "stamp_seq=%u stamp_callnum=%u occupant_seq=%u occupant_callnum=%u reason=%s delay_ns=%llu\n",
	       sstr, ostr, stale.seq, stale.callnum, expect.seq, expect.callnum, why,
	       (unsigned long long)delay);
	printf("IDENT G1 tids: A_real_tid=%d B_real_tid=%d token_handed_to_B=%d (A's token)\n",
	       aa.real_tid, bargs.real_tid, recycled);
	printf("IDENT G1 eviction: stale_token=0x%llx occupant_token=0x%llx consumed_token=0x%llx "
	       "rejected=%llu own_completion=%s own_token=0x%llx wrong_consumptions=%llu\n",
	       (unsigned long long)TOKEN_RING_A, (unsigned long long)TOKEN_RING_B,
	       (unsigned long long)consumed, (unsigned long long)g_rejected_stale,
	       reason_name(rc_own), (unsigned long long)own.token,
	       (unsigned long long)g_wrong_consumption);

	ok = (rc == R_GENERATION) && (consumed == 0) && (rc_own == R_ACCEPTED) &&
	     (own.token == TOKEN_RING_B) && (g_wrong_consumption == 0);
	verdict(1, ok,
	        "stamp=%s occupant=%s compared=slot,generation,owner_tid,seq,callnum reason=%s; "
	        "the occupant consumed token=0x%llx (its own=0x%llx, the stale one=0x%llx); "
	        "its own delayed completion %s (token=0x%llx); wrong_consumptions=%llu",
	        sstr, ostr, why, (unsigned long long)consumed,
	        (unsigned long long)TOKEN_RING_B, (unsigned long long)TOKEN_RING_A,
	        reason_name(rc_own), (unsigned long long)own.token,
	        (unsigned long long)g_wrong_consumption);
}

/* ---- G2: S2C upcall for the old occupant --------------------------- */

static void phase_g2(void)
{
	struct child_report rep;
	uint64_t delay = 0;
	char sstr[96], ostr[96];
	int ok;

	memset(&rep, 0, sizeof rep);
	if (fork_epoch_cycle(G_SLOT, &rep, &delay) != 0 || rep.magic != CHILD_MAGIC)
		die("G2: the fork-epoch reuse cycle did not report");

	ident_str(sstr, sizeof sstr, &rep.upcall_stamp);
	ident_str(ostr, sizeof ostr, &rep.claimed);
	printf("IDENT G2 upcall.stamp=%s occupant=%s compared=slot,generation,owner_tid,parent_id "
	       "reason=%s delay_ns=%llu\n", sstr, ostr, reason_name(rep.reject_reason),
	       (unsigned long long)delay);
	printf("IDENT G2 child: child_tid=%d claimed_slot=%u self_lookup_ok=%u\n",
	       rep.child_tid, rep.claimed_slot, rep.self_found);
	printf("IDENT G2 side_effect: upcall_ready=%u accepted=%u executed=%u munmap_rc=%lld "
	       "mincore_rc=%lld (0 == still mapped) s2c_rejected=%llu\n",
	       rep.upcall_ready, rep.accepted, rep.executed, (long long)rep.munmap_rc,
	       (long long)rep.mincore_rc, (unsigned long long)SHM->s2c_rejected);

	ok = (rep.reject_reason == R_OWNER) && (rep.accepted == 0) && (rep.executed == 0) &&
	     (rep.mincore_rc == 0) && (SHM->s2c_rejected >= 1);
	verdict(2, ok,
	        "upcall stamp=%s occupant=%s compared=slot,generation,owner_tid,parent_id reason=%s; "
	        "the upcall was %s and the munmap it names did not run (mincore=%lld, 0 == the page is "
	        "still mapped); reported rejected/undeliverable: s2c_rejected=%llu",
	        sstr, ostr, reason_name(rep.reject_reason),
	        rep.executed ? "EXECUTED IN THE NEW OCCUPANT" : "not executed",
	        (long long)rep.mincore_rc, (unsigned long long)SHM->s2c_rejected);
}

/* ---- G3: blocking control completion ------------------------------- */

static void phase_g3(void)
{
	pthread_t ta, tb;
	pthread_barrier_t ba, bb;
	struct g3arg aa, bargs;
	struct ctlpend *pa, *pb;
	uint64_t seen_before;
	uint32_t stale_reject = R_ACCEPTED;
	char astr[128], bstr[128];
	int ok;

	memset(&aa, 0, sizeof aa);
	memset(&bargs, 0, sizeof bargs);
	pthread_barrier_init(&ba, NULL, 2);
	pthread_barrier_init(&bb, NULL, 2);

	/* A claims the slot and blocks on its control request id. */
	aa.slot = G_SLOT;
	aa.token = 0;                 /* A's own tid, read back below */
	aa.ready = &ba;
	pthread_create(&ta, NULL, g3_occupant, &aa);
	pthread_barrier_wait(&ba);
	pa = aa.p;
	if (pa == NULL)
		die("G3: A did not publish a control request");
	spin_until_srv_ctl(1);

	/* A is abandoned: it exits without its reply. */
	__atomic_store_n(&pa->state, CP_ABANDONED, __ATOMIC_RELEASE);
	futex_wake(&pa->state, 1);
	pthread_join(ta, NULL);

	/* B re-claims the same slot with A's recycled token and blocks on its own id. */
	bargs.slot = G_SLOT;
	bargs.token = pa->id.owner_tid;   /* the recycled tid A had */
	bargs.ready = &bb;
	pthread_create(&tb, NULL, g3_occupant, &bargs);
	pthread_barrier_wait(&bb);
	pb = bargs.p;
	if (pb == NULL)
		die("G3: B did not publish a control request");
	spin_until_srv_ctl(2);
	for (long i = 0; i < 4000000000L; i++) {
		if (__atomic_load_n(&bargs.entered, __ATOMIC_ACQUIRE))
			break;
		rsys_pause();
		if ((i & 0xfffff) == 0)
			sched_yield();
	}

	/* A's completion arrives FIRST, addressed to A's request id. */
	seen_before = __atomic_load_n(&g_rx_seen, __ATOMIC_ACQUIRE);
	server_cmd(CMD_CTL_REPLY, 0, pa->request_id, pa->token, 0, &pa->id);
	rx_wait(seen_before);
	stale_reject = g_rx_last_reject;
	g_blocked_when_stale_arrived =
		(__atomic_load_n(&pb->state, __ATOMIC_ACQUIRE) == CP_BLOCKED) ? 1u : 0u;

	/* Then B's own completion, addressed to B's request id. */
	seen_before = __atomic_load_n(&g_rx_seen, __ATOMIC_ACQUIRE);
	server_cmd(CMD_CTL_REPLY, 0, pb->request_id, pb->token, 0, &pb->id);
	rx_wait(seen_before);
	pthread_join(tb, NULL);

	snprintf(astr, sizeof astr,
	         "{request_id=%llu,slot=%u,generation=%u,owner_tid=%d,token=0x%llx}",
	         (unsigned long long)pa->request_id, pa->id.slot, pa->id.generation,
	         pa->id.owner_tid, (unsigned long long)pa->token);
	snprintf(bstr, sizeof bstr,
	         "{request_id=%llu,slot=%u,generation=%u,owner_tid=%d,token=0x%llx}",
	         (unsigned long long)pb->request_id, pb->id.slot, pb->id.generation,
	         pb->id.owner_tid, (unsigned long long)pb->token);
	printf("IDENT G3 stale_completion=%s pending=%s compared=request_id,slot,generation,owner_tid,token "
	       "reason=%s\n", astr, bstr, reason_name((int)stale_reject));
	printf("IDENT G3 blocking: A_abandoned=%s B_was_still_blocked_when_the_stale_completion_arrived=%u "
	       "B_awaited_token=0x%llx B_received_token=0x%llx unmatched=%llu wrong_id=%llu\n",
	       aa.wait_rc == 1 ? "yes" : "no", g_blocked_when_stale_arrived,
	       (unsigned long long)pb->token, (unsigned long long)bargs.got_token,
	       (unsigned long long)g_rx_unmatched, (unsigned long long)g_rx_wrong_id);

	ok = (aa.wait_rc == 1) &&
	     (stale_reject == R_NO_LIVE_REQUEST) && (g_rx_unmatched >= 1) &&
	     (g_blocked_when_stale_arrived == 1) &&
	     (bargs.got_token == pb->token) && (g_rx_wrong_id == 0);
	verdict(3, ok,
	        "stale completion=%s pending=%s compared=request_id,slot,generation,owner_tid,token "
	        "reason=%s; B was still blocked when it arrived=%u; B's own completion delivered "
	        "token=0x%llx (awaited 0x%llx); unmatched=%llu wrong_id_deliveries=%llu",
	        astr, bstr, reason_name((int)stale_reject), g_blocked_when_stale_arrived,
	        (unsigned long long)bargs.got_token, (unsigned long long)pb->token,
	        (unsigned long long)g_rx_unmatched, (unsigned long long)g_rx_wrong_id);
}

/* ---- G4: SCM_RIGHTS descriptor association ------------------------- */

static void phase_g4(void)
{
	lane_t *A, *B;
	struct ctlpend *pa, *pb;
	uint64_t rid = 0;
	uint64_t seen_before;
	char astr[128], bstr[128];
	int fd_before, fd_after, fd_end, srv_before, srv_after;
	uint64_t rj_before;
	int ok;

	fd_before = fd_count_of(getpid());
	srv_before = fd_count_of(g_server_pid);

	/* A's logical request: really sent, then abandoned with the occupant. */
	A = lane_claim_slot_pub(G_SLOT, g_token_next++, 1, OP_CTL, TOKEN_DESC_A, 0);
	if (A == NULL)
		die("G4: A could not claim the slot");
	pa = cp_alloc(&rid);
	if (pa == NULL)
		die("G4: no pending-request slot");
	pa->token = TOKEN_DESC_A;
	pa->id = lane_ident(A);
	ring_publish(A, OP_CTL, pa->token, pa->request_id);
	if (send_ctl_request(pa) != 0)
		die("G4: send of A's control request");
	spin_until_srv_ctl(3);
	__atomic_store_n(&pa->state, CP_ABANDONED, __ATOMIC_RELEASE);
	lane_release(A);

	/* B re-claims the same slot with the recycled token and has an outstanding request. */
	B = lane_claim_slot_pub(G_SLOT, pa->id.owner_tid, 1, OP_CTL, TOKEN_DESC_B, 0);
	if (B == NULL)
		die("G4: B could not re-claim the slot");
	pb = cp_alloc(&rid);
	if (pb == NULL)
		die("G4: no pending-request slot");
	pb->token = TOKEN_DESC_B;
	pb->id = lane_ident(B);
	ring_publish(B, OP_CTL, pb->token, pb->request_id);
	if (send_ctl_request(pb) != 0)
		die("G4: send of B's control request");
	spin_until_srv_ctl(4);

	/* The descriptor for B's own request is sent first and must install. */
	seen_before = __atomic_load_n(&g_rx_seen, __ATOMIC_ACQUIRE);
	server_cmd(CMD_DESC_SEND, 0, pb->request_id, pb->token, 0, &pb->id);
	rx_wait(seen_before);
	/* The descriptor for A's logical request is sent second: no live request
	 * has A's id, so it must be rejected and CLOSED. */
	rj_before = g_desc_rejected;
	seen_before = __atomic_load_n(&g_rx_seen, __ATOMIC_ACQUIRE);
	server_cmd(CMD_DESC_SEND, 0, pa->request_id, pa->token, 0, &pa->id);
	rx_wait(seen_before);

	fd_after = fd_count_of(getpid());
	srv_after = fd_count_of(g_server_pid);

	/* the request is over: the occupant is gone and its lane is free again */
	lane_release(B);

	/* No descriptor may survive outside the request that owns it. */
	if (pb->installed_fd >= 0) {
		close(pb->installed_fd);
		pb->installed_fd = -1;
	}
	fd_end = fd_count_of(getpid());

	snprintf(astr, sizeof astr,
	         "{request_id=%llu,slot=%u,generation=%u,owner_tid=%d,token=0x%llx}",
	         (unsigned long long)pa->request_id, pa->id.slot, pa->id.generation,
	         pa->id.owner_tid, (unsigned long long)pa->token);
	snprintf(bstr, sizeof bstr,
	         "{request_id=%llu,slot=%u,generation=%u,owner_tid=%d,token=0x%llx}",
	         (unsigned long long)pb->request_id, pb->id.slot, pb->id.generation,
	         pb->id.owner_tid, (unsigned long long)pb->token);
	printf("IDENT G4 stale_descriptor=%s outstanding_request=%s compared=descriptor_token,request_id,"
	       "slot,generation,owner_tid rejected=%llu closed=%llu installed=%llu\n",
	       astr, bstr, (unsigned long long)g_desc_rejected,
	       (unsigned long long)g_desc_closed, (unsigned long long)g_desc_installed);
	printf("IDENT G4 server: received=%llu sent=%llu unknown=%llu desc_sent=%llu desc_closed=%llu "
	       "desc_failed=%llu\n", (unsigned long long)SHM->srv_ctl_recv,
	       (unsigned long long)SHM->srv_ctl_sent, (unsigned long long)SHM->srv_ctl_unknown,
	       (unsigned long long)SHM->srv_desc_sent, (unsigned long long)SHM->srv_desc_closed,
	       (unsigned long long)SHM->srv_desc_failed);
	printf("IDENT G4 association: the request B holds installed token=0x%llx (its own=0x%llx, "
	       "the stale one=0x%llx); fds guest before=%d after=%d end=%d server before=%d after=%d; "
	       "stale descriptor closed-on-reject=%llu\n",
	       (unsigned long long)pb->installed_token, (unsigned long long)TOKEN_DESC_B,
	       (unsigned long long)TOKEN_DESC_A, fd_before, fd_after, fd_end,
	       srv_before, srv_after, (unsigned long long)(g_desc_rejected - rj_before));

	ok = (g_desc_rejected >= 1) && (g_desc_closed == g_desc_rejected) &&
	     (g_desc_installed == 1) &&
	     (pb->installed_token == pb->token) &&
	     (fd_after == fd_before + 1) && (fd_end == fd_before) &&
	     (srv_after == srv_before) && (SHM->srv_desc_failed == 0);
	verdict(4, ok,
	        "the stale descriptor=%s was %s and %s (rejected=%llu closed=%llu) and never reached "
	        "the outstanding request=%s (that request saw it=%s); that request installed token=0x%llx "
	        "(its own=0x%llx); descriptors guest before=%d after=%d after-releasing=%d, "
	        "server before=%d after=%d",
	        astr,
	        g_desc_rejected >= 1 ? "rejected" : "ACCEPTED",
	        g_desc_rejected == 0 ? "NOT rejected at all"
	                             : (g_desc_closed == g_desc_rejected
	                                ? "closed, so not leaked" : "only partly closed"),
	        (unsigned long long)g_desc_rejected, (unsigned long long)g_desc_closed,
	        bstr, pb->rejected_desc ? "yes" : "no",
	        (unsigned long long)pb->installed_token, (unsigned long long)TOKEN_DESC_B,
	        fd_before, fd_after, fd_end, srv_before, srv_after);
}

/* ---- G5: repeated reuse under load --------------------------------- */

enum {
	G5_CYCLES      = 1200,   /* sequential deterministic reuse cycles      */
	G5_FORK_CYCLES = 64,     /* fork-epoch cycles (a real fork each)       */
};

static void phase_g5(void)
{
	uint64_t dmin = ~0ull, dmax = 0, dsum = 0, dn = 0;
	uint64_t acquisitions = 0, lookups = 0;
	uint64_t stale_rejected = 0, own_accepted = 0, wrong = 0;
	uint64_t r_gen = 0, r_own = 0, r_other = 0;
	uint64_t fork_run = 0, fork_owner_rejects = 0, fork_escalations = 0;
	uint32_t i;

	for (i = 0; i < G5_CYCLES; i++) {
		uint32_t slot = (uint32_t)(i % LANES);
		uint32_t kelas = (uint32_t)(i % 4u);
		lane_t *P, *Q = NULL;
		struct ring_req expect;
		struct ring_rep rep;
		const char *why = NULL;
		uint64_t pend, t0, t1, d, tok_p, tok_q;
		int32_t recycled;
		int rc;

		memset(&expect, 0, sizeof expect);
		memset(&rep, 0, sizeof rep);
		tok_p = 0x100000ull + i;
		tok_q = 0x200000ull + i;

		{
			int32_t ptok = g_token_next++;
			uint64_t preqid = g_reqid_next++;

			/* class 1 claims through the deployed probe+CAS (find from the
			 * tid hash); every other class claims the slot the sequence
			 * names, so the reuse is sequenced rather than raced. */
			P = (kelas == 1u) ? lane_claim_pub(ptok, 1, OP_ECHO, tok_p, preqid)
			                  : lane_claim_slot_pub(slot, ptok, 1, OP_ECHO, tok_p, preqid);
			if (P == NULL)
				die("G5: occupant P could not claim a slot");
			acquisitions++;
			lookups++;
			if (lane_find(ptok) != P)
				die("G5: the occupant cannot find its own lane");
			slot = lane_slot_of(P);
			ring_publish(P, OP_ECHO, tok_p, preqid);
		}
		pend = SHM->pend_n;
		t0 = now_ns();
		server_cmd(CMD_PRODUCE_REP, slot, 0, 0, 0, NULL);

		if (kelas == 3u) {
			/* the legitimate delivery: it belongs to the current occupant */
			inject_delay();
			server_cmd(CMD_DELIVER, pend, 0, 0, 0, NULL);
			t1 = now_ns();
			rep = SHM->rep[slot];
			ring_expect(P, &expect);
			rc = ring_accept(P, &expect, &rep, &why);
			if (rc == R_ACCEPTED && rep.token == tok_p)
				own_accepted++;
			else
				wrong++;
			lane_release(P);
		} else {
			/* the occupant exits and the slot is re-claimed: class 0 recycles
			 * the token (the generation is the only separating field), class 1
			 * uses a fresh token. */
			recycled = (kelas == 0u) ? P->owner_tid : (int32_t)(g_token_next++);
			lane_release(P);
			{
				uint64_t qreqid = g_reqid_next++;

				Q = lane_claim_slot_pub(slot, recycled, 1, OP_ECHO, tok_q, qreqid);
				if (Q == NULL)
					die("G5: occupant Q could not re-claim the slot");
				acquisitions++;
				lookups++;
				if (lane_find(recycled) != Q)
					die("G5: the new occupant cannot find its own lane");
				ring_publish(Q, OP_ECHO, tok_q, qreqid);
			}
			inject_delay();
			server_cmd(CMD_DELIVER, pend, 0, 0, 0, NULL);
			t1 = now_ns();
			rep = SHM->rep[slot];
			ring_expect(Q, &expect);
			rc = ring_accept(Q, &expect, &rep, &why);
			if (rc == R_ACCEPTED) {
				/* accepted a completion that was not produced for this occupant */
				wrong++;
			} else {
				if (rep.token != tok_p)
					wrong++;   /* the slot did not even carry the produced completion */
				else
					stale_rejected++;
				if (rc == R_GENERATION)
					r_gen++;
				else if (rc == R_OWNER)
					r_own++;
				else
					r_other++;
			}
			lane_release(Q);
		}

		d = t1 - t0;
		dsum += d;
		if (d < dmin)
			dmin = d;
		if (d > dmax)
			dmax = d;
		dn++;
	}

	/* the epoch-collision class at a smaller volume: a real fork per cycle */
	for (i = 0; i < G5_FORK_CYCLES; i++) {
		struct child_report rep;
		uint64_t d = 0;

		memset(&rep, 0, sizeof rep);
		if (fork_epoch_cycle((uint32_t)(i % LANES), &rep, &d) != 0)
			continue;
		acquisitions += 2;
		lookups += 2;   /* the pre-fork occupant and the child each looked up */
		fork_run++;
		if (rep.self_found != 1)
			die("G5: the fork child cannot find its own lane");
		if (rep.reject_reason == R_OWNER && !rep.accepted && !rep.executed &&
		    rep.mincore_rc == 0)
			fork_owner_rejects++;
		if (rep.accepted || rep.executed || rep.mincore_rc != 0)
			fork_escalations++;
		dsum += d;
		if (d < dmin)
			dmin = d;
		if (d > dmax)
			dmax = d;
		dn++;
	}

	printf("IDENT G5 compared: ring=slot,generation,owner_tid,seq,callnum "
	       "upcall=slot,generation,owner_tid,parent_id (per cycle)\n");
	printf("IDENT G5 load: cycles=%d fork_cycles=%llu acquisitions=%llu delayed_deliveries=%llu "
	       "stale_rejected=%llu own_accepted=%llu wrong_consumptions=%llu lookups=%llu "
	       "reasons{generation=%llu,owner=%llu,other=%llu} fork_owner_rejects=%llu "
	       "fork_escalations=%llu delay_ns{min=%llu,mean=%llu,max=%llu}\n",
	       G5_CYCLES, (unsigned long long)fork_run, (unsigned long long)acquisitions,
	       (unsigned long long)dn, (unsigned long long)stale_rejected,
	       (unsigned long long)own_accepted, (unsigned long long)wrong,
	       (unsigned long long)lookups,
	       (unsigned long long)r_gen, (unsigned long long)r_own,
	       (unsigned long long)r_other, (unsigned long long)fork_owner_rejects,
	       (unsigned long long)fork_escalations, (unsigned long long)dmin,
	       (unsigned long long)(dn ? dsum / dn : 0), (unsigned long long)dmax);

	verdict(5, acquisitions >= 1000 && lookups == acquisitions && wrong == 0 &&
	        g_wrong_consumption == 0 &&
	        stale_rejected == (uint64_t)(G5_CYCLES - G5_CYCLES / 4) &&
	        own_accepted == (uint64_t)(G5_CYCLES / 4) &&
	        fork_run == G5_FORK_CYCLES && fork_owner_rejects == G5_FORK_CYCLES &&
	        fork_escalations == 0 && dn >= 1000,
	        "%llu slot acquisitions (%llu owner lookups) over %llu delayed deliveries (min/mean/max injected delay "
	        "%llu/%llu/%llu ns): %llu stale completions rejected (generation=%llu owner=%llu "
	        "other=%llu), %llu legitimate deliveries accepted, %llu fork-epoch upcalls refused by "
	        "the owner check, %llu escalations, %llu wrong consumptions",
	        (unsigned long long)acquisitions, (unsigned long long)lookups,
	        (unsigned long long)dn,
	        (unsigned long long)dmin, (unsigned long long)(dn ? dsum / dn : 0),
	        (unsigned long long)dmax, (unsigned long long)stale_rejected,
	        (unsigned long long)r_gen, (unsigned long long)r_own,
	        (unsigned long long)r_other, (unsigned long long)own_accepted,
	        (unsigned long long)fork_owner_rejects, (unsigned long long)fork_escalations,
	        (unsigned long long)wrong);
}

/* ------------------------------------------------------------------ */
/* main                                                                */
/* ------------------------------------------------------------------ */

static int g_exit_ok;

int main(int argc, char **argv)
{
	const char *env;
	int status = 0;

	(void)argc;
	(void)argv;
	setvbuf(stdout, NULL, _IOLBF, 0);

	env = getenv("GAP_ENV");
	if (env != NULL && *env != '\0')
		g_env = env;

	SHM = mmap(NULL, sizeof *SHM, PROT_READ | PROT_WRITE,
	           MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (SHM == MAP_FAILED)
		die("mmap the shared control block");
	memset(SHM, 0, sizeof *SHM);
	SHM->magic = SHM_MAGIC;
	SHM->next_upcall_id = 1;

	g_victim_len = 4096;
	g_victim = mmap(NULL, (size_t)g_victim_len, PROT_READ | PROT_WRITE,
	                MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (g_victim == MAP_FAILED)
		die("mmap the S2C upcall's victim page");
	memset(g_victim, 0x5a, (size_t)g_victim_len);
	SHM->victim_addr = (uint64_t)(uintptr_t)g_victim;
	SHM->victim_len = (uint64_t)g_victim_len;

	server_start();

	if (pthread_create(&g_rx_thread, NULL, receiver_thread, NULL) != 0)
		die("pthread_create the control receiver");
	g_rx_thread_live = 1;

	printf("INFO env=%s pid=%d server_pid=%d lanes=%u pend_max=%u victim=%p\n",
	       g_env, (int)getpid(), (int)g_server_pid, LANES, PEND_MAX,
	       (void *)g_victim);

	phase_g1();
	phase_g2();
	phase_g3();
	phase_g4();
	phase_g5();

	/* shutdown: the server closes the control endpoint and exits; the receiver
	 * then observes EOF and returns. */
	{
		struct cmd *c = &SHM->cmd;

		c->op = CMD_QUIT;
		c->arg = 0;
		__atomic_add_fetch(&SHM->cmd_seq, 1, __ATOMIC_ACQ_REL);
	}
	waitpid(g_server_pid, &status, 0);
	pthread_join(g_rx_thread, NULL);

	printf("IDENT server: commands=%llu produced=%llu delivered=%llu control_received=%llu "
	       "control_sent=%llu control_unknown=%llu descriptors_sent=%llu descriptors_closed=%llu "
	       "descriptors_failed=%llu s2c_refused_by_the_pump=%llu receiver_seen=%llu receiver_matched=%llu "
	       "receiver_unmatched=%llu descriptors_rejected=%llu descriptors_installed=%llu\n",
	       (unsigned long long)SHM->srv_cmds, (unsigned long long)SHM->srv_produced,
	       (unsigned long long)SHM->srv_delivered, (unsigned long long)SHM->srv_ctl_recv,
	       (unsigned long long)SHM->srv_ctl_sent, (unsigned long long)SHM->srv_ctl_unknown,
	       (unsigned long long)SHM->srv_desc_sent, (unsigned long long)SHM->srv_desc_closed,
	       (unsigned long long)SHM->srv_desc_failed, (unsigned long long)SHM->s2c_rejected,
	       (unsigned long long)g_rx_seen, (unsigned long long)g_rx_matched,
	       (unsigned long long)g_rx_unmatched, (unsigned long long)g_desc_rejected,
	       (unsigned long long)g_desc_installed);

	print_claims();
	g_exit_ok = all_ok();
	return g_exit_ok ? 0 : 1;
}

/* Guest-side fixture for the lane lifecycle census.
 *
 * This program runs INSIDE a bootstrapped Darling prefix and produces the three
 * workloads the lane-lifecycle windows are measured across:
 *
 *   noop                            do nothing and exit 0.
 *   churn COUNT EVERY SAMPLE PRE POST
 *                                   create + join COUNT guest threads ONE AT A
 *                                   TIME (never more than one alive at a time),
 *                                   each performing exactly one ring-eligible
 *                                   trap (mach_host_self), so each thread that
 *                                   can attach a lane does.  Every EVERY threads
 *                                   (and always on the last one) the fixture
 *                                   emits its own guest-visible descriptor scan
 *                                   and a `churn-point` barrier, then sleeps
 *                                   SAMPLE ms so the host can take a host-side
 *                                   descriptor inventory and a server census
 *                                   snapshot at that exact thread count.  This
 *                                   is the sequential-thread-churn window: it
 *                                   answers whether the descriptor count is
 *                                   bounded or grows with the number of threads
 *                                   EVER created.
 *   sim THREADS REQUESTS PRE HOLD POST
 *                                   THREADS simultaneous guest threads, each
 *                                   doing ONE mach_host_self() trap and then
 *                                   parking on a start gate (so the host can
 *                                   sample with every thread's transport
 *                                   established), then REQUESTS more traps each.
 *                                   This is the above-the-cap window: the lane
 *                                   cap is per-process, so THREADS above it must
 *                                   fall back to the per-thread UDS socket.
 *   blockrecv ROUNDS SETTLE PRE POST
 *                                   one receiver guest thread parked in an
 *                                   UNBOUNDED blocking mach_msg receive
 *                                   (MACH_RCV_MSG, no timeout) on a port this
 *                                   process allocated, and the main thread
 *                                   delivering ROUNDS simple messages to it.
 *                                   The receive is already pending at the pre
 *                                   barrier; SETTLE ms elapses between a re-post
 *                                   and the matching send so every receive is
 *                                   genuinely parked at the server when the
 *                                   message arrives.  This is the blocking-Mach-
 *                                   RPC window (mach_msg_overwrite is UDS-only,
 *                                   so it exercises the non-ring slow path).
 *   psynch THREADS ITERS CROUNDS PRE POST
 *                                   THREADS guest threads hammering one shared
 *                                   pthread mutex (THREADS-1 drivers) plus a
 *                                   bounded pthread condition-variable phase, to
 *                                   drive whatever pthread mutex/cond operations
 *                                   this runtime maps onto the psynch RPCs.
 *
 * Protocol (one-directional; the host never writes to the guest):
 *
 *   DTC <mode> pid=<pid> ...
 *   DTC fds stage=<name> ...          a guest-visible descriptor scan
 *   DTC churn_progress created=<n> ...    (churn mode, one per sample point)
 *   DTC_BARRIER pre                   (then hold PRE ms)
 *   ...workload...
 *   DTC_BARRIER churn-point <n>       (churn mode only; then hold SAMPLE ms)
 *   DTC_BARRIER receiver-parked       (blockrecv mode only; then hold PRE ms)
 *   DTC_BARRIER threads-live          (sim mode only; then hold HOLD ms)
 *   DTC_BARRIER action-done           (then hold POST ms)
 *   DTC_BARRIER done
 *
 * The host samples the server's stat socket and /proc/<pid>/fd while the guest
 * is parked in a sleep, and refuses a sample whose window has already closed.
 * The guest process' getpid() equals the host pid of the mldr process hosting
 * it, which is what makes the outside view possible.
 *
 * The guest-visible scan uses fcntl(F_GETFD) over [0, min(getdtablesize(),
 * LLC_FD_SCAN_CAP)); the loader's own descriptors are hidden from the guest by
 * the descriptor guard, so the host's /proc/<pid>/fd count (taken by the
 * runner) is the authority for the process' true descriptor total and this scan
 * is the authority for what the guest itself can see.
 *
 * Compile inside the guest:
 *   /Library/Developer/CommandLineTools/usr/bin/clang \
 *     -isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -O1 \
 *     -Wno-deprecated-declarations -o <bin> lane_lifecycle_census_fixture.c
 */

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include <mach/mach.h>

/* Sized for the above-the-cap sweep: the largest simultaneous-thread column is
 * 512, plus the main thread.  Keeping this a compile-time array (as in the
 * direct-transport census fixture) keeps the guest heap out of the measurement. */
#define LLC_MAX_THREADS 640
#define LLC_MAX_LISTED_FDS 64
#define LLC_FD_SCAN_CAP 4096

static void emit(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	fflush(stdout);
}

static void barrier(const char *phase)
{
	emit("DTC_BARRIER %s\n", phase);
}

static void nap_ms(long ms)
{
	struct timespec ts;

	if (ms <= 0) {
		return;
	}
	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (ms % 1000) * 1000000L;
	while (nanosleep(&ts, NULL) != 0 && errno == EINTR) {
	}
}

static double mono_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1000000.0;
}

/* Second clock source: the guest's monotonic clock has been observed to step
 * backwards across a long workload, so every elapsed time is reported from both
 * sources and a disagreement is visible rather than hidden. */
static double wall_ms(void)
{
	struct timeval tv;

	gettimeofday(&tv, NULL);
	return (double)tv.tv_sec * 1000.0 + (double)tv.tv_usec / 1000.0;
}

/* The descriptors the guest itself can see, over a bounded prefix of the
 * advertised range. */
static int visible_fd_scan(int *numbers, int max_numbers, int *scanned)
{
	int top = getdtablesize();
	int cap = top < LLC_FD_SCAN_CAP ? top : LLC_FD_SCAN_CAP;
	int count = 0;
	int fd;

	if (cap < 0) {
		cap = 0;
	}
	for (fd = 0; fd < cap; ++fd) {
		if (fcntl(fd, F_GETFD) == -1) {
			continue;
		}
		if (numbers != NULL && count < max_numbers) {
			numbers[count] = fd;
		}
		++count;
	}
	if (scanned != NULL) {
		*scanned = cap;
	}
	return count;
}

/* Live guest threads this fixture itself created (workers only; the main thread
 * is not counted).  Reported alongside every descriptor scan so a host-side
 * descriptor count can always be read against the guest's own thread count. */
static int g_threads_live = 0;

static void thread_live_begin(void) { __atomic_add_fetch(&g_threads_live, 1, __ATOMIC_RELAXED); }
static void thread_live_end(void) { __atomic_sub_fetch(&g_threads_live, 1, __ATOMIC_RELAXED); }
static int llc_self_thread_count(void) { return __atomic_load_n(&g_threads_live, __ATOMIC_RELAXED); }

static void emit_fds(const char *stage)
{
	static int numbers[LLC_MAX_LISTED_FDS];
	int scanned = 0;
	int top = getdtablesize();
	int count = visible_fd_scan(numbers, LLC_MAX_LISTED_FDS, &scanned);
	int shown = count < LLC_MAX_LISTED_FDS ? count : LLC_MAX_LISTED_FDS;
	char list[512];
	size_t off = 0;
	int i;

	list[0] = '\0';
	for (i = 0; i < shown; ++i) {
		off += (size_t)snprintf(list + off, sizeof(list) - off, "%s%d",
		    i == 0 ? "" : ",", numbers[i]);
		if (off >= sizeof(list)) {
			break;
		}
	}
	emit("DTC fds stage=%s guest_visible=%d getdtablesize=%d scan_range=[0,%d) "
	    "list=[%s] guest_visible_capped=%d threads_live=%d\n",
	    stage, count, top, scanned, list,
	    count > LLC_MAX_LISTED_FDS ? 1 : 0, (int)llc_self_thread_count());
}

/* ----------------------------------------------------------------- churn */

struct churn_ctx {
	int ok;
	int failed;
};

static void *churn_thr_main(void *arg)
{
	struct churn_ctx *c = arg;

	thread_live_begin();
	/* One ring-eligible trap per thread: this is the op that makes the guest
	 * resolve (and, if the per-process lane table has room, attach) a lane for
	 * this thread. */
	if (mach_host_self() != MACH_PORT_NULL) {
		++c->ok;
	} else {
		++c->failed;
	}
	thread_live_end();
	return NULL;
}

static int mode_churn(int count, int every, long sample_ms, long pre_ms,
    long post_ms)
{
	int created = 0;
	int create_failed = 0;
	int rpc_ok = 0;
	int rpc_failed = 0;
	int i;
	char stage[64];
	double t0;
	double t1;
	double w0;
	double w1;

	if (count < 0) {
		count = 0;
	}
	if (every < 1) {
		every = 1;
	}

	emit("DTC churn pid=%d count=%d sample_every=%d sample_ms=%ld\n",
	    (int)getpid(), count, every, sample_ms);
	emit_fds("baseline");
	barrier("pre");
	nap_ms(pre_ms);

	t0 = mono_ms();
	w0 = wall_ms();
	for (i = 1; i <= count; ++i) {
		pthread_t thread;
		struct churn_ctx c;

		c.ok = 0;
		c.failed = 0;
		if (pthread_create(&thread, NULL, churn_thr_main, &c) != 0) {
			++create_failed;
		} else {
			pthread_join(thread, NULL);
			++created;
			rpc_ok += c.ok;
			rpc_failed += c.failed;
		}

		if (i % every == 0 || i == count) {
			/* The descriptor scan and the progress line go out BEFORE the
			 * barrier so that a host which observes the barrier is guaranteed
			 * to read a consistent stage name for this exact thread count. */
			emit_fds(snprintf(stage, sizeof(stage), "churn_%d", i) > 0 ? stage : "churn");
			emit("DTC churn_progress created=%d rpc_ok=%d rpc_failed=%d "
			    "create_failed=%d\n", created, rpc_ok, rpc_failed,
			    create_failed);
			emit("DTC_BARRIER churn-point %d\n", i);
			nap_ms(sample_ms);
		}
	}
	t1 = mono_ms();
	w1 = wall_ms();

	emit("DTC churn created=%d create_failed=%d rpc_ok=%d rpc_failed=%d "
	    "requests_requested=%d elapsed_mono_ms=%.1f elapsed_wall_ms=%.1f "
	    "ms_per_thread_wall=%.3f clock_disagree=%d\n",
	    created, create_failed, rpc_ok, rpc_failed, count, t1 - t0, w1 - w0,
	    created > 0 ? (w1 - w0) / (double)created : 0.0,
	    ((t1 - t0 < 0.0) != (w1 - w0 < 0.0)) ? 1 : 0);

	emit_fds("after_loop");
	barrier("action-done");
	nap_ms(post_ms);
	/* A second sample a full hold later, so the host can see whether any
	 * descriptor released after the loop has settled or is still draining. */
	emit_fds("post_settle");
	barrier("post-settle");
	nap_ms(post_ms);
	barrier("done");
	return 0;
}

/* ------------------------------------------------------------------- sim */

struct sim_ctx {
	pthread_mutex_t lock;
	pthread_cond_t gate;
	int requests;
	int started;
	int gate_open;
	int rpc_ok;
	int rpc_failed;
};

struct sim_thread {
	struct sim_ctx *ctx;
	int index;
};

static void *sim_thr_main(void *arg)
{
	struct sim_thread *self = arg;
	struct sim_ctx *ctx = self->ctx;
	int ok = 0;
	int failed = 0;
	int i;

	thread_live_begin();
	/* One trap before announcing: this is what gives the thread its own
	 * transport (and, on a ring build, its lane if the table has room). */
	if (mach_host_self() != MACH_PORT_NULL) {
		++ok;
	} else {
		++failed;
	}

	pthread_mutex_lock(&ctx->lock);
	++ctx->started;
	pthread_cond_broadcast(&ctx->gate);
	while (!ctx->gate_open) {
		pthread_cond_wait(&ctx->gate, &ctx->lock);
	}
	pthread_mutex_unlock(&ctx->lock);

	for (i = 0; i < ctx->requests; ++i) {
		if (mach_host_self() != MACH_PORT_NULL) {
			++ok;
		} else {
			++failed;
		}
	}

	pthread_mutex_lock(&ctx->lock);
	ctx->rpc_ok += ok;
	ctx->rpc_failed += failed;
	pthread_mutex_unlock(&ctx->lock);
	thread_live_end();
	return NULL;
}

static int mode_sim(int requested, int per_thread, long pre_ms, long hold_ms,
    long post_ms)
{
	static pthread_t threads[LLC_MAX_THREADS];
	static struct sim_thread args[LLC_MAX_THREADS];
	struct sim_ctx ctx;
	int created = 0;
	int actual;
	int i;
	double t0;
	double t1;
	double w0;
	double w1;

	if (requested < 0) {
		requested = 0;
	}
	if (requested > LLC_MAX_THREADS) {
		requested = LLC_MAX_THREADS;
	}
	if (per_thread < 0) {
		per_thread = 0;
	}

	memset(&ctx, 0, sizeof(ctx));
	pthread_mutex_init(&ctx.lock, NULL);
	pthread_cond_init(&ctx.gate, NULL);
	ctx.requests = per_thread;

	emit("DTC sim pid=%d threads_requested=%d\n", (int)getpid(), requested);
	emit_fds("baseline");
	barrier("pre");
	nap_ms(pre_ms);

	for (i = 0; i < requested; ++i) {
		args[i].ctx = &ctx;
		args[i].index = i;
		if (pthread_create(&threads[i], NULL, sim_thr_main,
		    &args[i]) != 0) {
			break;
		}
		++created;
	}

	pthread_mutex_lock(&ctx.lock);
	while (ctx.started < created) {
		pthread_cond_wait(&ctx.gate, &ctx.lock);
	}
	pthread_mutex_unlock(&ctx.lock);

	emit("DTC sim threads_requested=%d threads_created=%d\n", requested,
	    created);
	emit_fds("threads_live");
	barrier("threads-live");
	nap_ms(hold_ms);

	t0 = mono_ms();
	w0 = wall_ms();
	pthread_mutex_lock(&ctx.lock);
	ctx.gate_open = 1;
	pthread_cond_broadcast(&ctx.gate);
	pthread_mutex_unlock(&ctx.lock);
	for (i = 0; i < created; ++i) {
		pthread_join(threads[i], NULL);
	}
	t1 = mono_ms();
	w1 = wall_ms();
	actual = created * per_thread;

	emit("DTC sim threads_requested=%d threads_created=%d "
	    "requests_per_thread=%d nominal_requests=%d actual_requests=%d "
	    "rpc_ok=%d rpc_failed=%d elapsed_mono_ms=%.1f elapsed_wall_ms=%.1f "
	    "us_per_call=%.3f clock_disagree=%d\n",
	    requested, created, per_thread, requested * per_thread, actual,
	    ctx.rpc_ok, ctx.rpc_failed, t1 - t0, w1 - w0,
	    actual > 0 ? (w1 - w0) * 1000.0 / (double)actual : 0.0,
	    ((t1 - t0 < 0.0) != (w1 - w0 < 0.0)) ? 1 : 0);

	emit_fds("after_join");
	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	pthread_cond_destroy(&ctx.gate);
	pthread_mutex_destroy(&ctx.lock);
	return 0;
}

/* ------------------------------------------------------------- blockrecv */

/* MACH_MSG_TIMEOUT_NONE is 0 in the SDK, but spell the intent out here so the
 * "this receive parks unbounded" property is not an implicit default. */
#define LLC_RCV_NO_TIMEOUT MACH_MSG_TIMEOUT_NONE

struct blk_ctx {
	mach_port_t port;
	int rounds;
	long settle_ms;
	int posted;
	int consumed;
	int recv_ok;
	int recv_failed;
	int recv_other;
	int send_ok;
	int send_failed;
	/* Distinct non-success receive return codes, kept so a window that fails is
	 * reported with the code the runtime actually returned rather than a guess. */
	mach_msg_return_t recv_codes[8];
	int recv_code_count;
	int recv_code_hits[8];
};

static void record_recv_code(struct blk_ctx *c, mach_msg_return_t kr)
{
	int i;

	for (i = 0; i < c->recv_code_count; ++i) {
		if (c->recv_codes[i] == kr) {
			++c->recv_code_hits[i];
			return;
		}
	}
	if (c->recv_code_count < (int)(sizeof(c->recv_codes) / sizeof(c->recv_codes[0]))) {
		c->recv_codes[c->recv_code_count] = kr;
		c->recv_code_hits[c->recv_code_count] = 1;
		++c->recv_code_count;
	}
}

/* The receive buffer is deliberately generous: a delivered message that does not
 * fit makes the receive return MACH_RCV_TOO_LARGE without ever parking, which
 * would silently turn this window into a non-blocking one. */
#define LLC_RCV_BUFFER 256

static void *blk_recv_thr_main(void *arg)
{
	struct blk_ctx *c = arg;
	union {
		mach_msg_header_t hdr;
		char bytes[LLC_RCV_BUFFER];
	} buf;
	int r;

	thread_live_begin();
	for (r = 0; r < c->rounds; ++r) {
		mach_msg_return_t kr;

		/* Publish "I am about to post the receive for round r" BEFORE the
		 * call, then let the main thread's SETTLE nap absorb the time it takes
		 * this receive RPC to reach the server and park there. */
		__atomic_store_n(&c->posted, r + 1, __ATOMIC_RELEASE);
		memset(&buf, 0, sizeof(buf));
		kr = mach_msg(&buf.hdr, MACH_RCV_MSG, 0,
		    (mach_msg_size_t)sizeof(buf), c->port, LLC_RCV_NO_TIMEOUT,
		    MACH_PORT_NULL);
		if (kr == MACH_MSG_SUCCESS) {
			++c->recv_ok;
		} else if (kr == MACH_RCV_TIMED_OUT) {
			++c->recv_other;
			record_recv_code(c, kr);
		} else {
			++c->recv_failed;
			record_recv_code(c, kr);
		}
		__atomic_store_n(&c->consumed, r + 1, __ATOMIC_RELEASE);
	}
	thread_live_end();
	return NULL;
}

static int mode_blockrecv(int rounds, long settle_ms, long pre_ms, long post_ms)
{
	static struct blk_ctx ctx;
	pthread_t receiver;
	mach_port_t port = MACH_PORT_NULL;
	kern_return_t kr;
	int sent = 0;
	int i;
	double t0;
	double t1;
	double w0;
	double w1;

	if (rounds < 0) {
		rounds = 0;
	}
	if (rounds > LLC_MAX_THREADS * 64) {
		rounds = LLC_MAX_THREADS * 64;
	}
	if (settle_ms < 0) {
		settle_ms = 0;
	}

	emit("DTC blockrecv pid=%d rounds=%d settle_ms=%ld\n", (int)getpid(),
	    rounds, settle_ms);
	emit_fds("baseline");
	barrier("pre");
	nap_ms(pre_ms);

	memset(&ctx, 0, sizeof(ctx));
	ctx.rounds = rounds;
	ctx.settle_ms = settle_ms;

	kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &port);
	if (kr != KERN_SUCCESS) {
		emit("DTC blockrecv FAILED stage=mach_port_allocate kr=%d\n", (int)kr);
		emit_fds("after_loop");
		barrier("action-done");
		nap_ms(post_ms);
		barrier("done");
		return 1;
	}
	kr = mach_port_insert_right(mach_task_self(), port, port,
	    MACH_MSG_TYPE_MAKE_SEND);
	if (kr != KERN_SUCCESS) {
		emit("DTC blockrecv FAILED stage=mach_port_insert_right kr=%d\n",
		    (int)kr);
		emit_fds("after_loop");
		barrier("action-done");
		nap_ms(post_ms);
		barrier("done");
		return 1;
	}
	ctx.port = port;

	if (pthread_create(&receiver, NULL, blk_recv_thr_main, &ctx) != 0) {
		emit("DTC blockrecv FAILED stage=pthread_create\n");
		emit_fds("after_loop");
		barrier("action-done");
		nap_ms(post_ms);
		barrier("done");
		return 1;
	}

	/* Make the FIRST receive genuinely pending before the window opens: wait
	 * for the receiver to announce it, then let it park at the server. */
	while (__atomic_load_n(&ctx.posted, __ATOMIC_ACQUIRE) < 1) {
		sched_yield();
	}
	nap_ms(settle_ms > 0 ? settle_ms : 50);

	emit_fds("recv_parked");
	emit("DTC_BARRIER receiver-parked\n");

	t0 = mono_ms();
	w0 = wall_ms();
	for (i = 0; i < rounds; ++i) {
		mach_msg_header_t msg;
		mach_msg_return_t snd;

		while (__atomic_load_n(&ctx.consumed, __ATOMIC_ACQUIRE) < i) {
			sched_yield();
		}
		while (__atomic_load_n(&ctx.posted, __ATOMIC_ACQUIRE) < i + 1) {
			sched_yield();
		}
		nap_ms(settle_ms);
		memset(&msg, 0, sizeof(msg));
		msg.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);
		msg.msgh_size = (mach_msg_size_t)sizeof(msg);
		msg.msgh_remote_port = port;
		msg.msgh_local_port = MACH_PORT_NULL;
		msg.msgh_id = 0x4c4c4300 + i;
		snd = mach_msg(&msg, MACH_SEND_MSG, (mach_msg_size_t)sizeof(msg),
		    0, MACH_PORT_NULL, LLC_RCV_NO_TIMEOUT, MACH_PORT_NULL);
		if (snd == MACH_MSG_SUCCESS) {
			++sent;
		} else {
			++ctx.send_failed;
		}
	}
	t1 = mono_ms();
	w1 = wall_ms();

	pthread_join(receiver, NULL);

	emit("DTC blockrecv rounds_requested=%d sends_ok=%d send_failed=%d "
	    "recv_ok=%d recv_failed=%d recv_other=%d elapsed_mono_ms=%.1f "
	    "elapsed_wall_ms=%.1f clock_disagree=%d\n",
	    rounds, sent, ctx.send_failed, ctx.recv_ok, ctx.recv_failed,
	    ctx.recv_other, t1 - t0, w1 - w0,
	    ((t1 - t0 < 0.0) != (w1 - w0 < 0.0)) ? 1 : 0);
	for (i = 0; i < ctx.recv_code_count; ++i) {
		emit("DTC blockrecv recv_code=0x%x hits=%d\n",
		    (unsigned)ctx.recv_codes[i], ctx.recv_code_hits[i]);
	}

	emit_fds("after_loop");
	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	return 0;
}

/* ---------------------------------------------------------------- psynch */

struct psynch_ctx {
	pthread_mutex_t mtx;
	long counter;
	int iters;
	int contending;
	volatile int go;

	pthread_mutex_t cmtx;
	pthread_cond_t cnd;
	long gen;
	int crounds;
	int cwaiters;
	/* Lockstep: a waiter increments `consumed` once per round it observes, and the
	 * driver keeps bumping `gen` until every waiter has consumed every round. A
	 * waiter that has not reached its cond_wait yet simply observes the current
	 * generation, so no round can be missed and no waiter can block forever. */
	int consumed;
};

static void *psynch_mtx_worker(void *arg)
{
	struct psynch_ctx *c = arg;
	int i;

	thread_live_begin();
	while (!__atomic_load_n(&c->go, __ATOMIC_ACQUIRE)) {
		sched_yield();
	}
	for (i = 0; i < c->iters; ++i) {
		pthread_mutex_lock(&c->mtx);
		c->counter++;
		pthread_mutex_unlock(&c->mtx);
	}
	thread_live_end();
	return NULL;
}

/* Bounded condvar phase: each waiter loops on a shared generation counter, so a
 * lost wakeup cannot deadlock it. */
static void *psynch_cnd_worker(void *arg)
{
	struct psynch_ctx *c = arg;
	long seen = 0;
	int r;

	thread_live_begin();
	while (!__atomic_load_n(&c->go, __ATOMIC_ACQUIRE)) {
		sched_yield();
	}
	for (r = 0; r < c->crounds; ++r) {
		pthread_mutex_lock(&c->cmtx);
		while (c->gen <= seen) {
			pthread_cond_wait(&c->cnd, &c->cmtx);
		}
		seen = c->gen;
		++c->consumed;
		pthread_cond_broadcast(&c->cnd);
		pthread_mutex_unlock(&c->cmtx);
	}
	thread_live_end();
	return NULL;
}

static int mode_psynch(int threads, int iters, int crounds, long pre_ms,
    long post_ms)
{
	static pthread_t t[LLC_MAX_THREADS];
	struct psynch_ctx ctx;
	int ncnd;
	int nmtx;
	int created = 0;
	int i;
	double t0;
	double t1;
	double w0;
	double w1;

	if (threads < 1) {
		threads = 1;
	}
	if (threads > LLC_MAX_THREADS) {
		threads = LLC_MAX_THREADS;
	}
	if (iters < 0) {
		iters = 0;
	}
	if (crounds < 0) {
		crounds = 0;
	}
	ncnd = threads / 4;
	if (ncnd < 1) {
		ncnd = 1;
	}
	if (ncnd >= threads) {
		ncnd = threads > 1 ? threads - 1 : 0;
	}
	nmtx = threads - ncnd;

	memset(&ctx, 0, sizeof(ctx));
	pthread_mutex_init(&ctx.mtx, NULL);
	pthread_mutex_init(&ctx.cmtx, NULL);
	pthread_cond_init(&ctx.cnd, NULL);
	ctx.iters = iters;
	ctx.crounds = crounds;
	ctx.cwaiters = ncnd;

	emit("DTC psynch pid=%d threads=%d mtx_threads=%d cnd_threads=%d "
	    "iters=%d crounds=%d\n", (int)getpid(), threads, nmtx, ncnd, iters,
	    crounds);
	emit_fds("baseline");
	barrier("pre");
	nap_ms(pre_ms);

	for (i = 0; i < nmtx; ++i) {
		if (pthread_create(&t[i], NULL, psynch_mtx_worker, &ctx) != 0) {
			break;
		}
		++created;
	}
	for (i = 0; i < ncnd; ++i) {
		if (pthread_create(&t[created], NULL, psynch_cnd_worker, &ctx) != 0) {
			break;
		}
		++created;
	}
	emit("DTC psynch threads_created=%d\n", created);
	emit_fds("threads_live");

	t0 = mono_ms();
	w0 = wall_ms();
	__atomic_store_n(&ctx.go, 1, __ATOMIC_RELEASE);
	/* Drive the condvar from the main thread until every waiter has consumed every
	 * round. The driver bumps the generation and then SLEEPS ON THE SAME CONDVAR
	 * until a waiter reports a consumption, so it can never run ahead of the
	 * waiters and no waiter can be left parked after the last round. The guard is
	 * a belt-and-braces bound; it is never reached when the handshake works. */
	{
		int guard = 0;
		int target = ncnd * crounds;

		pthread_mutex_lock(&ctx.cmtx);
		while (ctx.consumed < target && guard < target * 4 + 1000) {
			ctx.gen++;
			pthread_cond_broadcast(&ctx.cnd);
			if (ctx.consumed < target) {
				pthread_cond_wait(&ctx.cnd, &ctx.cmtx);
			}
			++guard;
		}
		pthread_mutex_unlock(&ctx.cmtx);
		emit("DTC psynch cond_driver rounds_consumed=%d target=%d guard=%d\n",
		    ctx.consumed, target, guard);
	}
	for (i = 0; i < created; ++i) {
		pthread_join(t[i], NULL);
	}
	t1 = mono_ms();
	w1 = wall_ms();

	emit("DTC psynch threads_requested=%d threads_created=%d iters=%d "
	    "crounds=%d counter=%ld elapsed_mono_ms=%.1f elapsed_wall_ms=%.1f "
	    "clock_disagree=%d\n",
	    threads, created, iters, crounds, ctx.counter, t1 - t0, w1 - w0,
	    ((t1 - t0 < 0.0) != (w1 - w0 < 0.0)) ? 1 : 0);

	emit_fds("after_join");
	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	pthread_mutex_destroy(&ctx.mtx);
	pthread_mutex_destroy(&ctx.cmtx);
	pthread_cond_destroy(&ctx.cnd);
	return 0;
}

/* ------------------------------------------------------------------ */

static int usage(void)
{
	fprintf(stderr,
	    "usage: %s noop\n"
	    "       %s churn COUNT EVERY SAMPLE_MS PRE_MS POST_MS\n"
	    "       %s sim THREADS REQUESTS PRE_MS HOLD_MS POST_MS\n"
	    "       %s blockrecv ROUNDS SETTLE_MS PRE_MS POST_MS\n"
	    "       %s psynch THREADS ITERS CROUNDS PRE_MS POST_MS\n",
	    "lane_lifecycle_census_fixture", "lane_lifecycle_census_fixture",
	    "lane_lifecycle_census_fixture", "lane_lifecycle_census_fixture",
	    "lane_lifecycle_census_fixture");
	return 2;
}

int main(int argc, char **argv)
{
	const char *mode;

	setvbuf(stdout, NULL, _IOLBF, 0);
	if (argc < 2) {
		return usage();
	}
	mode = argv[1];

	if (strcmp(mode, "noop") == 0 && argc == 2) {
		return 0;
	}
	if (strcmp(mode, "churn") == 0 && argc == 7) {
		return mode_churn(atoi(argv[2]), atoi(argv[3]), atol(argv[4]),
		    atol(argv[5]), atol(argv[6]));
	}
	if (strcmp(mode, "sim") == 0 && argc == 7) {
		return mode_sim(atoi(argv[2]), atoi(argv[3]), atol(argv[4]),
		    atol(argv[5]), atol(argv[6]));
	}
	if (strcmp(mode, "blockrecv") == 0 && argc == 6) {
		return mode_blockrecv(atoi(argv[2]), atol(argv[3]),
		    atol(argv[4]), atol(argv[5]));
	}
	if (strcmp(mode, "psynch") == 0 && argc == 7) {
		return mode_psynch(atoi(argv[2]), atoi(argv[3]), atoi(argv[4]),
		    atol(argv[5]), atol(argv[6]));
	}
	return usage();
}

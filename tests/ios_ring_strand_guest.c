/*
 * Bounded, Xcode-free reproducer for the T4 "invisible parking" strand.
 *
 * WHY THIS EXISTS. The T4 link hangs with two guest ld threads parked in a HOST futex while the
 * server's manager idles in ep_poll and one server thread sits in a futex of its own. The
 * surviving reading of that state, recorded in docs/ulock-queue-design.md ("the wait blocks where
 * the server cannot see it"), is:
 *
 *   a blocking operation that does NOT get a lane -- no lane for the thread, the c2s ring has no
 *   free slot, or the lane-acquire budget is spent -- falls back to the pre-migration per-thread
 *   datagram, which `mach_driver_get_fd` now declines unconditionally (`[rpc-socket-DENIED]`), so
 *   the RPC returns negative and `sys_ulock_wait` parks the thread in `ulock_wait_futex`
 *   (ulock_wait.c:57-118). The thread is then blocked inside the guest process where the server
 *   cannot see it, and the paired wake -- which DOES reach the server -- believes it did everything.
 *
 * The real ld reaches that state through Apple's toolchain, which is a 12-stage, minute-long run.
 * This fixture reaches it in seconds and with no Xcode at all, because the transport the blocking
 * family uses is a single existing, default-off runtime knob: `DARLING_GUEST_RING_BLOCKING=0`
 * ("the caller's datagram path runs exactly as it did before the migration",
 * dserver-ring.c:3345-3368). Two threads contending on ONE wait path is all it takes; no Apple
 * binary, no SDK, no prefix-side instrumentation.
 *
 * WHAT IS SIMULATED, EXACTLY. Nothing: the fixture calls the guest's real `__ulock_wait` /
 * `__ulock_wake` (the same pair libplatform's os_unfair_lock slow path uses) and the real
 * pthread_mutex psynch path. The runtime knob changes the transport only.
 *
 * ARMS
 *   stage=ulock-park     four waiters park on ONE word with `timeout=0` (INFINITE, like the real
 *                        ld) and a single main thread wakes them one at a time, repeating the wake
 *                        until each waiter is out or the stage deadline passes. With the Ring
 *                        (product) transport the wake must also travel the ring, i.e. it must
 *                        survive `gr_wait_reply` (dserver-ring.c:2442-2470) -- the exact park
 *                        both ld threads are in. With `DARLING_GUEST_RING_BLOCKING=0` the pair
 *                        takes the datagram, is denied, and parks in the host futex.
 *   stage=psynch-contend four threads hammer ONE pthread_mutex, which forces the
 *                        `__psynch_mutexwait` slow path over the lane. This mirrors the measured
 *                        reproducer tests/ios_psynch_route_guest.c (which hangs in gr_wait_reply)
 *                        and is the mutex half of the T4 shape.
 *
 * BOUNDED BY CONSTRUCTION. Every waiter uses `timeout=0`, so a lost wake parks instead of
 * returning a retryable timeout. A watchdog thread -- created before any worker, never touching
 * the contended word, and paced with usleep alone -- fires when a stage makes no progress for
 * `IOS_RING_STRAND_DEADLINE_MS` (default 5000) and prints the verdict itself, so a strand reports
 * `stranded=N` in seconds instead of hanging the run. The runner's harness `--wait` is the outer
 * bound if even the watchdog cannot be scheduled.
 *
 * VERDICT (one machine-readable line; tag is this runner's shared `IOS-UNFAIR-LOCK` marker)
 *   pass=1 only when no worker is stranded AND the observed transport is the one the arm declares:
 *            product arm   host_futex_wait=0 AND depth_contract=ok -- the waits rode the ring lane
 *                          to the server-owned queue, whose depth multiset is exactly {0..K-1};
 *            datagram arm  host_futex_wait=1 -- the constant-1 futex signature, which is exactly
 *                          what that arm forces and expects.
 *   pass=0 when a worker did not finish inside the deadline, OR the transport is not the declared
 *          one. The product arm's `pass=0 ... host_futex_wait=1 depth_contract=violated` IS the RED
 *          for design step 6 (the blocking family must ride the ring); MEASURED on the deployed
 *          runtime (2026-10-10, DPREFIX=prefix-ios, hunt/t4-bsdreply-trace/ring-strand-2.log) it
 *          does not: the product arm shows the futex signature, so that arm reports pass=0 and the
 *          datagram arm (same signature, declared) reports pass=1.
 *     IOS-UNFAIR-LOCK pass=1 waiters=4 stranded=0 stage=done host_futex_wait=0 \
 *         depth_contract=ok expect_host_futex=0 ops=4000
 *     IOS-UNFAIR-LOCK pass=0 waiters=4 stranded=4 stage=psynch-contend host_futex_wait=1 \
 *         depth_contract=violated expect_host_futex=0 ops=137 reason=deadline_ms=5000
 *   (ops= is the psynch critical-section count, so a parked stage -- frozen ops -- is
 *   distinguishable from a stage that was merely slower than the deadline.)
 *
 * The transport is observable from the guest alone, with no new print layer in the runtime:
 * `ulock_wait_futex` returns the conservative constant 1 (ulock_wait.c:49-53), while the
 * server-owned queue returns the true remaining depth (`*retval = --ull->ull_nwaiters`,
 * xnu/bsd/kern/sys_ulock.c), so four one-at-a-time wakes yield {0,1,2,3} on the queue and {1,1,1,1}
 * on the futex. `depth_contract` and `host_futex_wait` are that difference.
 *
 * Run it with:
 *   IOS_UNFAIR_LOCK_FIXTURE=tests/ios_ring_strand_guest.c DPREFIX=... DARLING_BUILD_DIR=... \
 *       bash tests/run-ios-unfair-lock-contract.sh
 * or, for the product/datagram A/B in one command:
 *   DPREFIX=... DARLING_BUILD_DIR=... bash tests/run-ios-ring-strand-contract.sh
 */
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* bsd/sys/ulock.h: the opcode is the low byte, ULF_NO_ERRNO is a flag. The sibling fixture
 * (tests/ios_ulock_contract_guest.c) uses these same two values against this runtime. */
#define STRAND_UL_COMPARE_AND_WAIT 1u
#define STRAND_ULF_NO_ERRNO        0x01000000u
#define STRAND_OP_WAIT (STRAND_UL_COMPARE_AND_WAIT | STRAND_ULF_NO_ERRNO)
#define STRAND_OP_WAKE (STRAND_UL_COMPARE_AND_WAIT)

extern int __ulock_wait(uint32_t operation, void *addr, uint64_t value, uint32_t timeout);
extern int __ulock_wake(uint32_t operation, void *addr, uint64_t wake_value);

#define STRAND_WAITERS        4
#define STRAND_WAIT_WORD      0xdeadbeefu
#define STRAND_PSYNCH_WORKERS 4
/* Contention, not volume, is what forces the slow path: the FIRST contended lock takes it. The
 * count only adds exposure for a race, and every extra iteration is deadline budget the watchdog
 * may spend, so it stays small. */
#define STRAND_PSYNCH_ITERS   1000
/* A value no ulock_wait can return (Darwin returns the remaining queue depth, 0..K, or a raw
 * negated errno), so a waiter that never came back is unambiguous. */
#define STRAND_SENTINEL       0x7fffffff
#define STRAND_DEFAULT_DEADLINE_MS 5000
#define STRAND_TICK_MS        100

enum {
	STRAND_STAGE_INIT = 0,
	STRAND_STAGE_ULOCK_PARK,
	STRAND_STAGE_PSYNCH_CONTEND,
	STRAND_STAGE_DONE,
};

static const char *const strand_stage_names[] = {
	"init", "ulock-park", "psynch-contend", "done",
};

static uint32_t strand_word;
static pthread_mutex_t strand_mutex = PTHREAD_MUTEX_INITIALIZER;
static int strand_stage = STRAND_STAGE_INIT;   /* __atomic */
static int strand_outstanding = 0;             /* __atomic: workers not yet finished */
static int strand_psynch_ops = 0;              /* __atomic */
static volatile int strand_ulock_ret[STRAND_WAITERS];
static int strand_deadline_ms = STRAND_DEFAULT_DEADLINE_MS;
static int strand_run_ulock = 1;
static int strand_run_psynch = 1;
static int strand_expect_host_futex = 0;

/* ------------------------------------------------------------------ observations */

/* Darwin's queue reports the remaining depth, so K one-at-a-time wakes yield exactly {0..K-1}.
 * The futex fallback reports the constant 1 instead (ulock_wait.c), which cannot satisfy this. */
static int
strand_depth_contract(void)
{
	int seen[STRAND_WAITERS] = {0};

	for (int i = 0; i < STRAND_WAITERS; i++) {
		int r = strand_ulock_ret[i];
		if (r < 0 || r >= STRAND_WAITERS)
			return 0;
		seen[r] = 1;
	}
	for (int i = 0; i < STRAND_WAITERS; i++) {
		if (!seen[i])
			return 0;
	}
	return 1;
}

/* 1 = the host-futex signature (every observed return is the constant 1, none emptied a server
 * queue), 0 = the server queue answered (at least one waiter saw depth 0), -1 = nothing was
 * observed (every waiter is stranded, so the transport of the waits that hang cannot be read). */
static int
strand_host_futex_wait(void)
{
	int observed = 0;

	for (int i = 0; i < STRAND_WAITERS; i++) {
		int r = strand_ulock_ret[i];
		if (r == STRAND_SENTINEL || r < 0)
			continue;
		observed = 1;
		if (r == 0)
			return 0;
	}
	return observed ? 1 : -1;
}

static const char *
strand_hf_name(void)
{
	int hf = strand_host_futex_wait();

	return hf < 0 ? "unknown" : (hf ? "1" : "0");
}

static const char *
strand_depth_state(void)
{
	if (__atomic_load_n(&strand_outstanding, __ATOMIC_ACQUIRE) > 0)
		return "incomplete";
	return strand_depth_contract() ? "ok" : "violated";
}

static void
strand_announce(int ok, int deadline_hit)
{
	int stage = __atomic_load_n(&strand_stage, __ATOMIC_ACQUIRE);

	if (stage < STRAND_STAGE_INIT || stage > STRAND_STAGE_DONE)
		stage = STRAND_STAGE_INIT;
	printf("IOS-UNFAIR-LOCK pass=%d waiters=%d stranded=%d stage=%s host_futex_wait=%s depth_contract=%s expect_host_futex=%d",
	       ok,
	       STRAND_WAITERS,
	       __atomic_load_n(&strand_outstanding, __ATOMIC_ACQUIRE),
	       strand_stage_names[stage],
	       strand_hf_name(),
	       strand_depth_state(),
	       strand_expect_host_futex);
	/* psynch stage progress: a stranded stage reports the critical sections that DID complete, so a
	 * park (ops frozen) is distinguishable from a stage that was merely slower than the deadline. */
	printf(" ops=%d", __atomic_load_n(&strand_psynch_ops, __ATOMIC_ACQUIRE));
	if (deadline_hit)
		printf(" reason=deadline_ms=%d", strand_deadline_ms);
	printf("\n");
	fflush(stdout);
}

/* ------------------------------------------------------------------ watchdog */

static void *
strand_watchdog(void *arg)
{
	int seen_stage = -1;
	int elapsed_ms = 0;

	(void)arg;
	for (;;) {
		usleep(STRAND_TICK_MS * 1000);

		int stage = __atomic_load_n(&strand_stage, __ATOMIC_ACQUIRE);
		int outstanding = __atomic_load_n(&strand_outstanding, __ATOMIC_ACQUIRE);
		if (stage == STRAND_STAGE_DONE || outstanding <= 0) {
			/* Between stages or after the last one: no worker is owed an exit. */
			seen_stage = -1;
			elapsed_ms = 0;
			continue;
		}
		if (stage != seen_stage) {
			seen_stage = stage;
			elapsed_ms = 0;
			continue;
		}
		elapsed_ms += STRAND_TICK_MS;
		if (elapsed_ms >= strand_deadline_ms) {
			/* The only announcer on a strand: main is parked in a worker's wait path or is
			 * idling for exactly this line. */
			strand_announce(0, 1);
			_exit(1);
		}
	}
}

/* ------------------------------------------------------------------ workers */

static void *
strand_ulock_waiter(void *arg)
{
	int me = (int)(intptr_t)arg;

	/* timeout 0 = INFINITE, exactly as the real ld waits: a lost wake must PARK the thread, not
	 * hand the caller a retryable timeout that hides the defect. */
	strand_ulock_ret[me] = __ulock_wait(STRAND_OP_WAIT, (void *)&strand_word,
	                                    (uint64_t)strand_word, 0);
	__atomic_fetch_sub(&strand_outstanding, 1, __ATOMIC_RELEASE);
	return NULL;
}

static void *
strand_psynch_worker(void *arg)
{
	(void)arg;
	for (int i = 0; i < STRAND_PSYNCH_ITERS; i++) {
		pthread_mutex_lock(&strand_mutex);
		__atomic_fetch_add(&strand_psynch_ops, 1, __ATOMIC_RELAXED);
		pthread_mutex_unlock(&strand_mutex);
	}
	__atomic_fetch_sub(&strand_outstanding, 1, __ATOMIC_RELEASE);
	return NULL;
}

/* Wait for every worker of the current stage, bounded: a stranded worker must be reported by the
 * watchdog, never joined forever. */
static int
strand_wait_outstanding(void)
{
	int elapsed_ms = 0;

	while (__atomic_load_n(&strand_outstanding, __ATOMIC_ACQUIRE) > 0) {
		if (elapsed_ms >= strand_deadline_ms)
			return 0;
		usleep(STRAND_TICK_MS * 1000);
		elapsed_ms += STRAND_TICK_MS;
	}
	return 1;
}

/* A stranded stage leaves main with nothing correct to do: joining would block in the same lost
 * wait. Idle until the watchdog prints the verdict and exits the process. */
static void
strand_await_watchdog(void)
{
	for (;;)
		usleep(STRAND_TICK_MS * 1000);
}

/* ------------------------------------------------------------------ stages */

/* Returns 0 only when a worker is stranded (the watchdog will report it). */
static int
strand_run_ulock_stage(void)
{
	pthread_t waiters[STRAND_WAITERS];

	strand_word = STRAND_WAIT_WORD;
	for (int i = 0; i < STRAND_WAITERS; i++)
		strand_ulock_ret[i] = STRAND_SENTINEL;
	__atomic_store_n(&strand_outstanding, STRAND_WAITERS, __ATOMIC_RELEASE);
	__atomic_store_n(&strand_stage, STRAND_STAGE_ULOCK_PARK, __ATOMIC_RELEASE);
	printf("IOS-UNFAIR-LOCK stage=ulock-park waiters=%d expect_host_futex=%d deadline_ms=%d word=0x%x\n",
	       STRAND_WAITERS, strand_expect_host_futex, strand_deadline_ms, strand_word);
	fflush(stdout);

	for (int i = 0; i < STRAND_WAITERS; i++) {
		if (pthread_create(&waiters[i], NULL, strand_ulock_waiter, (void *)(intptr_t)i) != 0) {
			printf("IOS-UNFAIR-LOCK pass=0 waiters=%d stranded=%d stage=ulock-park host_futex_wait=unknown depth_contract=incomplete reason=pthread_create\n",
			       STRAND_WAITERS, __atomic_load_n(&strand_outstanding, __ATOMIC_ACQUIRE));
			fflush(stdout);
			_exit(1);
		}
	}
	/* Every waiter must be parked before the first wake: only then is the sequence of remaining
	 * depths well defined, and the sibling fixture measures that this settles in 500 ms. The wakes
	 * then REPEAT until every waiter is out or the deadline passes -- a wake whose waiter had not
	 * parked yet is a no-op on both transports (ulock_wake.c rings the server queue AND the host
	 * futex), so repeating costs one syscall, while a single missed wake would be reported as a
	 * strand the runtime did not cause. */
	usleep(500 * 1000);
	int elapsed_ms = 0;
	while (__atomic_load_n(&strand_outstanding, __ATOMIC_ACQUIRE) > 0 &&
	       elapsed_ms < strand_deadline_ms) {
		(void)__ulock_wake(STRAND_OP_WAKE, (void *)&strand_word, 1);
		usleep(100 * 1000);
		elapsed_ms += 100;
	}

	if (!strand_wait_outstanding())
		return 0;

	for (int i = 0; i < STRAND_WAITERS; i++)
		pthread_join(waiters[i], NULL);

	printf("IOS-UNFAIR-LOCK ulock-park returns=%d,%d,%d,%d depth_contract=%s host_futex_wait=%s\n",
	       strand_ulock_ret[0], strand_ulock_ret[1], strand_ulock_ret[2], strand_ulock_ret[3],
	       strand_depth_contract() ? "ok" : "violated",
	       strand_hf_name());
	fflush(stdout);
	return 1;
}

static int
strand_run_psynch_stage(void)
{
	pthread_t workers[STRAND_PSYNCH_WORKERS];

	__atomic_store_n(&strand_psynch_ops, 0, __ATOMIC_RELAXED);
	__atomic_store_n(&strand_outstanding, STRAND_PSYNCH_WORKERS, __ATOMIC_RELEASE);
	__atomic_store_n(&strand_stage, STRAND_STAGE_PSYNCH_CONTEND, __ATOMIC_RELEASE);
	printf("IOS-UNFAIR-LOCK stage=psynch-contend workers=%d iters=%d\n",
	       STRAND_PSYNCH_WORKERS, STRAND_PSYNCH_ITERS);
	fflush(stdout);

	for (int i = 0; i < STRAND_PSYNCH_WORKERS; i++) {
		if (pthread_create(&workers[i], NULL, strand_psynch_worker, NULL) != 0) {
			printf("IOS-UNFAIR-LOCK pass=0 stranded=%d stage=psynch-contend host_futex_wait=unknown depth_contract=incomplete reason=pthread_create\n",
			       __atomic_load_n(&strand_outstanding, __ATOMIC_ACQUIRE));
			fflush(stdout);
			_exit(1);
		}
	}

	if (!strand_wait_outstanding())
		return 0;

	for (int i = 0; i < STRAND_PSYNCH_WORKERS; i++)
		pthread_join(workers[i], NULL);

	printf("IOS-UNFAIR-LOCK psynch-contend ops=%d\n",
	       __atomic_load_n(&strand_psynch_ops, __ATOMIC_ACQUIRE));
	fflush(stdout);
	return 1;
}

/* ------------------------------------------------------------------ setup */

static int
strand_env_is(const char *name, const char *value)
{
	const char *v = getenv(name);

	return v != NULL && strcmp(v, value) == 0;
}

static void
strand_read_env(void)
{
	const char *deadline = getenv("IOS_RING_STRAND_DEADLINE_MS");
	const char *arms = getenv("IOS_RING_STRAND_ARMS");

	if (deadline != NULL && atoi(deadline) > 0)
		strand_deadline_ms = atoi(deadline);

	if (arms != NULL && strcmp(arms, "ulock") == 0) {
		strand_run_psynch = 0;
	} else if (arms != NULL && strcmp(arms, "psynch") == 0) {
		strand_run_ulock = 0;
	}

	/* The runner declares the transport; the fixture then REQUIRES the guest-observable signature
	 * of that transport, so "the blocking op silently left the ring" cannot pass unnoticed. */
	strand_expect_host_futex = strand_env_is("DARLING_GUEST_RING_BLOCKING", "0");
}

int
main(void)
{
	pthread_t watchdog;
	int ok = 1;

	setvbuf(stdout, NULL, _IONBF, 0);
	strand_read_env();

	printf("IOS-UNFAIR-LOCK stage=init arms=%s%s expect_host_futex=%d deadline_ms=%d\n",
	       strand_run_ulock ? "ulock" : "", strand_run_psynch ? (strand_run_ulock ? "+psynch" : "psynch") : "",
	       strand_expect_host_futex, strand_deadline_ms);
	fflush(stdout);

	if (pthread_create(&watchdog, NULL, strand_watchdog, NULL) != 0) {
		printf("IOS-UNFAIR-LOCK pass=0 stranded=0 stage=init host_futex_wait=unknown depth_contract=incomplete reason=no_watchdog\n");
		fflush(stdout);
		return 1;
	}
	pthread_detach(watchdog);

	if (strand_run_ulock && !strand_run_ulock_stage())
		strand_await_watchdog();   /* never returns: the watchdog owns the verdict line */
	if (strand_run_psynch && !strand_run_psynch_stage())
		strand_await_watchdog();

	__atomic_store_n(&strand_stage, STRAND_STAGE_DONE, __ATOMIC_RELEASE);

	/* The declared transport must be the one the guest actually observed. Datagram arm: the parking
	 * IS the expected behaviour, so pass requires it. Product arm: the blocking family must ride the
	 * ring lane to the server-owned queue; a silent fall back to the host futex is the RED this
	 * fixture exists to report, so `host_futex_wait=1` (with depth_contract=violated) FAILS it. */
	if (strand_run_ulock) {
		int hf = strand_host_futex_wait();
		if (strand_expect_host_futex)
			ok = (hf == 1) && ok;
		else
			ok = (hf == 0 && strand_depth_contract()) && ok;
	}

	strand_announce(ok, 0);
	return ok ? 0 : 1;
}

/*
 * Reproducer for the psynch Ring path (bead dar-xu21).
 *
 * Background: the nine psynch ops were classified in DSERVER_RING_OP_CLASS but absent from
 * DSERVER_RING_C2S_OPCODES and from RING_GENERATED_SIMPLE, so the generated wrapper had no ring
 * fast path. sys_psynch_mutexwait and sys_psynch_mutexdrop call __simple_abort() whenever
 * dserver_rpc_<op> returns negative (libpthread's pthread_mutex_lock/unlock slow path reaches
 * __psynch_mutexwait at pthread_mutex.c:770), so the caller was killed instead of served.
 * darlingserver bce5bf9 added the nine ops to the set of record and to the generated ring set;
 * measured on the real iOS ld, the "psynch_mutex* failed internally: -32" aborts are gone.
 *
 * MEASURED AFTER THAT FIX (2026-10-08, prefix-ios): this fixture does NOT complete. Several
 * threads contending one mutex force __psynch_mutexwait, and the process then hangs -- the server
 * log shows its lanes attach and then no servicing, while the guest waits in gr_wait_reply
 * (dserver-ring.c:2437). That is the SAME failure the real ld hits (both ld threads are parked in
 * gr_wait_reply+0x12a at the crash core). So this fixture is the small reproducer for the REMAINING
 * psynch-over-the-ring request/reply failure, not a green regression for the routing fix.
 *
 * Run through the existing guest-fixture runner (no new framework):
 *   IOS_UNFAIR_LOCK_FIXTURE=tests/ios_psynch_route_guest.c \
 *   DARLING_BUILD_DIR=<build> DPREFIX=<prefix> bash tests/run-ios-unfair-lock-contract.sh
 */
#include <pthread.h>
#include <stdio.h>

#define THREADS 4
#define ITERS   200000

static pthread_mutex_t counter_lock = PTHREAD_MUTEX_INITIALIZER;
static long counter;

static void* worker(void* arg) {
	(void)arg;
	for (int i = 0; i < ITERS; ++i) {
		pthread_mutex_lock(&counter_lock);
		++counter;
		pthread_mutex_unlock(&counter_lock);
	}
	return 0;
}

int main(void) {
	pthread_t threads[THREADS];

	for (int i = 0; i < THREADS; ++i) {
		if (pthread_create(&threads[i], 0, worker, 0) != 0) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=pthread_create failed\n");
			return 1;
		}
	}
	worker(0);
	for (int i = 0; i < THREADS; ++i) {
		pthread_join(threads[i], 0);
	}

	if (counter != (long)(THREADS + 1) * ITERS) {
		printf("IOS-UNFAIR-LOCK pass=0 counter=%ld expected=%ld\n",
		    counter, (long)(THREADS + 1) * ITERS);
		return 1;
	}
	printf("IOS-UNFAIR-LOCK pass=1 psynch-mutex %d threads x %d iterations, counter=%ld\n",
	    THREADS + 1, ITERS, counter);
	return 0;
}

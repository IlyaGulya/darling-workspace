/*
 * Minimal churn reproducer: the shape of the full fixture's ld-shape-buckets arm, alone.
 *
 * Why it exists: that arm reaches `stage=ld-shape-buckets` and then stalls for as long as the harness waits
 * (measured at 900s), and it has never once completed in this environment -- before the queue the fixture
 * died earlier, at the fork arm. Keeping the arm's shape but not its scale makes the stall reproducible in
 * seconds:
 *
 *     IOS_UNFAIR_LOCK_FIXTURE=tests/ios_ulock_churn_guest.c DPREFIX=... DARLING_BUILD_DIR=... \
 *         bash tests/run-ios-unfair-lock-contract.sh
 *
 * Shape, taken from the ld core: a heap table of buckets, 0x100 bytes apart, with an os_unfair_lock at
 * +0x80 of each bucket (OS_UNFAIR_LOCK_INIT is zero, which is the free value), hammered by a pool of
 * short-lived threads so that threads die while others wait. A bounded, fixed iteration count means the
 * fixture either finishes or it is stuck.
 */
#include <os/lock.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define IOS_CHURN_THREADS 8
#define IOS_CHURN_ITERS 20000
#define IOS_CHURN_BUCKETS 8
#define IOS_CHURN_STRIDE 0x100
#define IOS_CHURN_LOCK_OFF 0x80

static unsigned char *table;
static volatile int go;
static volatile int done[IOS_CHURN_THREADS];

static void *
worker(void *arg)
{
	int me = (int)(intptr_t)arg;
	unsigned int seed = 0x9e3779b9u ^ (unsigned int)me;

	while (!__atomic_load_n(&go, __ATOMIC_ACQUIRE))
		usleep(100);

	for (int i = 0; i < IOS_CHURN_ITERS; i++) {
		seed = seed * 1103515245u + 12345u;
		int b = (int)((seed >> 16) % IOS_CHURN_BUCKETS);
		os_unfair_lock *l = (os_unfair_lock *)(table + (size_t)b * IOS_CHURN_STRIDE + IOS_CHURN_LOCK_OFF);
		os_unfair_lock_lock(l);
		/* A handful of instructions inside the critical section, so contention is real but short. */
		for (volatile int k = 0; k < 4; k++)
			;
		os_unfair_lock_unlock(l);
	}
	__atomic_store_n(&done[me], 1, __ATOMIC_RELEASE);
	return NULL;
}

int
main(void)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	printf("IOS-UNFAIR-LOCK stage=churn-only threads=%d iters=%d buckets=%d\n",
			IOS_CHURN_THREADS, IOS_CHURN_ITERS, IOS_CHURN_BUCKETS);

	table = calloc(IOS_CHURN_BUCKETS, IOS_CHURN_STRIDE);
	if (table == NULL) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=alloc\n");
		return 1;
	}

	pthread_t threads[IOS_CHURN_THREADS];
	for (int i = 0; i < IOS_CHURN_THREADS; i++)
		pthread_create(&threads[i], NULL, worker, (void *)(intptr_t)i);
	usleep(100 * 1000);
	__atomic_store_n(&go, 1, __ATOMIC_RELEASE);

	for (int i = 0; i < IOS_CHURN_THREADS; i++) {
		printf("IOS-UNFAIR-LOCK churn join=%d\n", i);
		pthread_join(threads[i], NULL);
	}

	int finished = 0;
	for (int i = 0; i < IOS_CHURN_THREADS; i++)
		finished += __atomic_load_n(&done[i], __ATOMIC_ACQUIRE);
	printf("IOS-UNFAIR-LOCK churn finished=%d/%d\n", finished, IOS_CHURN_THREADS);
	if (finished != IOS_CHURN_THREADS) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=churn_worker_incomplete\n");
		return 1;
	}
	printf("IOS-UNFAIR-LOCK pass=1 reason=churn_completes\n");
	return 0;
}

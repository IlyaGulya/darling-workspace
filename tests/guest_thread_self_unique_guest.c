/*
 * Two questions, both about the identity that libplatform's os_unfair_lock uses as the lock owner
 * (TSD slot __TSD_MACH_THREAD_SELF, read in libplatform/src/os/lock.c):
 *
 *  1. CENSUS: is every live guest thread's self port name non-zero, distinct from every other live
 *     thread's, and stable for the thread's lifetime? A zero token is not merely untidy: its lock does
 *     a CAS from NO_OWNER(0) to self(0), which succeeds while storing zero, and its unlock writes zero
 *     over whatever word it names.
 *
 *  2. MUTEX (the property that matters): can two threads be inside an os_unfair_lock critical section
 *     at once? The census cannot answer this -- dispatch may legitimately run several blocks on one
 *     worker -- so this arm tests the lock directly: every thread takes a shared unfair lock, checks
 *     that no one else is inside, holds it across a yield, then leaves. A violation means the guest's
 *     lock owner identity does not distinguish two running threads.
 *
 * Verdict lines (`dwdiag verdict` reads the first):
 *   UNFAIRLOCK-MUTEX pass=1 threads=K iterations=N violations=0
 *   TSSELF pass=1 threads=K zeros=0 unstable=0 duplicate-live=0
 */
#include <mach/mach.h>
#include <os/lock.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <dispatch/dispatch.h>

#define MAXREC 128
#define NTHREADS 12
#define NITER 200
#define NDISPATCH 8

static os_unfair_lock shared_lock = OS_UNFAIR_LOCK_INIT;
static volatile int inside;
static volatile int violations;
static pthread_mutex_t rec_lock = PTHREAD_MUTEX_INITIALIZER;

struct rec {
	unsigned token;
	unsigned long thread_id;
	int dispatch;
};

static struct rec recs[MAXREC];
static int nrecs;

static void
record(unsigned token, unsigned long thread_id, int dispatch)
{
	pthread_mutex_lock(&rec_lock);
	if (nrecs < MAXREC) {
		recs[nrecs].token = token;
		recs[nrecs].thread_id = thread_id;
		recs[nrecs].dispatch = dispatch;
		nrecs++;
	}
	pthread_mutex_unlock(&rec_lock);
}

/* The mutual-exclusion oracle. Everything it needs is a shared unfair lock and a flag. */
static void
mutex_round(void)
{
	os_unfair_lock_lock(&shared_lock);
	if (inside) {
		/* Another thread is in the critical section while we hold the lock: the owner identity did
		 * not exclude us, so the lock is not a lock. */
		__sync_fetch_and_add((int *)&violations, 1);
	}
	inside = 1;
	sched_yield();
	usleep(50);
	inside = 0;
	os_unfair_lock_unlock(&shared_lock);
}

static void *
thread_main(void *arg)
{
	(void)arg;
	unsigned long me = (unsigned long)pthread_mach_thread_np(pthread_self());
	record((unsigned)mach_thread_self(), me, 0);
	for (int i = 0; i < NITER; ++i) {
		mutex_round();
	}
	/* The token must not change while the thread lives. */
	record((unsigned)mach_thread_self(), me, 0);
	return NULL;
}

int
main(void)
{
	pthread_t threads[NTHREADS];
	int i;

	record((unsigned)mach_thread_self(), (unsigned long)pthread_mach_thread_np(pthread_self()), 0);

	for (i = 0; i < NTHREADS; ++i) {
		if (pthread_create(&threads[i], NULL, thread_main, NULL) != 0) {
			fprintf(stderr, "TSSELF pthread_create failed at %d\n", i);
			return 2;
		}
	}
	for (i = 0; i < NTHREADS; ++i) {
		pthread_join(threads[i], NULL);
	}

	/* Workqueue threads are a different population: the kernel starts them, and the guest's
	 * _pthread_wqthread_setup is what stores their port in the slot. */
	dispatch_apply(NDISPATCH, DISPATCH_APPLY_AUTO, ^(size_t index) {
		(void)index;
		record((unsigned)mach_thread_self(),
		    (unsigned long)pthread_mach_thread_np(pthread_self()), 1);
		for (int i = 0; i < 50; ++i) {
			mutex_round();
		}
	});

	int zeros = 0;
	int unstable = 0;
	int duplicate_live = 0;
	for (i = 0; i < nrecs; ++i) {
		if (recs[i].token == 0) {
			zeros++;
		}
		for (int j = i + 1; j < nrecs; ++j) {
			if (recs[i].thread_id == recs[j].thread_id && recs[i].token != recs[j].token) {
				unstable++;
			}
			/* Two DIFFERENT threads reporting one token is the defect; one worker running several
			 * blocks is not. */
			if (recs[i].thread_id != recs[j].thread_id && recs[i].token == recs[j].token) {
				duplicate_live++;
			}
		}
	}

	printf("TSSELF threads=%d records=%d zeros=%d unstable=%d duplicate-live=%d dispatch=%d\n", NTHREADS,
	    nrecs, zeros, unstable, duplicate_live, NDISPATCH);
	unsigned long first_id = 0;
	int distinct_threads = 0;
	for (i = 0; i < nrecs; ++i) {
		int seen = 0;
		for (int j = 0; j < i; ++j) {
			if (recs[j].thread_id == recs[i].thread_id) {
				seen = 1;
			}
		}
		if (!seen) {
			distinct_threads++;
		}
	}
	printf("TSSELF distinct-threads=%d first-token=0x%x\n", distinct_threads,
	    nrecs > 0 ? recs[0].token : 0);
	printf("UNFAIRLOCK-MUTEX pass=%d threads=%d iterations=%d violations=%d\n",
	    (violations == 0) ? 1 : 0, NTHREADS + NDISPATCH, NTHREADS * NITER + NDISPATCH * 50, violations);

	int ok = (zeros == 0) && (unstable == 0) && (duplicate_live == 0) && (violations == 0);
	printf("TSSELF pass=%d\n", ok ? 1 : 0);
	return ok ? 0 : 1;
}

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
/* OCCUPANCY, not a flag. A boolean set by the holder and cleared before its unlock can be read stale by an
 * admitted thread and, worse, cleared by a thread that was itself admitted while another was inside, which
 * hides later overlaps. A fetch-add/sub pair answers "was anyone already inside" atomically, and it stays
 * defined behaviour even when exclusion is broken -- an ordinary race here is undefined and would make the
 * oracle's own output unreliable exactly in the case it exists to report. */
static int occupancy;
static int violations;
static int direct_zero;
static pthread_mutex_t rec_lock = PTHREAD_MUTEX_INITIALIZER;

struct rec {
	unsigned token;
	unsigned long thread_id;
	int dispatch;
};

static struct rec recs[MAXREC];
static int nrecs;

/* Every census participant waits here, so "all these threads are live at once" is a fact of the run rather
 * than an assumption about scheduling order. Hand-rolled, because the barrier API is not declared in this
 * compilation mode and a missing declaration made the whole fixture fail to build. */
static pthread_mutex_t barrier_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t barrier_cond = PTHREAD_COND_INITIALIZER;
static int barrier_waiting;
static int barrier_total;
static unsigned main_direct;

static void
census_barrier(int participants)
{
	pthread_mutex_lock(&barrier_lock);
	if (barrier_total == 0) {
		barrier_total = participants;
	}
	barrier_waiting++;
	if (barrier_waiting == barrier_total) {
		barrier_waiting = 0;
		pthread_cond_broadcast(&barrier_cond);
	} else {
		while (barrier_waiting != 0) {
			pthread_cond_wait(&barrier_cond, &barrier_lock);
		}
	}
	pthread_mutex_unlock(&barrier_lock);
}

/*
 * Read this thread's TSD self slot WITHOUT touching the lock implementation: take a private, freshly
 * initialized lock and look at the word. libplatform stores the thread's self token there, so a word that is
 * still zero after a successful lock means the token this thread reads is zero -- a token that excludes
 * nobody, because the CAS from NO_OWNER(0) to self(0) succeeds while leaving the word free.
 */
/*
 * The DIRECT reader: the same byte libplatform reads as the owner identity, fetched without going through the
 * lock implementation at all. The private-lock probe below and this must agree; when they disagree, the probe
 * is the one that is wrong, because it observes a lock rather than the thread.
 */
static unsigned
direct_slot3(void)
{
	unsigned long v = 0;
#if defined(__x86_64__)
	__asm__ volatile("movq %%gs:24, %0" : "=r"(v));
#endif
	return (unsigned)v;
}

/* The lock-based probe, kept because it measures what the lock actually sees. */
static int
self_slot_is_zero(void)
{
	os_unfair_lock probe = OS_UNFAIR_LOCK_INIT;
	os_unfair_lock_lock(&probe);
	unsigned word = *(volatile unsigned *)&probe;
	os_unfair_lock_unlock(&probe);
	return word == 0;
}

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

/* What the lock word held around a violation. The guest may read its own lock: this is the test, not a probe
 * inside the lock's implementation. `owner` is the word with the low bit masked off, which is how the
 * implementation encodes "waiters exist". */
struct violation {
	unsigned word_before;
	unsigned word_after;
	unsigned own_token;
	unsigned holder_token;
};

static struct violation viol[16];
static int nviol;

/* The word's owner encoding: libplatform compares (current | 1) against self, so this is how the word is read
 * back. It is NOT a mask: a zero word means unlocked, not "owned by token 1". */
static unsigned
owner_of(unsigned word)
{
	return word | 1u;
}

/* The mutual-exclusion oracle. Everything it needs is a shared unfair lock and a flag. */
static void
mutex_round(void)
{
	unsigned before = __atomic_load_n((unsigned *)&shared_lock, __ATOMIC_SEQ_CST);
	os_unfair_lock_lock(&shared_lock);
	int was_occupied = __atomic_fetch_add(&occupancy, 1, __ATOMIC_SEQ_CST);
	unsigned after = __atomic_load_n((unsigned *)&shared_lock, __ATOMIC_SEQ_CST);
	if (was_occupied != 0) {
		/* Someone was already inside when the lock was granted to us: the owner identity did not
		 * exclude us. Record the word and both readers so the failure says which token it held
		 * instead of only that it happened. */
		__atomic_fetch_add(&violations, 1, __ATOMIC_SEQ_CST);
		unsigned slot = (unsigned)__atomic_fetch_add(&nviol, 1, __ATOMIC_SEQ_CST);
		if (slot < sizeof(viol) / sizeof(viol[0])) {
			viol[slot].word_before = before;
			viol[slot].word_after = after;
			viol[slot].own_token = (unsigned)mach_thread_self();
			viol[slot].holder_token = owner_of(after);
		}
	}
	sched_yield();
	usleep(50);
	__atomic_fetch_sub(&occupancy, 1, __ATOMIC_SEQ_CST);
	os_unfair_lock_unlock(&shared_lock);
}

static void *
thread_main(void *arg)
{
	(void)arg;
	unsigned long me = (unsigned long)pthread_mach_thread_np(pthread_self());
	unsigned direct = direct_slot3();
	if (direct == 0) {
		__atomic_fetch_add(&direct_zero, 1, __ATOMIC_SEQ_CST);
	}
	record((unsigned)mach_thread_self(), me, 0);
	census_barrier(NTHREADS + 1);
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

	main_direct = direct_slot3();
	if (main_direct == 0) {
		__atomic_fetch_add(&direct_zero, 1, __ATOMIC_SEQ_CST);
	}
	record((unsigned)mach_thread_self(), (unsigned long)pthread_mach_thread_np(pthread_self()), 0);

	for (i = 0; i < NTHREADS; ++i) {
		if (pthread_create(&threads[i], NULL, thread_main, NULL) != 0) {
			fprintf(stderr, "TSSELF pthread_create failed at %d\n", i);
			return 2;
		}
	}
	/* Join the census: at this point the main thread and all NTHREADS spawned threads have published their
	 * direct readings and are alive simultaneously. */
	census_barrier(NTHREADS + 1);
	for (i = 0; i < NTHREADS; ++i) {
		pthread_join(threads[i], NULL);
	}

	/* Workqueue threads are a different population: the kernel starts them, and the guest's
	 * _pthread_wqthread_setup is what stores their port in the slot. */
	dispatch_apply(NDISPATCH, DISPATCH_APPLY_AUTO, ^(size_t index) {
		(void)index;
		if (direct_slot3() == 0) {
			__atomic_fetch_add(&direct_zero, 1, __ATOMIC_SEQ_CST);
		}
		record((unsigned)mach_thread_self(),
		    (unsigned long)pthread_mach_thread_np(pthread_self()), 1);
		for (int i = 0; i < 50; ++i) {
			mutex_round();
		}
	});

	int zeros = 0;
	int unstable = 0;
	int duplicate_live = 0;
	int slot3_zero = 0;
	for (i = 0; i < 8; ++i) {
		/* Repeat: a slot burned into the pthread structure from an earlier thread would show up as an
		 * inconsistency between attempts, and this is the arm that names the defect's mechanism. */
		if (self_slot_is_zero()) {
			slot3_zero++;
		}
	}
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
	printf("TSSELF main-self-slot-zero probes=%d zero=%d\n", 8, slot3_zero);
	printf("TSSELF direct-slot3 main=0x%x participants-zero=%d\n", main_direct, direct_zero);
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

	for (i = 0; i < nviol && i < 16; ++i) {
		printf("UNFAIRLOCK-VIOLATION i=%d word_before=0x%x word_after=0x%x own=0x%x holder_in_word=0x%x\n", i,
		    viol[i].word_before, viol[i].word_after, viol[i].own_token, viol[i].holder_token);
	}

	int ok = (zeros == 0) && (unstable == 0) && (duplicate_live == 0) && (violations == 0) &&
	    (slot3_zero == 0) && (direct_zero == 0);
	printf("TSSELF pass=%d\n", ok ? 1 : 0);
	return ok ? 0 : 1;
}

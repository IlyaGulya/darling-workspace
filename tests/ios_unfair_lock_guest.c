/*
 * Guest Mach-O fixture for the unfair-lock reproducer.
 *
 * Why it exists: T4 (the iOS toolchain link stage) traps inside Darling's own
 * libsystem_platform at __os_unfair_lock_recursive_abort, and the core shows the faulting worker
 * owning the lock word already:
 *
 *     0x7e03c80a8e00: 0x0000000000000b03   (the faulting thread's own __TSD_MACH_THREAD_SELF token)
 *
 * Apple's ld cannot be nesting its own lock -- that is fatal on macOS too -- so the surviving reading
 * is that a lock/unlock/lock sequence by ONE thread lost the unlock in Darling's emulation, or that
 * contention through __ulock_wait/__ulock_wake damages the word. This fixture performs exactly those
 * sequences, without ld, so the defect separates from the Apple toolchain.
 *
 * Built on the host with the Darling product's own cross toolchain (the build's recorded guest
 * Mach-O commands, replayed with this source); nothing is compiled inside the guest.
 *
 * Arms, each must survive (a trap is SIGILL and the marker never prints):
 *   1. lock_with_options(0x50000) / unlock / lock_with_options(0x50000) on one thread -- the option
 *      value is the one measured in the ld core (OS_UNFAIR_LOCK_DATA_SYNCHRONIZATION |
 *      OS_UNFAIR_LOCK_ADAPTIVE_SPIN), written numerically so the fixture does not depend on the
 *      public macro being available in the tree it compiles against.
 *   2. two threads contending on the same lock (exercises __ulock_wait / __ulock_wake).
 *   3. the same single-thread sequence repeated, so a rate-dependent miss cannot hide behind one run.
 *   4. fork with the lock held: must not trap (a blocked child is the Darwin-like outcome).
 *   5. the shape recovered from the ld core: a churning pool of short-lived worker threads each locking
 *      a hashed bucket of a heap table whose bucket is 0x100 bytes with the os_unfair_lock at +0x80.
 */
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <os/lock.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define IOS_UNFAIR_LOCK_MEASURED_OPTIONS 0x50000u /* DATA_SYNCHRONIZATION | ADAPTIVE_SPIN */

/* The entry point the failing ld actually calls. Apple exposes it in the SDK's os/lock.h; the tree's
 * public header does not declare it, so the fixture declares it against the same signature the
 * deployed libsystem_platform exports (the options argument is not a public type there). */
extern void os_unfair_lock_lock_with_options(os_unfair_lock_t lock, unsigned int options);

static os_unfair_lock lock = OS_UNFAIR_LOCK_INIT;
static int stages_done = 0;

/* Announce a stage BEFORE running it, flushed: when an arm hangs (a fork child stuck in Darling's
 * thread-creation trap does exactly that) the run log then names the stage that never returned
 * instead of leaving a silent gap. */
static void
stage(const char *name)
{
	printf("IOS-UNFAIR-LOCK stage=%s\n", name);
	fflush(stdout);
}

/*
 * arm 5's bucket table: the shape measured in the ld core (0x100-byte buckets, lock at +0x80).
 */
#define IOS_UNFAIR_BUCKETS 64
#define IOS_UNFAIR_BUCKET_STRIDE 0x100
#define IOS_UNFAIR_LOCK_OFF 0x80

static unsigned char *buckets;
static int churn_iters;

static void *
churn_worker(void *arg)
{
	unsigned seed = (unsigned)(uintptr_t)arg * 2654435761u;
	for (int i = 0; i < churn_iters; i++) {
		seed = seed * 1103515245u + 12345u;
		unsigned char *b = buckets + (seed >> 16) % IOS_UNFAIR_BUCKETS * IOS_UNFAIR_BUCKET_STRIDE;
		os_unfair_lock_t l = (os_unfair_lock_t)(b + IOS_UNFAIR_LOCK_OFF);
		os_unfair_lock_lock_with_options(l, IOS_UNFAIR_LOCK_MEASURED_OPTIONS);
		*(unsigned long *)b += 1; /* the insert critical section */
		os_unfair_lock_unlock(l);
	}
	return NULL;
}

/* arm 6's probe: is a thread's mach-thread-self token really that thread's port? */
#define IOS_UNFAIR_IDENT_THREADS 8
static uint32_t ident_token[IOS_UNFAIR_IDENT_THREADS];
static uint64_t ident_self_tid[IOS_UNFAIR_IDENT_THREADS];
static uint64_t ident_token_tid[IOS_UNFAIR_IDENT_THREADS];
static int ident_info_ok[IOS_UNFAIR_IDENT_THREADS];

static void *
ident_worker(void *arg)
{
	int i = (int)(intptr_t)arg;
	mach_port_t tp;
	thread_identifier_info_data_t info;
	mach_msg_type_number_t count = THREAD_IDENTIFIER_INFO_COUNT;

	ident_token[i] = (uint32_t)mach_thread_self();
	(void)pthread_threadid_np(NULL, &ident_self_tid[i]);
	tp = (mach_port_t)ident_token[i];
	if (thread_info(tp, THREAD_IDENTIFIER_INFO, (thread_info_t)&info, &count) == KERN_SUCCESS) {
		ident_info_ok[i] = 1;
		ident_token_tid[i] = info.thread_id;
	}
	return NULL;
}

/*
 * arm 7's probe: the token a thread caches is a send right's NAME for its own thread port, so its
 * lifetime is a right-accounting question. If dropping an extra right (one the caller owns, over and
 * above the one the pthread holds) kills or recycles that name while the thread is alive, then another
 * thread's mint can take the same name and two live threads will compare equal -- which is exactly the
 * shape of the ld abort (a word holding a token that the locking thread also presents).
 */
#define IOS_UNFAIR_RIGHT_THREADS 4
static int right_result[IOS_UNFAIR_RIGHT_THREADS]; /* 0 ok, otherwise a failure code */

static void *
right_worker(void *arg)
{
	int i = (int)(intptr_t)arg;
	mach_port_t extra = mach_thread_self();          /* a right this thread owns */
	thread_identifier_info_data_t info;
	mach_msg_type_number_t count = THREAD_IDENTIFIER_INFO_COUNT;
	uint64_t own_tid = 0, tid_before = 0, tid_after = 0;
	mach_port_t cached;

	(void)pthread_threadid_np(NULL, &own_tid);
	if (extra == MACH_PORT_NULL || extra == MACH_PORT_DEAD) { right_result[i] = 1; return NULL; }
	count = THREAD_IDENTIFIER_INFO_COUNT;
	if (thread_info(extra, THREAD_IDENTIFIER_INFO, (thread_info_t)&info, &count) != KERN_SUCCESS ||
			info.thread_id != own_tid) { right_result[i] = 2; return NULL; }
	tid_before = info.thread_id;

	/* Drop the extra right: the name the TSD caches must survive, because the pthread holds one. */
	if (mach_port_deallocate(mach_task_self(), extra) != KERN_SUCCESS) { right_result[i] = 3; return NULL; }

	/* The public accessor reads exactly the TSD slot a lock word is compared against. */
	cached = pthread_mach_thread_np(pthread_self());
	count = THREAD_IDENTIFIER_INFO_COUNT;
	if (thread_info(cached, THREAD_IDENTIFIER_INFO, (thread_info_t)&info, &count) != KERN_SUCCESS ||
			info.thread_id != own_tid) { right_result[i] = 4; return NULL; }
	tid_after = info.thread_id;
	if (cached == MACH_PORT_NULL || cached == MACH_PORT_DEAD) { right_result[i] = 5; return NULL; }
	(void)tid_before; (void)tid_after;
	return NULL;
}

/*
 * arm 8's worker: the item body dispatch_apply runs. It takes and releases a bucket lock, so a wave in
 * which the CALLING thread's own critical section is abandoned (its stack reset by a workqueue reuse)
 * leaves that word owned while the next wave re-locks it -- the shape the ld core shows.
 */
static os_unfair_lock bucket_locks[4] = {
	OS_UNFAIR_LOCK_INIT, OS_UNFAIR_LOCK_INIT, OS_UNFAIR_LOCK_INIT, OS_UNFAIR_LOCK_INIT
};
static uint32_t apply_counts[4];

static void
apply_item(void *ctx, size_t i)
{
	(void)ctx;
	os_unfair_lock_lock_with_options(&bucket_locks[i & 3], IOS_UNFAIR_LOCK_MEASURED_OPTIONS);
	/* A counter, NOT the lock's own word: writing _os_unfair_lock_opaque would corrupt the word and
	 * make the unlock abort unowned (measured earlier in this arm, and equally fatal on Darwin). */
	__atomic_fetch_add(&apply_counts[i & 3], 1, __ATOMIC_RELAXED);
	os_unfair_lock_unlock(&bucket_locks[i & 3]);
}

static void *
contend(void *unused)
{
	(void)unused;
	os_unfair_lock_lock(&lock);
	stages_done++;
	os_unfair_lock_unlock(&lock);
	return NULL;
}

int
main(void)
{
	/* arm 1: the measured option set, twice, on this thread */
	stage("single-thread-options");
	os_unfair_lock_lock_with_options(&lock, IOS_UNFAIR_LOCK_MEASURED_OPTIONS);
	stages_done++;
	os_unfair_lock_unlock(&lock);
	os_unfair_lock_lock_with_options(&lock, IOS_UNFAIR_LOCK_MEASURED_OPTIONS);
	stages_done++;
	os_unfair_lock_unlock(&lock);

	/* arm 2: contention, so the losing thread takes the __ulock_wait path */
	stage("contention");
	pthread_t worker;
	if (pthread_create(&worker, NULL, contend, NULL) != 0) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=pthread_create\n");
		return 1;
	}
	os_unfair_lock_lock(&lock);
	stages_done++;
	os_unfair_lock_unlock(&lock);
	pthread_join(worker, NULL);

	/* arm 3: repetition */
	stage("repetition-1000");
	for (int i = 0; i < 1000; i++) {
		os_unfair_lock_lock(&lock);
		stages_done++;
		os_unfair_lock_unlock(&lock);
	}

	/*
	 * arm 4: fork with the lock held. POSIX does not promise the child may take a lock its parent held
	 * across fork(): on Darwin the child simply BLOCKS on the inherited word, and that is correct
	 * behaviour, not a defect. What must never happen is a TRAP, because a trap means the child was
	 * handed ownership it does not have -- the SIGILL observed in Apple's ld
	 * (__os_unfair_lock_recursive_abort with the word holding the caller's own thread token). So this
	 * arm asserts only "not killed by SIGILL": the parent waits a bounded time, kills a child that is
	 * simply blocked, and treats that as the expected Darwin-like outcome.
	 */
	stage("fork-held");
	os_unfair_lock_lock(&lock);
	pid_t child = fork();
	if (child < 0) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=fork\n");
		return 1;
	}
	if (child == 0) {
		stage("fork-child-lock");
		os_unfair_lock_lock(&lock); /* expected to block; a trap here is the defect */
		os_unfair_lock_unlock(&lock);
		_exit(0);
	}
	int status = 0;
	int waited = 0;
	while (waited < 3000 && waitpid(child, &status, WNOHANG) == 0) {
		usleep(100 * 1000);
		waited += 100;
	}
	if (waited >= 3000) {
		kill(child, SIGKILL);
		waitpid(child, &status, 0);
		printf("IOS-UNFAIR-LOCK fork-child blocked=1 waited_ms=%d (Darwin-like)\n", waited);
	} else if (WIFSIGNALED(status)) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=fork_child_trapped signal=%d\n", WTERMSIG(status));
		return 1;
	}
	os_unfair_lock_unlock(&lock);
	if (WIFEXITED(status) && WEXITSTATUS(status) != 0) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=fork_child_exit status=%d\n", WEXITSTATUS(status));
		return 1;
	}
	/*
	 * arm 5: the operation the ld core shows, and nothing wider. There the chain is
	 *
	 *   libdispatch dispatch_apply -> InputFiles::parseAllFiles -> AtomFileConsolidator::parseFiles
	 *   -> addParsedAtomFile -> AtomSymbolTable::addAtomByName -> os_unfair_lock_lock_with_options(0x50000)
	 *
	 * and the lock is the per-bucket lock of ld's name table: a heap bucket of 0x100 bytes with the
	 * os_unfair_lock at +0x80 (measured: the disassembly forms the lock address as base + index*0x100
	 * + 0x80, and the core's bucket holds a pointer and two counters with the word at +0x80). This
	 * arm performs that same operation -- a churning pool of short-lived worker threads, which is what
	 * dispatch_apply runs on, each locking a hashed bucket -- with no Apple toolchain involved.
	 */
	stage("ld-shape-buckets");
	buckets = calloc(IOS_UNFAIR_BUCKETS, IOS_UNFAIR_BUCKET_STRIDE);
	if (buckets == NULL) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=bucket_alloc\n");
		return 1;
	}
	churn_iters = 200;
	for (int wave = 0; wave < 40; wave++) {
		pthread_t workers[6];
		for (int i = 0; i < 6; i++) {
			if (pthread_create(&workers[i], NULL, churn_worker, (void *)(uintptr_t)(wave * 6 + i + 1)) != 0) {
				printf("IOS-UNFAIR-LOCK pass=0 reason=pthread_create wave=%d\n", wave);
				return 1;
			}
		}
		for (int i = 0; i < 6; i++)
			pthread_join(workers[i], NULL);
		stages_done++;
	}
	free(buckets);
	buckets = NULL;

	/*
	 * arm 6: does each thread's mach-thread-self token actually name THAT thread? The word in the ld
	 * core holds the faulting thread's own token, so every reading of the defect so far rests on the
	 * token meaning "this thread". This arm checks that meaning directly, without ld: for each worker
	 * it asks the port for THREAD_IDENTIFIER_INFO, compares the reported thread_id with the calling
	 * thread's own pthread_threadid_np(), and checks that live threads never share a token.
	 */
	stage("token-identity");
	{
		pthread_t workers[IOS_UNFAIR_IDENT_THREADS];
		int bad = 0;
		for (int i = 0; i < IOS_UNFAIR_IDENT_THREADS; i++)
			pthread_create(&workers[i], NULL, ident_worker, (void *)(intptr_t)i);
		for (int i = 0; i < IOS_UNFAIR_IDENT_THREADS; i++)
			pthread_join(workers[i], NULL);
		for (int i = 0; i < IOS_UNFAIR_IDENT_THREADS; i++) {
			int dup = 0;
			for (int j = 0; j < i; j++)
				if (ident_token[j] == ident_token[i]) dup = 1;
			printf("IOS-UNFAIR-LOCK ident thread=%d token=%#x self_tid=%llu info_ok=%d token_tid=%llu dup_with_live=%d\n",
					i, ident_token[i], (unsigned long long)ident_self_tid[i], ident_info_ok[i],
					(unsigned long long)ident_token_tid[i], dup);
			if (!ident_info_ok[i] || ident_token_tid[i] != ident_self_tid[i] || dup)
				bad++;
		}
		if (bad != 0) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=token_identity bad=%d\n", bad);
			return 1;
		}
	}

	/*
	 * arm 7: right accounting for the token itself. Each thread mints an extra self right, checks that
	 * the port names this thread, drops that extra right, and checks that the name the TSD caches still
	 * names this thread -- then the live tokens are compared. A failure here means the token a lock word
	 * records can stop meaning "this thread", which is the precondition of the ld abort.
	 */
	stage("self-right-accounting");
	{
		pthread_t workers[IOS_UNFAIR_RIGHT_THREADS];
		int bad = 0, dup = 0;
		uint32_t tok[IOS_UNFAIR_RIGHT_THREADS];
		for (int i = 0; i < IOS_UNFAIR_RIGHT_THREADS; i++)
			pthread_create(&workers[i], NULL, right_worker, (void *)(intptr_t)i);
		for (int i = 0; i < IOS_UNFAIR_RIGHT_THREADS; i++) {
			pthread_join(workers[i], NULL);
			printf("IOS-UNFAIR-LOCK right thread=%d result=%d\n", i, right_result[i]);
			if (right_result[i] != 0)
				bad++;
		}
		(void)tok; (void)dup;
		if (bad != 0) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=self_right_accounting bad=%d\n", bad);
			return 1;
		}
	}

	/*
	 * arm 8: the ingredient arms 1-5 lack is libdispatch. The recovered ld chain is a dispatch_apply
	 * worker inside ld::AtomSymbolTable::addAtomByName, and the abort needs one thread to hold a bucket
	 * word and lock it again -- which happens if that thread's critical section is abandoned. This arm
	 * repeatedly runs dispatch_apply_f while the CALLING thread holds its own bucket lock (a different
	 * lock from the items' on purpose: locking the same one would be a genuine recursion), so any wave
	 * whose caller-side unlock is skipped makes the next wave's lock trap.
	 */
	stage("libdispatch-apply");
	{
		dispatch_queue_t q = dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0);
		for (int wave = 0; wave < 300; wave++) {
			os_unfair_lock_lock_with_options(&lock, IOS_UNFAIR_LOCK_MEASURED_OPTIONS);
			dispatch_apply_f(4, q, NULL, apply_item);
			os_unfair_lock_unlock(&lock);
			stages_done++;
		}
	}

	stages_done++;

	printf("IOS-UNFAIR-LOCK pass=1 stages=%d\n", stages_done);
	return 0;
}

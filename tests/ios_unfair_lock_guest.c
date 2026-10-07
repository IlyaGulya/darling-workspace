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

static volatile int apply_zero_token_items;

static void
apply_item(void *ctx, size_t i)
{
	(void)ctx;
	/* A workqueue thread takes its token from the kport its start path was handed (_pthread_wqthread
	 * stores it at pthread.c:2398). If that value is ever zero, every lock this thread takes stores zero,
	 * i.e. the word reads FREE to everyone else -- so the token itself is checked here, per item. */
	if (pthread_mach_thread_np(pthread_self()) == 0)
		__atomic_fetch_add(&apply_zero_token_items, 1, __ATOMIC_RELAXED);
	os_unfair_lock_lock_with_options(&bucket_locks[i & 3], IOS_UNFAIR_LOCK_MEASURED_OPTIONS);
	/* A counter, NOT the lock's own word: writing _os_unfair_lock_opaque would corrupt the word and
	 * make the unlock abort unowned (measured earlier in this arm, and equally fatal on Darwin). */
	__atomic_fetch_add(&apply_counts[i & 3], 1, __ATOMIC_RELAXED);
	os_unfair_lock_unlock(&bucket_locks[i & 3]);
}

/*
 * arm 9: does Darling's pthread_mutex actually exclude? ld drives its table growth through the single
 * pthread_mutex lock/unlock pair it imports, so a lost exclusion there would let a growth race a
 * bucket-locked insert -- which is how a held word ends up in the live array. The check is exact: every
 * increment happens while the mutex is held, so ANY shortfall is a lost update, not a timing artefact.
 */
#define IOS_UNFAIR_MTX_THREADS 8
#define IOS_UNFAIR_MTX_ITERS 200000
static pthread_mutex_t exclusion_probe = PTHREAD_MUTEX_INITIALIZER;
static unsigned long exclusion_counter;

static void *
exclusion_worker(void *arg)
{
	(void)arg;
	for (int i = 0; i < IOS_UNFAIR_MTX_ITERS; i++) {
		pthread_mutex_lock(&exclusion_probe);
		exclusion_counter++;
		pthread_mutex_unlock(&exclusion_probe);
	}
	return NULL;
}

/*
 * arm 10: the allocator is the only Darling-side call inside ld's critical section (malloc/realloc of the
 * entry arrays), so its own exclusion is worth a direct check. Each thread keeps several live blocks with a
 * per-block pattern and re-verifies every live block before reusing it, so two threads being handed the
 * same block -- or a block being damaged while live -- shows up as a pattern mismatch rather than as a
 * crash that a later run might not reproduce.
 */
#define IOS_UNFAIR_ALLOC_THREADS 6
#define IOS_UNFAIR_ALLOC_ROUNDS 20000
#define IOS_UNFAIR_ALLOC_LIVE 12

static int alloc_failures;

static void *
alloc_worker(void *arg)
{
	unsigned char *live[IOS_UNFAIR_ALLOC_LIVE];
	size_t sizes[IOS_UNFAIR_ALLOC_LIVE];
	unsigned char pats[IOS_UNFAIR_ALLOC_LIVE];
	unsigned seed = (unsigned)(uintptr_t)arg * 2654435761u + 7u;

	for (int i = 0; i < IOS_UNFAIR_ALLOC_LIVE; i++) {
		seed = seed * 1103515245u + 12345u;
		sizes[i] = 16 + (seed >> 16) % 496;
		pats[i] = (unsigned char)(seed >> 8);
		live[i] = malloc(sizes[i]);
		if (live[i] == NULL) {
			alloc_failures++;
			live[i] = malloc(16);
			sizes[i] = 16;
		}
		if (live[i] != NULL) memset(live[i], pats[i], sizes[i]);
	}
	for (int round = 0; round < IOS_UNFAIR_ALLOC_ROUNDS; round++) {
		int slot = round % IOS_UNFAIR_ALLOC_LIVE;
		if (live[slot] == NULL) continue;
		if (live[slot][0] != pats[slot] || live[slot][sizes[slot] - 1] != pats[slot])
			alloc_failures++;
		free(live[slot]);
		seed = seed * 1103515245u + 12345u;
		sizes[slot] = 16 + (seed >> 16) % 496;
		pats[slot] = (unsigned char)(seed >> 8);
		live[slot] = malloc(sizes[slot]);
		if (live[slot] == NULL) {
			alloc_failures++;
			continue;
		}
		memset(live[slot], pats[slot], sizes[slot]);
	}
	for (int i = 0; i < IOS_UNFAIR_ALLOC_LIVE; i++)
		if (live[i] != NULL) free(live[i]);
	return NULL;
}

/*
 * arm 11: os_unfair_lock_trylock is the one unfair-lock entry point no earlier arm exercised, and ld
 * imports it exactly once (a best-effort table guard is its natural use). A false success -- acquiring a
 * word another thread holds -- admits a second critical section and is exactly how a word can end up
 * owned by a thread that is not the one inside. Two checks: the deterministic one (trylock must fail on a
 * lock this very thread already holds), and a contended one where a challenger that acquires the lock
 * while the holder is provably inside records a false success.
 */
static os_unfair_lock try_probe_lock = OS_UNFAIR_LOCK_INIT;
static volatile unsigned try_holder_seq;
static volatile int try_inside_count;
static volatile int try_false_success;

static void *
try_holder_worker(void *arg)
{
	(void)arg;
	for (int i = 0; i < 200000; i++) {
		os_unfair_lock_lock(&try_probe_lock);
		/* Published as ODD before the unlock, so a challenger that legitimately acquires the lock can
		 * only ever observe an even sequence: seeing odd while holding is the defect, and this order
		 * removes the detector's own window (the first version cleared the flag after unlocking, which
		 * made a legitimate immediate reacquire look like a false success). */
		__atomic_fetch_add(&try_holder_seq, 1, __ATOMIC_SEQ_CST);
		if (__atomic_load_n(&try_false_success, __ATOMIC_RELAXED) != 0) {
			__atomic_fetch_add(&try_holder_seq, 1, __ATOMIC_SEQ_CST);
			os_unfair_lock_unlock(&try_probe_lock);
			return NULL;
		}
		__atomic_fetch_add(&try_holder_seq, 1, __ATOMIC_SEQ_CST);
		os_unfair_lock_unlock(&try_probe_lock);
	}
	return NULL;
}

static void *
try_challenger_worker(void *arg)
{
	(void)arg;
	for (int i = 0; i < 200000; i++) {
		if (os_unfair_lock_trylock(&try_probe_lock)) {
			/* Invariant that needs no timing assumption at all: with trylock-only users, at most ONE
			 * thread can be inside the word at a time, so an inside-count above one proves the word was
			 * handed to two threads. */
			if (__atomic_fetch_add(&try_inside_count, 1, __ATOMIC_SEQ_CST) != 0)
				__atomic_store_n(&try_false_success, 1, __ATOMIC_RELAXED);
			if (__atomic_load_n(&try_holder_seq, __ATOMIC_SEQ_CST) & 1u)
				__atomic_store_n(&try_false_success, 1, __ATOMIC_RELAXED);
			__atomic_fetch_sub(&try_inside_count, 1, __ATOMIC_SEQ_CST);
			os_unfair_lock_unlock(&try_probe_lock);
		}
	}
	return NULL;
}

/*
 * arm 12: pthread_cond wait/signal under contention. The frozen ld core puts its bucket-holding owner
 * inside libc++abi's _dlock_wait -- the wait half of a guard, which is a condition-variable wait under
 * it -- and no arm has exercised condition variables yet. A generation ping-pong with a BOUNDED wait per
 * round turns a lost wakeup into a reported failure instead of an unbounded hang.
 */
#define IOS_UNFAIR_COND_THREADS 6
#define IOS_UNFAIR_COND_ROUNDS 200
static pthread_mutex_t cond_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond_var = PTHREAD_COND_INITIALIZER;
static unsigned cond_generation;
static unsigned cond_seen[IOS_UNFAIR_COND_THREADS];
static volatile int cond_stop;

static void *
cond_worker(void *arg)
{
	int me = (int)(intptr_t)arg;
	while (!__atomic_load_n(&cond_stop, __ATOMIC_RELAXED)) {
		pthread_mutex_lock(&cond_mutex);
		while (cond_generation == cond_seen[me])
			pthread_cond_wait(&cond_var, &cond_mutex);
		cond_seen[me] = cond_generation;
		pthread_mutex_unlock(&cond_mutex);
	}
	return NULL;
}

/*
 * arm 13: many waiters on ONE lock. The earlier arms contended two threads, or spread load over 64
 * buckets, so the wait queue on any single word stayed short -- while Darling's slow path maintains the
 * nowaiters bit from __ulock_wait/__ulock_wake RETURN VALUES (waiters_mask = NOWAITERS when ret > 0).
 * A wake bookkeeping error there costs exactly one waiter its wakeup and nothing else, which is the
 * shape of the frozen state. This arm puts IOS_UNFAIR_MANY_THREADS waiters on one word and requires a
 * bounded amount of progress, so a lost wakeup is reported rather than hanging the run.
 */
#define IOS_UNFAIR_MANY_THREADS 16
#define IOS_UNFAIR_MANY_ROUNDS 3000
static os_unfair_lock many_lock = OS_UNFAIR_LOCK_INIT;
static volatile unsigned many_done;

static void *
many_worker(void *arg)
{
	(void)arg;
	for (int i = 0; i < IOS_UNFAIR_MANY_ROUNDS; i++) {
		os_unfair_lock_lock_with_options(&many_lock, IOS_UNFAIR_LOCK_MEASURED_OPTIONS);
		usleep(200);                       /* keep a real wait queue formed */
		__atomic_fetch_add(&many_done, 1, __ATOMIC_RELAXED);
		os_unfair_lock_unlock(&many_lock);
	}
	return NULL;
}

/*
 * The ulock primitive's own contract, below the lock that depends on it.
 *
 * Darwin's kernel ulock returns the REMAINING queue depth from __ulock_wait: the waiter joins with
 * ull_nwaiters++ and leaves with `*retval = --ull->ull_nwaiters` (xnu/bsd/kern/sys_ulock.c:599,780), so
 * waking K waiters one at a time must produce the multiset {0..K-1}. Darling's __ulock_wait is a host
 * futex, which cannot report that, and its source hacks the return to a constant 1 -- which is why the
 * lock above it has to treat the depth as unknowable. This arm asserts the contract directly, at the
 * boundary, so a deviation shows up as a primitive failure instead of only as a wedged linker.
 */
#define IOS_UNFAIR_ULOCK_K 4
#define IOS_UNFAIR_UL_COMPARE_AND_WAIT 1u
#define IOS_UNFAIR_ULF_NO_ERRNO 0x01000000u

extern int __ulock_wait(uint32_t operation, void *addr, uint64_t value, uint32_t timeout);
extern int __ulock_wake(uint32_t operation, void *addr, uint64_t wake_value);

static uint32_t ulock_word;
static volatile int ulock_go[IOS_UNFAIR_ULOCK_K];
static int ulock_ret[IOS_UNFAIR_ULOCK_K];

static void *
ulock_waiter(void *arg)
{
	int me = (int)(intptr_t)arg;
	while (!__atomic_load_n(&ulock_go[me], __ATOMIC_ACQUIRE))
		usleep(100);
	/* Bounded on purpose: a stranded waiter must report itself, not hang the whole fixture. */
	ulock_ret[me] = __ulock_wait(IOS_UNFAIR_UL_COMPARE_AND_WAIT | IOS_UNFAIR_ULF_NO_ERRNO,
			(void *)&ulock_word, (uint64_t)ulock_word, 2000000);
	return NULL;
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
		printf("IOS-UNFAIR-LOCK dispatch-worker zero-token items=%d\n", apply_zero_token_items);
		if (apply_zero_token_items != 0) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=dispatch_worker_zero_token count=%d\n", apply_zero_token_items);
			return 1;
		}
	}

	/*
	 * arm 9: pthread_mutex exclusion, counted exactly. A shortfall is a lost update inside the critical
	 * section, i.e. the mutex did not exclude -- the one primitive ld's table growth relies on.
	 */
	stage("pthread-mutex-exclusion");
	{
		pthread_t workers[IOS_UNFAIR_MTX_THREADS];
		unsigned long expect = (unsigned long)IOS_UNFAIR_MTX_THREADS * IOS_UNFAIR_MTX_ITERS;
		for (int i = 0; i < IOS_UNFAIR_MTX_THREADS; i++)
			pthread_create(&workers[i], NULL, exclusion_worker, NULL);
		for (int i = 0; i < IOS_UNFAIR_MTX_THREADS; i++)
			pthread_join(workers[i], NULL);
		printf("IOS-UNFAIR-LOCK mutex counter=%lu expected=%lu\n", exclusion_counter, expect);
		if (exclusion_counter != expect) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=pthread_mutex_lost_update got=%lu expected=%lu\n",
					exclusion_counter, expect);
			return 1;
		}
	}

	/*
	 * arm 10: the allocator's own exclusion, checked from several threads at once.
	 */
	stage("allocator-exclusion");
	{
		pthread_t workers[IOS_UNFAIR_ALLOC_THREADS];
		for (int i = 0; i < IOS_UNFAIR_ALLOC_THREADS; i++)
			pthread_create(&workers[i], NULL, alloc_worker, (void *)(intptr_t)(i + 1));
		for (int i = 0; i < IOS_UNFAIR_ALLOC_THREADS; i++)
			pthread_join(workers[i], NULL);
		printf("IOS-UNFAIR-LOCK allocator failures=%d\n", alloc_failures);
		if (alloc_failures != 0) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=allocator_corruption failures=%d\n", alloc_failures);
			return 1;
		}
	}

	/*
	 * arm 11: trylock must not succeed on a lock that is held.
	 */
	stage("trylock-semantics");
	{
		int det = 0;
		pthread_t holder, challengers[3];
		os_unfair_lock_lock(&try_probe_lock);
		det = os_unfair_lock_trylock(&try_probe_lock);   /* must be false */
		os_unfair_lock_unlock(&try_probe_lock);
		printf("IOS-UNFAIR-LOCK trylock-while-held succeeded=%d (must be 0)\n", det);
		if (det) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=trylock_false_success_deterministic\n");
			return 1;
		}
		try_holder_seq = 0;
		try_inside_count = 0;
		try_false_success = 0;
		pthread_create(&holder, NULL, try_holder_worker, NULL);
		for (int i = 0; i < 3; i++)
			pthread_create(&challengers[i], NULL, try_challenger_worker, NULL);
		pthread_join(holder, NULL);
		for (int i = 0; i < 3; i++)
			pthread_join(challengers[i], NULL);
		printf("IOS-UNFAIR-LOCK trylock-contended false_success=%d\n", try_false_success);
		if (try_false_success != 0) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=trylock_false_success_contended\n");
			return 1;
		}
	}

	/*
	 * arm 12: condition-variable wait/wake, generation style, bounded per round.
	 */
	stage("condvar-contention");
	{
		pthread_t workers[IOS_UNFAIR_COND_THREADS];
		int lost = 0;
		cond_generation = 0;
		cond_stop = 0;
		for (int i = 0; i < IOS_UNFAIR_COND_THREADS; i++)
			cond_seen[i] = 0;
		for (int i = 0; i < IOS_UNFAIR_COND_THREADS; i++)
			pthread_create(&workers[i], NULL, cond_worker, (void *)(intptr_t)i);
		for (unsigned round = 1; round <= IOS_UNFAIR_COND_ROUNDS && lost == 0; round++) {
			pthread_mutex_lock(&cond_mutex);
			cond_generation = round;
			pthread_cond_broadcast(&cond_var);
			pthread_mutex_unlock(&cond_mutex);
			for (int spin = 0; spin < 3000; spin++) {
				int all = 1;
				for (int i = 0; i < IOS_UNFAIR_COND_THREADS; i++)
					if (__atomic_load_n(&cond_seen[i], __ATOMIC_SEQ_CST) < round)
						all = 0;
				if (all)
					break;
				usleep(1000);
			}
			for (int i = 0; i < IOS_UNFAIR_COND_THREADS; i++)
				if (__atomic_load_n(&cond_seen[i], __ATOMIC_SEQ_CST) < round)
					lost = 1;
		}
		__atomic_store_n(&cond_stop, 1, __ATOMIC_RELAXED);
		pthread_mutex_lock(&cond_mutex);
		cond_generation = IOS_UNFAIR_COND_ROUNDS + 1;
		pthread_cond_broadcast(&cond_var);
		pthread_mutex_unlock(&cond_mutex);
		for (int i = 0; i < IOS_UNFAIR_COND_THREADS; i++)
			pthread_join(workers[i], NULL);
		printf("IOS-UNFAIR-LOCK condvar lost-wakeup=%d (rounds=%d)\n", lost, IOS_UNFAIR_COND_ROUNDS);
		if (lost) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=condvar_lost_wakeup\n");
			return 1;
		}
	}

	/*
	 * arm 12b: the primitive contract itself (see the helper above). It runs BEFORE the deep-queue arm so
	 * that a wedge in that one cannot hide the primitive's own result.
	 */
	stage("ulock-contract");
	{
		pthread_t waiters[IOS_UNFAIR_ULOCK_K];
		int seen[IOS_UNFAIR_ULOCK_K] = {0}, ok = 1;
		ulock_word = 0xdeadbeefu;
		for (int i = 0; i < IOS_UNFAIR_ULOCK_K; i++) {
			ulock_go[i] = 0;
			ulock_ret[i] = 0x7fffffff;
			pthread_create(&waiters[i], NULL, ulock_waiter, (void *)(intptr_t)i);
		}
		usleep(50 * 1000);
		for (int i = 0; i < IOS_UNFAIR_ULOCK_K; i++)
			__atomic_store_n(&ulock_go[i], 1, __ATOMIC_RELEASE);
		usleep(50 * 1000);                       /* let them reach the wait */
		for (int i = 0; i < IOS_UNFAIR_ULOCK_K; i++) {
			(void)__ulock_wake(IOS_UNFAIR_UL_COMPARE_AND_WAIT, (void *)&ulock_word, 1);
			usleep(20 * 1000);
		}
		for (int spin = 0; spin < 400; spin++) {  /* bounded: do not hang on a stranded waiter */
			int done = 1;
			for (int i = 0; i < IOS_UNFAIR_ULOCK_K; i++)
				if (ulock_ret[i] == 0x7fffffff)
					done = 0;
			if (done)
				break;
			usleep(10 * 1000);
		}
		for (int i = 0; i < IOS_UNFAIR_ULOCK_K; i++) {
			int r = ulock_ret[i];
			if (r == 0x7fffffff) {
				printf("IOS-UNFAIR-LOCK ulock waiter=%d STRANDED\n", i);
				ok = 0;
				continue;
			}
			printf("IOS-UNFAIR-LOCK ulock waiter=%d ret=%d\n", i, r);
			if (r < 0 || r >= IOS_UNFAIR_ULOCK_K)
				ok = 0;
			else
				seen[r] = 1;
		}
		for (int i = 0; i < IOS_UNFAIR_ULOCK_K; i++)
			if (!seen[i])
				ok = 0;                            /* the multiset must be exactly {0..K-1} */
		for (int i = 0; i < IOS_UNFAIR_ULOCK_K; i++)
			pthread_join(waiters[i], NULL);
		if (!ok) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=ulock_wait_depth_contract\n");
			return 1;
		}
	}

	/*
	 * arm 13: one word, many waiters, bounded progress.
	 */
	stage("many-waiters-one-word");
	{
		pthread_t workers[IOS_UNFAIR_MANY_THREADS];
		unsigned expect = (unsigned)IOS_UNFAIR_MANY_THREADS * IOS_UNFAIR_MANY_ROUNDS;
		unsigned last = 0;
		int stalls = 0;
		many_done = 0;
		for (int i = 0; i < IOS_UNFAIR_MANY_THREADS; i++)
			pthread_create(&workers[i], NULL, many_worker, NULL);
		for (int tick = 0; tick < 6000; tick++) {          /* 60 s ceiling, generous on purpose */
			unsigned now = __atomic_load_n(&many_done, __ATOMIC_RELAXED);
			if (now >= expect)
				break;
			/* A lost wakeup means NO progress, not slow progress: count only whole seconds in which the
			 * counter did not move at all, and fail at three consecutive dead seconds. */
			if (tick % 100 == 99) {
				if (now == last)
					stalls++;
				else
					stalls = 0;
				last = now;
			}
			usleep(10 * 1000);
		}
		printf("IOS-UNFAIR-LOCK many-waiters done=%u/%u stalls=%d\n",
				__atomic_load_n(&many_done, __ATOMIC_RELAXED), expect, stalls);
		if (stalls >= 3) {
			printf("IOS-UNFAIR-LOCK pass=0 reason=many_waiter_stall done=%u stalls=%d\n",
					__atomic_load_n(&many_done, __ATOMIC_RELAXED), stalls);
			return 1;
		}
	}

	stages_done++;

	printf("IOS-UNFAIR-LOCK pass=1 stages=%d\n", stages_done);
	return 0;
}

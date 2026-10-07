/*
 * The guest ulock primitive on its own, with nothing above or beside it.
 *
 * Purpose: when the full fixture (tests/ios_unfair_lock_guest.c) fails, the failure is 12 stages deep and
 * a run costs over a minute, so attributing it is expensive. This fixture runs the primitive's contract in
 * two variants, in one short run, and prints each result before the next starts:
 *
 *   variant=spin    no usleep anywhere: the ulock/futex path alone
 *   variant=usleep  the same waits paced with usleep, i.e. the semaphore path alongside them
 *
 * If the server dies during the second variant, the first variant's line is already in the log and says
 * whether the primitive alone is enough to provoke it. The contract it asserts is Darwin's: __ulock_wait
 * returns the remaining queue depth (xnu/bsd/kern/sys_ulock.c:780, `*retval = --ull->ull_nwaiters`), so
 * waking K waiters one at a time yields the multiset {0..K-1}.
 *
 * Verdict: `pass=1` only when every waiter returned a real value, the multiset is exactly {0..K-1}, no
 * waiter was stranded, and the usleep variant completed. With the futex emulation (which returns a constant
 * 1) the spin variant reports a violation, so this is the RED for bead dar-lf4b and its GREEN gate.
 *
 * Run it with: IOS_UNFAIR_LOCK_FIXTURE=tests/ios_ulock_contract_guest.c DPREFIX=... DARLING_BUILD_DIR=...
 *              bash tests/run-ios-unfair-lock-contract.sh
 */
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

#define IOS_ULOCK_K 4
#define IOS_ULOCK_OP_WAIT (1u | 0x01000000u) /* UL_COMPARE_AND_WAIT | ULF_NO_ERRNO */
#define IOS_ULOCK_OP_WAKE (1u)               /* UL_COMPARE_AND_WAIT */

extern int __ulock_wait(uint32_t operation, void *addr, uint64_t value, uint32_t timeout);
extern int __ulock_wake(uint32_t operation, void *addr, uint64_t wake_value);

static uint32_t ulock_word;
static volatile int ulock_go[IOS_ULOCK_K];
static int ulock_ret[IOS_ULOCK_K];
static volatile int variant_sleeps;

static void *
ulock_waiter(void *arg)
{
	int me = (int)(intptr_t)arg;

	while (!__atomic_load_n(&ulock_go[me], __ATOMIC_ACQUIRE)) {
		if (variant_sleeps)
			usleep(100);
	}
	/* Bounded on purpose: a stranded waiter must report itself instead of hanging the fixture. */
	ulock_ret[me] = __ulock_wait(IOS_ULOCK_OP_WAIT, (void *)&ulock_word,
			(uint64_t)ulock_word, 2000000);
	return NULL;
}

static void
spin(long rounds)
{
	for (volatile long i = 0; i < rounds; i++)
		;
}

/* Returns 1 when this variant satisfied the contract: every waiter returned, and the multiset of returns is
 * exactly {0..K-1}. */
static int
run_variant(const char *name, int sleeps)
{
	pthread_t waiters[IOS_ULOCK_K];
	int seen[IOS_ULOCK_K] = {0}, ok = 1, stranded = 0;

	variant_sleeps = sleeps;
	ulock_word = 0xdeadbeefu;
	for (int i = 0; i < IOS_ULOCK_K; i++) {
		ulock_go[i] = 0;
		ulock_ret[i] = 0x7fffffff;
		pthread_create(&waiters[i], NULL, ulock_waiter, (void *)(intptr_t)i);
	}
	printf("IOS-UNFAIR-LOCK variant=%s start\n", name);
	fflush(stdout);
	if (sleeps)
		usleep(50 * 1000);
	else
		spin(40000000L); /* let the queue fill without touching a semaphore */
	for (int i = 0; i < IOS_ULOCK_K; i++)
		__atomic_store_n(&ulock_go[i], 1, __ATOMIC_RELEASE);
	if (sleeps)
		usleep(50 * 1000);
	else
		spin(40000000L);
	for (int i = 0; i < IOS_ULOCK_K; i++) {
		(void)__ulock_wake(IOS_ULOCK_OP_WAKE, (void *)&ulock_word, 1);
		if (sleeps)
			usleep(20 * 1000);
		else
			spin(10000000L);
	}
	for (int i = 0; i < IOS_ULOCK_K; i++)
		pthread_join(waiters[i], NULL);

	printf("IOS-UNFAIR-LOCK variant=%s", name);
	for (int i = 0; i < IOS_ULOCK_K; i++)
		printf(" ret%d=%d", i, ulock_ret[i]);
	printf("\n");
	fflush(stdout);
	for (int i = 0; i < IOS_ULOCK_K; i++) {
		int r = ulock_ret[i];
		if (r == 0x7fffffff) {
			stranded++;
			ok = 0;
			continue;
		}
		if (r < 0 || r >= IOS_ULOCK_K)
			ok = 0;
		else
			seen[r] = 1;
	}
	for (int i = 0; i < IOS_ULOCK_K; i++)
		if (!seen[i])
			ok = 0; /* the multiset must be exactly {0..K-1} */
	printf("IOS-UNFAIR-LOCK variant=%s depth_contract=%s stranded=%d\n", name,
			ok ? "ok" : "violated", stranded);
	fflush(stdout);
	return ok;
}

int
main(void)
{
	int ok;

	setvbuf(stdout, NULL, _IONBF, 0);
	printf("IOS-UNFAIR-LOCK stage=ulock-only K=%d\n", IOS_ULOCK_K);
	ok = run_variant("spin", 0);   /* the ulock/futex path alone */
	/*
	 * Attribution control: usleep with no waiter parked. If the next variant fails while this one completes,
	 * the hazard needs a thread blocked in the futex, not merely the semaphore path.
	 */
	printf("IOS-UNFAIR-LOCK variant=usleep-idle start\n");
	fflush(stdout);
	for (int i = 0; i < 5; i++)
		usleep(20 * 1000);
	printf("IOS-UNFAIR-LOCK variant=usleep-idle done\n");
	fflush(stdout);
	/* Reaching this line at all is the second variant's first result: it must not hang or panic. */
	ok = run_variant("usleep", 1) && ok;
	printf("IOS-UNFAIR-LOCK pass=%d reason=primitive_depth_contract\n", ok ? 1 : 0);
	return ok ? 0 : 1;
}

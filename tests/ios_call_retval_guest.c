/*
 * One dispatched call must report its OWN return value to the guest.
 *
 * Darwin's BSD dispatch creates a fresh `int32_t retval = 0` for every syscall, so a trap body is free
 * to leave that value untouched when it has nothing to report. That is exactly what the ULF_NO_ERRNO
 * convention in ulock_wake does on SUCCESS: the negated errno, when there is one, is the only thing it
 * writes (xnu/bsd/kern/sys_ulock.c, `munge_retval`: `if ((flags & ULF_NO_ERRNO) && (ret != 0)) {
 * *retval = -ret; ret = 0; }`). If a server instead keeps one value per thread and reuses it across
 * calls, a successful ulock_wake hands the guest the PREVIOUS call's value.
 *
 * Measured on the real Xcode 26.3 ld, which is why this fixture exists (bead dar-lf4b): the linker
 * unlocked a contended bucket lock, ulock_wake succeeded, and libplatform's
 * `_os_unfair_lock_unlock_slow` received a stale -1. That function accepts only 0, -ENOENT and -EINTR
 * (it tests the negated result with `sub $2` / `sub $4`) and executes its `ud2` -- "ulock_wake
 * failure" -- for anything else, so the linker died of SIGILL and clang reported "linker command
 * failed due to signal".
 *
 * Driver, on one thread and with no other call in between:
 *   1. take the lock, then let a peer queue on it (the peer's unlock must take the slow path);
 *   2. seed THIS thread's per-call value with something the unlock slow path REJECTS -- a rejected
 *      __ulock_wake (invalid opcode -> EINVAL) writes -EINVAL into it;
 *   3. unlock. The only call between the seed and this unlock is the unlock's own __ulock_wake, which
 *      SUCCEEDS and therefore writes nothing: if the value is reused, libplatform traps here.
 *
 * Verdict: `pass=1` only when the unlock survives and the peer makes progress. Without the fresh
 * per-call value the process dies of SIGILL and prints no verdict at all.
 *
 * Run it with: IOS_UNFAIR_LOCK_FIXTURE=tests/ios_call_retval_guest.c DPREFIX=... DARLING_BUILD_DIR=...
 *              bash tests/run-ios-unfair-lock-contract.sh
 */
#include <os/lock.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

/* bsd/sys/ulock.h: the opcode is the low byte and ULF_NO_ERRNO is a flag; 0x7f is neither
 * UL_COMPARE_AND_WAIT (1) nor UL_UNFAIR_LOCK (2), so the server rejects it with EINVAL. */
#define IOS_CALL_RETVAL_ULF_NO_ERRNO 0x01000000u
#define IOS_CALL_RETVAL_UL_INVALID_OPCODE 0x0000007Fu

extern int __ulock_wake(uint32_t operation, void *addr, uint64_t wake_value);

static os_unfair_lock call_retval_lock = OS_UNFAIR_LOCK_INIT;
static volatile int call_retval_queued;
static volatile int call_retval_peer_done;

static void *
call_retval_peer(void *arg)
{
	(void)arg;
	__atomic_store_n(&call_retval_queued, 1, __ATOMIC_RELEASE);
	os_unfair_lock_lock(&call_retval_lock);
	os_unfair_lock_unlock(&call_retval_lock);
	__atomic_store_n(&call_retval_peer_done, 1, __ATOMIC_RELEASE);
	return NULL;
}

int
main(void)
{
	pthread_t peer;
	int seed;

	printf("IOS-UNFAIR-LOCK stage=call-retval-freshness\n");
	fflush(stdout);

	os_unfair_lock_lock(&call_retval_lock);
	pthread_create(&peer, NULL, call_retval_peer, NULL);
	for (int i = 0; i < 2000 && !__atomic_load_n(&call_retval_queued, __ATOMIC_ACQUIRE); i++)
		usleep(1000);
	usleep(200 * 1000); /* let the peer park in __ulock_wait */

	seed = __ulock_wake(IOS_CALL_RETVAL_UL_INVALID_OPCODE | IOS_CALL_RETVAL_ULF_NO_ERRNO,
			(void *)&call_retval_lock, 0);

	os_unfair_lock_unlock(&call_retval_lock); /* SIGILL here when the value is reused */

	pthread_join(peer, NULL);

	printf("IOS-UNFAIR-LOCK seed=%d peer_done=%d\n", seed,
			__atomic_load_n(&call_retval_peer_done, __ATOMIC_ACQUIRE));
	fflush(stdout);

	if (!__atomic_load_n(&call_retval_peer_done, __ATOMIC_ACQUIRE)) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=peer_did_not_acquire\n");
		return 1;
	}

	printf("IOS-UNFAIR-LOCK pass=1 reason=call_retval_freshness\n");
	return 0;
}

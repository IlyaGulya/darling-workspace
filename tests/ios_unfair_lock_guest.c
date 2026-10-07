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
 */
#include <os/lock.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
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
	stages_done++;

	printf("IOS-UNFAIR-LOCK pass=1 stages=%d\n", stages_done);
	return 0;
}

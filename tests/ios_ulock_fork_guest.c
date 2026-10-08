/*
 * Minimal fork reproducer: the fork-child half of the unfair-lock fixture, alone.
 *
 * Why it exists: the full fixture spends over a minute before it reaches the fork arm, and the arm is the
 * one thing that fails under the server-side ulock queue. This fixture does only that arm, so a fix can be
 * judged in seconds through the same runner:
 *
 *     IOS_UNFAIR_LOCK_FIXTURE=tests/ios_ulock_fork_guest.c DPREFIX=... DARLING_BUILD_DIR=... \
 *         bash tests/run-ios-unfair-lock-contract.sh
 *
 * The expectation is Darwin's: a child forked from a process holding an unfair lock BLOCKS when it takes
 * the same lock (its inherited holder thread does not exist in the child), and it must not trap. Measured
 * under the queue: the child enters the inherited process-control lane instead
 * ([plane-retry] first -1 op=7), then faults with SIGSEGV, which is the defect this fixture pins.
 */
#include <mach-o/dyld.h>
#include <os/lock.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static os_unfair_lock lock = OS_UNFAIR_LOCK_INIT;

int
main(void)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	printf("IOS-UNFAIR-LOCK stage=fork-only\n");
	/* Reported so a fault address in the run log can be turned into an image offset: the child inherits
	 * this slide across fork, so one line covers both processes. */
	printf("IOS-UNFAIR-LOCK slide=0x%lx lock_addr=%p\n",
			(unsigned long)_dyld_get_image_vmaddr_slide(0), (void *)&lock);

	os_unfair_lock_lock(&lock);
	pid_t child = fork();
	if (child < 0) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=fork\n");
		return 1;
	}
	if (child == 0) {
		/* Must block here; a trap is the defect, and acquiring it would be a different (also wrong) result. */
		printf("IOS-UNFAIR-LOCK fork-child entered\n");
		os_unfair_lock_lock(&lock);
		printf("IOS-UNFAIR-LOCK fork-child acquired (unexpected under Darwin semantics)\n");
		os_unfair_lock_unlock(&lock);
		_exit(0);
	}

	int status = 0, waited = 0;
	while (waited < 3000 && waitpid(child, &status, WNOHANG) == 0) {
		usleep(100 * 1000);
		waited += 100;
	}
	if (waited >= 3000) {
		kill(child, SIGKILL);
		waitpid(child, &status, 0);
		printf("IOS-UNFAIR-LOCK fork-child blocked=1 waited_ms=%d (Darwin-like)\n", waited);
		os_unfair_lock_unlock(&lock);
		printf("IOS-UNFAIR-LOCK pass=1 reason=fork_child_blocks\n");
		return 0;
	}
	if (WIFSIGNALED(status)) {
		printf("IOS-UNFAIR-LOCK pass=0 reason=fork_child_trapped signal=%d\n", WTERMSIG(status));
		return 1;
	}
	os_unfair_lock_unlock(&lock);
	printf("IOS-UNFAIR-LOCK pass=0 reason=fork_child_exited_prematurely status=%d\n",
			WIFEXITED(status) ? WEXITSTATUS(status) : -1);
	return 1;
}

// Regression test for dar-cps: abort_with_payload() must terminate ONLY the
// calling process, never broadcast SIGABRT to its process group.
//
// macOS abort_with_payload (reached by gnulib/gettext conftests via libsystem's
// os/assumes.c -> abort_with_payload) terminates only the aborting process. Darling
// implemented the sys_abort_with_payload syscall as sys_kill(0, SIGABRT), and Linux
// kill(0, sig) targets the caller's ENTIRE PROCESS GROUP. So a conftest that aborts
// broadcast SIGABRT to its parent/sibling processes sharing the pgid and killed them
// too -- observed taking down the parent brew Ruby with a spurious SI_USER SIGABRT
// during `brew reinstall -s gettext` (dar-cps; same family as dar-gwn.1.5 /
// dar-gwn.6.4, whose sigexc default-effect path was already fixed to tgkill-self).
// Fix: sys_abort_with_payload re-raises to THIS thread via tgkill(pid, tid, SIGABRT).
//
// This is a GUEST test: it must run inside Darling so abort_with_payload() resolves
// to Darling's libsystem sys_abort_with_payload. See run-abort-with-payload-no-group-broadcast.sh.
//
// Method (deterministic RED->GREEN):
//   - parent and child share one process group; parent installs a SIGABRT HANDLER
//     so a broadcast is observable (sets a flag) instead of lethal,
//   - child restores SIG_DFL and calls abort_with_payload() exactly like a conftest,
//   - RED (broken): the kill(0) broadcast reaches the parent -> parent_got_SIGABRT=1,
//   - GREEN (fixed/macOS): abort stays local; parent survives, only the child dies.
// Exit 0 == GREEN (parent survived). Exit 1 == RED (broadcast hit the parent).
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <signal.h>
#include <string.h>
#include <stdint.h>
#include <sys/wait.h>
#include <sys/types.h>

// The real macOS libsystem entry; lives in libSystem. Declared directly so the
// test does not depend on a private <sys/reason.h> being present.
extern void abort_with_payload(uint32_t reason_namespace, uint64_t reason_code,
                               void *payload, uint32_t payload_size,
                               const char *reason_string, uint64_t reason_flags);

#define OS_REASON_LIBSYSTEM 5

static volatile sig_atomic_t parent_got_abrt = 0;
static void on_abrt(int sig) { (void)sig; parent_got_abrt = 1; }

int main(void) {
	setpgid(0, 0);
	pid_t pgrp = getpgrp();

	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_abrt;
	sigemptyset(&sa.sa_mask);
	sa.sa_flags = 0;
	if (sigaction(SIGABRT, &sa, NULL) != 0) { perror("sigaction"); return 2; }

	fflush(stdout);
	pid_t pid = fork();
	if (pid < 0) { perror("fork"); return 2; }

	if (pid == 0) {
		signal(SIGABRT, SIG_DFL);  // child dies of its own SIGABRT, like a conftest
		setpgid(0, pgrp);
		abort_with_payload(OS_REASON_LIBSYSTEM, 0, (void*)0, 0u, "dar-cps regress", 0ULL);
		// If the (fixed) implementation returned without terminating us, self-raise
		// so the child still dies of SIGABRT for the child_died check. This is a
		// thread-local raise() and does NOT broadcast.
		raise(SIGABRT);
		_exit(99);
	}

	int status = 0;
	while (waitpid(pid, &status, 0) < 0) { if (parent_got_abrt) break; }
	for (volatile int i = 0; i < 3000000 && !parent_got_abrt; i++) { }

	int child_abrt = WIFSIGNALED(status) && WTERMSIG(status) == SIGABRT;
	printf("child_died_of_SIGABRT=%d parent_got_SIGABRT=%d pgrp=%d\n",
	       child_abrt, (int)parent_got_abrt, (int)pgrp);

	if (parent_got_abrt) {
		printf("RED: abort_with_payload BROADCAST SIGABRT to the process group (parent hit)\n");
		return 1;
	}
	if (!child_abrt) {
		printf("INCONCLUSIVE: child did not die of SIGABRT (status=0x%x)\n", status);
		return 3;
	}
	printf("GREEN: abort_with_payload stayed local to the child; parent survived\n");
	return 0;
}

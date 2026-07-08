#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define UL_COMPARE_AND_WAIT 1
#define ULF_NO_ERRNO 0x01000000

extern int __ulock_wait(uint32_t operation, void *addr, uint64_t value,
	uint32_t timeout);
extern int __ulock_wake(uint32_t operation, void *addr, uint64_t wake_value);

static volatile uint32_t ulock_word;
static volatile sig_atomic_t saw_signal;
static int ready_pipe[2];

static void
signal_handler(int signo)
{
	(void)signo;
	saw_signal = 1;
}

static void
fail_errno(const char *what)
{
	fprintf(stderr, "%s: %s (%d)\n", what, strerror(errno), errno);
	exit(2);
}

static void *
waiter_thread(void *arg)
{
	(void)arg;

	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = signal_handler;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGUSR1, &sa, NULL) != 0) {
		return (void *)10;
	}

	if (write(ready_pipe[1], "r", 1) != 1) {
		return (void *)11;
	}

	int rc = __ulock_wait(UL_COMPARE_AND_WAIT | ULF_NO_ERRNO,
		(void *)&ulock_word, 0, 0);
	if (!saw_signal) {
		fprintf(stderr, "waiter did not observe SIGUSR1\n");
		return (void *)12;
	}
	if (rc < 0) {
		fprintf(stderr, "__ulock_wait returned %d before wake\n", rc);
		return (void *)13;
	}
	return 0;
}

int
main(void)
{
	if (pipe(ready_pipe) != 0) {
		fail_errno("pipe");
	}

	pthread_t thread;
	int err = pthread_create(&thread, NULL, waiter_thread, NULL);
	if (err != 0) {
		fprintf(stderr, "pthread_create: %s (%d)\n", strerror(err), err);
		return 2;
	}

	char ready = 0;
	if (read(ready_pipe[0], &ready, 1) != 1) {
		fail_errno("read ready");
	}

	usleep(100000);
	err = pthread_kill(thread, SIGUSR1);
	if (err != 0) {
		fprintf(stderr, "pthread_kill: %s (%d)\n", strerror(err), err);
		return 2;
	}

	usleep(100000);
	ulock_word = 1;
	int wake_rc = __ulock_wake(UL_COMPARE_AND_WAIT | ULF_NO_ERRNO,
		(void *)&ulock_word, 0);
	if (wake_rc < 0) {
		fprintf(stderr, "__ulock_wake returned %d\n", wake_rc);
		return 1;
	}

	void *thread_result = NULL;
	err = pthread_join(thread, &thread_result);
	if (err != 0) {
		fprintf(stderr, "pthread_join: %s (%d)\n", strerror(err), err);
		return 2;
	}
	if (thread_result != NULL) {
		fprintf(stderr, "waiter result=%ld\n", (long)thread_result);
		return 1;
	}

	puts("ULOCK_EINTR_RETRY_GUEST_OK");
	return 0;
}

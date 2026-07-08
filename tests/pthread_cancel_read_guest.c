#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static int pipe_fds[2];

static void
interrupt_handler(int signal_number)
{
	(void)signal_number;
}

static void
timeout_handler(int signal_number)
{
	(void)signal_number;
	_exit(124);
}

static void*
blocking_reader(void* arg)
{
	(void)arg;
	char byte;

	if (pthread_setcancelstate(PTHREAD_CANCEL_ENABLE, NULL) != 0)
		return (void*)1;
	if (pthread_setcanceltype(PTHREAD_CANCEL_DEFERRED, NULL) != 0)
		return (void*)2;

	for (;;) {
		ssize_t nread = read(pipe_fds[0], &byte, 1);
		if (nread > 0)
			return (void*)3;
		if (nread == 0)
			return (void*)4;
		if (errno != EINTR)
			return (void*)5;
	}
}

int
main(void)
{
	pthread_t thread;
	void* result = NULL;
	struct sigaction action;

	signal(SIGALRM, timeout_handler);
	sigemptyset(&action.sa_mask);
	action.sa_handler = interrupt_handler;
	action.sa_flags = 0;
	if (sigaction(SIGUSR1, &action, NULL) != 0) {
		perror("sigaction");
		return 2;
	}
	alarm(10);

	if (pipe(pipe_fds) != 0) {
		perror("pipe");
		return 2;
	}
	if (pthread_create(&thread, NULL, blocking_reader, NULL) != 0) {
		perror("pthread_create");
		return 2;
	}

	usleep(200000);
	if (pthread_cancel(thread) != 0) {
		fprintf(stderr, "pthread_cancel failed\n");
		return 1;
	}
	if (pthread_kill(thread, SIGUSR1) != 0) {
		fprintf(stderr, "pthread_kill failed\n");
		return 1;
	}
	if (pthread_join(thread, &result) != 0) {
		perror("pthread_join");
		return 1;
	}
	if (result != PTHREAD_CANCELED) {
		fprintf(stderr, "thread was not canceled: %p\n", result);
		return 1;
	}

	alarm(0);
	printf("PTHREAD_CANCEL_READ_GUEST_OK\n");
	return 0;
}

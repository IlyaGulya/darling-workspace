#include <errno.h>
#include <mach/mach.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static void
die_mach(const char* what, kern_return_t kr)
{
	fprintf(stderr, "%s: %s\n", what, mach_error_string(kr));
	_exit(2);
}

static void*
exception_handler(void* arg)
{
	mach_port_t port = (mach_port_t)(uintptr_t)arg;
	union {
		mach_msg_header_t header;
		unsigned char bytes[4096];
	} message;
	kern_return_t kr;

	kr = mach_msg(&message.header, MACH_RCV_MSG, 0, sizeof(message.bytes),
	    port, MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL);
	if (kr != KERN_SUCCESS)
		die_mach("mach_msg receive", kr);

	/*
	 * This is the regression shape: the handler consumes the fatal
	 * EXC_BAD_ACCESS message but never sends a reply. The fixed server bounds
	 * the faulting thread's synchronous reply wait and falls through to the
	 * default fatal signal path; the old server leaves the child wedged.
	 */
	for (;;)
		sleep(60);
}

static void
child_main(void)
{
	mach_port_t port = MACH_PORT_NULL;
	pthread_t thread;
	kern_return_t kr;

	kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &port);
	if (kr != KERN_SUCCESS)
		die_mach("mach_port_allocate", kr);
	kr = mach_port_insert_right(mach_task_self(), port, port,
	    MACH_MSG_TYPE_MAKE_SEND);
	if (kr != KERN_SUCCESS)
		die_mach("mach_port_insert_right", kr);
	kr = task_set_exception_ports(mach_task_self(), EXC_MASK_BAD_ACCESS, port,
	    EXCEPTION_DEFAULT, THREAD_STATE_NONE);
	if (kr != KERN_SUCCESS)
		die_mach("task_set_exception_ports", kr);

	if (pthread_create(&thread, NULL, exception_handler,
	    (void*)(uintptr_t)port) != 0) {
		perror("pthread_create");
		_exit(2);
	}

	sleep(1);
	*(volatile int*)0 = 1;
	_exit(3);
}

int
main(void)
{
	pid_t child = fork();
	time_t deadline = time(NULL) + 12;
	int status = 0;

	if (child < 0) {
		perror("fork");
		return 2;
	}
	if (child == 0)
		child_main();

	for (;;) {
		pid_t waited = waitpid(child, &status, WNOHANG);
		if (waited == child)
			break;
		if (waited < 0) {
			perror("waitpid");
			return 2;
		}
		if (time(NULL) >= deadline) {
			kill(child, SIGKILL);
			waitpid(child, NULL, 0);
			fprintf(stderr, "child wedged waiting for fatal exception reply\n");
			return 1;
		}
		usleep(100000);
	}

	if (WIFSIGNALED(status)) {
		int signal_number = WTERMSIG(status);
		if (signal_number == SIGSEGV || signal_number == SIGBUS) {
			printf("FATAL_EXCEPTION_UNREPLYING_HANDLER_OK\n");
			return 0;
		}
		fprintf(stderr, "child died from unexpected signal %d\n", signal_number);
		return 1;
	}
	if (WIFEXITED(status)) {
		fprintf(stderr, "child exited unexpectedly with status %d\n",
		    WEXITSTATUS(status));
		return 1;
	}
	fprintf(stderr, "child ended in unexpected wait status 0x%x\n", status);
	return 1;
}

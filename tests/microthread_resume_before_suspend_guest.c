#include <mach/mach.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int target_returned = 0;
static kern_return_t target_suspend_kr = KERN_FAILURE;

static const char *fallback_fault_path =
    "/private/var/tmp/dserver-microthread-resume-before-suspend-fault";

static void
timeout_handler(int signo)
{
	(void)signo;
	_exit(124);
}

static FILE *
open_fault_file(void)
{
	const char *env_path = getenv("DSERVER_TEST_FAULT_FILE");
	if (env_path && env_path[0] == '/') {
		FILE *file = fopen(env_path, "w");
		if (file) {
			return file;
		}

		char system_root_path[4096];
		snprintf(system_root_path, sizeof(system_root_path),
		    "/Volumes/SystemRoot%s", env_path);
		file = fopen(system_root_path, "w");
		if (file) {
			return file;
		}
	}

	return fopen(fallback_fault_path, "w");
}

static void
write_fault(void)
{
	FILE *file = open_fault_file();
	if (!file) {
		perror("fopen fault");
		exit(20);
	}
	fputs("microthread.resume_before_suspend\n", file);
	fclose(file);
}

static void *
target_main(void *arg)
{
	(void)arg;

	write_fault();
	mach_port_t target_thread = pthread_mach_thread_np(pthread_self());
	target_suspend_kr = thread_suspend(target_thread);

	target_returned = 1;

	return NULL;
}

int
main(void)
{
	signal(SIGALRM, timeout_handler);
	alarm(10);

	pthread_t target;
	int err = pthread_create(&target, NULL, target_main, NULL);
	if (err != 0) {
		fprintf(stderr, "pthread_create target: %s\n", strerror(err));
		return 2;
	}

	err = pthread_join(target, NULL);
	if (err != 0) {
		fprintf(stderr, "pthread_join target: %s\n", strerror(err));
		return 3;
	}

	alarm(0);

	if (!target_returned) {
		fprintf(stderr, "target did not return from thread_suspend\n");
		return 4;
	}
	if (target_suspend_kr != KERN_SUCCESS) {
		fprintf(stderr, "thread_suspend failed: %d\n", target_suspend_kr);
		return 5;
	}

	puts("MICROTHREAD_RESUME_BEFORE_SUSPEND_OK");
	return 0;
}

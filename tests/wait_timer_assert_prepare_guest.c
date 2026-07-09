#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static const char *fallback_fault_path = "/private/var/tmp/dserver-wait-prepare-fault";

static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond = PTHREAD_COND_INITIALIZER;
static int ready;
static int release_wait;

static void
timeout_handler(int signal_number)
{
	(void)signal_number;
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
		_exit(20);
	}
	fputs("waitq.assert_wait_stale_timer\n", file);
	fclose(file);
}

static void *
waiter_main(void *arg)
{
	(void)arg;
	int rc;

	rc = pthread_mutex_lock(&mutex);
	if (rc != 0) {
		return (void *)(long)rc;
	}

	ready = 1;
	pthread_cond_signal(&cond);
	while (!release_wait) {
		rc = pthread_cond_wait(&cond, &mutex);
		if (rc != 0) {
			pthread_mutex_unlock(&mutex);
			return (void *)(long)rc;
		}
	}

	pthread_mutex_unlock(&mutex);
	return NULL;
}

int
main(void)
{
	pthread_t waiter;
	void *result = NULL;
	int rc;

	signal(SIGALRM, timeout_handler);
	alarm(10);
	write_fault();

	rc = pthread_create(&waiter, NULL, waiter_main, NULL);
	if (rc != 0) {
		fprintf(stderr, "pthread_create failed: %d\n", rc);
		return 2;
	}

	rc = pthread_mutex_lock(&mutex);
	if (rc != 0) {
		fprintf(stderr, "pthread_mutex_lock failed: %d\n", rc);
		return 2;
	}
	while (!ready) {
		rc = pthread_cond_wait(&cond, &mutex);
		if (rc != 0) {
			fprintf(stderr, "pthread_cond_wait for ready failed: %d\n", rc);
			return 2;
		}
	}
	release_wait = 1;
	pthread_cond_signal(&cond);
	pthread_mutex_unlock(&mutex);

	rc = pthread_join(waiter, &result);
	if (rc != 0) {
		fprintf(stderr, "pthread_join failed: %d\n", rc);
		return 2;
	}
	if (result != NULL) {
		fprintf(stderr, "wait returned error: %ld\n", (long)result);
		return 1;
	}

	alarm(0);
	unlink(fallback_fault_path);
	puts("WAIT_TIMER_ASSERT_PREPARE_GUEST_OK");
	return 0;
}

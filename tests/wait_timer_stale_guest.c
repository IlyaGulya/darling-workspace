#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

enum waiter_result {
	WAITER_OK = 0,
	WAITER_MUTEX_LOCK_FAILED = 10,
	WAITER_TIMED_WAIT_FAILED = 100,
	WAITER_UNTIMED_WAIT_FAILED = 200,
};

static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond = PTHREAD_COND_INITIALIZER;
static int ready;
static int woke_from_timed_wait;
static int release_untimed_wait;

static void
timeout_handler(int signal_number)
{
	(void)signal_number;
	_exit(124);
}

static struct timespec
deadline_after_ms(long milliseconds)
{
	struct timespec deadline;

	if (clock_gettime(CLOCK_REALTIME, &deadline) != 0) {
		perror("clock_gettime");
		_exit(2);
	}

	deadline.tv_sec += milliseconds / 1000;
	deadline.tv_nsec += (milliseconds % 1000) * 1000000L;
	if (deadline.tv_nsec >= 1000000000L) {
		deadline.tv_sec++;
		deadline.tv_nsec -= 1000000000L;
	}

	return deadline;
}

static void*
waiter_main(void* arg)
{
	(void)arg;
	struct timespec deadline = deadline_after_ms(250);
	int rc;

	if (pthread_mutex_lock(&mutex) != 0)
		return (void*)(intptr_t)WAITER_MUTEX_LOCK_FAILED;

	ready = 1;
	pthread_cond_signal(&cond);

	rc = pthread_cond_timedwait(&cond, &mutex, &deadline);
	if (rc != 0) {
		pthread_mutex_unlock(&mutex);
		return (void*)(intptr_t)(WAITER_TIMED_WAIT_FAILED + rc);
	}

	woke_from_timed_wait = 1;
	pthread_cond_signal(&cond);

	while (!release_untimed_wait) {
		rc = pthread_cond_wait(&cond, &mutex);
		if (rc != 0) {
			pthread_mutex_unlock(&mutex);
			return (void*)(intptr_t)(WAITER_UNTIMED_WAIT_FAILED + rc);
		}
	}

	pthread_mutex_unlock(&mutex);
	return NULL;
}

int
main(void)
{
	pthread_t waiter;
	void* result = NULL;
	intptr_t waiter_result;
	int rc;

	signal(SIGALRM, timeout_handler);
	alarm(10);

	rc = pthread_create(&waiter, NULL, waiter_main, NULL);
	if (rc != 0) {
		fprintf(stderr, "pthread_create failed: %d\n", rc);
		return 2;
	}

	if (pthread_mutex_lock(&mutex) != 0) {
		fprintf(stderr, "pthread_mutex_lock failed\n");
		return 2;
	}
	while (!ready) {
		rc = pthread_cond_wait(&cond, &mutex);
		if (rc != 0) {
			fprintf(stderr, "pthread_cond_wait for ready failed: %d\n", rc);
			return 2;
		}
	}
	pthread_mutex_unlock(&mutex);

	usleep(50000);

	if (pthread_mutex_lock(&mutex) != 0) {
		fprintf(stderr, "pthread_mutex_lock failed\n");
		return 2;
	}
	pthread_cond_signal(&cond);
	while (!woke_from_timed_wait) {
		rc = pthread_cond_wait(&cond, &mutex);
		if (rc != 0) {
			fprintf(stderr, "pthread_cond_wait for timed wake failed: %d\n", rc);
			return 2;
		}
	}
	pthread_mutex_unlock(&mutex);

	usleep(400000);

	if (pthread_mutex_lock(&mutex) != 0) {
		fprintf(stderr, "pthread_mutex_lock failed\n");
		return 2;
	}
	release_untimed_wait = 1;
	pthread_cond_signal(&cond);
	pthread_mutex_unlock(&mutex);

	rc = pthread_join(waiter, &result);
	if (rc != 0) {
		fprintf(stderr, "pthread_join failed: %d\n", rc);
		return 2;
	}
	waiter_result = (intptr_t)result;
	if (waiter_result != WAITER_OK) {
		if (waiter_result >= WAITER_UNTIMED_WAIT_FAILED) {
			fprintf(stderr,
			    "untimed wait observed stale timeout/error: %ld\n",
			    (long)(waiter_result - WAITER_UNTIMED_WAIT_FAILED));
		} else if (waiter_result >= WAITER_TIMED_WAIT_FAILED) {
			fprintf(stderr,
			    "timed wait failed before early wake: %ld\n",
			    (long)(waiter_result - WAITER_TIMED_WAIT_FAILED));
		} else {
			fprintf(stderr, "waiter setup failed: %ld\n", (long)waiter_result);
		}
		return 1;
	}

	alarm(0);
	printf("WAIT_TIMER_STALE_GUEST_OK\n");
	return 0;
}

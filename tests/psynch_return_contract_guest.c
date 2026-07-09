#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static volatile int timed_out;
static volatile int stop_storm;

static void
fail_errno(const char *what, int err)
{
	fprintf(stderr, "%s: %s (%d)\n", what, strerror(err), err);
	_exit(2);
}

static void
alarm_handler(int signo)
{
	(void)signo;
	timed_out = 1;
}

static void
signal_handler(int signo)
{
	(void)signo;
}

static void
deadline_after_us(struct timespec *ts, long usec)
{
	struct timeval tv;
	gettimeofday(&tv, NULL);
	tv.tv_usec += usec;
	tv.tv_sec += tv.tv_usec / 1000000;
	tv.tv_usec %= 1000000;
	ts->tv_sec = tv.tv_sec;
	ts->tv_nsec = tv.tv_usec * 1000;
}

static void *
storm_thread(void *arg)
{
	pthread_t target = *(pthread_t *)arg;
	while (!stop_storm) {
		pthread_kill(target, SIGUSR1);
		usleep(2000);
	}
	return NULL;
}

int
main(void)
{
	pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
	pthread_mutex_t mtx = PTHREAD_MUTEX_INITIALIZER;
	pthread_t self = pthread_self();
	pthread_t storm;
	struct sigaction sa;
	int timeouts = 0;
	int rc;

	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = signal_handler;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGUSR1, &sa, NULL) != 0) {
		fail_errno("sigaction SIGUSR1", errno);
	}
	signal(SIGALRM, alarm_handler);
	alarm(10);

	stop_storm = 0;
	rc = pthread_create(&storm, NULL, storm_thread, &self);
	if (rc) {
		fail_errno("pthread_create storm", rc);
	}

	rc = pthread_mutex_lock(&mtx);
	if (rc) {
		fail_errno("pthread_mutex_lock", rc);
	}
	while (timeouts < 25) {
		struct timespec ts;

		deadline_after_us(&ts, 1000);
		rc = pthread_cond_timedwait(&cv, &mtx, &ts);
		if (rc == ETIMEDOUT) {
			timeouts++;
		} else if (rc == EINTR) {
			fail_errno("pthread_cond_timedwait leaked EINTR", rc);
		} else if (rc != 0) {
			fail_errno("pthread_cond_timedwait unexpected", rc);
		}
		if (timed_out) {
			fail_errno("pthread_cond_timedwait alarm", ETIMEDOUT);
		}
	}
	rc = pthread_mutex_unlock(&mtx);
	if (rc) {
		fail_errno("pthread_mutex_unlock", rc);
	}

	stop_storm = 1;
	rc = pthread_join(storm, NULL);
	if (rc) {
		fail_errno("pthread_join storm", rc);
	}

	printf("PSYNCH_RETURN_CONTRACT_GUEST_OK timeouts=%d\n", timeouts);
	return 0;
}

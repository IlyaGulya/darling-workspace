#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static volatile int stop_storm;
static volatile int timed_out;

static void
phase(const char *name)
{
	printf("PHASE %s\n", name);
	fflush(stdout);
}

static int
env_int(const char *name, int fallback)
{
	const char *value = getenv(name);
	if (!value || !*value)
		return fallback;
	char *end = NULL;
	long parsed = strtol(value, &end, 10);
	if (!end || *end != '\0' || parsed <= 0 || parsed > 1000000)
		return fallback;
	return (int)parsed;
}

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

static void
test_cond_timedwait_eintr_contract(void)
{
	phase("cond_timedwait_eintr:start");
	pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
	pthread_mutex_t mtx = PTHREAD_MUTEX_INITIALIZER;
	pthread_t self = pthread_self();
	pthread_t storm;
	int timeouts = 0;
	int target_timeouts = env_int("PSYNCH_COND_TIMEOUTS", 10);
	int rc;

	stop_storm = 0;
	rc = pthread_create(&storm, NULL, storm_thread, &self);
	if (rc) fail_errno("pthread_create storm", rc);

	rc = pthread_mutex_lock(&mtx);
	if (rc) fail_errno("pthread_mutex_lock timed", rc);
	while (timeouts < target_timeouts) {
		struct timespec ts;
		deadline_after_us(&ts, 1000);
		rc = pthread_cond_timedwait(&cv, &mtx, &ts);
		if (rc == ETIMEDOUT) {
			timeouts++;
		} else if (rc == 0) {
			continue;
		} else if (rc == EINTR) {
			fail_errno("pthread_cond_timedwait returned EINTR", rc);
		} else {
			fail_errno("pthread_cond_timedwait unexpected", rc);
		}
		if (timed_out) fail_errno("cond timedwait alarm", ETIMEDOUT);
	}
	rc = pthread_mutex_unlock(&mtx);
	if (rc) fail_errno("pthread_mutex_unlock timed", rc);

	stop_storm = 1;
	rc = pthread_join(storm, NULL);
	if (rc) fail_errno("pthread_join storm", rc);
	phase("cond_timedwait_eintr:done");
}

struct cv_handoff {
	pthread_cond_t cv;
	pthread_mutex_t mtx;
	int work;
	int done;
	int target;
};

static void *
consumer_thread(void *arg)
{
	struct cv_handoff *ctx = arg;
	for (;;) {
		int rc = pthread_mutex_lock(&ctx->mtx);
		if (rc) fail_errno("consumer mutex lock", rc);
		while (ctx->work == 0 && ctx->done < ctx->target) {
			rc = pthread_cond_wait(&ctx->cv, &ctx->mtx);
			if (rc) fail_errno("pthread_cond_wait", rc);
		}
		if (ctx->done >= ctx->target) {
			rc = pthread_mutex_unlock(&ctx->mtx);
			if (rc) fail_errno("consumer mutex unlock exit", rc);
			return NULL;
		}
		ctx->work--;
		ctx->done++;
		rc = pthread_mutex_unlock(&ctx->mtx);
		if (rc) fail_errno("consumer mutex unlock", rc);
	}
}

static void
test_cond_handoff_under_signals(void)
{
	phase("cond_handoff:start");
	struct cv_handoff ctx = {
		.cv = PTHREAD_COND_INITIALIZER,
		.mtx = PTHREAD_MUTEX_INITIALIZER,
		.target = env_int("PSYNCH_COND_HANDOFF_TARGET", 1000),
	};
	pthread_t consumers[4];
	pthread_t storm;
	pthread_t main_thread = pthread_self();
	int rc;

	stop_storm = 0;
	rc = pthread_create(&storm, NULL, storm_thread, &main_thread);
	if (rc) fail_errno("pthread_create handoff storm", rc);

	for (size_t i = 0; i < sizeof(consumers) / sizeof(consumers[0]); i++) {
		rc = pthread_create(&consumers[i], NULL, consumer_thread, &ctx);
		if (rc) fail_errno("pthread_create consumer", rc);
	}

	for (int i = 0; i < ctx.target; i++) {
		rc = pthread_mutex_lock(&ctx.mtx);
		if (rc) fail_errno("producer mutex lock", rc);
		ctx.work++;
		rc = pthread_cond_signal(&ctx.cv);
		if (rc) fail_errno("pthread_cond_signal", rc);
		rc = pthread_mutex_unlock(&ctx.mtx);
		if (rc) fail_errno("producer mutex unlock", rc);
		if ((i & 0x3f) == 0) sched_yield();
		if (timed_out) fail_errno("cond handoff alarm", ETIMEDOUT);
	}

	rc = pthread_mutex_lock(&ctx.mtx);
	if (rc) fail_errno("producer final lock", rc);
	while (ctx.done < ctx.target) {
		rc = pthread_mutex_unlock(&ctx.mtx);
		if (rc) fail_errno("producer poll unlock", rc);
		usleep(1000);
		rc = pthread_mutex_lock(&ctx.mtx);
		if (rc) fail_errno("producer poll lock", rc);
		if (timed_out) fail_errno("cond handoff drain alarm", ETIMEDOUT);
	}
	rc = pthread_cond_broadcast(&ctx.cv);
	if (rc) fail_errno("pthread_cond_broadcast", rc);
	rc = pthread_mutex_unlock(&ctx.mtx);
	if (rc) fail_errno("producer final unlock", rc);

	for (size_t i = 0; i < sizeof(consumers) / sizeof(consumers[0]); i++) {
		rc = pthread_join(consumers[i], NULL);
		if (rc) fail_errno("pthread_join consumer", rc);
	}

	stop_storm = 1;
	rc = pthread_join(storm, NULL);
	if (rc) fail_errno("pthread_join handoff storm", rc);
	phase("cond_handoff:done");
}

struct lock_stress {
	pthread_mutex_t mtx;
	pthread_rwlock_t rwlock;
	int iters;
	int value;
};

static void *
mutex_worker(void *arg)
{
	struct lock_stress *ctx = arg;
	for (int i = 0; i < ctx->iters; i++) {
		int rc = pthread_mutex_lock(&ctx->mtx);
		if (rc) fail_errno("stress mutex lock", rc);
		ctx->value++;
		rc = pthread_mutex_unlock(&ctx->mtx);
		if (rc) fail_errno("stress mutex unlock", rc);
	}
	return NULL;
}

static void *
rwlock_worker(void *arg)
{
	struct lock_stress *ctx = arg;
	for (int i = 0; i < ctx->iters; i++) {
		int rc;
		if (i & 1) {
			rc = pthread_rwlock_wrlock(&ctx->rwlock);
			if (rc) fail_errno("rwlock wrlock", rc);
			ctx->value++;
		} else {
			rc = pthread_rwlock_rdlock(&ctx->rwlock);
			if (rc) fail_errno("rwlock rdlock", rc);
			(void)ctx->value;
		}
		rc = pthread_rwlock_unlock(&ctx->rwlock);
		if (rc) fail_errno("rwlock unlock", rc);
	}
	return NULL;
}

static void
run_workers(void *(*fn)(void *), struct lock_stress *ctx)
{
	pthread_t threads[8];
	int rc;
	for (size_t i = 0; i < sizeof(threads) / sizeof(threads[0]); i++) {
		rc = pthread_create(&threads[i], NULL, fn, ctx);
		if (rc) fail_errno("pthread_create worker", rc);
	}
	for (size_t i = 0; i < sizeof(threads) / sizeof(threads[0]); i++) {
		rc = pthread_join(threads[i], NULL);
		if (rc) fail_errno("pthread_join worker", rc);
	}
}

int
main(void)
{
	phase("setup:start");
	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = signal_handler;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGUSR1, &sa, NULL) != 0) {
		fail_errno("sigaction SIGUSR1", errno);
	}
	signal(SIGALRM, alarm_handler);
	alarm(30);
	phase("setup:done");

	test_cond_timedwait_eintr_contract();
	test_cond_handoff_under_signals();

	struct lock_stress ctx = {
		.mtx = PTHREAD_MUTEX_INITIALIZER,
		.rwlock = PTHREAD_RWLOCK_INITIALIZER,
		.iters = env_int("PSYNCH_LOCK_STRESS_ITERS", 1000),
	};
	phase("mutex_stress:start");
	run_workers(mutex_worker, &ctx);
	phase("mutex_stress:done");
	phase("rwlock_stress:start");
	run_workers(rwlock_worker, &ctx);
	phase("rwlock_stress:done");

	printf("PSYNCH_KERNEL_RETURN_CONTRACT_OK value=%d\n", ctx.value);
	return 0;
}

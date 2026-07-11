#include <errno.h>
#include <mach/mach.h>

#include <pthread.h>
#include <stdint.h>
#include <stdio.h>

extern int __pthread_canceled(int action);
extern int __pthread_markcancel(mach_port_t thread_port);

static int expect_result(const char* operation, int actual, int expected) {
	if (actual == expected) {
		return 0;
	}
	fprintf(stderr, "%s returned %d, expected %d\n", operation, actual, expected);
	return 1;
}

static int expect_errno(const char* operation, int actual, int expected_errno) {
	if (actual == -1 && errno == expected_errno) {
		return 0;
	}
	fprintf(
		stderr,
		"%s returned %d with errno %d, expected -1 with errno %d\n",
		operation,
		actual,
		errno,
		expected_errno
	);
	return 1;
}

static void* exercise_cancellation_state(void* context) {
	(void)context;
	mach_port_t self = mach_thread_self();
	int status = 1;

	if (expect_result("__pthread_canceled(disable)", __pthread_canceled(2), 0) != 0) {
		goto out;
	}
	if (expect_result("__pthread_markcancel(self)", __pthread_markcancel(self), 0) != 0) {
		goto out;
	}
	errno = 0;
	if (expect_errno("__pthread_canceled(disabled)", __pthread_canceled(0), EINVAL) != 0) {
		goto out;
	}
	if (expect_result("__pthread_canceled(enable)", __pthread_canceled(1), 0) != 0) {
		goto out;
	}
	if (expect_result("__pthread_canceled(consume)", __pthread_canceled(0), 0) != 0) {
		goto out;
	}

	status = 0;

	out:
	(void)mach_port_deallocate(mach_task_self(), self);
	return (void*)(intptr_t)status;
}

int main(void) {
	pthread_t worker;
	void* worker_result = NULL;

	if (pthread_create(&worker, NULL, exercise_cancellation_state, NULL) != 0) {
		perror("pthread_create");
		return 1;
	}
	if (pthread_join(worker, &worker_result) != 0) {
		perror("pthread_join");
		return 1;
	}
	if ((intptr_t)worker_result != 0) {
		return 1;
	}

	puts("PTHREAD_CANCELLATION_TRACE_GUEST_OK");
	return 0;
}

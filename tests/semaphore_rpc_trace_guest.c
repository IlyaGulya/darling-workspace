#include <mach/mach.h>
#include <mach/semaphore.h>

#include <signal.h>
#include <stdio.h>
#include <unistd.h>

static void timeout_handler(int signal_number) {
	(void)signal_number;
	_exit(124);
}

static int expect_result(const char* operation, kern_return_t actual, kern_return_t expected) {
	if (actual == expected) {
		return 0;
	}
	fprintf(stderr, "%s returned %d, expected %d\n", operation, actual, expected);
	return 1;
}

int main(void) {
	semaphore_t first = MACH_PORT_NULL;
	semaphore_t second = MACH_PORT_NULL;
	semaphore_t signal_target = MACH_PORT_NULL;
	mach_timespec_t timeout = {
		.tv_sec = 0,
		.tv_nsec = 1000000,
	};
	kern_return_t result;
	int status = 1;

	signal(SIGALRM, timeout_handler);
	alarm(10);

	result = semaphore_create(mach_task_self(), &first, SYNC_POLICY_FIFO, 0);
	if (expect_result("semaphore_create(first)", result, KERN_SUCCESS) != 0) {
		goto out;
	}
	result = semaphore_timedwait(first, timeout);
	if (expect_result("semaphore_timedwait(timeout)", result, KERN_OPERATION_TIMED_OUT) != 0) {
		goto out;
	}
	result = semaphore_signal(first);
	if (expect_result("semaphore_signal", result, KERN_SUCCESS) != 0) {
		goto out;
	}
	result = semaphore_timedwait(first, timeout);
	if (expect_result("semaphore_timedwait(success)", result, KERN_SUCCESS) != 0) {
		goto out;
	}

	result = semaphore_create(mach_task_self(), &second, SYNC_POLICY_FIFO, 0);
	if (expect_result("semaphore_create(second)", result, KERN_SUCCESS) != 0) {
		goto out;
	}
	result = semaphore_create(mach_task_self(), &signal_target, SYNC_POLICY_FIFO, 0);
	if (expect_result("semaphore_create(signal_target)", result, KERN_SUCCESS) != 0) {
		goto out;
	}
	result = semaphore_timedwait_signal(second, signal_target, timeout);
	if (expect_result("semaphore_timedwait_signal(timeout)", result, KERN_OPERATION_TIMED_OUT) != 0) {
		goto out;
	}
	result = semaphore_timedwait(signal_target, timeout);
	if (expect_result("semaphore_timedwait(signal_target)", result, KERN_SUCCESS) != 0) {
		goto out;
	}

	puts("SEMAPHORE_RPC_TRACE_GUEST_OK");
	status = 0;

out:
	if (signal_target != MACH_PORT_NULL) {
		(void)semaphore_destroy(mach_task_self(), signal_target);
	}
	if (second != MACH_PORT_NULL) {
		(void)semaphore_destroy(mach_task_self(), second);
	}
	if (first != MACH_PORT_NULL) {
		(void)semaphore_destroy(mach_task_self(), first);
	}
	alarm(0);
	return status;
}

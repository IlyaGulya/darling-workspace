#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

struct thread {
	int wait_timer;
	unsigned short wait_timer_active;
	bool wait_timer_is_set;
};
typedef struct thread* thread_t;

static bool cancelResult;
static int cancelCalls;

bool timer_call_cancel(int* timer);

#include <darlingserver/duct-tape/wait-timer.h>

bool
timer_call_cancel(int* timer)
{
	(void)timer;
	cancelCalls++;
	return cancelResult;
}

static void
check(bool condition, const char* message)
{
	if (!condition) {
		fprintf(stderr, "%s\n", message);
		exit(1);
	}
}

int
main(void)
{
	struct thread thread = {
		.wait_timer = 7,
		.wait_timer_active = 1,
		.wait_timer_is_set = true,
	};

	cancelResult = true;
	cancelCalls = 0;
	check(dtape_thread_prepare_for_wait(&thread), "prepare_for_wait did not cancel a stale timer");
	check(cancelCalls == 1, "prepare_for_wait did not call timer_call_cancel");
	check(!thread.wait_timer_is_set, "prepare_for_wait left stale timer marked armed");
	check(thread.wait_timer_active == 0, "prepare_for_wait did not decrement cancelled timer");

	thread.wait_timer_active = 2;
	thread.wait_timer_is_set = true;
	cancelResult = false;
	cancelCalls = 0;
	check(dtape_thread_prepare_for_wait(&thread), "prepare_for_wait did not handle in-flight timer");
	check(cancelCalls == 1, "prepare_for_wait did not probe in-flight timer");
	check(!thread.wait_timer_is_set, "prepare_for_wait left in-flight timer marked armed");
	check(thread.wait_timer_active == 2, "prepare_for_wait decremented an in-flight timer");
}

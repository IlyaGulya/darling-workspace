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
		.wait_timer = 42,
		.wait_timer_active = 3,
		.wait_timer_is_set = false,
	};

	cancelCalls = 0;
	cancelResult = true;
	check(!dtape_thread_cancel_wait_timer(&thread), "unarmed wait timer reported cancellation");
	check(cancelCalls == 0, "unarmed wait timer called timer_call_cancel");
	check(thread.wait_timer_active == 3, "unarmed wait timer changed active count");

	thread.wait_timer_is_set = true;
	cancelCalls = 0;
	cancelResult = true;
	check(dtape_thread_cancel_wait_timer(&thread), "armed wait timer was not cancelled");
	check(cancelCalls == 1, "armed wait timer did not call timer_call_cancel");
	check(!thread.wait_timer_is_set, "cancelled wait timer stayed marked armed");
	check(thread.wait_timer_active == 2, "cancelled wait timer did not decrement active count");

	thread.wait_timer_is_set = true;
	cancelCalls = 0;
	cancelResult = false;
	check(dtape_thread_cancel_wait_timer(&thread), "in-flight wait timer was not handled");
	check(cancelCalls == 1, "in-flight wait timer did not call timer_call_cancel");
	check(!thread.wait_timer_is_set, "in-flight wait timer stayed marked armed");
	check(thread.wait_timer_active == 2, "in-flight wait timer incorrectly changed active count");
}

#include <cstdlib>
#include <iostream>

struct dtape_semaphore {};
using dtape_semaphore_t = dtape_semaphore;

typedef enum dtape_semaphore_wait_result {
	dtape_semaphore_wait_result_error = -1,
	dtape_semaphore_wait_result_ok = 0,
	dtape_semaphore_wait_result_interrupted = 1,
	dtape_semaphore_wait_result_timed_out = 2,
} dtape_semaphore_wait_result_t;

static dtape_semaphore_wait_result nextResult = dtape_semaphore_wait_result_error;
static unsigned int observedTimeout = 0;

extern "C" dtape_semaphore_wait_result_t dtape_semaphore_down_timeout(dtape_semaphore_t*, unsigned int seconds);

#include <darlingserver/fork-checkin.hpp>

extern "C" dtape_semaphore_wait_result_t
dtape_semaphore_down_timeout(dtape_semaphore_t*, unsigned int seconds)
{
	observedTimeout = seconds;
	return nextResult;
}

static void
check(bool condition, const char* message)
{
	if (!condition) {
		std::cerr << message << std::endl;
		std::exit(1);
	}
}

static void
expect(dtape_semaphore_wait_result_t raw, DarlingServer::ForkCheckinWaitResult expected, unsigned int timeout)
{
	dtape_semaphore semaphore;
	observedTimeout = 0;
	nextResult = raw;

	auto result = DarlingServer::waitForForkCheckin(&semaphore, timeout);
	check(result == expected, "unexpected fork checkin wait result");
	check(observedTimeout == timeout, "fork checkin wait used the wrong timeout");
}

int
main()
{
	expect(dtape_semaphore_wait_result_ok, DarlingServer::ForkCheckinWaitResult::Observed, 1);
	expect(dtape_semaphore_wait_result_interrupted, DarlingServer::ForkCheckinWaitResult::Interrupted, 2);
	expect(dtape_semaphore_wait_result_timed_out, DarlingServer::ForkCheckinWaitResult::TimedOut, 3);
	expect(dtape_semaphore_wait_result_error, DarlingServer::ForkCheckinWaitResult::Error, 4);

	dtape_semaphore semaphore;
	nextResult = dtape_semaphore_wait_result_timed_out;
	(void)DarlingServer::waitForForkCheckin(&semaphore);
	check(observedTimeout == DarlingServer::ForkCheckinWaitTimeoutSeconds, "default fork checkin timeout changed");
	check(observedTimeout == 30, "fork checkin timeout is not the intended defensive bound");
}

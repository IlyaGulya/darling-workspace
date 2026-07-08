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

static dtape_semaphore_wait_result_t nextResult = dtape_semaphore_wait_result_error;
static unsigned int observedTimeout = 0;

extern "C" dtape_semaphore_wait_result_t dtape_semaphore_down_timeout(dtape_semaphore_t*, unsigned int seconds);

#include <darlingserver/fork-checkin.hpp>

static DarlingServer::ForkCheckinState* stateToMarkDuringWait = nullptr;

extern "C" dtape_semaphore_wait_result_t
dtape_semaphore_down_timeout(dtape_semaphore_t*, unsigned int seconds)
{
	observedTimeout = seconds;
	if (seconds != 0 && stateToMarkDuringWait != nullptr) {
		stateToMarkDuringWait->markChildCheckedIn();
		stateToMarkDuringWait = nullptr;
	}
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

int
main()
{
	dtape_semaphore semaphore;
	DarlingServer::ForkCheckinState state;

	state.markChildCheckedIn();
	nextResult = dtape_semaphore_wait_result_error;
	check(state.wait(&semaphore) == DarlingServer::ForkCheckinWaitResult::Observed,
	    "sticky checkin was not observed before waiting");
	check(observedTimeout == 0, "sticky fast path did not drain the semaphore non-blockingly");

	nextResult = dtape_semaphore_wait_result_timed_out;
	check(state.wait(&semaphore, 7) == DarlingServer::ForkCheckinWaitResult::TimedOut,
	    "sticky flag leaked into the next fork wait");
	check(observedTimeout == 7, "non-sticky wait did not use the requested timeout");

	nextResult = dtape_semaphore_wait_result_interrupted;
	stateToMarkDuringWait = &state;
	check(state.wait(&semaphore, 8) == DarlingServer::ForkCheckinWaitResult::Observed,
	    "sticky checkin during an interrupted wait was not observed");
	check(observedTimeout == 8, "interrupted wait did not use the requested timeout");

	nextResult = dtape_semaphore_wait_result_timed_out;
	stateToMarkDuringWait = &state;
	check(state.wait(&semaphore, 9) == DarlingServer::ForkCheckinWaitResult::Observed,
	    "sticky checkin at timeout was not observed");
	check(observedTimeout == 9, "timeout wait did not use the requested timeout");

	state.reset();
	nextResult = dtape_semaphore_wait_result_interrupted;
	check(state.wait(&semaphore, 10) == DarlingServer::ForkCheckinWaitResult::Interrupted,
	    "reset sticky state hid a real interrupt");
}

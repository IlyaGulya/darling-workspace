#include <cstdlib>
#include <iostream>

#include <darlingserver/microthread-resume.hpp>

static void check(bool condition, const char* message) {
	if (!condition) {
		std::cerr << message << '\n';
		std::exit(1);
	}
}

// The previous dispatcher rejected overlapping execution without handing the
// request back to the worker that was still completing the previous reply.
class OldExecution {
	bool running = false;
public:
	void begin() { running = true; }
	bool deferDispatch() { return running; }
	bool finish() { running = false; return false; }
};

template<class Execution>
static unsigned serveOverlappingCalls() {
	Execution execution;
	execution.begin();
	unsigned replies = 1;
	check(execution.deferDispatch(), "overlapping dispatch acquired an occupied thread");
	check(execution.deferDispatch(), "repeated dispatch acquired an occupied thread");
	if (execution.finish()) {
		execution.begin();
		++replies;
		check(!execution.finish(), "coalesced dispatch was delivered more than once");
	}
	check(!execution.deferDispatch(), "released execution still defers fresh work");
	return replies;
}

int main() {
	check(serveOverlappingCalls<OldExecution>() == 1,
		"old model did not reproduce the unanswered next call");
	std::cout << "MICROTHREAD_OLD_DISPATCH_LOST_CALL\n";
	check(serveOverlappingCalls<DarlingServer::MicrothreadExecution>() == 2,
		"next call was not handed off after execution stopped");
	std::cout << "MICROTHREAD_DISPATCH_HANDOFF_OK\n";
}

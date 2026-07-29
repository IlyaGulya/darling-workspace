// Deterministic behavior fixture for the production standard-signal
// coalescing helper. ThreadWakeTestAdapter is the private friend seam declared
// by Thread; this test does not copy the helper algorithm.
#include <darlingserver/thread.hpp>

#include <csignal>
#include <cstdint>
#include <cstdio>

namespace DarlingServer {
struct ThreadWakeTestAdapter {
	static bool markCoalescedStandardSignal(uint64_t& pendingMask, int signal) {
		return Thread::_markCoalescedStandardSignalPendingLocked(pendingMask, signal);
	}
};
}

int main() {
	uint64_t pendingMask = 0;
	const uint64_t bit = 1ull << SIGUSR1;

	if (!DarlingServer::ThreadWakeTestAdapter::markCoalescedStandardSignal(
			pendingMask, SIGUSR1)) {
		std::fputs("first SIGUSR1 delivery was incorrectly suppressed\n", stderr);
		return 1;
	}
	if (pendingMask != bit) {
		std::fputs("first SIGUSR1 delivery did not set its pending bit\n", stderr);
		return 1;
	}
	if (DarlingServer::ThreadWakeTestAdapter::markCoalescedStandardSignal(
			pendingMask, SIGUSR1)) {
		std::fputs("duplicate pending SIGUSR1 was not suppressed\n", stderr);
		return 1;
	}
	if (pendingMask != bit) {
		std::fputs("duplicate SIGUSR1 changed the pending mask\n", stderr);
		return 1;
	}

	pendingMask &= ~bit;
	if (!DarlingServer::ThreadWakeTestAdapter::markCoalescedStandardSignal(
			pendingMask, SIGUSR1) || pendingMask != bit) {
		std::fputs("SIGUSR1 did not re-arm after completion clear\n", stderr);
		return 1;
	}

	std::puts("STANDARD_SIGNAL_COALESCING_HELPER_OK");
	return 0;
}

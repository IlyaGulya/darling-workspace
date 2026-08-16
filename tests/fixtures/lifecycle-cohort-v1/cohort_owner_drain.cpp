#include <cassert>
#include <cstddef>
#include <limits>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <darlingserver/lifecycle-cohort-owner.hpp>

struct darling_lifecycle_cohort_controller {
	int finish_calls = 0;
	int pending_before_success = 0;
	int cleanup_pending_before_success = 0;
	int abandon_calls = 0;
	int abandon_pending_before_success = 0;
	bool recovery_pending = false;
};

static int live_workers = 0;
static int destructive_cleanup_calls = 0;
static int normal_finish_calls = 0;

extern "C" int darling_lifecycle_cohort_finish(
	struct darling_lifecycle_cohort_controller* controller) {
	assert(controller);
	++normal_finish_calls;
	++controller->finish_calls;
	if (controller->recovery_pending)
		return DARLING_LIFECYCLE_FINISH_RECOVERY_PENDING;
	if (controller->finish_calls <= controller->pending_before_success)
		return DARLING_LIFECYCLE_FINISH_DRAIN_PENDING;
	if (controller->cleanup_pending_before_success-- > 0)
		return DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING;
	++destructive_cleanup_calls;
	--live_workers;
	delete controller;
	return DARLING_LIFECYCLE_FINISH_OK;
}

extern "C" int darling_lifecycle_cohort_abandon(
	struct darling_lifecycle_cohort_controller* controller) {
	assert(controller);
	++controller->abandon_calls;
	if (controller->abandon_calls <= controller->abandon_pending_before_success)
		return DARLING_LIFECYCLE_ABANDON_PENDING;
	--live_workers;
	delete controller;
	return 0;
}

int main(int argc, char** argv) {
	if (argc == 2) {
		const int endpoint = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
		assert(endpoint >= 0);
		struct sockaddr_un address = {};
		address.sun_family = AF_UNIX;
		assert(std::strlen(argv[1]) < sizeof(address.sun_path));
		std::strcpy(address.sun_path, argv[1]);
		assert(bind(endpoint, reinterpret_cast<struct sockaddr*>(&address),
			sizeof(address)) == 0);
		auto* controller = new darling_lifecycle_cohort_controller{
			0, std::numeric_limits<int>::max(), 0, 0, std::numeric_limits<int>::max()};
		++live_workers;
		LifecycleCohortOwner owner;
		assert(owner.adopt(controller));
		(void)endpoint;
		return 0; /* bounded destructor retries then fail-closed abort */
	}
	{
		auto* controller = new darling_lifecycle_cohort_controller{0, 2, 0, 0, 0};
		++live_workers;
		LifecycleCohortOwner owner;
		assert(owner.adopt(controller));
		assert(owner.finish() == DARLING_LIFECYCLE_FINISH_OK);
		assert(live_workers == 0);
	}
	{
		auto* controller = new darling_lifecycle_cohort_controller{0, 0, 0, 0, 0, true};
		++live_workers;
		LifecycleCohortOwner owner;
		assert(owner.adopt(controller));
		const int finishBefore = normal_finish_calls;
		assert(owner.finish() == DARLING_LIFECYCLE_FINISH_RECOVERY_PENDING);
		assert(owner.finish() == DARLING_LIFECYCLE_FINISH_RECOVERY_PENDING);
		assert(normal_finish_calls == finishBefore + 1);
		assert(controller->abandon_calls == 0);
		assert(owner.takeRecoveryForHandoff() == controller);
		--live_workers;
		delete controller;
	}
	{
		auto* controller = new darling_lifecycle_cohort_controller{0, 99, 0, 0, 1};
		++live_workers;
		LifecycleCohortOwner owner;
		assert(owner.adopt(controller));
		const int before = normal_finish_calls;
		assert(owner.finish() == DARLING_LIFECYCLE_ABANDON_PENDING);
		assert(normal_finish_calls == before + 3);
		assert(owner.finish() == DARLING_LIFECYCLE_FINISH_ABANDONED);
		/* ABANDONING never returned to the now-successful destructive finish. */
		assert(normal_finish_calls == before + 3);
	}
	{
		auto* controller = new darling_lifecycle_cohort_controller{0, 0, 2, 0, 0};
		++live_workers;
		LifecycleCohortOwner owner;
		assert(owner.adopt(controller));
		const int before = normal_finish_calls;
		assert(owner.finish() == DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING);
		assert(owner.finish() == DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING);
		assert(controller->abandon_calls == 0);
		assert(owner.finish() == DARLING_LIFECYCLE_FINISH_OK);
		assert(normal_finish_calls == before + 3);
	}
	assert(live_workers == 0);
	assert(destructive_cleanup_calls == 2);
	return 0;
}

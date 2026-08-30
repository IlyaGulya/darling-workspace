#include <darling_lifecycle_cohort.h>

#include <fcntl.h>
#include <unistd.h>

#include <cstdio>

#if DARLING_LIFECYCLE_COHORT_ABI_VERSION != 5
#error "unexpected lifecycle cohort ABI"
#endif

int main(int argc, char** argv) {
	if (argc != 2)
		return 64;
	const int prefix = open(argv[1], O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
	if (prefix < 0)
		return 65;
	darling_lifecycle_cohort_bootstrap bootstrap = {};
	auto* controller = darling_lifecycle_cohort_start(prefix, prefix, argv[1], getpid(), &bootstrap);
	close(prefix);
	if (!controller)
		return 66;
	if (darling_lifecycle_guest_namespace_configure(controller) != 0)
		return 67;
	if (bootstrap.darlingserver_fd >= 0)
		close(bootstrap.darlingserver_fd);
	if (bootstrap.dserver_log_fd >= 0)
		close(bootstrap.dserver_log_fd);
	const int result = darling_lifecycle_cohort_finish(controller);
	if (result != DARLING_LIFECYCLE_FINISH_OK)
		return 68;
	std::puts("RUNTIME_LOWER_BINDING_ABI_VALID");
	return 0;
}

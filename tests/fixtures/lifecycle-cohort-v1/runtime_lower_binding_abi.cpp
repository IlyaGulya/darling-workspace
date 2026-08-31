#include <darling_lifecycle_cohort.h>

#include <fcntl.h>
#include <unistd.h>

#include <cstdio>

#if DARLING_LIFECYCLE_COHORT_ABI_VERSION != 6
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
	darling_lifecycle_user_home_plan home = {};
	home.schema_version = DARLING_LIFECYCLE_USER_HOME_SCHEMA_V1;
	home.owner_uid = geteuid();
	home.owner_gid = getegid();
	home.shared_mode = DARLING_LIFECYCLE_USER_HOME_SHARED_MODE;
	home.user_mode = DARLING_LIFECYCLE_USER_HOME_USER_MODE;
	home.login = "abi-user";
	home.targets[0] = "/Volumes/SystemRoot/home/abi-user";
	if (darling_lifecycle_cohort_prepare_user_home(controller, &home) != 0)
		return 69;
	if (darling_lifecycle_guest_namespace_configure(controller) != 0)
		return 67;
	if (bootstrap.darlingserver_fd >= 0)
		close(bootstrap.darlingserver_fd);
	if (bootstrap.dserver_log_fd >= 0)
		close(bootstrap.dserver_log_fd);
	int result = DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING;
	for (unsigned int attempt = 0;
		attempt < 64 && result == DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING;
		++attempt) {
		result = darling_lifecycle_cohort_finish(controller);
		if (result == DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING)
			usleep(1000);
	}
	if (result != DARLING_LIFECYCLE_FINISH_OK) {
		std::fprintf(stderr, "finish status=%d\n", result);
		return 68;
	}
	std::puts("RUNTIME_LOWER_BINDING_ABI_VALID");
	return 0;
}

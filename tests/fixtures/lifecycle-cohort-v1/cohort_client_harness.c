#define _GNU_SOURCE
#include <darling_lifecycle_cohort.h>

#include "lifecycle_cohort_client.h"

#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

static void fail(const char* message) {
	perror(message);
	exit(1);
}

static void write_file(const char* path, const char* value, mode_t mode) {
	int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, mode);
	if (fd < 0)
		fail("open fixture file");
	if (write(fd, value, strlen(value)) != (ssize_t)strlen(value))
		fail("write fixture file");
	if (fchmod(fd, mode) != 0)
		fail("fchmod fixture file");
	close(fd);
}

static void expect_missing(const char* path) {
	struct stat status;
	errno = 0;
	if (lstat(path, &status) == 0 || errno != ENOENT)
		fail("lifecycle object was not removed");
}

static void prepare_prefix(const char* prefix) {
	char path[4096];
	char state[1024];
	if (snprintf(path, sizeof(path), "%s/var/run", prefix) >= (int)sizeof(path))
		fail("path too long");
	if (mkdir(prefix, 0700) != 0 || mkdir(strcat(strcpy(path, prefix), "/var"), 0700) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/var/run"), 0700) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/var/tmp"), 0700) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/var/tmp/launchd"), 0700) != 0)
		fail("mkdir fixture");
	if (chmod(strcat(strcpy(path, prefix), "/var"), 0755) != 0 ||
		chmod(strcat(strcpy(path, prefix), "/var/run"), 0755) != 0 ||
		chmod(strcat(strcpy(path, prefix), "/var/tmp"), 01777) != 0 ||
		chmod(strcat(strcpy(path, prefix), "/var/tmp/launchd"), 0700) != 0)
		fail("chmod fixture directories");
	struct stat metadata;
	if (stat(prefix, &metadata) != 0)
		fail("stat prefix fixture");
	int state_length = snprintf(state, sizeof(state),
		"DARLING_PREFIX_STATE_V2\n"
		"schema_version=2\n"
		"runtime_mode=rootless-eunion\n"
		"generation=1\n"
		"prefix_device=%ju\n"
		"prefix_inode=%ju\n"
		"owner_uid=%ju\n"
		"owner_gid=%ju\n"
		"provenance=darling-runtime-prefix-lifecycle-v2\n",
		(uintmax_t)metadata.st_dev, (uintmax_t)metadata.st_ino,
		(uintmax_t)metadata.st_uid, (uintmax_t)metadata.st_gid);
	if (state_length <= 0 || (size_t)state_length >= sizeof(state) ||
		snprintf(path, sizeof(path), "%s/.darling-prefix-state-v2", prefix) >= (int)sizeof(path))
		fail("prefix state fixture");
	write_file(path, state, 0600);
}

static size_t open_fd_count(void) {
	DIR* directory = opendir("/proc/self/fd");
	if (!directory)
		fail("open fd census");
	size_t count = 0;
	while (readdir(directory))
		++count;
	closedir(directory);
	return count;
}

static void verify_malformed_response_fd_is_closed(const char* prefix) {
	char endpoint[4096];
	if (snprintf(endpoint, sizeof(endpoint), "%s/.lc-v1.sock", prefix) >=
		(int)sizeof(endpoint))
		fail("fake controller path");
	int listener = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
	if (listener < 0)
		fail("fake controller socket");
	struct sockaddr_un address = {.sun_family = AF_UNIX};
	if (strlen(endpoint) >= sizeof(address.sun_path))
		fail("fake controller path capacity");
	strcpy(address.sun_path, endpoint);
	if (bind(listener, (struct sockaddr*)&address, sizeof(address)) != 0 || listen(listener, 1) != 0)
		fail("fake controller bind");
	char nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES + 1];
	memset(nonce, '0', DARLING_LIFECYCLE_NONCE_HEX_BYTES);
	nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES] = 0;
	if (setenv("DARLING_LIFECYCLE_COHORT_V1", "1", 1) != 0 ||
		setenv("DARLING_LIFECYCLE_CONTROL_NAME", "/.lc-v1.sock", 1) != 0 ||
		setenv("DARLING_LIFECYCLE_CONTROL_NONCE", nonce, 1) != 0 ||
		setenv("DARLING_LIFECYCLE_CONTROL_TEST_ROOT", prefix, 1) != 0)
		fail("fake controller environment");
	pid_t server = fork();
	if (server < 0)
		fail("fake controller fork");
	if (server == 0) {
		int client = accept4(listener, NULL, NULL, SOCK_CLOEXEC);
		unsigned char request[48];
		if (client < 0 || recv(client, request, sizeof(request), 0) != sizeof(request))
			_exit(122);
		struct {
			unsigned char bytes[32];
		} malformed = {};
		int passed[2] = {
			open("/dev/null", O_RDONLY | O_CLOEXEC),
			open("/dev/null", O_RDONLY | O_CLOEXEC),
		};
		char control[CMSG_SPACE(sizeof(passed))] = {};
		struct iovec iov = {.iov_base = &malformed, .iov_len = sizeof(malformed)};
		struct msghdr message = {
			.msg_iov = &iov,
			.msg_iovlen = 1,
			.msg_control = control,
			.msg_controllen = sizeof(control),
		};
		struct cmsghdr* header = CMSG_FIRSTHDR(&message);
		header->cmsg_level = SOL_SOCKET;
		header->cmsg_type = SCM_RIGHTS;
		header->cmsg_len = CMSG_LEN(sizeof(passed));
		memcpy(CMSG_DATA(header), passed, sizeof(passed));
		if (passed[0] < 0 || passed[1] < 0 ||
			sendmsg(client, &message, MSG_NOSIGNAL) != sizeof(malformed))
			_exit(123);
		_exit(0);
	}
	size_t before = open_fd_count();
	if (darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD) >= 0)
		fail("malformed controller response accepted");
	int status = 0;
	if (waitpid(server, &status, 0) != server || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
		fail("fake controller response");
	if (open_fd_count() != before)
		fail("malformed SCM_RIGHTS leaked descriptor");
	close(listener);
	if (unlink(endpoint) != 0)
		fail("fake controller cleanup");
}

static pid_t only_child_pid(void) {
	char path[128];
	if (snprintf(path, sizeof(path), "/proc/self/task/%d/children", getpid()) >= (int)sizeof(path))
		fail("children path");
	FILE* file = fopen(path, "r");
	pid_t child = -1;
	pid_t extra = -1;
	if (!file || fscanf(file, "%d", &child) != 1 || fscanf(file, "%d", &extra) == 1) {
		if (file)
			fclose(file);
		fail("controller child census");
	}
	fclose(file);
	return child;
}

static struct darling_lifecycle_cohort_controller* start_controller(
	const char* prefix,
	pid_t init_pid,
	struct darling_lifecycle_cohort_bootstrap* bootstrap
) {
	int prefix_fd = open(prefix, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
	if (prefix_fd < 0)
		fail("open retained prefix");
	struct darling_lifecycle_cohort_controller* controller =
		darling_lifecycle_cohort_start(prefix_fd, prefix, init_pid, bootstrap);
	close(prefix_fd);
	return controller;
}

static void verify_sigkill_owner_cleanup(const char* parent_prefix) {
	char prefix[4096];
	if (snprintf(prefix, sizeof(prefix), "%s-owner-killed", parent_prefix) >= (int)sizeof(prefix))
		fail("killed-owner prefix");
	prepare_prefix(prefix);
	int ready[2];
	if (pipe2(ready, O_CLOEXEC) != 0)
		fail("owner ready pipe");
	pid_t owner = fork();
	if (owner < 0)
		fail("fork killed owner");
	if (owner == 0) {
		close(ready[0]);
		if (setpgid(0, 0) != 0)
			_exit(119);
		struct darling_lifecycle_cohort_bootstrap bootstrap = {};
		struct darling_lifecycle_cohort_controller* controller =
			start_controller(prefix, getpid(), &bootstrap);
		if (!controller || bootstrap.darlingserver_fd < 0)
			_exit(120);
		close(bootstrap.darlingserver_fd);
		pid_t supervisor = only_child_pid();
		if (write(ready[1], &supervisor, sizeof(supervisor)) != sizeof(supervisor))
			_exit(121);
		for (;;)
			pause();
	}
	close(ready[1]);
	pid_t supervisor = -1;
	if (read(ready[0], &supervisor, sizeof(supervisor)) != sizeof(supervisor) || supervisor <= 0)
		fail("controller supervisor readiness");
	close(ready[0]);
	int supervisor_pidfd = (int)syscall(SYS_pidfd_open, supervisor, 0);
	if (supervisor_pidfd < 0)
		fail("pidfd_open controller supervisor");
	if (kill(-owner, SIGKILL) != 0)
		fail("kill controller owner process group");
	int owner_status = 0;
	if (waitpid(owner, &owner_status, 0) != owner || !WIFSIGNALED(owner_status) ||
		WTERMSIG(owner_status) != SIGKILL)
		fail("wait killed controller owner");
	struct pollfd descriptor = {.fd = supervisor_pidfd, .events = POLLIN};
	if (poll(&descriptor, 1, 2000) != 1 || !(descriptor.revents & POLLIN))
		fail("controller supervisor did not clean up after owner death");
	close(supervisor_pidfd);
	const char* removed[] = {
		"/.init.pid",
		"/.darlingserver.sock",
		"/.lc-v1.sock",
	};
	char path[4096];
	for (size_t index = 0; index < sizeof(removed) / sizeof(removed[0]); ++index) {
		if (snprintf(path, sizeof(path), "%s%s", prefix, removed[index]) >= (int)sizeof(path))
			fail("killed-owner cleanup path");
		expect_missing(path);
	}
}

static void configure_transport(const struct darling_lifecycle_cohort_bootstrap* bootstrap) {
	char name[DARLING_LIFECYCLE_CONTROL_NAME_CAPACITY + 1] = {};
	char nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES + 1] = {};
	if (bootstrap->control_name_len == 0 ||
		bootstrap->control_name_len >= DARLING_LIFECYCLE_CONTROL_NAME_CAPACITY)
		fail("invalid control name");
	memcpy(name, bootstrap->control_name, bootstrap->control_name_len);
	memcpy(nonce, bootstrap->nonce_hex, DARLING_LIFECYCLE_NONCE_HEX_BYTES);
	if (setenv("DARLING_LIFECYCLE_COHORT_V1", "1", 1) != 0 ||
		setenv("DARLING_LIFECYCLE_CONTROL_NAME", name, 1) != 0 ||
		setenv("DARLING_LIFECYCLE_CONTROL_NONCE", nonce, 1) != 0)
		fail("setenv transport");
}

static int shellspawn_client(const char* prefix) {
	int shellspawn = darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_SHELLSPAWN);
	if (shellspawn < 0)
		fail("publish shellspawn");
	const char* action = getenv("COHORT_HARNESS_SHELLSPAWN_ACTION");
	if (action && strcmp(action, "hold") == 0) {
		const char* raw_ready = getenv("COHORT_HARNESS_READY_FD");
		int ready = raw_ready ? atoi(raw_ready) : -1;
		char byte = 'R';
		if (ready < 0 || write(ready, &byte, 1) != 1)
			fail("shellspawn hold readiness");
		for (;;)
			pause();
	}
	if (action && strcmp(action, "restart") == 0) {
		struct stat status;
		if (fstat(shellspawn, &status) != 0 || !S_ISSOCK(status.st_mode))
			fail("restarted shellspawn endpoint");
		if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_SHELLSPAWN) != 0)
			fail("retire restarted shellspawn");
		close(shellspawn);
		return 0;
	}
	char endpoint[4096];
	char saved[4096];
	snprintf(endpoint, sizeof(endpoint), "%s/var/run/shellspawn.sock", prefix);
	snprintf(saved, sizeof(saved), "%s/var/run/shellspawn.sock.saved", prefix);
	if (rename(endpoint, saved) != 0)
		fail("stage endpoint replacement");
	write_file(endpoint, "replacement", 0600);
	if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_SHELLSPAWN) == 0)
		fail("endpoint replacement accepted");
	char replacement[32] = {};
	int replacement_fd = open(endpoint, O_RDONLY | O_CLOEXEC);
	if (replacement_fd < 0 || read(replacement_fd, replacement, sizeof(replacement)) != 11 ||
		memcmp(replacement, "replacement", 11) != 0)
		fail("replacement was not preserved");
	close(replacement_fd);
	if (unlink(endpoint) != 0 || rename(saved, endpoint) != 0)
		fail("restore endpoint fixture");
	if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_SHELLSPAWN) != 0)
		fail("retire restored shellspawn");
	close(shellspawn);
	return 0;
}

static void verify_shellspawn_keepalive_restart(const char* prefix, const char* program) {
	int ready[2];
	if (pipe(ready) != 0)
		fail("shellspawn KeepAlive pipe");
	pid_t first = fork();
	if (first < 0)
		fail("fork first shellspawn");
	if (first == 0) {
		close(ready[0]);
		char ready_value[32];
		snprintf(ready_value, sizeof(ready_value), "%d", ready[1]);
		setenv("COHORT_HARNESS_SHELLSPAWN_PREFIX", prefix, 1);
		setenv("COHORT_HARNESS_SHELLSPAWN_ACTION", "hold", 1);
		setenv("COHORT_HARNESS_READY_FD", ready_value, 1);
		execl(program, "shellspawn", NULL);
		_exit(127);
	}
	close(ready[1]);
	char ready_byte = 0;
	if (read(ready[0], &ready_byte, 1) != 1 || ready_byte != 'R')
		fail("first shellspawn readiness");
	close(ready[0]);
	if (kill(first, SIGKILL) != 0)
		fail("SIGKILL shellspawn");
	int first_status = 0;
	if (waitpid(first, &first_status, 0) != first || !WIFSIGNALED(first_status) ||
		WTERMSIG(first_status) != SIGKILL)
		fail("wait killed shellspawn");

	pid_t restarted = fork();
	if (restarted < 0)
		fail("fork restarted shellspawn");
	if (restarted == 0) {
		setenv("COHORT_HARNESS_SHELLSPAWN_PREFIX", prefix, 1);
		setenv("COHORT_HARNESS_SHELLSPAWN_ACTION", "restart", 1);
		execl(program, "shellspawn", NULL);
		_exit(127);
	}
	int restart_status = 0;
	if (waitpid(restarted, &restart_status, 0) != restarted || !WIFEXITED(restart_status) ||
		WEXITSTATUS(restart_status) != 0)
		fail("KeepAlive shellspawn restart");
	char endpoint[4096];
	if (snprintf(endpoint, sizeof(endpoint), "%s/var/run/shellspawn.sock", prefix) >=
		(int)sizeof(endpoint))
		fail("shellspawn restart path");
	expect_missing(endpoint);
}

static int reject_activation(int endpoint_fd, void* context) {
	(void)endpoint_fd;
	(void)context;
	errno = EIO;
	return -1;
}

struct nonce_mutation_context {
	const char* replacement;
};

static int mutate_nonce_during_activation(int endpoint_fd, void* context) {
	(void)endpoint_fd;
	const struct nonce_mutation_context* mutation = context;
	return setenv("DARLING_LIFECYCLE_CONTROL_NONCE", mutation->replacement, 1);
}

static void verify_pending_nonce_snapshot(
	const char* original_nonce,
	const char* replacement_nonce
) {
	struct nonce_mutation_context mutation = {.replacement = replacement_nonce};
	int endpoint = darling_lifecycle_publish_and_activate_endpoint(
		DARLING_LIFECYCLE_ENDPOINT_LAUNCHD,
		&mutate_nonce_during_activation,
		&mutation
	);
	int activation_errno = errno;
	if (setenv("DARLING_LIFECYCLE_CONTROL_NONCE", original_nonce, 1) != 0)
		fail("restore nonce after activation mutation");
	errno = activation_errno;
	if (endpoint < 0)
		fail("pending transaction reread mutated nonce");
	if (darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD) >= 0)
		fail("nonce snapshot commit did not retain ownership");
	if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD) != 0)
		fail("retire nonce snapshot endpoint");
	close(endpoint);
}

static void verify_adoption_fault_rollback(const char* prefix, const char* fault) {
	char endpoint[4096];
	if (snprintf(endpoint, sizeof(endpoint), "%s/var/tmp/launchd/sock", prefix) >=
		(int)sizeof(endpoint))
		fail("adoption fault endpoint path");
	if (setenv("DARLING_LIFECYCLE_COHORT_TEST_ADOPTION_FAULT", fault, 1) != 0)
		fail("set adoption fault");
	if (darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD) >= 0)
		fail("adoption fault accepted");
	if (unsetenv("DARLING_LIFECYCLE_COHORT_TEST_ADOPTION_FAULT") != 0)
		fail("clear adoption fault");
	for (size_t attempt = 0; attempt < 100; ++attempt) {
		if (lstat(endpoint, &(struct stat){0}) != 0 && errno == ENOENT)
			return;
		usleep(5000);
	}
	fail("adoption fault did not roll back pending endpoint");
}

static void verify_lost_final_ack_commit(void) {
	if (setenv("DARLING_LIFECYCLE_COHORT_TEST_FINAL_ACK_FAULT", "lost", 1) != 0)
		fail("set final ACK fault");
	int endpoint = darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD);
	if (endpoint < 0)
		fail("lost final ACK revoked committed endpoint");
	if (unsetenv("DARLING_LIFECYCLE_COHORT_TEST_FINAL_ACK_FAULT") != 0)
		fail("clear final ACK fault");
	int accepting = 0;
	socklen_t option_length = sizeof(accepting);
	if (getsockopt(endpoint, SOL_SOCKET, SO_ACCEPTCONN, &accepting, &option_length) != 0 ||
		option_length != sizeof(accepting) || accepting != 1)
		fail("committed endpoint listener was not retained after ACK loss");
	if (darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD) >= 0)
		fail("lost final ACK made committed endpoint publishable again");
	if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD) != 0)
		fail("retire endpoint committed across ACK loss");
	close(endpoint);
}

static int unauthorized_client(enum darling_lifecycle_endpoint_kind kind) {
	if (darling_lifecycle_publish_endpoint(kind) >= 0)
		fail("unauthorized peer accepted");
	return 0;
}

static void expect_unauthorized_child(
	const char* program,
	const char* argv0,
	enum darling_lifecycle_endpoint_kind kind
) {
	char kind_value[16];
	snprintf(kind_value, sizeof(kind_value), "%d", (int)kind);
	pid_t child = fork();
	if (child < 0)
		fail("fork unauthorized fixture");
	if (child == 0) {
		execl(program, argv0, "--unauthorized-client", kind_value, NULL);
		_exit(127);
	}
	int child_status = 0;
	if (waitpid(child, &child_status, 0) != child || !WIFEXITED(child_status) ||
		WEXITSTATUS(child_status) != 0)
		fail("unauthorized fixture failed");
}

int main(int argc, char** argv) {
	const char* shellspawn_prefix = getenv("COHORT_HARNESS_SHELLSPAWN_PREFIX");
	if (argc == 1 && shellspawn_prefix && strcmp(argv[0], "shellspawn") == 0)
		return shellspawn_client(shellspawn_prefix);
	if (argc == 3 && strcmp(argv[1], "--unauthorized-client") == 0)
		return unauthorized_client((enum darling_lifecycle_endpoint_kind)atoi(argv[2]));
	const char* prefix = getenv("COHORT_HARNESS_LAUNCHD_PREFIX");
	if (argc != 1 || !prefix || strcmp(argv[0], "/sbin/launchd") != 0) {
		fprintf(stderr, "usage: COHORT_HARNESS_LAUNCHD_PREFIX=... /sbin/launchd\n");
		return 2;
	}
	char path[4096];
	prepare_prefix(prefix);
	verify_malformed_response_fd_is_closed(prefix);
	struct darling_lifecycle_cohort_bootstrap bootstrap = {};
	struct darling_lifecycle_cohort_controller* controller =
		start_controller(prefix, getppid(), &bootstrap);
	if (!controller || bootstrap.darlingserver_fd < 0)
		fail("start Rust controller");
	configure_transport(&bootstrap);
	if (setenv("DARLING_LIFECYCLE_CONTROL_TEST_ROOT", prefix, 1) != 0)
		fail("setenv test root");

	struct stat status;
	if (fstat(bootstrap.darlingserver_fd, &status) != 0 || !S_ISSOCK(status.st_mode))
		fail("Darlingserver capability");
	if (snprintf(path, sizeof(path), "%s/.darlingserver.sock", prefix) >= (int)sizeof(path) ||
		stat(path, &status) != 0 || !S_ISSOCK(status.st_mode) || (status.st_mode & 0777) != 0775)
		fail("Darlingserver endpoint mode");
	if (snprintf(path, sizeof(path), "%s/.init.pid", prefix) >= (int)sizeof(path) ||
		stat(path, &status) != 0 || !S_ISREG(status.st_mode) || (status.st_mode & 0777) != 0600)
		fail("init pid mode");
	if (snprintf(path, sizeof(path), "%s/.lc-v1.sock", prefix) >=
		(int)sizeof(path) || stat(path, &status) != 0 || !S_ISSOCK(status.st_mode) ||
		(status.st_mode & 0777) != 0600)
		fail("controller endpoint mode");
	if (snprintf(path, sizeof(path), "%s/.lifecycle.lock", prefix) >= (int)sizeof(path))
		fail("lock path");
	int competing_lock = open(path, O_RDWR | O_NOFOLLOW | O_CLOEXEC);
	if (competing_lock < 0 || flock(competing_lock, LOCK_EX | LOCK_NB) == 0)
		fail("exclusive lifecycle lease not retained");
	close(competing_lock);

	char original_nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES + 1];
	strcpy(original_nonce, getenv("DARLING_LIFECYCLE_CONTROL_NONCE"));
	char wrong_nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES + 1];
	memset(wrong_nonce, '0', DARLING_LIFECYCLE_NONCE_HEX_BYTES);
	wrong_nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES] = 0;
	setenv("DARLING_LIFECYCLE_CONTROL_NONCE", wrong_nonce, 1);
	for (size_t rejected = 0; rejected < 129; ++rejected) {
		if (darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD) >= 0)
			fail("wrong nonce flood accepted");
	}
	setenv("DARLING_LIFECYCLE_CONTROL_NONCE", original_nonce, 1);

	expect_unauthorized_child("/proc/self/exe", "not-launchd", DARLING_LIFECYCLE_ENDPOINT_LAUNCHD);
	verify_adoption_fault_rollback(prefix, "dup");
	verify_adoption_fault_rollback(prefix, "listen");
	verify_lost_final_ack_commit();
	verify_pending_nonce_snapshot(original_nonce, wrong_nonce);
	if (darling_lifecycle_publish_and_activate_endpoint(
			DARLING_LIFECYCLE_ENDPOINT_LAUNCHD, &reject_activation, NULL) >= 0)
		fail("post-publication activation failure accepted");
	if (snprintf(path, sizeof(path), "%s/var/tmp/launchd/sock", prefix) >= (int)sizeof(path))
		fail("launchd rollback path");
	expect_missing(path);
	int launchd = darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD);
	if (launchd < 0)
		fail("publish launchd");
	expect_unauthorized_child("/proc/self/exe", "not-shellspawn", DARLING_LIFECYCLE_ENDPOINT_SHELLSPAWN);
	char shellspawn_program[4096];
	if (snprintf(shellspawn_program, sizeof(shellspawn_program), "%s/shellspawn", prefix) >=
		(int)sizeof(shellspawn_program) || symlink("/proc/self/exe", shellspawn_program) != 0)
		fail("create shellspawn fixture executable");
	verify_shellspawn_keepalive_restart(prefix, shellspawn_program);
	pid_t child = fork();
	if (child < 0)
		fail("fork shellspawn fixture");
	if (child == 0) {
		setenv("COHORT_HARNESS_SHELLSPAWN_PREFIX", prefix, 1);
		execl(shellspawn_program, "shellspawn", NULL);
		_exit(127);
	}
	int child_status = 0;
	if (waitpid(child, &child_status, 0) != child || !WIFEXITED(child_status) ||
		WEXITSTATUS(child_status) != 0)
		fail("shellspawn fixture failed");
	if (unlink(shellspawn_program) != 0)
		fail("remove shellspawn fixture executable");
	close(launchd);
	if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_LAUNCHD) != 0)
		fail("retire launchd");
	close(bootstrap.darlingserver_fd);
	if (darling_lifecycle_cohort_finish(controller) != 0)
		fail("finish Rust controller");
	const char* removed[] = {
		"/.init.pid",
		"/.darlingserver.sock",
		"/var/run/shellspawn.sock",
		"/var/tmp/launchd/sock",
		"/.lc-v1.sock",
	};
	for (size_t index = 0; index < sizeof(removed) / sizeof(removed[0]); ++index) {
		if (snprintf(path, sizeof(path), "%s%s", prefix, removed[index]) >= (int)sizeof(path))
			fail("cleanup path");
		expect_missing(path);
	}
	verify_sigkill_owner_cleanup(prefix);

	printf("LIFECYCLE_COHORT_ROUTING_VALID endpoints=5 lease=exact-exclusive-flock replacement=preserved shellspawn_keepalive=ready activation_rollback=clean commit_ack_loss=retained nonce_snapshot=stable flood=bounded owner_group_sigkill=clean scm_rights_leaks=0\n");
	return 0;
}

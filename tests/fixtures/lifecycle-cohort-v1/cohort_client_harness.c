#define _GNU_SOURCE
#include <darling_lifecycle_cohort.h>

#include "lifecycle_cohort_client.h"

#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <pthread.h>
#include <stdatomic.h>
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

static atomic_bool churn_running;

static void* allocator_churn(void* context) {
	(void)context;
	while (atomic_load_explicit(&churn_running, memory_order_relaxed)) {
		void* value = malloc(4096);
		if (value) {
			memset(value, 0x5a, 4096);
			free(value);
		}
	}
	return NULL;
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
	char executable[4096];
	char state[2048];
	char binding[4096];
	const char* worker = getenv("COHORT_HARNESS_WORKER");
	ssize_t executable_length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
	if (!worker || executable_length <= 0 || executable_length >= (ssize_t)sizeof(executable))
		fail("fixture executable identity");
	executable[executable_length] = 0;
	if (mkdir(prefix, 0700) != 0 || mkdir(strcat(strcpy(path, prefix), "/var"), 0700) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/var/run"), 0700) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/var/tmp"), 0700) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/var/tmp/launchd"), 0700) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/bin"), 0755) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/libexec"), 0755) != 0 ||
		mkdir(strcat(strcpy(path, prefix), "/libexec/darling"), 0755) != 0)
		fail("mkdir fixture");
	if (chmod(strcat(strcpy(path, prefix), "/var"), 0755) != 0 ||
		chmod(strcat(strcpy(path, prefix), "/var/run"), 0755) != 0 ||
		chmod(strcat(strcpy(path, prefix), "/var/tmp"), 01777) != 0 ||
		chmod(strcat(strcpy(path, prefix), "/var/tmp/launchd"), 0700) != 0)
		fail("chmod fixture directories");
	if (link(executable, strcat(strcpy(path, prefix), "/bin/darlingserver")) != 0 ||
		link(worker, strcat(strcpy(path, prefix), "/libexec/darling-lifecycle-controller-worker")) != 0)
		fail("link fixture executable");
	if (chmod(strcat(strcpy(path, prefix), "/libexec/darling-lifecycle-controller-worker"), 0755) != 0)
		fail("chmod worker fixture");
	struct stat metadata, lower, controller, worker_metadata;
	if (stat(prefix, &metadata) != 0 ||
		stat(strcat(strcpy(path, prefix), "/libexec/darling"), &lower) != 0 ||
		stat(strcat(strcpy(path, prefix), "/bin/darlingserver"), &controller) != 0 ||
		stat(strcat(strcpy(path, prefix), "/libexec/darling-lifecycle-controller-worker"), &worker_metadata) != 0)
		fail("stat deployment fixture");
	int state_length = snprintf(state, sizeof(state),
		"DARLING_PREFIX_STATE_V3\nschema_version=3\nruntime_mode=rootless-eunion\ngeneration=1\n"
		"prefix_device=%ju\nprefix_inode=%ju\nsidecar_device=%ju\nsidecar_inode=%ju\n"
		"owner_uid=%ju\nowner_gid=%ju\nprovenance=darling-runtime-prefix-sidecar-v1\n",
		(uintmax_t)metadata.st_dev, (uintmax_t)metadata.st_ino,
		(uintmax_t)metadata.st_dev, (uintmax_t)metadata.st_ino,
		(uintmax_t)metadata.st_uid, (uintmax_t)metadata.st_gid);
	if (state_length <= 0 || (size_t)state_length >= sizeof(state))
		fail("prefix state fixture");
	write_file(strcat(strcpy(path, prefix), "/.darling-prefix-state-v3"), state, 0600);
	int binding_length = snprintf(binding, sizeof(binding),
		"DARLING_RUNTIME_LOWER_BINDING_V3\nschema_version=3\ntransaction_id=11111111111111111111111111111111\nprefix_generation=1\n"
		"session_prefix_device=%ju\nsession_prefix_inode=%ju\ndestination=libexec/darling\n"
		"prefix_device=%ju\nprefix_inode=%ju\nlower_device=%ju\nlower_inode=%ju\nlower_type=directory\nlower_mode=%ju\nlower_uid=%ju\nlower_gid=%ju\n"
		"controller_destination=bin/darlingserver\ncontroller_device=%ju\ncontroller_inode=%ju\ncontroller_type=regular\ncontroller_mode=%ju\ncontroller_uid=%ju\ncontroller_gid=%ju\n"
		"worker_destination=libexec/darling-lifecycle-controller-worker\nworker_device=%ju\nworker_inode=%ju\nworker_type=regular\nworker_mode=%ju\nworker_uid=%ju\nworker_gid=%ju\nprovenance=product-deployment-transaction-v3\n",
		(uintmax_t)metadata.st_dev, (uintmax_t)metadata.st_ino,
		(uintmax_t)metadata.st_dev, (uintmax_t)metadata.st_ino,
		(uintmax_t)lower.st_dev, (uintmax_t)lower.st_ino, (uintmax_t)(lower.st_mode & 07777), (uintmax_t)lower.st_uid, (uintmax_t)lower.st_gid,
		(uintmax_t)controller.st_dev, (uintmax_t)controller.st_ino, (uintmax_t)(controller.st_mode & 07777), (uintmax_t)controller.st_uid, (uintmax_t)controller.st_gid,
		(uintmax_t)worker_metadata.st_dev, (uintmax_t)worker_metadata.st_ino, (uintmax_t)(worker_metadata.st_mode & 07777), (uintmax_t)worker_metadata.st_uid, (uintmax_t)worker_metadata.st_gid);
	if (binding_length <= 0 || (size_t)binding_length >= sizeof(binding))
		fail("binding fixture");
	write_file(strcat(strcpy(path, prefix), "/.darling-runtime-lower-binding-v1"), binding, 0600);
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
		darling_lifecycle_cohort_start(prefix_fd, prefix_fd, prefix, init_pid, bootstrap);
	close(prefix_fd);
	return controller;
}

static void verify_sigkill_owner_forensic_preserve(const char* parent_prefix) {
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
	const char* retained[] = {
		"/.init.pid",
		"/.darlingserver.sock",
		"/.lc-v1.sock",
	};
	struct stat before[sizeof(retained) / sizeof(retained[0])];
	char path[4096];
	for (size_t index = 0; index < sizeof(retained) / sizeof(retained[0]); ++index) {
		if (snprintf(path, sizeof(path), "%s%s", prefix, retained[index]) >= (int)sizeof(path) ||
			lstat(path, &before[index]) != 0)
			fail("capture killed-owner forensic identity");
	}
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
	for (size_t index = 0; index < sizeof(retained) / sizeof(retained[0]); ++index) {
		struct stat after;
		if (snprintf(path, sizeof(path), "%s%s", prefix, retained[index]) >= (int)sizeof(path) ||
			lstat(path, &after) != 0 || after.st_dev != before[index].st_dev ||
			after.st_ino != before[index].st_ino)
			fail("killed-owner forensic identity was not preserved");
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

static int accept_dynamic_listener(int endpoint_fd, void* context) {
	(void)context;
	int accepting = 0;
	socklen_t length = sizeof(accepting);
	return getsockopt(endpoint_fd, SOL_SOCKET, SO_ACCEPTCONN, &accepting, &length) == 0 &&
		length == sizeof(accepting) && accepting == 1 ? 0 : -1;
}

static size_t directory_entry_count(const char* path) {
	DIR* directory = opendir(path);
	if (!directory)
		fail("open dynamic parent census");
	size_t count = 0;
	struct dirent* entry;
	while ((entry = readdir(directory))) {
		if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, ".."))
			++count;
	}
	closedir(directory);
	return count;
}

static int mutate_nonce_and_accept_dynamic(int endpoint_fd, void* context) {
	const char* replacement = context;
	if (setenv("DARLING_LIFECYCLE_CONTROL_NONCE", replacement, 1) != 0)
		return -1;
	return accept_dynamic_listener(endpoint_fd, NULL);
}

static void verify_dynamic_transaction_faults(const char* prefix) {
	char parent[4096];
	char path[256] = {};
	if (snprintf(parent, sizeof(parent), "%s/private/var/tmp", prefix) >= (int)sizeof(parent))
		fail("dynamic parent path");
	size_t baseline = directory_entry_count(parent);
	if (darling_lifecycle_publish_and_activate_dynamic_endpoint(
			DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD,
			path,
			sizeof(path),
			&reject_activation,
			NULL) >= 0 || directory_entry_count(parent) != baseline)
		fail("dynamic activation rollback");

	char original_nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES + 1];
	const char* raw_nonce = getenv("DARLING_LIFECYCLE_CONTROL_NONCE");
	if (!raw_nonce || strlen(raw_nonce) != DARLING_LIFECYCLE_NONCE_HEX_BYTES)
		fail("dynamic transaction nonce");
	strcpy(original_nonce, raw_nonce);
	char wrong_nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES + 1];
	memset(wrong_nonce, '0', DARLING_LIFECYCLE_NONCE_HEX_BYTES);
	wrong_nonce[DARLING_LIFECYCLE_NONCE_HEX_BYTES] = 0;
	int listener = darling_lifecycle_publish_and_activate_dynamic_endpoint(
		DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD,
		path,
		sizeof(path),
		&mutate_nonce_and_accept_dynamic,
		wrong_nonce
	);
	int saved_errno = errno;
	if (setenv("DARLING_LIFECYCLE_CONTROL_NONCE", original_nonce, 1) != 0)
		fail("restore dynamic transaction nonce");
	errno = saved_errno;
	if (listener < 0)
		fail("dynamic pending transaction reread nonce");
	if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD) != 0)
		fail("retire dynamic nonce snapshot endpoint");
	close(listener);

	if (setenv("DARLING_LIFECYCLE_COHORT_TEST_FINAL_ACK_FAULT", "lost", 1) != 0)
		fail("set dynamic final ACK fault");
	listener = darling_lifecycle_publish_and_activate_dynamic_endpoint(
		DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD,
		path,
		sizeof(path),
		&accept_dynamic_listener,
		NULL
	);
	if (unsetenv("DARLING_LIFECYCLE_COHORT_TEST_FINAL_ACK_FAULT") != 0)
		fail("clear dynamic final ACK fault");
	if (listener < 0 ||
		darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD) >= 0)
		fail("dynamic lost ACK ownership");
	if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD) != 0)
		fail("retire dynamic lost ACK endpoint");
	close(listener);
	if (directory_entry_count(parent) != baseline)
		fail("dynamic transaction tail");
}

static void exercise_dynamic_listener(int listener, const char* prefix, const char* guest_path) {
	char host_path[4096];
	if (snprintf(host_path, sizeof(host_path), "%s%s", prefix, guest_path) >=
		(int)sizeof(host_path))
		fail("dynamic endpoint host path");
	int client = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (client < 0)
		fail("dynamic endpoint client socket");
	struct sockaddr_un address = {.sun_family = AF_UNIX};
	if (strlen(host_path) >= sizeof(address.sun_path))
		fail("dynamic endpoint sockaddr capacity");
	strcpy(address.sun_path, host_path);
	if (connect(client, (struct sockaddr*)&address, sizeof(address)) != 0)
		fail("dynamic endpoint connect");
	int accepted = accept4(listener, NULL, NULL, SOCK_CLOEXEC);
	if (accepted < 0)
		fail("dynamic endpoint accept");
	char sent = 'P';
	char received = 0;
	if (write(client, &sent, 1) != 1 || read(accepted, &received, 1) != 1 ||
		received != sent)
		fail("dynamic endpoint RPC");
	close(accepted);
	close(client);
}

static int per_user_launchd_client(const char* prefix) {
	const char* action = getenv("COHORT_HARNESS_PER_USER_ACTION");
	if (action && strcmp(action, "transaction-faults") == 0) {
		verify_dynamic_transaction_faults(prefix);
		return 0;
	}
	char guest_path[256] = {};
	int listener = darling_lifecycle_publish_and_activate_dynamic_endpoint(
		DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD,
		guest_path,
		sizeof(guest_path),
		&accept_dynamic_listener,
		NULL
	);
	static const char dynamic_prefix[] = "/private/var/tmp/launchd-";
	if (listener < 0 ||
		strncmp(guest_path, dynamic_prefix, sizeof(dynamic_prefix) - 1) != 0 ||
		!strstr(guest_path, "/sock"))
		fail("publish per-user launchd endpoint");
	exercise_dynamic_listener(listener, prefix, guest_path);

	if (action && strcmp(action, "hold") == 0) {
		const char* raw_ready = getenv("COHORT_HARNESS_READY_FD");
		int ready = raw_ready ? atoi(raw_ready) : -1;
		uint16_t length = (uint16_t)strlen(guest_path);
		if (ready < 0 || write(ready, &length, sizeof(length)) != sizeof(length) ||
			write(ready, guest_path, length) != length)
			fail("per-user hold readiness");
		for (;;)
			pause();
	}
	if (action && strcmp(action, "replacement") == 0) {
		char host_path[4096];
		char saved_path[4096];
		if (snprintf(host_path, sizeof(host_path), "%s%s", prefix, guest_path) >=
			(int)sizeof(host_path) ||
			snprintf(saved_path, sizeof(saved_path), "%s.saved", host_path) >=
			(int)sizeof(saved_path))
			fail("dynamic replacement path");
		if (rename(host_path, saved_path) != 0)
			fail("stage dynamic endpoint replacement");
		write_file(host_path, "replacement", 0600);
		if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD) == 0)
			fail("dynamic endpoint replacement accepted");
		char content[16] = {};
		int replacement = open(host_path, O_RDONLY | O_CLOEXEC);
		if (replacement < 0 || read(replacement, content, sizeof(content)) != 11 ||
			memcmp(content, "replacement", 11) != 0)
			fail("dynamic replacement not preserved");
		close(replacement);
		if (unlink(host_path) != 0 || rename(saved_path, host_path) != 0 ||
			darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD) != 0)
			fail("restore and retire dynamic endpoint");
		close(listener);
		return 0;
	}

	if (darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD) >= 0)
		fail("duplicate per-user launchd endpoint accepted");
	if (darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD) != 0)
		fail("retire per-user launchd endpoint");
	close(listener);
	char host_path[4096];
	if (snprintf(host_path, sizeof(host_path), "%s%s", prefix, guest_path) >=
		(int)sizeof(host_path))
		fail("retired dynamic endpoint path");
	expect_missing(host_path);
	char* separator = strrchr(host_path, '/');
	if (!separator)
		fail("dynamic directory path");
	*separator = 0;
	expect_missing(host_path);
	return 0;
}

static void run_per_user_fixture(
	const char* prefix,
	const char* program,
	const char* action
) {
	pid_t child = fork();
	if (child < 0)
		fail("fork per-user fixture");
	if (child == 0) {
		setenv("COHORT_HARNESS_PER_USER_PREFIX", prefix, 1);
		setenv("COHORT_HARNESS_PER_USER_ACTION", action, 1);
		execl(program, "/sbin/launchd", NULL);
		_exit(127);
	}
	int status = 0;
	if (waitpid(child, &status, 0) != child || !WIFEXITED(status) ||
		WEXITSTATUS(status) != 0)
		fail("per-user fixture failed");
}

static void verify_per_user_owner_death_restart(const char* prefix, const char* program) {
	int ready[2];
	if (pipe2(ready, O_CLOEXEC) != 0)
		fail("per-user readiness pipe");
	pid_t first = fork();
	if (first < 0)
		fail("fork per-user launchd");
	if (first == 0) {
		close(ready[0]);
		char ready_value[32];
		snprintf(ready_value, sizeof(ready_value), "%d", ready[1]);
		fcntl(ready[1], F_SETFD, 0);
		setenv("COHORT_HARNESS_PER_USER_PREFIX", prefix, 1);
		setenv("COHORT_HARNESS_PER_USER_ACTION", "hold", 1);
		setenv("COHORT_HARNESS_READY_FD", ready_value, 1);
		execl(program, "/sbin/launchd", NULL);
		_exit(127);
	}
	close(ready[1]);
	uint16_t old_length = 0;
	char old_guest_path[256] = {};
	if (read(ready[0], &old_length, sizeof(old_length)) != sizeof(old_length) ||
		old_length == 0 || old_length >= sizeof(old_guest_path) ||
		read(ready[0], old_guest_path, old_length) != old_length)
		fail("per-user endpoint readiness");
	close(ready[0]);
	if (kill(first, SIGKILL) != 0)
		fail("kill per-user launchd");
	int status = 0;
	if (waitpid(first, &status, 0) != first || !WIFSIGNALED(status) ||
		WTERMSIG(status) != SIGKILL)
		fail("wait killed per-user launchd");

	pid_t second = fork();
	if (second < 0)
		fail("fork restarted per-user launchd");
	if (second == 0) {
		setenv("COHORT_HARNESS_PER_USER_PREFIX", prefix, 1);
		setenv("COHORT_HARNESS_PER_USER_ACTION", "restart", 1);
		execl(program, "/sbin/launchd", NULL);
		_exit(127);
	}
	if (waitpid(second, &status, 0) != second || !WIFEXITED(status) ||
		WEXITSTATUS(status) != 0)
		fail("restarted per-user launchd");
	char old_host_path[4096];
	if (snprintf(old_host_path, sizeof(old_host_path), "%s%s", prefix, old_guest_path) >=
		(int)sizeof(old_host_path))
		fail("old per-user endpoint path");
	expect_missing(old_host_path);
	char* separator = strrchr(old_host_path, '/');
	if (!separator)
		fail("old per-user directory path");
	*separator = 0;
	expect_missing(old_host_path);
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
	if (errno != EEXIST)
		fail("typed endpoint-exists response errno");
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
	const char* per_user_prefix = getenv("COHORT_HARNESS_PER_USER_PREFIX");
	if (argc == 1 && per_user_prefix && strcmp(argv[0], "/sbin/launchd") == 0)
		return per_user_launchd_client(per_user_prefix);
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
	pthread_t churn[4];
	atomic_store(&churn_running, true);
	for (size_t index = 0; index < 4; ++index)
		if (pthread_create(&churn[index], NULL, allocator_churn, NULL) != 0)
			fail("start allocator churn");
	struct darling_lifecycle_cohort_bootstrap bootstrap = {};
	struct darling_lifecycle_cohort_controller* controller =
		start_controller(prefix, getppid(), &bootstrap);
	atomic_store(&churn_running, false);
	for (size_t index = 0; index < 4; ++index)
		if (pthread_join(churn[index], NULL) != 0)
			fail("join allocator churn");
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
	expect_unauthorized_child(
		"/proc/self/exe",
		"not-per-user-launchd",
		DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD
	);
	run_per_user_fixture(prefix, "/proc/self/exe", "replacement");
	run_per_user_fixture(prefix, "/proc/self/exe", "transaction-faults");
	verify_per_user_owner_death_restart(prefix, "/proc/self/exe");
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
	verify_sigkill_owner_forensic_preserve(prefix);

	printf("LIFECYCLE_COHORT_ROUTING_VALID endpoints=6 lease=exact-exclusive-flock replacement=preserved per_user_rpc=ready per_user_owner_restart=ready dynamic_cleanup=clean shellspawn_keepalive=ready activation_rollback=clean commit_ack_loss=retained nonce_snapshot=stable flood=bounded owner_group_sigkill=forensic-preserved scm_rights_leaks=0\n");
	return 0;
}

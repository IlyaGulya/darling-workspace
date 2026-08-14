#include <mach/mach.h>
#include <servers/bootstrap.h>

#include "bootstrap_priv.h"
#include "launch.h"
#include "launch_priv.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

extern kern_return_t _vprocmgr_getsocket(name_t socket_path);

#define SOCKET_PATH_CAPACITY 1024
#define TRIGGER_PATH "/private/var/tmp/.lifecycle-cohort-per-user-trigger"

static int wait_for_trigger(void) {
	struct stat status;

	for (unsigned int attempt = 0; attempt < 600; ++attempt) {
		if (lstat(TRIGGER_PATH, &status) == 0)
			return S_ISREG(status.st_mode) && status.st_nlink == 1 ? 0 : -1;
		if (errno != ENOENT)
			return -1;
		usleep(100000);
	}
	return -1;
}

static int find_per_user_socket(char path[SOCKET_PATH_CAPACITY]) {
	DIR* directory = opendir("/private/var/tmp");
	struct dirent* entry;
	int matches = 0;

	if (!directory)
		return -1;
	while ((entry = readdir(directory)) != NULL) {
		static const char prefix[] = "launchd-";
		const char* cursor;
		size_t name_length;
		struct stat status;

		name_length = strlen(entry->d_name);
		if (name_length <= sizeof(prefix) - 1 + 1 + 8 ||
			strncmp(entry->d_name, prefix, sizeof(prefix) - 1) != 0)
			continue;
		cursor = entry->d_name + sizeof(prefix) - 1;
		if (*cursor < '1' || *cursor > '9')
			continue;
		while (*cursor >= '0' && *cursor <= '9')
			++cursor;
		if (*cursor != '-' || strlen(cursor + 1) != 8)
			continue;
		++cursor;
		for (size_t index = 0; index < 8; ++index) {
			if (!((cursor[index] >= '0' && cursor[index] <= '9') ||
				(cursor[index] >= 'a' && cursor[index] <= 'f')))
				goto next;
		}
		if (cursor[8] != 0)
			continue;
		if (snprintf(path, SOCKET_PATH_CAPACITY, "/private/var/tmp/%s/sock", entry->d_name) >= SOCKET_PATH_CAPACITY)
			continue;
		if (lstat(path, &status) != 0 || !S_ISSOCK(status.st_mode))
			continue;
		++matches;
		continue;
next:
		continue;
	}
	closedir(directory);
	return matches == 1 ? 0 : -1;
}

static int prove_legacy_rpc(const char* socket_path, int* hold_fd) {
	launch_data_t message = launch_data_alloc(LAUNCH_DATA_DICTIONARY);
	launch_data_t values = launch_data_alloc(LAUNCH_DATA_DICTIONARY);
	launch_data_t marker = launch_data_new_string("READY");
	launch_data_t response;
	struct sockaddr_un address = {.sun_family = AF_UNIX};
	int result = -1;

	*hold_fd = -1;
	if (!message || !values || !marker || strlen(socket_path) >= sizeof(address.sun_path))
		goto out;
	strcpy(address.sun_path, socket_path);
	*hold_fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (*hold_fd < 0 || connect(*hold_fd, (struct sockaddr*)&address, sizeof(address)) != 0)
		goto out;
	launch_data_dict_insert(values, marker, "COHORT_PER_USER_RPC_MARKER");
	marker = NULL;
	launch_data_dict_insert(message, values, LAUNCH_KEY_SETUSERENVIRONMENT);
	values = NULL;
	if (setenv(LAUNCHD_SOCKET_ENV, socket_path, 1) != 0)
		goto out;
	response = launch_msg(message);
	if (response && launch_data_get_type(response) == LAUNCH_DATA_ERRNO &&
		launch_data_get_errno(response) == 0)
		result = 0;
	if (response)
		launch_data_free(response);
out:
	if (result != 0 && *hold_fd >= 0) {
		close(*hold_fd);
		*hold_fd = -1;
	}
	if (marker)
		launch_data_free(marker);
	if (values)
		launch_data_free(values);
	if (message)
		launch_data_free(message);
	return result;
}

int main(void) {
	mach_port_t current = MACH_PORT_NULL;
	mach_port_t root = MACH_PORT_NULL;
	mach_port_t per_user = MACH_PORT_NULL;
	name_t activation_path = {0};
	char socket_path[SOCKET_PATH_CAPACITY] = {0};
	int hold_fd = -1;
	kern_return_t result;

	if (wait_for_trigger() != 0) {
		fprintf(stderr, "per-user trigger rejected\n");
		return 1;
	}

	result = task_get_bootstrap_port(mach_task_self(), &current);

	if (result != KERN_SUCCESS || current == MACH_PORT_NULL) {
		fprintf(stderr, "task_get_bootstrap_port failed: %u\n", result);
		return 1;
	}
	result = bootstrap_get_root(current, &root);

	if (result != BOOTSTRAP_SUCCESS) {
		fprintf(stderr, "bootstrap_get_root failed: %u\n", result);
		return 1;
	}
	result = bootstrap_look_up_per_user(root, NULL, getuid(), &per_user);
	mach_port_deallocate(mach_task_self(), root);
	if (result != BOOTSTRAP_SUCCESS) {
		fprintf(stderr, "bootstrap_look_up_per_user failed: %u\n", result);
		return 1;
	}
	bootstrap_port = per_user;
	(void)_vprocmgr_getsocket(activation_path);
	if (find_per_user_socket(socket_path) != 0) {
		fprintf(stderr, "per-user socket discovery failed\n");
		return 1;
	}
	if (prove_legacy_rpc(socket_path, &hold_fd) != 0) {
		fprintf(stderr, "per-user launchd RPC failed\n");
		return 1;
	}
	printf("COHORT_PER_USER_RPC_OK %s\n", socket_path);
	fflush(stdout);
	sleep(300);
	close(hold_fd);
	mach_port_deallocate(mach_task_self(), per_user);
	return 0;
}

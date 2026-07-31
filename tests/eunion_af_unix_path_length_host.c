/*
 * Behavioral host fixture for the production BSD sockaddr translator.
 *
 * This includes the real userspace vchroot implementation so the test can
 * select an exact upper/lower root, then links the production network duct
 * source. No path-length algorithm is reimplemented here.
 */
#define TEST 1
#define _GNU_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define LINUX_EEXIST EEXIST
#define LINUX_EPERM EPERM
#define LINUX_AT_FDCWD AT_FDCWD
#define LINUX_ENOTDIR ENOTDIR
#define LINUX_EIO EIO
#define LINUX_EMFILE EMFILE

int eunion_test_perthread_wd = AT_FDCWD;
int dserver_rpc_vchroot_path(char* path, unsigned long size, uint64_t* token)
{
	(void)path;
	(void)size;
	(void)token;
	return -1;
}
void __simple_abort(void) { __builtin_trap(); }
int __simple_printf(const char* format, ...)
{
	(void)format;
	return 0;
}

#define main vchroot_unit_main
#include "vchroot_userspace.c"
#undef main

#include "eunion_sidecar_test_support.h"

#include <darling/emulation/xnu_syscall/bsd/helper/network/duct.h>

int errno_linux_to_bsd(int error)
{
	return error;
}

static void fail(const char* message)
{
	fprintf(stderr, "FAIL %s\n", message);
	exit(1);
}

static void require(int condition, const char* message)
{
	if (!condition)
		fail(message);
}

static void make_guest_path(char* path, size_t length, char fill)
{
	require(length >= 4 && length < sizeof(((struct sockaddr_fixup*)0)->sun_path),
		"requested guest path length is outside fixture bounds");
	path[0] = '/';
	path[1] = 't';
	path[2] = '/';
	memset(path + 3, fill, length - 3);
	path[length] = '\0';
}

static int translate(const char* guest, struct sockaddr_fixup* output)
{
	struct sockaddr_fixup input;
	memset(&input, 0, sizeof(input));
	input.bsd_length = (unsigned char)(
		offsetof(struct sockaddr_fixup, sun_path) + strlen(guest) + 1
	);
	input.bsd_family = PF_LOCAL;
	strcpy(input.sun_path, guest);
	memset(output, 0xa5, sizeof(*output));
	return sockaddr_fixup_from_bsd(output, &input, input.bsd_length);
}

static void bind_translated(const struct sockaddr_fixup* translated)
{
	struct sockaddr_un address;
	memset(&address, 0, sizeof(address));
	address.sun_family = AF_UNIX;
	strcpy(address.sun_path, translated->sun_path);
	int fd = socket(AF_UNIX, SOCK_STREAM, 0);
	require(fd >= 0, "host socket creation failed");
	int rc = bind(fd, (const struct sockaddr*)&address,
		(socklen_t)(offsetof(struct sockaddr_un, sun_path) +
			strlen(address.sun_path) + 1));
	int saved_errno = errno;
	close(fd);
	if (rc != 0) {
		errno = saved_errno;
		perror("bind");
		fail("translated host bind failed");
	}
}

int main(int argc, char** argv)
{
	require(argc == 2, "usage: fixture ROOT");
	char prefix[4096];
	char lower[4096];
	char sidecar[4096];
	require(snprintf(prefix, sizeof(prefix), "%s/prefix", argv[1]) <
		(int)sizeof(prefix), "prefix path overflow");
	require(snprintf(lower, sizeof(lower), "%s/prefix/libexec/darling", argv[1]) <
		(int)sizeof(lower), "lower path overflow");
	require(snprintf(sidecar, sizeof(sidecar), "%s%s", prefix,
		EUNION_SIDECAR_SUFFIX) < (int)sizeof(sidecar),
		"sidecar path overflow");
	require(mkdir(prefix, 0700) == 0 || errno == EEXIST, "create prefix");
	require(mkdir(sidecar, 0700) == 0, "create external sidecar");
	char upper_parent[4096];
	require(snprintf(upper_parent, sizeof(upper_parent), "%s/t", prefix) <
		(int)sizeof(upper_parent), "upper parent overflow");
	require(mkdir(upper_parent, 0700) == 0, "create upper parent");

	strcpy(prefix_path, prefix);
	prefix_path_len = (int)strlen(prefix);
	strcpy(libexec_path, lower);
	libexec_path_len = (int)strlen(lower);
	struct eunion_sidecar_runtime fixture_runtime = {
		.upper_root = prefix,
		.lower_root = lower,
		.sidecar_root = sidecar,
	};
	require(eunion_sidecar_test_write_prefix_state(&fixture_runtime) == 0,
		"write versioned prefix binding");
	require(eunion_sidecar_initialize(&fixture_runtime) == 0,
		"initialize sidecar state");
	int prefix_descriptor = open(prefix,
		O_PATH | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	require(prefix_descriptor >= 0, "open retained prefix capability");
	require(eunion_init_from_prefix(prefix_descriptor) == 0,
		"activate retained-FD E-UNION runtime");
	struct stat supplied_prefix;
	struct stat owned_prefix;
	require(fstat(prefix_descriptor, &supplied_prefix) == 0 &&
		fstat(eunion_anchored_runtime.upper_descriptor, &owned_prefix) == 0 &&
		supplied_prefix.st_dev == owned_prefix.st_dev &&
		supplied_prefix.st_ino == owned_prefix.st_ino,
		"runtime capability retains the validated prefix inode");
	require(close(prefix_descriptor) == 0,
		"caller releases supplied prefix descriptor");

	struct sockaddr_fixup output;
	char guest[sizeof(output.sun_path)];
	char expanded[4096];

	make_guest_path(guest, 16, 's');
	int result = translate(guest, &output);
	require(result >= 0, "short path translation failed");
	require(snprintf(expanded, sizeof(expanded), "%s%s", prefix, guest) <
		(int)sizeof(expanded), "short expanded path overflow");
	require(strcmp(output.sun_path, expanded) == 0,
		"short path translation was not exact");
	bind_translated(&output);
	struct stat status;
	require(lstat(expanded, &status) == 0 && S_ISSOCK(status.st_mode),
		"short socket missing at exact expanded path");
	require(unlink(expanded) == 0, "remove short socket");

	size_t boundary_guest_length =
		sizeof(output.sun_path) - 1 - strlen(prefix);
	make_guest_path(guest, boundary_guest_length, 'b');
	result = translate(guest, &output);
	require(result >= 0, "boundary path translation failed");
	require(strlen(output.sun_path) == sizeof(output.sun_path) - 1,
		"boundary path did not retain exact maximum length");
	bind_translated(&output);
	require(lstat(output.sun_path, &status) == 0 && S_ISSOCK(status.st_mode),
		"boundary socket missing at exact expanded path");
	require(unlink(output.sun_path) == 0, "remove boundary socket");

	make_guest_path(guest, boundary_guest_length + 1, 'l');
	require(snprintf(expanded, sizeof(expanded), "%s%s", prefix, guest) <
		(int)sizeof(expanded), "overlong expanded path overflow");
	char truncated[sizeof(output.sun_path)];
	memcpy(truncated, expanded, sizeof(truncated) - 1);
	truncated[sizeof(truncated) - 1] = '\0';
	unlink(truncated);
	result = translate(guest, &output);
	if (result != -ENAMETOOLONG)
		fprintf(stderr, "overlong translation result=%d output=%s\n",
			result, output.sun_path);
	require(result == -ENAMETOOLONG,
		"overlong path did not return deterministic ENAMETOOLONG");
	require(lstat(truncated, &status) != 0 && errno == ENOENT,
		"overlong translation created a truncated socket");

	int owned_prefix_descriptor = eunion_anchored_runtime.upper_descriptor;
	require(eunion_sidecar_runtime_release(&eunion_anchored_runtime) == 0,
		"release runtime-owned prefix capability");
	errno = 0;
	require(fcntl(owned_prefix_descriptor, F_GETFD) == -1 && errno == EBADF,
		"released runtime descriptor is closed");

	printf("EUNION_AF_UNIX_PATH_LENGTH_OK short=16 boundary=%zu overlong=%zu\n",
		boundary_guest_length, boundary_guest_length + 1);
	return 0;
}

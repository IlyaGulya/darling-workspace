// Guest-side runtime probe for the FD-less vchroot path RPC.
//
// A: a valid directory descriptor must be accepted, which under the ported
//    build means the guest snapshots /proc/self/fd once and the server accepts
//    the path.
// B: the same descriptor after close() must be rejected in the guest, before any
//    RPC is sent, and must not disturb the vchroot state published by A. The leg
//    classifies the rejection site so a runtime that only rejects it later, in
//    the descriptor transfer or in the server, fails the probe instead of
//    satisfying it with a negative return.
// C: the valid call must transport no descriptor. That is observed from the host
//    (strace over the launcher tree), not from the guest, so the probe publishes
//    descriptor-transport windows around the calls the runner counts.
//
// The probe calls the guest entry point directly instead of the vchroot helper
// so a closed descriptor is expressible.

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

typedef int (*vchroot_fn)(int dfd);

// One descriptor-transport window boundary. The printed marker is the guest-side
// declaration that the operation ran; the readlink is what makes the boundary
// visible in the per-process host trace, because the guest resolves this path
// with a raw Linux readlinkat in the probe's own process. Both are emitted so a
// window cannot be satisfied by an unrelated instruction stream.
static void descriptor_window(const char *id, const char *phase, const char *label) {
	char path[256];
	char buffer[64];
	ssize_t ignored;

	snprintf(path, sizeof(path), "/tmp/darling-descriptor-window-%s-%s", id, phase);
	ignored = readlink(path, buffer, sizeof(buffer));
	(void)ignored;
	printf("DSERVER_DESCRIPTOR_WINDOW %s %s\n", label, id);
	fflush(stdout);
}

int main(void) {
	// The guest entry point lives in libsystem_kernel and is not part of the
	// CommandLineTools SDK stubs, so resolve it through the loaded image.
	vchroot_fn vchroot = (vchroot_fn)dlsym(RTLD_DEFAULT, "__darling_vchroot");
	if (vchroot == NULL) {
		printf("VCHROOT_SYMBOL_MISSING %s\n", dlerror());
		return 9;
	}

	int fd = open("/", O_RDONLY | O_DIRECTORY);
	if (fd < 0) {
		printf("VCHROOT_OPEN_ROOT_FAILED errno=%d\n", errno);
		return 10;
	}
	printf("VCHROOT_ROOT_FD=%d\n", fd);

	// C's negative control: console_open is an unaffected FD-bearing call -- the
	// server replies with the console socket -- so this same run must show a
	// descriptor for it. Without that, a zero count on the vchroot window below
	// could not distinguish "transports no descriptor" from "the observation sees
	// no descriptors at all".
	descriptor_window("console-open-control", "begin", "BEGIN");
	errno = 0;
	int console = open("/dev/console", O_RDONLY);
	int console_errno = errno;
	descriptor_window("console-open-control", "end", "END");
	printf("VCHROOT_FDLESS_CONTROL_CONSOLE fd=%d errno=%d\n", console, console_errno);
	if (console < 0) {
		printf("VCHROOT_FDLESS_CONTROL_CONSOLE_FAILED\n");
		return 16;
	}
	close(console);

	descriptor_window("vchroot-fdless-valid", "begin", "BEGIN");
	errno = 0;
	int rv = vchroot(fd);
	int saved = errno;
	descriptor_window("vchroot-fdless-valid", "end", "END");
	printf("VCHROOT_VALID_FD rv=%d errno=%d\n", rv, saved);
	if (rv < 0) {
		printf("VCHROOT_FDLESS_VALID_FAILED\n");
		return 11;
	}
	printf("VCHROOT_FDLESS_VALID_OK\n");

	if (close(fd) != 0) {
		printf("VCHROOT_CLOSE_FAILED errno=%d\n", errno);
		return 12;
	}

	errno = 0;
	rv = vchroot(fd);
	saved = errno;
	printf("VCHROOT_CLOSED_FD rv=%d errno=%d errname=%s\n", rv, saved, strerror(saved));
	if (rv >= 0) {
		printf("VCHROOT_FDLESS_CLOSED_ACCEPTED\n");
		return 13;
	}
	printf("VCHROOT_FDLESS_CLOSED_REJECTED\n");

	// B is only a control if the rejection happens in the right place. The port
	// rejects the closed descriptor locally, from the single readlink of
	// /proc/self/fd/N, so the result is ENOENT and no RPC is attempted. The
	// earlier code sent the descriptor first, so the transfer or the server
	// rejected it with EBADF instead.
	if (rv != -ENOENT) {
		printf("VCHROOT_CLOSED_REJECTION_SITE=other rv=%d\n", rv);
		return 15;
	}
	printf("VCHROOT_CLOSED_REJECTION_SITE=local-snapshot\n");

	// A's published state must survive the rejected call: expanding a guest path
	// still resolves under the vchroot set by the valid descriptor.
	int again = open("/", O_RDONLY | O_DIRECTORY);
	printf("VCHROOT_REOPEN_ROOT_FD=%d\n", again);
	if (again < 0) {
		printf("VCHROOT_STATE_CHANGED\n");
		return 14;
	}
	close(again);
	printf("VCHROOT_FDLESS_PROBE_OK\n");
	return 0;
}

#define _GNU_SOURCE
#define TEST 1
#ifndef DARLING_GUEST_NAMESPACE_AUTHORITY_SOURCE
#define DARLING_GUEST_NAMESPACE_AUTHORITY_SOURCE "../../../xnu/darling/src/libsystem_kernel/emulation/src/linux_premigration/guest_namespace_authority.c"
#endif
#include DARLING_GUEST_NAMESPACE_AUTHORITY_SOURCE

#include <errno.h>
#include <ftw.h>
#include <limits.h>
#include <pthread.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/syscall.h>

static void require(int condition, const char* message) {
	if (!condition) { perror(message); exit(1); }
}

static int remove_owned(const char* path, const struct stat* st, int type, struct FTW* ftw) {
	(void)st; (void)type; (void)ftw;
	return remove(path);
}

static void* initialize_concurrently(void* unused) {
	(void)unused;
	return (void*)(intptr_t)darling_guest_namespace_initialize();
}

static void verify_scm_rights_cloexec(void) {
	int sockets[2];
	require(socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, sockets) == 0,
		"SCM_RIGHTS socketpair");
	int source = memfd_create("scm-cloexec", 0);
	char payload = 'x';
	struct iovec send_iov = {.iov_base = &payload, .iov_len = 1};
	char control[CMSG_SPACE(sizeof(int))] = {0};
	struct msghdr send_message = {.msg_iov = &send_iov, .msg_iovlen = 1,
		.msg_control = control, .msg_controllen = sizeof(control)};
	struct cmsghdr* header = CMSG_FIRSTHDR(&send_message);
	header->cmsg_level = SOL_SOCKET; header->cmsg_type = SCM_RIGHTS;
	header->cmsg_len = CMSG_LEN(sizeof(int));
	memcpy(CMSG_DATA(header), &source, sizeof(source));
	require(sendmsg(sockets[0], &send_message, MSG_NOSIGNAL) == 1, "SCM_RIGHTS send");
	char received_payload = 0;
	struct iovec receive_iov = {.iov_base = &received_payload, .iov_len = 1};
	char receive_control[CMSG_SPACE(sizeof(int))] = {0};
	struct msghdr receive_message = {.msg_iov = &receive_iov, .msg_iovlen = 1,
		.msg_control = receive_control, .msg_controllen = sizeof(receive_control)};
	require(recvmsg(sockets[1], &receive_message, MSG_CMSG_CLOEXEC) == 1,
		"SCM_RIGHTS receive CLOEXEC");
	int received = -1;
	memcpy(&received, CMSG_DATA(CMSG_FIRSTHDR(&receive_message)), sizeof(received));
	require(received >= 0 && (fcntl(received, F_GETFD) & FD_CLOEXEC) != 0,
		"SCM_RIGHTS delivered FD_CLOEXEC");
	close(received); close(source); close(sockets[0]); close(sockets[1]);
}

static unsigned int transaction_calls;
static unsigned int replay_calls;
static long transaction_probe(uint32_t operation, const char* source,
	const char* destination, int flags, uint32_t mode) {
	++transaction_calls;
	require(source && source[0] == '/', "transaction source");
	if (operation == 1) {
		require(strcmp(source, "/pilot/file") == 0 && destination == NULL &&
			flags == (O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC) && mode == 06705,
			"create RPC arguments");
		return 77;
	}
	if (operation == 2) {
		require(strcmp(source, "/pilot") == 0 && destination == NULL && mode == 07705,
			"mkdir RPC arguments");
		return 0;
	}
	if (operation == 3) {
		require(strcmp(source, "/pilot/file") == 0 && destination == NULL,
			"unlink RPC arguments");
		return 0;
	}
	if (operation == 4) {
		require(strcmp(source, "/pilot/file") == 0 &&
			strcmp(destination, "/pilot/renamed") == 0, "rename RPC arguments");
		return 0;
	}
	return -95;
}

static long replay_probe(uint32_t operation, const char* source,
	const char* destination, int flags, uint32_t mode) {
	(void)destination; (void)flags; (void)mode;
	require(operation == 3 && strcmp(source, "/pilot/retry") == 0,
		"replay RPC arguments");
	return ++replay_calls == 1 ? -32 : 0;
}

int main(void) {
	verify_scm_rights_cloexec();
	char root[] = "/tmp/darling-gna-contract.XXXXXX";
	require(mkdtemp(root) != NULL, "mkdtemp");
	char parent[PATH_MAX], replacement[PATH_MAX], saved[PATH_MAX], lockpath[PATH_MAX];
	snprintf(parent, sizeof(parent), "%s/parent", root);
	snprintf(replacement, sizeof(replacement), "%s/parent/replacement", root);
	snprintf(saved, sizeof(saved), "%s/parent.retained", root);
	snprintf(lockpath, sizeof(lockpath), "%s/.lifecycle.lock", root);
	require(mkdir(parent, 0700) == 0, "mkdir parent");
	int lock = open(lockpath, O_RDWR | O_CREAT | O_EXCL, 0600);
	int prefix = open(root, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
	int gate = memfd_create("gate", MFD_CLOEXEC);
	int lease = memfd_create("lease", MFD_CLOEXEC);
	int controller_pidfd = (int)syscall(SYS_pidfd_open, getpid(), 0);
	require(lock >= 0 && prefix >= 0 && gate >= 0 && lease >= 0 && controller_pidfd >= 0,
		"capabilities");
	require(flock(lock, LOCK_EX | LOCK_NB) == 0, "lock");
	require(ftruncate(lease, sizeof(struct lease_page)) == 0, "lease size");
	struct lease_page* page = mmap(NULL, sizeof(*page), PROT_READ | PROT_WRITE,
		MAP_SHARED, lease, 0);
	require(page != MAP_FAILED, "lease map");
	struct stat st;
	memcpy(page->magic, "DLGNSA2", 8); page->version = 2; atomic_store(&page->state, ACTIVE);
	page->generation = 44; fstat(prefix, &st); page->prefix = (struct cap_identity){st.st_dev, st.st_ino};
	fstat(lock, &st); page->lock = (struct cap_identity){st.st_dev, st.st_ino};
	fstat(gate, &st); page->gate = (struct cap_identity){st.st_dev, st.st_ino};
	page->controller_pid = getpid();
	darling_guest_namespace_test_set_capability(0, lease);
	darling_guest_namespace_test_set_capability(1, gate);
	darling_guest_namespace_test_set_capability(2, prefix);
	darling_guest_namespace_test_set_capability(3, lock);
	darling_guest_namespace_test_set_capability(4, controller_pidfd);
	pthread_t initializers[16];
	for (unsigned int index = 0; index < 16; ++index)
		require(pthread_create(&initializers[index], NULL, initialize_concurrently, NULL) == 0,
			"concurrent initialize");
	for (unsigned int index = 0; index < 16; ++index) {
		void* result = NULL;
		require(pthread_join(initializers[index], &result) == 0 &&
			(int)(intptr_t)result == DARLING_GUEST_NAMESPACE_REQUIRED,
			"concurrent initialization never observes OFF");
	}

	/* Neither unsetenv nor an empty exec environment participates in activation. */
	unsetenv("DARLING_LIFECYCLE_COHORT_V1");
	require(darling_guest_namespace_state() == DARLING_GUEST_NAMESPACE_REQUIRED,
		"environment independent");
	darling_guest_namespace_test_set_transaction(transaction_probe);
	mode_t prior_umask = umask(0072);
	require(darling_guest_namespace_mkdir("/pilot", 07777) == 0,
		"mkdir routed through transaction service");
	require(mkdirat(prefix, "pilot", 0700) == 0, "fixture upper pilot");
	require(darling_guest_namespace_create("/pilot/file",
		O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 06777) == 77,
		"create FD routed through transaction service");
	umask(prior_umask);
	int created = openat(prefix, "pilot/file", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
	require(created >= 0 && write(created, "payload", 7) == 7, "fixture upper create");
	close(created);
	require(darling_guest_namespace_rename("/pilot/file", "/pilot/renamed") == 0,
		"rename routed through transaction service");
	require(darling_guest_namespace_unlink("/pilot/file", 0) == 0,
		"unlink routed through transaction service");
	require(transaction_calls == 4, "exact transaction RPC count");
	darling_guest_namespace_test_set_transaction(replay_probe);
	require(darling_guest_namespace_unlink("/pilot/retry", 0) == 0 && replay_calls == 2,
		"lost response replays one exact transaction");
	char oversized[1030];
	oversized[0] = '/';
	memset(oversized + 1, 'x', sizeof(oversized) - 2);
	oversized[sizeof(oversized) - 1] = 0;
	require(darling_guest_namespace_unlink(oversized, 0) == -36,
		"oversized transaction path rejected before RPC");
	char pilot_file[PATH_MAX];
	snprintf(pilot_file, sizeof(pilot_file), "%s/pilot/file", root);
	require(access(pilot_file, F_OK) == 0, "rejected mutations preserve namespace");
	darling_guest_namespace_test_set_transaction(NULL);
	require(darling_guest_namespace_unlink("/pilot/absent", 0) == -95,
		"unavailable service fails closed");

	struct darling_guest_namespace_mutation mutation;
	struct darling_guest_namespace_resolved resolved;
	require(darling_guest_namespace_begin(&mutation) == 1, "authorize");
	require(darling_guest_namespace_resolve(&mutation, "/parent/created", &resolved) == 0,
		"resolve retained parent");
	require(rename(parent, saved) == 0 && mkdir(parent, 0700) == 0, "replace parent");
	int replacement_fd = open(replacement, O_WRONLY | O_CREAT | O_EXCL, 0600);
	require(replacement_fd >= 0 && write(replacement_fd, "replacement", 11) == 11,
		"write replacement");
	close(replacement_fd);
	require(mkdirat(resolved.parent_fd, resolved.leaf, 0700) == 0, "fd-relative mutation");
	char replacement_bytes[12] = {};
	replacement_fd = open(replacement, O_RDONLY | O_CLOEXEC);
	require(replacement_fd >= 0 && read(replacement_fd, replacement_bytes, 11) == 11 &&
		memcmp(replacement_bytes, "replacement", 11) == 0,
		"replacement byte identity preserved");
	close(replacement_fd);
	char retained_created[PATH_MAX + 16];
	snprintf(retained_created, sizeof(retained_created), "%s/created", saved);
	require(access(retained_created, F_OK) == 0, "retained parent selected");
	darling_guest_namespace_resolved_close(&resolved);
	darling_guest_namespace_end(&mutation);
	require(darling_guest_namespace_begin(&mutation) == 1, "authorize before lock swap");
	char oldlock[PATH_MAX];
	snprintf(oldlock, sizeof(oldlock), "%s/.lifecycle.lock.old", root);
	require(rename(lockpath, oldlock) == 0, "replace named lock");
	int forged_lock = open(lockpath, O_RDWR | O_CREAT | O_EXCL, 0600);
	require(forged_lock >= 0, "create replacement lock"); close(forged_lock);
	require(!mutation_still_valid(&mutation), "post-authorization split lock rejected");
	darling_guest_namespace_end(&mutation);
	require(darling_guest_namespace_begin(&mutation) < 0, "split lock remains rejected");

	atomic_store(&page->state, 2);
	require(darling_guest_namespace_begin(&mutation) < 0, "revoked authority");
	darling_guest_namespace_test_reset();
	close(lease); close(gate); close(prefix); close(lock); close(controller_pidfd);
	require(nftw(root, remove_owned, 16, FTW_DEPTH | FTW_PHYS) == 0, "owned cleanup");
	return 0;
}

#ifndef DARLING_LIFECYCLE_COHORT_H
#define DARLING_LIFECYCLE_COHORT_H

#include <stdint.h>
#include <stdbool.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DARLING_LIFECYCLE_CONTROL_NAME_CAPACITY 80
#define DARLING_LIFECYCLE_COHORT_ABI_VERSION 6
#define DARLING_LIFECYCLE_NONCE_HEX_BYTES 64
#define DARLING_GUEST_NAMESPACE_BOOTSTRAP_FD 1023
#define DARLING_GUEST_NAMESPACE_VCHROOT_FD 1010
#define DARLING_GUEST_NAMESPACE_LOWER_FD 1013
#define DARLING_GUEST_NAMESPACE_PREFIX_FD 1010
#define DARLING_GUEST_NAMESPACE_AUTHORITY_DESCRIPTOR_COUNT 6
#define DARLING_GUEST_NAMESPACE_DESCRIPTOR_COUNT 6
#define DARLING_LIFECYCLE_FINISH_OK 0
#define DARLING_LIFECYCLE_FINISH_ERROR -1
#define DARLING_LIFECYCLE_FINISH_DRAIN_PENDING 1
#define DARLING_LIFECYCLE_FINISH_ABANDONED 2
#define DARLING_LIFECYCLE_ABANDON_PENDING 3
#define DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING 4
#define DARLING_LIFECYCLE_FINISH_RECOVERY_PENDING 5

struct darling_guest_namespace_identity {
	uint64_t device;
	uint64_t inode;
};

struct darling_guest_namespace_bootstrap {
	uint8_t magic[8];
	uint32_t version;
	uint32_t required;
	uint64_t generation;
	struct darling_guest_namespace_identity prefix;
	struct darling_guest_namespace_identity lock;
	struct darling_guest_namespace_identity gate;
	struct darling_guest_namespace_identity lower;
	uint32_t descriptor_count;
	uint32_t reserved;
};

struct darling_lifecycle_cohort_bootstrap {
	int darlingserver_fd;
	int dserver_log_fd;
	uint16_t control_name_len;
	uint16_t reserved;
	uint8_t control_name[DARLING_LIFECYCLE_CONTROL_NAME_CAPACITY];
	uint8_t nonce_hex[DARLING_LIFECYCLE_NONCE_HEX_BYTES];
};

struct darling_lifecycle_cohort_controller;

#define DARLING_GUEST_TRANSACTION_PATH_CAPACITY 1024
#define DARLING_GUEST_TRANSACTION_CREATE 1
#define DARLING_GUEST_TRANSACTION_MKDIR 2
#define DARLING_GUEST_TRANSACTION_UNLINK 3
#define DARLING_GUEST_TRANSACTION_RENAME 4

struct darling_guest_namespace_transaction {
	uint8_t transaction_id[16];
	uint32_t operation;
	int32_t flags;
	uint32_t mode;
	uint16_t source_length;
	uint16_t destination_length;
	uint8_t source[DARLING_GUEST_TRANSACTION_PATH_CAPACITY];
	uint8_t destination[DARLING_GUEST_TRANSACTION_PATH_CAPACITY];
};

struct darling_guest_namespace_transaction_result {
	int32_t result;
	uint32_t disposition;
	uint64_t device;
	uint64_t inode;
	int32_t created_fd;
	uint32_t reserved;
};

struct darling_lifecycle_cohort_controller* darling_lifecycle_cohort_start(
	int prefix_fd,
	int deployment_prefix_fd,
	const char* prefix_argument,
	pid_t init_pid,
	struct darling_lifecycle_cohort_bootstrap* output
);

int darling_lifecycle_cohort_finish(
	struct darling_lifecycle_cohort_controller* controller
);

/* Return the live Rust controller worker PID that a subreaper must retain
 * until cleanup is committed.  A non-positive result is fail-closed. */
pid_t darling_lifecycle_cohort_worker_pid(
	struct darling_lifecycle_cohort_controller* controller
);

bool darling_lifecycle_cohort_admission_open(
	struct darling_lifecycle_cohort_controller* controller
);

/* Rotate/create the generation-owned public var/run directory. */
int darling_lifecycle_cohort_prepare_var_run(
	struct darling_lifecycle_cohort_controller* controller
);

#define DARLING_LIFECYCLE_USER_HOME_SCHEMA_V1 1u
#define DARLING_LIFECYCLE_USER_HOME_LINK_COUNT 8u
#define DARLING_LIFECYCLE_USER_HOME_SHARED_MODE 0777u
#define DARLING_LIFECYCLE_USER_HOME_USER_MODE 0755u
struct darling_lifecycle_user_home_plan {
	uint32_t schema_version;
	uid_t owner_uid;
	gid_t owner_gid;
	uint32_t shared_mode;
	uint32_t user_mode;
	uint32_t reserved;
	const char* login;
	/* LinuxHome followed by Desktop, Downloads, Public, Documents, Music,
	 * Pictures and Movies. Optional XDG targets are NULL. */
	const char* targets[DARLING_LIFECYCLE_USER_HOME_LINK_COUNT];
};

int darling_lifecycle_cohort_prepare_user_home(
	struct darling_lifecycle_cohort_controller* controller,
	const struct darling_lifecycle_user_home_plan* plan
);
/* On DARLING_LIFECYCLE_FINISH_DRAIN_PENDING the same controller pointer remains
 * owned by the caller and must be retried; worker cleanup has not started. */
/* On DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING CLEANUP was irreversibly
 * committed. The same pointer remains owned only to collect ACK/exit/reap;
 * abandon and forensic-preserve transitions are forbidden. */
/* RECOVERY_PENDING retains exact recovery FDs in the same pointer. It may
 * only be retried or handed to a durable recovery owner; abandon is refused. */

int darling_lifecycle_cohort_abandon(
	struct darling_lifecycle_cohort_controller* controller
);
/* DARLING_LIFECYCLE_ABANDON_PENDING preserves the exact pointer and worker for
 * retry; zero consumes it after confirmed exit/reap. */

int darling_lifecycle_cohort_send_guest_namespace_bootstrap(
	struct darling_lifecycle_cohort_controller* controller,
	int socket_fd
);

int darling_lifecycle_guest_namespace_configure(
	struct darling_lifecycle_cohort_controller* controller
);

/* Return a caller-owned duplicate of the authenticated retained lower root. */
int darling_lifecycle_guest_namespace_directory(
	struct darling_lifecycle_cohort_controller* controller
);

int darling_lifecycle_guest_namespace_transaction(
	struct darling_lifecycle_cohort_controller* controller,
	const struct darling_guest_namespace_transaction* request,
	struct darling_guest_namespace_transaction_result* result
);

#ifdef __cplusplus
}
#endif

#endif

#ifndef DARLING_LIFECYCLE_COHORT_H
#define DARLING_LIFECYCLE_COHORT_H

#include <stdint.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DARLING_LIFECYCLE_CONTROL_NAME_CAPACITY 80
#define DARLING_LIFECYCLE_NONCE_HEX_BYTES 64
#define DARLING_GUEST_NAMESPACE_BOOTSTRAP_FD 1023
#define DARLING_GUEST_NAMESPACE_DESCRIPTOR_COUNT 5
#define DARLING_LIFECYCLE_FINISH_OK 0
#define DARLING_LIFECYCLE_FINISH_ERROR -1
#define DARLING_LIFECYCLE_FINISH_DRAIN_PENDING 1
#define DARLING_LIFECYCLE_FINISH_ABANDONED 2
#define DARLING_LIFECYCLE_ABANDON_PENDING 3
#define DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING 4

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

struct darling_lifecycle_cohort_controller* darling_lifecycle_cohort_start(
	int prefix_fd,
	const char* prefix_argument,
	pid_t init_pid,
	struct darling_lifecycle_cohort_bootstrap* output
);

int darling_lifecycle_cohort_finish(
	struct darling_lifecycle_cohort_controller* controller
);
/* On DARLING_LIFECYCLE_FINISH_DRAIN_PENDING the same controller pointer remains
 * owned by the caller and must be retried; worker cleanup has not started. */
/* On DARLING_LIFECYCLE_FINISH_CLEANUP_PENDING CLEANUP was irreversibly
 * committed. The same pointer remains owned only to collect ACK/exit/reap;
 * abandon and forensic-preserve transitions are forbidden. */

int darling_lifecycle_cohort_abandon(
	struct darling_lifecycle_cohort_controller* controller
);
/* DARLING_LIFECYCLE_ABANDON_PENDING preserves the exact pointer and worker for
 * retry; zero consumes it after confirmed exit/reap. */

int darling_lifecycle_cohort_send_guest_namespace_bootstrap(
	struct darling_lifecycle_cohort_controller* controller,
	int socket_fd
);

#ifdef __cplusplus
}
#endif

#endif

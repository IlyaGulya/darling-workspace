#ifndef DARLING_LIFECYCLE_COHORT_H
#define DARLING_LIFECYCLE_COHORT_H

#include <stdint.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DARLING_LIFECYCLE_CONTROL_NAME_CAPACITY 80
#define DARLING_LIFECYCLE_NONCE_HEX_BYTES 64

struct darling_lifecycle_cohort_bootstrap {
	int darlingserver_fd;
	uint16_t control_name_len;
	uint16_t reserved;
	uint8_t control_name[DARLING_LIFECYCLE_CONTROL_NAME_CAPACITY];
	uint8_t nonce_hex[DARLING_LIFECYCLE_NONCE_HEX_BYTES];
};

struct darling_lifecycle_cohort_controller;

struct darling_lifecycle_cohort_controller* darling_lifecycle_cohort_start(
	const char* prefix,
	pid_t init_pid,
	struct darling_lifecycle_cohort_bootstrap* output
);

int darling_lifecycle_cohort_finish(
	struct darling_lifecycle_cohort_controller* controller
);

#ifdef __cplusplus
}
#endif

#endif

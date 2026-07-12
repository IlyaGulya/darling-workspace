#include <errno.h>
#include <stdint.h>
#include <stdio.h>

#define PTHRW_INC (1u << 8)

extern uint32_t __psynch_cvwait(void *cv, uint64_t cvlsgen, uint32_t cvugen,
		void *mutex, uint64_t mugen, uint32_t flags, int64_t sec,
		uint32_t nsec);

int
main(void)
{
	uint32_t cv = 0;
	const uint64_t balanced = ((uint64_t)PTHRW_INC << 32) | PTHRW_INC;

	errno = 0;
	uint32_t result = __psynch_cvwait(&cv, balanced, 0, NULL, 0, 0, 0, 1000000);
	int error = errno & 0xff;

	if (result == (uint32_t)-1 && error == ETIMEDOUT) {
		puts("PSYNCH_CVWAIT_BALANCED_GUEST_OK");
		return 0;
	}
	if (result == (uint32_t)-1 && error == EINVAL) {
		puts("PSYNCH_CVWAIT_BALANCED_OLD_EQUAL_REJECTED errno=22");
		return 1;
	}

	fprintf(stderr, "unexpected balanced cvwait result=%u errno=%d\n", result,
	    error);
	return 2;
}

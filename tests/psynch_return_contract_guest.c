#include <errno.h>
#include <stdint.h>
#include <stdio.h>

#define PTHRW_COUNT_SHIFT 8
#define PTHRW_INC (1u << PTHRW_COUNT_SHIFT)

extern uint32_t __psynch_cvwait(void *cv, uint64_t cvlsgen, uint32_t cvugen,
		void *mutex, uint64_t mugen, uint32_t flags, int64_t sec,
		uint32_t nsec);

int
main(void)
{
	uint32_t cv_word = 0;
	const uint64_t cvlsgen = PTHRW_INC;

	errno = 0;
	uint32_t rc = __psynch_cvwait(&cv_word, cvlsgen, 0, NULL, 0, 0, 0, 1000000);
	int saved_errno = errno;

	if (rc == (uint32_t)-1 && (saved_errno & 0xff) == ETIMEDOUT) {
		printf("PSYNCH_RETURN_CONTRACT_GUEST_OK errno=%d\n", saved_errno);
		return 0;
	}

	if (rc != (uint32_t)-1 && saved_errno == 0 && (rc & 0xff) == ETIMEDOUT) {
		printf("PSYNCH_RETURN_CONTRACT_GUEST_OLD_POSITIVE_RETURN rc=%u\n", rc);
		return 1;
	}

	fprintf(stderr, "unexpected __psynch_cvwait result rc=%u errno=%d\n",
			rc, saved_errno);
	return 2;
}

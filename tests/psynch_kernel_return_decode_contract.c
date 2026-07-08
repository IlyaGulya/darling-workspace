#include <errno.h>
#include <stdint.h>
#include <stdio.h>

int *_pthread_errno_address_direct(void);

#include "inline_internal.h"

int test_errno;

int*
_pthread_errno_address_direct(void)
{
	return &test_errno;
}

static int expect_u32(const char* label, uint32_t got, uint32_t want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got 0x%x, want 0x%x\n", label, got, want);
	return 1;
}

int main(void)
{
	int failed = 0;

	test_errno = EINTR;
	_pthread_psynch_kernel_return_t decoded =
		_pthread_psynch_kernel_return_decode((uint32_t)-1);
	failed |= expect_u32("-1 raw_error", decoded.raw_error, EINTR);
	failed |= expect_u32("-1 base_error", decoded.base_error, EINTR);
	failed |= expect_u32("-1 status_bits", decoded.status_bits, 0);
	failed |= expect_u32("-1 updateval", decoded.updateval, 0);

	decoded = _pthread_psynch_kernel_return_decode((uint32_t)-(int32_t)(ETIMEDOUT | 0x300));
	failed |= expect_u32("augmented raw_error", decoded.raw_error, ETIMEDOUT | 0x300);
	failed |= expect_u32("augmented base_error", decoded.base_error, ETIMEDOUT);
	failed |= expect_u32("augmented status_bits", decoded.status_bits, 0x300);
	failed |= expect_u32("augmented updateval", decoded.updateval, 0);

	decoded = _pthread_psynch_kernel_return_decode(0x12345678u);
	failed |= expect_u32("success raw_error", decoded.raw_error, 0);
	failed |= expect_u32("success base_error", decoded.base_error, 0);
	failed |= expect_u32("success status_bits", decoded.status_bits, 0);
	failed |= expect_u32("success updateval", decoded.updateval, 0x12345678u);

	return failed ? 1 : 0;
}

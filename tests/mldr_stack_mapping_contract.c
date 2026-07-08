#include <errno.h>
#include <stdio.h>
#include <sys/mman.h>

#include "src/startup/mldr/stack_mapping.h"

struct map_call {
	void* addr;
	size_t length;
	int prot;
	int flags;
	int fd;
	off_t offset;
};

static struct map_call calls[4];
static int call_count;
static void* results[4];
static int errnos[4];

static void reset_fake(void)
{
	call_count = 0;
	for (int i = 0; i < 4; ++i) {
		results[i] = MAP_FAILED;
		errnos[i] = 0;
		calls[i] = (struct map_call){0};
	}
}

static void* fake_map(void* addr, size_t length, int prot, int flags, int fd, off_t offset)
{
	int idx = call_count++;
	if (idx >= 4) {
		errno = E2BIG;
		return MAP_FAILED;
	}
	calls[idx] = (struct map_call){
		.addr = addr,
		.length = length,
		.prot = prot,
		.flags = flags,
		.fd = fd,
		.offset = offset,
	};
	if (results[idx] == MAP_FAILED)
		errno = errnos[idx];
	return results[idx];
}

static int expect_int(const char* label, long got, long want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %ld, want %ld\n", label, got, want);
	return 1;
}

static int expect_ptr(const char* label, const void* got, const void* want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %p, want %p\n", label, got, want);
	return 1;
}

static int check_preferred_success(void)
{
	reset_fake();
	unsigned long preferred_top = 0x70000000UL;
	unsigned long size = 0x10000UL;
	void* preferred_base = (void*)(preferred_top - size);
	unsigned long stack_top = 0;
	results[0] = preferred_base;

	void* stack = mldr_map_guest_stack(fake_map, preferred_top, size, &stack_top);
	if (expect_ptr("preferred stack", stack, preferred_base))
		return 1;
	if (expect_int("preferred call count", call_count, 1))
		return 1;
	if (expect_ptr("preferred addr", calls[0].addr, preferred_base))
		return 1;
	if (expect_int("preferred length", calls[0].length, size))
		return 1;
	if (expect_int("preferred prot", calls[0].prot, PROT_READ | PROT_WRITE))
		return 1;
	if (expect_int("preferred fixed flag", !!(calls[0].flags & MAP_FIXED_NOREPLACE), 1))
		return 1;
	if (expect_int("preferred grow flag", !!(calls[0].flags & MAP_GROWSDOWN), 1))
		return 1;
	if (expect_int("preferred stack_top", stack_top, preferred_top))
		return 1;
	return 0;
}

static int check_eexist_fallback(void)
{
	reset_fake();
	unsigned long preferred_top = 0x70000000UL;
	unsigned long size = 0x10000UL;
	void* preferred_base = (void*)(preferred_top - size);
	void* fallback_base = (void*)0x60000000UL;
	unsigned long stack_top = 0;
	results[0] = MAP_FAILED;
	errnos[0] = EEXIST;
	results[1] = fallback_base;

	void* stack = mldr_map_guest_stack(fake_map, preferred_top, size, &stack_top);
	if (expect_ptr("fallback stack", stack, fallback_base))
		return 1;
	if (expect_int("fallback call count", call_count, 2))
		return 1;
	if (expect_ptr("fallback first addr", calls[0].addr, preferred_base))
		return 1;
	if (expect_int("fallback first fixed flag", !!(calls[0].flags & MAP_FIXED_NOREPLACE), 1))
		return 1;
	if (expect_ptr("fallback second addr hint", calls[1].addr, preferred_base))
		return 1;
	if (expect_int("fallback second fixed flag", !!(calls[1].flags & MAP_FIXED_NOREPLACE), 0))
		return 1;
	if (expect_int("fallback second grow flag", !!(calls[1].flags & MAP_GROWSDOWN), 1))
		return 1;
	if (expect_int("fallback stack_top", stack_top, (unsigned long)fallback_base + size))
		return 1;
	return 0;
}

static int check_non_eexist_failure_does_not_retry(void)
{
	reset_fake();
	unsigned long stack_top = 0xdeadbeefUL;
	results[0] = MAP_FAILED;
	errnos[0] = EACCES;

	void* stack = mldr_map_guest_stack(fake_map, 0x70000000UL, 0x10000UL, &stack_top);
	if (expect_ptr("fail stack", stack, MAP_FAILED))
		return 1;
	if (expect_int("fail call count", call_count, 1))
		return 1;
	if (expect_int("fail stack_top unchanged", stack_top, 0xdeadbeefUL))
		return 1;
	return 0;
}

int main(void)
{
	if (check_preferred_success())
		return 1;
	if (check_eexist_fallback())
		return 1;
	if (check_non_eexist_failure_does_not_retry())
		return 1;
	puts("GREEN: mldr stack mapping fallback contract");
	return 0;
}

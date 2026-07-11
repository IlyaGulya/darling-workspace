#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "src/startup/mldr/elfcalls/dthreads.h"

#if !defined(__x86_64__)
#error "This contract exercises the x86_64 Darling guest TSD ABI"
#endif

#define ARCH_SET_GS 0x1001
#define ARCH_GET_GS 0x1004

static int expect_uintptr(const char* label, uintptr_t got, uintptr_t want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %#lx, want %#lx\n", label,
		(unsigned long)got, (unsigned long)want);
	return 1;
}

static uintptr_t read_guest_tsd_slot(unsigned int slot)
{
	uintptr_t value;
	if (slot == DTHREAD_TSD_SLOT_PTHREAD_SELF) {
		__asm__ volatile("movq %%gs:0, %0" : "=r"(value));
	} else if (slot == DTHREAD_TSD_SLOT_MACH_THREAD_SELF) {
		__asm__ volatile("movq %%gs:24, %0" : "=r"(value));
	} else {
		return 0;
	}
	return value;
}

int main(void)
{
	struct _dthread dthread;
	unsigned long original_gs = 0;
	const uint32_t mach_thread_self = 0x1234;
	int failed = 0;

	if (syscall(SYS_arch_prctl, ARCH_GET_GS, &original_gs) == -1) {
		perror("ARCH_GET_GS");
		return 2;
	}

	memset(&dthread, 0, sizeof(dthread));
	__darling_dthread_initialize(&dthread, 0, (void*)0x100000, 0x1000, NULL, 0);
	dthread.tsd[DTHREAD_TSD_SLOT_MACH_THREAD_SELF] =
		(void*)(uintptr_t)mach_thread_self;

	if (__darling_dthread_set_tsd_base(&dthread.tsd[0]) != 0) {
		fprintf(stderr, "failed to set Darling guest TSD base: %s\n", strerror(errno));
		return 2;
	}

	failed |= expect_uintptr("guest pthread self",
		read_guest_tsd_slot(DTHREAD_TSD_SLOT_PTHREAD_SELF), (uintptr_t)&dthread);
	failed |= expect_uintptr("guest mach thread self",
		read_guest_tsd_slot(DTHREAD_TSD_SLOT_MACH_THREAD_SELF), mach_thread_self);

	if (syscall(SYS_arch_prctl, ARCH_SET_GS, original_gs) == -1) {
		perror("restore ARCH_SET_GS");
		return 2;
	}
	if (failed)
		return 1;

	puts("GREEN: mldr main-thread TSD contract");
	return 0;
}

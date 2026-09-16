/* Minimal reproducer for the reserved-descriptor limit defect.
 *
 * The guest must be told a descriptor limit it can actually use. Stock
 * libunistring asserts exactly that and fails on a build with the ring
 * transport enabled:
 *   test-dup2.c:174          dup2 (fd, bad_fd - 1) == bad_fd - 1
 *   test-getdtablesize.c:37  dup2 (0, getdtablesize() - 1) == getdtablesize () - 1
 *
 * Measured on the matched pair on 2026-09-16: the OFF build reports the same
 * limits and can use the last descriptor, the ON build reports the same limits
 * and is refused EBADF at getdtablesize() - 1 and getdtablesize() - 2, while
 * those numbers are not occupied by anything. Ring traffic is not required to
 * see it, so it is a property of a build with the transport compiled in.
 *
 * Exit status is the verdict: 0 correct, 1 defect present, 2 cannot decide.
 * Run it through tests/run-ring-fd-limit-repro.sh, which owns the guest
 * transport; compiling it by hand needs -isysroot for the guest SDK.
 */

#include <stdio.h>
#include <unistd.h>

#ifdef REPRO_ACTIVATE_RING
#include <mach/mach.h>
#endif

int main(void)
{
#ifdef REPRO_ACTIVATE_RING
	int calls = 0;
	int i;

	for (i = 0; i < 36; ++i) {
		if (mach_host_self() != MACH_PORT_NULL) {
			++calls;
		}
	}
	printf("REPRO host_self_calls=%d\n", calls);
#endif

	int table = getdtablesize();
	if (table < 2) {
		printf("REPRO indecisive table=%d\n", table);
		return 2;
	}

	int highest = table - 1;
	int result = dup2(0, highest);
	int above = dup2(0, table);
	int correct = (result == highest) && (above == -1);

	printf("REPRO table=%d dup2(0,%d)=%d dup2(0,%d)=%d %s\n",
	       table, highest, result, table, above, correct ? "OK" : "BAD");
	return correct ? 0 : 1;
}

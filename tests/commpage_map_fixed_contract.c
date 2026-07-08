#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

#include "src/startup/mldr/commpage.h"
#include <i386/cpu_capabilities.h>

int main(void)
{
	void *want = (void *)_COMM_PAGE64_BASE_ADDRESS;
	void *pre = mmap(want, _COMM_PAGE64_AREA_LENGTH, PROT_READ | PROT_WRITE,
	    MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
	if (pre != want) {
		perror("premap canonical commpage");
		return 2;
	}

	((uint8_t *)want)[_COMM_PAGE_PHYSICAL_CPUS - _COMM_PAGE_START_ADDRESS] = 0;
	commpage_setup(1);

	uint8_t physical =
	    ((uint8_t *)want)[_COMM_PAGE_PHYSICAL_CPUS - _COMM_PAGE_START_ADDRESS];
	uint16_t version =
	    *(uint16_t *)((uint8_t *)want + (_COMM_PAGE_VERSION - _COMM_PAGE_START_ADDRESS));

	if (physical == 0) {
		fprintf(stderr, "canonical commpage was not populated\n");
		return 1;
	}
	if (version != _COMM_PAGE_THIS_VERSION) {
		fprintf(stderr, "bad commpage version %u\n", version);
		return 1;
	}
	puts("GREEN: commpage MAP_FIXED contract");
	return 0;
}

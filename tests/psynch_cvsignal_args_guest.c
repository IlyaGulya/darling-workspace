#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

extern int psynch_cvsignal_stub(void* cv, uint64_t cvlsgen, uint32_t cvugen, int thread_port, void* mutex, uint64_t mugen, uint64_t tid, uint32_t flags) asm("___psynch_cvsignal");
extern int psynch_cvbroad_stub(void* cv, uint64_t cvlsgen, uint64_t cvudgen, uint32_t flags, void* mutex, uint64_t mugen, uint64_t tid) asm("___psynch_cvbroad");

int main(void) {
	void* cv = mmap((void*)0x123450001000ULL, 4096, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE | MAP_FIXED, -1, 0);
	void* mutex = mmap((void*)0x123450002000ULL, 4096, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE | MAP_FIXED, -1, 0);
	if (cv != (void*)0x123450001000ULL || mutex != (void*)0x123450002000ULL) {
		perror("mmap");
		return 2;
	}
	uint64_t cvlsgen = 0x100000123ULL;
	uint64_t cvudgen = 0x200000456ULL;
	uint32_t cvugen = 0x789U;
	uint64_t mugen = 0x300000abcULL;
	uint64_t tid = 0x400000defULL;
	uint32_t flags = 0x55U;

	(void)psynch_cvsignal_stub(cv, cvlsgen, cvugen, -1, mutex, mugen, tid, flags);
	(void)psynch_cvbroad_stub(cv, cvlsgen, cvudgen, flags, mutex, mugen, tid);

	write(1, "PSYNCH_CVSIGNAL_ARGS_GUEST_OK\n", 31);
	_exit(0);
}

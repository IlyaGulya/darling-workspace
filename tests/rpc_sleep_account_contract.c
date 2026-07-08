#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>
#include <darling/emulation/linux_premigration/resources/rpc-sleep-account.h>

struct write_record {
	long fd;
	char data[128];
	long len;
};

static struct write_record writes[8];
static int write_count;
static int open_count;
static int close_count;

static int fail_int(const char* label, unsigned long got, unsigned long want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %lu, want %lu\n", label, got, want);
	return 1;
}

long test_linux_syscall(long nr, ...)
{
	va_list ap;
	va_start(ap, nr);
	if (nr == __NR_openat) {
		(void)va_arg(ap, long);
		(void)va_arg(ap, const char*);
		(void)va_arg(ap, long);
		(void)va_arg(ap, long);
		va_end(ap);
		++open_count;
		return 42;
	}
	if (nr == __NR_getpid) {
		va_end(ap);
		return 1234;
	}
	if (nr == __NR_write) {
		long fd = va_arg(ap, long);
		const char* data = va_arg(ap, const char*);
		long len = va_arg(ap, long);
		va_end(ap);
		if (write_count >= (int)(sizeof(writes) / sizeof(writes[0])))
			return -1;
		writes[write_count].fd = fd;
		writes[write_count].len = len;
		if (len >= (long)sizeof(writes[write_count].data))
			len = (long)sizeof(writes[write_count].data) - 1;
		memcpy(writes[write_count].data, data, (size_t)len);
		writes[write_count].data[len] = 0;
		++write_count;
		return len;
	}
	if (nr == __NR_close) {
		(void)va_arg(ap, long);
		va_end(ap);
		++close_count;
		return 0;
	}
	va_end(ap);
	return 0;
}

static int check_accounting(void)
{
	unsigned long start = __darling_rpc_sleep_account_begin();
	__darling_rpc_sleep_account_end(start, 0x123);
	__darling_rpc_sleep_account_end(0, 0x223);
	__darling_rpc_sleep_account_end(0, 0x07);

	if (fail_int("masked call count", __darling_rpc_sleep_count[0x23], 2))
		return 1;
	if (__darling_rpc_sleep_total_ticks[0x23] == 0) {
		fprintf(stderr, "masked total ticks did not increase\n");
		return 1;
	}
	if (fail_int("second call count", __darling_rpc_sleep_count[0x07], 1))
		return 1;
	if (fail_int("unrelated bucket", __darling_rpc_sleep_count[0x24], 0))
		return 1;
	return 0;
}

static int check_dump_once(void)
{
	__darling_rpc_sleep_dump();
	if (fail_int("open count", open_count, 1))
		return 1;
	if (fail_int("close count", close_count, 1))
		return 1;
	if (fail_int("write count", write_count, 2))
		return 1;
	if (writes[0].fd != 42 || writes[1].fd != 42) {
		fprintf(stderr, "dump wrote to wrong fd\n");
		return 1;
	}
	if (!strstr(writes[0].data, "1234 ") || !strstr(writes[1].data, "1234 ")) {
		fprintf(stderr, "dump lines do not include pid: '%s' / '%s'\n", writes[0].data, writes[1].data);
		return 1;
	}
	if ((!strstr(writes[0].data, "7 1 ") && !strstr(writes[1].data, "7 1 ")) ||
			(!strstr(writes[0].data, "35 2 ") && !strstr(writes[1].data, "35 2 "))) {
		fprintf(stderr, "dump lines do not include expected buckets: '%s' / '%s'\n", writes[0].data, writes[1].data);
		return 1;
	}

	__darling_rpc_sleep_dump();
	if (fail_int("open count after second dump", open_count, 1))
		return 1;
	if (fail_int("write count after second dump", write_count, 2))
		return 1;
	return 0;
}

int main(void)
{
	if (check_accounting())
		return 1;
	if (check_dump_once())
		return 1;
	puts("GREEN: RPC sleep account contract");
	return 0;
}

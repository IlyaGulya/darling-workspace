#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>

#include <darling/emulation/conversion/duct_errno.h>
#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>
#include <darling/emulation/xnu_syscall/bsd/impl/psynch/ulock_wait.h>
#include <darling/emulation/xnu_syscall/bsd/impl/psynch/ulock_wake.h>
#include <darlingserver/rpc.h>

struct futex_call {
	void* addr;
	long op;
	uint64_t value;
	void* timeout;
};

static struct futex_call calls[8];
static long results[8];
static int result_count;
static int call_count;
static int rpc_status = -LINUX_EIO;
static uint32_t rpc_value;
static int rpc_calls;
static uint32_t rpc_operation, rpc_timeout;
static uint64_t rpc_address, rpc_argument;

int dserver_rpc_ulock_wait(uint32_t operation, uint64_t addr, uint64_t value,
	uint32_t timeout, uint32_t* retval)
{
	++rpc_calls;
	rpc_operation = operation; rpc_address = addr;
	rpc_argument = value; rpc_timeout = timeout;
	*retval = rpc_value;
	return rpc_status;
}

int dserver_rpc_ulock_wake(uint32_t operation, uint64_t addr, uint64_t value,
	uint32_t* retval)
{
	return dserver_rpc_ulock_wait(operation, addr, value, 0, retval);
}

void __simple_fprintf(int fd, const char* format, ...) { (void)fd; (void)format; }

int errno_linux_to_bsd(int err)
{
	return err;
}

long test_linux_syscall(long nr, ...)
{
	if (nr != __NR_futex)
		return -LINUX_EINVAL;

	va_list ap;
	va_start(ap, nr);
	void* addr = va_arg(ap, void*);
	int op = va_arg(ap, int);
	uint64_t value = (op & FUTEX_WAKE) ? (uint64_t)va_arg(ap, int) : va_arg(ap, uint64_t);
	void* timeout = (op & FUTEX_WAKE) ? 0 : va_arg(ap, void*);
	va_end(ap);

	int idx = call_count++;
	if (idx >= (int)(sizeof(calls) / sizeof(calls[0])))
		return -LINUX_EINVAL;
	calls[idx] = (struct futex_call){
		.addr = addr,
		.op = op,
		.value = value,
		.timeout = timeout,
	};
	return idx < result_count ? results[idx] : -LINUX_EINVAL;
}

static void reset_results(const long* seq, int count)
{
	call_count = 0;
	result_count = count;
	for (int i = 0; i < 8; ++i) {
		results[i] = 0;
		calls[i] = (struct futex_call){0};
	}
	for (int i = 0; i < count; ++i)
		results[i] = seq[i];
}

static int expect_int(const char* label, long got, long want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %ld, want %ld\n", label, got, want);
	return 1;
}

static int check_untimed_wait_retries_eintr(void)
{
	uint32_t word = 0;
	const long seq[] = { -LINUX_EINTR, -LINUX_EINTR, 0 };
	reset_results(seq, 3);

	long ret = sys_ulock_wait(XNU_UL_COMPARE_AND_WAIT | XNU_ULF_NO_ERRNO, &word, 17, 0);
	if (expect_int("untimed wait ret", ret, 1))
		return 1;
	if (expect_int("untimed wait call count", call_count, 3))
		return 1;
	for (int i = 0; i < 3; ++i) {
		if (expect_int("untimed wait op", calls[i].op, FUTEX_WAIT | FUTEX_PRIVATE_FLAG))
			return 1;
		if (expect_int("untimed wait value", calls[i].value, 17))
			return 1;
		if (calls[i].timeout != 0) {
			fprintf(stderr, "untimed wait passed non-null timeout\n");
			return 1;
		}
	}
	return 0;
}

static int check_timed_wait_does_not_retry_eintr(void)
{
	uint32_t word = 0;
	const long seq[] = { -LINUX_EINTR, 0 };
	reset_results(seq, 2);

	long ret = sys_ulock_wait(XNU_UL_UNFAIR_LOCK | XNU_ULF_NO_ERRNO, &word, 99, 1000);
	if (expect_int("timed wait ret", ret, -LINUX_EINTR))
		return 1;
	if (expect_int("timed wait call count", call_count, 1))
		return 1;
	if (calls[0].timeout == 0) {
		fprintf(stderr, "timed wait did not pass timeout\n");
		return 1;
	}
	return 0;
}

static int check_wake_returns_clean_errno(void)
{
	uint32_t word = 0;
	const long seq[] = { -LINUX_EINTR };
	reset_results(seq, 1);

	long ret = sys_ulock_wake(XNU_UL_COMPARE_AND_WAIT | XNU_ULF_NO_ERRNO, &word, 0);
	if (expect_int("wake clean errno", ret, -LINUX_EINTR))
		return 1;
	if (expect_int("wake call count", call_count, 1))
		return 1;
	if (expect_int("wake op", calls[0].op, FUTEX_WAKE | FUTEX_PRIVATE_FLAG))
		return 1;
	if (expect_int("wake count", calls[0].value, 1))
		return 1;
	return 0;
}

static int check_wake_success_returns_zero(void)
{
	uint32_t word = 0;
	const long seq[] = { 3 };
	reset_results(seq, 1);

	long ret = sys_ulock_wake(XNU_UL_UNFAIR_LOCK | XNU_ULF_WAKE_ALL, &word, 0);
	if (expect_int("wake success", ret, 0))
		return 1;
	if (expect_int("wake all count", calls[0].value, INT32_MAX))
		return 1;
	return 0;
}

static int check_server_results(void)
{
	uint32_t word = 0;
	const uint32_t operation = XNU_UL_UNFAIR_LOCK | XNU_ULF_NO_ERRNO;
	const int statuses[] = { -LINUX_EINTR, EINVAL, 0, 0 };
	const uint32_t values[] = { 0, 0, (uint32_t)-EINTR, 4 };
	const long expected[] = { -EINTR, -EINVAL, -EINTR, 4 };
	for (unsigned i = 0; i < sizeof(statuses) / sizeof(statuses[0]); ++i) {
		reset_results(0, 0);
		rpc_calls = 0; rpc_status = statuses[i]; rpc_value = values[i];
		if (expect_int("server wait result", sys_ulock_wait(operation, &word, 17, 123), expected[i]) ||
		    expect_int("server wait RPC count", rpc_calls, 1) ||
		    expect_int("server wait no futex", call_count, 0) ||
		    expect_int("server wait operation", rpc_operation, operation) ||
		    expect_int("server wait address", rpc_address, (uintptr_t)&word) ||
		    expect_int("server wait value", rpc_argument, 17) ||
		    expect_int("server wait timeout", rpc_timeout, 123)) return 1;
	}
	for (unsigned i = 0; i < sizeof(statuses) / sizeof(statuses[0]); ++i) {
		const long wake_results[] = { 0 };
		reset_results(wake_results, 1);
		rpc_calls = 0; rpc_status = statuses[i]; rpc_value = values[i];
		if (expect_int("server wake result", sys_ulock_wake(operation, &word, 0), expected[i]) ||
		    expect_int("server wake RPC count", rpc_calls, 1) ||
		    expect_int("server wake host queue only on success", call_count, statuses[i] == 0)) return 1;
	}
	return 0;
}
int main(void)
{
	if (check_untimed_wait_retries_eintr())
		return 1;
	if (check_timed_wait_does_not_retry_eintr())
		return 1;
	if (check_wake_returns_clean_errno())
		return 1;
	if (check_wake_success_returns_zero())
		return 1;
	if (check_server_results())
		return 1;
	puts("GREEN: ulock EINTR retry contract");
	return 0;
}

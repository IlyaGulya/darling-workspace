#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include <darling/emulation/linux_premigration/resources/dserver-rpc-defs.h>

#ifndef LINUX_EIO
#define LINUX_EIO 5
#endif

struct recv_call {
	long nr;
	int socket;
	long flags;
};

static struct recv_call recv_calls[16];
static long recv_results[16];
static int recv_result_count;
static int recv_call_count;

static void reset_results(const long* results, int count)
{
	for (int i = 0; i < count; ++i)
		recv_results[i] = results[i];
	recv_result_count = count;
	recv_call_count = 0;
}

static int fail_int(const char* label, long got, long want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %ld, want %ld\n", label, got, want);
	return 1;
}

long test_linux_syscall(long nr, ...)
{
	va_list ap;
	va_start(ap, nr);
	if (nr == __NR_recvmsg) {
		int socket = va_arg(ap, int);
		(void)va_arg(ap, struct linux_msghdr*);
		long flags = va_arg(ap, long);
		va_end(ap);
		if (recv_call_count >= (int)(sizeof(recv_calls) / sizeof(recv_calls[0])))
			return -LINUX_EIO;
		recv_calls[recv_call_count].nr = nr;
		recv_calls[recv_call_count].socket = socket;
		recv_calls[recv_call_count].flags = flags;
		long result = recv_call_count < recv_result_count ?
			recv_results[recv_call_count] : -LINUX_EIO;
		++recv_call_count;
		return result;
	}
	va_end(ap);
	return 0;
}

static long receive_once(void)
{
	char payload[64] = {0};
	struct iovec iov = {
		.iov_base = payload,
		.iov_len = sizeof(payload),
	};
	struct linux_msghdr msg = {
		.msg_iov = &iov,
		.msg_iovlen = 1,
	};
	return dserver_rpc_hooks_receive_message(77, &msg);
}

#if DARLING_GUEST_RECVSPIN > 0
static int check_fast_reply_uses_nonblocking_poll(void)
{
	const long results[] = { 1 };
	reset_results(results, 1);
	long ret = receive_once();
	if (fail_int("fast reply ret", ret, 1))
		return 1;
	if (fail_int("fast reply call count", recv_call_count, 1))
		return 1;
	if (fail_int("fast reply flags", recv_calls[0].flags, LINUX_MSG_DONTWAIT))
		return 1;
	return 0;
}

static int check_eagain_budget_falls_back_to_blocking(void)
{
	const long results[] = { -LINUX_EAGAIN, -LINUX_EAGAIN, -LINUX_EAGAIN, 1 };
	reset_results(results, 4);
	long ret = receive_once();
	if (fail_int("fallback ret", ret, 1))
		return 1;
	if (fail_int("fallback call count", recv_call_count, 4))
		return 1;
	for (int i = 0; i < 3; ++i) {
		if (fail_int("spin flags", recv_calls[i].flags, LINUX_MSG_DONTWAIT))
			return 1;
	}
	if (fail_int("blocking fallback flags", recv_calls[3].flags, 0))
		return 1;
	return 0;
}

static int check_real_spin_error_does_not_block(void)
{
	const long results[] = { -LINUX_EIO };
	reset_results(results, 1);
	long ret = receive_once();
	if (fail_int("real error ret", ret, -LINUX_EIO))
		return 1;
	if (fail_int("real error call count", recv_call_count, 1))
		return 1;
	if (fail_int("real error flags", recv_calls[0].flags, LINUX_MSG_DONTWAIT))
		return 1;
	return 0;
}
#else
static int check_disabled_budget_blocks_immediately(void)
{
	const long results[] = { 1 };
	reset_results(results, 1);
	long ret = receive_once();
	if (fail_int("disabled ret", ret, 1))
		return 1;
	if (fail_int("disabled call count", recv_call_count, 1))
		return 1;
	if (fail_int("disabled flags", recv_calls[0].flags, 0))
		return 1;
	return 0;
}
#endif

int main(void)
{
#if DARLING_GUEST_RECVSPIN > 0
	if (check_fast_reply_uses_nonblocking_poll())
		return 1;
	if (check_eagain_budget_falls_back_to_blocking())
		return 1;
	if (check_real_spin_error_does_not_block())
		return 1;
#else
	if (check_disabled_budget_blocks_immediately())
		return 1;
#endif
	puts("GREEN: dylib receive hook adaptive spin contract");
	return 0;
}

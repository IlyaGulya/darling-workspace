#include <signal.h>
#include <stdarg.h>
#include <stdio.h>

#include <darling/emulation/xnu_syscall/bsd/impl/misc/abort_with_payload.h>
#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>

enum {
	SYSCALL_NONE = 0,
	SYSCALL_GETPID,
	SYSCALL_GETTID,
	SYSCALL_TGKILL,
};

static int last_syscall;
static long seen_pid;
static long seen_tid;
static long seen_signal;
static int saw_group_kill;

long test_linux_syscall(long nr, ...)
{
	va_list ap;
	va_start(ap, nr);
	if (nr == __NR_getpid) {
		last_syscall = SYSCALL_GETPID;
		va_end(ap);
		return 4242;
	}
	if (nr == __NR_gettid) {
		last_syscall = SYSCALL_GETTID;
		va_end(ap);
		return 4343;
	}
	if (nr == __NR_tgkill) {
		last_syscall = SYSCALL_TGKILL;
		long a1 = va_arg(ap, long);
		long a2 = va_arg(ap, long);
		long a3 = va_arg(ap, long);
		seen_pid = a1;
		seen_tid = a2;
		seen_signal = a3;
		va_end(ap);
		return 0;
	}
	va_end(ap);
	fprintf(stderr, "unexpected linux syscall nr=%ld\n", nr);
	return -1;
}

long sys_kill(int pid, int sig, int posix)
{
	if (pid == 0 && sig == SIGABRT && posix == 1)
		saw_group_kill = 1;
	return 0;
}

int signum_bsd_to_linux(int signum)
{
	if (signum == SIGABRT)
		return 6;
	return -1;
}

void __simple_printf(const char* message, ...)
{
	(void)message;
}

static int expect_long(const char* label, long got, long want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %ld, want %ld\n", label, got, want);
	return 1;
}

int main(void)
{
	long ret = sys_abort_with_payload(5, 7, 0, 0, "contract", 0);

	int failed = 0;
	failed |= expect_long("return value", ret, 0);
	failed |= expect_long("process group kill", saw_group_kill, 0);
	failed |= expect_long("final syscall", last_syscall, SYSCALL_TGKILL);
	failed |= expect_long("tgkill pid", seen_pid, 4242);
	failed |= expect_long("tgkill tid", seen_tid, 4343);
	failed |= expect_long("tgkill signal", seen_signal, 6);
	return failed ? 1 : 0;
}

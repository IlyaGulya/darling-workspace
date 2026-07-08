#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <darling/emulation/linux_premigration/signal/sigexc.h>
#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>
#include <darling/emulation/xnu_syscall/bsd/impl/signal/sigaction.h>

#define SIGSEGV 11

bsd_sig_handler* sig_handlers[32];
int sig_flags[32];
unsigned int sig_masks[32];

static long last_tgkill_pid;
static long last_tgkill_tid;
static long last_tgkill_signal;
static int saw_group_kill;
static int saw_default_sigaction;
static int interrupt_exit_count;

long test_linux_syscall(long nr, ...)
{
	va_list ap;
	va_start(ap, nr);
	if (nr == __NR_getpid) {
		va_end(ap);
		return 4242;
	}
	if (nr == __NR_gettid) {
		va_end(ap);
		return 4343;
	}
	if (nr == __NR_tgkill) {
		last_tgkill_pid = va_arg(ap, long);
		last_tgkill_tid = va_arg(ap, long);
		last_tgkill_signal = va_arg(ap, long);
		va_end(ap);
		return 0;
	}
	if (nr == __NR_kill) {
		long pid = va_arg(ap, long);
		long sig = va_arg(ap, long);
		if (pid == 0 && sig == LINUX_SIGSEGV)
			saw_group_kill = 1;
		va_end(ap);
		return 0;
	}
	if (nr == __NR_rt_sigaction) {
		long sig = va_arg(ap, long);
		struct linux_sigaction* sa = va_arg(ap, struct linux_sigaction*);
		if (sig == LINUX_SIGSEGV && sa && sa->sa_sigaction == 0)
			saw_default_sigaction = 1;
		va_end(ap);
		return 0;
	}
	va_end(ap);
	return 0;
}

int signum_linux_to_bsd(int signum)
{
	if (signum == LINUX_SIGSEGV)
		return SIGSEGV;
	return -1;
}

int signum_bsd_to_linux(int signum)
{
	if (signum == SIGSEGV)
		return LINUX_SIGSEGV;
	return -1;
}

void handler_linux_to_bsd(int linux_signum, struct linux_siginfo* info, void* ctxt)
{
	(void)linux_signum;
	(void)info;
	(void)ctxt;
}

int dserver_rpc_sigprocess(int bsd_signum_in, int linux_signum, int sender_pid,
	int code, void* fault_addr, void* thread_state, void* float_state,
	int* bsd_signum_out)
{
	(void)bsd_signum_in;
	(void)linux_signum;
	(void)sender_pid;
	(void)code;
	(void)fault_addr;
	(void)thread_state;
	(void)float_state;
	*bsd_signum_out = SIGSEGV;
	return 0;
}

int dserver_rpc_interrupt_enter(void)
{
	return 0;
}

int mach_thread_self(void)
{
	return 31337;
}

int dserver_rpc_interrupt_exit(void)
{
	++interrupt_exit_count;
	return 0;
}

int dserver_rpc_thread_suspended(void* thread_state, void* float_state)
{
	(void)thread_state;
	(void)float_state;
	return 0;
}

int dserver_rpc_s2c_perform(void)
{
	return 0;
}

void __simple_printf(const char* message, ...)
{
	(void)message;
}

void __simple_kprintf(const char* message, ...)
{
	(void)message;
}

void __simple_abort(void)
{
	__builtin_trap();
}

void sig_restorer(void)
{
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
	struct linux_siginfo info;
	struct linux_ucontext ctxt;
	memset(&info, 0, sizeof(info));
	memset(&ctxt, 0, sizeof(ctxt));
	info.si_pid = 99;
	info.si_code = 0;
	info.si_addr = (void*)0x1234;

	sigexc_handler(LINUX_SIGSEGV, &info, &ctxt);

	int failed = 0;
	failed |= expect_long("default sigaction", saw_default_sigaction, 1);
	failed |= expect_long("process group kill", saw_group_kill, 0);
	failed |= expect_long("tgkill pid", last_tgkill_pid, 4242);
	failed |= expect_long("tgkill tid", last_tgkill_tid, 4343);
	failed |= expect_long("tgkill signal", last_tgkill_signal, LINUX_SIGSEGV);
	failed |= expect_long("interrupt exit", interrupt_exit_count, 1);
	if (!failed)
		puts("GREEN: sigexc default resend-self contract");
	return failed ? 1 : 0;
}

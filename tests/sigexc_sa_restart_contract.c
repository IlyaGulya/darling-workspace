#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>
#include <darling/emulation/xnu_syscall/bsd/impl/signal/sigaction.h>

#define SIGSEGV 11

static int rt_sigaction_count;
static int last_linux_signum;
static int last_flags;
static linux_sig_handler* last_handler;

long test_linux_syscall(long nr, ...)
{
	va_list ap;
	va_start(ap, nr);
	if (nr == __NR_rt_sigaction) {
		last_linux_signum = (int)va_arg(ap, long);
		struct linux_sigaction* sa = va_arg(ap, struct linux_sigaction*);
		if (sa) {
			last_flags = sa->sa_flags;
			last_handler = sa->sa_sigaction;
		}
		++rt_sigaction_count;
		va_end(ap);
		return 0;
	}
	va_end(ap);
	return 0;
}

int signum_bsd_to_linux(int signum)
{
	if (signum == SIGSEGV)
		return LINUX_SIGSEGV;
	return 0;
}

int signum_linux_to_bsd(int signum)
{
	if (signum == LINUX_SIGSEGV)
		return SIGSEGV;
	return 0;
}

void sigset_bsd_to_linux(const sigset_t* bsd, linux_sigset_t* linux_set)
{
	*linux_set = bsd ? *bsd : 0;
}

void sigset_linux_to_bsd(const linux_sigset_t* linux_set, sigset_t* bsd)
{
	*bsd = linux_set ? (sigset_t)*linux_set : 0;
}

int errno_linux_to_bsd(int err)
{
	return err;
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

int dserver_rpc_interrupt_enter(void) { return 0; }
int dserver_rpc_interrupt_exit(void) { return 0; }
int dserver_rpc_thread_suspended(void* thread_state, void* float_state)
{
	(void)thread_state;
	(void)float_state;
	return 0;
}
int dserver_rpc_s2c_perform(void) { return 0; }
int mach_thread_self(void) { return 31337; }

void __simple_printf(const char* message, ...) { (void)message; }
void __simple_kprintf(const char* message, ...) { (void)message; }
void __simple_abort(void) { __builtin_trap(); }
void sig_restorer(void) {}

static int expect_long(const char* label, long got, long want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %ld, want %ld\n", label, got, want);
	return 1;
}

static void reset_capture(void)
{
	rt_sigaction_count = 0;
	last_linux_signum = 0;
	last_flags = 0;
	last_handler = 0;
}

static int install_and_check(const char* label, bsd_sig_handler* handler, int bsd_flags, int want_restart)
{
	struct bsd___sigaction nsa;
	memset(&nsa, 0, sizeof(nsa));
	nsa.sa_sigaction = handler;
	nsa.sa_flags = bsd_flags;

	reset_capture();
	long ret = sys_sigaction(SIGSEGV, &nsa, 0);

	int failed = 0;
	failed |= expect_long(label, ret, 0);
	failed |= expect_long("rt_sigaction count", rt_sigaction_count, 1);
	failed |= expect_long("linux signum", last_linux_signum, LINUX_SIGSEGV);
	failed |= expect_long("restart flag", !!(last_flags & LINUX_SA_RESTART), want_restart);
	failed |= expect_long("siginfo flag", !!(last_flags & LINUX_SA_SIGINFO), 1);
	failed |= expect_long("onstack flag", !!(last_flags & LINUX_SA_ONSTACK), 1);
	failed |= expect_long("handler installed", last_handler != 0, 1);
	return failed;
}

static void app_handler(int signum, struct bsd_siginfo* info, void* ctxt)
{
	(void)signum;
	(void)info;
	(void)ctxt;
}

int main(void)
{
	int failed = 0;
	failed |= install_and_check("SA_RESTART install", app_handler, BSD_SA_RESTART, 1);
	failed |= install_and_check("non-restart install", app_handler, 0, 0);
	failed |= install_and_check("SIG_IGN install", XNU_SIG_IGN, 0, 1);
	return failed ? 1 : 0;
}

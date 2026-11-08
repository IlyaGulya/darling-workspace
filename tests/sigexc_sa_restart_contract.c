#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>
#include <darling/emulation/xnu_syscall/bsd/impl/signal/sigaction.h>
#include <darlingserver/rpc.h>
#include <darling/emulation/linux_premigration/elfcalls_wrapper.h>
#include <darling/emulation/linux_premigration/signal/sigexc.h>

#define SIGSEGV 11

static int rt_sigaction_count;
static int last_linux_signum;
static int last_flags;
static linux_sig_handler* last_handler;
static struct linux_sigaction installed_action;
static long install_result;

long test_linux_syscall(long nr, ...)
{
	va_list ap;
	va_start(ap, nr);
	if (nr == __NR_rt_sigaction) {
		last_linux_signum = va_arg(ap, int);
		struct linux_sigaction* sa = va_arg(ap, struct linux_sigaction*);
		struct linux_sigaction* old = va_arg(ap, struct linux_sigaction*);
		if (old) *old = installed_action;
		if (sa && install_result == 0) {
			installed_action = *sa;
			last_flags = sa->sa_flags;
			last_handler = sa->sa_sigaction;
		}
		++rt_sigaction_count;
		va_end(ap);
		return sa ? install_result : 0;
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
	int code, uint64_t fault_addr, uint64_t thread_state, uint64_t float_state,
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
int dserver_rpc_thread_suspended(uint64_t thread_state, uint64_t float_state)
{
	(void)thread_state;
	(void)float_state;
	return 0;
}
int dserver_rpc_s2c_perform(void) { return 0; }
int mach_thread_self(void) { return 31337; }
int dserver_rpc_started_suspended(_Bool* value) { (void)value; __builtin_trap(); }
int dserver_rpc_get_tracer(int32_t* value) { (void)value; __builtin_trap(); }
int __dserver_plane_urgent_publish(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3)
{
	(void)op; (void)p0; (void)p1; (void)p2; (void)p3;
	__builtin_trap();
}
int __dserver_plane_urgent_interrupt_exit(uint32_t arch, int32_t* status)
{
	(void)arch; (void)status; __builtin_trap();
}
int __dserver_plane_urgent_reserve(struct dserver_urgent_request* request)
{
	(void)request; __builtin_trap();
}
int __dserver_plane_urgent_commit(struct dserver_urgent_request* request, uint32_t op,
	const uint64_t* words, uint32_t count)
{
	(void)request; (void)op; (void)words; (void)count; __builtin_trap();
}
void __dserver_plane_urgent_wait(struct dserver_urgent_request* request, int32_t* status, uint64_t* signal)
{
	(void)request; (void)status; (void)signal; __builtin_trap();
}
void __dserver_plane_urgent_release(struct dserver_urgent_request* request)
{
	(void)request; __builtin_trap();
}
void __simple_fprintf(int fd, const char* format, ...)
{
	(void)fd; (void)format; __builtin_trap();
}

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

static int install_and_check(const char* label, bsd_sig_handler* handler, int bsd_flags,
	int want_restart, int want_calls)
{
	struct bsd___sigaction nsa;
	memset(&nsa, 0, sizeof(nsa));
	nsa.sa_sigaction = handler;
	nsa.sa_flags = bsd_flags;

	reset_capture();
	long ret = sys_sigaction(SIGSEGV, &nsa, 0);

	int failed = 0;
	failed |= expect_long(label, ret, 0);
	failed |= expect_long("rt_sigaction count", rt_sigaction_count, want_calls);
	failed |= expect_long("linux signum", last_linux_signum, LINUX_SIGSEGV);
	failed |= expect_long("restart flag", !!(last_flags & LINUX_SA_RESTART), want_restart);
	failed |= expect_long("siginfo flag", !!(last_flags & LINUX_SA_SIGINFO), 1);
	failed |= expect_long("onstack flag", !!(last_flags & LINUX_SA_ONSTACK), 1);
	failed |= expect_long("handler installed", last_handler == (linux_sig_handler*)handler ||
		(handler != XNU_SIG_IGN && last_handler == (linux_sig_handler*)sigexc_handler), 1);
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
	failed |= install_and_check("SA_RESTART install", app_handler, BSD_SA_RESTART, 1, 1);
	failed |= install_and_check("non-restart install", app_handler, 0, 0, 1);
	failed |= install_and_check("SIG_IGN install", XNU_SIG_IGN, 0, 1, 3);
	failed |= install_and_check("leave SIG_IGN", app_handler, 0, 0, 3);
	install_result = -22;
	struct bsd___sigaction failing = { .sa_sigaction = XNU_SIG_IGN };
	reset_capture();
	failed |= expect_long("install failure", sys_sigaction(SIGSEGV, &failing, 0), -22);
	failed |= expect_long("failure no subsequent install", rt_sigaction_count, 1);
	if (!failed)
		puts("GREEN: sigexc SA_RESTART contract");
	return failed ? 1 : 0;
}

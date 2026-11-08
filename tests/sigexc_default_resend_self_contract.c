#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <darling/emulation/linux_premigration/signal/sigexc.h>
#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>
#include <darling/emulation/xnu_syscall/bsd/impl/signal/sigaction.h>
#include <darlingserver/rpc.h>
#include <darling/emulation/linux_premigration/elfcalls_wrapper.h>

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
static int tgkill_count;
static int delivered_signal = SIGSEGV;
static int sigprocess_count;

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
		++tgkill_count;
		last_tgkill_pid = va_arg(ap, int);
		last_tgkill_tid = va_arg(ap, int);
		last_tgkill_signal = va_arg(ap, int);
		va_end(ap);
		return 0;
	}
	if (nr == __NR_kill) {
		int pid = va_arg(ap, int);
		int sig = va_arg(ap, int);
		(void)pid; (void)sig;
		saw_group_kill = 1;
		va_end(ap);
		return 0;
	}
	if (nr == __NR_rt_sigaction) {
		int sig = va_arg(ap, int);
		struct linux_sigaction* sa = va_arg(ap, struct linux_sigaction*);
		if (sig == LINUX_SIGSEGV && sa && sa->sa_sigaction == 0)
			saw_default_sigaction = 1;
		va_end(ap);
		return 0;
	}
	va_end(ap);
	if (nr == __NR_openat) return -2; /* Explicit unavailable diagnostic proc file. */
	__builtin_trap();
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
	++sigprocess_count;
	*bsd_signum_out = delivered_signal;
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

int dserver_rpc_thread_suspended(uint64_t thread_state, uint64_t float_state)
{
	(void)thread_state;
	(void)float_state;
	return 0;
}

int dserver_rpc_s2c_perform(void)
{
	return 0;
}
int dserver_rpc_started_suspended(_Bool* value) { (void)value; __builtin_trap(); }
int dserver_rpc_get_tracer(int32_t* value) { (void)value; __builtin_trap(); }
/* No urgent publication: exercise the real, pre-admission RPC signal route. */
int __dserver_plane_urgent_publish(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3)
{
	(void)op; (void)p0; (void)p1; (void)p2; (void)p3;
	return DSERVER_URGENT_NOT_PUBLISHED;
}
int __dserver_plane_urgent_interrupt_exit(uint32_t arch, int32_t* status)
{
	(void)arch; (void)status; return DSERVER_URGENT_NOT_PUBLISHED;
}
int __dserver_plane_urgent_reserve(struct dserver_urgent_request* request)
{
	(void)request; return DSERVER_URGENT_NOT_PUBLISHED;
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
void __simple_fprintf(int fd, const char* format, ...) { (void)fd; (void)format; }
void sigset_bsd_to_linux(const sigset_t* bsd, linux_sigset_t* native)
{
	*native = *bsd;
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
	failed |= expect_long("single resend", tgkill_count, 1);
	failed |= expect_long("single signal RPC", sigprocess_count, 1);
	delivered_signal = 0;
	sigexc_handler(LINUX_SIGSEGV, &info, &ctxt);
	failed |= expect_long("drop does not resend", tgkill_count, 1);
	failed |= expect_long("drop interrupt exit", interrupt_exit_count, 2);
	failed |= expect_long("drop signal RPC", sigprocess_count, 2);
	if (!failed)
		puts("GREEN: sigexc default resend-self contract");
	return failed ? 1 : 0;
}

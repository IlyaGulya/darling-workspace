/*
 * Deterministic host contract for the dtape_mutex OWNERSHIP CONTRACT, driven against the REAL
 * product implementation (duct-tape/src/locks.c is compiled unchanged into this binary).
 *
 * Why: a T4 run aborted with
 *     darlingserver duct-tape panic: "Lock assertion failed (not owned but expected to be owned)"
 * inside dtape_mutex_unlock. The panic proves an OWNERSHIP MISMATCH; it does not prove which
 * transition produced it. This contract fixes the vocabulary instead of guessing:
 *
 *   legal    an active-thread lock/unlock pair
 *   legal    a no-active-thread pair (the fallback: the native queue lock IS the acquisition and
 *            dtape_owner deliberately stays 0)
 *   ILLEGAL  acquiring through the no-active-thread fallback and releasing with an active thread
 *   ILLEGAL  acquiring as guest thread A and releasing as guest thread B
 *
 * and it shows the two ILLEGAL transitions produce exactly the measured panic while the legal ones
 * do not. That makes the mechanism reproducible in milliseconds instead of waiting for a rare run,
 * and it is the evidence required BEFORE changing any lock behaviour.
 *
 * It is not a source audit: locks.c is executed. Duct-tape's panic() and dtape_log() live in
 * duct-tape/src/stubs.c, which is NOT compiled here, so this file supplies them.
 *
 * Two constraints of the compile context shape this file:
 *   - it must not include <stdio.h> / <string.h> / <setjmp.h>: duct-tape C files compile against
 *     Darwin and XNU headers whose declarations clash with glibc's (the same reason sys_ulock.c
 *     declares the libc calls it needs by hand, and why setjmp here collided with XNU's jmp_buf);
 *   - panic() is noreturn, so each arm runs in its own forked child and the parent reads the exit
 *     status. No longjmp is needed, and one arm cannot corrupt the next.
 *
 * Exit status of an arm child: 0 = completed, 3 = the not-owned assertion, 4 = another panic,
 * 5 = contention never suspended, 6 = suspension guard tripped.
 */

#include <darlingserver/duct-tape/stubs.h>
#include <darlingserver/duct-tape/hooks.internal.h>
#include <darlingserver/duct-tape/thread.h>
#include <darlingserver/duct-tape/locks.h>
#include <darlingserver/duct-tape/log.h>

#include <kern/locks.h>
#include <kern/waitq.h>
#include <kern/thread.h>

#include <stdarg.h>

extern int snprintf(char *str, unsigned long size, const char *format, ...);
extern int vsnprintf(char *str, unsigned long size, const char *format, va_list ap);
extern long write(int fd, const void *buf, unsigned long count);
extern int fork(void);
extern int waitpid(int pid, int *status, int options);
extern void _exit(int status);

static void
out(const char *format, ...)
{
	char line[512];
	va_list ap;
	int n;

	va_start(ap, format);
	n = vsnprintf(line, sizeof(line), format, ap);
	va_end(ap);

	if (n > 0) {
		write(2, line, (n < (int)sizeof(line)) ? (unsigned long)n : (unsigned long)(sizeof(line) - 1));
	}
}

static int
contains(const char *haystack, const char *needle)
{
	const char *h;
	const char *n;

	for (h = haystack; *h; ++h) {
		for (n = needle; *n && h[n - needle] == *n; ++n) {
		}
		if (*n == '\0') {
			return 1;
		}
	}
	return 0;
}

/* XNU stringizes the panic argument and appends "@file:line", so match on the assertion text
 * itself rather than on the whole line. */
#define PANIC_NOT_OWNED "not owned but expected to be owned"

/* ---------------------------------------------------------------- stubs */

enum { CTX_NONE = 0, CTX_A = 1, CTX_B = 2 };

static dtape_thread_t thread_a;
static dtape_thread_t thread_b;
static int current_ctx = CTX_A;

thread_t
current_thread(void)
{
	switch (current_ctx) {
	case CTX_A: return (thread_t)&thread_a.xnu_thread;
	case CTX_B: return (thread_t)&thread_b.xnu_thread;
	default:    return NULL;
	}
}

dtape_thread_t *
dtape_thread_for_xnu_thread_unused(thread_t xthread)
{
	if (xthread == NULL) {
		return NULL;
	}
	if (xthread == (thread_t)&thread_a.xnu_thread) {
		return &thread_a;
	}
	if (xthread == (thread_t)&thread_b.xnu_thread) {
		return &thread_b;
	}
	return NULL;
}

/* The assertion is the subject of this contract, so a firing assertion is a RESULT, not a crash.
 * XNU declares panic as a macro that invokes (panic)(...) with a "@file:line" suffix, so the macro
 * must be undefined here to define the function, and callers compare by prefix. */
#undef panic

void
panic(const char *fmt, ...)
{
	char msg[256];
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);

	out("DTAPE-MUTEX-OWNERSHIP panic: %s\n", msg);
	_exit(contains(msg, PANIC_NOT_OWNED) ? 3 : 4);
}

void
dtape_log(dtape_log_level_t level, const char *format, ...)
{
	(void)level;
	(void)format;
}

/* ------------------------------------------------------------- harness */

static dtape_mutex_t contract_mutex;
static int suspends;
static int resumes;
static dtape_mutex_t *suspend_mutex;
static int suspend_release_as = CTX_NONE;

static void
stub_thread_suspend(void *thread_context, dtape_thread_continuation_callback_f continuation_callback,
		void *continuation_context, libsimple_lock_t *unlock_me)
{
	(void)thread_context;
	(void)continuation_callback;
	(void)continuation_context;

	if (++suspends > 5) {
		out("DTAPE-MUTEX-OWNERSHIP guard=suspend_loop\n");
		_exit(6);
	}

	/* Documented contract: the hook unlocks the lock handed to it once the microthread is
	 * suspended, and the waker resumes it later. */
	if (unlock_me) {
		libsimple_lock_unlock(unlock_me);
	}

	/* Model the waker: the recorded owner releases the mutex from ITS context, which dequeues the
	 * parked link and calls thread_resume. */
	if (suspend_mutex) {
		int saved = current_ctx;
		current_ctx = suspend_release_as;
		dtape_mutex_unlock(suspend_mutex);
		current_ctx = saved;
	}
}

static void
stub_thread_resume(void *thread_context)
{
	(void)thread_context;
	++resumes;
}

static dtape_hooks_t contract_hooks;

const dtape_hooks_t *dtape_hooks = &contract_hooks;

/* ---------------------------------------------------------------- arms */

enum {
	ARM_LEGAL_ACTIVE = 1,
	ARM_LEGAL_FALLBACK = 2,
	ARM_ILLEGAL_FALLBACK_THEN_ACTIVE = 3,
	ARM_ILLEGAL_A_THEN_B = 4,
	ARM_CONTENDED = 5,
};

static void
run_arm(int which)
{
	dtape_mutex_init(&contract_mutex);

	switch (which) {
	case ARM_LEGAL_ACTIVE:
		current_ctx = CTX_A;
		dtape_mutex_lock(&contract_mutex);
		dtape_mutex_unlock(&contract_mutex);
		break;

	case ARM_LEGAL_FALLBACK:
		current_ctx = CTX_NONE;
		dtape_mutex_lock(&contract_mutex);
		dtape_mutex_unlock(&contract_mutex);
		break;

	case ARM_ILLEGAL_FALLBACK_THEN_ACTIVE:
		current_ctx = CTX_NONE;
		dtape_mutex_lock(&contract_mutex);
		current_ctx = CTX_A;
		dtape_mutex_unlock(&contract_mutex);
		break;

	case ARM_ILLEGAL_A_THEN_B:
		current_ctx = CTX_A;
		dtape_mutex_lock(&contract_mutex);
		current_ctx = CTX_B;
		dtape_mutex_unlock(&contract_mutex);
		break;

	case ARM_CONTENDED:
		current_ctx = CTX_A;
		dtape_mutex_lock(&contract_mutex);
		suspend_mutex = &contract_mutex;
		suspend_release_as = CTX_A;
		current_ctx = CTX_B;
		dtape_mutex_lock(&contract_mutex);   /* suspends; the stub releases as A and resumes B */
		dtape_mutex_unlock(&contract_mutex);
		suspend_mutex = NULL;
		if (suspends == 0) {
			_exit(5);
		}
		break;
	}

	_exit(0);
}

static const char *
arm_name(int which)
{
	switch (which) {
	case ARM_LEGAL_ACTIVE: return "legal-active-thread-pair";
	case ARM_LEGAL_FALLBACK: return "legal-fallback-pair";
	case ARM_ILLEGAL_FALLBACK_THEN_ACTIVE: return "illegal-fallback-acquire-then-active-release";
	case ARM_ILLEGAL_A_THEN_B: return "illegal-acquire-a-release-b";
	case ARM_CONTENDED: return "legal-contended-with-suspension";
	default: return "?";
	}
}

int
main(void)
{
	static const int arms[] = {
		ARM_LEGAL_ACTIVE,
		ARM_LEGAL_FALLBACK,
		ARM_ILLEGAL_FALLBACK_THEN_ACTIVE,
		ARM_ILLEGAL_A_THEN_B,
		ARM_CONTENDED,
	};
	static const int expected[] = { 0, 0, 3, 3, 0 };
	int failures = 0;
	unsigned i;

	contract_hooks.thread_suspend = stub_thread_suspend;
	contract_hooks.thread_resume = stub_thread_resume;

	for (i = 0; i < sizeof(arms) / sizeof(arms[0]); ++i) {
		int status = 0;
		int code;
		int pid = fork();

		if (pid == 0) {
			run_arm(arms[i]);
		}
		(void)waitpid(pid, &status, 0);
		code = (status >> 8) & 0xff;

		out("DTAPE-MUTEX-OWNERSHIP arm=%s exit=%d expected=%d\n",
				arm_name(arms[i]), code, expected[i]);

		if (code != expected[i]) {
			++failures;
			if (expected[i] == 3) {
				out("DTAPE-MUTEX-OWNERSHIP pass=0 reason=illegal_transition_not_detected arm=%s\n",
						arm_name(arms[i]));
			} else {
				out("DTAPE-MUTEX-OWNERSHIP pass=0 reason=legal_transition_rejected arm=%s\n",
						arm_name(arms[i]));
			}
		}
	}

	if (failures != 0) {
		out("DTAPE-MUTEX-OWNERSHIP pass=0 failures=%d\n", failures);
		return 1;
	}

	out("DTAPE-MUTEX-OWNERSHIP PASS\n");
	return 0;
}

/*
 * native_thread_identity_native.c -- the NATIVE (host ELF) half of the permanent oracle for the guest
 * false-identity defect (bead dar-liaq).
 *
 * WHAT THIS IS: a host shared object that the GUEST loads through the loader's own native-library API
 * (_elfcalls->dlopen, the same mechanism the product's native wrappers use) and which creates a thread with the
 * HOST libc's pthread_create. That thread is NOT created through Darling's guest-thread path, so nothing would
 * install a personal guest TSD base (ARCH_SET_GS) or an identity in TSD slot 3 (__TSD_MACH_THREAD_SELF, the word
 * libsystem_platform/libdispatch read at %gs:24) for it.
 *
 * WHAT IT REPORTS, for BOTH the creating and the created thread:
 *   gs_base       the raw GS base of that thread (arch_prctl(ARCH_GET_GS) -- the register the guest reads)
 *   slot3_native  the 8 bytes at gs_base+24, read by this native code with `movq %gs:24`
 *   slot3_guest   the same 8 bytes, read by the GUEST through the callback the guest passes in
 * so the two halves of the measurement are independent of each other.
 *
 * VERDICT (the contract):
 *   RED    the child's gs_base EQUALS the creator's and its slot 3 EQUALS the creator's live token: the child
 *          answers another live thread's identity, which is what makes libdispatch's ownership test
 *          (_dispatch_lock_is_locked_by(word, self), self = slot3 & 0xfffffffc) read a gate owned by a DIFFERENT
 *          thread as "mine" and trap with "trying to lock recursively".
 *   GREEN  the child has its OWN gs_base and reads slot 3 as no identity (0) or its own token -- never the
 *          creator's live token.
 *
 * Exit codes: 0 GREEN, 1 RED (contract violated), 2 the instrument could not measure (never confused with RED).
 *
 * Build (host, by tests/run-native-thread-identity.sh):
 *   cc -shared -fPIC -O0 -g -o libnatident.so native_thread_identity_native.c
 */
#define _GNU_SOURCE
#include <asm/prctl.h>
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#define NATIDENT_GREEN 0
#define NATIDENT_RED 1
#define NATIDENT_INSTRUMENT_FAILED 2

/* The guest's own reader of TSD slot 3 (`movq %gs:24` in the guest), passed in by the guest probe. */
static int (*guest_slot3_reader)(void);

/* One raw write per line, so the oracle's output cannot be lost to buffering in a process that is about to be
 * judged; the guest's own stdout/stderr are both captured by the runner. */
static void emit(const char* fmt, ...)
{
	char buf[512];
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);
	if (n > 0) {
		if ((size_t)n > sizeof(buf)) {
			n = (int)sizeof(buf);
		}
		(void)!write(2, buf, (size_t)n);
	}
}

/* The native view of this thread's GS base. Report the raw failure instead of a plausible zero when the syscall is
 * unavailable: an instrument that answers "0" for "cannot read" is the defect this oracle exists to avoid. */
static unsigned long native_gs_base(int* out_rc, int* out_errno)
{
	unsigned long gs = 0;
	errno = 0;
	long rc = syscall(SYS_arch_prctl, ARCH_GET_GS, &gs);
	*out_rc = (int)rc;
	*out_errno = errno;
	return rc == 0 ? gs : 0;
}

static unsigned long native_slot3(void)
{
	unsigned long v = 0;
	__asm__ volatile("movq %%gs:24, %0" : "=r"(v));
	return v;
}

struct gs_report {
	int tid;
	int rc;
	int err;
	unsigned long gs_base;
	unsigned long slot3_native;
	unsigned long slot3_guest;
};

static void measure(struct gs_report* r)
{
	r->tid = (int)syscall(SYS_gettid);
	r->gs_base = native_gs_base(&r->rc, &r->err);
	r->slot3_native = native_slot3();
	r->slot3_guest = guest_slot3_reader ? (unsigned long)guest_slot3_reader() : 0;
}

static void* native_child(void* arg)
{
	measure((struct gs_report*)arg);
	return NULL;
}

static void print_report(const char* tag, const struct gs_report* r)
{
	emit("NATIDENT-%-7s tid=%d gs_base=0x%lx (arch_prctl rc=%d errno=%d) slot3_native=0x%lx slot3_guest=0x%lx\n",
	    tag, r->tid, r->gs_base, r->rc, r->err, r->slot3_native, r->slot3_guest);
}

int natident_probe(int (*slot3_reader)(void))
{
	guest_slot3_reader = slot3_reader;

	struct gs_report creator = {0};
	measure(&creator);
	print_report("CREATOR", &creator);
	if (creator.rc != 0) {
		emit("NATIDENT-VERDICT INSTRUMENT-FAILED arch_prctl(ARCH_GET_GS) failed for the creator (errno=%d)\n",
		    creator.err);
		return NATIDENT_INSTRUMENT_FAILED;
	}
	if (creator.slot3_guest == 0 && creator.slot3_native == 0) {
		/* A creator with no identity of its own cannot show inheritance: the child reading zero would then be
		 * indistinguishable from a child that inherited nothing. Refuse to judge rather than report a GREEN
		 * that measured nothing. */
		emit("NATIDENT-VERDICT INSTRUMENT-FAILED the creating thread carries no identity (slot3=0), so "
		     "inheritance cannot be measured\n");
		return NATIDENT_INSTRUMENT_FAILED;
	}

	struct gs_report child = {0};
	pthread_t t;
	int rc = pthread_create(&t, NULL, native_child, &child);
	if (rc != 0) {
		emit("NATIDENT-VERDICT INSTRUMENT-FAILED pthread_create rc=%d (%s)\n", rc, strerror(rc));
		return NATIDENT_INSTRUMENT_FAILED;
	}
	pthread_join(t, NULL);
	print_report("CHILD", &child);
	if (child.rc != 0) {
		emit("NATIDENT-VERDICT INSTRUMENT-FAILED arch_prctl(ARCH_GET_GS) failed for the child (errno=%d)\n",
		    child.err);
		return NATIDENT_INSTRUMENT_FAILED;
	}

	/* The defect is not "the child has a gs_base": it is that the child ANSWERS A LIVE TOKEN that belongs to
	 * another thread -- which is what makes a once/lock guard trap on "already locked by self". */
	int shares_gs = (creator.gs_base != 0 && child.gs_base == creator.gs_base);
	int same_live_token = (child.slot3_guest != 0 && child.slot3_guest == creator.slot3_guest);
	int zero_identity = (child.slot3_guest == 0 && child.slot3_native == 0);
	const char* verdict;
	int rc_out;
	if (same_live_token) {
		verdict = "RED-false-identity";
		rc_out = NATIDENT_RED;
	} else if (zero_identity) {
		verdict = "GREEN-no-identity";
		rc_out = NATIDENT_GREEN;
	} else if (!shares_gs) {
		verdict = "GREEN-own-gs-base";
		rc_out = NATIDENT_GREEN;
	} else {
		verdict = "GREEN-shares-gs-base-no-live-token";
		rc_out = NATIDENT_GREEN;
	}

	emit("NATIDENT-VERDICT %s child_shares_gs_base=%d child_answers_creator_token=%d child_identity_is_zero=%d "
	     "creator_gs_base=0x%lx child_gs_base=0x%lx creator_token=0x%lx child_token=0x%lx\n",
	    verdict, shares_gs, same_live_token, zero_identity,
	    creator.gs_base, child.gs_base, creator.slot3_guest, child.slot3_guest);
	emit("NATIDENT-PROBE-RC=%d\n", rc_out);
	return rc_out;
}

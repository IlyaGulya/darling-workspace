/*
 * nsdate_now_guest.c -- permanent guest oracle for +[NSDate now], the class property Apple's NSDate.h
 * declares in the NSDateCreation category (macOS 10.15 / iOS 13).
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder (scripts/xcodebuild-workload-run.sh, both workloads)
 * stopped in the build tool's own ERROR-REPORTING path after the earlier rungs were fixed:
 *
 *   ** INTERNAL ERROR: Uncaught exception **
 *   Uncaught Exception: +[NSDate now]: unrecognized selector sent to instance 0x7FC9845CBE40
 *   Stack:
 *     4   +[NSObject(NSObject) doesNotRecognizeSelector:] (in CoreFoundation)
 *     5   ___forwarding___ (in CoreFoundation)
 *     6   _CF_forwarding_prep_0 (in CoreFoundation)
 *     7   -[Xcode3CommandLineBuildTool resolveErrorTriageResultBundleDestination] (in Xcode3Core)
 *     8   -[Xcode3CommandLineBuildTool saveResultBundleForError:triageWithSchemeTask:error:] (in Xcode3Core)
 *     9   -[Xcode3CommandLineBuildTool _printError:schemeTask:andFailWithCode:] (in Xcode3Core)
 *    10   -[Xcode3CommandLineBuildTool _printErrorString:andFailWithCode:] (in Xcode3Core)
 *    11   -[Xcode3CommandLineBuildTool run] (in Xcode3Core)
 *    12   XcodeBuildMain (in libxcodebuildLoader.dylib)
 *   [sigexc-fatal sig=4 code=2 addr=0x7FC982B0847F pid=1369742 tid=1369742]
 *   XCB_RC=132
 *
 * The class object is CoreFoundation's; the accessor is declared and implemented in Foundation.framework
 * on Darwin (NSDate.h groups it with +date, +distantFuture and +distantPast in NSDateCreation).  The
 * deployed Foundation carried no `now` selector string at all, so the error the tool was trying to print
 * was hidden behind the missing API.
 *
 * WHAT THIS MEASURES, in one process, with no compile-time reference to Foundation or ObjC
 * (dlopen + objc_msgSend + sel_registerName only), on the DEPLOYED Foundation:
 *
 *   1. +[NSDate now] must respond and return a non-nil NSDate.  In the unfixed runtime the send raises
 *      the unrecognized-selector NSException, which is the RED arm's own line.
 *   2. The instance must be a valid CURRENT date: its timeIntervalSinceReferenceDate agrees with
 *      +[NSDate date] within a sane delta AND with the process's own wall clock (time(2)) within the
 *      same delta, so a date that merely "is a date" cannot pass.
 *   3. It must NOT be a sentinel: not the reference-date zero, not distantFuture, not distantPast, and
 *      not the same object as either of those (which is how a lazy "return distantFuture" would pass
 *      a value-only check).
 *
 * Every intermediate fact is printed unconditionally as an NSDATE-* line, so a RED arm quotes the
 * failure from the run's own output; NSDATE_NOW_VERDICT=GREEN is the machine-readable line the harness
 * ok-marker reads.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the unfixed Foundation answered the send with
 * the unrecognized-selector exception above.
 */

#include <dlfcn.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"

/* NSTimeIntervalSince1970 and the two distant bounds, from NSDate.h / the tree's NSDate.m. */
#define NSTIMEINTERVAL_SINCE_1970 978307200.0
#define DISTANT_FUTURE 63113904000.0
#define DISTANT_PAST (-63114076800.0)

/* The delta inside which the class's own +date and the process wall clock must agree with +now. */
#define SANE_DELTA 5.0

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);
static const char *(*classNameFn)(void *);

static int failures;

static void *lookup(void *handle, const char *name)
{
	void *found = dlsym(handle, name);
	if (!found) {
		char underscored[128];
		snprintf(underscored, sizeof underscored, "_%s", name);
		found = dlsym(handle, underscored);
	}
	if (!found && name[0] == '_') {
		found = dlsym(handle, name + 1);
	}
	if (!found) {
		printf("NSDATE-NOTE symbol=%s error=%s\n", name, dlerror());
	}
	return found;
}

static void *send(void *object, const char *selector)
{
	return msgSend(object, selRegister(selector));
}

/* A double-returning send: the value comes back in xmm0, so a void* prototype would read garbage. */
static double send_double(void *object, const char *selector)
{
	union {
		void *(*plain)(void *, void *, ...);
		double (*d)(void *, void *);
	} pun;
	pun.plain = msgSend;
	return pun.d(object, selRegister(selector));
}

/* A BOOL (signed char) returning send. */
static int send_bool(void *object, const char *selector, void *argument)
{
	union {
		void *(*plain)(void *, void *, ...);
		signed char (*b)(void *, void *, void *);
	} pun;
	pun.plain = msgSend;
	return pun.b(object, selRegister(selector), argument);
}

static void check(const char *name, int ok, const char *detail)
{
	printf("NSDATE-CHECK name=%s ok=%d %s\n", name, ok, detail ? detail : "");
	if (!ok) {
		failures++;
	}
}

int main(int argc, char **argv)
{
	void *objc;
	void *foundation;

	(void)argc;
	(void)argv;
	setvbuf(stdout, NULL, _IOLBF, 0);

	objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation) {
		printf("NSDATE-FAIL stage=dlopen objc=%p foundation=%p err=%s\n", objc, foundation, dlerror());
		printf("NSDATE_NOW_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))lookup(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))lookup(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))lookup(objc, "objc_getClass");
	classNameFn = (const char *(*)(void *))lookup(objc, "object_getClassName");
	if (!msgSend || !selRegister || !getClassFn || !classNameFn) {
		printf("NSDATE-FAIL stage=runtime-symbols\n");
		printf("NSDATE_NOW_VERDICT=RED\n");
		return 1;
	}

	void *date_class = getClassFn("NSDate");
	if (!date_class) {
		date_class = lookup(foundation, "_OBJC_CLASS_$_NSDate");
	}
	if (!date_class) {
		printf("NSDATE-FAIL stage=class NSDate=(none) image-foundation=%p\n", foundation);
		printf("NSDATE_NOW_VERDICT=RED\n");
		return 1;
	}

	/* Reported, not gated before the send: the contract is that the send WORKS.  In the unfixed runtime
	 * this is 0 and the send below raises the unrecognized-selector exception, which is the RED line. */
	void *now_sel = selRegister("now");
	/* +respondsToSelector: is a CLASS method, so it must be sent to the class object (which checks the
	 * metaclass); class_respondsToSelector(date_class, sel) would ask about INSTANCE methods and answer 0
	 * for a class method.  Reported, not gated before the send: the contract is that the send WORKS. */
	int responds = send_bool(date_class, "respondsToSelector:", now_sel);
	printf("NSDATE-WALL class=%s respondsToSelector:now=%d image-foundation=%p\n",
	    classNameFn(date_class), responds, foundation);

	/* The wall's own call, unpredicated: +[NSDate now]. */
	void *now = msgSend(date_class, now_sel);

	/* The two references the contract is measured against. */
	void *date = send(date_class, "date");
	void *future = send(date_class, "distantFuture");
	void *past = send(date_class, "distantPast");
	time_t wall = time(NULL);

	if (!now) {
		printf("NSDATE-FAIL stage=call now=(nil)\n");
		printf("NSDATE_NOW_VERDICT=RED\n");
		return 1;
	}

	double t_now = send_double(now, "timeIntervalSinceReferenceDate");
	double t_date = date ? send_double(date, "timeIntervalSinceReferenceDate") : 0.0;
	double t_wall = (double)wall - NSTIMEINTERVAL_SINCE_1970;
	int is_date = send_bool(now, "isKindOfClass:", date_class);

	printf("NSDATE-NOW class=%s timeInterval=%.6f\n", classNameFn(now), t_now);
	printf("NSDATE-REF date=%.6f wall=%.6f distantFuture-object=%p distantPast-object=%p\n",
	    t_date, t_wall, future, past);

	char detail[160];

	snprintf(detail, sizeof detail, "class=%s", classNameFn(now));
	check("is-nsdate", is_date, detail);

	snprintf(detail, sizeof detail, "timeInterval=%.6f", t_now);
	check("not-reference-zero", t_now != 0.0, detail);
	check("not-distant-future", t_now != DISTANT_FUTURE, detail);
	check("not-distant-past", t_now != DISTANT_PAST, detail);

	snprintf(detail, sizeof detail, "now=%.6f date=%.6f delta=%.6f", t_now, t_date, fabs(t_now - t_date));
	check("agrees-with-date", fabs(t_now - t_date) < SANE_DELTA, detail);

	snprintf(detail, sizeof detail, "now=%.6f wall=%.6f delta=%.6f", t_now, t_wall, fabs(t_now - t_wall));
	check("agrees-with-wallclock", fabs(t_now - t_wall) < SANE_DELTA, detail);

	check("distinct-from-distant-future", now != future, NULL);
	check("distinct-from-distant-past", now != past, NULL);

	if (failures != 0) {
		printf("NSDATE-FAIL failures=%d\n", failures);
		printf("NSDATE_NOW_VERDICT=RED\n");
		return 1;
	}
	printf("NSDATE_NOW_VERDICT=GREEN\n");
	return 0;
}

/*
 * string_contents_file_guest.c -- workspace oracle (add_compat_test) for dar-liaq.
 *
 * WHAT IT MEASURES.  The two NSString members that read a file into a string WITHOUT naming an
 * encoding, and the two that do name one, one case per process invocation:
 *
 *   instance-deprecated  -[NSString initWithContentsOfFile:]                 (deprecated)
 *   class-deprecated     +[NSString stringWithContentsOfFile:]               (deprecated; the class
 *                                                                             member delegates to the
 *                                                                             instance one)
 *   class-deprecated-bom +[NSString stringWithContentsOfFile:] on a UTF-8 BOM file (informational)
 *   class-encoding       +[NSString stringWithContentsOfFile:encoding:error:]  (control)
 *   instance-encoding    -[NSString initWithContentsOfFile:encoding:error:]    (control)
 *
 * WHY.  Xcode 26.3's own images send the deprecated instance member: 23 references of
 * `initWithContentsOfFile:` across 7 images of the workload's 64-image load closure (DevToolsCore,
 * DevToolsSupport, IDEFoundation, DVTFoundation, DVTInstrumentsUtilities, Localization,
 * SymbolicationDT).  On the bytes deployed before this rung's fix that send was FATAL:
 * -_initWithBytesOfUnknownEncoding:length:copy:usedEncoding: (src/NSString.m) freed the bytes it
 * had just copied, and on this path those bytes are the NSData's own buffer, so the NSData's
 * deallocator freed the same pointer again (double free -> abort -> SIGILL, rc 132).
 *
 * WHY THERE IS NO fork() HERE, AND WHAT THE FIRST ARM REALLY DIED OF.  The first version of this
 * fixture isolated its cases with fork(), and every case -- controls included -- reported
 * `DIED signal=11`; the arm was read as the fixture's own fork() usage.  It was not fork().  The
 * fixture -- like the fork-based one before it -- passed the file's path as a C string to the send,
 * and the path argument is an NSString: the implementation's first `[path UTF8String]` dispatched to
 * a byte buffer and the process died of SIGSEGV at an address inside the path text (measured: the
 * signal's fault address in `red-arm-pre-fix.txt` lies in the fixture's own path literal).  The
 * spawn and waitpid calls were innocent, and the arm measured neither the members nor the runtime.
 * Isolation is not needed for attribution anyway: each case is a separate process, because the CTest
 * entry for each case invokes this binary with that case's name as argv[1] (`add_compat_test(... ARGS
 * <case>)`).  A trap in one case cannot affect another, and a case that dies takes only its own test
 * down -- which is exactly what a RED arm should show.  Without argv[1] the cases run in the order
 * below INSIDE one process (useful for a manual probe; on pre-fix bytes that process dies at
 * `class-deprecated` after the controls have printed ok).
 *
 * The verdict line is the fixture's own and is the per-case CTest OK_MARKER:
 * STRING_CONTENTS_VERDICT=GREEN only when the case's assertions held.  Each case proves the file
 * was read back byte for byte (`-UTF8String` equals the bytes written, `-length` equals their
 * count), and the two controls (which the deprecated members delegate to the same reader as) are
 * registered as their own entries so an arm where they fail cannot be read as a member failure.
 *
 * The BOM case is the one exception: it asserts only that the send answered a string and did not
 * trap.  Whether the leading U+FEFF is stripped is a separate question this rung did not set out to
 * settle (MEASURED post-fix: the send answers 10 characters starting at U+0068, so the leading BOM
 * is consumed rather than kept), so the case reports the length and the first code unit instead of
 * asserting Apple's documented BOM behaviour.
 *
 * dlopen + dlsym + plain I/O only: no compile-time reference to Foundation, so the RED arm
 * measures the deployed runtime and not the headers in this tree.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"

#define TMPDIR_GUEST "/private/var/tmp"
#define EXPECTED "hello file contents\n"
#define EXPECTED_LEN 20

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);

static void *s_(const char *n) { return selRegister(n); }

static void *NSClass(const char *name) { return getClassFn(name); }

static const char *CStr(void *obj)
{
	if (!obj) return "(nil)";
	const char *p = (const char *)msgSend(obj, s_("UTF8String"));
	return p ? p : "(no-utf8)";
}

static long Len(void *obj) { return obj ? (long)msgSend(obj, s_("length")) : -1L; }

static int responds(void *obj, const char *name)
{
	return (int)(long)msgSend(obj, s_("respondsToSelector:"), s_(name));
}

static int write_file(const char *path, const char *text)
{
	FILE *f = fopen(path, "w");
	if (!f) return -1;
	fputs(text, f);
	fclose(f);
	return 0;
}

/* The path argument these members take is an NSString, NOT a C string.  The first version of this
 * fixture (and the fork-based one before it) passed the C path buffer straight to the send, so the
 * implementation's very first `[path UTF8String]` dispatched to a byte buffer and every case --
 * controls included -- died of signal 11 at an address inside the path text.  That is what the
 * earlier "every case DIED" arm measured; it was never fork().  Build the object here. */
static void *NSStringFromCStr(const char *s)
{
	return msgSend(NSClass("NSString"), s_("stringWithUTF8String:"), (void *)s);
}

static char g_plain[256];
static char g_bom[256];

/* One assertion, one line.  Printed from inside the read-back so a dying process leaves the last
 * `STRING_CONTENTS_SEND` line as the record of which send killed it. */
static int report_string(const char *case_name, void *str)
{
	if (!str) {
		printf("STRING_CONTENTS_CASE %s string=nil\n", case_name);
		return 1;
	}
	const char *text = CStr(str);
	long len = Len(str);
	if (strcmp(text, EXPECTED) == 0 && len == EXPECTED_LEN) {
		printf("STRING_CONTENTS_CASE %s ok len=%ld text=expected\n", case_name, len);
		return 0;
	}
	printf("STRING_CONTENTS_CASE %s WRONG len=%ld text=<%s>\n", case_name, len, text);
	return 1;
}

/* ---- the cases --------------------------------------------------------------------- */

static int case_class_deprecated(void)
{
	void *path = NSStringFromCStr(g_plain);
	if (!path) {
		printf("STRING_CONTENTS_CASE class-deprecated path=nil\n");
		return 1;
	}
	printf("STRING_CONTENTS_SEND class-deprecated +stringWithContentsOfFile: %s\n", g_plain);
	fflush(stdout);
	return report_string("class-deprecated",
			     msgSend(NSClass("NSString"), s_("stringWithContentsOfFile:"), path));
}

static int case_instance_deprecated(void)
{
	void *alloced = msgSend(NSClass("NSString"), s_("alloc"));
	void *path = NSStringFromCStr(g_plain);
	if (!alloced || !path) {
		printf("STRING_CONTENTS_CASE instance-deprecated alloc=%s path=%s\n",
		       alloced ? "ok" : "nil", path ? "ok" : "nil");
		return 1;
	}
	printf("STRING_CONTENTS_SEND instance-deprecated -initWithContentsOfFile: %s\n", g_plain);
	fflush(stdout);
	return report_string("instance-deprecated",
			     msgSend(alloced, s_("initWithContentsOfFile:"), path));
}

static int case_class_deprecated_bom(void)
{
	void *path = NSStringFromCStr(g_bom);
	if (!path) {
		printf("STRING_CONTENTS_CASE class-deprecated-bom path=nil\n");
		return 1;
	}
	printf("STRING_CONTENTS_SEND class-deprecated-bom +stringWithContentsOfFile: %s\n", g_bom);
	fflush(stdout);
	void *str = msgSend(NSClass("NSString"), s_("stringWithContentsOfFile:"), path);
	if (!str) {
		printf("STRING_CONTENTS_CASE class-deprecated-bom string=nil\n");
		return 1;
	}
	long len = Len(str);
	unsigned short first = 0;
	if (len > 0) first = (unsigned short)(long)msgSend(str, s_("characterAtIndex:"), (void *)0L);
	printf("STRING_CONTENTS_CASE class-deprecated-bom ok len=%ld first=U+%04X "
	       "(informational: BOM stripping is not asserted)\n", len, (unsigned)first);
	return 0;
}

static int case_class_encoding(void)
{
	void *path = NSStringFromCStr(g_plain);
	if (!path) {
		printf("STRING_CONTENTS_CASE class-encoding path=nil\n");
		return 1;
	}
	printf("STRING_CONTENTS_SEND class-encoding +stringWithContentsOfFile:encoding:error:\n");
	fflush(stdout);
	return report_string("class-encoding",
			     msgSend(NSClass("NSString"), s_("stringWithContentsOfFile:encoding:error:"),
				     path, (void *)4 /* NSUTF8StringEncoding */, (void *)0));
}

static int case_instance_encoding(void)
{
	void *alloced = msgSend(NSClass("NSString"), s_("alloc"));
	void *path = NSStringFromCStr(g_plain);
	if (!alloced || !path) {
		printf("STRING_CONTENTS_CASE instance-encoding alloc=%s path=%s\n",
		       alloced ? "ok" : "nil", path ? "ok" : "nil");
		return 1;
	}
	printf("STRING_CONTENTS_SEND instance-encoding -initWithContentsOfFile:encoding:error:\n");
	fflush(stdout);
	return report_string("instance-encoding",
			     msgSend(alloced, s_("initWithContentsOfFile:encoding:error:"),
				     path, (void *)4, (void *)0));
}

struct case_def {
	const char *name;
	int (*body)(void);
};

static const struct case_def g_cases[] = {
	/* The controls first: without argv[1] this order is what a manual pre-fix probe prints before
	 * the deprecated member it dies in.  Both controls read through the SAME
	 * _NSReadBytesFromFile/NSData path as the deprecated members, differing only in that they
	 * name the encoding, so they bound what "the file read works" means on this runtime. */
	{ "class-encoding", case_class_encoding },
	{ "instance-encoding", case_instance_encoding },
	{ "class-deprecated", case_class_deprecated },
	{ "instance-deprecated", case_instance_deprecated },
	{ "class-deprecated-bom", case_class_deprecated_bom },
};

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);

	void *objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	void *foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation) {
		printf("ORACLE_RC=2 dlopen failed: %s\n", dlerror());
		return 2;
	}
	msgSend = (void *(*)(void *, void *, ...))dlsym(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(objc, "objc_getClass");
	if (!msgSend || !selRegister || !getClassFn) {
		printf("ORACLE_RC=2 objc runtime symbols missing\n");
		return 2;
	}

	snprintf(g_plain, sizeof g_plain, TMPDIR_GUEST "/string-contents-plain.%d.txt", (int)getpid());
	snprintf(g_bom, sizeof g_bom, TMPDIR_GUEST "/string-contents-bom.%d.txt", (int)getpid());

	printf("STRING-CONTENTS classes NSString=%p Foundation=%p\n",
	       NSClass("NSString"), foundation);
	printf("STRING-CONTENTS responds class stringWithContentsOfFile:=%d "
	       "stringWithContentsOfFile:encoding:error:=%d\n",
	       responds(NSClass("NSString"), "stringWithContentsOfFile:"),
	       responds(NSClass("NSString"), "stringWithContentsOfFile:encoding:error:"));
	void *probeInstance = msgSend(NSClass("NSString"), s_("alloc"));
	printf("STRING-CONTENTS responds instance initWithContentsOfFile:=%d "
	       "initWithContentsOfFile:encoding:error:=%d%s\n",
	       responds(probeInstance, "initWithContentsOfFile:"),
	       responds(probeInstance, "initWithContentsOfFile:encoding:error:"),
	       probeInstance ? "" : " (alloc answered nil!)");

	if (write_file(g_plain, EXPECTED) != 0 || write_file(g_bom, "\xEF\xBB\xBFhello bom\n") != 0) {
		printf("ORACLE_RC=2 cannot write the fixture files under %s\n", TMPDIR_GUEST);
		return 2;
	}

	const char *only = argc > 1 ? argv[1] : NULL;
	int ran = 0, failures = 0;
	for (size_t i = 0; i < sizeof g_cases / sizeof g_cases[0]; i++) {
		if (only != NULL && strcmp(only, g_cases[i].name) != 0) continue;
		ran++;
		printf("STRING_CONTENTS_RUN %s\n", g_cases[i].name);
		fflush(stdout);
		failures += g_cases[i].body();
	}
	if (!ran) {
		printf("STRING_CONTENTS_UNKNOWN_CASE %s\n", only);
		printf("STRING_CONTENTS_VERDICT=RED\n");
		return 2;
	}

	unlink(g_plain);
	unlink(g_bom);

	printf("STRING_CONTENTS_RAN cases=%d failures=%d\n", ran, failures);
	printf("STRING_CONTENTS_VERDICT=%s\n", failures == 0 ? "GREEN" : "RED");
	return failures == 0 ? 0 : 8;
}

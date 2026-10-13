/*
 * dateformatter_guest.c -- permanent guest oracle for the CFDateFormatter/NSDateFormatter
 * path Xcode3Core's error-printing code uses, and the diagnostic that names the NULL the
 * ladder crashed on.
 *
 * WHY THIS EXISTS. After +[NSDate now] was added (the previous rung), the Xcode compatibility
 * ladder (scripts/xcodebuild-workload-run.sh, both workloads) died one frame later in the SAME
 * error-reporting path:
 *
 *   CORE-SIGNAL sig=11 SIGSEGV
 *   CORE-FAULT  .../CoreFoundation base=0x... file+0x3a0986
 *               symbol=CFDateFormatterCreateStringWithAbsoluteTime + 0xd6
 *   CORE-INSN   48 8b 78 10   (movq 0x10(%rax),%rdi; rax NULL => si_addr 0x10)
 *     #0 CFDateFormatterCreateStringWithDate + 0x75      (CoreFoundation)
 *     #1 -[NSDateFormatter stringFromDate:] + 0x24a      (Foundation; DWARF-resolved at vm 0x1cb4a)
 *     #2 __33-[Xcode3CommandLineBuildTool ...]           (Xcode3Core, saveResultBundleForError:triage path)
 *     #7 XcodeBuildMain                                  (libxcodebuildLoader.dylib)
 *   XCB_RC=139
 *
 * THE INSTRUCTION IDENTIFIES THE NULL. Disassembled from the DEPLOYED CoreFoundation copy
 * (llvm-objdump --macho --disassemble .../CoreFoundation):
 *
 *   _CFDateFormatterCreateStringWithAbsoluteTime:           ; 0x678b0
 *     ...
 *     0x678d0: 48 89 b5 d8 f9 ff ff  movq %rsi, -0x628(%rbp)  ; save the FORMATTER (arg 2)
 *     ...
 *     0x6797f: 48 8b 85 d8 f9 ff ff  movq -0x628(%rbp), %rax  ; rax = formatter
 *     0x67986: 48 8b 78 10           movq 0x10(%rax), %rdi    ; rdi = formatter->_df
 *     0x6798a: ...                   (moves ud, buffer, ... and calls _udat_format)
 *
 * 0x67986 is exactly the reported +0xd6, and struct __CFDateFormatter (CFDateFormatter.c:217)
 * is `CFRuntimeBase _base;` (16 bytes) `UDateFormat *_df;` so _df is at offset 0x10. si_addr
 * 0x10 therefore means rax == 0: the FORMATTER OBJECT ITSELF is NULL -- not its _df, not a
 * locale or format handle. `-[NSDateFormatter stringFromDate:]` (deployed Foundation, 0x1cb45)
 * loads the `_formatter` ivar and passes it verbatim; `_formatter` is set only by
 * `-[NSDateFormatter _regenerateFormatter]` (Foundation/src/NSDateFormatter.m), which leaves it
 * NULL when `CFDateFormatterCreate` returns NULL (it logs "Date Formatter creation failed" and
 * returns). So the question this fixture answers is: DID CFDateFormatterCreate RUN, and WHY did
 * it return NULL?
 *
 * WHAT THIS MEASURES, in one process, with no compile-time reference to Foundation, ObjC or ICU
 * (dlopen + dlsym + objc_msgSend + sel_registerName only), on the DEPLOYED runtime:
 *
 *   1. The locale inputs: [NSLocale currentLocale] and CFLocaleGetSystem(), their identifiers.
 *   2. ICU `udat_open` DIRECTLY, for the tree's own style/locale combinations, so a failure is
 *      attributed to ICU (data missing) vs to the locale string our CFLocale supplies.
 *   3. `CFDateFormatterCreate` (the call `_regenerateFormatter` makes), for the same inputs.
 *   4. `-[NSDateFormatter stringFromDate:]` through the same call path Xcode uses, with a FIXED
 *      known date, and asserts the produced string's SHAPE: non-empty, longer than a bare year,
 *      containing the input year and a time separator -- the requested Medium/Medium style.
 *
 * Every intermediate fact is printed unconditionally as a DF-* line, so a RED arm quotes the
 * failure from the run's own output; DF_VERDICT=GREEN is the machine-readable line the harness
 * ok-marker reads.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the unfixed CoreFoundation's
 * CFDateFormatterCreate returned NULL for the locale/style Xcode passes (the DF-CREATE line),
 * and the fixture's stringFromDate: call then dies with the same SIGSEGV the ladder showed.
 */

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define CF_LIBRARY "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
#define ICU_LIBRARY "/usr/lib/libicucore.A.dylib"

/* CFDateFormatterStyle (include/CoreFoundation/CFDateFormatter.h:45). */
#define kNoStyle 0
#define kShortStyle 1
#define kMediumStyle 2
#define kLongStyle 3
#define kFullStyle 4

/* ICU UDateFormatStyle names used inside __ResetUDateFormat (CFDateFormatter.c:598). */
#define UDAT_NONE 0
#define UDAT_SHORT 1
#define UDAT_MEDIUM 2
#define UDAT_LONG 3
#define UDAT_FULL 4

/* 2021-06-15T12:00:00Z as NSTimeInterval since the 2001 reference date. */
#define KNOWN_DATE 645451200.0

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);

/* CoreFoundation, resolved by C name. */
static void *(*CFDateFormatterCreate)(void *, void *, long, long);
static void *(*CFDateFormatterCreateStringWithDate)(void *, void *, void *);
static void *(*CFLocaleGetSystem)(void);
static void *(*CFLocaleGetIdentifier)(void *);
static int (*CFStringGetCString)(void *, char *, long, unsigned);
static void *(*CFDateCreate)(void *, double);
static double (*CFDateGetAbsoluteTime)(void *);

/* ICU, same entry point __ResetUDateFormat uses (__cficu_udat_open == udat_open). */
static void *(*udat_open)(int, int, const char *, const uint16_t *, int32_t, const uint16_t *, int32_t, int32_t *);

static int failures;

static void *lookup(void *handle, const char *name)
{
	void *found = dlsym(handle, name);
	if (!found) {
		char underscored[160];
		snprintf(underscored, sizeof underscored, "_%s", name);
		found = dlsym(handle, underscored);
	}
	if (!found && name[0] == '_') {
		found = dlsym(handle, name + 1);
	}
	if (!found) {
		printf("DF-NOTE symbol=%s error=%s\n", name, dlerror());
	}
	return found;
}

static void *send(void *object, const char *selector)
{
	return msgSend(object, selRegister(selector));
}

/* A double-arg send: the NSInteger/double argument goes in xmm0/rdi, so a plain void* prototype
 * would pass garbage. */
static void *send_double_arg(void *object, const char *selector, double value)
{
	union {
		void *(*plain)(void *, void *, ...);
		void *(*d)(void *, void *, double);
	} pun;
	pun.plain = msgSend;
	return pun.d(object, selRegister(selector), value);
}

/* A one-pointer-argument send (e.g. -stringFromDate:). */
static void *send1(void *object, const char *selector, void *argument)
{
	union {
		void *(*plain)(void *, void *, ...);
		void *(*p)(void *, void *, void *);
	} pun;
	pun.plain = msgSend;
	return pun.p(object, selRegister(selector), argument);
}

/* A one-pointer-arg (NSInteger) send. */
static void *send_long_arg(void *object, const char *selector, long value)
{
	union {
		void *(*plain)(void *, void *, ...);
		void *(*l)(void *, void *, long);
	} pun;
	pun.plain = msgSend;
	return pun.l(object, selRegister(selector), value);
}

static const char *cstr(void *cfString)
{
	static char buffer[512];
	buffer[0] = 0;
	if (!cfString) {
		return "(null)";
	}
	if (!CFStringGetCString(cfString, buffer, sizeof buffer, 0x0600 /* kCFStringEncodingASCII */)) {
		snprintf(buffer, sizeof buffer, "(<unconvertible %p>)", cfString);
	}
	return buffer;
}

static void check(const char *name, int ok, const char *detail)
{
	printf("DF-CHECK name=%s ok=%d %s\n", name, ok, detail ? detail : "");
	if (!ok) {
		failures++;
	}
}

int main(int argc, char **argv)
{
	void *objc;
	void *cf;
	void *foundation;
	void *icu;

	void *localeClass;
	void *currentLocale;
	void *systemLocale;

	(void)argc;
	(void)argv;
	setvbuf(stdout, NULL, _IOLBF, 0);

	objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	icu = dlopen(ICU_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	printf("DF-NOTE handles objc=%p cf=%p foundation=%p icu=%p\n", objc, cf, foundation, icu);

	msgSend = (void *(*)(void *, void *, ...))lookup(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))lookup(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))lookup(objc, "objc_getClass");

	CFDateFormatterCreate = (void *(*)(void *, void *, long, long))lookup(cf, "CFDateFormatterCreate");
	CFDateFormatterCreateStringWithDate = (void *(*)(void *, void *, void *))lookup(cf, "CFDateFormatterCreateStringWithDate");
	CFLocaleGetSystem = (void *(*)(void))lookup(cf, "CFLocaleGetSystem");
	CFLocaleGetIdentifier = (void *(*)(void *))lookup(cf, "CFLocaleGetIdentifier");
	CFStringGetCString = (int (*)(void *, char *, long, unsigned))lookup(cf, "CFStringGetCString");
	CFDateCreate = (void *(*)(void *, double))lookup(cf, "CFDateCreate");
	CFDateGetAbsoluteTime = (double (*)(void *))lookup(cf, "CFDateGetAbsoluteTime");
	udat_open = (void *(*)(int, int, const char *, const uint16_t *, int32_t, const uint16_t *, int32_t, int32_t *))lookup(icu, "udat_open");

	/* 0. Where ICU says its data lives, and whether the data file is actually readable from the
	 * guest. CoreFoundation does not embed ICU data; this build compiles ICU with
	 * -DICU_DATA_DIR="/usr/share/icu/" (icu/icuSources/CMakeLists.txt:20) and the CMake install
	 * target ships icudt66l.dat to libexec/darling/usr/share/icu. A prefix missing that file is
	 * the whole reason udat_open fails below. */
	{
		const char *(*uGetDataDirectory)(void) = (const char *(*)(void))lookup(icu, "u_getDataDirectory");
		const char *dir = uGetDataDirectory ? uGetDataDirectory() : NULL;
		printf("DF-ICUDIR u_getDataDirectory=\"%s\"\n", dir ? dir : "(null)");
		const char *paths[] = {
			"/usr/share/icu/icudt66l.dat",
			"/usr/local/share/icu/icudt66l.dat",
		};
		unsigned i;
		for (i = 0; i < sizeof paths / sizeof paths[0]; i++) {
			FILE *f = fopen(paths[i], "rb");
			long size = -1;
			if (f) {
				fseek(f, 0, SEEK_END);
				size = ftell(f);
				fclose(f);
			}
			printf("DF-ICUDATA path=\"%s\" readable=%d size=%ld\n", paths[i], f != NULL, size);
		}
	}

	/* 1. The locale inputs Xcode's NSDateFormatter would receive. */
	localeClass = getClassFn("NSLocale");
	currentLocale = send(localeClass, "currentLocale");
	systemLocale = CFLocaleGetSystem();
	printf("DF-LOCALE current=%p identifier=\"%s\"\n", currentLocale,
	    cstr(CFLocaleGetIdentifier(currentLocale)));
	printf("DF-LOCALE system=%p identifier=\"%s\"\n", systemLocale,
	    cstr(CFLocaleGetIdentifier(systemLocale)));

	/* 2. ICU directly: does a clean literal locale open? If this fails too, the cause is ICU
	 * data, not the locale string our CFLocale supplies. */
	{
		const char *probe_locales[] = { "en_US", "en", "root" };
		unsigned i;
		for (i = 0; i < sizeof probe_locales / sizeof probe_locales[0]; i++) {
			int32_t status = 0;
			void *df = udat_open(UDAT_NONE, UDAT_MEDIUM, probe_locales[i], NULL, 0, NULL, 0, &status);
			printf("DF-UOPEN locale=\"%s\" dateStyle=MEDIUM timeStyle=NONE ptr=%p status=%d\n",
			    probe_locales[i], df, status);
		}
		int32_t status = 0;
		void *df = udat_open(UDAT_MEDIUM, UDAT_MEDIUM, cstr(CFLocaleGetIdentifier(currentLocale)), NULL, 0, NULL, 0, &status);
		printf("DF-UOPEN locale=\"%s\" dateStyle=MEDIUM timeStyle=MEDIUM ptr=%p status=%d\n",
		    cstr(CFLocaleGetIdentifier(currentLocale)), df, status);
	}

	/* 3. CFDateFormatterCreate, the call _regenerateFormatter makes; and the CFDateRef the
	 * formatter is asked to print. */
	void *dfCurrent = CFDateFormatterCreate(NULL, currentLocale, kMediumStyle, kMediumStyle);
	printf("DF-CREATE locale=currentLocale dateStyle=MEDIUM timeStyle=MEDIUM ptr=%p\n", dfCurrent);
	void *dfSystem = CFDateFormatterCreate(NULL, systemLocale, kMediumStyle, kMediumStyle);
	printf("DF-CREATE locale=systemLocale dateStyle=MEDIUM timeStyle=MEDIUM ptr=%p\n", dfSystem);
	void *dfDateOnly = CFDateFormatterCreate(NULL, systemLocale, kLongStyle, kNoStyle);
	printf("DF-CREATE locale=systemLocale dateStyle=LONG timeStyle=NONE ptr=%p\n", dfDateOnly);

	void *knownDate = CFDateCreate(NULL, KNOWN_DATE);
	printf("DF-DATE cfdate=%p absolute=%.3f\n", knownDate,
	    knownDate ? CFDateGetAbsoluteTime(knownDate) : -1.0);

	check("cfdateformatter-create-current", dfCurrent != NULL, "locale=currentLocale medium/medium");
	check("cfdateformatter-create-system", dfSystem != NULL, "locale=systemLocale medium/medium");
	check("cfdateformatter-create-dateonly", dfDateOnly != NULL, "locale=systemLocale long/none");
	check("cfdate-known-date", knownDate != NULL && CFDateGetAbsoluteTime(knownDate) == KNOWN_DATE, "2021-06-15T12:00:00Z");

	/* 4. The NSLog-formatter path Xcode uses: [[NSDateFormatter alloc] init] + styles +
	 * stringFromDate:. This is the call that faulted on the unfixed runtime. */
	{
		void *formatterClass = getClassFn("NSDateFormatter");
		void *dateClass = getClassFn("NSDate");
		void *formatter = send(formatterClass, "alloc");
		formatter = send(formatter, "init");
		send_long_arg(formatter, "setDateStyle:", kMediumStyle);
		send_long_arg(formatter, "setTimeStyle:", kMediumStyle);
		void *date = send_double_arg(dateClass, "dateWithTimeIntervalSinceReferenceDate:", KNOWN_DATE);
		printf("DF-NSDATE ptr=%p absolute=%.3f\n", date, date ? CFDateGetAbsoluteTime((void *)date) : -1.0);

		/* This is the exact message send that segfaulted at vm 0x1cb45 on the unfixed
		 * Foundation; it is deliberately unguarded so the RED arm reproduces the crash. */
		void *string = send1(formatter, "stringFromDate:", date);
		const char *value = string ? (const char *)send(string, "UTF8String") : NULL;
		printf("DF-STRING formatter=%p date=%p string=%p value=\"%s\"\n",
		    formatter, date, string, value ? value : "(null)");

		check("stringFromDate-non-null", string != NULL, "NSDateFormatter medium/medium");
		check("stringFromDate-non-empty", value != NULL && value[0] != 0, "empty string is not a date");
		check("stringFromDate-has-year", value != NULL && strstr(value, "2021") != NULL, "the input date's year");
		check("stringFromDate-has-time", value != NULL && strchr(value, ':') != NULL, "medium time style wants HH:MM");
	}

	if (failures != 0) {
		printf("DF-FAIL failures=%d\n", failures);
		printf("DF_VERDICT=RED\n");
		return 1;
	}
	printf("DF_VERDICT=GREEN\n");
	return 0;
}

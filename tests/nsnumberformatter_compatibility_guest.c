/*
 * nsnumberformatter_compatibility_guest.c -- permanent guest oracle for
 *
 *   the NSNumberFormatterCompatibility block (macOS-only, Apple SDK
 *   Foundation/NSNumberFormatter.h): hasThousandSeparators, thousandSeparator, localizesFormat,
 *   format, attributedStringForZero/Nil/NotANumber and roundingBehavior
 *
 * WHY THIS EXISTS. xcodebuild dies with
 *
 *   Uncaught Exception: -[NSNumberFormatter setHasThousandSeparators:]: unrecognized selector sent
 *   to instance 0x...
 *
 * raised from SharedFrameworks/DVTInstrumentsUtilities.framework/Versions/A/DVTInstrumentsUtilities
 * (sha256 ef40c0e5bafa5be8454f535f36347e558f311cd14dae3b5c9e77f494591a8ac5) inside the
 * `HexFormatStyle` dispatch_once initializer, which sends
 *   13f2e: [fmt setHasThousandSeparators:]   (edx=1)
 * right after setNumberStyle: and setUsesGroupingSeparator: on the SAME receiver. The exception
 * unwinds to _Xcode3CommandLineToolHandleException and abort(2). The same image, and the plug-in set
 * (IDEInterfaceBuilderCocoaIntegration, CocoaPlugin, InstrumentsPlugIn, DebuggerUI, InstrumentsKit,
 * IDESwiftPackageUI, SpatialInspectorXcodePlugin, MLShared, RealityToolKit, RealityToolsShared,
 * SourceEditorPlugin), reference the rest of the block, so the oracle checks the FAMILY and not the
 * one selector that happened to appear in a stack.
 *
 * WHAT THIS MEASURES, in one guest process on the deployed runtime, with no compile-time reference to
 * Foundation or ObjC (dlopen + dlsym + objc_msgSend + sel_registerName only):
 *
 *   1. every selector of the block EXISTS on NSNumberFormatter as an instance method (reported,
 *      never raised -- which is what lets the RED arm print the missing members instead of dying);
 *   2. the stored state round-trips: each setter's value is what its getter returns afterwards;
 *   3. the DECLARED DEFAULTS of a freshly initialised formatter -- hasThousandSeparators NO,
 *      localizesFormat YES, thousandSeparator nil, format nil, attributedStringForZero "0",
 *      attributedStringForNil "", attributedStringForNotANumber "NaN", roundingBehavior the
 *      NSDecimalNumberHandler default;
 *   4. the exact send sequence DVTInstrumentsUtilities' HexFormatStyle performs raises nothing and
 *      still leaves a formatter that formats a number (its result is printed, not asserted: this
 *      fixture is not an ICU number-formatting oracle);
 *   5. setFormat:'s value reaches the formatter rather than only the dictionary: after
 *      [fmt setFormat:@"#,##0.00"] the getter returns it AND stringFromNumber: still answers.
 *
 * NSNUMBERFORMATTER_COMPAT_VERDICT=GREEN is the machine-readable line the harness ok-marker reads;
 * every measured value is printed unconditionally as an NFCOMPAT-* line so a RED arm quotes the
 * failure from the run's own output. Exit status is 0 on GREEN, 1 on RED.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);
static void *(*classGetInstanceMethod)(void *, void *);

static void *clsNSNumberFormatter;
static void *clsNSString;
static void *clsNSNumber;
static void *clsNSAttributedString;
static void *clsNSDecimalNumberHandler;
static int failures;

static void *send0(void *o, const char *sel)
{
	return msgSend(o, selRegister(sel));
}

static void *send1(void *o, const char *sel, void *a)
{
	return msgSend(o, selRegister(sel), a);
}

static void send1_uint(void *o, const char *sel, unsigned long a)
{
	union { void *(*p)(void *, void *, ...); void (*f)(void *, void *, unsigned long); } u;
	u.p = msgSend;
	u.f(o, selRegister(sel), a);
}

static void send1_double(void *o, const char *sel, double a)
{
	union { void *(*p)(void *, void *, ...); void (*f)(void *, void *, double); } u;
	u.p = msgSend;
	u.f(o, selRegister(sel), a);
}

static unsigned char send_bool0(void *o, const char *sel)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel));
}

static unsigned char send_bool1(void *o, const char *sel, void *a)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *, void *); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel), a);
}

/* +[NSString stringWithUTF8String:] */
static void *nsstr(const char *s)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, const char *); } u;
	u.p = msgSend;
	return u.f(clsNSString, selRegister("stringWithUTF8String:"), s);
}

/* +[NSNumber numberWithDouble:] */
static void *nsdouble(double d)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, double); } u;
	u.p = msgSend;
	return u.f(clsNSNumber, selRegister("numberWithDouble:"), d);
}

static const char *cstr_of(void *nsstring)
{
	if (!nsstring)
		return "(nil)";
	const char *s = (const char *)send0(nsstring, "UTF8String");
	return s ? s : "(no-utf8)";
}

static int nsstring_equals(void *nsstring, const char *c)
{
	if (!nsstring)
		return 0;
	return (int)send_bool1(nsstring, "isEqualToString:", nsstr(c));
}

static void check(const char *what, int ok, const char *detail)
{
	if (!ok)
		failures++;
	printf("NFCOMPAT-CHECK %-34s %s %s\n", what, ok ? "ok" : "MISMATCH", detail ? detail : "");
}

/* Every member of the block, exactly as the Apple SDK declares it. */
static const char *const compat_selectors[] = {
	"hasThousandSeparators", "setHasThousandSeparators:",
	"thousandSeparator", "setThousandSeparator:",
	"localizesFormat", "setLocalizesFormat:",
	"format", "setFormat:",
	"attributedStringForZero", "setAttributedStringForZero:",
	"attributedStringForNil", "setAttributedStringForNil:",
	"attributedStringForNotANumber", "setAttributedStringForNotANumber:",
	"roundingBehavior", "setRoundingBehavior:",
};

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	unsigned i;

	void *libobjc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!libobjc) {
		printf("NFCOMPAT-DLOPEN libobjc failed: %s\n", dlerror());
		printf("NSNUMBERFORMATTER_COMPAT_VERDICT=RED\n");
		return 1;
	}
	void *libfoundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!libfoundation) {
		printf("NFCOMPAT-DLOPEN foundation failed: %s\n", dlerror());
		printf("NSNUMBERFORMATTER_COMPAT_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))dlsym(libobjc, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(libobjc, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(libobjc, "objc_getClass");
	classGetInstanceMethod = (void *(*)(void *, void *))dlsym(libobjc, "class_getInstanceMethod");
	if (!msgSend || !selRegister || !getClassFn || !classGetInstanceMethod) {
		printf("NFCOMPAT-DLOPEN objc symbols missing\n");
		printf("NSNUMBERFORMATTER_COMPAT_VERDICT=RED\n");
		return 1;
	}

	clsNSNumberFormatter = getClassFn("NSNumberFormatter");
	clsNSString = getClassFn("NSString");
	clsNSNumber = getClassFn("NSNumber");
	clsNSAttributedString = getClassFn("NSAttributedString");
	clsNSDecimalNumberHandler = getClassFn("NSDecimalNumberHandler");
	printf("NFCOMPAT-CLASS NSNumberFormatter=%p NSString=%p NSNumber=%p NSAttributedString=%p "
	       "NSDecimalNumberHandler=%p\n", clsNSNumberFormatter, clsNSString, clsNSNumber,
	       clsNSAttributedString, clsNSDecimalNumberHandler);
	if (!clsNSNumberFormatter || !clsNSString || !clsNSNumber) {
		printf("NFCOMPAT-DLOPEN required classes missing\n");
		printf("NSNUMBERFORMATTER_COMPAT_VERDICT=RED\n");
		return 1;
	}

	/* 1. presence, reported rather than raised */
	unsigned absent = 0;
	for (i = 0; i < sizeof(compat_selectors) / sizeof(compat_selectors[0]); i++) {
		void *m = classGetInstanceMethod(clsNSNumberFormatter, selRegister(compat_selectors[i]));
		printf("NFCOMPAT-MEMBER %-38s %s\n", compat_selectors[i], m ? "present" : "ABSENT");
		if (!m)
			absent++;
	}
	if (absent) {
		printf("NFCOMPAT-DECISION %u of the block's %u selectors are absent on this deployed "
		       "runtime, so any image that sends one dies with an unrecognized selector\n",
		       absent, (unsigned)(sizeof(compat_selectors) / sizeof(compat_selectors[0])));
		printf("NSNUMBERFORMATTER_COMPAT_VERDICT=RED\n");
		return 1;
	}

	/* 2/3. stored state and declared defaults */
	void *fmt = send0(send0(clsNSNumberFormatter, "alloc"), "init");
	if (!fmt) {
		printf("NFCOMPAT-DECISION cannot initialise an NSNumberFormatter\n");
		printf("NSNUMBERFORMATTER_COMPAT_VERDICT=RED\n");
		return 1;
	}

	check("default hasThousandSeparators", send_bool0(fmt, "hasThousandSeparators") == 0, "expected NO");
	send1_uint(fmt, "setHasThousandSeparators:", 1);
	check("setHasThousandSeparators:YES", send_bool0(fmt, "hasThousandSeparators") == 1,
	      "getter returned NO");
	send1_uint(fmt, "setHasThousandSeparators:", 0);
	check("setHasThousandSeparators:NO", send_bool0(fmt, "hasThousandSeparators") == 0,
	      "getter returned YES");

	check("default thousandSeparator", send0(fmt, "thousandSeparator") == NULL, "expected nil");
	send1(fmt, "setThousandSeparator:", nsstr("'"));
	check("setThousandSeparator:@\"'\"", nsstring_equals(send0(fmt, "thousandSeparator"), "'"),
	      cstr_of(send0(fmt, "thousandSeparator")));

	check("default localizesFormat", send_bool0(fmt, "localizesFormat") == 1, "expected YES");
	send1_uint(fmt, "setLocalizesFormat:", 0);
	check("setLocalizesFormat:NO", send_bool0(fmt, "localizesFormat") == 0, "getter returned YES");

	check("default format", send0(fmt, "format") == NULL, "expected nil");
	send1(fmt, "setFormat:", nsstr("#,##0.00"));
	check("setFormat:@\"#,##0.00\"", nsstring_equals(send0(fmt, "format"), "#,##0.00"),
	      cstr_of(send0(fmt, "format")));
	void *formatted = send1(fmt, "stringFromNumber:", nsdouble(1234567.0));
	printf("NFCOMPAT-VALUE stringFromNumber:1234567.0 after setFormat: = %s\n", cstr_of(formatted));
	check("formatting still answers", formatted != NULL, "stringFromNumber: returned nil");

	/* 4. the exact send sequence DVTInstrumentsUtilities' HexFormatStyle performs */
	void *hex = send0(send0(clsNSNumberFormatter, "alloc"), "init");
	send1_uint(hex, "setNumberStyle:", 1 /* NSNumberFormatterDecimalStyle */);
	send1_uint(hex, "setUsesGroupingSeparator:", 1);
	send1_uint(hex, "setHasThousandSeparators:", 1);
	send1_uint(hex, "setMinimumFractionDigits:", 0);
	send1_uint(hex, "setMaximumFractionDigits:", 4);
	void *hexout = send1(hex, "stringFromNumber:", nsdouble(1234.56789));
	printf("NFCOMPAT-VALUE HexFormatStyle sequence -> stringFromNumber:1234.56789 = %s\n",
	       cstr_of(hexout));
	check("HexFormatStyle sequence survives", hexout != NULL, "stringFromNumber: returned nil");

	/* 5. the attributed-string defaults and the rounding behavior default */
	if (clsNSAttributedString) {
		void *zero = send0(fmt, "attributedStringForZero");
		void *nil_ = send0(fmt, "attributedStringForNil");
		void *nan = send0(fmt, "attributedStringForNotANumber");
		printf("NFCOMPAT-VALUE attributedStringForZero=%s ForNil=%s ForNotANumber=%s\n",
		       zero ? cstr_of(send0(zero, "string")) : "(nil)",
		       nil_ ? cstr_of(send0(nil_, "string")) : "(nil)",
		       nan ? cstr_of(send0(nan, "string")) : "(nil)");
		check("default attributedStringForZero", zero && nsstring_equals(send0(zero, "string"), "0"),
		      zero ? "expected \"0\"" : "nil");
		check("default attributedStringForNil", nil_ && nsstring_equals(send0(nil_, "string"), ""),
		      nil_ ? "expected \"\"" : "nil");
		check("default attributedStringForNotANumber",
		      nan && nsstring_equals(send0(nan, "string"), "NaN"), nan ? "expected \"NaN\"" : "nil");
		void *mine = send1(send0(clsNSAttributedString, "alloc"), "initWithString:", nsstr("ZERO"));
		send1(fmt, "setAttributedStringForZero:", mine);
		check("setAttributedStringForZero:",
		      nsstring_equals(send0(send0(fmt, "attributedStringForZero"), "string"), "ZERO"),
		      cstr_of(send0(send0(fmt, "attributedStringForZero"), "string")));
	} else {
		printf("NFCOMPAT-SKIP NSAttributedString is not resolvable on this runtime\n");
	}

	if (clsNSDecimalNumberHandler) {
		void *behavior = send0(fmt, "roundingBehavior");
		void *defaultBehavior = send0(clsNSDecimalNumberHandler, "defaultDecimalNumberHandler");
		printf("NFCOMPAT-VALUE roundingBehavior=%p default=%p\n", behavior, defaultBehavior);
		check("default roundingBehavior", behavior != NULL, "nil");
		check("roundingBehavior is the handler default", behavior == defaultBehavior,
		      behavior == defaultBehavior ? "" : "not the same object as +[NSDecimalNumberHandler "
		                                          "defaultDecimalNumberHandler]");
	} else {
		printf("NFCOMPAT-SKIP NSDecimalNumberHandler is not resolvable on this runtime\n");
	}

	printf("NFCOMPAT-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("NSNUMBERFORMATTER_COMPAT_VERDICT=GREEN\n");
	else
		printf("NSNUMBERFORMATTER_COMPAT_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

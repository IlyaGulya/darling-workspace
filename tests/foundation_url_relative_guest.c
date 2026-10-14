/*
 * foundation_url_relative_guest.c -- permanent guest oracle for the NSURL file-URL-with-a-base
 * family on the DEPLOYED Darling runtime:
 *
 *   +[NSURL fileURLWithPath:isDirectory:relativeToURL:]      (class method; Apple SDK NSURL.h:46)
 *   -[NSURL initFileURLWithPath:isDirectory:relativeToURL:]  (the designated initializer, NSURL.h:36)
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder (scripts/xcodebuild-workload-run.sh, both
 * workloads) dies during Xcode's initial PLUG-IN SCAN with
 *
 *   DVTPlugInErrorDomain code 1 "Scanning for plug-ins failed."
 *   DVTUnderlyingExceptionErrorKey = +[NSURL fileURLWithPath:isDirectory:relativeToURL:]:
 *                                     unrecognized selector sent to instance 0x...
 *
 * The only call site in the image that raises it is the single selref use in DVTFoundation
 *
 *   -[DVTPlugInBundleMetadata URLForResource:withExtension:]
 *
 * (DVTFoundation 55d190e10e08642f5265685af501e975fcaf94a11ea623d14b6d7aa865b2f054, x86_64 slice
 * vm 0x18911d = fat file offset 0x18d11d). That function reaches, in order, `path`,
 * `stringByAppendingPathComponent:`, `stringByAppendingPathExtension:`, `defaultManager`,
 * `fileExistsAtPath:isDirectory:`, `pathExtension`, `isEqual:` -- all present in this tree's
 * Foundation -- so the one absent member of the family is the whole of the wall.
 *
 * WHAT THIS MEASURES, in one guest process on the deployed runtime, with no compile-time reference
 * to Foundation or ObjC (dlopen + dlsym + objc_msgSend + sel_registerName only): the PRESENCE of
 * each family member (by class_getClassMethod/class_getInstanceMethod, so a missing selector is
 * reported rather than raised) and then, for the concrete URL objects those methods return, the
 * four observable properties a caller can gate on: relativeString, baseURL, absoluteString/path
 * (the absolute resolution) and the directory marking that the trailing slash carries.
 *
 * The expectations below are the ones the runtime's own CoreFoundation defines and the vendored
 * swift-corelibs-foundation reference relies on (CoreFoundation/CFURL.c,
 * _CFURLCreateWithFileSystemPath):
 *
 *   * an absolute path IGNORES the base           -> baseURL nil, absolute string is the path
 *   * a relative path keeps the base               -> relativeString is the path as given,
 *                                                     absoluteString resolves against the base
 *   * isDirectory appends the trailing slash the   -> "child" becomes "child/", which is the only
 *     URL carries                                    observable of the flag NSURL exposes
 *   * an empty path WITH a base IS the base        -> absoluteString is the base's own
 *   * a relative path with NO base uses the cwd    -> absoluteString is cwd + path
 *   * no path has to EXIST                          -> a non-existent name still builds a URL
 *
 * FOUNDATION_URL_RELATIVE_VERDICT=GREEN is the machine-readable line the harness ok-marker reads;
 * every measured value is printed unconditionally as a URLREL-* line so a RED arm quotes the
 * failure from the run's own output. Exit status is 0 on GREEN, 1 on RED.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the oracle is run against the prefix as
 * deployed. See evidence/foundation-baseline-url-rung/README.md for the two arms.
 */

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);
static void *(*classGetClassMethod)(void *, void *);
static void *(*classGetInstanceMethod)(void *, void *);

static void *clsNSURL;
static void *clsNSString;
static int failures;

/* --- sends ---------------------------------------------------------------------------------- */

static void *send0(void *o, const char *sel)
{
	return msgSend(o, selRegister(sel));
}

/* +[NSString stringWithUTF8String:] */
static void *nsstr(const char *s)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, const char *); } u;
	u.p = msgSend;
	return u.f(clsNSString, selRegister("stringWithUTF8String:"), s);
}

/* +[NSURL fileURLWithPath:isDirectory:] */
static void *url_with_path_isdir(const char *path, int isDir)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, void *, unsigned char); } u;
	u.p = msgSend;
	return u.f(clsNSURL, selRegister("fileURLWithPath:isDirectory:"), nsstr(path), (unsigned char)isDir);
}

/* +[NSURL fileURLWithPath:isDirectory:relativeToURL:] */
static void *url_with_path_isdir_rel(const char *path, int isDir, void *base)
{
	union {
		void *(*p)(void *, void *, ...);
		void *(*f)(void *, void *, void *, unsigned char, void *);
	} u;
	u.p = msgSend;
	return u.f(clsNSURL, selRegister("fileURLWithPath:isDirectory:relativeToURL:"),
		   nsstr(path), (unsigned char)isDir, base);
}

/* -[NSURL initFileURLWithPath:isDirectory:relativeToURL:] on a freshly allocated instance */
static void *url_init_with_path_isdir_rel(const char *path, int isDir, void *base)
{
	union {
		void *(*p)(void *, void *, ...);
		void *(*f)(void *, void *, void *, unsigned char, void *);
	} u;
	u.p = msgSend;
	void *alloc = send0(clsNSURL, "alloc");
	return u.f(alloc, selRegister("initFileURLWithPath:isDirectory:relativeToURL:"),
		   nsstr(path), (unsigned char)isDir, base);
}

static unsigned char send_bool0(void *o, const char *sel)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel));
}

/* --- reporting ------------------------------------------------------------------------------ */

static const char *cstr_of(void *nsstring)
{
	if (!nsstring)
		return "(nil)";
	const char *s = (const char *)send0(nsstring, "UTF8String");
	return s ? s : "(no-utf8)";
}

static void check(const char *what, const char *expected, const char *actual)
{
	int ok = actual && strcmp(expected, actual) == 0;
	if (!ok)
		failures++;
	printf("URLREL-CHECK %-28s %s expected=%s actual=%s\n", what, ok ? "ok" : "MISMATCH",
	       expected, actual ? actual : "(null)");
}

/* An absent method is the arm's whole point, so it is a printed fact, never a raise. */
static int member_present(void *cls, const char *sel, int classMethod)
{
	void *m = classMethod ? classGetClassMethod(cls, selRegister(sel))
			      : classGetInstanceMethod(cls, selRegister(sel));
	printf("URLREL-MEMBER %-46s %-13s %s\n", sel, classMethod ? "class_method" : "instance_method",
	       m ? "present" : "ABSENT");
	return m != NULL;
}

/* --- the family, as a contract -------------------------------------------------------------- */

static void case_family(const char *label, void *u, const char *exp_relative, const char *exp_absolute,
			const char *exp_base, const char *exp_path, int exp_isfile)
{
	if (!u) {
		failures++;
		printf("URLREL-CASE %s result=(nil)\n", label);
		return;
	}
	char k[96];
	const char *relative = cstr_of(send0(u, "relativeString"));
	const char *absolute = cstr_of(send0(u, "absoluteString"));
	void *base = send0(u, "baseURL");
	const char *base_str = base ? cstr_of(send0(base, "absoluteString")) : "(nil)";
	const char *path = cstr_of(send0(u, "path"));
	unsigned char isfile = send_bool0(u, "isFileURL");

	printf("URLREL-CASE %s relativeString=%s absoluteString=%s baseURL=%s path=%s isFileURL=%d\n",
	       label, relative, absolute, base_str, path, (int)isfile);

	snprintf(k, sizeof k, "%s.relativeString", label);
	check(k, exp_relative, relative);
	snprintf(k, sizeof k, "%s.absoluteString", label);
	check(k, exp_absolute, absolute);
	snprintf(k, sizeof k, "%s.baseURL", label);
	check(k, exp_base, base_str);
	snprintf(k, sizeof k, "%s.path", label);
	check(k, exp_path, path);
	if ((int)isfile != exp_isfile) {
		failures++;
		printf("URLREL-CHECK %-28s MISMATCH expected=%d actual=%d\n", "isFileURL", exp_isfile,
		       (int)isfile);
	}
}

/* The relative-path-with-no-base case: the base comes from the process's own current directory,
 * so the expectation is the RELATIONSHIP (absolute endswith the relative path, and it is no
 * longer the bare relative string), not a host path copied from outside. */
static void case_cwd_fallback(void)
{
	void *u = url_with_path_isdir_rel("foundation-urlrel-cwd", 0, NULL);
	if (!u) {
		failures++;
		printf("URLREL-CASE cwd_fallback result=(nil)\n");
		return;
	}
	const char *relative = cstr_of(send0(u, "relativeString"));
	const char *absolute = cstr_of(send0(u, "absoluteString"));
	void *base = send0(u, "baseURL");
	printf("URLREL-CASE cwd_fallback relativeString=%s absoluteString=%s baseURL=%s\n", relative,
	       absolute, base ? cstr_of(send0(base, "absoluteString")) : "(nil)");
	check("cwd_fallback.relativeString", "foundation-urlrel-cwd", relative);
	size_t alen = strlen(absolute), slen = strlen("foundation-urlrel-cwd");
	int suffix_ok = alen > slen && strcmp(absolute + (alen - slen), "foundation-urlrel-cwd") == 0;
	if (!suffix_ok)
		failures++;
	printf("URLREL-CHECK %-28s %s absoluteString resolves through the cwd\n", "cwd_fallback.abs",
	       suffix_ok ? "ok" : "MISMATCH");
}

static void case_non_existent(void)
{
	static const char *missing = "/private/var/tmp/foundation-urlrel-does-not-exist";
	struct stat st;
	int exists = (stat(missing, &st) == 0);
	if (exists) {
		failures++;
		printf("URLREL-CHECK %-28s MISMATCH the control path exists, so this case proves nothing\n",
		       "missing_path_control");
		return;
	}
	void *base = url_with_path_isdir("/private/var/tmp/", 1);
	void *u = url_with_path_isdir_rel("foundation-urlrel-does-not-exist", 0, base);
	case_family("missing_path", u, "foundation-urlrel-does-not-exist",
		    "file:///private/var/tmp/foundation-urlrel-does-not-exist",
		    "file:///private/var/tmp/", missing, 1);
}

static void case_init_matches_factory(void)
{
	void *base = url_with_path_isdir("/private/var/tmp/", 1);
	void *from_factory = url_with_path_isdir_rel("foundation-urlrel-same", 0, base);
	void *from_init = url_init_with_path_isdir_rel("foundation-urlrel-same", 0, base);
	const char *a = from_factory ? cstr_of(send0(from_factory, "absoluteString")) : "(nil)";
	const char *b = from_init ? cstr_of(send0(from_init, "absoluteString")) : "(nil)";
	check("init_vs_factory.absoluteString", a, b);
	/* The designated initializer is the one the class method must agree with, both ways. */
	if (!from_init) {
		failures++;
		printf("URLREL-CHECK %-28s MISMATCH init returned nil\n", "init_vs_factory");
	}
}

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);

	void *libobjc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!libobjc) {
		printf("URLREL-DLOPEN libobjc failed: %s\n", dlerror());
		printf("FOUNDATION_URL_RELATIVE_VERDICT=RED\n");
		return 1;
	}
	void *libfoundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!libfoundation) {
		printf("URLREL-DLOPEN foundation failed: %s\n", dlerror());
		printf("FOUNDATION_URL_RELATIVE_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))dlsym(libobjc, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(libobjc, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(libobjc, "objc_getClass");
	classGetClassMethod = (void *(*)(void *, void *))dlsym(libobjc, "class_getClassMethod");
	classGetInstanceMethod = (void *(*)(void *, void *))dlsym(libobjc, "class_getInstanceMethod");
	if (!msgSend || !selRegister || !getClassFn || !classGetClassMethod || !classGetInstanceMethod) {
		printf("URLREL-DLOPEN objc runtime symbols missing\n");
		printf("FOUNDATION_URL_RELATIVE_VERDICT=RED\n");
		return 1;
	}

	clsNSURL = getClassFn("NSURL");
	clsNSString = getClassFn("NSString");
	if (!clsNSURL || !clsNSString) {
		printf("URLREL-CLASS NSURL=%p NSString=%p\n", clsNSURL, clsNSString);
		printf("FOUNDATION_URL_RELATIVE_VERDICT=RED\n");
		return 1;
	}
	printf("URLREL-CLASS NSURL=%p NSString=%p\n", clsNSURL, clsNSString);

	/* 1. presence */
	int have_factory = member_present(clsNSURL, "fileURLWithPath:isDirectory:relativeToURL:", 1);
	int have_init = member_present(clsNSURL, "initFileURLWithPath:isDirectory:relativeToURL:", 0);
	member_present(clsNSURL, "fileURLWithPath:isDirectory:", 1);
	member_present(clsNSURL, "initFileURLWithPath:isDirectory:", 0);

	if (!have_factory || !have_init) {
		/* The deployed runtime is the pre-fix one: report the observable Xcode sees. */
		printf("URLREL-DECISION the family member Xcode's plug-in scan demands is absent on this "
		       "deployed runtime\n");
		printf("FOUNDATION_URL_RELATIVE_VERDICT=RED\n");
		return 1;
	}

	/* 2. the contract, on real URL objects */
	void *base = url_with_path_isdir("/private/var/tmp/", 1);
	const char *base_abs = base ? cstr_of(send0(base, "absoluteString")) : "(nil)";
	printf("URLREL-BASE absoluteString=%s\n", base_abs);
	check("base.absoluteString", "file:///private/var/tmp/", base_abs);

	/* a non-existent member: isDirectory:NO */
	case_family("rel_file", url_with_path_isdir_rel("foundation-urlrel-child", 0, base),
		    "foundation-urlrel-child", "file:///private/var/tmp/foundation-urlrel-child",
		    "file:///private/var/tmp/", "/private/var/tmp/foundation-urlrel-child", 1);

	/* a non-existent member: isDirectory:YES -- the trailing slash is the only observable of it */
	case_family("rel_dir", url_with_path_isdir_rel("foundation-urlrel-child", 1, base),
		    "foundation-urlrel-child/", "file:///private/var/tmp/foundation-urlrel-child/",
		    "file:///private/var/tmp/", "/private/var/tmp/foundation-urlrel-child", 1);

	/* a path that EXISTS as a directory, named with the same isDirectory:YES the scan passes */
	mkdir("/private/var/tmp/foundation-urlrel-dir", 0755);
	case_family("rel_existing_dir", url_with_path_isdir_rel("foundation-urlrel-dir", 1, base),
		    "foundation-urlrel-dir/", "file:///private/var/tmp/foundation-urlrel-dir/",
		    "file:///private/var/tmp/", "/private/var/tmp/foundation-urlrel-dir", 1);
	rmdir("/private/var/tmp/foundation-urlrel-dir");

	/* no existence requirement at all */
	case_non_existent();

	/* an absolute path ignores the base: baseURL nil */
	case_family("abs_with_base",
		    url_with_path_isdir_rel("/private/var/tmp/foundation-urlrel-abs", 0, base),
		    "file:///private/var/tmp/foundation-urlrel-abs",
		    "file:///private/var/tmp/foundation-urlrel-abs", "(nil)",
		    "/private/var/tmp/foundation-urlrel-abs", 1);

	/* an empty path WITH a base is the base itself; its POSIX path has no trailing slash because
	 * CFURLCreateStringWithFileSystemPath strips a directory URL's trailing separator (the URL's
	 * own directory marking stays visible in absoluteString) */
	case_family("empty_with_base", url_with_path_isdir_rel("", 0, base),
		    "file:///private/var/tmp/", "file:///private/var/tmp/", "(nil)",
		    "/private/var/tmp", 1);

	/* a relative path with no base resolves through the current directory */
	case_cwd_fallback();

	/* the class method and the designated initializer are the same operation */
	case_init_matches_factory();

	/* the pre-existing, non-relative member still behaves as it did */
	void *plain = url_with_path_isdir("/private/var/tmp/", 1);
	check("plain_dir.absoluteString", "file:///private/var/tmp/",
	      plain ? cstr_of(send0(plain, "absoluteString")) : "(nil)");

	printf("URLREL-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("FOUNDATION_URL_RELATIVE_VERDICT=GREEN\n");
	else
		printf("FOUNDATION_URL_RELATIVE_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

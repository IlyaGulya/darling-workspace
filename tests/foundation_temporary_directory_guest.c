/*
 * foundation_temporary_directory_guest.c -- permanent guest oracle for
 *
 *   -[NSFileManager temporaryDirectory]     (Apple SDK NSFileManager.h:462, category NSUserInformation)
 *
 * WHY THIS EXISTS. Once Xcode's plug-in scan gets past its own missing NSURL initializer, the
 * ladder (scripts/xcodebuild-workload-run.sh, both workloads) dies inside xcodebuild's
 * ERROR-PRINTING path, which turns every error into an uncaught exception and SIGILL:
 *
 *   Uncaught Exception: -[NSFileManager temporaryDirectory]: unrecognized selector sent to instance
 *     6  _CF_forwarding_prep_0                        (in CoreFoundation)
 *     7  -[Xcode3CommandLineBuildTool resolveErrorTriageResultBundleDestination]  (in Xcode3Core)
 *     8  -[Xcode3CommandLineBuildTool saveResultBundleForError:triageWithSchemeTask:error:]
 *     9  -[Xcode3CommandLineBuildTool _printError:schemeTask:andFailWithCode:]
 *    10  -[Xcode3CommandLineBuildTool _printErrorString:andFailWithCode:]
 *    11  -[Xcode3CommandLineBuildTool run]
 *
 * (Xcode3Core 5400f4961d7ed59d62fed3e27bc3ee36fcfceb9729d229fcabfc4402d1abb5c5, x86_64 slice
 * vm 0x3dd94 = fat file offset 0x41d94, whose ONE selref use of the selector is at vm 0x3decc =
 * fat file offset 0x41ecc; the same image has exactly one other, at vm 0x65840.) That call site's
 * neighbours in the same function are `defaultManager`, `URLByAppendingPathComponent:`, `path`,
 * `now`, `stringFromDate:` and `setDateFormat:` -- all present in this tree's Foundation -- and
 * `homeDirectoryForCurrentUser` is NOT among them, so this accessor is the whole of the wall.
 *
 * WHAT THIS MEASURES, in one guest process on the deployed runtime, with no compile-time reference
 * to Foundation or ObjC (dlopen + dlsym + objc_msgSend + sel_registerName only):
 *
 *   1. the accessor EXISTS as an instance method (reported, never raised);
 *   2. the URL it returns is a FILE url with NO base and the DIRECTORY MARKING Darwin's URL
 *      carries -- a trailing slash on absoluteString;
 *   3. its path IS NSTemporaryDirectory() up to that trailing separator (CoreFoundation's
 *      CFURLCreateStringWithFileSystemPath strips a directory URL's trailing slash, so [url path]
 *      and NSTemporaryDirectory() differ by exactly the separator and by nothing else) -- read out
 *      of the same deployed Foundation, so the oracle proves the two agree inside the runtime
 *      instead of asserting a path from outside;
 *   4. that path EXISTS and IS a directory in the guest's own filesystem;
 *   5. the directory is USABLE: the URLByAppendingPathComponent: step the Xcode call site performs
 *      immediately afterwards produces a path under it, and a directory can be created and removed
 *      there.
 *
 * This is deliberately NOT an assertion about which directory Darwin would choose: no path is
 * named in this file, so a runtime whose NSTemporaryDirectory() is /tmp/ (this one, where launchd
 * exports TMPDIR=confstr(_CS_DARWIN_USER_TEMP_DIR) and __dirhelper falls back to /tmp) and a
 * runtime whose is /var/folders/.../T/ are both measured by the same contract. The value it found
 * is printed.
 *
 * FOUNDATION_TEMPDIR_VERDICT=GREEN is the machine-readable line the harness ok-marker reads; every
 * measured value is printed unconditionally as a FTD-* line so a RED arm quotes the failure from
 * the run's own output. Exit status is 0 on GREEN, 1 on RED.
 */

#include <dlfcn.h>
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
static void *(*classGetInstanceMethod)(void *, void *);

static void *clsNSFileManager;
static void *clsNSString;
static int failures;

static void *send0(void *o, const char *sel)
{
	return msgSend(o, selRegister(sel));
}

static void *send1(void *o, const char *sel, void *a)
{
	return msgSend(o, selRegister(sel), a);
}

static unsigned char send_bool0(void *o, const char *sel)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel));
}

/* +[NSString stringWithUTF8String:] */
static void *nsstr(const char *s)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, const char *); } u;
	u.p = msgSend;
	return u.f(clsNSString, selRegister("stringWithUTF8String:"), s);
}

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
	printf("FTD-CHECK %-30s %s expected=%s actual=%s\n", what, ok ? "ok" : "MISMATCH", expected,
	       actual ? actual : "(null)");
}

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);

	void *libobjc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!libobjc) {
		printf("FTD-DLOPEN libobjc failed: %s\n", dlerror());
		printf("FOUNDATION_TEMPDIR_VERDICT=RED\n");
		return 1;
	}
	void *libfoundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!libfoundation) {
		printf("FTD-DLOPEN foundation failed: %s\n", dlerror());
		printf("FOUNDATION_TEMPDIR_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))dlsym(libobjc, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(libobjc, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(libobjc, "objc_getClass");
	classGetInstanceMethod = (void *(*)(void *, void *))dlsym(libobjc, "class_getInstanceMethod");
	void *(*nsTempDir)(void) = (void *(*)(void))dlsym(libfoundation, "NSTemporaryDirectory");
	if (!msgSend || !selRegister || !getClassFn || !classGetInstanceMethod || !nsTempDir) {
		printf("FTD-DLOPEN objc/Foundation symbols missing (NSTemporaryDirectory=%p)\n",
		       (void *)nsTempDir);
		printf("FOUNDATION_TEMPDIR_VERDICT=RED\n");
		return 1;
	}

	clsNSFileManager = getClassFn("NSFileManager");
	clsNSString = getClassFn("NSString");
	printf("FTD-CLASS NSFileManager=%p NSString=%p\n", clsNSFileManager, clsNSString);

	/* 1. presence, reported rather than raised */
	void *m = classGetInstanceMethod(clsNSFileManager, selRegister("temporaryDirectory"));
	printf("FTD-MEMBER temporaryDirectory instance_method %s\n", m ? "present" : "ABSENT");
	if (!m) {
		printf("FTD-DECISION the accessor Xcode3Core's error-triage path sends is absent on this "
		       "deployed runtime\n");
		printf("FOUNDATION_TEMPDIR_VERDICT=RED\n");
		return 1;
	}

	/* 2. the value */
	void *fm = send0(clsNSFileManager, "defaultManager");
	void *url = send0(fm, "temporaryDirectory");
	if (!fm || !url) {
		printf("FTD-VALUE defaultManager=%p temporaryDirectory=%p\n", fm, url);
		printf("FOUNDATION_TEMPDIR_VERDICT=RED\n");
		return 1;
	}

	const char *absolute = cstr_of(send0(url, "absoluteString"));
	const char *path = cstr_of(send0(url, "path"));
	void *base = send0(url, "baseURL");
	unsigned char isfile = send_bool0(url, "isFileURL");
	void *ns_tmp = nsTempDir();
	const char *env_tmp = getenv("TMPDIR");

	printf("FTD-VALUE absoluteString=%s path=%s baseURL=%s isFileURL=%d\n", absolute, path,
	       base ? cstr_of(send0(base, "absoluteString")) : "(nil)", (int)isfile);
	printf("FTD-NSTEMPDIR value=%s env_TMPDIR=%s\n", cstr_of(ns_tmp), env_tmp ? env_tmp : "(unset)");

	if (base) {
		failures++;
		printf("FTD-CHECK %-30s MISMATCH a temporary directory is an absolute file URL\n", "baseURL");
	}
	if (!(int)isfile) {
		failures++;
		printf("FTD-CHECK %-30s MISMATCH not a file URL\n", "isFileURL");
	}
	/* 3. the two APIs of the same name agree inside the runtime, compared on the DIRECTORY MARKING
	 *    the URL carries. That marking is the trailing slash of absoluteString, not of -[NSURL path]:
	 *    CoreFoundation's CFURLCreateStringWithFileSystemPath strips the trailing separator of every
	 *    directory URL ("For Tiger, leave this behavior in for all path types", CFURL.c) -- so on
	 *    Darwin too, [temporaryDirectory path] is NSTemporaryDirectory() minus that separator while
	 *    absoluteString keeps it. Comparing the two strings directly is what the previous revision of
	 *    this fixture got wrong, and the deployed runtime is what showed it. */
	const char *tmp = cstr_of(ns_tmp);
	size_t len = strlen(absolute);
	if (len == 0 || absolute[len - 1] != '/') {
		failures++;
		printf("FTD-CHECK %-30s MISMATCH the URL does not carry its directory marking: %s\n",
		       "trailing_slash", absolute);
	} else {
		printf("FTD-CHECK %-30s ok\n", "trailing_slash");
	}
	char expected_absolute[4096];
	snprintf(expected_absolute, sizeof expected_absolute, "file://%s", tmp);
	check("absoluteString == file://<tmp>", expected_absolute, absolute);
	char expected_path[4096];
	snprintf(expected_path, sizeof expected_path, "%s", tmp);
	size_t plen = strlen(expected_path);
	while (plen > 1 && expected_path[plen - 1] == '/')
		expected_path[--plen] = '\0';
	check("path == NSTemporaryDirectory()", expected_path, path);

	/* 4. it exists and is a directory in the guest's own filesystem */
	struct stat st;
	if (stat(path, &st) != 0) {
		failures++;
		printf("FTD-CHECK %-30s MISMATCH stat failed (errno set), path does not exist\n",
		       "exists");
	} else if (!S_ISDIR(st.st_mode)) {
		failures++;
		printf("FTD-CHECK %-30s MISMATCH not a directory (mode=%o)\n", "is_directory",
		       (unsigned)st.st_mode);
	} else {
		printf("FTD-CHECK %-30s ok (mode=%o uid=%u)\n", "exists_and_is_directory",
		       (unsigned)st.st_mode, (unsigned)st.st_uid);
	}

	/* 5. usable: the very next step the Xcode call site performs on this URL */
	char name[128];
	snprintf(name, sizeof name, "foundation-tempdir-oracle-%d", (int)getpid());
	void *appended = send1(url, "URLByAppendingPathComponent:", nsstr(name));
	if (!appended) {
		failures++;
		printf("FTD-CHECK %-30s MISMATCH URLByAppendingPathComponent: returned nil\n", "append");
	} else {
		const char *apath = cstr_of(send0(appended, "path"));
		char expected_apath[4096];
		/* built from NSTemporaryDirectory() (which keeps its separator), not from the stripped
		 * -[NSURL path] of the temporary directory */
		snprintf(expected_apath, sizeof expected_apath, "%s%s", tmp, name);
		printf("FTD-APPEND path=%s\n", apath);
		check("appended.absolute_path", expected_apath, apath);
		if (mkdir(apath, 0700) != 0) {
			failures++;
			printf("FTD-CHECK %-30s MISMATCH mkdir failed\n", "usable");
		} else if (rmdir(apath) != 0) {
			failures++;
			printf("FTD-CHECK %-30s MISMATCH rmdir failed\n", "usable");
		} else {
			printf("FTD-CHECK %-30s ok\n", "usable");
		}
	}

	printf("FTD-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("FOUNDATION_TEMPDIR_VERDICT=GREEN\n");
	else
		printf("FOUNDATION_TEMPDIR_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

/*
 * cfurl_reachability_guest.c -- guest oracle for the reachability primitive the Xcode project reader
 * asks about a project path: `CFURLResourceIsReachable` / `-[NSURL checkResourceIsReachableAndReturnError:]`.
 *
 * WHY THIS EXISTS. Both are a `#warning TODO FIXME; return false;` stub in this tree
 * (`src/external/corefoundation/NSURL.m`), so on the deployed runtime EVERY file URL answers
 * "not reachable" -- an existing project directory included -- and the error CFURL.h promises for a
 * false answer ("If this function returns false, the optional error is populated") is never set.
 * Apple's own open-source overlay (`FoundationEssentials/URL/URL.swift`, in this tree) implements
 * `URL.checkResourceIsReachable()` as
 *
 *     let result = ns.checkResourceIsReachableAndReturnError(&error)
 *     if let e = error { throw e } else { return result }
 *
 * so with this stub a caller gets `false` and NO error to throw: "the resource does not exist" about
 * a resource that does. The contract this oracle asserts is the one the header and the overlay
 * together state, not a guess:
 *
 *   existing file / directory -> true, and no error
 *   missing file / directory  -> false, and a POPULATED NSError in NSCocoaErrorDomain (the code is
 *                                reported; NSFileNoSuchFileError 4 and NSFileReadNoSuchFileError 260
 *                                are both accepted because Apple's two open-source paths disagree,
 *                                see the README)
 *   non-file URL              -> false
 *
 * WHAT IT MEASURES, per case, in one guest process on the DEPLOYED runtime: the raw CF function
 * (`CFURLResourceIsReachable`, `dlsym` from the deployed CoreFoundation), the ObjC member
 * (`-[NSURL checkResourceIsReachableAndReturnError:]`, sent with objc_msgSend), that member's NSError
 * (domain, code, localizedDescription, userInfo keys) and the CFError from the raw function. Plain
 * I/O and dlopen/dlsym/objc_msgSend only -- no compile-time reference to CoreFoundation or
 * Foundation -- so an arm measures the DEPLOYED runtime and not this tree's headers.
 *
 * The tree it measures is created by the fixture itself under /private/var/tmp, so the bytes are a
 * literal in this file and the same in both arms:
 *
 *   /private/var/tmp/cfurl-reach-fixture/file.txt        "cfurl-reach\n"
 *   /private/var/tmp/cfurl-reach-fixture/adir/            a directory
 *   /private/var/tmp/cfurl-reach-fixture/adir/inner.txt  "cfurl-reach-inner\n"
 *   cases: existing-file=file.txt  existing-dir=adir  missing-leaf=adir/nope
 *          missing-dir=nope  non-file=https://example.com/
 *
 * argv[1]: the case (required). One ctest entry per case, so a trap takes only its own entry down
 * and the failing entry names the case.
 *
 * Verdict line: CFURL_REACH_VERDICT=GREEN ... (OK_MARKER) / CFURL_REACH_VERDICT=RED ...
 */
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define CF_LIBRARY "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FIXTURE_DIR "/private/var/tmp/cfurl-reach-fixture"

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);
static void *clsNSStringG, *clsNSURLG;

static void *send0(void *o, const char *sel) { return msgSend(o, selRegister(sel)); }
static void *send1(void *o, const char *sel, void *a) { return msgSend(o, selRegister(sel), a); }
static void *send2(void *o, const char *sel, void *a, void *b) { return msgSend(o, selRegister(sel), a, b); }
static long send_long0(void *o, const char *sel) { return (long)(intptr_t)msgSend(o, selRegister(sel)); }

/* unsigned char (Darwin BOOL) return with one object argument -- the two reachability members. */
static unsigned char send_bool_err(void *o, const char *sel, void **errp)
{
	unsigned char r;
	void *e = NULL;
	r = (unsigned char)(uintptr_t)msgSend(o, selRegister(sel), (void *)&e);
	if (errp)
		*errp = e;
	return r;
}

static void *nsstr(const char *s)
{
	return send1(clsNSStringG, "stringWithUTF8String:", (void *)s);
}

static const char *cstr(void *s)
{
	const char *p;

	if (!s)
		return "(nil)";
	p = (const char *)send0(s, "UTF8String");
	return p ? p : "(null)";
}

/* CoreFoundation, resolved from the DEPLOYED image by dlsym. */
static unsigned char (*CFURLResourceIsReachable)(void *, void **);
static void *(*CFErrorGetDomain)(void *);
static long (*CFErrorGetCode)(void *);
static void *(*CFErrorCopyDescription)(void *);
static void *(*CFStringCreateWithCString)(void *, const char *, unsigned);
static void *(*CFURLCreateWithString)(void *, void *, void *);
static unsigned char (*CFStringGetCString)(void *, char *, long, unsigned);

static void cfstr_to_c(void *s, char *out, size_t n)
{
	out[0] = 0;
	if (s)
		CFStringGetCString(s, out, (long)n, 0x08000100 /* kCFStringEncodingUTF8 */);
}

static void print_cf_error(const char *label, void *cfError)
{
	char dom[256], desc[512];

	if (!cfError) {
		printf("%s cf-error=none\n", label);
		return;
	}
	cfstr_to_c(CFErrorGetDomain(cfError), dom, sizeof(dom));
	desc[0] = 0;
	if (CFErrorCopyDescription)
		cfstr_to_c(CFErrorCopyDescription(cfError), desc, sizeof(desc));
	printf("%s cf-error domain=%s code=%ld desc=%s\n", label, dom, CFErrorGetCode(cfError), desc);
}

static void print_ns_error(const char *label, void *err)
{
	void *dom, *desc, *ui;

	if (!err) {
		printf("%s ns-error=none\n", label);
		return;
	}
	dom = send0(err, "domain");
	desc = send0(err, "localizedDescription");
	ui = send0(err, "userInfo");
	printf("%s ns-error domain=%s code=%ld localized=%s userInfo-keys=%s\n", label, cstr(dom),
	       send_long0(err, "code"), cstr(desc), cstr(send0(ui, "allKeys")));
}

static void make_fixture_tree(void)
{
	int fd;

	mkdir(FIXTURE_DIR, 0755);
	mkdir(FIXTURE_DIR "/adir", 0755);
	fd = open(FIXTURE_DIR "/file.txt", O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (fd >= 0) {
		(void)!write(fd, "cfurl-reach\n", 12);
		close(fd);
	}
	fd = open(FIXTURE_DIR "/adir/inner.txt", O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (fd >= 0) {
		(void)!write(fd, "cfurl-reach-inner\n", 18);
		close(fd);
	}
}

int main(int argc, char **argv)
{
	const char *caseName = argc > 1 ? argv[1] : "";
	char path[512];
	void *cf, *objc, *url, *nsErr = NULL, *cfErr = NULL, *nsFileUrl;
	unsigned char reachNs, reachCf;
	int isFile = 0, exists = 0, csdir = 0;
	int wantReachable = -1, wantError = -1, green = 1;
	struct stat st;

	printf("CFURL_REACH case=%s\n", caseName);

	objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !cf) {
		printf("CFURL_REACH_VERDICT=RED case=%s dlopen-failed objc=%p cf=%p dlerror=%s\n", caseName,
		       objc, cf, dlerror() ? dlerror() : "(none)");
		return 0;
	}
	msgSend = (void *(*)(void *, void *, ...))dlsym(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(objc, "objc_getClass");
	CFURLResourceIsReachable = (unsigned char (*)(void *, void **))dlsym(cf, "CFURLResourceIsReachable");
	CFErrorGetDomain = (void *(*)(void *))dlsym(cf, "CFErrorGetDomain");
	CFErrorGetCode = (long (*)(void *))dlsym(cf, "CFErrorGetCode");
	CFErrorCopyDescription = (void *(*)(void *))dlsym(cf, "CFErrorCopyDescription");
	CFStringCreateWithCString = (void *(*)(void *, const char *, unsigned))dlsym(cf, "CFStringCreateWithCString");
	CFURLCreateWithString = (void *(*)(void *, void *, void *))dlsym(cf, "CFURLCreateWithString");
	CFStringGetCString = (unsigned char (*)(void *, char *, long, unsigned))dlsym(cf, "CFStringGetCString");
	clsNSStringG = getClassFn ? getClassFn("NSString") : NULL;
	clsNSURLG = getClassFn ? getClassFn("NSURL") : NULL;
	printf("CFURL_REACH_LOAD objc=%s cf=%s NSString=%p NSURL=%p\n", dlerror() ? "?" : "ok",
	       "loaded", clsNSStringG, clsNSURLG);
	if (!msgSend || !selRegister || !clsNSStringG || !clsNSURLG || !CFURLResourceIsReachable ||
	    !CFErrorGetDomain || !CFErrorGetCode || !CFStringGetCString) {
		printf("CFURL_REACH_VERDICT=RED case=%s missing-symbol\n", caseName);
		return 0;
	}

	make_fixture_tree();

	if (strcmp(caseName, "existing-file") == 0) {
		snprintf(path, sizeof(path), "%s/file.txt", FIXTURE_DIR);
		wantReachable = 1;
		wantError = 0;
	} else if (strcmp(caseName, "existing-dir") == 0) {
		snprintf(path, sizeof(path), "%s/adir", FIXTURE_DIR);
		wantReachable = 1;
		wantError = 0;
		isFile = 0;
		csdir = 1;
	} else if (strcmp(caseName, "missing-leaf") == 0) {
		snprintf(path, sizeof(path), "%s/adir/nope", FIXTURE_DIR);
		wantReachable = 0;
		wantError = 1;
	} else if (strcmp(caseName, "missing-dir") == 0) {
		snprintf(path, sizeof(path), "%s/nope", FIXTURE_DIR);
		wantReachable = 0;
		wantError = 1;
		csdir = 1;
	} else if (strcmp(caseName, "non-file") == 0) {
		path[0] = 0;
		wantReachable = 0;
		wantError = -1;
	} else {
		printf("CFURL_REACH_VERDICT=RED case=%s unknown-case\n", caseName);
		return 0;
	}

	exists = (strcmp(caseName, "non-file") == 0) ? 0 : (stat(path, &st) == 0);
	printf("CFURL_REACH_INPUT path=%s exists=%d isdir=%d want-reachable=%d want-error=%d\n", path,
	       exists, (exists && S_ISDIR(st.st_mode)) ? 1 : 0, wantReachable, wantError);

	if (strcmp(caseName, "non-file") == 0) {
		url = send0(clsNSURLG, "alloc");
		url = send2(url, "initWithString:", nsstr("https://example.com/"), NULL);
	} else {
		url = send2(send0(clsNSURLG, "alloc"), "initFileURLWithPath:isDirectory:",
			    nsstr(path), (void *)(intptr_t)csdir);
	}
	nsFileUrl = url;
	printf("CFURL_REACH_URL class=%p isFileURL=%ld abs=%s path=%s\n", url,
	       url ? send_long0(url, "isFileURL") : -1, url ? cstr(send0(url, "absoluteString")) : "(nil)",
	       url ? cstr(send0(url, "path")) : "(nil)");
	if (!url) {
		printf("CFURL_REACH_VERDICT=RED case=%s url-nil\n", caseName);
		return 0;
	}

	/* The ObjC member the workload's callers actually send. */
	nsErr = NULL;
	reachNs = send_bool_err(url, "checkResourceIsReachableAndReturnError:", &nsErr);
	printf("CFURL_REACH_NSURL reachable=%d\n", (int)reachNs);
	print_ns_error("CFURL_REACH_NSURL", nsErr);

	/* The raw CF function, on the SAME object (CFURL and NSURL are toll-free bridged here). */
	cfErr = NULL;
	reachCf = CFURLResourceIsReachable(url, &cfErr);
	printf("CFURL_REACH_CF reachable=%d\n", (int)reachCf);
	print_cf_error("CFURL_REACH_CF", cfErr);

	if (wantReachable >= 0 && (int)reachNs != wantReachable) {
		printf("CFURL_REACH_MISMATCH nsurl-reachable=%d want=%d\n", (int)reachNs, wantReachable);
		green = 0;
	}
	if (wantReachable >= 0 && (int)reachCf != wantReachable) {
		printf("CFURL_REACH_MISMATCH cf-reachable=%d want=%d\n", (int)reachCf, wantReachable);
		green = 0;
	}
	if (wantError == 0 && nsErr != NULL) {
		printf("CFURL_REACH_MISMATCH nsurl-error-set-for-an-existing-resource\n");
		green = 0;
	}
	if (wantError == 1 && nsErr == NULL) {
		printf("CFURL_REACH_MISMATCH nsurl-error-not-populated-for-a-missing-resource\n");
		green = 0;
	}
	if (wantError == 1) {
		long code = send_long0(nsErr, "code");
		if (strcmp(cstr(send0(nsErr, "domain")), "NSCocoaErrorDomain") != 0) {
			printf("CFURL_REACH_MISMATCH nsurl-error-domain=%s\n", cstr(send0(nsErr, "domain")));
			green = 0;
		}
		if (code != 4 && code != 260) {
			printf("CFURL_REACH_MISMATCH nsurl-error-code=%ld (accepted: 4|260)\n", code);
			green = 0;
		}
		if (cfErr == NULL) {
			printf("CFURL_REACH_MISMATCH cf-error-not-populated-for-a-missing-resource\n");
			green = 0;
		}
	}

	printf("CFURL_REACH_VERDICT=%s case=%s reachable-ns=%d reachable-cf=%d ns-error=%s\n",
	       green ? "GREEN" : "RED", caseName, (int)reachNs, (int)reachCf, nsErr ? "yes" : "no");
	return 0;
}

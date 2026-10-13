/*
 * dictionary_contents_of_url_error_guest.c -- permanent guest oracle for the ERROR CONTRACT of the
 * plist readers +[NSDictionary dictionaryWithContentsOfURL:error:] /
 * -[NSDictionary initWithContentsOfURL:error:] on the deployed CoreFoundation + Foundation.
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder (scripts/xcodebuild-workload-run.sh, both
 * workloads) reached a wall where the workload died by signal with no build message at all:
 *
 *   [MT] DVTAssertions: ASSERTION FAILURE in DVTFrameworks/DVTFoundation/DeveloperStructure/DVTToolsVersion.m:136
 *   Details:  Failed to read beta plist: Error Domain=NSDictionaryErrorDomain Code=-1 "The operation couldn\u2019t be completed. (NSDictionaryErrorDomain error -1.)"
 *   Method:   +setCurrentBetaVersionFromPlistFile:
 *   3   +[DVTToolsVersion setCurrentBetaVersionFromPlistFile:].cold.2 (in DVTFoundation)
 *   4   +[DVTToolsVersion setCurrentBetaVersionFromPlistFile:] (in DVTFoundation)
 *   5   IDEInitialize (in IDEFoundation)
 *   [sigexc-fatal sig=4 code=2 addr=0x7F6C6CC2C47F pid=1187874 tid=1187874]
 *   XCB_RC=132
 *
 * The plist it read is Xcode's own Contents/Resources/BetaVersion.plist, which a release Xcode does
 * not ship (the tree names the path in IDEFoundation and no such file exists anywhere in
 * Xcode.app). The read therefore legitimately fails, and the caller is written to accept exactly
 * that: disassembling DVTFoundation's +[DVTToolsVersion setCurrentBetaVersionFromPlistFile:]
 * (x86_64 slice, 0x54945) shows the failure path sending -[NSError dvt_hasDomain:errorCode:] twice,
 * with NSCocoaErrorDomain and codes 260 (NSFileReadNoSuchFileError) and 4 (NSFileNoSuchFileError),
 * and taking the assertion only when neither matches. Darling's reader answered with a made-up
 * error -- NSDictionaryErrorDomain code -1, a domain that exists nowhere else in this tree -- so the
 * caller could not recognize "the file is not there" and killed xcodebuild through its assertion
 * handler's ud2 (libsystem_c.dylib file offset 0xF247D).
 *
 * WHAT THIS MEASURES, in one process, with no compile-time reference to Foundation or ObjC
 * (dlopen + objc_msgSend + sel_registerName only):
 *
 *   1. ABSENT: a path that does not exist -> the reader returns nil and the error says why:
 *      NSCocoaErrorDomain/NSFileReadNoSuchFileError (260) with NSUnderlyingError
 *      NSPOSIXErrorDomain/ENOENT (2). The underlying POSIX pair is gated deliberately: it is the
 *      errno of the open(2) that failed, so an error that merely spells "260" without having
 *      attempted the read does not pass this oracle.
 *   2. PRESENT: a minimal plist written by the fixture itself must still parse -- non-nil
 *      dictionary, the expected key and value, and NO error set on the way out (the reader must not
 *      invent one for a read that worked).
 *   3. WALL: when argv[1] names the ladder's own beta-plist path, reading it must either succeed or
 *      fail with exactly the domain/code pair the DVT caller tolerates (NSCocoaErrorDomain 260 or
 *      4). That is the wall's own contract, measured on the wall's own path.
 *
 * A DIAGNOSTIC, not a gate: the plist PARSE failure path (data readable, not a property list) is
 * reported as DICTURL-NOTE parse-* and is NOT gated here. The reader's historic shape for it --
 * CFPropertyListCreateWithData returning NULL is still handed to -[NSDictionary
 * initWithDictionary:] -- is left as it was by the fix under test, so gating it would gate a
 * pre-existing behaviour rather than this contract. The fix's own change there is that the property
 * list's CFError now reaches the caller instead of an invented one.
 *
 * Every intermediate fact is printed unconditionally as a DICTURL-* line, so a RED arm quotes the
 * failure from the run's own output; DICTURL_VERDICT=GREEN is the machine-readable line the harness
 * ok-marker reads.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the unfixed CoreFoundation answers the
 * absent case with the invented domain, so the RED line is the runtime's own error and not a
 * fixture verdict.
 *
 * MEASURED RED (prefix-verify14, the parent CoreFoundation rebuilt from this tree and deployed to BOTH
 * copies, sha256 6d2690e3..., with Foundation 71553644..., 2026-10-13):
 *   DICTURL-ABSENT path=/private/var/tmp/dicturl-absent-1246854.plist nil=1 domain=NSDictionaryErrorDomain code=-1
 *   DICTURL-ABSENT-UNDERLYING underlying=(none)
 *   DICTURL-MISMATCH absent-domain got=NSDictionaryErrorDomain/-1 want=NSCocoaErrorDomain/260
 *   DICTURL-MISMATCH absent-error carries no underlying POSIX error, so the 260 was not derived from a failed read
 *   DICTURL-PRESENT path=/private/var/tmp/dicturl-present-1246854.plist nil=0 count=2 seed=26A123 error=(none)/0
 *   DICTURL-WALL path=/Volumes/SystemRoot/.../Xcode.app/Contents/Resources/BetaVersion.plist nil=1 count=-1 domain=NSDictionaryErrorDomain code=-1 caller-tolerates=0
 *   DICTURL-MISMATCH wall: the DVT caller asserts on NSDictionaryErrorDomain/-1 for this path
 *   DICTURL-NOTE parse-corrupt nil=0 error=(none)/0
 *   DICTURL-FAIL failures=3
 *   DICTURL_VERDICT=RED
 * MEASURED GREEN (same prefix, fixed CoreFoundation sha256 4642b1cb... + Foundation 9fa81e94..., same
 * driver and same harness declaration):
 *   DICTURL-ABSENT path=/private/var/tmp/dicturl-absent-1262754.plist nil=1 domain=NSCocoaErrorDomain code=260
 *   DICTURL-ABSENT-UNDERLYING underlying=NSPOSIXErrorDomain/2
 *   DICTURL-PRESENT path=/private/var/tmp/dicturl-present-1262754.plist nil=0 count=2 seed=26A123 error=(none)/0
 *   DICTURL-WALL path=/Volumes/SystemRoot/.../Xcode.app/Contents/Resources/BetaVersion.plist nil=1 count=-1 domain=NSCocoaErrorDomain code=260 caller-tolerates=1
 *   DICTURL-NOTE parse-corrupt nil=0 error=NSCocoaErrorDomain/3840
 *   DICTURL_VERDICT=GREEN
 */

#include <dlfcn.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
#define CF_LIBRARY "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"

/* The two error facts the DVT caller tests, and the codes it tolerates (see the header). */
#define COCOA_DOMAIN "NSCocoaErrorDomain"
#define POSIX_DOMAIN "NSPOSIXErrorDomain"
#define FILE_READ_NO_SUCH_FILE 260
#define FILE_NO_SUCH_FILE 4

/* Apple's NSError userInfo keys, whose VALUES are these strings. */
#define UNDERLYING_ERROR_KEY "NSUnderlyingError"

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
		printf("DICTURL-NOTE symbol=%s error=%s\n", name, dlerror());
	}
	return found;
}

static void *send(void *object, const char *selector)
{
	return msgSend(object, selRegister(selector));
}

static void *send1(void *object, const char *selector, void *argument)
{
	return msgSend(object, selRegister(selector), argument);
}

/* An NSString built without a Foundation reference at compile time: the class is looked up by name
 * and every string in this fixture is created through it. */
static void *string_from_c(void *string_class, const char *c)
{
	return send1(string_class, "stringWithUTF8String:", (void *)c);
}

static const char *c_from_string(void *string)
{
	return string ? (const char *)send(string, "UTF8String") : "(nil)";
}

/* The reader under test, once, with the error out-parameter the caller passes. */
static void *read_plist(void *dict_class, void *url_class, void *string_class, const char *path, void **error_out)
{
	void *url = send1(url_class, "fileURLWithPath:", string_from_c(string_class, path));
	if (!url) {
		printf("DICTURL-NOTE fileURLWithPath=nil path=%s\n", path);
		*error_out = NULL;
		return NULL;
	}
	void *error = NULL;
	void *dict = msgSend(dict_class, selRegister("dictionaryWithContentsOfURL:error:"), url, &error);
	*error_out = error;
	return dict;
}

static const char *error_domain(void *error, long *code_out)
{
	if (!error) {
		*code_out = 0;
		return "(none)";
	}
	*code_out = (long)msgSend(error, selRegister("code"));
	return c_from_string(send(error, "domain"));
}

/* The POSIX error the reader wrapped, as "domain/code", or "(none)". */
static void report_underlying(void *error)
{
	if (!error) {
		printf(" underlying=(none)\n");
		return;
	}
	void *userInfo = send(error, "userInfo");
	void *underlying = userInfo
	    ? send1(userInfo, "objectForKey:", string_from_c(getClassFn("NSString"), UNDERLYING_ERROR_KEY))
	    : NULL;
	if (!underlying) {
		printf(" underlying=(none)\n");
		return;
	}
	long code = 0;
	const char *domain = error_domain(underlying, &code);
	printf(" underlying=%s/%ld\n", domain, code);
	if (strcmp(domain, POSIX_DOMAIN) != 0) {
		printf("DICTURL-MISMATCH underlying-domain got=%s want=%s\n", domain, POSIX_DOMAIN);
		failures++;
	}
}

/* The caller's tolerance, as the call the DVT assertion performs. */
static int caller_tolerates(const char *domain, long code)
{
	return strcmp(domain, COCOA_DOMAIN) == 0
	    && (code == FILE_READ_NO_SUCH_FILE || code == FILE_NO_SUCH_FILE);
}

/* 1. ABSENT -- the wall's shape: the plist is legitimately not there. */
static void check_absent(void *dict_class, void *url_class, void *string_class)
{
	char path[128];
	snprintf(path, sizeof path, "/private/var/tmp/dicturl-absent-%d.plist", (int)getpid());

	void *error = NULL;
	void *dict = read_plist(dict_class, url_class, string_class, path, &error);
	long code = 0;
	const char *domain = error_domain(error, &code);
	printf("DICTURL-ABSENT path=%s nil=%d domain=%s code=%ld\n", path, dict == NULL, domain, code);
	printf("DICTURL-ABSENT-UNDERLYING");
	report_underlying(error);
	if (dict != NULL) {
		printf("DICTURL-MISMATCH absent-read returned a dictionary\n");
		failures++;
	}
	if (strcmp(domain, COCOA_DOMAIN) != 0 || code != FILE_READ_NO_SUCH_FILE) {
		printf("DICTURL-MISMATCH absent-domain got=%s/%ld want=%s/%d\n",
		    domain, code, COCOA_DOMAIN, FILE_READ_NO_SUCH_FILE);
		failures++;
	}
	void *userInfo = error ? send(error, "userInfo") : NULL;
	void *underlying = userInfo
	    ? send1(userInfo, "objectForKey:", string_from_c(getClassFn("NSString"), UNDERLYING_ERROR_KEY))
	    : NULL;
	if (!underlying) {
		printf("DICTURL-MISMATCH absent-error carries no underlying POSIX error, so the 260 was not derived from a failed read\n");
		failures++;
	} else {
		long underlying_code = 0;
		const char *underlying_domain = error_domain(underlying, &underlying_code);
		if (strcmp(underlying_domain, POSIX_DOMAIN) != 0 || underlying_code != ENOENT) {
			printf("DICTURL-MISMATCH absent-underlying got=%s/%ld want=%s/%d\n",
			    underlying_domain, underlying_code, POSIX_DOMAIN, ENOENT);
			failures++;
		}
	}
}

/* 2. PRESENT -- a minimal plist must still parse, and must not collect an error. */
static const char *minimal_plist =
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
    "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
    "<plist version=\"1.0\">\n"
    "<dict>\n"
    "\t<key>SeedNumber</key>\n"
    "\t<string>26A123</string>\n"
    "\t<key>BetaVersionString</key>\n"
    "\t<string>26.3 beta</string>\n"
    "</dict>\n"
    "</plist>\n";

static void check_present(void *dict_class, void *url_class, void *string_class)
{
	char path[128];
	snprintf(path, sizeof path, "/private/var/tmp/dicturl-present-%d.plist", (int)getpid());

	FILE *file = fopen(path, "wb");
	if (!file) {
		printf("DICTURL-FAIL stage=write-plist path=%s errno=%d\n", path, errno);
		failures++;
		return;
	}
	fputs(minimal_plist, file);
	fclose(file);

	void *error = NULL;
	void *dict = read_plist(dict_class, url_class, string_class, path, &error);
	long code = 0;
	const char *domain = error_domain(error, &code);
	long count = dict ? (long)msgSend(dict, selRegister("count")) : -1;
	void *seed = dict
	    ? send1(dict, "objectForKey:", string_from_c(string_class, "SeedNumber"))
	    : NULL;
	const char *seed_value = c_from_string(seed);
	printf("DICTURL-PRESENT path=%s nil=%d count=%ld seed=%s error=%s/%ld\n",
	    path, dict == NULL, count, seed_value, domain, code);

	if (dict == NULL) {
		printf("DICTURL-MISMATCH present-read returned nil (the reader must parse a real plist)\n");
		failures++;
	}
	if (error != NULL) {
		printf("DICTURL-MISMATCH present-read set an error for a read that succeeded\n");
		failures++;
	}
	if (count != 2) {
		printf("DICTURL-MISMATCH present-count got=%ld want=2\n", count);
		failures++;
	}
	if (!seed || strcmp(seed_value, "26A123") != 0) {
		printf("DICTURL-MISMATCH present-seed got=%s want=26A123\n", seed_value);
		failures++;
	}
	remove(path);
}

/* 3. WALL -- the ladder's own beta-plist path, when the runner names it. */
static void check_wall(void *dict_class, void *url_class, void *string_class, const char *path)
{
	void *error = NULL;
	void *dict = read_plist(dict_class, url_class, string_class, path, &error);
	long code = 0;
	const char *domain = error_domain(error, &code);
	long count = dict ? (long)msgSend(dict, selRegister("count")) : -1;
	printf("DICTURL-WALL path=%s nil=%d count=%ld domain=%s code=%ld caller-tolerates=%d\n",
	    path, dict == NULL, count, domain, code, caller_tolerates(domain, code));

	if (dict == NULL && !caller_tolerates(domain, code)) {
		printf("DICTURL-MISMATCH wall: the DVT caller asserts on %s/%ld for this path\n", domain, code);
		failures++;
	}
}

/* A NOTE, never a gate: the parse-failure shape (data readable, not a property list). */
static void note_parse_failure(void *dict_class, void *url_class, void *string_class)
{
	char path[128];
	snprintf(path, sizeof path, "/private/var/tmp/dicturl-corrupt-%d.plist", (int)getpid());
	FILE *file = fopen(path, "wb");
	if (!file) {
		printf("DICTURL-NOTE parse-corrupt=write-failed\n");
		return;
	}
	fputs("this is not a property list at all\n", file);
	fclose(file);

	void *error = NULL;
	void *dict = read_plist(dict_class, url_class, string_class, path, &error);
	long code = 0;
	const char *domain = error_domain(error, &code);
	printf("DICTURL-NOTE parse-corrupt nil=%d error=%s/%ld\n", dict == NULL, domain, code);
	remove(path);
}

int main(int argc, char **argv)
{
	void *objc;
	void *foundation;
	void *cf;

	setvbuf(stdout, NULL, _IOLBF, 0);

	objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation || !cf) {
		printf("DICTURL-FAIL stage=dlopen objc=%p foundation=%p cf=%p err=%s\n", objc, foundation, cf, dlerror());
		printf("DICTURL_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))lookup(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))lookup(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))lookup(objc, "objc_getClass");
	classNameFn = (const char *(*)(void *))lookup(objc, "class_getName");
	if (!msgSend || !selRegister || !getClassFn || !classNameFn) {
		printf("DICTURL-FAIL stage=runtime-symbols\n");
		printf("DICTURL_VERDICT=RED\n");
		return 1;
	}

	void *string_class = lookup(foundation, "_OBJC_CLASS_$_NSString");
	if (!string_class) {
		string_class = lookup(cf, "_OBJC_CLASS_$_NSString");
	}
	void *dict_class = getClassFn("NSDictionary");
	if (!dict_class) {
		dict_class = lookup(foundation, "_OBJC_CLASS_$_NSDictionary");
	}
	void *url_class = getClassFn("NSURL");
	if (!url_class) {
		url_class = lookup(foundation, "_OBJC_CLASS_$_NSURL");
	}
	if (!string_class || !dict_class || !url_class) {
		printf("DICTURL-FAIL stage=classes string=%p dictionary=%p url=%p\n", string_class, dict_class, url_class);
		printf("DICTURL_VERDICT=RED\n");
		return 1;
	}
	printf("DICTURL-CLASS dictionary=%s url=%s image-foundation=%p image-cf=%p\n",
	    classNameFn(dict_class), classNameFn(url_class), foundation, cf);

	check_absent(dict_class, url_class, string_class);
	check_present(dict_class, url_class, string_class);
	if (argc > 1) {
		check_wall(dict_class, url_class, string_class, argv[1]);
	} else {
		printf("DICTURL-NOTE wall=not-named (pass the beta-plist path as argv[1] to gate the wall's own contract)\n");
	}
	note_parse_failure(dict_class, url_class, string_class);

	if (failures != 0) {
		printf("DICTURL-FAIL failures=%d\n", failures);
		printf("DICTURL_VERDICT=RED\n");
		return 1;
	}
	printf("DICTURL_VERDICT=GREEN\n");
	return 0;
}

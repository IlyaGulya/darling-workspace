/*
 * xcodebuild_project_read_probe.m -- a guest-injected diagnostic (DYLD_INSERT_LIBRARIES) for the
 * project-read wall: WHAT DOES THE READER ASK, AND WHAT DOES IT DO WITH THE ANSWER?
 *
 * WHY. The wall is `xcodebuild: error: Unable to read project 'iosapp.xcodeproj'. Reason: The
 * operation couldn't be completed. (NSCocoaErrorDomain error 4.)`, and the reader never opens
 * project.pbxproj -- so the failing step is an existence/type/workspace question about the project
 * path, and the answer that question receives decides everything. Two candidate causes need opposite
 * fixes and are indistinguishable from the outside:
 *
 *   (b) a runtime primitive lies. `CFURLResourceIsReachable` is a `#warning TODO FIXME; return false;`
 *       stub in this tree (`src/external/corefoundation/NSURL.m`), so `-[NSURL
 *       checkResourceIsReachableAndReturnError:]` answers "not reachable" for EVERY file URL --
 *       existing ones included -- and never populates the error CFURL.h promises. Xcode's own code
 *       asks that question (`Xcode3Core` imports the Swift symbol
 *       `Foundation.URL.checkResourceIsReachable()`; DVTFoundation/IDEFoundation send the ObjC
 *       member), and Apple's overlay resolves it as `if let e = error { throw e } else { return
 *       result }` -- so a caller gets `false` and nothing to throw.
 *   (a) the fixture is an incomplete .xcodeproj (no project.xcworkspace), and the reader asks about
 *       THAT path -- in which case a correct runtime would also answer "no" and the reader would
 *       bail, exactly as the previous rung's fixture-vs-runtime question asks.
 *
 * This probe answers both by instrumenting the questions themselves, and it is an INSTRUMENT, never a
 * fix: it is injected, absent from the artifact, and never deployed.
 *
 * WHAT IT HOOKS (every hook logs one line, written with a single write(2), to PRJ_LOG):
 *
 *   -[NSURL checkResourceIsReachableAndReturnError:]            (answer, path, exists, caller site)
 *   -[NSURL checkPromisedItemIsReachableAndReturnError:]
 *   -[NSFileManager fileExistsAtPath:] / ...:isDirectory:       (the existence question)
 *   -[NSFileManager contentsOfDirectoryAtPath:error:]           (the enumeration question)
 *   -[NSFileManager attributesOfItemAtPath:error:]              (the type question)
 *   -[NSFileManager destinationOfSymbolicLinkAtPath:error:]
 *   -[NSData initWithContentsOfFile:options:error:]
 *   -[NSString initWithContentsOfFile:encoding:error:]
 *   +[NSDictionary dictionaryWithContentsOfFile:]
 *   +[NSDictionary dictionaryWithContentsOfURL:]
 *   -[NSBundle initWithPath:]
 *
 * Every NSError that comes back is logged with domain, code and userInfo keys -- which is the datum
 * the tool's own message drops (it prints only `localizedDescription`, and this tree's CFError.c
 * produces the generic "The operation couldn't be completed. (NSCocoaErrorDomain error 4.)" whenever
 * the error carries no description).
 *
 * PRJ_FORCE=1 CHANGES ONE ANSWER, ON PURPOSE, so the second question ("what does the reader do with a
 * CORRECT answer?") can be measured without deploying anything: reachability is answered from the
 * filesystem (`stat`) instead of the stub, exactly as a fixed runtime would answer it.
 *
 * BUILD (in the guest, with the ladder's own toolchain):
 *   <Xcode>/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang \
 *     -isysroot <Xcode>/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk \
 *     -dynamiclib -framework Foundation -o libprojread.dylib xcodebuild_project_read_probe.m
 */
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <dlfcn.h>
#import <fcntl.h>
#import <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int g_fd = -1;
static int g_lines = 0;
#define PRJ_MAX_LINES 20000

/* Re-entrancy guard. These hooks are installed on Foundation methods that Foundation itself calls, so
 * a hook's own logging must not drive another hook (a log line that calls a hooked method would
 * recurse). One flag per thread, set only around the probe's OWN Foundation calls. */
static __thread int prj_busy;

static void prj_log(const char *fmt, ...)
{
	char buf[1600];
	int n;
	va_list ap;

	if (prj_busy || g_lines > PRJ_MAX_LINES)
		return;
	if (g_fd < 0) {
		const char *p = getenv("PRJ_LOG");
		if (p == NULL || p[0] == '\0')
			return;
		g_fd = open(p, O_WRONLY | O_CREAT | O_APPEND, 0644);
		if (g_fd < 0)
			return;
	}
	g_lines++;
	prj_busy = 1;
	n = snprintf(buf, sizeof(buf), "[prjread pid=%d tid=%lx] ", (int)getpid(), (unsigned long)pthread_self());
	va_start(ap, fmt);
	n += vsnprintf(buf + n, sizeof(buf) - (size_t)n - 2, fmt, ap);
	va_end(ap);
	if (n < 0)
		return;
	if (n > (int)sizeof(buf) - 2)
		n = (int)sizeof(buf) - 2;
	buf[n++] = '\n';
	(void)!write(g_fd, buf, (size_t)n);
	prj_busy = 0;
}

static void prj_site(void *ra, char *out, size_t n)
{
	Dl_info info;

	if (ra != NULL && dladdr(ra, &info) && info.dli_fname != NULL)
		snprintf(out, n, "%s+0x%llx", info.dli_fname,
			 (unsigned long long)((uintptr_t)ra - (uintptr_t)info.dli_fbase));
	else
		snprintf(out, n, "?+0x%llx", (unsigned long long)(uintptr_t)ra);
}

static int prj_exists(const char *path, int *isDir)
{
	struct stat st;

	if (isDir)
		*isDir = 0;
	if (path == NULL || stat(path, &st) != 0)
		return 0;
	if (isDir && S_ISDIR(st.st_mode))
		*isDir = 1;
	return 1;
}

/* NSError -> "domain=.. code=.. keys=.." , without touching the error if it is nil. */
static void prj_err(char *out, size_t n, NSError *err)
{
	int busy = prj_busy;

	if (busy) {
		snprintf(out, n, "err=busy");
		return;
	}
	prj_busy = 1;
	if (err == nil) {
		snprintf(out, n, "err=none");
		prj_busy = 0;
		return;
	}
	snprintf(out, n, "err.domain=%s err.code=%ld err.keys=%s", [[err domain] UTF8String],
		 (long)[err code], [[[[err userInfo] allKeys] componentsJoinedByString:@","] UTF8String]);
	prj_busy = 0;
}

static const char *prj_path(NSString *p)
{
	const char *c;

	if (p == nil)
		return "(nil)";
	c = [p fileSystemRepresentation];
	return c ? c : "(null-fsrep)";
}

static const char *prj_urlstr(NSURL *u)
{
	if (u == nil)
		return "(nil)";
	return [[u absoluteString] UTF8String] ?: "(nil)";
}

/* ---- the reachability member, whose answer the whole wall may hinge on ------------------------- */

typedef BOOL (*ReachIMP)(id, SEL, NSError **);
static ReachIMP orig_reach;
static ReachIMP orig_promised;

static BOOL prj_answer(id self, SEL cmd, ReachIMP orig, const char *what, NSError **error, void *ra)
{
	NSString *path = nil;
	const char *cpath = "(unreadable)";
	char site[512], errbuf[512];
	int isDir = 0, exists;
	BOOL origAnswer;
	int force = (getenv("PRJ_FORCE") != NULL && getenv("PRJ_FORCE")[0] == '1');

	if ([self isKindOfClass:[NSURL class]])
		path = [(NSURL *)self path];
	if (path != nil)
		cpath = prj_path(path);

	exists = prj_exists(cpath, &isDir);
	origAnswer = error == NULL ? orig(self, cmd, NULL) : orig(self, cmd, error);
	prj_err(errbuf, sizeof(errbuf), error == NULL ? nil : *error);
	prj_site(ra, site, sizeof(site));
	prj_log("%s path=%s exists=%d isDir=%d runtime-answer=%d %s site=%s force=%d",
		what, cpath, exists, isDir, (int)origAnswer, errbuf, site, force);

	if (!force)
		return origAnswer;

	/* The force arm: the CORRECT answer, so the caller's next step can be observed. */
	if (exists) {
		if (error)
			*error = nil;
		return YES;
	}
	if (error) {
		*error = [NSError errorWithDomain:NSCocoaErrorDomain
					     code:NSFileNoSuchFileError
					 userInfo:@{ NSFilePathErrorKey: path ?: @"",
							 NSURLErrorKey: self }];
	}
	return NO;
}

static BOOL hook_reach(id self, SEL cmd, NSError **error)
{
	return prj_answer(self, cmd, orig_reach, "NSURL checkResourceIsReachable", error,
			  __builtin_return_address(0));
}

static BOOL hook_promised(id self, SEL cmd, NSError **error)
{
	return prj_answer(self, cmd, orig_promised, "NSURL checkPromisedItemIsReachable", error,
			  __builtin_return_address(0));
}

/* ---- the file questions ---------------------------------------------------------------------- */

typedef BOOL (*BoolPathIMP)(id, SEL, NSString *);
typedef BOOL (*BoolPathBoolIMP)(id, SEL, NSString *, BOOL *);
typedef id (*ObjPathErrorIMP)(id, SEL, NSString *, NSError **);
typedef id (*ObjPathULongErrorIMP)(id, SEL, NSString *, NSUInteger, NSError **);
typedef id (*ObjPathErrorKeysIMP)(id, SEL, id *, NSString *, NSError **);

static BoolPathIMP orig_fe;
static BoolPathBoolIMP orig_feid;
static ObjPathErrorIMP orig_cod, orig_aoi, orig_dosl;
static ObjPathULongErrorIMP orig_data, orig_string;

static BOOL hook_fe(id self, SEL cmd, NSString *path)
{
	void *ra = __builtin_return_address(0);
	BOOL r = orig_fe(self, cmd, path);
	char site[512];

	prj_site(ra, site, sizeof(site));
	prj_log("NSFileManager fileExistsAtPath: path=%s -> %d site=%s", prj_path(path), (int)r, site);
	return r;
}

static BOOL hook_feid(id self, SEL cmd, NSString *path, BOOL *isDir)
{
	void *ra = __builtin_return_address(0);
	BOOL r = orig_feid(self, cmd, path, isDir);
	char site[512];

	prj_site(ra, site, sizeof(site));
	prj_log("NSFileManager fileExistsAtPath:isDirectory: path=%s -> %d isDir=%d site=%s",
		prj_path(path), (int)r, isDir ? (int)*isDir : -1, site);
	return r;
}

static id hook_cod(id self, SEL cmd, NSString *path, NSError **error)
{
	void *ra = __builtin_return_address(0);
	id r = orig_cod(self, cmd, path, error);
	char site[512], errbuf[512];

	prj_err(errbuf, sizeof(errbuf), error == NULL ? nil : *error);
	prj_site(ra, site, sizeof(site));
	prj_log("NSFileManager contentsOfDirectoryAtPath: path=%s -> %s %s site=%s", prj_path(path),
		r == nil ? "(nil)" : "array", errbuf, site);
	return r;
}

static id hook_aoi(id self, SEL cmd, NSString *path, NSError **error)
{
	void *ra = __builtin_return_address(0);
	id r = orig_aoi(self, cmd, path, error);
	char site[512], errbuf[512];

	prj_err(errbuf, sizeof(errbuf), error == NULL ? nil : *error);
	prj_site(ra, site, sizeof(site));
	prj_log("NSFileManager attributesOfItemAtPath: path=%s -> %s %s site=%s", prj_path(path),
		r == nil ? "(nil)" : "dict", errbuf, site);
	return r;
}

static id hook_dosl(id self, SEL cmd, NSString *path, NSError **error)
{
	void *ra = __builtin_return_address(0);
	id r = orig_dosl(self, cmd, path, error);
	char site[512], errbuf[512];

	prj_err(errbuf, sizeof(errbuf), error == NULL ? nil : *error);
	prj_site(ra, site, sizeof(site));
	prj_log("NSFileManager destinationOfSymbolicLinkAtPath: path=%s -> %s %s site=%s",
		prj_path(path), r == nil ? "(nil)" : "string", errbuf, site);
	return r;
}

static id hook_data(id self, SEL cmd, NSString *path, NSUInteger options, NSError **error)
{
	void *ra = __builtin_return_address(0);
	id r = orig_data(self, cmd, path, options, error);
	char site[512], errbuf[512];

	prj_err(errbuf, sizeof(errbuf), error == NULL ? nil : *error);
	prj_site(ra, site, sizeof(site));
	prj_log("NSData initWithContentsOfFile: path=%s opts=%lu -> %s %s site=%s", prj_path(path),
		(unsigned long)options, r == nil ? "(nil)" : "data", errbuf, site);
	return r;
}

static id hook_string(id self, SEL cmd, NSString *path, NSUInteger enc, NSError **error)
{
	void *ra = __builtin_return_address(0);
	id r = orig_string(self, cmd, path, enc, error);
	char site[512], errbuf[512];

	prj_err(errbuf, sizeof(errbuf), error == NULL ? nil : *error);
	prj_site(ra, site, sizeof(site));
	prj_log("NSString initWithContentsOfFile: path=%s enc=%lu -> %s %s site=%s", prj_path(path),
		(unsigned long)enc, r == nil ? "(nil)" : "string", errbuf, site);
	return r;
}

/* ---- the class members ----------------------------------------------------------------------- */

typedef id (*ClassPathIMP)(id, SEL, NSString *);
typedef id (*ClassURLIMP)(id, SEL, NSURL *);

static ClassPathIMP orig_dict_file, orig_bundle_path;
static ClassURLIMP orig_dict_url;

static id hook_dict_file(id self, SEL cmd, NSString *path)
{
	void *ra = __builtin_return_address(0);
	id r = orig_dict_file(self, cmd, path);
	char site[512];

	prj_site(ra, site, sizeof(site));
	prj_log("NSDictionary dictionaryWithContentsOfFile: path=%s -> %s site=%s", prj_path(path),
		r == nil ? "(nil)" : "dict", site);
	return r;
}

static id hook_dict_url(id self, SEL cmd, NSURL *url)
{
	void *ra = __builtin_return_address(0);
	id r = orig_dict_url(self, cmd, url);
	char site[512];

	prj_site(ra, site, sizeof(site));
	prj_log("NSDictionary dictionaryWithContentsOfURL: url=%s -> %s site=%s", prj_urlstr(url),
		r == nil ? "(nil)" : "dict", site);
	return r;
}

static id hook_bundle_path(id self, SEL cmd, NSString *path)
{
	void *ra = __builtin_return_address(0);
	id r = orig_bundle_path(self, cmd, path);
	char site[512];

	prj_site(ra, site, sizeof(site));
	prj_log("NSBundle initWithPath: path=%s -> %s site=%s", prj_path(path),
		r == nil ? "(nil)" : "bundle", site);
	return r;
}

/* ---- installation ---------------------------------------------------------------------------- */

static void prj_inst(Class cls, SEL sel, IMP repl, IMP *orig, const char *what)
{
	Method m;

	if (cls == Nil) {
		prj_log("NOT-FOUND class-for %s", what);
		return;
	}
	m = class_getInstanceMethod(cls, sel);
	if (m == NULL) {
		prj_log("NOT-FOUND %s", what);
		return;
	}
	*orig = method_getImplementation(m);
	method_setImplementation(m, repl);
	prj_log("INSTALLED %s", what);
}

static void prj_inst_class(Class cls, SEL sel, IMP repl, IMP *orig, const char *what)
{
	Method m;

	if (cls == Nil) {
		prj_log("NOT-FOUND class-for %s", what);
		return;
	}
	m = class_getClassMethod(cls, sel);
	if (m == NULL) {
		prj_log("NOT-FOUND %s", what);
		return;
	}
	*orig = method_getImplementation(m);
	method_setImplementation(m, repl);
	prj_log("INSTALLED %s", what);
}

__attribute__((constructor)) static void prj_init(void)
{
	Class nsurl = objc_getClass("NSURL");
	Class fm = objc_getClass("NSFileManager");
	Class data = objc_getClass("NSData");
	Class str = objc_getClass("NSString");
	Class dict = objc_getClass("NSDictionary");
	Class bundle = objc_getClass("NSBundle");

	prj_log("LOAD force=%s log=%s", getenv("PRJ_FORCE") ? getenv("PRJ_FORCE") : "",
		getenv("PRJ_LOG") ? getenv("PRJ_LOG") : "");
	prj_inst(nsurl, @selector(checkResourceIsReachableAndReturnError:), (IMP)hook_reach,
		 (IMP *)&orig_reach, "NSURL checkResourceIsReachableAndReturnError:");
	prj_inst(nsurl, @selector(checkPromisedItemIsReachableAndReturnError:), (IMP)hook_promised,
		 (IMP *)&orig_promised, "NSURL checkPromisedItemIsReachableAndReturnError:");
	prj_inst(fm, @selector(fileExistsAtPath:), (IMP)hook_fe, (IMP *)&orig_fe,
		 "NSFileManager fileExistsAtPath:");
	prj_inst(fm, @selector(fileExistsAtPath:isDirectory:), (IMP)hook_feid, (IMP *)&orig_feid,
		 "NSFileManager fileExistsAtPath:isDirectory:");
	prj_inst(fm, @selector(contentsOfDirectoryAtPath:error:), (IMP)hook_cod, (IMP *)&orig_cod,
		 "NSFileManager contentsOfDirectoryAtPath:error:");
	prj_inst(fm, @selector(attributesOfItemAtPath:error:), (IMP)hook_aoi, (IMP *)&orig_aoi,
		 "NSFileManager attributesOfItemAtPath:error:");
	prj_inst(fm, @selector(destinationOfSymbolicLinkAtPath:error:), (IMP)hook_dosl, (IMP *)&orig_dosl,
		 "NSFileManager destinationOfSymbolicLinkAtPath:error:");
	prj_inst(data, @selector(initWithContentsOfFile:options:error:), (IMP)hook_data, (IMP *)&orig_data,
		 "NSData initWithContentsOfFile:options:error:");
	prj_inst(str, @selector(initWithContentsOfFile:encoding:error:), (IMP)hook_string,
		 (IMP *)&orig_string, "NSString initWithContentsOfFile:encoding:error:");
	prj_inst_class(dict, @selector(dictionaryWithContentsOfFile:), (IMP)hook_dict_file,
		       (IMP *)&orig_dict_file, "NSDictionary dictionaryWithContentsOfFile:");
	prj_inst_class(dict, @selector(dictionaryWithContentsOfURL:), (IMP)hook_dict_url,
		       (IMP *)&orig_dict_url, "NSDictionary dictionaryWithContentsOfURL:");
	prj_inst(bundle, @selector(initWithPath:), (IMP)hook_bundle_path, (IMP *)&orig_bundle_path,
		 "NSBundle initWithPath:");
}

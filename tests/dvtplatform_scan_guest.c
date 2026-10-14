/*
 * dvtplatform_scan_guest.c -- permanent guest oracle for the step of
 * +[DVTPlatform loadAllPlatformsReturningError:] that finds (or fails to find) Xcode's own
 * macOS platform on the DEPLOYED Darling runtime.
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder (scripts/xcodebuild-workload-run.sh, both
 * workloads) dies inside xcodebuild's initialization, before any build task runs:
 *
 *   domain DVTXcodebuildGeneralErrorDomain code 70
 *   NSLocalizedDescription = "Initialization failed.\n\tReason: No macOS Platform was found, but
 *   Xcode requires a macOS Platform. You may have an incomplete or damaged installation of Xcode."
 *
 * read out of the run's own post-mortem cores (evidence/original-xcode-error-recovered/). The
 * error is IDEFoundation's IDEInitialize() failure and its reason text is produced by
 * DVTFoundation's +[DVTPlatform loadAllPlatformsReturningError:] -- the CFSTR
 * "No macOS Platform was found, ..." lives in that function's cold error path. That function
 * computes its scan root as
 *
 *   [[DVTDeveloperPaths defaultPaths] developerPlatformsDirectory]     =  <developer folder>/Platforms
 *   [DVTPlatform extraPlatformFolders]                                (+ env XCODE_EXTRA_PLATFORM_FOLDERS)
 *
 * and then, per folder: -[NSFileManager fileExistsAtPath:isDirectory:] -> -[... contentsOfDirectoryAtPath:error:]
 * -> names ending in ".platform" -> -[DVTFilePath filePathForRelativePathString:] ->
 * +[DVTPlatform _loadPlatformAtPath:] -> +[DVTPlatform _propertyDictionaryForPlatformAtPath:] (which reads
 * Info.plist as an NSDictionary, merges the bundle's localizedInfoDictionary, and requires
 * `Type` == "Platform"). The macOS platform's own Info.plist is a BINARY property list (bplist00),
 * unlike Darling's synthetic XML one under darling/Developer/.
 *
 * WHAT THIS MEASURES, in one guest process on the deployed runtime, with no compile-time reference
 * to Foundation or ObjC (dlopen + dlsym + objc_msgSend + sel_registerName only):
 *
 *   1. the scan root the tool itself computes (DEVELOPER_DIR and XCODE_DEVELOPER_DIR_PATH as seen,
 *      the DVTDeveloperPaths class actually in use, and its developerDirectory / platforms directory);
 *   2. every step of the scan with its own error, in the tool's own order, on the tool's own objects;
 *   3. the real +[DVTPlatform loadAllPlatformsReturningError:] result, its NSError, allPlatforms and
 *      platformForIdentifier:@"com.apple.platform.macosx" -- the exact predicate the caller gates on.
 *
 * Every intermediate fact is printed unconditionally as a DVTP-* line, so a RED arm quotes the
 * failure from the run's own output; DVTP_VERDICT=GREEN is the machine-readable line the harness
 * ok-marker reads. GREEN requires the tool's own predicate to hold (a macOS platform resolves and
 * loadAllPlatformsReturningError: reports success), not a lookalike condition computed here.
 *
 * argv[1]: path to the DVTFoundation binary to load (required; the driver passes the Xcode one).
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the oracle is run against the prefix as
 * deployed. See tests/dvtplatform-scan-guest.sh.
 */

#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
#define CF_LIBRARY "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);
static const char *(*getClassNameFn)(void *);

/* Untyped sends for id-returning selectors. */
static void *send0(void *o, const char *sel) { return msgSend(o, selRegister(sel)); }
static void *send1(void *o, const char *sel, void *a) { return msgSend(o, selRegister(sel), a); }

/* Typed sends: NSInteger/BOOL returns do not survive a void* prototype. */
static long send_int0(void *o, const char *sel)
{
	union { void *(*p)(void *, void *, ...); long (*f)(void *, void *); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel));
}
static unsigned char send_bool0(void *o, const char *sel)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel));
}
/* -[NSFileManager fileExistsAtPath:isDirectory:] */
static unsigned char send_bool_path_isdir(void *o, const char *sel, void *path, char *isDir)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *, void *, void *); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel), path, isDir);
}
/* any 2-pointer-arg selector returning id (contentsOfDirectoryAtPath:error: etc.) */
static void *send_path_err(void *o, const char *sel, void *path, void **errOut)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, void *, void **); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel), path, errOut);
}
/* a class method whose single argument is a pointer-to-pointer (loadAllPlatformsReturningError:) */
static unsigned char send_class_bool_ptr1(void *cls, const char *sel, void **arg)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *, void **); } u;
	u.p = msgSend;
	return u.f(cls, selRegister(sel), arg);
}

static const char *cstr_of(void *nsstring)
{
	if (!nsstring)
		return "(nil)";
	const char *s = (const char *)send0(nsstring, "UTF8String");
	return s ? s : "(no-utf8)";
}

static void *clsNSStringG;

/* A DVTFilePath (or NSString) printed as text: -pathString only exists on the former. */
static const char *cstr_of_pathlike(void *o)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *, void *); } u;
	u.p = msgSend;
	if (o && u.f(o, selRegister("respondsToSelector:"), selRegister("pathString")))
		o = send0(o, "pathString");
	return cstr_of(o);
}

/* +[NSString stringWithUTF8String:] */
static void *nsstr(const char *s)
{
	return send1(clsNSStringG, "stringWithUTF8String:", (void *)s);
}

/* Print an NSError the way the caller sees it. */
static void print_error(const char *prefix, void *err)
{
	if (!err) {
		printf("%s domain=(none) code=0 desc=(none)\n", prefix);
		return;
	}
	const char *domain = cstr_of(send0(err, "domain"));
	long code = send_int0(err, "code");
	const char *desc = cstr_of(send0(err, "localizedDescription"));
	printf("%s domain=%s code=%ld desc=%s\n", prefix, domain, code, desc);
}

/* The scan step this rung is about: a dictionary read from a platform's Info.plist and the two
 * keys DVTPlatform's own predicate and the identifier index are built from. */
static void report_dict(const char *prefix, const char *path, void *dict)
{
	if (!dict) {
		printf("%s path=%s dict=(nil)\n", prefix, path);
		return;
	}
	void *type = send1(dict, "objectForKey:", nsstr("Type"));
	void *ident = send1(dict, "objectForKey:", nsstr("CFBundleIdentifier"));
	void *name = send1(dict, "objectForKey:", nsstr("CFBundleName"));
	long n = send_int0(dict, "count");
	printf("%s path=%s dict=1 keys=%ld Type=%s CFBundleIdentifier=%s CFBundleName=%s\n",
	       prefix, path, n, cstr_of(type), cstr_of(ident), cstr_of(name));
}


/* ---------------------------------------------------------------------------------------------
 * The layers under -[NSFileManager contentsOfDirectoryAtPath:error:]: the BSD libc directory API
 * (opendir/readdir/readdir_r) and, under it, Darling's getdirentries emulation, whose buffer is
 * `struct dirent` written by the emulation and read by libc. A directory that exists, is readable,
 * and lists nothing is one of these two returning "no entries"; each is asked separately so a fix
 * can be aimed at the layer that is actually wrong.
 * ------------------------------------------------------------------------------------------- */
static void layer_probe(const char *dir)
{
	printf("DVTP-LAYER dir=%s\n", dir);
	errno = 0;
	DIR *d = opendir(dir);
	int open_errno = errno;
	printf("DVTP-LAYER opendir=%s errno=%d\n", d ? "ok" : "NULL", open_errno);
	if (d) {
		struct dirent de;
		struct dirent *res = NULL;
		int n = 0, rr = 0;
		errno = 0;
		while ((rr = readdir_r(d, &de, &res)) == 0 && res) {
			if (n < 4)
				printf("DVTP-LAYER readdir_r[%d] name=%s type=%u ino=%llu reclen=%u namlen=%u\n",
				       n, de.d_name, (unsigned)de.d_type, (unsigned long long)de.d_ino,
				       (unsigned)de.d_reclen, (unsigned)de.d_namlen);
			n++;
			errno = 0;
		}
		printf("DVTP-LAYER readdir_r entries=%d final_rc=%d errno=%d\n", n, rr, errno);

		rewinddir(d);
		struct dirent *p = NULL;
		n = 0;
		errno = 0;
		while ((p = readdir(d)) != NULL) {
			if (n < 4)
				printf("DVTP-LAYER readdir[%d] name=%s type=%u reclen=%u namlen=%u\n", n,
				       p->d_name, (unsigned)p->d_type, (unsigned)p->d_reclen, (unsigned)p->d_namlen);
			n++;
		}
		printf("DVTP-LAYER readdir entries=%d errno=%d\n", n, errno);
		closedir(d);
	} else {
		printf("DVTP-LAYER readdir skipped (no DIR)\n");
	}

	/* The emulation below libc: getdirentries(64) fills a Darwin-layout buffer. Decoded here with
	 * the SDK's own struct dirent, so a layout mismatch and an empty result look different. */
	static long (*getdirentries64_fn)(int, char *, unsigned, long *);
	if (!getdirentries64_fn)
		getdirentries64_fn = (long (*)(int, char *, unsigned, long *))dlsym(RTLD_DEFAULT, "getdirentries64");
	/* The emulation itself: __getdirentries64 is the libsystem_kernel entry libc's readdir uses.
	 * Its raw return is the fact that separates "the listing is empty" from "libc could not parse
	 * what the listing returned". Both the 2048-byte size opendir primes with and a large one are
	 * asked, because the emulation sizes its own Linux-side buffer from the caller's size. */
	static long (*gd64_fn)(int, char *, unsigned, long *);
	if (!gd64_fn)
		gd64_fn = (long (*)(int, char *, unsigned, long *))dlsym(RTLD_DEFAULT, "__getdirentries64");
	if (!getdirentries64_fn)
		getdirentries64_fn = (long (*)(int, char *, unsigned, long *))dlsym(RTLD_DEFAULT, "getdirentries64");
	{
		int fd = open(dir, O_RDONLY);
		printf("DVTP-LAYER open=%s errno=%d\n", fd >= 0 ? "ok" : "NULL", errno);
		if (fd >= 0) {
			printf("DVTP-LAYER __getdirentries64 symbol=%s\n", gd64_fn ? "present" : "(absent)");
			unsigned sizes[2] = { 2048, 8192 };
			for (int si = 0; gd64_fn && si < 2; si++) {
				static char buf[8192];
				memset(buf, 0, sizeof buf);
				long off = 0;
				errno = 0;
				long r = gd64_fn(fd, buf, sizes[si], &off);
				printf("DVTP-LAYER __getdirentries64 size=%u r=%ld seekoff=%ld errno=%d\n",
				       sizes[si], r, off, errno);
				if (r > 0) {
					long o = 0;
					int n = 0;
					while (o < r && n < 4) {
						struct dirent *e = (struct dirent *)(buf + o);
						printf("DVTP-LAYER __getdirentries64[%d] name=%s type=%u ino=%llu reclen=%u namlen=%u\n",
						       n, e->d_name, (unsigned)e->d_type, (unsigned long long)e->d_ino,
						       (unsigned)e->d_reclen, (unsigned)e->d_namlen);
						if (e->d_reclen == 0)
							break;
						o += e->d_reclen;
						n++;
					}
				}
				if (lseek(fd, 0, SEEK_SET) < 0)
					break;
			}
			close(fd);
		}
	}
}

int main(int argc, char **argv)
{
	static int failures;
#define FAIL(stage, detail) do { printf("DVTP-FAIL stage=%s %s\n", (stage), (detail)); failures++; } while (0)

	if (argc < 2) {
		FAIL("argv", "detail=usage: dvtplatform_scan_guest <path-to-DVTFoundation>");
		printf("DVTP_VERDICT=RED\n");
		return 1;
	}
	const char *dvtPath = argv[1];

	printf("DVTP-ENV DEVELOPER_DIR=%s\n", getenv("DEVELOPER_DIR") ? getenv("DEVELOPER_DIR") : "(unset)");
	printf("DVTP-ENV XCODE_DEVELOPER_DIR_PATH=%s\n",
	       getenv("XCODE_DEVELOPER_DIR_PATH") ? getenv("XCODE_DEVELOPER_DIR_PATH") : "(unset)");
	printf("DVTP-ARG dvt=%s\n", dvtPath);

	void *objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	void *foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	void *cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation || !cf) {
		char detail[512];
		snprintf(detail, sizeof detail, "objc=%p foundation=%p cf=%p err=%s", objc, foundation, cf, dlerror());
		FAIL("dlopen-base", detail);
		printf("DVTP_VERDICT=RED\n");
		return 1;
	}
	msgSend = (void *(*)(void *, void *, ...))dlsym(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(objc, "objc_getClass");
	getClassNameFn = (const char *(*)(void *))dlsym(objc, "object_getClassName");
	if (!msgSend || !selRegister || !getClassFn) {
		FAIL("dlsym-objc", "detail=objc_msgSend/sel_registerName/objc_getClass missing");
		printf("DVTP_VERDICT=RED\n");
		return 1;
	}

	/* DVTFoundation is loaded from the exact path the caller names, so the developer-folder
	 * deduction that reads the framework's OWN bundle path is exercised, not simulated. */
	void *dvt = dlopen(dvtPath, RTLD_NOW | RTLD_GLOBAL);
	if (!dvt) {
		char detail[1024];
		snprintf(detail, sizeof detail, "path=%s err=%s", dvtPath, dlerror());
		FAIL("dlopen-dvt", detail);
		printf("DVTP_VERDICT=RED\n");
		return 1;
	}
	printf("DVTP-DLOPEN dvt=%p\n", dvt);

	void *clsDVTPlatform = getClassFn("DVTPlatform");
	void *clsDVTDeveloperPaths = getClassFn("DVTDeveloperPaths");
	void *clsDVTFilePath = getClassFn("DVTFilePath");
	void *clsNSFileManager = getClassFn("NSFileManager");
	void *clsNSDictionary = getClassFn("NSDictionary");
	void *clsNSString = getClassFn("NSString");
	if (!clsDVTPlatform || !clsDVTDeveloperPaths || !clsDVTFilePath || !clsNSFileManager) {
		char detail[256];
		snprintf(detail, sizeof detail, "DVTPlatform=%p DVTDeveloperPaths=%p DVTFilePath=%p NSFileManager=%p",
			 clsDVTPlatform, clsDVTDeveloperPaths, clsDVTFilePath, clsNSFileManager);
		FAIL("classes", detail);
		printf("DVTP_VERDICT=RED\n");
		return 1;
	}
	printf("DVTP-CLASS DVTPlatform=%p DVTDeveloperPaths=%p DVTFilePath=%p\n",
	       clsDVTPlatform, clsDVTDeveloperPaths, clsDVTFilePath);
	clsNSStringG = clsNSString;

	/* ---- 1. the scan root the tool itself computes ------------------------------------------ */
	void *paths = send0(clsDVTDeveloperPaths, "defaultPaths");
	printf("DVTP-PATHS class=%s\n", paths ? getClassNameFn(paths) : "(nil)");
	void *devDir = paths ? send0(paths, "developerDirectory") : NULL;
	printf("DVTP-PATHS developerDirectory class=%s path=%s\n",
	       devDir ? getClassNameFn(devDir) : "(nil)", cstr_of_pathlike(devDir));
	void *devDirPath = send0(clsDVTDeveloperPaths, "developerDirectoryPath");
	printf("DVTP-PATHS developerDirectoryPath class=%s value=%s\n",
	       devDirPath ? getClassNameFn(devDirPath) : "(nil)", cstr_of_pathlike(devDirPath));
	void *platformsDir = paths ? send0(paths, "developerPlatformsDirectory") : NULL;
	const char *platformsDirStr = platformsDir ? cstr_of_pathlike(platformsDir) : "(nil)";
	printf("DVTP-PATHS developerPlatformsDirectory class=%s path=%s\n",
	       platformsDir ? getClassNameFn(platformsDir) : "(nil)", platformsDirStr);

	void *extra = send0(clsDVTPlatform, "extraPlatformFolders");
	printf("DVTP-PATHS onlySearchExtraPlatformFolders=%d\n", send_bool0(clsDVTPlatform, "onlySearchExtraPlatformFolders"));
	if (extra) {
		long n = send_int0(extra, "count");
		printf("DVTP-PATHS extraPlatformFolders count=%ld\n", n);
		for (long i = 0; i < n && i < 16; i++) {
			union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, long); } u;
			u.p = msgSend;
			void *item = u.f(extra, selRegister("objectAtIndex:"), i);
			printf("DVTP-PATHS extraPlatformFolders[%ld] class=%s path=%s\n", i, getClassNameFn(item),
			       cstr_of_pathlike(item));
		}
	} else {
		printf("DVTP-PATHS extraPlatformFolders=(nil)\n");
	}

	/* ---- 2a. the layers under the enumeration, so a fix can be aimed ---------------------------- */
	layer_probe(platformsDirStr);
	{
		char parent[4096];
		snprintf(parent, sizeof parent, "%s", platformsDirStr);
		char *slash = strrchr(parent, '/');
		if (slash)
			*slash = 0;
		layer_probe(parent);
		layer_probe("/private/var/tmp");
	}

	/* ---- 2b. the scan steps, in the tool's own order, on the tool's own objects ---------------- */
	void *fm = send0(clsNSFileManager, "defaultManager");
	char isDir = 0;
	unsigned char exists = send_bool_path_isdir(fm, "fileExistsAtPath:isDirectory:",
						    platformsDir ? send0(platformsDir, "pathString") : NULL, &isDir);
	printf("DVTP-SCAN1 fileExistsAtPath:isDirectory: exists=%d isDirectory=%d path=%s\n",
	       exists, isDir, platformsDirStr);

	void *contentsErr = NULL;
	void *contents = platformsDir
		? send_path_err(fm, "contentsOfDirectoryAtPath:error:", send0(platformsDir, "pathString"), &contentsErr)
		: NULL;
	print_error("DVTP-SCAN2 contentsOfDirectoryAtPath:error:", contentsErr);
	long nContents = contents ? send_int0(contents, "count") : -1;
	printf("DVTP-SCAN2 contents count=%ld\n", nContents);
	if (contents) {
		for (long i = 0; i < nContents && i < 32; i++) {
			union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, long); } u;
			u.p = msgSend;
			void *name = u.f(contents, selRegister("objectAtIndex:"), i);
			printf("DVTP-SCAN2 item[%ld]=%s\n", i, cstr_of(name));
		}
	}

	/* The two predicates the scan applies to a name, then the platform's own objects. */
	if (contents) {
		for (long i = 0; i < nContents && i < 32; i++) {
			union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, long); } u;
			u.p = msgSend;
			void *name = u.f(contents, selRegister("objectAtIndex:"), i);
			void *suffix = nsstr(".platform");
			union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *, void *); } hu;
			hu.p = msgSend;
			unsigned char hs = hu.f(name, selRegister("hasSuffix:"), suffix);
			if (!hs)
				continue;

			void *platPath = platformsDir ? send1(platformsDir, "filePathForRelativePathString:", name) : NULL;
			printf("DVTP-SCAN3 platform name=%s filePathForRelativePathString=%s\n", cstr_of(name),
			       platPath ? cstr_of(send0(platPath, "pathString")) : "(nil)");
			if (!platPath)
				continue;

			/* The tool's own reader (Info.plist + bundle localizedInfoDictionary + Type check). */
			void *propDict = send1(clsDVTPlatform, "_propertyDictionaryForPlatformAtPath:", platPath);
			report_dict("DVTP-SCAN4 propertyDictionary", cstr_of_pathlike(platPath), propDict);

			/* The same file read straight out of Foundation, to attribute a nil above. */
			const char *platStr = cstr_of_pathlike(platPath);
			char plistPath[4096];
			snprintf(plistPath, sizeof plistPath, "%s/Info.plist", platStr);
			void *plistStr = nsstr(plistPath);
			void *dictFromPath = send1(clsNSDictionary, "dictionaryWithContentsOfFile:", plistStr);
			report_dict("DVTP-SCAN5 dictionaryWithContentsOfFile", plistPath, dictFromPath);
			void *dictFile = send1(send0(clsNSDictionary, "alloc"), "initWithContentsOfFile:", plistStr);
			report_dict("DVTP-SCAN6 initWithContentsOfFile", plistPath, dictFile);

			/* The size and first bytes of the plist, so a parse failure can be told from an
			 * absent file without trusting any Darling API. */
			FILE *fp = fopen(plistPath, "rb");
			if (!fp) {
				printf("DVTP-SCAN7 file=nil path=%s errno-report=open-failed\n", plistPath);
			} else {
				unsigned char head[8] = { 0 };
				size_t got = fread(head, 1, sizeof head, fp);
				long size = 0;
				if (fseek(fp, 0, SEEK_END) == 0)
					size = ftell(fp);
				fclose(fp);
				printf("DVTP-SCAN7 file=1 path=%s size=%ld head=%02x%02x%02x%02x%02x%02x%02x%02x (read=%zu)\n",
				       plistPath, size, head[0], head[1], head[2], head[3], head[4], head[5], head[6], head[7], got);
			}
			/* Do the same read through NSData + NSPropertyListSerialization. */
			void *data = send1(send0(getClassFn("NSData"), "alloc"), "initWithContentsOfFile:", plistStr);
			printf("DVTP-SCAN8 NSData initWithContentsOfFile=%s length=%ld\n", data ? "1" : "(nil)",
			       data ? send_int0(data, "length") : -1L);
		}
	}

	/* ---- 3. the tool's own predicate: the exact call the ladder fails in ---------------------- */
	void *err = NULL;
	unsigned char loadOK = send_class_bool_ptr1(clsDVTPlatform, "loadAllPlatformsReturningError:", &err);
	printf("DVTP-LOADALL rc=%d\n", loadOK);
	print_error("DVTP-LOADALL", err);

	void *macosx = send1(clsDVTPlatform, "platformForIdentifier:", nsstr("com.apple.platform.macosx"));
	printf("DVTP-MACOSX platformForIdentifier=com.apple.platform.macosx -> class=%s identifier=%s\n",
	       macosx ? getClassNameFn(macosx) : "(nil)",
	       macosx ? cstr_of(send0(macosx, "identifier")) : "(nil)");

	/* The verdict is the tool's own predicate: a macOS platform exists AND the load reports success
	 * (or, for the caller's tolerant shape, at worst an error the caller is written to accept). */
	int green = macosx != NULL;
	printf("DVTP-PREDICATE macosx_platform=%d loadAllPlatforms_rc=%d hard_failures=%d\n",
	       macosx ? 1 : 0, loadOK, failures);
	printf("DVTP_VERDICT=%s\n", green ? "GREEN" : "RED");
	fflush(stdout);

	/* Reported AFTER the verdict: +[DVTPlatform allPlatforms] and +defaultPlatform take DVTAssert
	 * on the not-yet-loaded state and would abort the process before the verdict is printed. */
	void *all = send0(clsDVTPlatform, "allPlatforms");
	printf("DVTP-ALLPLATFORMS class=%s\n", all ? getClassNameFn(all) : "(nil)");
	if (all) {
		long n = send_int0(all, "count");
		printf("DVTP-ALLPLATFORMS count=%ld\n", n);
		for (long i = 0; i < n && i < 32; i++) {
			union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, long); } u;
			u.p = msgSend;
			void *p = u.f(all, selRegister("objectAtIndex:"), i);
			void *ident = send0(p, "identifier");
			void *name = send0(p, "name");
			printf("DVTP-ALLPLATFORMS[%ld] class=%s identifier=%s name=%s\n", i, getClassNameFn(p),
			       ident ? cstr_of(ident) : "(nil)", name ? cstr_of(name) : "(nil)");
		}
	}

	void *def = send0(clsDVTPlatform, "defaultPlatform");
	printf("DVTP-DEFAULT class=%s identifier=%s\n", def ? getClassNameFn(def) : "(nil)",
	       def ? cstr_of(send0(def, "identifier")) : "(nil)");

	return green ? 0 : 1;
}

/*
 * xcodebuild_project_read_guest.c -- guest oracle for the step of `xcodebuild -project <path>` that
 * reads the project, measured on the DEPLOYED Darling runtime.
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder (scripts/xcodebuild-workload-run.sh, both
 * workloads) gets past plug-in loading with the documented DVT_PLUG_INS_TO_IGNORE set and then stops
 * with the tool's own line:
 *
 *   xcodebuild: error: Unable to read project 'iosapp.xcodeproj'.
 *       Reason: The operation couldn't be completed. (NSCocoaErrorDomain error 4.)
 *
 * `NSFileNoSuchFileError` is a file-MANAGER/URL error, not a read error (a failed read is
 * NSFileReadNoSuchFileError 260), and the bare description says the NSError carries no
 * NSLocalizedDescription -- so the operation that failed is an existence/enumeration step and the
 * question is WHERE the file it asked for was, not whether its bytes parse. Three candidate causes
 * must be separated by measurement, because they need opposite fixes: (a) we opened the WRONG path
 * (relative-path or dirfd resolution), (b) the file is NOT THERE (or not visible at that layer), or
 * (c) the read/parse failed above libc.
 *
 * The ladder has paid for this class once already: an out-of-union directory enumerated as EMPTY
 * with no error (evidence/dvtplatform-platform-scan-rung/). So the layers are asked SEPARATELY, on
 * both the relative and the absolute form of the same path, and the fixture's own machine-readable
 * verdict is the predicate the ladder needs: the project file is visible, readable, enumerable and
 * its bytes reach a Foundation NSString at every layer.
 *
 * WHAT IT MEASURES, in one guest process on the deployed runtime, with no compile-time reference to
 * Foundation or ObjC (dlopen + dlsym + objc_msgSend + sel_registerName only):
 *
 *   1. the process's own cwd, and the two path forms Xcode can hand to the reader;
 *   2. the libc layer, per form: stat / lstat / access / open+read / realpath;
 *   3. the directory layer of the .xcodeproj itself: opendir/readdir(_r) and the
 *      __getdirentries64 emulation below them (a listing that is empty with errno 0 is a layer
 *      defect, not a missing file);
 *   4. the dirfd layer: openat(dirfd, "project.pbxproj") and openat(AT_FDCWD, <abs file>);
 *   5. the Foundation layer, on the tool's own objects: -[NSFileManager fileExistsAtPath:],
 *      ...isDirectory:, contentsOfDirectoryAtPath:error:, attributesOfItemAtPath:error:,
 *      -[NSData initWithContentsOfFile:options:error:], -[NSString
 *      stringWithContentsOfFile:encoding:error:], -[NSString stringByStandardizingPath],
 *      stringByResolvingSymlinksInPath, -[NSURL checkResourceIsReachableAndReturnError:],
 *      resourceValuesForKeys:error:;
 *   6. every NSError's FULL userInfo (NSFilePathErrorKey is what names the path that was not
 *      found -- the tool's own message drops it, which is why the wall reads as bare "error 4");
 *   7. DVTFoundation's own file object (+[DVTFilePath filePathForPathString:], -pathString,
 *      -fileExists) so the same question is asked of the framework the reader uses.
 *
 * argv[1]: path to the DVTFoundation binary to load (required).
 * argv[2]: the project directory, in the guest's namespace (required), e.g. /private/var/tmp/xcodebuild-iosapp/iosapp.xcodeproj
 *
 * Every intermediate fact is printed unconditionally as an XPR-* line. XPR_VERDICT=GREEN is the
 * machine-readable line the harness ok-marker reads; GREEN requires every layer above to succeed on
 * BOTH path forms and the file's bytes to reach Foundation.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the oracle is run against the prefix as
 * deployed. See tests/xcodebuild-project-read-guest.sh.
 */

#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
#define CF_LIBRARY "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);
static void *clsNSStringG;

static void *send0(void *o, const char *sel) { return msgSend(o, selRegister(sel)); }
static void *send1(void *o, const char *sel, void *a) { return msgSend(o, selRegister(sel), a); }

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
/* -[NSFileManager fileExistsAtPath:] and the other one-argument BOOL selectors */
static unsigned char send_bool1(void *o, const char *sel, void *a)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *, void *); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel), a);
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
/* -[NSData initWithContentsOfFile:options:error:] */
static void *send_path_ulong_err(void *o, const char *sel, void *path, unsigned long opt, void **errOut)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, void *, unsigned long, void **); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel), path, opt, errOut);
}
/* -[NSString stringWithContentsOfFile:encoding:error:] (encoding is an unsigned long) */
static void *send_path_ulong_err2(void *o, const char *sel, void *path, unsigned long enc, void **errOut)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, void *, unsigned long, void **); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel), path, enc, errOut);
}
/* -[NSURL resourceValuesForKeys:error:] */
static void *send_id_err(void *o, const char *sel, void *keys, void **errOut)
{
	union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, void *, void **); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel), keys, errOut);
}
/* -[NSURL checkResourceIsReachableAndReturnError:] */
static unsigned char send_bool_err(void *o, const char *sel, void **errOut)
{
	union { void *(*p)(void *, void *, ...); unsigned char (*f)(void *, void *, void **); } u;
	u.p = msgSend;
	return u.f(o, selRegister(sel), errOut);
}

static const char *cstr_of(void *nsstring)
{
	if (!nsstring)
		return "(nil)";
	const char *s = (const char *)send0(nsstring, "UTF8String");
	return s ? s : "(no-utf8)";
}

static void *nsstr(const char *s) { return send1(clsNSStringG, "stringWithUTF8String:", (void *)s); }

/* Print an NSError the way the caller sees it, PLUS its whole userInfo: the tool's own message
 * prints only localizedDescription, and for a bare NSError that is the generic "The operation
 * couldn't be completed." -- the path that was not found lives in NSFilePathErrorKey, so dropping
 * userInfo is what makes this wall unreadable. */
static void print_error_full(const char *prefix, void *err)
{
	if (!err) {
		printf("%s domain=(none) code=0 desc=(none) userInfo=(none)\n", prefix);
		return;
	}
	const char *domain = cstr_of(send0(err, "domain"));
	long code = send_int0(err, "code");
	const char *desc = cstr_of(send0(err, "localizedDescription"));
	void *ui = send0(err, "userInfo");
	char uibuf[1024];
	if (ui) {
		const char *s = cstr_of(send0(ui, "description"));
		snprintf(uibuf, sizeof uibuf, "%s", s ? s : "(no-desc)");
	} else {
		snprintf(uibuf, sizeof uibuf, "(none)");
	}
	printf("%s domain=%s code=%ld desc=%s userInfo=%s\n", prefix, domain, code, desc, uibuf);
}

/* --------------------------------------------------------------------------------------------
 * Layer 1: libc, on ONE path form. `label` names the form (rel/abs) so a fix can be aimed.
 * ------------------------------------------------------------------------------------------ */
static void libc_layers(const char *label, const char *path)
{
	struct stat st;
	int rc;

	errno = 0;
	rc = stat(path, &st);
	printf("XPR-LIBC %s stat rc=%d errno=%d mode=%o size=%lld path=%s\n", label, rc, errno,
	       rc == 0 ? (unsigned)st.st_mode : 0u, rc == 0 ? (long long)st.st_size : -1LL, path);

	errno = 0;
	rc = lstat(path, &st);
	printf("XPR-LIBC %s lstat rc=%d errno=%d mode=%o\n", label, rc, errno, rc == 0 ? (unsigned)st.st_mode : 0u);

	errno = 0;
	rc = access(path, R_OK);
	printf("XPR-LIBC %s access-R_OK rc=%d errno=%d\n", label, rc, errno);

	errno = 0;
	int fd = open(path, O_RDONLY);
	int open_errno = errno;
	printf("XPR-LIBC %s open-O_RDONLY fd=%d errno=%d\n", label, fd, open_errno);
	if (fd >= 0) {
		char buf[64];
		errno = 0;
		ssize_t n = read(fd, buf, sizeof buf);
		printf("XPR-LIBC %s read n=%zd errno=%d head=%.*s\n", label, n, errno,
		       n > 0 ? (int)(n < 20 ? n : 20) : 0, buf);
		close(fd);
	}

	errno = 0;
	char rp[PATH_MAX];
	char *r = realpath(path, rp);
	printf("XPR-LIBC %s realpath=%s errno=%d\n", label, r ? r : "(nil)", errno);
}

/* The layers under -[NSFileManager contentsOfDirectoryAtPath:error:]: BSD libc's directory API and,
 * below it, Darling's getdirentries emulation. A directory that exists and lists NOTHING with errno
 * 0 is a layer defect, not a missing directory -- each layer is asked separately. */
static void dir_layers(const char *label, const char *dir)
{
	printf("XPR-DIR %s dir=%s\n", label, dir);
	errno = 0;
	DIR *d = opendir(dir);
	int open_errno = errno;
	printf("XPR-DIR %s opendir=%s errno=%d\n", label, d ? "ok" : "NULL", open_errno);
	if (d) {
		struct dirent de, *res = NULL;
		int n = 0, rr = 0;
		errno = 0;
		while ((rr = readdir_r(d, &de, &res)) == 0 && res) {
			if (n < 6)
				printf("XPR-DIR %s readdir_r[%d] name=%s type=%u ino=%llu reclen=%u namlen=%u\n",
				       label, n, de.d_name, (unsigned)de.d_type, (unsigned long long)de.d_ino,
				       (unsigned)de.d_reclen, (unsigned)de.d_namlen);
			n++;
			errno = 0;
		}
		printf("XPR-DIR %s readdir_r entries=%d final_rc=%d errno=%d\n", label, n, rr, errno);
		rewinddir(d);
		struct dirent *p = NULL;
		n = 0;
		errno = 0;
		while ((p = readdir(d)) != NULL)
			n++;
		printf("XPR-DIR %s readdir entries=%d errno=%d\n", label, n, errno);
		closedir(d);
	}

	static long (*gd64_fn)(int, char *, unsigned, long *);
	if (!gd64_fn)
		gd64_fn = (long (*)(int, char *, unsigned, long *))dlsym(RTLD_DEFAULT, "__getdirentries64");
	printf("XPR-DIR %s __getdirentries64 symbol=%s\n", label, gd64_fn ? "present" : "(absent)");
	errno = 0;
	int dfd = open(dir, O_RDONLY);
	printf("XPR-DIR %s open fd=%d errno=%d\n", label, dfd, errno);
	if (dfd >= 0) {
		unsigned sizes[2] = { 2048, 8192 };
		for (int si = 0; gd64_fn && si < 2; si++) {
			static char buf[8192];
			memset(buf, 0, sizeof buf);
			long off = 0;
			errno = 0;
			long r = gd64_fn(dfd, buf, sizes[si], &off);
			printf("XPR-DIR %s __getdirentries64 size=%u r=%ld seekoff=%ld errno=%d\n", label, sizes[si], r,
			       off, errno);
			if (r > 0) {
				long o = 0;
				int n = 0;
				while (o < r && n < 6) {
					struct dirent *e = (struct dirent *)(buf + o);
					printf("XPR-DIR %s __getdirentries64[%d] name=%s type=%u ino=%llu reclen=%u namlen=%u\n",
					       label, n, e->d_name, (unsigned)e->d_type, (unsigned long long)e->d_ino,
					       (unsigned)e->d_reclen, (unsigned)e->d_namlen);
					if (e->d_reclen == 0)
						break;
					o += e->d_reclen;
					n++;
				}
			}
			if (lseek(dfd, 0, SEEK_SET) < 0)
				break;
		}
		/* Layer 4: the dirfd form Xcode's own reader may use -- openat(dirfd, <leaf>) and
		 * openat(AT_FDCWD, <absolute file>). A wrong relative resolution and a missing file
		 * look different here. */
		errno = 0;
		int leaf = openat(dfd, "project.pbxproj", O_RDONLY);
		printf("XPR-DIR %s openat-dirfd-leaf fd=%d errno=%d\n", label, leaf, errno);
		if (leaf >= 0)
			close(leaf);
		close(dfd);
	}
}

static void openat_layers(const char *absfile)
{
	errno = 0;
	int fd = openat(AT_FDCWD, absfile, O_RDONLY);
	printf("XPR-OPENAT at-fdcwd-abs fd=%d errno=%d path=%s\n", fd, errno, absfile);
	if (fd >= 0)
		close(fd);
}

int main(int argc, char **argv)
{
	static int failures;
#define FAIL(stage, detail) do { printf("XPR-FAIL stage=%s %s\n", (stage), (detail)); failures++; } while (0)

	if (argc < 3) {
		FAIL("argv", "detail=usage: xcodebuild_project_read_guest <path-to-DVTFoundation> <project-dir>");
		printf("XPR_VERDICT=RED\n");
		return 1;
	}
	const char *dvtPath = argv[1];
	const char *projDir = argv[2];

	char cwd[PATH_MAX];
	if (!getcwd(cwd, sizeof cwd))
		snprintf(cwd, sizeof cwd, "(getcwd failed errno=%d)", errno);
	printf("XPR-ENV cwd=%s\n", cwd);

	void *objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	void *foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	void *cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation || !cf) {
		char detail[512];
		snprintf(detail, sizeof detail, "objc=%p foundation=%p cf=%p err=%s", objc, foundation, cf, dlerror());
		FAIL("dlopen-base", detail);
		printf("XPR_VERDICT=RED\n");
		return 1;
	}
	msgSend = (void *(*)(void *, void *, ...))dlsym(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(objc, "objc_getClass");
	if (!msgSend || !selRegister || !getClassFn) {
		FAIL("dlsym-objc", "detail=objc_msgSend/sel_registerName/objc_getClass missing");
		printf("XPR_VERDICT=RED\n");
		return 1;
	}
	void *dvt = dlopen(dvtPath, RTLD_NOW | RTLD_GLOBAL);
	if (!dvt) {
		char detail[1024];
		snprintf(detail, sizeof detail, "path=%s err=%s", dvtPath, dlerror());
		FAIL("dlopen-dvt", detail);
		printf("XPR_VERDICT=RED\n");
		return 1;
	}

	void *clsNSFileManager = getClassFn("NSFileManager");
	void *clsNSString = getClassFn("NSString");
	void *clsNSData = getClassFn("NSData");
	void *clsNSURL = getClassFn("NSURL");
	void *clsNSArray = getClassFn("NSArray");
	void *clsDVTFilePath = getClassFn("DVTFilePath");
	if (!clsNSFileManager || !clsNSString || !clsNSData || !clsNSURL || !clsNSArray) {
		char detail[256];
		snprintf(detail, sizeof detail, "NSFileManager=%p NSString=%p NSData=%p NSURL=%p NSArray=%p", clsNSFileManager,
			 clsNSString, clsNSData, clsNSURL, clsNSArray);
		FAIL("classes", detail);
		printf("XPR_VERDICT=RED\n");
		return 1;
	}
	clsNSStringG = clsNSString;
	printf("XPR-CLASS NSFileManager=%p NSString=%p NSData=%p NSURL=%p DVTFilePath=%p\n", clsNSFileManager, clsNSString,
	       clsNSData, clsNSURL, clsDVTFilePath);

	/* ---- 1/2. the two path forms, and the libc layer on each --------------------------------- */
	char absDir[PATH_MAX], absFile[PATH_MAX], relFile[PATH_MAX];
	snprintf(absDir, sizeof absDir, "%s", projDir);
	snprintf(absFile, sizeof absFile, "%s/project.pbxproj", projDir);
	/* The relative form is what `xcodebuild -project <name>.xcodeproj` gives the reader; it is
	 * derived from cwd exactly the way a caller that received a relative path would derive it. */
	const char *relDir = strrchr(projDir, '/');
	relDir = relDir ? relDir + 1 : projDir;
	snprintf(relFile, sizeof relFile, "%s/project.pbxproj", relDir);
	printf("XPR-PATHS projDir=%s relDir=%s absFile=%s relFile=%s\n", projDir, relDir, absFile, relFile);

	libc_layers("abs-file", absFile);
	libc_layers("rel-file", relFile);
	libc_layers("abs-dir", absDir);
	libc_layers("rel-dir", relDir);
	dir_layers("abs-dir", absDir);
	dir_layers("rel-dir", relDir);
	openat_layers(absFile);

	/* ---- 5. Foundation, on the tool's own objects --------------------------------------------- */
	void *fm = send0(clsNSFileManager, "defaultManager");
	printf("XPR-FM class=%p currentDirectoryPath=%s\n", fm,
	       cstr_of(send0(fm, "currentDirectoryPath")));

	void *relFileStr = nsstr(relFile);
	void *absFileStr = nsstr(absFile);
	void *relDirStr = nsstr(relDir);
	void *absDirStr = nsstr(absDir);

	char isDir = 0;
	printf("XPR-FM fileExistsAtPath: rel-dir=%d abs-dir=%d rel-file=%d abs-file=%d\n",
	       send_bool1(fm, "fileExistsAtPath:", relDirStr), send_bool1(fm, "fileExistsAtPath:", absDirStr),
	       send_bool1(fm, "fileExistsAtPath:", relFileStr), send_bool1(fm, "fileExistsAtPath:", absFileStr));
	isDir = 0;
	unsigned char e1 = send_bool_path_isdir(fm, "fileExistsAtPath:isDirectory:", relDirStr, &isDir);
	printf("XPR-FM fileExistsAtPath:isDirectory: rel-dir exists=%d isDirectory=%d\n", e1, isDir);
	isDir = 0;
	unsigned char e2 = send_bool_path_isdir(fm, "fileExistsAtPath:isDirectory:", absDirStr, &isDir);
	printf("XPR-FM fileExistsAtPath:isDirectory: abs-dir exists=%d isDirectory=%d\n", e2, isDir);

	void *err = NULL;
	void *contents = send_path_err(fm, "contentsOfDirectoryAtPath:error:", relDirStr, &err);
	print_error_full("XPR-FM contentsOfDirectoryAtPath:error: rel-dir", err);
	printf("XPR-FM contentsOfDirectoryAtPath: rel-dir count=%ld\n", contents ? send_int0(contents, "count") : -1L);
	if (contents) {
		long n = send_int0(contents, "count");
		for (long i = 0; i < n && i < 8; i++) {
			union { void *(*p)(void *, void *, ...); void *(*f)(void *, void *, long); } u;
			u.p = msgSend;
			printf("XPR-FM contents item[%ld]=%s\n", i, cstr_of(u.f(contents, selRegister("objectAtIndex:"), i)));
		}
	}
	err = NULL;
	void *contentsAbs = send_path_err(fm, "contentsOfDirectoryAtPath:error:", absDirStr, &err);
	print_error_full("XPR-FM contentsOfDirectoryAtPath:error: abs-dir", err);
	printf("XPR-FM contentsOfDirectoryAtPath: abs-dir count=%ld\n",
	       contentsAbs ? send_int0(contentsAbs, "count") : -1L);

	err = NULL;
	void *attrs = send_path_err(fm, "attributesOfItemAtPath:error:", absFileStr, &err);
	print_error_full("XPR-FM attributesOfItemAtPath:error: abs-file", err);
	printf("XPR-FM attributesOfItemAtPath: abs-file class=%p\n", attrs);

	err = NULL;
	void *data = send_path_ulong_err(send0(clsNSData, "alloc"), "initWithContentsOfFile:options:error:", relFileStr,
					 0UL, &err);
	print_error_full("XPR-FM NSData initWithContentsOfFile:options:error: rel-file", err);
	printf("XPR-FM NSData rel-file class=%p length=%ld\n", data, data ? send_int0(data, "length") : -1L);
	err = NULL;
	void *dataAbs = send_path_ulong_err(send0(clsNSData, "alloc"), "initWithContentsOfFile:options:error:", absFileStr,
					    0UL, &err);
	print_error_full("XPR-FM NSData initWithContentsOfFile:options:error: abs-file", err);
	printf("XPR-FM NSData abs-file class=%p length=%ld\n", dataAbs, dataAbs ? send_int0(dataAbs, "length") : -1L);

	err = NULL;
	/* NSUTF8StringEncoding == 4; the .pbxproj is ASCII/UTF-8 OpenStep plist text. */
	void *str = send_path_ulong_err2(send0(clsNSString, "alloc"), "initWithContentsOfFile:encoding:error:", relFileStr,
					 4UL, &err);
	print_error_full("XPR-FM NSString initWithContentsOfFile:encoding:error: rel-file", err);
	printf("XPR-FM NSString rel-file class=%p length=%ld\n", str, str ? send_int0(str, "length") : -1L);

	printf("XPR-FM stringByStandardizingPath rel-file=%s abs-file=%s\n",
	       cstr_of(send0(relFileStr, "stringByStandardizingPath")),
	       cstr_of(send0(absFileStr, "stringByStandardizingPath")));
	printf("XPR-FM stringByResolvingSymlinksInPath rel-file=%s abs-file=%s\n",
	       cstr_of(send0(relFileStr, "stringByResolvingSymlinksInPath")),
	       cstr_of(send0(absFileStr, "stringByResolvingSymlinksInPath")));

	err = NULL;
	void *url = send1(clsNSURL, "fileURLWithPath:", absFileStr);
	printf("XPR-FM NSURL fileURLWithPath: abs-file class=%p absoluteString=%s\n", url,
	       url ? cstr_of(send0(url, "absoluteString")) : "(nil)");
	if (url) {
		err = NULL;
		unsigned char reach = send_bool_err(url, "checkResourceIsReachableAndReturnError:", &err);
		print_error_full("XPR-FM NSURL checkResourceIsReachableAndReturnError: abs-file", err);
		printf("XPR-FM NSURL reachable abs-file=%d\n", reach);
		void *keys = send1(clsNSArray, "arrayWithObject:", nsstr("NSURLIsDirectoryKey"));
		err = NULL;
		void *vals = send_id_err(url, "resourceValuesForKeys:error:", keys, &err);
		print_error_full("XPR-FM NSURL resourceValuesForKeys:error: abs-file", err);
		printf("XPR-FM NSURL resourceValues abs-file class=%p\n", vals);
	}

	/* ---- 7. the framework's own file object --------------------------------------------------- */
	if (clsDVTFilePath) {
		void *fp = send1(clsDVTFilePath, "filePathForPathString:", absFileStr);
		printf("XPR-DVT filePathForPathString: abs-file class=%p\n", fp);
		if (fp) {
			printf("XPR-DVT pathString=%s\n", cstr_of(send0(fp, "pathString")));
			printf("XPR-DVT fileExists=%d\n", send_bool0(fp, "fileExists"));
		}
		void *fpd = send1(clsDVTFilePath, "filePathForPathString:", absDirStr);
		printf("XPR-DVT filePathForPathString: abs-dir class=%p fileExists=%d\n", fpd,
		       fpd ? send_bool0(fpd, "fileExists") : -1);
	} else {
		printf("XPR-DVT class=(absent)\n");
	}

	/* ---- verdict ------------------------------------------------------------------------------ */
	/* GREEN is the ladder's predicate: the project file is visible at every layer, on both path
	 * forms, and its bytes reach Foundation. A listing that is empty with errno 0, an error whose
	 * userInfo names a different path, or a failed read each fail this and say which layer did. */
	{
		struct stat st;
		if (stat(absFile, &st) != 0 || st.st_size <= 0) {
			FAIL("abs-file-stat", "detail=the absolute project file is not a readable non-empty file");
		}
		if (stat(relFile, &st) != 0 || st.st_size <= 0) {
			FAIL("rel-file-stat", "detail=the relative project file is not a readable non-empty file");
		}
		if (!data || send_int0(data, "length") <= 0) {
			FAIL("nsdata", "detail=Foundation read no bytes from the relative project file");
		}
		if (!str || send_int0(str, "length") <= 0) {
			FAIL("nsstring", "detail=Foundation decoded no characters from the relative project file");
		}
		if (!contents || send_int0(contents, "count") <= 0) {
			FAIL("contentsOfDirectory", "detail=the project directory listed nothing to Foundation");
		}
	}
	printf("XPR failures=%d\n", failures);
	printf("XPR_VERDICT=%s\n", failures == 0 ? "GREEN" : "RED");
	return failures == 0 ? 0 : 1;
}

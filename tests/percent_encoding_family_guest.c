/*
 * percent_encoding_family_guest.c -- permanent guest oracle for the NSString(NSURLUtilities)
 * percent-encoding family and the NSCharacterSet(NSURLUtilities) sets it consumes, on the DEPLOYED
 * Foundation/CoreFoundation.
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder dies in the guest with
 *
 *   Uncaught Exception: -[__NSCFString stringByAddingPercentEncodingWithAllowedCharacters:]:
 *     unrecognized selector sent to instance ...
 *   ... +[PackedPathEntry entryWithParent:bytes:length:] (PackedPaths)
 *   ... XCInitializeCoreIfNeeded (DevToolsCore) ... XcodeBuildMain
 *
 * measured on prefix-verify14.  The FAMILY, not one selector, is the scope: NSURL.h declares
 * NSString(NSURLUtilities) as
 *
 *   - (nullable NSString *)stringByAddingPercentEncodingWithAllowedCharacters:(NSCharacterSet *)set;
 *   @property (nullable, readonly, copy) NSString *stringByRemovingPercentEncoding;
 *   - (nullable NSString *)stringByAddingPercentEscapesUsingEncoding:(NSStringEncoding)enc;   (deprecated)
 *   - (nullable NSString *)stringByReplacingPercentEscapesUsingEncoding:(NSStringEncoding)enc;(deprecated)
 *
 * and NSCharacterSet(NSURLUtilities) as the six URL*AllowedCharacterSet accessors.  What the
 * workload's load closure (the load-command graph of Framework/libxcodebuildLoader.dylib, 64 images)
 * actually references, and what the deployed runtime answered, is printed by this fixture so the
 * boundary is a measurement rather than a guess.
 *
 * WHAT IT MEASURES, in one process, with no compile-time reference to Foundation or ObjC:
 *
 *   * the receiver's class (the workload's is __NSCFString);
 *   * which family selectors the receiver and the class objects actually answer (-respondsToSelector:);
 *   * the two URL sets the closure references, and whether the accessors answer them;
 *   * the encoding contract, grounded in the tree's own
 *     _CFStringCreateByAddingPercentEncodingWithAllowedCharacters
 *     (swift-corelibs-foundation CoreFoundation/URL.subproj/CFURLComponents_URIParser.c):
 *     '/' '@' ':' '.' 'A' pass through a URLPathAllowed set, ';' is always percent-encoded there and
 *     ':' only passes AFTER the first '/', a byte outside the set (including '%' itself and any byte
 *     of a non-ASCII UTF-8 sequence) becomes uppercase %XX, and the URLQueryAllowed set keeps ';' ':'
 *     '?' '=';
 *   * the round trip back through -stringByRemovingPercentEncoding.
 *
 * Every member is guarded by -respondsToSelector: so a RED arm names the missing member and exits
 * cleanly instead of dying in the runtime's uncaught-exception path.  PERCENT_ENCODING_VERDICT=GREEN
 * is the machine-readable line the harness ok-marker reads.
 */

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
#define CF_LIBRARY "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"

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
	if (!found)
		printf("PERCENT-NOTE symbol=%s error=%s\n", name, dlerror());
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

static int responds(void *object, const char *selector)
{
	return (int)(long)msgSend(object, selRegister("respondsToSelector:"), selRegister(selector));
}

/* An NSString without a compile-time Foundation reference: +[NSString stringWithUTF8String:], the
 * class looked up by name, so the receiver is exactly what the workload's receiver is. */
static void *string_from_c(void *string_class, const char *c)
{
	return send1(string_class, "stringWithUTF8String:", (void *)c);
}

static const char *c_from_string(void *string)
{
	return (const char *)send(string, "UTF8String");
}

static int check_encoding(void *string_class, void *set, const char *set_label,
                          const char *input, const char *expected, const char *label)
{
	void *string = string_from_c(string_class, input);
	void *encoded = string ? send1(string, "stringByAddingPercentEncodingWithAllowedCharacters:", set) : NULL;
	const char *got = encoded ? c_from_string(encoded) : "(nil)";
	int ok = encoded != NULL && got != NULL && strcmp(got, expected) == 0;

	printf("PERCENT-ENCODE %s set=%s input=\"%s\" got=\"%s\" expected=\"%s\" ok=%d\n",
	    label, set_label, input, got ? got : "(null)", expected, ok);
	if (!ok)
		failures++;

	if (encoded && responds(encoded, "stringByRemovingPercentEncoding")) {
		void *decoded = send(encoded, "stringByRemovingPercentEncoding");
		const char *back = decoded ? c_from_string(decoded) : "(nil)";
		int roundtrip = decoded != NULL && back != NULL && strcmp(back, input) == 0;
		printf("PERCENT-ROUNDTRIP input=\"%s\" decoded=\"%s\" ok=%d\n",
		    input, back ? back : "(null)", roundtrip);
		if (!roundtrip)
			failures++;
	}
	return ok;
}

int main(void)
{
	void *objc;
	void *foundation;
	void *cf;
	void *string_class;
	void *set_class;
	void *receiver;
	void *path_set;
	void *query_set;
	int has_percent;
	int has_removing;
	int has_adding_legacy;
	int has_replacing_legacy;
	int has_path;
	int has_query;

	setvbuf(stdout, NULL, _IOLBF, 0);

	objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation || !cf) {
		printf("PERCENT-FAIL stage=dlopen objc=%p foundation=%p cf=%p err=%s\n", objc, foundation, cf, dlerror());
		printf("PERCENT_ENCODING_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))lookup(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))lookup(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))lookup(objc, "objc_getClass");
	classNameFn = (const char *(*)(void *))lookup(objc, "class_getName");
	if (!msgSend || !selRegister || !getClassFn || !classNameFn) {
		printf("PERCENT-FAIL stage=runtime-symbols\n");
		printf("PERCENT_ENCODING_VERDICT=RED\n");
		return 1;
	}

	string_class = lookup(foundation, "_OBJC_CLASS_$_NSString");
	if (!string_class)
		string_class = lookup(cf, "_OBJC_CLASS_$_NSString");
	set_class = getClassFn("NSCharacterSet");
	if (!set_class)
		set_class = (void *)lookup(foundation, "_OBJC_CLASS_$_NSCharacterSet");
	if (!string_class || !set_class) {
		printf("PERCENT-FAIL stage=classes NSString=%p NSCharacterSet=%p\n", string_class, set_class);
		printf("PERCENT_ENCODING_VERDICT=RED\n");
		return 1;
	}

	/* ---- the receiver the workload sends the selector to ---- */
	receiver = string_from_c(string_class, "a b/c?d;e:f");
	if (!receiver) {
		printf("PERCENT-FAIL stage=receiver-unavailable\n");
		printf("PERCENT_ENCODING_VERDICT=RED\n");
		return 1;
	}
	printf("PERCENT-RECEIVER class=%s\n", classNameFn((void *)msgSend(receiver, selRegister("class"))));

	/* ---- the four family selectors, as the deployed runtime answers them ---- */
	has_percent         = responds(receiver, "stringByAddingPercentEncodingWithAllowedCharacters:");
	has_removing        = responds(receiver, "stringByRemovingPercentEncoding");
	has_adding_legacy   = responds(receiver, "stringByAddingPercentEscapesUsingEncoding:");
	has_replacing_legacy= responds(receiver, "stringByReplacingPercentEscapesUsingEncoding:");
	printf("PERCENT-FAMILY stringByAddingPercentEncodingWithAllowedCharacters: responds=%d\n", has_percent);
	printf("PERCENT-FAMILY stringByRemovingPercentEncoding responds=%d\n", has_removing);
	printf("PERCENT-FAMILY stringByAddingPercentEscapesUsingEncoding: responds=%d\n", has_adding_legacy);
	printf("PERCENT-FAMILY stringByReplacingPercentEscapesUsingEncoding: responds=%d\n", has_replacing_legacy);

	/* ---- the NSCharacterSet URL sets the load closure references ---- */
	has_path  = responds(set_class, "URLPathAllowedCharacterSet");
	has_query = responds(set_class, "URLQueryAllowedCharacterSet");
	printf("PERCENT-SET URLPathAllowedCharacterSet responds=%d\n", has_path);
	printf("PERCENT-SET URLQueryAllowedCharacterSet responds=%d\n", has_query);

	path_set  = has_path ? send(set_class, "URLPathAllowedCharacterSet") : NULL;
	query_set = has_query ? send(set_class, "URLQueryAllowedCharacterSet") : NULL;
	printf("PERCENT-SET-OBJECT path=%p query=%p\n", path_set, query_set);

	if (!has_percent || !has_removing || !path_set || !query_set) {
		printf("PERCENT-MISSING percent=%d removing=%d path=%d query=%d\n",
		    !has_percent, !has_removing, !path_set, !query_set);
		printf("PERCENT_ENCODING_VERDICT=RED\n");
		return 1;
	}

	/* ---- the encoding contract (authority: CFURLComponents_URIParser.c) ---- */
	/* URLPathAllowed: ':' passes only after the first '/'; ';' never passes; '?' and '#' never. */
	check_encoding(string_class, path_set, "path", "a b/c?d;e:f", "a%20b/c%3Fd%3Be:f", "path-late-colon");
	check_encoding(string_class, path_set, "path", "a:b/c", "a%3Ab/c", "path-early-colon");
	check_encoding(string_class, path_set, "path", "a/:b", "a/:b", "path-after-slash-colon");
	check_encoding(string_class, path_set, "path", "a;b", "a%3Bb", "path-semicolon");
	check_encoding(string_class, path_set, "path", "a%b", "a%25b", "path-escapes-percent");
	check_encoding(string_class, path_set, "path", "a#b", "a%23b", "path-hash");
	/* UTF-8: each byte of the multi-byte sequence is escaped. */
	check_encoding(string_class, path_set, "path", "\xC3\xA9", "%C3%A9", "path-utf8");
	/* URLQueryAllowed: ';' ':' '?' '=' '&' pass; ' ' and '#' do not. */
	check_encoding(string_class, query_set, "query", "a b/c?d;e:f", "a%20b/c?d;e:f", "query-keeps-;-:-?");
	check_encoding(string_class, query_set, "query", "a=b&c=d#e", "a=b&c=d%23e", "query-keeps-=&-encodes-#");
	check_encoding(string_class, query_set, "query", "\xC3\xA9", "%C3%A9", "query-utf8");
	/* Empty input is returned unchanged. */
	check_encoding(string_class, path_set, "path", "", "", "path-empty");

	if (failures != 0) {
		printf("PERCENT-FAIL failures=%d\n", failures);
		printf("PERCENT_ENCODING_VERDICT=RED\n");
		return 1;
	}
	printf("PERCENT_ENCODING_VERDICT=GREEN\n");
	return 0;
}

/*
 * url_path_allowed_character_set_guest.c -- permanent guest oracle for
 * +[NSCharacterSet URLPathAllowedCharacterSet] on the deployed Foundation/CoreFoundation.
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder (scripts/xcodebuild-workload-run.sh,
 * both workloads) dies in the guest with
 *
 *   ** INTERNAL ERROR: Uncaught exception **
 *   Uncaught Exception: +[NSCharacterSet URLPathAllowedCharacterSet]: unrecognized selector sent to instance 0x7eacc97cd360
 *   ...
 *   __simple_abort pid=598492 ra=0x7EACC77CE44E[sigexc-fatal sig=6 ...]
 *   XCB_RC=134
 *
 * measured on prefix-verify14 (2026-10-13), when a dynamic library the load closure brings in
 * (libswiftFoundation, PackedPaths and DevToolsCore all reference the selector) reaches the
 * accessor. Darling's NSCharacterSet comes from CoreFoundation (NSCharacterSet.m, re-exported by
 * Foundation), and THAT class implemented only the CoreFoundation predefined sets: it carried no
 * URLPathAllowedCharacterSet, and nothing in the deployed CoreFoundation defined
 * _CFURLComponentsGetURL*AllowedCharacterSet either (CFURLComponents_URIParser.c from the
 * swift-corelibs-foundation submodule is not part of the CF build). The fix implements the
 * accessor and takes its membership from that submodule's own kURLPathAllowedCharacters.
 *
 * WHAT THIS MEASURES, in one process, with no compile-time reference to Foundation or ObjC:
 * the contract Apple documents for NSCharacterSet(NSURLUtilities), and the membership this tree
 * defines in CFURLComponents_URIParser.c kURLPathAllowedCharacters
 * "!$&'()*+,-./0123456789:=@ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz~":
 *
 *   non-nil; two sends return the SAME object (shared);
 *   '/' '@' ':' '.' 'A' are members, ';' ' ' '?' '#' are not;
 *   immutable in BEHAVIOUR: a mutable copy takes addCharactersInString: (positive control) while
 *   the shared set does not, and the shared set has the same concrete class as the tree's own
 *   predefined sets (+[NSCharacterSet whitespaceCharacterSet]).
 *
 * That last shape is deliberate: this tree's concrete NSCharacterSet class is __NSCFCharacterSet,
 * a subclass of the NSMutableCharacterSet branch, so a class-name test would gate the tree's class
 * layout rather than this accessor. The positive control is what makes "nothing changed" mean
 * something.
 *
 * A DIAGNOSTIC, not a gate: -[NSString stringByAddingPercentEncodingWithAllowedCharacters:], the
 * accessor's real consumer, is NOT implemented in this runtime (measured 2026-10-13: the send
 * raises '-[__NSCFString stringByAddingPercentEncodingWithAllowedCharacters:]: unrecognized
 * selector', which aborts through the runtime's uncaught-exception path). The fixture asks
 * -respondsToSelector: and reports URLPATH-NOTE consumer=... available=0, so the next rung's
 * missing API is visible in the same run without failing this oracle. DO NOT treat that note as
 * this fix's scope.
 *
 * Every intermediate fact is printed unconditionally as a URLPATH-* line, so a RED arm can quote
 * the unrecognized-selector failure from the run's own output; URLPATH_VERDICT=GREEN is the
 * machine-readable line the harness ok-marker reads.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: with no +URLPathAllowedCharacterSet in
 * the loaded CoreFoundation the accessor send aborts the process with the uncaught exception
 * quoted above, so the RED line is the runtime's own exception and not a fixture verdict.
 *
 * MEASURED RED (prefix-verify14, deployed CoreFoundation sha256 4afd5fdb...):
 *   urlpathprobe[630250:3e8] Terminating app due to uncaught exception 'NSException', reason:
 *     '+[NSCharacterSet URLPathAllowedCharacterSet]: unrecognized selector sent to instance 0x7bd992a20360'
 *   URLPATH_RUN_RC=132
 * MEASURED GREEN (same prefix, deployed CoreFoundation sha256 bf8aecff...):
 *   URLPATH-SET non-nil=1 shared-identity=1 class=__NSCFCharacterSet self-class=__NSCFCharacterSet predefined-class=__NSCFCharacterSet isKindOfNSMutableCharacterSet=1 mutable-control-added-space=1 immutable-after-add-space=0
 *   URLPATH-MEMBER '/' 0x2f member=1 expected=1 ... URLPATH-MEMBER '#' 0x23 member=0 expected=0
 *   URLPATH_VERDICT=GREEN
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
		printf("URLPATH-NOTE symbol=%s error=%s\n", name, dlerror());
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

/* An NSString literal without a Foundation reference: every instance of __NSCFConstantString in
 * this image's own __cstring/__data has the class the runtime knows, so the string is built with
 * +[NSString stringWithUTF8String:], the class looked up by name. */
static void *string_from_c(void *string_class, const char *c)
{
	return send1(string_class, "stringWithUTF8String:", (void *)c);
}

static const char *c_from_string(void *string)
{
	return (const char *)send(string, "UTF8String");
}

static void check_member(void *set, const char *label, unsigned code, int expected)
{
	int actual = (int)(long)msgSend(set, selRegister("characterIsMember:"), (unsigned long)code);
	printf("URLPATH-MEMBER '%s' 0x%02x member=%d expected=%d\n", label, code, actual, expected);
	if (actual != expected) {
		printf("URLPATH-MISMATCH member '%s' got=%d want=%d\n", label, actual, expected);
		failures++;
	}
}

/* The accessor's real consumer is -[NSString stringByAddingPercentEncodingWithAllowedCharacters:] --
 * but Darling's Foundation does not implement that method yet (measured 2026-10-13: the send raises
 * '-[__NSCFString stringByAddingPercentEncodingWithAllowedCharacters:]: unrecognized selector sent to
 * instance', which aborts through the runtime's uncaught-exception path). So the note is guarded by
 * -respondsToSelector: and reports the gap instead of dying on it: the accessor and its membership
 * are what this oracle gates, and the missing consumer is recorded here as the next rung's name. */
static void note_percent_encoding(void *set, void *string_class, const char *input)
{
	void *string = string_from_c(string_class, input);
	if (!string) {
		printf("URLPATH-NOTE percent-encoding=\"string-unavailable\" input=\"%s\"\n", input);
		return;
	}
	int responds = (int)(long)msgSend(string, selRegister("respondsToSelector:"),
	    selRegister("stringByAddingPercentEncodingWithAllowedCharacters:"));
	printf("URLPATH-NOTE consumer=stringByAddingPercentEncodingWithAllowedCharacters: available=%d input=\"%s\"\n",
	    responds, input);
	if (!responds) {
		return;
	}
	void *encoded = send1(string, "stringByAddingPercentEncodingWithAllowedCharacters:", set);
	if (!encoded) {
		printf("URLPATH-NOTE percent-encoding=\"nil-result\" input=\"%s\"\n", input);
		return;
	}
	const char *actual = c_from_string(encoded);
	printf("URLPATH-NOTE percent-encoding=\"%s\" input=\"%s\"\n", actual ? actual : "(null)", input);
}

int main(void)
{
	void *objc;
	void *foundation;
	void *cf;
	void *cls;
	void *set;
	void *second;

	setvbuf(stdout, NULL, _IOLBF, 0);

	objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation || !cf) {
		printf("URLPATH-FAIL stage=dlopen objc=%p foundation=%p cf=%p err=%s\n", objc, foundation, cf, dlerror());
		printf("URLPATH_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))lookup(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))lookup(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))lookup(objc, "objc_getClass");
	classNameFn = (const char *(*)(void *))lookup(objc, "class_getName");
	if (!msgSend || !selRegister || !getClassFn || !classNameFn) {
		printf("URLPATH-FAIL stage=runtime-symbols\n");
		printf("URLPATH_VERDICT=RED\n");
		return 1;
	}

	void *string_class = lookup(foundation, "_OBJC_CLASS_$_NSString");
	if (!string_class) {
		string_class = lookup(cf, "_OBJC_CLASS_$_NSString");
	}
	if (!string_class) {
		printf("URLPATH-FAIL stage=class NSString not found\n");
		printf("URLPATH_VERDICT=RED\n");
		return 1;
	}

	cls = getClassFn("NSCharacterSet");
	if (!cls) {
		cls = (void *)lookup(foundation, "_OBJC_CLASS_$_NSCharacterSet");
	}
	if (!cls) {
		printf("URLPATH-FAIL stage=class NSCharacterSet not registered\n");
		printf("URLPATH_VERDICT=RED\n");
		return 1;
	}
	printf("URLPATH-CLASS name=%s image-foundation=%p image-cf=%p\n", classNameFn(cls), foundation, cf);

	/* ---- the accessor: on the unfixed runtime this send is the abort ---- */
	set = send(cls, "URLPathAllowedCharacterSet");

	if (set == NULL) {
		printf("URLPATH-FAIL stage=accessor +[NSCharacterSet URLPathAllowedCharacterSet] returned nil\n");
		printf("URLPATH_VERDICT=RED\n");
		return 1;
	}

	/* ---- contract: non-nil, shared, immutable ----
	 *
	 * Immutability is measured by BEHAVIOUR, because this runtime's concrete class for a CF
	 * character set is __NSCFCharacterSet, which sits under the NSMutableCharacterSet branch of the
	 * class tree (isKindOfClass: is true for BOTH NSCharacterSet and NSMutableCharacterSet), so a
	 * class-name test would be measuring the tree's class layout rather than the accessor.
	 * What the accessor promises is a shared, NON-MUTABLE set:
	 *   (a) two sends answer the same object;
	 *   (b) the same class answers a CF predefined set (precedent), so this is the tree's normal
	 *       accessor shape and not a special case built for this method;
	 *   (c) no mutating message changes it -- and the mutating messages must be REAL in this
	 *       runtime, which the fixture proves with a positive control below, so "nothing changed"
	 *       cannot pass because mutation is a no-op everywhere.
	 */
	second = send(cls, "URLPathAllowedCharacterSet");
	int shared = (second == set);
	void *control = send(cls, "whitespaceCharacterSet");
	const char *set_class = classNameFn((void *)msgSend(set, selRegister("class")));
	const char *control_class = control ? classNameFn((void *)msgSend(control, selRegister("class"))) : "(nil)";
	int same_shape_as_predefined = (control && strcmp(set_class, control_class) == 0);

	/* (c) the mutating path, exercised on a MUTABLE COPY first so the control is positive. */
	void *mutable_class = getClassFn("NSMutableCharacterSet");
	int is_kind_of_mutable = (mutable_class && msgSend(set, selRegister("isKindOfClass:"), mutable_class) != NULL);
	void *mutable_copy = send(set, "mutableCopy");
	int mutable_copy_change = 0;
	if (mutable_copy) {
		msgSend(mutable_copy, selRegister("addCharactersInString:"), (void *)string_from_c(string_class, " "));
		mutable_copy_change = (int)(long)msgSend(mutable_copy, selRegister("characterIsMember:"), (unsigned long)' ');
	}
	/* the shared set itself must not change under the same message */
	msgSend(set, selRegister("addCharactersInString:"), (void *)string_from_c(string_class, " "));
	int set_after_mutation = (int)(long)msgSend(set, selRegister("characterIsMember:"), (unsigned long)' ');
	printf("URLPATH-SET non-nil=1 shared-identity=%d class=%s self-class=%s predefined-class=%s "
	       "isKindOfNSMutableCharacterSet=%d mutable-control-added-space=%d immutable-after-add-space=%d\n",
	    shared, set_class, set_class, control_class, is_kind_of_mutable, mutable_copy_change, set_after_mutation);
	if (!shared || !same_shape_as_predefined) {
		printf("URLPATH-MISMATCH contract shared=%d sameShapeAsPredefined=%d\n", shared, same_shape_as_predefined);
		failures++;
	}
	if (mutable_copy_change != 1) {
		printf("URLPATH-MISMATCH mutable-control did not take a mutation (added=%d): the immutability check below would be vacuous\n",
		    mutable_copy_change);
		failures++;
	}
	if (set_after_mutation != 0) {
		printf("URLPATH-MISMATCH immutable-set mutated: space became a member after addCharactersInString:\n");
		failures++;
	}
	(void)is_kind_of_mutable;

	/* ---- membership: documented members and documented non-members ---- */
	check_member(set, "/", '/', 1);
	check_member(set, "@", '@', 1);
	check_member(set, ":", ':', 1);
	check_member(set, ".", '.', 1);
	check_member(set, "A", 'A', 1);
	check_member(set, ";", ';', 0);
	check_member(set, " ", ' ', 0);
	check_member(set, "?", '?', 0);
	check_member(set, "#", '#', 0);

	note_percent_encoding(set, string_class, "a b/c?d;e");

	if (failures != 0) {
		printf("URLPATH-FAIL failures=%d\n", failures);
		printf("URLPATH_VERDICT=RED\n");
		return 1;
	}
	printf("URLPATH_VERDICT=GREEN\n");
	return 0;
}

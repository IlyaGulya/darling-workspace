/*
 * regular_expression_range_count_guest.c -- permanent guest oracle for the RANGE COUNT AND GROUP RANGES of
 * an NSRegularExpression match (NSTextCheckingResult) on the deployed Foundation.
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder (scripts/xcodebuild-workload-run.sh, both workloads)
 * reached the last assertion in the workload's own stream:
 *
 *   [MT] DVTAssertions: ASSERTION FAILURE in DVTFrameworks/DVTFoundation/DeveloperStructure/DVTBuildVersion.m:48
 *   Details:  There must be five ranges in this build version match
 *     3 -[DVTBuildVersion initWithString:].cold.2   4 -[DVTBuildVersion initWithString:]
 *     5 +[DVTBuildVersion buildVersionWithString:]  6 __33-[DVTToolsInfo toolsBuildVersion]_block_invoke
 *    13 -[DVTToolsInfo toolsBuildVersion]           14 IDEInitialize
 *   [sigexc-fatal sig=4 code=2 addr=0x78F6133C747F]  XCB_RC=132
 *
 * The caller is +[DVTBuildVersion buildVersionWithString:] with the string Xcode's own Contents/version.plist
 * carries (ProductBuildVersion). Disassembling DVTFoundation's -[DVTBuildVersion initWithString:] (x86_64
 * slice, 0x302d9) shows the sequence: the class method builds the pattern, `-firstMatchInString:options:range:`
 * produces a match, `testq %r15,%r15` branches to cold.3 ("Unable to parse build version: %@", line 47) when
 * the match is nil, and then, at 0x303c6, `cmpq $0x5, %rax` on the value just sent to the match object branches
 * to cold.1 (line 48) -- the assertion above -- when the count is not five. Line 49's cold.2 ("Build version is
 * not valid") is the later validation, reached only after rangeAtIndex: 1..4 have been read.
 *
 * The pattern is ^(\d+)([A-Z])(\d+)([a-z]?)$: four capture groups plus the whole match, so a Darwin-faithful
 * match reports numberOfRanges == 5 and rangeAtIndex: 0..4. Darling's Foundation backs
 * +[NSRegularExpression regularExpressionWithPattern:options:error:] itself (the deployed
 * Foundation.framework defines _OBJC_CLASS_$_NSRegularExpression and libswiftFoundation, which Foundation
 * reexports, does not), over ICU; its match objects did not carry the count.
 *
 * WHAT THIS MEASURES, in one process, with no compile-time reference to Foundation or ObjC
 * (dlopen + objc_msgSend + sel_registerName only), on the DEPLOYED Foundation:
 *
 *   1. NO-OPTIONAL: the wall's own input -- the ProductBuildVersion string of the Xcode under test, with the
 *      optional ([a-z]?) group present but empty -- must report numberOfRanges == 5 with
 *      {0,6} {0,2} {2,1} {3,3} {6,0}. The last range is an EMPTY MATCH at the end of the subject, not
 *      NSNotFound: the group participated and matched zero characters, which is a different fact from a group
 *      that did not participate, and the caller's validation distinguishes them.
 *   2. WITH-OPTIONAL: the same pattern on the same string plus one lowercase letter must still report
 *      numberOfRanges == 5, with the last range growing to {6,1}.
 *   3. UNPARTICIPATING: ^(a)|(b)$ on "a" -- an alternation whose second branch cannot match -- must report
 *      numberOfRanges == 3 with the second group as {NSNotFound, 0}. This is the other half of the contract
 *      and the one the ICU start/end protocol can turn into a plausible-looking wrong range.
 *
 * The vendored reference implementation in this tree
 * (corefoundation/submodules/swift-corelibs-foundation/Sources/Foundation/NSTextCheckingResult.swift,
 * NSRegularExpressionCheckingResult) has exactly these semantics: numberOfRanges is the count of the range
 * array the matcher filled, range(at:) is an index into it, and a range whose location is not found is
 * normalised to {NSNotFound, 0}.
 *
 * Every intermediate fact is printed unconditionally as a REGEX-* line, so a RED arm quotes the failure from
 * the run's own output; REGEX_VERDICT=GREEN is the machine-readable line the harness ok-marker reads.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the unfixed Foundation answered the wall's own
 * input with a range count that is not five, so the RED line is the runtime's own count.
 */

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"

/* The wall's pattern and the two strings the caller hands it: Xcode 26.3's ProductBuildVersion, and the
 * same string with the optional lowercase suffix actually present. */
#define BUILD_PATTERN "^([0-9]+)([A-Z])([0-9]+)([a-z]?)$"
#define BUILD_VERSION "17C529"
#define BUILD_VERSION_LETTER "17C529a"

/* The alternation whose second branch cannot participate. */
#define ALT_PATTERN "^(a)|(b)$"
#define ALT_SUBJECT "a"

#define NOT_FOUND ((unsigned long)0x7fffffffffffffffUL)  /* NSNotFound is NSIntegerMax, not ULONG_MAX */

typedef struct {
	unsigned long location;
	unsigned long length;
} NSRange;

/* objc_msgSend: the struct-returning form gets its own prototype, because a 16-byte NSRange comes back in
 * rax:rdx and a plain `void *` return type would read only half of it. */
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
		printf("REGEX-NOTE symbol=%s error=%s\n", name, dlerror());
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

/* An NSString built without a Foundation reference at compile time. */
static void *string_from_c(void *string_class, const char *c)
{
	return send1(string_class, "stringWithUTF8String:", (void *)c);
}

static const char *c_from_string(void *string)
{
	return string ? (const char *)send(string, "UTF8String") : "(nil)";
}

/* An NSRange-returning send. The two prototypes are punning through a union rather than cast, because a
 * cast between incompatible function types is a -Werror diagnostic and the two ABIs genuinely differ only
 * in how the 16-byte result is returned. */
static NSRange send_range(void *object, const char *selector, unsigned long index)
{
	union {
		void *(*plain)(void *, void *, ...);
		NSRange (*range)(void *, void *, unsigned long);
	} pun;
	pun.plain = msgSend;
	return pun.range(object, selRegister(selector), index);
}

/* One match of one pattern against one subject, reported raw then gated. */
struct expectation {
	unsigned long count;
	NSRange ranges[8];
};

static long check_one(void *regex_class, void *string_class, const char *label, const char *pattern,
    const char *subject, const struct expectation *want)
{
	void *pattern_string = string_from_c(string_class, pattern);
	void *subject_string = string_from_c(string_class, subject);
	void *error = NULL;
	void *regex = msgSend(regex_class, selRegister("regularExpressionWithPattern:options:error:"),
	    pattern_string, (void *)0, &error);
	if (!regex) {
		printf("REGEX-FAIL stage=compile label=%s pattern=%s err=%s\n", label, pattern, c_from_string(error));
		failures++;
		return -1;
	}
	long groups = (long)msgSend(regex, selRegister("numberOfCaptureGroups"));

	void *match = msgSend(regex, selRegister("firstMatchInString:options:range:"),
	    subject_string, (void *)0, (NSRange){0, strlen(subject)});
	if (!match) {
		printf("REGEX-FAIL stage=no-match label=%s pattern=%s subject=%s\n", label, pattern, subject);
		failures++;
		return -1;
	}

	unsigned long count = (unsigned long)msgSend(match, selRegister("numberOfRanges"));
	NSRange whole = send_range(match, "range", 0);
	printf("REGEX-MATCH label=%s pattern=%s subject=%s class=%s captureGroups=%ld numberOfRanges=%lu whole={%lu,%lu}\n",
	    label, pattern, subject, classNameFn((void *)(uintptr_t)match), groups, count,
	    whole.location, whole.length);
	if (groups != (long)(want->count - 1)) {
		printf("REGEX-CHECK label=%s name=capture-groups got=%ld want=%lu\n", label, groups, want->count - 1);
		failures++;
	}
	if (count != want->count) {
		printf("REGEX-CHECK label=%s name=number-of-ranges got=%lu want=%lu\n", label, count, want->count);
		failures++;
	}
	if (whole.location != want->ranges[0].location || whole.length != want->ranges[0].length) {
		printf("REGEX-CHECK label=%s name=whole-range got={%lu,%lu} want={%lu,%lu}\n", label,
		    whole.location, whole.length, want->ranges[0].location, want->ranges[0].length);
		failures++;
	}

	/* The private rangeArray is reported as a NOTE, not gated: it is not part of the public contract the
	 * caller uses, and Darwin's storage shape for it is not measurable here. */
	void *range_array = msgSend(match, selRegister("rangeArray"));
	if (range_array) {
		unsigned long array_count = (unsigned long)msgSend(range_array, selRegister("count"));
		printf("REGEX-NOTE label=%s rangeArray-count=%lu\n", label, array_count);
	} else {
		printf("REGEX-NOTE label=%s rangeArray=(none)\n", label);
	}

	/* Every index the caller can reach. Reading past numberOfRanges is out of contract on Darwin and is
	 * not probed here; the fixed implementation must simply not report more than it has. */
	for (unsigned long idx = 0; idx < want->count; idx++) {
		NSRange got = send_range(match, "rangeAtIndex:", idx);
		NSRange expect = want->ranges[idx];
		printf("REGEX-RANGE label=%s idx=%lu got={%lu,%lu} want={%lu,%lu}\n", label, idx,
		    got.location, got.length, expect.location, expect.length);
		if (got.location != expect.location || got.length != expect.length) {
			printf("REGEX-CHECK label=%s name=range-%lu got={%lu,%lu} want={%lu,%lu}\n", label, idx,
			    got.location, got.length, expect.location, expect.length);
			failures++;
		}
	}
	return groups;
}

int main(int argc, char **argv)
{
	void *objc;
	void *foundation;

	setvbuf(stdout, NULL, _IOLBF, 0);

	objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation) {
		printf("REGEX-FAIL stage=dlopen objc=%p foundation=%p err=%s\n", objc, foundation, dlerror());
		printf("REGEX_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))lookup(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))lookup(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))lookup(objc, "objc_getClass");
	classNameFn = (const char *(*)(void *))lookup(objc, "object_getClassName");
	if (!msgSend || !selRegister || !getClassFn || !classNameFn) {
		printf("REGEX-FAIL stage=runtime-symbols\n");
		printf("REGEX_VERDICT=RED\n");
		return 1;
	}

	void *string_class = lookup(foundation, "_OBJC_CLASS_$_NSString");
	void *regex_class = getClassFn("NSRegularExpression");
	if (!regex_class) {
		regex_class = lookup(foundation, "_OBJC_CLASS_$_NSRegularExpression");
	}
	if (!string_class || !regex_class) {
		printf("REGEX-FAIL stage=classes string=%p regex=%p\n", string_class, regex_class);
		printf("REGEX_VERDICT=RED\n");
		return 1;
	}
	printf("REGEX-CLASS regex=%s string=%s image-foundation=%p argv1=%s\n", classNameFn(regex_class),
	    classNameFn(string_class), foundation, argc > 1 ? argv[1] : "(none)");

	/* 1. The wall's own input: four groups, the optional one present and empty. */
	const struct expectation no_optional = {
	    .count = 5,
	    .ranges = {{0, 6}, {0, 2}, {2, 1}, {3, 3}, {6, 0}, {0, 0}, {0, 0}, {0, 0}},
	};
	check_one(regex_class, string_class, "no-optional", BUILD_PATTERN, BUILD_VERSION, &no_optional);

	/* 2. The same pattern with the optional group matching one character. */
	const struct expectation with_optional = {
	    .count = 5,
	    .ranges = {{0, 7}, {0, 2}, {2, 1}, {3, 3}, {6, 1}, {0, 0}, {0, 0}, {0, 0}},
	};
	check_one(regex_class, string_class, "with-optional", BUILD_PATTERN, BUILD_VERSION_LETTER, &with_optional);

	/* 3. A group that cannot participate at all. */
	const struct expectation unparticipating = {
	    .count = 3,
	    .ranges = {{0, 1}, {0, 1}, {NOT_FOUND, 0}, {0, 0}, {0, 0}, {0, 0}, {0, 0}, {0, 0}},
	};
	check_one(regex_class, string_class, "unparticipating", ALT_PATTERN, ALT_SUBJECT, &unparticipating);

	if (failures != 0) {
		printf("REGEX-FAIL failures=%d\n", failures);
		printf("REGEX_VERDICT=RED\n");
		return 1;
	}
	printf("REGEX_VERDICT=GREEN\n");
	return 0;
}

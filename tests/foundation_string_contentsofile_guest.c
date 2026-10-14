/*
 * foundation_string_contentsofile_guest.c -- permanent guest oracle for the ONE swift-mangled
 * Foundation overlay member Xcode 26.3's iOS plug-in demands and this runtime did not export:
 *
 *   _$sSS10FoundationE14contentsOfFileS2Sh_tKcfC
 *     = (extension in Foundation): Swift.String.init(contentsOfFile: __shared Swift.String)
 *       throws -> Swift.String
 *
 * WHY THIS EXISTS (measured, not inferred). After the MobileDevice.framework and liboah.dylib rungs
 * closed the IMAGE-not-found walls, both xcodebuild workloads stop in dyld one step later, on a
 * SYMBOL, in the plug-in scan:
 *
 *   Failed to load code for plug-in com.apple.dt.IDE.IDEiOSSupportCore ... (dlopen(
 *     .../Contents/PlugIns/IDEiOSSupportCore.framework/IDEiOSSupportCore, 265):
 *       Symbol not found: _$sSS10FoundationE14contentsOfFileS2Sh_tKcfC
 *       Referenced from: .../Contents/SharedFrameworks/XCTDaemonControlMobileDevice.framework/
 *                        Versions/A/XCTDaemonControlMobileDevice
 *       Expected in: /System/Library/Frameworks/Foundation.framework/Versions/C/Foundation)
 *
 * The referrer is XCTDaemonControlMobileDevice (x86_64 slice sha256
 * 72045dc52032c0baf8dd0c6906dc8f124c11888f1a76c3873f301a0ab97a02bf), reached from the plug-in
 * itself; it binds the name from dependency field 7 of its own import table (Foundation, strong,
 * eager), and Foundation.framework re-exports /usr/lib/swift/libswiftFoundation.dylib, so the
 * lookup is against that overlay's export trie. A whole-demand enumeration of the workload's load
 * closure (scripts/closure_analyze.py --cover, classified by state/swift-overlay-surface-rung/
 * classify_demand.py) reports exactly this ONE strong eager unmet swift import; the 83 other
 * Foundation-field swift binds of the same image resolve through the overlay (label `reexport`),
 * and its 125 CoreDevice binds are weak and bind to NULL.
 *
 * WHAT THIS MEASURES, in one guest process on the DEPLOYED runtime, with no compile-time reference
 * to Foundation at all (dlopen + dlsym + objc_msgSend only):
 *
 *   1. the demanded mangled name RESOLVES. The referrer's bind is two-level against Foundation
 *      (ordinal 7 of its import table) and Foundation.framework carries LC_REEXPORT_DYLIB for
 *      /usr/lib/swift/libswiftFoundation.dylib, so the searchable set is that overlay image; the
 *      fixture therefore asserts the name in the overlay image itself, under its own install name,
 *      and in the process-global table (RTLD_DEFAULT). This is the arm that is RED on a prefix
 *      deployed before the member existed: dlsym returns NULL for the demanded name and the loader
 *      would have stopped here, exactly as the workload's own dyld message says. (Darling's dlsym
 *      on the FOUNDATION handle does NOT search the LC_REEXPORT_DYLIB closure -- measured below and
 *      reported as an informational line, because a channel that cannot see the sibling cannot
 *      measure either member.);
 *   2. the CONTROL NAME _$sSS10FoundationE14contentsOfFile8encodingS2Sh_SSAAE8EncodingVtKcfC, the
 *      sibling the same image binds from the same field, is present in BOTH arms through the same
 *      channel -- so a RED arm proves the channel and the lookup are right and the difference is
 *      membership, not lookup;
 *   3. the DELEGATION TARGET's contract, because the member's Darwin body is a bridge and there is
 *      nothing else in it: Apple's own Swift-5.0 back-deployment overlay implements
 *      `init(contentsOfFile:)` as `NSString(contentsOfFile:path, usedEncoding: nil)` (disassembly:
 *      swift_bridgeObjectRetain, edx=0, call
 *      _$sSo8NSStringC14contentsOfFile12usedEncodingABSS_SpySuGSgtKcfCTf4xnn_n, then
 *      String(_cocoaString:)), and the in-tree swift-corelibs-foundation reference
 *      (Sources/Foundation/NSStringAPI.swift:369) has the same body. So this fixture exercises
 *      -[NSString initWithContentsOfFile:usedEncoding:error:] with a NULL usedEncoding on a
 *      UTF-8 file without a BOM, on a path that does not exist, and (reported separately, see the
 *      NOTE lines) on a UTF-16LE file with a BOM: that the primitive tolerates NULL, reads, and
 *      errors is what makes the member's one-line body truthful. The member itself cannot be
 *      CALLED from C (it is a Swift `__shared String` parameter with a throwing indirect-String
 *      return, and a hand-rolled calling convention would be a guess, not a measurement).
 *
 * FOUNDATION_STRING_CONTENTSOFFILE_VERDICT=GREEN is the machine-readable line the harness ok-marker
 * reads; every measured value is printed unconditionally as a STRINGCONTENTSOFFILE-* line so a RED
 * arm quotes the failure from the run's own output. Exit status is 0 on GREEN, 1 on RED.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the oracle is run against the prefix as
 * deployed, before and after the fix. See evidence/swift-overlay-surface-rung/README.md.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
/* The image that must answer the demand: Foundation.framework re-exports it, so a two-level bind
 * whose field is Foundation is searched here. Naming it directly is what makes the check possible
 * with dlsym, which does not follow LC_REEXPORT_DYLIB (measured: the FOUNDATION-handle line). */
#define OVERLAY_LIBRARY "/usr/lib/swift/libswiftFoundation.dylib"

/* The name XCTDaemonControlMobileDevice binds from its Foundation field (ordinal 7), strong+eager.
 * dyld's dlsym prepends the Mach-O leading underscore itself (the way `dlsym(h, "malloc")` finds
 * `_malloc`), so the lookup string is the mangled name WITHOUT it; the printed form keeps it. */
#define DEMANDED_SYMBOL "_$sSS10FoundationE14contentsOfFileS2Sh_tKcfC"
#define DEMANDED_LOOKUP "$sSS10FoundationE14contentsOfFileS2Sh_tKcfC"
/* The sibling the same image binds from the same field: present in both arms, so it is the control. */
#define CONTROL_SYMBOL "_$sSS10FoundationE14contentsOfFile8encodingS2Sh_SSAAE8EncodingVtKcfC"
#define CONTROL_LOOKUP "$sSS10FoundationE14contentsOfFile8encodingS2Sh_SSAAE8EncodingVtKcfC"

static int failures;

/* --- ObjC, reached by name only (no headers, no compile-time Foundation reference) ------------- */

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);

static void *clsNSString;

static int load_objc(void)
{
	void *lib = dlopen("/usr/lib/libobjc.A.dylib", RTLD_NOW | RTLD_GLOBAL);
	if (!lib) {
		printf("STRINGCONTENTSOFFILE-OBJC dlopen failed: %s\n", dlerror());
		return 0;
	}
	msgSend = (void *(*)(void *, void *, ...))dlsym(lib, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(lib, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(lib, "objc_getClass");
	if (!msgSend || !selRegister || !getClassFn) {
		printf("STRINGCONTENTSOFFILE-OBJC missing objc_msgSend/sel_registerName/objc_getClass\n");
		return 0;
	}
	clsNSString = getClassFn("NSString");
	return clsNSString != NULL;
}

/* -[NSString initWithContentsOfFile:usedEncoding:error:] on a freshly allocated instance.
 * The usedEncoding argument is deliberately NULL: that is the argument Apple's own implementation of
 * the demanded member passes. */
static void *string_from_file_used_encoding(const char *path, void *error_out)
{
	union {
		void *(*p)(void *, void *, ...);
		void *(*f)(void *, void *, void *, void *, void *);
	} u;
	u.p = msgSend;
	void *alloc = ((void *(*)(void *, void *))msgSend)(clsNSString, selRegister("alloc"));
	return u.f(alloc, selRegister("initWithContentsOfFile:usedEncoding:error:"),
		   ((void *(*)(void *, void *, const char *))msgSend)(clsNSString,
			selRegister("stringWithUTF8String:"), path), NULL, error_out);
}

static const char *utf8_of(void *nsstring)
{
	if (!nsstring)
		return "(nil)";
	return (const char *)((void *(*)(void *, void *))msgSend)(nsstring, selRegister("UTF8String"));
}

static void check(const char *what, const char *expected, const char *actual)
{
	int ok = actual != NULL && strcmp(expected, actual) == 0;
	if (!ok)
		failures++;
	printf("STRINGCONTENTSOFFILE-CHECK %-34s %-9s expected=%s actual=%s\n", what,
	       ok ? "ok" : "MISMATCH", expected, actual ? actual : "(null)");
}

/* Writes a file and returns its path, or NULL.  Two encodings of the same text: UTF-8 with no BOM
 * (which the detector must conclude is UTF-8) and UTF-16LE with a BOM (which the BOM decides). */
static const char *write_fixture(const char *path, const char *text, int utf16le)
{
	FILE *f = fopen(path, "wb");
	if (!f)
		return NULL;
	if (utf16le) {
		unsigned char bom[2] = { 0xFF, 0xFE };
		fwrite(bom, 1, 2, f);
		for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
			unsigned char pair[2] = { *p, 0x00 };
			fwrite(pair, 1, 2, f);
		}
	} else {
		fwrite(text, 1, strlen(text), f);
	}
	fclose(f);
	return path;
}

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	const char *text = "darling-overlay";
	char path8[] = "/private/tmp/contentsoffile-utf8-XXXXXX";
	char path16[] = "/private/tmp/contentsoffile-utf16-XXXXXX";

	/* --- 1. the name in the image that must answer it, and in the process-global table --------- */
	void *foundation = dlopen(FOUNDATION_LIBRARY, RTLD_LAZY | RTLD_GLOBAL);
	printf("STRINGCONTENTSOFFILE-DLOPEN %s handle=%p error=%s\n", FOUNDATION_LIBRARY, foundation,
	       foundation ? "-" : dlerror());
	if (!foundation) {
		printf("STRINGCONTENTSOFFILE-DECISION Foundation.framework itself does not load on this "
		       "deployed runtime, so the demand cannot be measured here\n");
		printf("FOUNDATION_STRING_CONTENTSOFFILE_VERDICT=RED\n");
		return 1;
	}

	void *overlay = dlopen(OVERLAY_LIBRARY, RTLD_LAZY | RTLD_GLOBAL);
	printf("STRINGCONTENTSOFFILE-DLOPEN %s handle=%p error=%s\n", OVERLAY_LIBRARY, overlay,
	       overlay ? "-" : dlerror());
	if (!overlay) {
		printf("STRINGCONTENTSOFFILE-DECISION the overlay Foundation re-exports does not load, so "
		       "the demand cannot be measured here\n");
		printf("FOUNDATION_STRING_CONTENTSOFFILE_VERDICT=RED\n");
		return 1;
	}

	/* The overlay image under its own install name, and the process-global table: the referrer's
	 * Foundation field reaches the overlay through the framework's LC_REEXPORT_DYLIB, and these
	 * are the two further views of the same lookup. */
	void *ch_demanded = dlsym(overlay, DEMANDED_LOOKUP);
	void *ch_control = dlsym(overlay, CONTROL_LOOKUP);
	void *flat_demanded = dlsym(RTLD_DEFAULT, DEMANDED_LOOKUP);
	void *flat_control = dlsym(RTLD_DEFAULT, CONTROL_LOOKUP);

	/* The Foundation handle: this is the field the referrer's bind names (ordinal 7), so this is
	 * the channel the wall is about. Measured here: a name Foundation re-exports IS found through
	 * its handle (the CONTROL line below), so the channel is usable. */
	void *fh_demanded = dlsym(foundation, DEMANDED_LOOKUP);
	void *fh_control = dlsym(foundation, CONTROL_LOOKUP);
	printf("STRINGCONTENTSOFFILE-DLSYM Foundation-handle  %-58s %s (%p)\n", CONTROL_SYMBOL,
	       fh_control ? "present" : "ABSENT", fh_control);
	printf("STRINGCONTENTSOFFILE-DLSYM Foundation-handle  %-58s %s (%p)\n", DEMANDED_SYMBOL,
	       fh_demanded ? "present" : "ABSENT", fh_demanded);
	printf("STRINGCONTENTSOFFILE-DLSYM %-18s %-58s %s (%p)\n", OVERLAY_LIBRARY, CONTROL_SYMBOL,
	       ch_control ? "present" : "ABSENT", ch_control);
	printf("STRINGCONTENTSOFFILE-DLSYM RTLD_DEFAULT      %-58s %s (%p)\n", CONTROL_SYMBOL,
	       flat_control ? "present" : "ABSENT", flat_control);
	printf("STRINGCONTENTSOFFILE-DLSYM %-18s %-58s %s (%p)\n", OVERLAY_LIBRARY, DEMANDED_SYMBOL,
	       ch_demanded ? "present" : "ABSENT", ch_demanded);
	printf("STRINGCONTENTSOFFILE-DLSYM RTLD_DEFAULT      %-58s %s (%p)\n", DEMANDED_SYMBOL,
	       flat_demanded ? "present" : "ABSENT", flat_demanded);

	/* The control defines which channels are usable: a channel that cannot see a member the
	 * referrer binds from the same field cannot measure either member. */
	struct { const char *name; void *demanded; void *control; } channels[] = {
		{ FOUNDATION_LIBRARY " (the referrer's field)", fh_demanded, fh_control },
		{ OVERLAY_LIBRARY, ch_demanded, ch_control },
		{ "RTLD_DEFAULT", flat_demanded, flat_control },
	};
	unsigned int usable = 0;
	for (unsigned long i = 0; i < sizeof(channels) / sizeof(channels[0]); i++) {
		if (!channels[i].control)
			continue;
		usable++;
		if (!channels[i].demanded) {
			failures++;
			printf("STRINGCONTENTSOFFILE-DECISION channel %s sees the control name but not the "
			       "demanded one, so dlopen of XCTDaemonControlMobileDevice -- and of any image "
			       "binding it -- stops with \"Symbol not found: %s\"\n", channels[i].name,
			       DEMANDED_SYMBOL);
		}
	}
	if (usable == 0) {
		failures++;
		printf("STRINGCONTENTSOFFILE-DECISION no channel sees the control name, so this fixture is "
		       "not looking where the referrer looks and its verdict means nothing\n");
	}
	printf("STRINGCONTENTSOFFILE-CHANNELS usable=%u of %lu\n", usable,
	       (unsigned long)(sizeof(channels) / sizeof(channels[0])));

	if (ch_control) {
		Dl_info info;
		if (dladdr(ch_control, &info) && info.dli_fname)
			printf("STRINGCONTENTSOFFILE-IMAGE control provided_by=%s\n", info.dli_fname);
	}

	/* --- 2. the delegation target the member's body uses, with a NULL usedEncoding ------------- */
	if (!load_objc()) {
		failures++;
		printf("STRINGCONTENTSOFFILE-DECISION the delegation target cannot be measured (no ObjC "
		       "runtime reached by name)\n");
	} else {
		int fd8 = mkstemp(path8);
		int fd16 = mkstemp(path16);
		if (fd8 >= 0)
			close(fd8);
		if (fd16 >= 0)
			close(fd16);
		if (!write_fixture(path8, text, 0) || !write_fixture(path16, text, 1)) {
			failures++;
			printf("STRINGCONTENTSOFFILE-DECISION could not write the fixtures under /private/tmp\n");
		} else {
			void *error = NULL;
			void *s8 = string_from_file_used_encoding(path8, &error);
			printf("STRINGCONTENTSOFFILE-READ utf8-no-bom file=%s result=%s error=%p\n", path8,
			       s8 ? utf8_of(s8) : "(nil)", error);
			check("utf8-no-bom", text, s8 ? utf8_of(s8) : NULL);

			/* NOT a gate, and why: this is the read path's own BOM detection, not this member's
			 * surface. src/external/foundation/src/NSBOMEncoding.m:19-45 compares the
			 * first four bytes against constants assembled as if the second BOM byte were the
			 * LESS significant one (0xFFFE0000 for a little-endian UTF-16 BOM), while
			 * NSString.m:845 memcpys them into a uint32_t on the host's own byte order, so on
			 * this x86_64 runtime no UTF-16 BOM matches and ICU detection decides instead.
			 * Apple's implementation of the demanded member delegates here too, so the member
			 * inherits exactly this; it is reported as its own finding and is NOT introduced,
			 * changed or claimed fixed by this rung. */
			error = NULL;
			void *s16 = string_from_file_used_encoding(path16, &error);
			printf("STRINGCONTENTSOFFILE-NOTE utf16le-bom file=%s result=%s error=%p\n", path16,
			       s16 ? utf8_of(s16) : "(nil)", error);
			printf("STRINGCONTENTSOFFILE-SEPARATE-FINDING utf16le-bom-decoded-as-requested=%s "
			       "(BOM table in src/external/foundation/src/NSBOMEncoding.m, not this overlay)\n",
			       (s16 && strcmp(utf8_of(s16), text) == 0) ? "yes" : "no");

			/* The error path: a path that does not exist must produce nil + an error. */
			error = NULL;
			void *missing = string_from_file_used_encoding("/private/tmp/does-not-exist-"
								      "contentsoffile", &error);
			int ok = missing == NULL && error != NULL;
			if (!ok)
				failures++;
			printf("STRINGCONTENTSOFFILE-CHECK %-34s %-9s result=%p error=%p\n",
			       "missing-file-nil-and-error", ok ? "ok" : "MISMATCH", missing, error);
		}
		unlink(path8);
		unlink(path16);
	}

	printf("STRINGCONTENTSOFFILE-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("FOUNDATION_STRING_CONTENTSOFFILE_VERDICT=GREEN\n");
	else
		printf("FOUNDATION_STRING_CONTENTSOFFILE_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

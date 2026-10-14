/*
 * coresimulator_load_guest.c -- permanent guest oracle for
 *
 *   /Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/CoreSimulator
 *
 * WHY THIS EXISTS. With MobileDevice deployed, Xcode 26.3's initial plug-in scan advanced to the
 * next image of the same walk and stopped on Xcode's own
 * Contents/Frameworks/IDESimulatorFoundation.framework/Versions/A/IDESimulatorFoundation
 * (sha256 83db461982c7f9846adf4a160baaa6b6d73925852a3d3f2429d6756b84873c0f, 1,072,192 bytes):
 *
 *   DVTPlugInLoading: Failed to load code for plug-in com.apple.dt.IDESimulatorFoundation ...
 *     (dlopen_preflight(.../IDESimulatorFoundation): Library not loaded:
 *       /Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/CoreSimulator
 *       Referenced from: .../Versions/A/IDESimulatorFoundation   Reason: image not found)
 *
 * The load command is STRONG (LC_LOAD_DYLIB at compatibility 1.0.0 / current 1051.17.8) and the
 * referrer binds EIGHT strong EAGER names from it -- six ObjC class refs and two C functions, read
 * from the plug-in's own chained-fixup import table -- so a file that merely exists would move the
 * failure from "image not found" to "symbol not found". The eight are asserted below.
 *
 * THE VERSION QUESTION, measured rather than assumed: dyld compares the compatibility version the
 * demanding load command carries against the compatibility version the PROVIDER declares in its
 * LC_ID_DYLIB. The demand is compatibility 1.0.0; the built image declares compatibility 1.0.0
 * (current 885.2.0). The demanded "current version" 1051.17.8 is informational. Measured with
 * `llvm-otool -l` on the built artifact and on the referrer.
 *
 * WHAT THIS MEASURES, in one guest process on the deployed runtime, with no compile-time reference
 * to the image at all (dlopen + dlsym + the ObjC runtime only):
 *
 *   1. the demanded install name resolves and the image LOADS (RTLD_NOW, so the image's OWN
 *      undefined symbols -- Foundation, AppKit, libobjc, libSystem -- would fail here too). This is
 *      the deployment property the CoreSimulator rung's declaration closes, and the one the loader
 *      checks before it may even look at names;
 *   2. every demanded name the referrer binds is exported by the loaded image. The eighth,
 *      _OBJC_CLASS_$_SimUsageTracker, is now implemented in this tree (src/private-frameworks/
 *      CoreSimulator/src/SimUsageTracker.m, whose header records the measurement of the contract
 *      from Apple's own CoreSimulator in Xcode 26.3's XcodeSystemResources.pkg and from this
 *      plug-in's two selector references), so this fixture asserts it instead of reporting it as a
 *      gap -- it was the previous arm's CORESIMULATOR-EXPORT-GAP line;
 *   3. the class answers the ONE call the plug-in makes, with the argument the plug-in builds. The
 *      plug-in's Swift method IDESimulatorAvailabilityProvider.lastUsageDate(forPlatform:architecture:)
 *      (disassembled at 0x2bd90) builds "platform.architecture", bridges it to one NSString, sends
 *      +sharedTrackerWithIdentifier: to the class, and sends -lastUsage to the result, whose NSDate
 *      it passes to Foundation.Date._unconditionallyBridgeFromObjectiveC -- a NON-optional bridge,
 *      so a nil answer would trap. Measured here: the class and the instance answer, the result is a
 *      date, and (informational) it is what an empty usage store means, [NSDate distantPast];
 *   4. which file the runtime loaded for that install name, informationally.
 *
 * CORESIMULATOR_VERDICT=GREEN is the machine-readable line the harness ok-marker reads; every
 * measured value is printed unconditionally as a CORESIMULATOR-* line so a RED arm quotes the
 * failure from the run's own output. Exit status is 0 on GREEN, 1 on RED.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: this oracle is run against the prefix as
 * deployed, before and after the fix.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

#define CORESIMULATOR_IMAGE \
	"/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/CoreSimulator"

/* The names Xcode's IDESimulatorFoundation binds from CoreSimulator, in the order its chained-fixup
 * import table carries them (dependency field: CoreSimulator; weak flag clear; eager). The leading
 * underscore of the Mach-O symbol is stripped, which is how dlsym names it. */
static const char *const bound_symbols[] = {
	"OBJC_CLASS_$_SimDiskImage",
	"OBJC_CLASS_$_SimDiskImageIdentifier",
	"OBJC_CLASS_$_SimDiskImageManager",
	"OBJC_CLASS_$_SimRuntime",
	"OBJC_CLASS_$_SimServiceContext",
	"OBJC_CLASS_$_SimUsageTracker",
	"SimDiskImageStateDisplayString",
	"SimDiskImageStateIsTerminal",
};

#define BOUND_SYMBOL_COUNT (sizeof(bound_symbols) / sizeof(bound_symbols[0]))

static int failures;

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);
static void *clsNSString;
static void *clsNSDate;

static int load_objc(void)
{
	void *lib = dlopen("/usr/lib/libobjc.A.dylib", RTLD_NOW | RTLD_GLOBAL);
	if (!lib) {
		printf("CORESIMULATOR-OBJC dlopen failed: %s\n", dlerror());
		return 0;
	}
	msgSend = (void *(*)(void *, void *, ...))dlsym(lib, "objc_msgSend");
	selRegister = (void *(*)(const char *))dlsym(lib, "sel_registerName");
	getClassFn = (void *(*)(const char *))dlsym(lib, "objc_getClass");
	if (!msgSend || !selRegister || !getClassFn) {
		printf("CORESIMULATOR-OBJC missing objc_msgSend/sel_registerName/objc_getClass\n");
		return 0;
	}
	clsNSString = getClassFn("NSString");
	clsNSDate = getClassFn("NSDate");
	return clsNSString != NULL && clsNSDate != NULL;
}

static int is_kind_of_class(void *object, void *cls)
{
	int (*bool_msgSend)(void *, void *, void *) = (int (*)(void *, void *, void *))msgSend;
	return bool_msgSend(object, selRegister("isKindOfClass:"), cls) != 0;
}

static void *nsstring_with_utf8(const char *text)
{
	void *(*string_msgSend)(void *, void *, const char *) =
		(void *(*)(void *, void *, const char *))msgSend;
	return string_msgSend(clsNSString, selRegister("stringWithUTF8String:"), text);
}

static const char *utf8_of(void *nsstring)
{
	if (!nsstring)
		return "(nil)";
	return (const char *)((void *(*)(void *, void *))msgSend)(nsstring,
		selRegister("UTF8String"));
}

/* 3. the one call the plug-in makes, with the argument it builds: the class object, one NSString
 *    ("<platform>.<architecture>"), and then -lastUsage on the tracked instance. The identifier
 *    below is the shape the plug-in's own disassembly builds (String.append of "." then the
 *    architecture), not a value chosen here. */
static void check_usage_tracker(void *image)
{
	void *trackerClass = dlsym(image, "OBJC_CLASS_$_SimUsageTracker");
	char *const identifier = "iOS.x86_64";

	printf("CORESIMULATOR-OC-CLASS SimUsageTracker class=%p\n", trackerClass);
	if (!trackerClass) {
		failures++;
		printf("CORESIMULATOR-DECISION the class object the plug-in binds is absent from the "
		       "loaded image, so the loader reports \"Symbol not found: "
		       "OBJC_CLASS_$_SimUsageTracker\" once the two missing images above it exist\n");
		return;
	}
	if (!load_objc()) {
		failures++;
		printf("CORESIMULATOR-DECISION the ObjC runtime is not usable, so the class's contract "
		       "cannot be exercised here\n");
		return;
	}

	void *identifierString = nsstring_with_utf8(identifier);
	void *(*class_msgSend)(void *, void *, void *) = (void *(*)(void *, void *, void *))msgSend;
	void *tracker = class_msgSend(trackerClass, selRegister("sharedTrackerWithIdentifier:"),
				      identifierString);
	int tracker_ok = tracker != NULL && is_kind_of_class(tracker, trackerClass);
	if (!tracker_ok)
		failures++;
	printf("CORESIMULATOR-OC-SHARED %-34s %-9s tracker=%p identifier=%s\n",
	       "+sharedTrackerWithIdentifier:", tracker_ok ? "ok" : "MISMATCH", tracker, identifier);

	if (!tracker_ok) {
		printf("CORESIMULATOR-DECISION the factory the plug-in's only call site sends did not "
		       "answer an instance, so the demand this rung closes is not met\n");
		msgSend(trackerClass, selRegister("release"));
		return;
	}

	void *returnedIdentifier = msgSend(tracker, selRegister("identifier"));
	void *key = msgSend(tracker, selRegister("key"));
	printf("CORESIMULATOR-OC-TRACKER identifier=%s key=%s\n", utf8_of(returnedIdentifier),
	       utf8_of(key));

	void *lastUsage = msgSend(tracker, selRegister("lastUsage"));
	int date_ok = lastUsage != NULL && is_kind_of_class(lastUsage, clsNSDate);
	if (!date_ok)
		failures++;
	printf("CORESIMULATOR-OC-LASTUSAGE %-34s %-9s date=%p (the plug-in bridges this through a "
	       "NON-optional Foundation.Date, so nil would trap)\n", "-lastUsage",
	       date_ok ? "ok" : "MISMATCH", lastUsage);

	if (date_ok) {
		/* Informational: nothing in this guest records simulator usage, so an empty store is
		 * what -lastUsage must report, and [NSDate distantPast] is Apple's own empty-store
		 * answer (measured from Apple's own CoreSimulator; see src/SimUsageTracker.m). */
		void *distantPast = msgSend(clsNSDate, selRegister("distantPast"));
		int (*bool3)(void *, void *, void *) = (int (*)(void *, void *, void *))msgSend;
		int same = bool3(lastUsage, selRegister("isEqualToDate:"), distantPast) != 0;
		printf("CORESIMULATOR-OC-EMPTYSTORE equals-distantPast=%s\n", same ? "yes" : "no");
	}

	msgSend(tracker, selRegister("release"));
}

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	unsigned int present = 0;
	unsigned long i;

	/* 1. the install name Xcode's load command carries */
	void *image = dlopen(CORESIMULATOR_IMAGE, RTLD_NOW | RTLD_GLOBAL);
	const char *dlopen_error = image ? "-" : dlerror();
	printf("CORESIMULATOR-DLOPEN %s handle=%p error=%s\n", CORESIMULATOR_IMAGE, image,
	       dlopen_error ? dlopen_error : "(no dlerror)");
	if (!image) {
		failures++;
		printf("CORESIMULATOR-DECISION no image answers %s on this deployed runtime, so Xcode's "
		       "IDESimulatorFoundation fails to load (\"image not found\") and the plug-in scan "
		       "aborts on it\n", CORESIMULATOR_IMAGE);
		printf("CORESIMULATOR_VERDICT=RED\n");
		return 1;
	}

	/* 2. every name the measured referrer binds, strong and eager */
	for (i = 0; i < BOUND_SYMBOL_COUNT; i++) {
		void *symbol = dlsym(image, bound_symbols[i]);
		char underscored[128];
		snprintf(underscored, sizeof(underscored), "_%s", bound_symbols[i]);
		if (symbol)
			present++;
		else
			failures++;
		printf("CORESIMULATOR-DLSYM %-48s %s (%p)\n", underscored,
		       symbol ? "present" : "ABSENT", symbol);
	}

	/* 3. the class's measured contract, exercised */
	check_usage_tracker(image);

	/* 4. informational: which file the runtime loaded for the install name */
	void *(*imageNameFn)(unsigned int) =
		(void *(*)(unsigned int))dlsym(RTLD_DEFAULT, "_dyld_get_image_name");
	void *(*imageCountFn)(void) = (void *(*)(void))dlsym(RTLD_DEFAULT, "_dyld_image_count");
	if (imageNameFn && imageCountFn) {
		unsigned int count = (unsigned int)(unsigned long)imageCountFn();
		unsigned int index;
		for (index = 0; index < count; index++) {
			const char *name = (const char *)imageNameFn(index);
			if (name && strstr(name, "CoreSimulator"))
				printf("CORESIMULATOR-IMAGE loaded_as=%s index=%u\n", name, index);
		}
	}

	printf("CORESIMULATOR-EXPORTS %u of %lu\n", present, (unsigned long)BOUND_SYMBOL_COUNT);
	printf("CORESIMULATOR-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("CORESIMULATOR_VERDICT=GREEN\n");
	else
		printf("CORESIMULATOR_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

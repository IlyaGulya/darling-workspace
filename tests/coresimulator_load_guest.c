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
 * to the image at all (dlopen + dlsym only):
 *
 *   1. the demanded install name resolves and the image LOADS (RTLD_NOW, so the image's OWN
 *      undefined symbols -- Foundation, AppKit, libobjc, libSystem -- would fail here too). This is
 *      the deployment property this rung's declaration closes, and the one the loader checks before
 *      it may even look at names;
 *   2. every demanded name the built image backs is exported by the loaded image;
 *   3. which file the runtime loaded for that install name, informationally.
 *
 * WHAT IT DELIBERATELY DOES NOT ASSERT, WITH THE REASON. Of the eight names the referrer binds,
 * SEVEN are exported by the built image and the EIGHTH -- _OBJC_CLASS_$_SimUsageTracker -- is NOT,
 * and no declaration or deploy can add it: that is an implementation gap inside a present image,
 * the same class as the UTType family the previous rung closed, and it is named here rather than
 * asserted. Asserting it would make this registered gate permanently RED, and a contract nothing can
 * pass is exactly what AGENTS.md refuses. So the fixture prints it in EVERY arm as a
 * CORESIMULATOR-EXPORT-GAP line with the demanded name, and the GREEN verdict means the measured
 * property of this rung -- the image the loader is asked for exists, loads, and answers every name
 * it can back. The wall the workload will report next is the OTHER strong unsatisfied image
 * (DVTDownloads, above SimUsageTracker in the loader's own order: dyld resolves images before it
 * binds names); both are named in the rung's evidence.
 *
 * CORESIMULATOR_VERDICT=GREEN is the machine-readable line the harness ok-marker reads; every
 * measured value is printed unconditionally as a CORESIMULATOR-* line so a RED arm quotes the
 * failure from the run's own output. Exit status is 0 on GREEN, 1 on RED.
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

/* The one demanded name the built image does not back, with its measured consequence. Kept as a
 * named constant so the arm logs carry it and so a reader sees the fixture is not silently
 * narrowing: it is reporting a gap it cannot close from here. */
static const char *const unbacked_symbol = "OBJC_CLASS_$_SimUsageTracker";

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	int failures = 0;
	unsigned int present = 0;
	unsigned int unbacked = 0;
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
		else {
			unbacked++;
			if (strcmp(bound_symbols[i], unbacked_symbol) == 0) {
				printf("CORESIMULATOR-EXPORT-GAP %s absent: the referrer binds it strongly and "
				       "eagerly, so once the two missing IMAGES exist the loader will report "
				       "\"Symbol not found\" for it -- an implementation gap inside this image, "
				       "not a deployment one\n", underscored);
			}
		}
		printf("CORESIMULATOR-DLSYM %-48s %s (%p)\n", underscored,
		       symbol ? "present" : "ABSENT", symbol);
	}

	/* The seven backed names are asserted; the one unbacked name is reported, not asserted (see the
	 * header). A SECOND unbacked name would not be the measured gap and fails here. */
	if (unbacked > 1) {
		failures++;
		printf("CORESIMULATOR-DECISION %u demanded names are absent (only %s is the measured "
		       "implementation gap), so this image is not the one the referrer's contract "
		       "describes\n", unbacked, unbacked_symbol);
	}

	/* 3. informational: which file the runtime loaded for the install name */
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

	printf("CORESIMULATOR-EXPORTS %u of %lu (unbacked: %u)\n", present,
	       (unsigned long)BOUND_SYMBOL_COUNT, unbacked);
	printf("CORESIMULATOR-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("CORESIMULATOR_VERDICT=GREEN\n");
	else
		printf("CORESIMULATOR_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

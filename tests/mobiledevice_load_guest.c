/*
 * mobiledevice_load_guest.c -- permanent guest oracle for
 *
 *   /System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice
 *
 * WHY THIS EXISTS. Xcode 26.3's initial plug-in scan cannot finish without this image, and neither
 * xcodebuild workload clears it without the same plug-in. Contents/PlugIns/IDEiOSSupportCore.framework/
 * Versions/A/IDEiOSSupportCore (sha256 3d941fe8f7fec44db944850aebe67d93a3a389559b7794162ea450c33b78fd5e)
 * carries a STRONG, non-weak LC_LOAD_DYLIB for exactly the path above at compatibility 1.0.0 / current
 * 1818.60.2, so dlopen_preflight refuses the whole plug-in:
 *
 *   Failed to load code for plug-in com.apple.dt.IDE.IDEiOSSupportCore ... (dlopen_preflight(
 *   .../IDEiOSSupportCore): Library not loaded:
 *     /System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice
 *     Referenced from: .../Versions/A/IDEiOSSupportCore   Reason: image not found)
 *
 * and xcodebuild then dies in its own error path. The image is not merely referenced: that same
 * referrer binds TWELVE strong EAGER names from it (read from the plug-in's own import table, whose
 * dependency field for them is the MobileDevice ordinal, 17 of 86), so a file that merely exists would
 * move the failure from "image not found" to "symbol not found". The twelve are asserted below.
 *
 * This is the third member of one class -- an image THIS TREE BUILDS, at a guest path no deploy
 * provider declared, discovered one guest abort at a time (icudt66l.dat, libobjc-trampolines.dylib,
 * now this). The oracle belongs to the demand, not to the fix: it names the install name Xcode's load
 * command carries and the names its import table binds, so it fails for the right reason in an arm
 * whose prefix was filled before the declaration existed.
 *
 * WHAT THIS MEASURES, in one guest process on the deployed runtime, with no compile-time reference to
 * the image at all (dlopen + dlsym only):
 *
 *   1. the demanded install name resolves and the image LOADS (RTLD_NOW, so the image's OWN undefined
 *      symbols -- Foundation, CoreFoundation, libobjc, libSystem -- would fail here too);
 *   2. every one of the twelve names the measured referrer binds is exported by the loaded image;
 *   3. informationally, which file the runtime loaded for that install name.
 *
 * It deliberately does NOT name the Xcode plug-in path or any host path: the contract the plug-in needs
 * is the image's install name plus those twelve symbols, and the end-to-end measurement of the plug-in
 * scan is the workload run, not a fixture that hardcodes an Xcode location.
 *
 * MOBILEDEVICE_VERDICT=GREEN is the machine-readable line the harness ok-marker reads; every measured
 * value is printed unconditionally as a MOBILEDEVICE-* line so a RED arm quotes the failure from the
 * run's own output. Exit status is 0 on GREEN, 1 on RED.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

#define MOBILEDEVICE_IMAGE \
	"/System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice"

/* The names Xcode's IDEiOSSupportCore binds from MobileDevice, one per entry, measured from that
 * image's own import table (dependency field: MobileDevice, weak flag clear, eager set). */
static const char *const bound_symbols[] = {
	"AMDGetBundleVersion",
	"AMDServiceConnectionGetSocket",
	"AMDServiceConnectionGetTypeID",
	"AMDServiceConnectionInvalidate",
	"AMDServiceConnectionReceive",
	"AMDServiceConnectionSend",
	"AMSCleanup",
	"AMSConnectToCrashReportCopyTarget",
	"AMSCopyCrashReportsFromTarget",
	"AMSDisconnectFromCrashReportCopyTarget",
	"AMSInitialize",
	"SZArchiverCreateStreamableZip",
};

#define BOUND_SYMBOL_COUNT (sizeof(bound_symbols) / sizeof(bound_symbols[0]))

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	int failures = 0;
	unsigned long i;

	/* 1. the install name Xcode's load command carries */
	void *image = dlopen(MOBILEDEVICE_IMAGE, RTLD_NOW | RTLD_GLOBAL);
	const char *dlopen_error = image ? "-" : dlerror();
	printf("MOBILEDEVICE-DLOPEN %s handle=%p error=%s\n", MOBILEDEVICE_IMAGE, image,
	       dlopen_error ? dlopen_error : "(no dlerror)");
	if (!image) {
		failures++;
		printf("MOBILEDEVICE-DECISION no image answers %s on this deployed runtime, so every image "
		       "whose load command names it (Xcode's IDEiOSSupportCore among them) fails to load and "
		       "the initial plug-in scan aborts\n", MOBILEDEVICE_IMAGE);
		printf("MOBILEDEVICE_VERDICT=RED\n");
		return 1;
	}

	/* 2. the twelve names the measured referrer binds, all strong and eager */
	unsigned int present = 0;
	for (i = 0; i < BOUND_SYMBOL_COUNT; i++) {
		char underscored[128];
		snprintf(underscored, sizeof(underscored), "_%s", bound_symbols[i]);
		void *symbol = dlsym(image, bound_symbols[i]);
		if (symbol)
			present++;
		printf("MOBILEDEVICE-DLSYM %-42s %s (%p)\n", underscored,
		       symbol ? "present" : "ABSENT", symbol);
	}
	if (present != BOUND_SYMBOL_COUNT) {
		failures++;
		printf("MOBILEDEVICE-DECISION the image loaded but exports %u of %lu bound names, so "
		       "IDEiOSSupportCore would now fail with \"Symbol not found\" instead of \"image not "
		       "found\"\n", present, (unsigned long)BOUND_SYMBOL_COUNT);
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
			if (name && strstr(name, "MobileDevice"))
				printf("MOBILEDEVICE-IMAGE loaded_as=%s index=%u\n", name, index);
		}
	}

	printf("MOBILEDEVICE-BOUND-%s %u of %lu\n",
	       present == BOUND_SYMBOL_COUNT ? "ALL" : "MISSING", present,
	       (unsigned long)BOUND_SYMBOL_COUNT);
	printf("MOBILEDEVICE-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("MOBILEDEVICE_VERDICT=GREEN\n");
	else
		printf("MOBILEDEVICE_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

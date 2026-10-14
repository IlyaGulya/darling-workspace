/*
 * liboah_translated_predicate_guest.c -- permanent guest oracle for
 *
 *   /usr/lib/liboah.dylib : bool oah_is_current_process_translated(void)
 *
 * WHY THIS EXISTS. Xcode 26.3's initial plug-in scan cannot finish without this image. Its iOS
 * platform plug-in, Contents/PlugIns/IDEiOSSupportCore.framework/Versions/A/IDEiOSSupportCore, has a
 * STRONG LC_LOAD_DYLIB for /usr/lib/liboah.dylib, so dlopen_preflight refuses the whole plug-in:
 *
 *   Failed to load code for plug-in com.apple.dt.IDE.IDEiOSSupportCore ... (dlopen_preflight(
 *   .../IDEiOSSupportCore): Library not loaded: /usr/lib/liboah.dylib
 *     Referenced from: .../Versions/A/IDEiOSSupportCore   Reason: image not found)
 *
 * and xcodebuild then dies in its own error path. The same install name is hard-linked, and imported
 * from, by Contents/PlugIns/IDEInterfaceBuilderCocoaTouchIntegration.framework/Versions/A/
 * IDEInterfaceBuilderCocoaTouchIntegration, which binds exactly ONE symbol from it --
 * _oah_is_current_process_translated -- so the image cannot be a file that merely exists.
 *
 * WHAT THIS MEASURES, in one guest process on the deployed runtime, with no compile-time reference to
 * the image at all (dlopen + dlsym only):
 *
 *   1. the install name resolves and the image LOADS (dlopen with RTLD_NOW, so its own undefined
 *      symbols would fail here too);
 *   2. the ONE symbol a measured Xcode image binds is exported and resolvable;
 *   3. calling it answers "not translated" -- the true answer for an x86_64 guest running natively
 *      on an x86_64 host, and the answer whose ABI the caller proves by testing the low BYTE of the
 *      result (IDEInterfaceBuilderCocoaTouchIntegration: `callq _oah_is_current_process_translated;
 *      testb %al, %al; je ...`), which is why the return type is bool and not a wider integer;
 *   4. informationally, which file the runtime loaded for that install name.
 *
 * It deliberately does NOT name the Xcode plug-in path or any host path: the contract the plug-in
 * needs is the image's install name plus that one symbol, and the end-to-end measurement of the
 * plug-in scan is the workload run, not a fixture that hardcodes an Xcode location.
 *
 * LIBOAH_VERDICT=GREEN is the machine-readable line the harness ok-marker reads; every measured value
 * is printed unconditionally as a LIBOAH-* line so a RED arm quotes the failure from the run's own
 * output. Exit status is 0 on GREEN, 1 on RED.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define OAH_LIBRARY "/usr/lib/liboah.dylib"
#define OAH_SYMBOL "oah_is_current_process_translated"

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	int failures = 0;

	/* 1. the install name Xcode's load commands carry */
	void *image = dlopen(OAH_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	const char *dlopen_error = image ? "-" : dlerror();
	printf("LIBOAH-DLOPEN %s handle=%p error=%s\n", OAH_LIBRARY, image,
	       dlopen_error ? dlopen_error : "(no dlerror)");
	if (!image) {
		failures++;
		printf("LIBOAH-DECISION no image answers %s on this deployed runtime, so every image whose "
		       "load command names it (Xcode's IDEiOSSupportCore among them) fails to load\n",
		       OAH_LIBRARY);
		printf("LIBOAH_VERDICT=RED\n");
		return 1;
	}

	/* 2. the one symbol a measured Xcode image binds from it */
	union {
		void *p;
		unsigned char (*f)(void);
	} oah;
	oah.p = dlsym(image, OAH_SYMBOL);
	printf("LIBOAH-DLSYM %s -> %s (%p)\n", OAH_SYMBOL, oah.p ? "present" : "ABSENT", oah.p);
	if (!oah.p) {
		failures++;
		printf("LIBOAH-DECISION the image loaded but does not export %s, so "
		       "IDEInterfaceBuilderCocoaTouchIntegration would now fail with \"Symbol not found\" "
		       "instead of \"image not found\"\n", OAH_SYMBOL);
		printf("LIBOAH_VERDICT=RED\n");
		return 1;
	}

	/* 3. the answer, read the width the caller reads (testb %al, %al) */
	unsigned char translated = oah.f();
	printf("LIBOAH-VALUE %s()=%u\n", OAH_SYMBOL, (unsigned)translated);
	if (translated != 0) {
		failures++;
		printf("LIBOAH-CHECK %-30s MISMATCH an x86_64 guest runs natively here, so it is not a "
		       "translated process\n", "not_translated");
	} else {
		printf("LIBOAH-CHECK %-30s ok\n", "not_translated");
	}

	/* 4. informational: which file the runtime loaded for the install name */
	void *(*imageNameFn)(unsigned int) =
		(void *(*)(unsigned int))dlsym(RTLD_DEFAULT, "_dyld_get_image_name");
	void *(*imageCountFn)(void) = (void *(*)(void))dlsym(RTLD_DEFAULT, "_dyld_image_count");
	if (imageNameFn && imageCountFn) {
		unsigned int count = (unsigned int)(unsigned long)imageCountFn();
		unsigned int i;
		for (i = 0; i < count; i++) {
			const char *name = (const char *)imageNameFn(i);
			if (name && strstr(name, "liboah")) {
				printf("LIBOAH-IMAGE loaded_as=%s index=%u\n", name, i);
			}
		}
	}

	printf("LIBOAH-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("LIBOAH_VERDICT=GREEN\n");
	else
		printf("LIBOAH_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

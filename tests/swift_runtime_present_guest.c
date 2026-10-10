/*
 * The runtime pieces a build tool loads must exist in the prefix.
 *
 * Measured, not assumed: running the real Xcode's xcodebuild under Darling makes
 * dyld name the first piece it cannot find, in this order --
 *
 *   /usr/lib/swift/libswiftCore.dylib
 *   /System/Library/PrivateFrameworks/PackageKit.framework/Versions/A/PackageKit
 *   /usr/lib/libarchive.2.dylib
 *
 * All three were buildable and installable before this test existed; they were
 * missing from the rootless toolchain component's manifest, so a prefix filled
 * through the ordinary deploy path did not carry them, and an agent ended up
 * installing them by hand. The point of this fixture is that the prefix proves
 * the property itself, so a manifest that loses them again fails here instead
 * of inside somebody's build.
 *
 * Hard failures are the two pieces whose own dependency closure is libSystem
 * alone: the Swift runtime and libarchive. PackageKit is reported but does not
 * fail this test, because its own framework closure is wider than the property
 * under test and a missing transitive framework there is a different finding.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

static int load_required(const char *guest_path, const char *label) {
	int rc;
	void *handle = dlopen(guest_path, RTLD_NOW | RTLD_LOCAL);
	if (handle == NULL) {
		const char *error = dlerror();
		printf("SWIFT_RUNTIME_ABSENT component=%s path=%s error=%s\n",
		       label, guest_path, error != NULL ? error : "(no dlerror)");
		return 1;
	}
	rc = dlclose(handle);
	if (rc != 0) {
		printf("SWIFT_RUNTIME_CLOSE_FAILED component=%s path=%s rc=%d\n",
		       label, guest_path, rc);
		return 1;
	}
	printf("SWIFT_RUNTIME_LOADED component=%s path=%s\n", label, guest_path);
	return 0;
}

int main(void) {
	int failed = 0;

	failed += load_required("/usr/lib/swift/libswiftCore.dylib", "swift-core");
	failed += load_required("/usr/lib/libarchive.2.dylib", "libarchive");

	/* Reported, not asserted: see the file comment. */
	{
		void *handle = dlopen(
			"/System/Library/PrivateFrameworks/PackageKit.framework/Versions/A/PackageKit",
			RTLD_NOW | RTLD_LOCAL);
		if (handle != NULL) {
			printf("SWIFT_RUNTIME_LOADED component=packagekit\n");
			dlclose(handle);
		} else {
			const char *error = dlerror();
			printf("SWIFT_RUNTIME_TRANSITIVE component=packagekit error=%s\n",
			       error != NULL ? error : "(no dlerror)");
		}
	}

	if (failed != 0) {
		printf("SWIFT_RUNTIME_PRESENT_FAILED missing=%d\n", failed);
		return 1;
	}
	printf("SWIFT_RUNTIME_PRESENT_OK\n");
	return 0;
}

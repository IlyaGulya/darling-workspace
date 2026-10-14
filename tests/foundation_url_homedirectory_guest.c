/*
 * foundation_url_homedirectory_guest.c -- permanent guest oracle for the ONE swift-mangled
 * Foundation overlay member Xcode 26.3's IDESimulatorFoundation demands and this runtime did not
 * export:
 *
 *   _$s10Foundation3URLV13homeDirectoryACvgZ
 *     = static Foundation.URL.homeDirectory.getter : Foundation.URL
 *
 * WHY THIS EXISTS (measured, not inferred). Enumerating the whole unsatisfied set of Xcode's
 * Contents/Frameworks/IDESimulatorFoundation.framework/Versions/A/IDESimulatorFoundation
 * (sha256 83db461982c7f9846adf4a160baaa6b6d73925852a3d3f2429d6756b84873c0f) against the deployed
 * prefix resolves every import of the plug-in's 239-image load closure except two SYMBOL members:
 * this one, bound from the plug-in's Foundation field, and _OBJC_CLASS_$_SimUsageTracker, bound
 * from its CoreSimulator field. The Foundation field is the framework the loader names
 *   Expected in: /System/Library/Frameworks/Foundation.framework/Versions/C/Foundation
 * and Foundation.framework re-exports /usr/lib/swift/libswiftFoundation.dylib as an ordinal, so the
 * lookup that has to succeed is against that overlay's export trie.
 *
 * WHAT THIS MEASURES, in one guest process on the DEPLOYED runtime, with no compile-time reference
 * to Foundation at all (dlopen + dlsym only):
 *
 *   1. the demanded mangled name RESOLVES in the overlay image under its own install name and in
 *      the process-global table. This is the arm that is RED on a prefix deployed before the member
 *      existed: dlsym returns NULL for the demanded name and the loader would have stopped here,
 *      exactly as the workload's own dyld message says. (Darling's dlsym on the FOUNDATION handle
 *      does NOT search the LC_REEXPORT_DYLIB closure, so that channel is reported informationally
 *      and not gated -- the same measured caveat the contentsOfFile oracle records.)
 *   2. the CONTROL NAME _$s10Foundation3URLV6string25encodingInvalidCharactersACSgSSh_SbtcfC, a
 *      sibling the SAME overlay carries from the previous rung and which is present in both arms, is
 *      found through the same channel -- so a RED arm proves the channel and the lookup are right
 *      and the difference is membership, not lookup.
 *   3. the DELEGATION TARGET's contract. The member's Darwin body is
 *      `URL(filePath: NSHomeDirectory(), directoryHint: .isDirectory)` (swift-foundation
 *      Sources/FoundationEssentials/URL/URL.swift:1573, commit dbacc67), so the primitive it stands
 *      on is the C function NSHomeDirectory(), which this tree implements in
 *      src/external/foundation/src/NSPathUtilities.m:389 as the HOME environment value with a "/"
 *      fallback. The fixture resolves NSHomeDirectory through the same Foundation handle the
 *      referrer's field names and asserts it returns a non-empty absolute path -- which is what
 *      makes the member's one-line body truthful. The member itself cannot be CALLED from C: URL is
 *      a resilient Swift struct returned indirectly from a foreign module, and a hand-rolled
 *      calling convention would be a guess, not a measurement.
 *
 * FOUNDATION_URL_HOMEDIRECTORY_VERDICT=GREEN is the machine-readable line the harness ok-marker
 * reads; every measured value is printed unconditionally as a FOUNDATION_URL_HOMEDIRECTORY-* line
 * so a RED arm quotes the failure from the run's own output. Exit status is 0 on GREEN, 1 on RED.
 *
 * The RED arm is the DEPLOYED RUNTIME, not a source tree: the oracle is run against the prefix as
 * deployed, before and after the fix.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
/* The image that must answer the demand: Foundation.framework re-exports it, so a two-level bind
 * whose field is Foundation is searched here. Naming it directly is what makes the check possible
 * with dlsym, which does not follow LC_REEXPORT_DYLIB (measured below). */
#define OVERLAY_LIBRARY "/usr/lib/swift/libswiftFoundation.dylib"

/* The name IDESimulatorFoundation binds from its Foundation field, strong and eager. dyld's dlsym
 * prepends the Mach-O leading underscore itself (the way `dlsym(h, "malloc")` finds `_malloc`), so
 * the lookup string is the mangled name WITHOUT it; the printed form keeps it. */
#define DEMANDED_SYMBOL "_$s10Foundation3URLV13homeDirectoryACvgZ"
#define DEMANDED_LOOKUP "$s10Foundation3URLV13homeDirectoryACvgZ"
/* The sibling the same overlay carries from the URL-string-init rung: present in BOTH arms, so it is
 * the control that separates "the lookup is wrong" from "the member is missing". */
#define CONTROL_SYMBOL "_$s10Foundation3URLV6string25encodingInvalidCharactersACSgSSh_SbtcfC"
#define CONTROL_LOOKUP "$s10Foundation3URLV6string25encodingInvalidCharactersACSgSSh_SbtcfC"

static int failures;

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);

	/* --- 1. the image that must answer it, and the view the referrer's bind uses ---------------- */
	void *foundation = dlopen(FOUNDATION_LIBRARY, RTLD_LAZY | RTLD_GLOBAL);
	printf("FOUNDATION_URL_HOMEDIRECTORY-DLOPEN %s handle=%p error=%s\n", FOUNDATION_LIBRARY,
	       foundation, foundation ? "-" : dlerror());
	if (!foundation) {
		printf("FOUNDATION_URL_HOMEDIRECTORY-DECISION Foundation.framework itself does not load on "
		       "this deployed runtime, so the demand cannot be measured here\n");
		printf("FOUNDATION_URL_HOMEDIRECTORY_VERDICT=RED\n");
		return 1;
	}

	void *overlay = dlopen(OVERLAY_LIBRARY, RTLD_LAZY | RTLD_GLOBAL);
	printf("FOUNDATION_URL_HOMEDIRECTORY-DLOPEN %s handle=%p error=%s\n", OVERLAY_LIBRARY, overlay,
	       overlay ? "-" : dlerror());
	if (!overlay) {
		printf("FOUNDATION_URL_HOMEDIRECTORY-DECISION the overlay Foundation re-exports does not "
		       "load, so the demand cannot be measured here\n");
		printf("FOUNDATION_URL_HOMEDIRECTORY_VERDICT=RED\n");
		return 1;
	}

	void *ch_demanded = dlsym(overlay, DEMANDED_LOOKUP);
	void *ch_control = dlsym(overlay, CONTROL_LOOKUP);
	void *flat_demanded = dlsym(RTLD_DEFAULT, DEMANDED_LOOKUP);
	void *flat_control = dlsym(RTLD_DEFAULT, CONTROL_LOOKUP);
	void *fh_demanded = dlsym(foundation, DEMANDED_LOOKUP);
	void *fh_control = dlsym(foundation, CONTROL_LOOKUP);

	printf("FOUNDATION_URL_HOMEDIRECTORY-DLSYM Foundation-handle  %-58s %s (%p)\n", CONTROL_SYMBOL,
	       fh_control ? "present" : "ABSENT", fh_control);
	printf("FOUNDATION_URL_HOMEDIRECTORY-DLSYM Foundation-handle  %-58s %s (%p)\n", DEMANDED_SYMBOL,
	       fh_demanded ? "present" : "ABSENT", fh_demanded);
	printf("FOUNDATION_URL_HOMEDIRECTORY-DLSYM %-18s %-58s %s (%p)\n", OVERLAY_LIBRARY,
	       CONTROL_SYMBOL, ch_control ? "present" : "ABSENT", ch_control);
	printf("FOUNDATION_URL_HOMEDIRECTORY-DLSYM RTLD_DEFAULT      %-58s %s (%p)\n", CONTROL_SYMBOL,
	       flat_control ? "present" : "ABSENT", flat_control);
	printf("FOUNDATION_URL_HOMEDIRECTORY-DLSYM %-18s %-58s %s (%p)\n", OVERLAY_LIBRARY,
	       DEMANDED_SYMBOL, ch_demanded ? "present" : "ABSENT", ch_demanded);
	printf("FOUNDATION_URL_HOMEDIRECTORY-DLSYM RTLD_DEFAULT      %-58s %s (%p)\n", DEMANDED_SYMBOL,
	       flat_demanded ? "present" : "ABSENT", flat_demanded);

	/* The control defines which channels are usable: a channel that cannot see a member the same
	 * overlay carries cannot measure either member. */
	struct { const char *name; void *demanded; void *control; } channels[] = {
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
			printf("FOUNDATION_URL_HOMEDIRECTORY-DECISION channel %s sees the control name but not "
			       "the demanded one, so dlopen of IDESimulatorFoundation -- and of any image "
			       "binding it -- stops with \"Symbol not found: %s\"\n", channels[i].name,
			       DEMANDED_SYMBOL);
		}
	}
	if (usable == 0) {
		failures++;
		printf("FOUNDATION_URL_HOMEDIRECTORY-DECISION no channel sees the control name, so this "
		       "fixture is not looking where the referrer looks and its verdict means nothing\n");
	}
	printf("FOUNDATION_URL_HOMEDIRECTORY-CHANNELS usable=%u of %lu\n", usable,
	       (unsigned long)(sizeof(channels) / sizeof(channels[0])));

	/* --- 2. the delegation target the member's body stands on ----------------------------------- */
	/* NSHomeDirectory() returns an NSString, so the C string is reached through the ObjC runtime,
	 * exactly as the contentsOfFile oracle reaches its own primitive. */
	void *libobjc = dlopen("/usr/lib/libobjc.A.dylib", RTLD_NOW | RTLD_GLOBAL);
	void *(*msgSend)(void *, void *) = libobjc
		? (void *(*)(void *, void *))dlsym(libobjc, "objc_msgSend") : NULL;
	void *(*selRegister)(const char *) = libobjc
		? (void *(*)(const char *))dlsym(libobjc, "sel_registerName") : NULL;
	printf("FOUNDATION_URL_HOMEDIRECTORY-OBJC libobjc=%p objc_msgSend=%p sel_registerName=%p\n",
	       libobjc, (void *)msgSend, (void *)selRegister);

	void *(*nsHomeDirectory)(void) = (void *(*)(void))dlsym(foundation, "NSHomeDirectory");
	printf("FOUNDATION_URL_HOMEDIRECTORY-DLSYM %-18s %-58s %s (%p)\n", FOUNDATION_LIBRARY,
	       "_NSHomeDirectory", nsHomeDirectory ? "present" : "ABSENT", nsHomeDirectory);
	if (!nsHomeDirectory) {
		failures++;
		printf("FOUNDATION_URL_HOMEDIRECTORY-DECISION the member's one-line body calls "
		       "NSHomeDirectory() and this Foundation does not export it, so the body would have "
		       "nothing truthful to stand on\n");
	} else if (!msgSend || !selRegister) {
		failures++;
		printf("FOUNDATION_URL_HOMEDIRECTORY-DECISION the ObjC runtime is not usable, so "
		       "NSHomeDirectory()'s NSString result cannot be read here\n");
	} else {
		void *home = nsHomeDirectory();
		const char *utf8 = home ? (const char *)msgSend(home, selRegister("UTF8String")) : NULL;
		const char *env_home = getenv("HOME");
		int ok = utf8 != NULL && utf8[0] == '/' && utf8[1] != '\0';
		if (!ok)
			failures++;
		printf("FOUNDATION_URL_HOMEDIRECTORY-CHECK %-34s %-9s home=%s getenv(HOME)=%s\n",
		       "home-directory-absolute", ok ? "ok" : "MISMATCH",
		       utf8 ? utf8 : "(null)", env_home ? env_home : "(unset)");
	}

	printf("FOUNDATION_URL_HOMEDIRECTORY-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("FOUNDATION_URL_HOMEDIRECTORY_VERDICT=GREEN\n");
	else
		printf("FOUNDATION_URL_HOMEDIRECTORY_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}

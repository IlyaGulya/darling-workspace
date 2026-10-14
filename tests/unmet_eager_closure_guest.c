/*
 * unmet_eager_closure_guest.c -- permanent guest oracle for the strong eager C imports of Xcode
 * 26.3's workload load closure that the deployed runtime did not export, plus the two INSTRUMENT
 * measurements that settled the census that names them (bead dar-liaq, rung
 * unmet-eager-closure).
 *
 * WHY THIS EXISTS (measured, not inferred).  After the Foundation string-overlay rung closed the
 * one swift-mangled Foundation name, both xcodebuild workloads stop one wall further on, still in
 * the plug-in scan of IDEiOSSupportCore:
 *
 *   dlopen(.../PlugIns/IDEiOSSupportCore.framework/IDEiOSSupportCore, 265):
 *     Symbol not found: _NSLocalizedFileSizeDescription
 *     Referenced from: .../SharedFrameworks/DTDeviceKitBase.framework/Versions/A/DTDeviceKitBase
 *     Expected in: .../Foundation.framework/Versions/C/Foundation
 *
 * A whole-closure census (scripts/closure_analyze.py --cover, classified by
 * scripts/closure_missing.py) of the four workload entries -- xcodebuild, its loader image,
 * IDEiOSSupportCore and XCTDaemonControlMobileDevice -- names exactly THREE such imports, all in
 * the reached closure, all strong and eager (weak binds to NULL, lazy is deferred):
 *
 *   _NSLocalizedFileSizeDescription   Foundation    field ordinal 13   referrer DTDeviceKitBase
 *   __LSASNCreate                     CoreServices  field ordinal  6   referrer IDEiOSSupportCore
 *   __LSOpenApplicationURL            CoreServices  field ordinal  6   referrer IDEiOSSupportCore
 *
 * WHAT THIS MEASURES, in one guest process on the DEPLOYED runtime, with no compile-time reference
 * to any of the three images (dlopen + dlsym only).  Each demanded name is looked up through the
 * SAME channel as a CONTROL name that the same image already exports, and a channel whose control
 * is not visible decides nothing -- so a RED arm proves the lookup is right and the difference is
 * membership:
 *
 *   1. Foundation: _NSLocalizedFileSizeDescription against the Foundation handle (the referrer's own
 *      dependency field) and against RTLD_DEFAULT; control _NSStringFromClass.
 *   2. CoreServices/LaunchServices: __LSASNCreate and __LSOpenApplicationURL against the
 *      LaunchServices sub-framework handle (the image that DEFINES them; the CoreServices umbrella
 *      only re-exports it) and against RTLD_DEFAULT; control _LSOpenCFURLRef, which the same image
 *      already exports.
 *   3. The plug-in's OWN dlopen (IDEiOSSupportCore), because that is the operation the workload
 *      fails and the one the referrer's demand actually gates.  Reported; it does not by itself
 *      decide the verdict, because "the names resolve" is what the loader needs and a plug-in load
 *      can fail later for reasons outside this demand.
 *
 * AND the two instrument measurements, which must be GREEN in BOTH arms because they are about the
 * runtime and the instrument, not about this change (they are why the census says three and not
 * nine):
 *
 *   A. /usr/lib/system/libunwind.dylib is a FLAT-NAMESPACE image, so its whole import table is
 *      flat-lookup binds, and its `___stack_chk_guard`/`___stderrp` are exported by the prefix's
 *      libsystem_c.dylib.  dlopen(..., RTLD_NOW) succeeds iff the flat lookups resolve at load,
 *      which the guest's own dyld does after mapping the whole closure (ImageLoader::link:
 *      recursiveLoadLibraries, then recursiveRebase, then recursiveBind; a flat lookup is
 *      flatFindExportedSymbol over sAllImages).  The census used to report these two as MISSING
 *      because the model resolved flat lookups against only the images its traversal had already
 *      visited.  dlsym against libsystem_c names WHERE the two live.
 *   B. .../SKAgent.xpc/Contents/Frameworks/SKSupport.dylib EXISTS in Xcode and exports the four
 *      SourceKit::UIdent symbols SKIPC.dylib/SKToolchain.dylib bind; those two images have NO
 *      LC_RPATH of their own, so the census reported their field image absent, while dyld resolves
 *      @rpath/SKSupport.dylib through the rpath of the image that loaded them
 *      (SourceKit.framework's @loader_path/XPCServices/.../Frameworks; dyld's RPathChain in
 *      recursiveLoadLibraries).  dlopen(SourceKit.framework) succeeding measures exactly that.
 */

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

#define FOUNDATION_LIBRARY \
	"/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
#define LAUNCHSERVICES_LIBRARY \
	"/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/" \
	"LaunchServices.framework/Versions/A/LaunchServices"
#define CORESERVICES_LIBRARY \
	"/System/Library/Frameworks/CoreServices.framework/Versions/A/CoreServices"
#define LIBUNWIND_LIBRARY "/usr/lib/system/libunwind.dylib"
#define LIBSYSTEM_C_LIBRARY "/usr/lib/system/libsystem_c.dylib"
#define XCODE_ROOT \
	"/Volumes/SystemRoot/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app/Contents"
#define PLUGIN_LIBRARY \
	XCODE_ROOT "/PlugIns/IDEiOSSupportCore.framework/Versions/A/IDEiOSSupportCore"
#define SOURCEKIT_LIBRARY \
	XCODE_ROOT "/SharedFrameworks/SourceKit.framework/Versions/A/SourceKit"
#define SKSUPPORT_LIBRARY \
	XCODE_ROOT "/SharedFrameworks/SourceKit.framework/Versions/A/XPCServices/" \
	"com.apple.dt.SKAgent.xpc/Contents/Frameworks/SKSupport.dylib"

/* dyld's dlsym prepends the Mach-O leading underscore itself (measured by the previous rung:
 * dlsym(h, "malloc") finds _malloc), so every lookup string below is the symbol's own C name
 * WITHOUT the Mach-O underscore.  That matters for the two LaunchServices names: Apple's private
 * API spells them `_LSASNCreate` and `_LSOpenApplicationURL`, so their Mach-O names are
 * `__LSASNCreate`/`__LSOpenApplicationURL` and the lookup string keeps one leading underscore.
 * (Measured the hard way: with "LSASNCreate" the post-fix lookup reads ABSENT while the very same
 * plug-in load that needs the symbol succeeds -- dlsym was asked for `_LSASNCreate`, which is not
 * the symbol's name.) */
static const char *const DEMANDED_FOUNDATION = "NSLocalizedFileSizeDescription";
static const char *const CONTROL_FOUNDATION = "NSStringFromClass";
static const char *const DEMANDED_LSASN = "_LSASNCreate";
static const char *const DEMANDED_LSOPEN = "_LSOpenApplicationURL";
static const char *const CONTROL_LAUNCHSERVICES = "LSOpenCFURLRef";

static int failures;
static int plugin_ok;

static void *open_handle(const char *what, const char *path)
{
	void *h = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
	printf("UNMET-EAGER-DLOPEN %-14s %s\n", what, path);
	printf("UNMET-EAGER-DLOPEN %-14s handle=%p error=%s\n", what, h, h ? "-" : dlerror());
	return h;
}

/* One lookup in one channel.  Returns whether the name was found; prints it either way. */
static int resolve(void *handle, const char *channel, const char *name)
{
	void *p = dlsym(handle, name);
	printf("UNMET-EAGER-DLSYM %-14s %-28s %s (%p)\n", channel, name,
	       p ? "present" : "ABSENT", p);
	return p != NULL;
}

/* The demanded name through a channel is only readable when the channel's CONTROL name is visible
 * in the same channel; without it a RED could be a broken lookup rather than an absent member. */
static int demand_in_channel(void *handle, const char *channel, const char *control,
                             const char *demanded, const char *what)
{
	int have_control = resolve(handle, channel, control);
	int have_demanded = resolve(handle, channel, demanded);
	if (!have_control) {
		printf("UNMET-EAGER-CHANNEL %-14s control %s ABSENT -- its verdict on %s means "
		       "nothing\n", channel, control, what);
		return 0;
	}
	return have_demanded;
}

static void plugin_check(void)
{
	void *h = open_handle("plugin", PLUGIN_LIBRARY);
	if (h) {
		printf("UNMET-EAGER-PLUGIN IDEiOSSupportCore dlopen OK -- the referrer's whole "
		       "demand resolved\n");
		plugin_ok = 1;
	} else {
		printf("UNMET-EAGER-PLUGIN IDEiOSSupportCore dlopen FAILED: %s\n", dlerror());
	}
}

/* A. the flat-namespace pair. */
static void instrument_libunwind(void)
{
	void *unwind = open_handle("libunwind", LIBUNWIND_LIBRARY);
	if (unwind) {
		printf("UNMET-EAGER-INSTRUMENT libunwind loads with RTLD_NOW -- its flat-lookup "
		       "imports resolved at load\n");
	} else {
		printf("UNMET-EAGER-INSTRUMENT libunwind RTLD_NOW FAILED: %s\n", dlerror());
		failures++;
	}

	void *libc = open_handle("libsystem_c", LIBSYSTEM_C_LIBRARY);
	if (libc) {
		resolve(libc, "libsystem_c", "printf");
		resolve(libc, "libsystem_c", "__stack_chk_guard");
		resolve(libc, "libsystem_c", "__stderrp");
	}
}

/* B. SKSupport, and the rpath chain that finds it. */
static void instrument_sksupport(void)
{
	void *sk = open_handle("SKSupport", SKSUPPORT_LIBRARY);
	if (sk) {
		resolve(sk, "SKSupport", "_ZN9SourceKit6UIdentC1EN17indexstoredb_llvm9StringRefE");
		resolve(sk, "SKSupport", "_ZNK9SourceKit6UIdent7getNameEv");
		resolve(sk, "SKSupport", "_ZNK9SourceKit6UIdent5c_strEv");
	} else {
		printf("UNMET-EAGER-INSTRUMENT SKSupport.dylib is absent or unreadable -- the "
		       "SourceKit field image could not be measured\n");
		failures++;
	}

	/* SKIPC.dylib/SKToolchain.dylib carry no LC_RPATH; SKSupport is reached only through the
	 * rpath chain of SourceKit.framework, which is what loading it measures. */
	void *sourcekit = open_handle("SourceKit", SOURCEKIT_LIBRARY);
	if (sourcekit) {
		printf("UNMET-EAGER-INSTRUMENT SourceKit.framework loads -- @rpath/SKSupport.dylib "
		       "resolved through its loader's rpath chain\n");
	} else {
		printf("UNMET-EAGER-INSTRUMENT SourceKit.framework FAILED: %s\n", dlerror());
		failures++;
	}
}

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);

	int found = 0;

	/* 1. the Foundation member, in the referrer's own dependency field and in the global table. */
	void *foundation = open_handle("Foundation", FOUNDATION_LIBRARY);
	if (foundation) {
		found += demand_in_channel(foundation, "Foundation", CONTROL_FOUNDATION,
		                           DEMANDED_FOUNDATION, "the Foundation name");
	} else {
		failures++;
	}
	found += demand_in_channel(RTLD_DEFAULT, "RTLD_DEFAULT", CONTROL_FOUNDATION,
	                           DEMANDED_FOUNDATION, "the Foundation name");

	/* 2. the two CoreServices members, in the image that DEFINES them. */
	void *launchservices = open_handle("LaunchServices", LAUNCHSERVICES_LIBRARY);
	if (launchservices) {
		found += demand_in_channel(launchservices, "LaunchServices", CONTROL_LAUNCHSERVICES,
		                           DEMANDED_LSASN, "the LSASNCreate name");
		found += demand_in_channel(launchservices, "LaunchServices", CONTROL_LAUNCHSERVICES,
		                           DEMANDED_LSOPEN, "the LSOpenApplicationURL name");
	} else {
		failures++;
	}
	found += demand_in_channel(RTLD_DEFAULT, "RTLD_DEFAULT", CONTROL_LAUNCHSERVICES,
	                           DEMANDED_LSASN, "the LSASNCreate name");
	found += demand_in_channel(RTLD_DEFAULT, "RTLD_DEFAULT", CONTROL_LAUNCHSERVICES,
	                           DEMANDED_LSOPEN, "the LSOpenApplicationURL name");

	/* 3. the operation the workload actually fails: loading the plug-in itself. */
	plugin_check();

	/* The two instrument measurements (must be GREEN in both arms). */
	instrument_libunwind();
	instrument_sksupport();

	printf("UNMET-EAGER-DEMANDED-RESOLVED %d of 6 demanded-name checks "
	       "(3 names x 2 channels)\n", found);
	printf("UNMET_EAGER_INSTRUMENT=%s\n", failures == 0 ? "GREEN" : "RED");
	printf("UNMET_EAGER_CLOSURE_VERDICT=%s\n", found == 6 ? "GREEN" : "RED");
	return found == 6 ? 0 : 1;
}

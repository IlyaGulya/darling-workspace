/*
 * nsbundle_bundle_with_identifier_guest.c -- the oracle for +[NSBundle bundleWithIdentifier:]
 * finding a bundle the CFBundle layer knows about.
 *
 * WHAT IT MEASURES (and why it is the behavioural divergence, not Xcode's symptom)
 *   Xcode's platform/SDK specification scan reaches +[PBXBuildRule initialize] (DevToolsCore),
 *   which builds a PBXFileType spec dictionary whose "Name" member comes from
 *   [NSBundle cachedBundleWithIdentifier:@"com.apple.Xcode.DevToolsCore"].  That DevToolsSupport
 *   method (measured by disassembly: +[NSBundle(TSBundleAdditions) cachedBundleWithIdentifier:])
 *   answers nil as soon as [NSBundle bundleWithIdentifier:] does, and a message to that nil then
 *   yields the nil MEMBER of the dictionary, i.e. the uncaught
 *   "Tried to init dictionary with nil object" that stops the workload.
 *
 *   In this tree +[NSBundle bundleWithIdentifier:] read only an NSBundle-private registration table
 *   (populated by -initWithPath:/-initWithURL:), with a standing TODO about the search it does not
 *   perform; Darwin answers the same question from the CFBundle layer, which ALSO knows the bundles
 *   of the images dyld loaded and every bundle CFBundleCreate was asked for.  So the divergence is:
 *   an identifier CFBundle can resolve must resolve through NSBundle.bundleWithIdentifier: too.
 *
 *   The fixture makes that case deterministic with no Xcode and no dlopen'd third-party image: it
 *   writes a minimal .bundle with an Info.plist, registers it through CFBundleCreate ONLY (never
 *   through NSBundle), and asks BOTH channels for the identifier.
 *
 * CHANNELS AND CONTROLS
 *   control 1  the bundle's own Info.plist is readable through CFBundleGetIdentifier
 *   control 2  CFBundleGetBundleWithIdentifier finds the bundle by identifier
 *   measured   [NSBundle bundleWithIdentifier:] returns that bundle (Darwin: yes; here: the FIX)
 *   A control that is not visible decides nothing: the fixture reports NSBUNDLE-ID CONTROL-MISSING
 *   and exits 3 rather than pretending a channel it could not see.
 *
 * Self-contained: dlopen + dlsym (and plain POSIX file I/O) only -- no compile-time reference to
 * Foundation, so the same bytes compile in any guest.  Prints NSBUNDLE_BUNDLE_ID_VERDICT=GREEN and
 * exits 0 when the demanded identifier resolves through NSBundle; RED and exit 1 otherwise.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <unistd.h>
#include <sys/stat.h>

typedef void *oid_t;
typedef void *osel_t;

#define ORACLE_IDENT "com.example.darling.nsbundle.bundleid"
#define ORACLE_ROOT  "/private/var/tmp/nsbundle-bundle-with-identifier.bundle"
#define ORACLE_CLEANUP 1

static oid_t (*p_objc_getClass)(const char *);
static osel_t (*p_sel_registerName)(const char *);
static oid_t (*p_msgSend)(oid_t, osel_t, ...);

static const char *nsobj_to_c(oid_t s)
{
    if (!s) return "(nil)";
    const char *p = (const char *)p_msgSend(s, p_sel_registerName("UTF8String"));
    return p ? p : "(unreadable)";
}

static int write_info_plist(const char *path)
{
    FILE *f = fopen(path, "w");
    if (!f) return 0;
    fprintf(f, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
               "<plist version=\"1.0\"><dict>"
               "<key>CFBundleIdentifier</key><string>" ORACLE_IDENT "</string>"
               "<key>CFBundleName</key><string>nsbundle-bundle-with-identifier</string>"
               "<key>CFBundlePackageType</key><string>BNDL</string>"
               "</dict></plist>\n");
    fclose(f);
    return 1;
}

int main(void)
{
    char contents[512], plist[600];
    snprintf(contents, sizeof(contents), "%s/Contents", ORACLE_ROOT);
    snprintf(plist, sizeof(plist), "%s/Info.plist", contents);

    unlink(plist);
    mkdir(ORACLE_ROOT, 0755);
    mkdir(contents, 0755);
    if (!write_info_plist(plist)) {
        printf("NSBUNDLE-ID FIXTURE-ERROR cannot write %s\n", plist);
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
        return 3;
    }

    void *cf = dlopen("/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation", RTLD_NOW);
    if (!cf) cf = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", RTLD_NOW);
    if (!cf) {
        printf("NSBUNDLE-ID FIXTURE-ERROR no CoreFoundation handle\n");
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
        return 3;
    }

    void *(*pCFStringCreateWithCString)(const void *, const char *, unsigned) =
        (void *(*)(const void *, const char *, unsigned))dlsym(cf, "CFStringCreateWithCString");
    void *(*pCFURLCreateWithFileSystemPath)(const void *, const void *, long, int) =
        (void *(*)(const void *, const void *, long, int))dlsym(cf, "CFURLCreateWithFileSystemPath");
    void *(*pCFBundleCreate)(const void *, const void *) =
        (void *(*)(const void *, const void *))dlsym(cf, "CFBundleCreate");
    void *(*pCFBundleGetIdentifier)(const void *) =
        (void *(*)(const void *))dlsym(cf, "CFBundleGetIdentifier");
    void *(*pCFBundleGetBundleWithIdentifier)(const void *) =
        (void *(*)(const void *))dlsym(cf, "CFBundleGetBundleWithIdentifier");
    long (*pCFStringGetLength)(const void *) = (long (*)(const void *))dlsym(cf, "CFStringGetLength");
    int (*pCFStringGetCString)(const void *, char *, long, unsigned) =
        (int (*)(const void *, char *, long, unsigned))dlsym(cf, "CFStringGetCString");

    if (!pCFStringCreateWithCString || !pCFURLCreateWithFileSystemPath || !pCFBundleCreate ||
        !pCFBundleGetIdentifier || !pCFBundleGetBundleWithIdentifier ||
        !pCFStringGetLength || !pCFStringGetCString) {
        printf("NSBUNDLE-ID CONTROL-MISSING CFBundle entry points not resolvable through dlopen\n");
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
        return 3;
    }

    void *cpath = pCFStringCreateWithCString(NULL, ORACLE_ROOT, 0x08000100u /* kCFStringEncodingUTF8 */);
    void *url = pCFURLCreateWithFileSystemPath(NULL, cpath, 0 /* kCFURLPOSIXPathStyle */, 1 /* isDirectory */);
    void *cfBundle = url ? pCFBundleCreate(NULL, url) : NULL;

    char cfIdent[256];
    cfIdent[0] = '\0';
    int control1 = 0, control2 = 0;
    if (cfBundle) {
        void *idcf = pCFBundleGetIdentifier(cfBundle);
        if (idcf) {
            long len = pCFStringGetLength(idcf);
            if (len > 0 && (size_t)len + 1 < sizeof(cfIdent)) {
                if (pCFStringGetCString(idcf, cfIdent, len + 1, 0x08000100u)) control1 = 1;
            }
        }
        void *want = pCFStringCreateWithCString(NULL, ORACLE_IDENT, 0x08000100u);
        control2 = pCFBundleGetBundleWithIdentifier(want) != NULL;
    }
    printf("NSBUNDLE-ID CONTROL plist=%s cfidentifier=\"%s\" cf_lookup=%s\n",
           cfBundle ? "ok" : "NULL", control1 ? cfIdent : "(none)",
           control2 ? "FOUND" : "NULL");

    if (!control1 || !control2 || strcmp(cfIdent, ORACLE_IDENT) != 0) {
        // The channel the measurement rides on is not visible here, so this run decides nothing.
        printf("NSBUNDLE-ID CONTROL-MISSING the CFBundle channel cannot see the fixture's own bundle\n");
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
        return 3;
    }

    void *objc = dlopen("/usr/lib/libobjc.A.dylib", RTLD_NOW);
    if (!objc) objc = dlopen("libobjc.A.dylib", RTLD_NOW);
    if (!objc) {
        printf("NSBUNDLE-ID CONTROL-MISSING no libobjc handle\n");
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
        return 3;
    }
    p_objc_getClass = (oid_t (*)(const char *))dlsym(objc, "objc_getClass");
    p_sel_registerName = (osel_t (*)(const char *))dlsym(objc, "sel_registerName");
    p_msgSend = (oid_t (*)(oid_t, osel_t, ...))dlsym(objc, "objc_msgSend");
    if (!p_objc_getClass || !p_sel_registerName || !p_msgSend) {
        printf("NSBUNDLE-ID CONTROL-MISSING libobjc entry points not resolvable\n");
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
        return 3;
    }
    if (!dlopen("/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation", RTLD_NOW)) {
        printf("NSBUNDLE-ID CONTROL-MISSING no Foundation handle\n");
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
        return 3;
    }

    oid_t NSString = p_objc_getClass("NSString");
    oid_t NSBundle = p_objc_getClass("NSBundle");
    if (!NSString || !NSBundle) {
        printf("NSBUNDLE-ID CONTROL-MISSING the ObjC classes are not visible\n");
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
        return 3;
    }

    oid_t sid = p_msgSend(NSString, p_sel_registerName("stringWithUTF8String:"), ORACLE_IDENT);
    oid_t nb = p_msgSend(NSBundle, p_sel_registerName("bundleWithIdentifier:"), sid);

    char nbPath[512];
    nbPath[0] = '\0';
    if (nb) {
        const char *p = nsobj_to_c(p_msgSend(nb, p_sel_registerName("bundlePath")));
        snprintf(nbPath, sizeof(nbPath), "%s", p);
    }

    printf("NSBUNDLE-ID DEMANDED [NSBundle bundleWithIdentifier:@\"%s\"] => %s\n",
           ORACLE_IDENT, nb ? nbPath : "nil");
    printf("NSBUNDLE-ID RESOLVED %d of 1 demanded identifier through NSBundle\n", nb ? 1 : 0);

#if ORACLE_CLEANUP
    unlink(plist);
    rmdir(contents);
    rmdir(ORACLE_ROOT);
#endif

    if (nb) {
        printf("NSBUNDLE_BUNDLE_ID_VERDICT=GREEN\n");
        return 0;
    }
    printf("NSBUNDLE_BUNDLE_ID_VERDICT=RED\n");
    return 1;
}

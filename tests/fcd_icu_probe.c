/*
 * e4 ICU diagnosis for the CFDateFormatterCreate NULL result.
 *
 * This mirrors the arguments CFDateFormatter's __ResetUDateFormat passes to
 * udat_open: UDAT_FULL/UDAT_FULL, the ASCII locale identifier, and the default
 * timezone's UTF-16 name. It reports the deployed ICU header/runtime versions,
 * exports, data directory, default locale, and the actual udat_open status.
 * It is a measurement probe, not a fallback or fix.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <unicode/udat.h>
#include <unicode/utypes.h>
#include <unicode/uloc.h>
#include <unicode/udata.h>
#include <unicode/uversion.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

static void print_version(void)
{
    UVersionInfo vi;
    char runtime[U_MAX_VERSION_STRING_LENGTH] = "<unknown>";
    u_getVersion(vi);
    u_versionToString(vi, runtime);
    printf("ICU_HEADER_VERSION=%s ICU_RUNTIME_VERSION=%s\n", U_ICU_VERSION, runtime);
#ifdef U_ICU_DATA_VERSION
    printf("ICU_HEADER_DATA_VERSION=%s\n", U_ICU_DATA_VERSION);
#endif
    const char *data_dir = NULL;
    void *lib = dlopen("/usr/lib/libicucore.A.dylib", RTLD_NOW | RTLD_LOCAL);
    if (lib != NULL) {
        typedef const char *(*get_data_dir_fn)(void);
        get_data_dir_fn get_data_dir = (get_data_dir_fn)dlsym(lib, "u_getDataDirectory");
        if (get_data_dir != NULL) {
            data_dir = get_data_dir();
        }
    }
    printf("ICU_DATA_DIR=%s\n", data_dir ? data_dir : "(null)");
    if (lib != NULL) {
        dlclose(lib);
    }
    printf("ICU_DEFAULT_LOCALE=%s\n", uloc_getDefault());
}

static void print_export(const char *path)
{
    void *h = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    printf("ICU_DLOPEN path=%s handle=%p err=%s\n", path, h, h ? "ok" : dlerror());
    if (h == NULL) {
        return;
    }
    const char *names[] = {
        "udat_open", "udat_close", "u_getVersion", "u_getDataDirectory",
        "uloc_getDefault", "u_errorName", "u_versionToString", NULL
    };
    for (int i = 0; names[i] != NULL; i++) {
        void *p = dlsym(h, names[i]);
        printf("ICU_EXPORT %s=%p\n", names[i], p);
    }
    dlclose(h);
}

static void probe_udat(const char *label, const char *locale,
                       const UChar *tz, int32_t tzlen)
{
    UErrorCode status = U_ZERO_ERROR;
    UDateFormat *df = udat_open(UDAT_FULL, UDAT_FULL, locale, tz, tzlen,
                                NULL, 0, &status);
    printf("ICU_UDAT_OPEN label=%s locale=%s tzlen=%d tz=%s status=%d name=%s df=%p\n",
           label, locale, tzlen, (tz != NULL ? "default" : "NULL"),
           (int)status, u_errorName(status), (void *)df);
    if (df != NULL) {
        udat_close(df);
    }

    status = U_ZERO_ERROR;
    df = udat_open(UDAT_FULL, UDAT_FULL, locale, NULL, 0, NULL, 0, &status);
    printf("ICU_UDAT_OPEN_NOTZ label=%s locale=%s status=%d name=%s df=%p\n",
           label, locale, (int)status, u_errorName(status), (void *)df);
    if (df != NULL) {
        udat_close(df);
    }
}

static void probe_locale(const char *label, CFLocaleRef loc,
                         const UChar *tz, int32_t tzlen)
{
    char locale[256] = "";
    CFStringRef ident = loc ? CFLocaleGetIdentifier(loc) : NULL;
    if (ident != NULL) {
        CFStringGetCString(ident, locale, (CFIndex)sizeof(locale),
                           kCFStringEncodingASCII);
    }
    printf("ICU_LOCALE label=%s ptr=%p id=%s\n", label, (void *)loc, locale);

    CFDateFormatterRef formatter = CFDateFormatterCreate(kCFAllocatorDefault, loc,
        kCFDateFormatterFullStyle, kCFDateFormatterFullStyle);
    printf("ICU_CF_FORMATTER label=%s formatter=%p\n", label, (void *)formatter);
    if (formatter != NULL) {
        CFRelease(formatter);
    }

    probe_udat(label, locale, tz, tzlen);
}

int main(void)
{
    print_version();
    print_export("/usr/lib/libicucore.A.dylib");

    CFTimeZoneRef zone = CFTimeZoneCopyDefault();
    CFStringRef zone_name = zone ? CFTimeZoneGetName(zone) : NULL;
    printf("ICU_DEFAULT_TIMEZONE=%s ptr=%p\n",
           zone_name ? "present" : "(null)", (void *)zone);
    UChar tz_buffer[256];
    int32_t tzlen = 0;
    if (zone_name != NULL) {
        CFIndex n = CFStringGetLength(zone_name);
        if (n > (CFIndex)(sizeof(tz_buffer) / sizeof(tz_buffer[0]))) {
            n = (CFIndex)(sizeof(tz_buffer) / sizeof(tz_buffer[0]));
        }
        CFStringGetCharacters(zone_name, CFRangeMake(0, n), (UniChar *)tz_buffer);
        tzlen = (int32_t)n;
    }

    CFLocaleRef current = CFLocaleCopyCurrent();
    CFLocaleRef system = CFLocaleGetSystem();
    CFLocaleRef en_us = CFLocaleCreate(kCFAllocatorDefault, CFSTR("en_US"));
    CFLocaleRef posix = CFLocaleCreate(kCFAllocatorDefault, CFSTR("en_US_POSIX"));

    probe_locale("current", current, tz_buffer, tzlen);
    probe_locale("system", system, tz_buffer, tzlen);
    probe_locale("en_US", en_us, tz_buffer, tzlen);
    probe_locale("en_US_POSIX", posix, tz_buffer, tzlen);

    if (current != NULL) CFRelease(current);
    if (en_us != NULL) CFRelease(en_us);
    if (posix != NULL) CFRelease(posix);
    if (zone != NULL) CFRelease(zone);

    printf("FCD_ICU_MEASURED\n");
    fflush(stdout);
    return 0;
}

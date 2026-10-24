/*
 * Minimal guest probe for the filecoordinationd date-description crash.
 *
 * NSDate -description eventually performs:
 *   CFLocaleCopyCurrent
 *   CFDateFormatterCreate(..., kCFDateFormatterFullStyle, kCFDateFormatterFullStyle)
 *   CFDateFormatterSetProperty(formatter, kCFDateFormatterTimeZone, tz)
 *   CFDateFormatterCreateStringWithDate(...)
 *
 * The run-bound core shows the property call with a NULL formatter, so the
 * creation result and the per-locale failure set are the measurement here.
 * This is a diagnostic fixture, not a fix: it never calls the property setter
 * with a NULL formatter and it never substitutes a fallback formatter.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <string.h>

static void locale_name(CFLocaleRef loc, char *buf, size_t cap)
{
    if (buf == NULL || cap == 0) return;
    buf[0] = '\0';
    if (loc == NULL) return;
    CFStringRef ident = CFLocaleGetIdentifier(loc);
    if (ident == NULL) return;
    CFStringGetCString(ident, buf, (CFIndex)cap, kCFStringEncodingUTF8);
}

static int probe(const char *label, CFLocaleRef loc)
{
    char name[256];
    locale_name(loc, name, sizeof(name));
    CFDateFormatterRef formatter = CFDateFormatterCreate(kCFAllocatorDefault, loc,
        kCFDateFormatterFullStyle, kCFDateFormatterFullStyle);
    printf("FCD_PROBE label=%s locale=%s locale_ptr=%p formatter=%p\n",
        label, name, (void *)loc, (void *)formatter);
    fflush(stdout);
    if (formatter == NULL) {
        printf("FCD_FORMATTER_NULL label=%s locale=%s\n", label, name);
        fflush(stdout);
        return 0;
    }

    CFTimeZoneRef tz = CFTimeZoneCreateWithTimeIntervalFromGMT(kCFAllocatorDefault, 0.0);
    printf("FCD_TIMEZONE label=%s tz=%p key=%s\n", label, (void *)tz,
        "kCFDateFormatterTimeZoneKey");
    fflush(stdout);
    if (tz != NULL) {
        CFDateFormatterSetProperty(formatter, kCFDateFormatterTimeZone, tz);
        CFRelease(tz);
    }
    CFDateFormatterSetFormat(formatter, CFSTR("yyyy-MM-dd HH:mm:ss '+0000'"));
    CFDateRef date = CFDateCreate(kCFAllocatorDefault, 0.0);
    CFStringRef text = CFDateFormatterCreateStringWithDate(kCFAllocatorDefault, formatter, date);
    char out[256] = "<null>";
    if (text != NULL) {
        CFStringGetCString(text, out, (CFIndex)sizeof(out), kCFStringEncodingUTF8);
        CFRelease(text);
    }
    printf("FCD_DESCRIPTION label=%s text=%s\n", label, out);
    fflush(stdout);
    if (date != NULL) CFRelease(date);
    CFRelease(formatter);
    return 1;
}

int main(void)
{
    CFLocaleRef current = CFLocaleCopyCurrent();
    CFLocaleRef system = CFLocaleGetSystem();
    CFLocaleRef en_us = CFLocaleCreate(kCFAllocatorDefault, CFSTR("en_US"));
    CFLocaleRef posix = CFLocaleCreate(kCFAllocatorDefault, CFSTR("en_US_POSIX"));

    printf("FCD_CURRENT_PTR=%p SYSTEM_PTR=%p EN_US_PTR=%p POSIX_PTR=%p\n",
        (void *)current, (void *)system, (void *)en_us, (void *)posix);
    fflush(stdout);

    int current_ok = probe("current", current);
    probe("system", system);
    probe("en_US", en_us);
    probe("en_US_POSIX", posix);

    if (en_us != NULL) CFRelease(en_us);
    if (posix != NULL) CFRelease(posix);
    if (current != NULL) CFRelease(current);

    if (!current_ok) {
        printf("FCD_DATE_FAIL current_formatter_create=NULL\n");
        fflush(stdout);
        return 2;
    }
    printf("FCD_DATE_OK\n");
    fflush(stdout);
    return 0;
}

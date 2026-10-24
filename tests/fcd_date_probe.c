/*
 * Guest oracle for the filecoordinationd date-description crash.
 *
 * It exercises the public CFDateFormatterCreate/SetProperty/SetFormat/CreateStringWithDate
 * path and then the original NSDate -description route. The oracle is the
 * deterministic formatted value for CFAbsoluteTime 0 with an explicit UTC
 * timezone and the exact NSDate format:
 *
 *     2001-01-01 00:00:00 +0000
 *
 * All four intended locale cases are aggregated. A NULL formatter, missing
 * timezone/date/text, or a different formatted value fails the fixture. No
 * fallback formatter or NULL no-op is accepted.
 */
#import <Foundation/Foundation.h>
#include <CoreFoundation/CoreFoundation.h>
#include <CommonCrypto/CommonDigest.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <sys/stat.h>
#include <errno.h>

static const char *kExpected = "2001-01-01 00:00:00 +0000";
static const char *kDataPath = "/usr/share/icu/icudt66l.dat";
static const char *kDataExpectedSha256 =
    "6b6a6faf4630a9ba0703a5da8ba28905edd3dd5dc790c98126a220e2d549367b";
static const uint64_t kDataExpectedSize = 29074368;

static int check_data_file(void)
{
    FILE *f = fopen(kDataPath, "rb");
    if (f == NULL) {
        printf("FCD_DATA_FAIL path=%s reason=open errno=%d\n", kDataPath, errno);
        return 0;
    }

    struct stat st;
    if (fstat(fileno(f), &st) != 0) {
        printf("FCD_DATA_FAIL path=%s reason=stat errno=%d\n", kDataPath, errno);
        fclose(f);
        return 0;
    }

    CC_SHA256_CTX ctx;
    CC_SHA256_Init(&ctx);
    unsigned char buf[65536];
    uint64_t total = 0;
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        CC_SHA256_Update(&ctx, buf, (CC_LONG)n);
        total += n;
    }
    if (ferror(f)) {
        printf("FCD_DATA_FAIL path=%s reason=read errno=%d\n", kDataPath, errno);
        fclose(f);
        return 0;
    }
    fclose(f);

    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &ctx);
    char hex[CC_SHA256_DIGEST_LENGTH * 2 + 1];
    for (size_t i = 0; i < CC_SHA256_DIGEST_LENGTH; i++)
        snprintf(hex + i * 2, 3, "%02x", digest[i]);
    hex[CC_SHA256_DIGEST_LENGTH * 2] = '\0';

    printf("FCD_DATA path=%s size=%llu expected_size=%llu sha256=%s expected_sha256=%s\n",
           kDataPath, (unsigned long long)total,
           (unsigned long long)kDataExpectedSize, hex, kDataExpectedSha256);
    fflush(stdout);

    int ok = ((uint64_t)st.st_size == kDataExpectedSize) &&
             (total == kDataExpectedSize) &&
             (strcmp(hex, kDataExpectedSha256) == 0);
    if (!ok) {
        printf("FCD_DATA_FAIL path=%s reason=mismatch size=%llu sha256=%s\n",
               kDataPath, (unsigned long long)total, hex);
    }
    return ok;
}

static void locale_name(CFLocaleRef loc, char *buf, size_t cap)
{
    if (buf == NULL || cap == 0) return;
    buf[0] = '\0';
    if (loc == NULL) return;
    CFStringRef ident = CFLocaleGetIdentifier(loc);
    if (ident == NULL) return;
    CFStringGetCString(ident, buf, (CFIndex)cap, kCFStringEncodingUTF8);
}

static int check_probe(const char *label, CFLocaleRef loc)
{
    char name[256];
    locale_name(loc, name, sizeof(name));
    if (loc == NULL) {
        printf("FCD_FAIL label=%s reason=null_locale\n", label);
        return 0;
    }

    CFDateFormatterRef formatter = CFDateFormatterCreate(kCFAllocatorDefault, loc,
        kCFDateFormatterFullStyle, kCFDateFormatterFullStyle);
    if (formatter == NULL) {
        printf("FCD_FAIL label=%s locale=%s reason=formatter=NULL\n", label, name);
        return 0;
    }

    CFTimeZoneRef tz = CFTimeZoneCreateWithTimeIntervalFromGMT(kCFAllocatorDefault, 0.0);
    if (tz == NULL) {
        printf("FCD_FAIL label=%s locale=%s reason=timezone=NULL\n", label, name);
        CFRelease(formatter);
        return 0;
    }
    CFDateFormatterSetProperty(formatter, kCFDateFormatterTimeZone, tz);
    CFRelease(tz);
    CFDateFormatterSetFormat(formatter, CFSTR("yyyy-MM-dd HH:mm:ss '+0000'"));

    CFDateRef date = CFDateCreate(kCFAllocatorDefault, 0.0);
    if (date == NULL) {
        printf("FCD_FAIL label=%s locale=%s reason=date=NULL\n", label, name);
        CFRelease(formatter);
        return 0;
    }
    CFStringRef text = CFDateFormatterCreateStringWithDate(kCFAllocatorDefault, formatter, date);
    if (text == NULL) {
        printf("FCD_FAIL label=%s locale=%s reason=text=NULL\n", label, name);
        CFRelease(date);
        CFRelease(formatter);
        return 0;
    }

    char out[256] = "";
    Boolean got = CFStringGetCString(text, out, (CFIndex)sizeof(out), kCFStringEncodingUTF8);
    printf("FCD_PROBE label=%s locale=%s formatter=%p date=%p text=%s expected=%s\n",
           label, name, (void *)formatter, (void *)date, got ? out : "<null>", kExpected);
    fflush(stdout);

    int ok = got && strcmp(out, kExpected) == 0;
    if (!ok) {
        printf("FCD_FAIL label=%s locale=%s reason=format_mismatch got=%s expected=%s\n",
               label, name, got ? out : "<null>", kExpected);
    }

    CFRelease(text);
    CFRelease(date);
    CFRelease(formatter);
    return ok;
}

int main(void)
{
    int all_ok = 1;
    int current_ok = 0;

    all_ok &= check_data_file();

    CFLocaleRef current = CFLocaleCopyCurrent();
    CFLocaleRef system = CFLocaleGetSystem();
    CFLocaleRef en_us = CFLocaleCreate(kCFAllocatorDefault, CFSTR("en_US"));
    CFLocaleRef posix = CFLocaleCreate(kCFAllocatorDefault, CFSTR("en_US_POSIX"));

    current_ok = check_probe("current", current);
    all_ok &= current_ok;
    all_ok &= check_probe("system", system);
    all_ok &= check_probe("en_US", en_us);
    all_ok &= check_probe("en_US_POSIX", posix);

    if (current != NULL) CFRelease(current);
    if (en_us != NULL) CFRelease(en_us);
    if (posix != NULL) CFRelease(posix);

    if (current_ok) {
        @autoreleasepool {
            NSDate *date = [NSDate dateWithTimeIntervalSinceReferenceDate:0.0];
            NSString *description = [date description];
            const char *text = description ? [description UTF8String] : NULL;
            printf("NSDATE_DESCRIPTION text=%s expected=%s\n",
                   text ? text : "<null>", kExpected);
            fflush(stdout);
            if (text == NULL || strcmp(text, kExpected) != 0) {
                printf("NSDATE_FAIL reason=format_mismatch\n");
                all_ok = 0;
            }
        }
    } else {
        printf("NSDATE_SKIPPED reason=current_cf_formatter_unavailable\n");
    }

    if (!all_ok) {
        printf("FCD_DATE_FAIL\n");
        fflush(stdout);
        return 2;
    }
    printf("FCD_DATE_OK\n");
    fflush(stdout);
    return 0;
}

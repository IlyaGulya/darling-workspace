/*
 * Prepared error-preserving ICU discriminator. Do not launch before the
 * data-ownership scout returns.
 *
 * Pattern mode takes udat.cpp's else branch, which passes this fresh status
 * directly into SimpleDateFormat; udat.cpp preserves that constructor error.
 * The resource checks use the existing ures_openDirect declarations and fresh
 * statuses. MissingResource (2) / InvalidFormat (3) can therefore be
 * distinguished from the style factory's synthetic 7. A 7 here still does not
 * prove OOM and no fallback/allocator change is authorized.
 */
#include <unicode/udat.h>
#include <unicode/ures.h>
#include <unicode/utypes.h>
#include <stdio.h>

static void print_status(const char *label, UErrorCode status, const void *ptr)
{
    printf("%s ptr=%p status=%d name=%s\n",
           label, ptr, (int)status, u_errorName(status));
    fflush(stdout);
}

int main(void)
{
    UErrorCode status = U_ZERO_ERROR;
    const UChar pattern[] = {'y', 'y', 'y', 'y'};
    UDateFormat *df = udat_open(UDAT_PATTERN, UDAT_PATTERN, "en_US",
                                NULL, 0, pattern, 4, &status);
    print_status("ICU_PATTERN_UDAT", status, df);
    if (df != NULL) {
        udat_close(df);
    }

    status = U_ZERO_ERROR;
    UResourceBundle *root = ures_openDirect(NULL, "root", &status);
    print_status("ICU_URES_ROOT", status, root);
    if (root != NULL) {
        ures_close(root);
    }

    status = U_ZERO_ERROR;
    UResourceBundle *supplemental = ures_openDirect(NULL, "supplementalData", &status);
    print_status("ICU_URES_SUPPLEMENTAL", status, supplemental);
    if (supplemental != NULL) {
        ures_close(supplemental);
    }

    printf("FCD_ICU_DISCRIMINATED\n");
    fflush(stdout);
    return 0;
}

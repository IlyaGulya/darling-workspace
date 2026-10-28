#ifndef EUNION_HOST_KAUTH_H
#define EUNION_HOST_KAUTH_H

#ifndef TEST
#error "E-UNION native ABI adapters are host-test-only"
#endif

#include <sys/types.h>

/* The only kauth declarations consumed by fchmod_extended.c. These ABI
 * sentinels are from the selected tree's bsd/sys/kauth.h; its full header
 * depends on Darwin-only machine, Mach and private sys/_types headers.
 * This adapter supplies no ACL implementation or guest runtime behavior. */
#define KAUTH_UID_NONE (~(uid_t)0 - 100)
#define KAUTH_GID_NONE (~(gid_t)0 - 100)
_Static_assert(sizeof(uid_t) == 4 && sizeof(gid_t) == 4,
    "Darwin ownership sentinel widths");

#endif

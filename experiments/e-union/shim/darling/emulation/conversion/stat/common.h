#ifndef EUNION_HOST_STAT_COMMON_H
#define EUNION_HOST_STAT_COMMON_H

/* glibc's statx headers already own __kernel_{u,}long_t as long, whereas
 * Darling's Darwin build declares equally wide long-long typedefs. Keep the
 * production structures verbatim and namespace only those two typedef names. */
#include <sys/stat.h>
#include <stddef.h>
#define __kernel_ulong_t eunion_host_kernel_ulong_t
#define __kernel_long_t eunion_host_kernel_long_t
#include_next <darling/emulation/conversion/stat/common.h>
#undef __kernel_ulong_t
#undef __kernel_long_t

#if defined(__x86_64__)
_Static_assert(sizeof(struct linux_stat) == sizeof(struct stat), "native Linux stat size");
_Static_assert(_Alignof(struct linux_stat) == _Alignof(struct stat), "native Linux stat alignment");
#define EUNION_STAT_OFFSET(field) \
    _Static_assert(offsetof(struct linux_stat, field) == offsetof(struct stat, field), "native Linux stat " #field)
EUNION_STAT_OFFSET(st_dev);
EUNION_STAT_OFFSET(st_ino);
EUNION_STAT_OFFSET(st_nlink);
EUNION_STAT_OFFSET(st_mode);
EUNION_STAT_OFFSET(st_uid);
EUNION_STAT_OFFSET(st_gid);
EUNION_STAT_OFFSET(st_rdev);
EUNION_STAT_OFFSET(st_size);
EUNION_STAT_OFFSET(st_blksize);
EUNION_STAT_OFFSET(st_blocks);
#undef EUNION_STAT_OFFSET
_Static_assert(offsetof(struct linux_stat, st_atime) == offsetof(struct stat, st_atim), "native Linux stat atime");
_Static_assert(offsetof(struct linux_stat, st_mtime) == offsetof(struct stat, st_mtim), "native Linux stat mtime");
_Static_assert(offsetof(struct linux_stat, st_ctime) == offsetof(struct stat, st_ctim), "native Linux stat ctime");
#endif

#endif

#ifndef EUNION_TEST_GUARDED_TABLE_H
#define EUNION_TEST_GUARDED_TABLE_H

typedef unsigned int guard_flags_t;
enum { guard_flag_prevent_close = 1u };

static inline int guard_table_check(int fd, guard_flags_t flags)
{
    (void)fd;
    (void)flags;
    return 0;
}

#endif

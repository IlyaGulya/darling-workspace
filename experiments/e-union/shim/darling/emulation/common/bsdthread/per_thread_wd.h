#ifndef EUNION_TEST_PER_THREAD_WD_H
#define EUNION_TEST_PER_THREAD_WD_H

extern int eunion_test_perthread_wd;

static inline int get_perthread_wd(void)
{
    return eunion_test_perthread_wd;
}

#endif

#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

__attribute__((constructor)) static void reserve_stack_obstacle(void)
{
    if (!getenv("DARLING_STACK_GROWTH_PROBE"))
        return;

    char executable[4096];
    ssize_t length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    if (length < 0 || length == sizeof(executable) - 1) {
        fprintf(stderr, "STACK_GROWTH_OBSTACLE_FAILED readlink errno=%d\n",
            length < 0 ? errno : ENAMETOOLONG);
        _exit(90);
    }
    executable[length] = '\0';
    const char *name = strrchr(executable, '/');
    if (!name || strcmp(name, "/mldr") != 0) {
        fprintf(stderr, "STACK_GROWTH_OBSTACLE_FAILED expected mldr, got %s\n", executable);
        _exit(90);
    }

    /* The original RED/GREEN oracle: a real page below the preferred guest
     * stack obstructs growth when mldr reserves only its initial 64 KiB. */
    void *address = (void *)0x7fffffde0000UL;
    void *mapping = mmap(address, 4096, PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    if (mapping != address) {
        fprintf(stderr, "STACK_GROWTH_OBSTACLE_FAILED mmap errno=%d address=%p\n",
            mapping == MAP_FAILED ? errno : 0, mapping);
        _exit(91);
    }

    static const char ready[] = "STACK_GROWTH_OBSTACLE_READY\n";
    if (write(STDERR_FILENO, ready, sizeof(ready) - 1) != sizeof(ready) - 1) {
        fprintf(stderr, "STACK_GROWTH_OBSTACLE_FAILED readiness write errno=%d\n", errno);
        _exit(92);
    }
}

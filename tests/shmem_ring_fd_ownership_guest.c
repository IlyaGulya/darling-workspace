#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void) {
    (void)mach_host_self();
    /* Shells and daemon launchers close descriptors they do not inherit. */
    for (int fd = 3; fd < 32; ++fd) close(fd);
    int pipes[16][2];
    for (int i = 0; i < 16; ++i) {
        if (pipe(pipes[i]) != 0) { perror("pipe"); return 1; }
    }
    pid_t child = fork();
    if (child < 0) { perror("fork"); return 1; }
    if (!child) {
        for (int i = 0; i < 16; ++i) {
            for (int j = 0; j < 2; ++j) {
                if (fcntl(pipes[i][j], F_GETFD) < 0) {
                    fprintf(stderr, "RING_FD_INHERITANCE_LOST fd=%d errno=%d\n", pipes[i][j], errno);
                    _exit(1);
                }
            }
        }
        _exit(0);
    }
    int status;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) return 1;
    for (int i = 0; i < 16; ++i) { close(pipes[i][0]); close(pipes[i][1]); }
    puts("RING_FD_INHERITANCE_OK");
    return 0;
}

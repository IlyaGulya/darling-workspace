#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t signal_closes;
static volatile sig_atomic_t signal_close_failed;

static void close_from_signal(int signo) {
    (void)signo;
    int saved_errno = errno;
    if (close(-1) != -1 || errno != EBADF) signal_close_failed = 1;
    ++signal_closes;
    errno = saved_errno;
}

static int check_signal_reentrancy(void) {
    struct sigaction action = {0};
    action.sa_handler = close_from_signal;
    action.sa_flags = SA_RESTART;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGALRM, &action, NULL) != 0) { perror("sigaction"); return 1; }
    struct itimerval timer = {{0, 1000}, {0, 1000}};
    if (setitimer(ITIMER_REAL, &timer, NULL) != 0) { perror("setitimer"); return 1; }
    /* close is async-signal-safe, including when it interrupts another close. */
    while (signal_closes < 1024) {
        if (close(-1) != -1 || errno != EBADF) signal_close_failed = 1;
    }
    timer = (struct itimerval){{0, 0}, {0, 0}};
    if (setitimer(ITIMER_REAL, &timer, NULL) != 0) { perror("setitimer stop"); return 1; }
    if (signal_close_failed) {
        fputs("RING_FD_SIGNAL_CLOSE_FAILED\n", stderr);
        return 1;
    }
    puts("RING_FD_SIGNAL_REENTRANCY_OK");
    return 0;
}

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
    if (check_signal_reentrancy() != 0) return 1;
    puts("RING_FD_INHERITANCE_OK");
    return 0;
}

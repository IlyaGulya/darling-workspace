#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile sig_atomic_t notifications;
static void child_signal(int signo) { (void)signo; ++notifications; }

int main(void) {
    struct sigaction action = {0};
    action.sa_handler = child_signal;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGCHLD, &action, NULL)) return 2;
    sigset_t blocked, previous, pending;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGCHLD);
    if (sigprocmask(SIG_BLOCK, &blocked, &previous)) return 3;
    char *missing[] = { "/private/var/tmp/west-nonexistent-spawn-executable", NULL };
    pid_t pid = -1;
    int result = posix_spawn(&pid, missing[0], NULL, NULL, missing, environ);
    int failed = result != ENOENT;
    printf("FAILED_SPAWN result=%d expected=%d\n", result, ENOENT);
    int status;
    errno = 0;
    pid_t leaked = waitpid(-1, &status, 0);
    int wait_error = errno;
    printf("FAILED_SPAWN_CHILD pid=%d errno=%d\n", (int)leaked, wait_error);
    if (leaked != -1 || wait_error != ECHILD) failed = 1;
    if (sigpending(&pending)) return 4;
    int pending_child = sigismember(&pending, SIGCHLD);
    if (pending_child) failed = 1;
    if (sigprocmask(SIG_SETMASK, &previous, NULL)) return 5;
    printf("FAILED_SPAWN_SIGNAL pending=%d delivered=%d\n", pending_child, (int)notifications);
    if (notifications) failed = 1;

    /* A successful spawn must still notify and remain waitable by its caller. */
    if (sigprocmask(SIG_BLOCK, &blocked, &previous)) return 6;
    notifications = 0;
    char *success[] = { "/bin/sleep", "0", NULL };
    result = posix_spawn(&pid, success[0], NULL, NULL, success, environ);
    if (result) { printf("SUCCESSFUL_SPAWN_ERROR=%d\n", result); return 7; }
    while (!notifications) sigsuspend(&previous);
    if (sigprocmask(SIG_SETMASK, &previous, NULL)) return 8;
    if (waitpid(pid, &status, 0) != pid || !WIFEXITED(status) || WEXITSTATUS(status)) return 9;
    printf("SUCCESSFUL_SPAWN_SIGNAL delivered=%d\n", (int)notifications);
    if (failed) { puts("POSIX_SPAWN_FAILURE_OWNERSHIP_BROKEN"); return 1; }
    puts("POSIX_SPAWN_FAILURE_OWNERSHIP_OK");
    return 0;
}

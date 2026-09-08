#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static volatile sig_atomic_t observed;

static void observe_mask(int signum)
{
    (void)signum;
    int saved_errno = errno;
    sigset_t mask;
    if (sigprocmask(SIG_BLOCK, NULL, &mask) != 0) {
        observed = -1;
    } else {
        observed = sigismember(&mask, SIGUSR1) |
            (sigismember(&mask, SIGUSR2) << 1) |
            (sigismember(&mask, SIGCHLD) << 2);
    }
    errno = saved_errno;
}

static void check(int result, const char *operation)
{
    if (result == -1) {
        perror(operation);
        exit(2);
    }
}

static void exercise(int flags, int explicit_self_mask, int expected)
{
    struct sigaction action = {0};
    action.sa_handler = observe_mask;
    action.sa_flags = flags;
    check(sigemptyset(&action.sa_mask), "sigemptyset");
    check(sigaddset(&action.sa_mask, SIGCHLD), "mask SIGCHLD in handler");
    if (explicit_self_mask)
        check(sigaddset(&action.sa_mask, SIGUSR1), "explicitly mask handler signal");
    check(sigaction(SIGUSR1, &action, NULL), "install handler");
    observed = -2;
    check(kill(getpid(), SIGUSR1), "deliver signal");
    if (observed != expected) {
        fprintf(stderr, "handler mask flags=%d explicit_self=%d expected=%d actual=%d\n",
            flags, explicit_self_mask, expected, (int)observed);
        puts("SIGNAL_HANDLER_MASK_BROKEN");
        exit(1);
    }
    sigset_t after;
    check(sigprocmask(SIG_BLOCK, NULL, &after), "read restored mask");
    if (sigismember(&after, SIGUSR1) != 0 ||
        sigismember(&after, SIGUSR2) != 1 ||
        sigismember(&after, SIGCHLD) != 0) {
        fputs("interrupted mask was not restored after handler return\n", stderr);
        puts("SIGNAL_HANDLER_MASK_BROKEN");
        exit(1);
    }
}

int main(void)
{
    sigset_t blocked;
    check(sigemptyset(&blocked), "sigemptyset");
    check(sigaddset(&blocked, SIGUSR2), "block unrelated SIGUSR2");
    check(sigprocmask(SIG_SETMASK, &blocked, NULL), "set interrupted mask");
    exercise(0, 0, 7);
    exercise(SA_NODEFER, 0, 6);
    exercise(SA_NODEFER, 1, 7);
    puts("SIGNAL_HANDLER_MASK_OK");
    return 0;
}

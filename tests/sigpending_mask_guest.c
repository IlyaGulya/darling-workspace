#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static volatile sig_atomic_t delivered;

static void receive_signal(int signum)
{
    delivered = signum;
}

static void require_mask(int expected)
{
    sigset_t pending;
    if (expected)
        sigemptyset(&pending);
    else
        sigfillset(&pending);
    if (sigpending(&pending)) {
        perror("sigpending");
        exit(2);
    }
    if (sigismember(&pending, SIGUSR1) != expected ||
        sigismember(&pending, SIGUSR2) != 0) {
        fprintf(stderr, "pending: expected_usr1=%d actual_usr1=%d actual_usr2=%d\n",
            expected, sigismember(&pending, SIGUSR1), sigismember(&pending, SIGUSR2));
        puts("SIGPENDING_MASK_COPYOUT_BROKEN");
        exit(1);
    }
}

int main(void)
{
    struct sigaction action = {0};
    action.sa_handler = receive_signal;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGUSR1, &action, NULL)) {
        perror("sigaction");
        return 2;
    }
    sigset_t blocked;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGUSR1);
    if (sigprocmask(SIG_BLOCK, &blocked, NULL)) {
        perror("sigprocmask");
        return 2;
    }
    require_mask(0);
    if (kill(getpid(), SIGUSR1)) {
        perror("kill");
        return 2;
    }
    require_mask(1);
    if (sigprocmask(SIG_UNBLOCK, &blocked, NULL)) {
        perror("sigprocmask");
        return 2;
    }
    if (delivered != SIGUSR1) {
        fputs("SIGUSR1 was not delivered after unblocking\n", stderr);
        return 2;
    }
    require_mask(0);
    puts("SIGPENDING_MASK_COPYOUT_OK");
    return 0;
}

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

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
        puts("SIGPENDING_MASK_COPYOUT_BROKEN");
        exit(1);
    }
}

int main(void)
{
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
    struct sigaction ignore = {0};
    ignore.sa_handler = SIG_IGN;
    sigemptyset(&ignore.sa_mask);
    if (sigaction(SIGUSR1, &ignore, NULL)) {
        perror("sigaction");
        return 2;
    }
    require_mask(0);
    puts("SIGPENDING_MASK_COPYOUT_OK");
    return 0;
}

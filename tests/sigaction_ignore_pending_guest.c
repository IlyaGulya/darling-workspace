#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t usr1_deliveries;
static volatile sig_atomic_t usr2_deliveries;

static void receive_signal(int signum)
{
    if (signum == SIGUSR1)
        ++usr1_deliveries;
    else if (signum == SIGUSR2)
        ++usr2_deliveries;
}

static void timeout_signal(int signum)
{
    (void)signum;
    static const char message[] = "SIGACTION_IGNORE_PENDING_TIMEOUT\n";
    (void)write(STDERR_FILENO, message, sizeof(message) - 1);
    _exit(3);
}

static void check(int result, const char *operation)
{
    if (result == -1) {
        perror(operation);
        exit(2);
    }
}

static void require_pending(int usr1, int usr2, const char *phase)
{
    sigset_t pending;
    check(sigfillset(&pending), "sigfillset");
    check(sigpending(&pending), "sigpending");
    int actual_usr1 = sigismember(&pending, SIGUSR1);
    int actual_usr2 = sigismember(&pending, SIGUSR2);
    if (actual_usr1 != usr1 || actual_usr2 != usr2) {
        fprintf(stderr, "%s: expected pending USR1=%d USR2=%d, got USR1=%d USR2=%d\n",
            phase, usr1, usr2, actual_usr1, actual_usr2);
        exit(1);
    }
}

static void wait_for_delivery(int signum, volatile sig_atomic_t *deliveries)
{
    sigset_t wait_mask;
    check(sigprocmask(SIG_BLOCK, NULL, &wait_mask), "read signal mask");
    check(sigdelset(&wait_mask, signum), "sigdelset");
    while (*deliveries == 0) {
        if (sigsuspend(&wait_mask) != -1 || errno != EINTR) {
            fputs("sigsuspend did not return EINTR\n", stderr);
            exit(2);
        }
    }
    if (*deliveries != 1) {
        fputs("signal was delivered more than once\n", stderr);
        exit(1);
    }
}

int main(void)
{
    struct sigaction action = {0};
    check(sigemptyset(&action.sa_mask), "sigemptyset");
    action.sa_handler = timeout_signal;
    check(sigaction(SIGALRM, &action, NULL), "install watchdog");
    action.sa_handler = receive_signal;
    check(sigaction(SIGUSR1, &action, NULL), "install SIGUSR1 handler");
    check(sigaction(SIGUSR2, &action, NULL), "install SIGUSR2 handler");

    sigset_t blocked;
    check(sigemptyset(&blocked), "sigemptyset");
    check(sigaddset(&blocked, SIGUSR1), "sigaddset SIGUSR1");
    check(sigaddset(&blocked, SIGUSR2), "sigaddset SIGUSR2");
    check(sigprocmask(SIG_SETMASK, &blocked, NULL), "block user signals");
    alarm(10);

    require_pending(0, 0, "initial state");
    check(kill(getpid(), SIGUSR1), "queue SIGUSR1");
    check(kill(getpid(), SIGUSR2), "queue SIGUSR2");
    require_pending(1, 1, "before ignore");

    action.sa_handler = SIG_IGN;
    check(sigaction(SIGUSR1, &action, NULL), "ignore SIGUSR1");
    sigset_t pending;
    check(sigemptyset(&pending), "sigemptyset");
    check(sigpending(&pending), "sigpending after ignore");
    if (sigismember(&pending, SIGUSR1) != 0) {
        puts("SIGACTION_IGNORE_PENDING_NOT_DISCARDED");
        return 1;
    }
    require_pending(0, 1, "after ignore");

    action.sa_handler = receive_signal;
    check(sigaction(SIGUSR1, &action, NULL), "leave ignore");
    require_pending(0, 1, "after reinstalling handler");
    sigset_t usr1_mask;
    check(sigemptyset(&usr1_mask), "sigemptyset");
    check(sigaddset(&usr1_mask, SIGUSR1), "sigaddset SIGUSR1");
    check(sigprocmask(SIG_UNBLOCK, &usr1_mask, NULL), "unblock discarded signal");
    if (usr1_deliveries != 0 || usr2_deliveries != 0) {
        fputs("discarded or unrelated signal was delivered\n", stderr);
        return 1;
    }
    require_pending(0, 1, "after unblocking discarded signal");

    check(sigprocmask(SIG_BLOCK, &usr1_mask, NULL), "reblock SIGUSR1");
    check(kill(getpid(), SIGUSR1), "queue fresh SIGUSR1");
    require_pending(1, 1, "fresh signal after leaving ignore");
    wait_for_delivery(SIGUSR1, &usr1_deliveries);
    require_pending(0, 1, "after fresh SIGUSR1 delivery");
    if (usr2_deliveries != 0) {
        fputs("unrelated blocked signal was delivered early\n", stderr);
        return 1;
    }
    wait_for_delivery(SIGUSR2, &usr2_deliveries);
    require_pending(0, 0, "after SIGUSR2 delivery");

    pid_t child = fork();
    check(child, "fork default-disposition child");
    if (child == 0) {
        action.sa_handler = SIG_IGN;
        check(sigaction(SIGUSR1, &action, NULL), "child ignore");
        action.sa_handler = SIG_DFL;
        check(sigaction(SIGUSR1, &action, NULL), "child restore default");
        check(sigprocmask(SIG_UNBLOCK, &usr1_mask, NULL), "child unblock");
        check(kill(getpid(), SIGUSR1), "child raise default signal");
        _exit(99);
    }
    int status;
    pid_t waited;
    do {
        waited = waitpid(child, &status, 0);
    } while (waited == -1 && errno == EINTR);
    check(waited, "wait default-disposition child");
    if (!WIFSIGNALED(status) || WTERMSIG(status) != SIGUSR1) {
        fputs("SIG_IGN to SIG_DFL did not restore default termination\n", stderr);
        return 1;
    }

    alarm(0);
    puts("SIGACTION_IGNORE_PENDING_OK");
    return 0;
}

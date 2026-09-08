#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <sched.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

/* The same executable supplies every successful child on both runtimes. */
enum { CHILD_STATUS = 37, FAST_SPAWNS = 64 };
static volatile sig_atomic_t notifications;
static volatile sig_atomic_t handler_reaping;
static volatile sig_atomic_t handler_pid;
static volatile sig_atomic_t handler_status;
static volatile sig_atomic_t handler_error;
static sigset_t delivery_mask;
static char fixture_dir[PATH_MAX];
static char missing_path[PATH_MAX];
static char executable[PATH_MAX];
static pid_t owned_child = -1;

static void child_signal(int signo)
{
    int saved_errno = errno;
    (void)signo;
    ++notifications;
    if (handler_reaping) {
        int status = 0;
        pid_t pid;
        do {
            pid = waitpid(-1, &status, WNOHANG);
        } while (pid < 0 && errno == EINTR);
        handler_pid = pid;
        handler_status = status;
        handler_error = pid < 0 ? errno : 0;
    }
    errno = saved_errno;
}

static void cleanup(void)
{
    if (owned_child > 0 && !(handler_reaping && handler_pid == owned_child)) {
        (void)kill(owned_child, SIGKILL);
        while (waitpid(owned_child, NULL, 0) < 0 && errno == EINTR) {}
    }
    if (fixture_dir[0]) (void)rmdir(fixture_dir);
}

static void infrastructure_error(const char *operation, int error)
{
    fprintf(stderr, "POSIX_SPAWN_FAILURE_OWNERSHIP_INFRA_ERROR: %s: %s (%d)\n",
            operation, strerror(error), error);
    exit(2);
}

static void broken(const char *assertion, long observed, long expected)
{
    fprintf(stderr, "%s: observed=%ld expected=%ld\n", assertion, observed, expected);
    puts("POSIX_SPAWN_FAILURE_OWNERSHIP_BROKEN");
    exit(1);
}

static int pending_child_signal(void)
{
    sigset_t pending;
    if (sigpending(&pending)) infrastructure_error("sigpending", errno);
    int result = sigismember(&pending, SIGCHLD);
    if (result < 0) infrastructure_error("sigismember", errno);
    return result;
}

static void require_pending(int expected)
{
    int pending = pending_child_signal();
    if (pending != expected) broken("pending SIGCHLD", pending, expected);
}

static void require_no_children(void)
{
    int status;
    pid_t result;
    do {
        result = waitpid(-1, &status, 0);
    } while (result < 0 && errno == EINTR);
    int error = errno;
    /* macOS may still be removing its private failed child after spawn
     * returns. Wait for the kernel's verdict, not a WNOHANG scheduling snapshot. */
    if (result != -1) broken("unexpected waitable child", result, -1);
    if (error != ECHILD) infrastructure_error("waitpid without children", error);
}

static void failed_spawn(int expected_pending)
{
    char *arguments[] = { missing_path, NULL };
    pid_t unused_pid = -1;
    int result = posix_spawn(&unused_pid, missing_path, NULL, NULL, arguments, environ);
    /* POSIX leaves the pid output unspecified on failure; do not assert it. */
    if (result != ENOENT) broken("failed spawn errno", result, ENOENT);
    require_pending(expected_pending);
}

static pid_t spawn_child(const char *mode, const posix_spawn_file_actions_t *actions)
{
    char *arguments[] = { executable, (char *)mode, NULL };
    pid_t pid;
    int result = posix_spawn(&pid, executable, actions, NULL, arguments, environ);
    if (result) infrastructure_error("spawn fixture child", result);
    if (pid <= 0) broken("successful spawn pid", pid, 1);
    owned_child = pid;
    return pid;
}

static void receive_notification(sig_atomic_t before)
{
    /* Deliver exactly the expected successful child's notification. This is
     * never called to clear a failed spawn's signal before testing privacy. */
    while (notifications == before) {
        errno = 0;
        if (sigsuspend(&delivery_mask) != -1 || errno != EINTR)
            infrastructure_error("sigsuspend", errno);
    }
    if (notifications != before + 1)
        broken("successful child notifications", notifications, before + 1);
    require_pending(0);
}

static void reap_child(pid_t pid)
{
    int status;
    pid_t result;
    do {
        result = waitpid(pid, &status, 0);
    } while (result < 0 && errno == EINTR);
    if (result != pid) broken("successful child remains waitable", result, pid);
    owned_child = -1;
    if (!WIFEXITED(status)) broken("successful child normal exit", status, CHILD_STATUS << 8);
    if (WEXITSTATUS(status) != CHILD_STATUS)
        broken("successful child exit status", WEXITSTATUS(status), CHILD_STATUS);
    require_no_children();
    require_pending(0);
}

static void transfer_byte(int fd, int writing, char expected)
{
    char byte = expected;
    ssize_t result;
    do {
        result = writing ? write(fd, &byte, 1) : read(fd, &byte, 1);
    } while (result < 0 && errno == EINTR);
    if (result != 1) infrastructure_error(writing ? "handshake write" : "handshake read",
                                          result < 0 ? errno : EPIPE);
    if (byte != expected) infrastructure_error("handshake token", EPROTO);
}

static void close_fd(int fd)
{
    if (close(fd)) infrastructure_error("close pipe", errno);
}

static void handshake(void)
{
    int release[2], ready[2];
    if (pipe(release) || pipe(ready)) infrastructure_error("handshake pipes", errno);
    posix_spawn_file_actions_t actions;
    int result = posix_spawn_file_actions_init(&actions);
    if (result) infrastructure_error("file actions init", result);
    result = posix_spawn_file_actions_adddup2(&actions, release[0], STDIN_FILENO);
    if (result) infrastructure_error("file actions stdin", result);
    result = posix_spawn_file_actions_adddup2(&actions, ready[1], STDOUT_FILENO);
    if (result) infrastructure_error("file actions stdout", result);
    int descriptors[] = { release[0], release[1], ready[0], ready[1] };
    for (unsigned i = 0; i < sizeof(descriptors) / sizeof(descriptors[0]); ++i) {
        result = posix_spawn_file_actions_addclose(&actions, descriptors[i]);
        if (result) infrastructure_error("file actions close", result);
    }

    sig_atomic_t before = notifications;
    pid_t pid = spawn_child("--handshake-child", &actions);
    result = posix_spawn_file_actions_destroy(&actions);
    if (result) infrastructure_error("file actions destroy", result);
    close_fd(release[0]);
    close_fd(ready[1]);
    transfer_byte(ready[0], 0, 'R');
    close_fd(ready[0]);

    /* The child cannot exit normally until this parent, after posix_spawn
     * returns, releases it. No elapsed-time or scheduling threshold is used. */
    int status;
    pid_t waited = waitpid(pid, &status, WNOHANG);
    if (waited != 0) {
        if (waited == pid) owned_child = -1;
        broken("handshake child alive when spawn returns", waited, 0);
    }
    require_pending(0);
    transfer_byte(release[1], 1, 'G');
    close_fd(release[1]);
    receive_notification(before);
    reap_child(pid);
    puts("HANDSHAKE spawn returned before child release and exit");
}

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--exit-child")) _exit(CHILD_STATUS);
    if (argc == 2 && !strcmp(argv[1], "--handshake-child")) {
        transfer_byte(STDOUT_FILENO, 1, 'R');
        transfer_byte(STDIN_FILENO, 0, 'G');
        _exit(CHILD_STATUS);
    }
    if (argc != 1) infrastructure_error("unexpected arguments", EINVAL);
    setvbuf(stdout, NULL, _IONBF, 0);
    /* Keep the file-action sources distinct from stdin/stdout destinations. */
    for (int fd = STDIN_FILENO; fd <= STDERR_FILENO; ++fd)
        if (fcntl(fd, F_GETFD) < 0) infrastructure_error("standard descriptor", errno);
    if (!realpath(argv[0], executable)) infrastructure_error("resolve fixture executable", errno);
    if (access(executable, X_OK)) infrastructure_error("fixture executable", errno);

    const char *tmp = getenv("TMPDIR");
    if (!tmp || !*tmp) tmp = "/private/var/tmp";
    int length = snprintf(fixture_dir, sizeof(fixture_dir), "%s/posix-spawn-ownership.XXXXXX", tmp);
    if (length < 0 || (size_t)length >= sizeof(fixture_dir))
        infrastructure_error("temporary directory path", ENAMETOOLONG);
    if (!mkdtemp(fixture_dir)) infrastructure_error("mkdtemp", errno);
    if (atexit(cleanup)) infrastructure_error("register cleanup", ENOMEM);
    length = snprintf(missing_path, sizeof(missing_path), "%s/missing-executable", fixture_dir);
    if (length < 0 || (size_t)length >= sizeof(missing_path))
        infrastructure_error("missing executable path", ENAMETOOLONG);

    struct sigaction action = {0};
    action.sa_handler = child_signal;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGCHLD, &action, NULL)) infrastructure_error("SIGCHLD handler", errno);
    sigset_t blocked;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGCHLD);
    if (sigprocmask(SIG_BLOCK, &blocked, &delivery_mask))
        infrastructure_error("block SIGCHLD", errno);
    sigdelset(&delivery_mask, SIGCHLD);
    require_no_children();
    require_pending(0);

    puts("CASE failed ENOENT spawn stays private");
    failed_spawn(0);
    require_no_children();
    require_pending(0);
    if (notifications) broken("failed spawn delivered SIGCHLD", notifications, 0);

    puts("CASE repeated fast success preserves ownership, exit status and notification");
    for (int iteration = 0; iteration < FAST_SPAWNS; ++iteration) {
        sig_atomic_t before = notifications;
        pid_t pid = spawn_child("--exit-child", NULL);
        receive_notification(before);
        reap_child(pid);
    }
    printf("FAST_SUCCESS count=%d exit_status=%d notifications=%d\n",
           FAST_SPAWNS, CHILD_STATUS, (int)notifications);

    puts("CASE application SIGCHLD handler reaps fast successful child");
    handler_reaping = 1;
    for (int iteration = 0; iteration < FAST_SPAWNS; ++iteration) {
        sig_atomic_t before = notifications;
        handler_pid = 0;
        handler_status = 0;
        handler_error = 0;
        char *arguments[] = { executable, "--exit-child", NULL };
        pid_t pid = -1;
        /* No other children exist here. The handler may reap before libc
         * publishes pid; compare its captured PID after posix_spawn returns. */
        if (sigprocmask(SIG_SETMASK, &delivery_mask, NULL))
            infrastructure_error("unblock handler SIGCHLD", errno);
        int result = posix_spawn(&pid, executable, NULL, NULL, arguments, environ);
        if (sigprocmask(SIG_BLOCK, &blocked, NULL))
            infrastructure_error("block handler SIGCHLD", errno);
        if (result) {
            if (handler_pid > 0)
                broken("spawn reported failure after handler reaped child", result, 0);
            infrastructure_error("spawn handler-reaped child", result);
        }
        if (pid <= 0) broken("handler-case successful spawn pid", pid, 1);
        owned_child = pid;
        receive_notification(before);
        if (handler_pid == pid) owned_child = -1;
        if (handler_pid != pid) {
            fprintf(stderr, "handler waitpid errno=%d\n", (int)handler_error);
            broken("handler reaped exact spawned child", handler_pid, pid);
        }
        int status = handler_status;
        if (!WIFEXITED(status))
            broken("handler child normal exit", status, CHILD_STATUS << 8);
        if (WEXITSTATUS(status) != CHILD_STATUS)
            broken("handler child exit status", WEXITSTATUS(status), CHILD_STATUS);
        require_no_children();
        require_pending(0);
    }
    handler_reaping = 0;
    printf("HANDLER_REAP_SUCCESS count=%d exit_status=%d\n", FAST_SPAWNS, CHILD_STATUS);

    puts("CASE spawn returns while child awaits parent handshake");
    handshake();

    puts("CASE failed spawn preserves unrelated pending successful-child SIGCHLD");
    sig_atomic_t before = notifications;
    pid_t pid = spawn_child("--exit-child", NULL);
    /* Reaping may discard SIGCHLD. Observe it without waiting/reaping or
     * consuming any signal first. CTest bounds missing-notification hangs;
     * a timeout is infrastructure/inconclusive, never semantic RED evidence. */
    while (!pending_child_signal()) {
        if (sched_yield()) infrastructure_error("sched_yield", errno);
    }
    failed_spawn(1);
    if (notifications != before)
        broken("blocked successful-child notification was delivered", notifications, before);
    receive_notification(before);
    reap_child(pid);
    puts("PENDING_SIGNAL successful-child notification survived ENOENT");
    if (rmdir(fixture_dir)) infrastructure_error("remove fixture directory", errno);
    fixture_dir[0] = '\0';

    puts("POSIX_SPAWN_FAILURE_OWNERSHIP_OK");
    return 0;
}

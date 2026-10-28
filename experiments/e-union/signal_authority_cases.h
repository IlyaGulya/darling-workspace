/* Real E-UNION consumers and the production signal_atomic helper, not a lock
 * model. Include after runner.c's helpers. The runner supplies ABI-only bridges
 * to the actual helper; neither bridge may implement a signal-masking policy.
 * TEST scheduling hooks self-disarm and do not exist in shipping builds. */
#ifndef EUNION_SIGNAL_AUTHORITY_CASES_H
#define EUNION_SIGNAL_AUTHORITY_CASES_H

#include <signal.h>
#include <poll.h>
#include <linux/filter.h>
#include <linux/memfd.h>
#include <linux/seccomp.h>
#include <sys/prctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>

void eunion_signal_test_begin(uint64_t* saved);
void eunion_signal_test_end(const uint64_t* saved);

enum signal_authority_case {
    SIGNAL_AUTHORITY_NAMESPACE,
    SIGNAL_AUTHORITY_BINDING,
    SIGNAL_AUTHORITY_DIRECTORY,
    SIGNAL_AUTHORITY_USR2,
    SIGNAL_AUTHORITY_BUS,
    SIGNAL_AUTHORITY_SYS,
    SIGNAL_AUTHORITY_RT_MASK,
};

static int signal_authority_ready[2], signal_authority_release[2];
static int signal_authority_event_fd;
static pid_t signal_authority_target_tid;
static volatile sig_atomic_t signal_authority_kind;
static volatile sig_atomic_t signal_authority_handler_count;
static volatile sig_atomic_t signal_authority_handler_ok;
static volatile sig_atomic_t signal_authority_in_atomic;
static volatile sig_atomic_t signal_authority_close_fd;
static int signal_authority_source_fd;
static int signal_authority_fault_fd;
static long signal_authority_fault_size;

/* Only these signal-safe operations run in application handlers: real managed
 * open/close/dup consumers, read/write/ftruncate, gettid and _exit. In particular
 * there is no assertion, stdio, allocation, or fixture setup in a handler. */
static void signal_authority_event(char event) {
    if (write(signal_authority_event_fd, &event, 1) != 1) _exit(90);
}

static void signal_authority_app_handler(int signum) {
    int saved_errno = errno;
    signal_authority_event('h');
    int ok = (pid_t)syscall(SYS_gettid) == signal_authority_target_tid;
    if (signal_authority_kind == SIGNAL_AUTHORITY_NAMESPACE) {
        char bytes[7];
        int fd = (int)sys_openat(BSD_AT_FDCWD, "/signal-authority/read-lower", 0, 0);
        ok = ok && signum == SIGUSR1 && fd >= 0;
        if (fd >= 0) {
            ssize_t count = read(fd, bytes, sizeof(bytes));
            ok = ok && count == sizeof(bytes) && bytes[0] == 'S' &&
                bytes[1] == 'I' && bytes[2] == 'G' && bytes[3] == 'N' &&
                bytes[4] == 'A' && bytes[5] == 'L' && bytes[6] == '\n';
            if (sys_close(fd) != 0) ok = 0;
        }
    } else if (signal_authority_kind == SIGNAL_AUTHORITY_USR2) {
        ok = ok && signum == SIGUSR2 && !signal_authority_in_atomic;
    } else {
        /* Neither descriptor is the interrupted operation's descriptor. Both
         * close and dup must survive reentry into otherwise unrelated state. */
        long closed = sys_close(signal_authority_close_fd);
        signal_authority_close_fd = -1;
        int alias = (int)sys_dup(signal_authority_source_fd);
        char byte = 0;
        ok = ok && signum == SIGUSR1 && closed == 0 && alias >= 0;
        if (alias >= 0) {
            if (read(alias, &byte, 1) != 1 || byte != 'U') ok = 0;
            if (sys_close(alias) != 0) ok = 0;
        }
    }
    signal_authority_handler_count++;
    signal_authority_handler_ok = ok;
    signal_authority_event(ok ? 'a' : 'f');
    errno = saved_errno;
}

static void signal_authority_fault_handler(int signum, siginfo_t* info, void* context) {
    (void)context;
    int saved_errno = errno;
    signal_authority_event('h');
    int ok = (pid_t)syscall(SYS_gettid) == signal_authority_target_tid;
    if (signal_authority_kind == SIGNAL_AUTHORITY_BUS) {
        ok = ok && signum == SIGBUS && info->si_code == BUS_ADRERR;
        /* Retry of the faulting load succeeds after extending its backing file.
         * This is a real synchronous fault, not raise(SIGBUS). */
        if (ftruncate(signal_authority_fault_fd, signal_authority_fault_size) != 0) ok = 0;
    } else {
        /* SECCOMP_RET_TRAP skips the trapped syscall; no instruction-pointer
         * emulation or fabricated getpid result is needed by this oracle. */
        ok = ok && signum == SIGSYS && info->si_code > 0 && info->si_syscall == SYS_getpid;
    }
    signal_authority_handler_count++;
    signal_authority_handler_ok = ok;
    signal_authority_event(ok ? 'a' : 'f');
    if (!ok) _exit(91);
    errno = saved_errno;
}

static void signal_authority_pause(void) {
    eunion_test_after_prepare = NULL;
    eunion_test_after_fd_prepare = NULL;
    eunion_test_directory_locked = NULL;
    signal_authority_target_tid = (pid_t)syscall(SYS_gettid);
    if (write(signal_authority_ready[1], &signal_authority_target_tid,
            sizeof(signal_authority_target_tid)) != sizeof(signal_authority_target_tid)) _exit(92);
    char byte;
    ssize_t count;
    do { count = read(signal_authority_release[0], &byte, 1); } while (count < 0 && errno == EINTR);
    if (count != 1 || byte != 'R') _exit(93);
}

static long long signal_authority_now(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return -1;
    return (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

/* Watchdogs only detect failure. No timeout, poll result or elapsed duration
 * chooses a successful interleaving; ready -> tgkill -> release does that. The
 * watchdog is outside the target because a correct atomic region defers ALRM. */
static int signal_authority_readable(int fd, long long deadline) {
    for (;;) {
        long long now = signal_authority_now();
        if (now < 0 || now >= deadline) return 0;
        struct pollfd event = { .fd = fd, .events = POLLIN };
        int result = poll(&event, 1, (int)(deadline - now));
        if (result < 0 && errno == EINTR) continue;
        return result > 0 && (event.revents & (POLLIN | POLLHUP));
    }
}

struct signal_authority_observation {
    int exited_ok, watchdog, status, count;
    char events[16];
};

static struct signal_authority_observation signal_authority_observe(pid_t child, int fd) {
    struct signal_authority_observation result = {0};
    long long deadline = signal_authority_now() + 15000;
    for (;;) {
        if (!signal_authority_readable(fd, deadline)) {
            result.watchdog = 1;
            kill(child, SIGKILL);
            break;
        }
        char event;
        ssize_t count = read(fd, &event, 1);
        if (count < 0 && errno == EINTR) continue;
        if (count == 0) break;
        if (count != 1 || result.count == sizeof(result.events)) {
            kill(child, SIGKILL);
            break;
        }
        result.events[result.count++] = event;
    }
    close(fd);
    pid_t waited;
    do { waited = waitpid(child, &result.status, 0); } while (waited < 0 && errno == EINTR);
    result.exited_ok = waited == child && WIFEXITED(result.status) && WEXITSTATUS(result.status) == 0;
    return result;
}

static int signal_authority_saw(const struct signal_authority_observation* observation, char event) {
    for (int i = 0; i < observation->count; i++) if (observation->events[i] == event) return 1;
    return 0;
}

static void signal_authority_expect(const char* name, const char* transition, int ok) {
    char description[256];
    snprintf(description, sizeof(description), "SA %s: %s", name, transition);
    check(description, ok);
}

static int signal_authority_install_app(int signum) {
    struct sigaction action = {0};
    action.sa_handler = signal_authority_app_handler;
    action.sa_flags = SA_RESTART;
    sigemptyset(&action.sa_mask);
    sigset_t unblocked;
    sigemptyset(&unblocked);
    sigaddset(&unblocked, signum);
    return sigaction(signum, &action, NULL) == 0 && sigprocmask(SIG_UNBLOCK, &unblocked, NULL) == 0;
}

static void signal_authority_consumer_child(enum signal_authority_case kind) {
    signal_authority_kind = kind;
    if (!signal_authority_install_app(SIGUSR1)) _exit(94);
    int operation_fd = -1;
    signal_authority_source_fd = signal_authority_close_fd = -1;
    if (kind != SIGNAL_AUTHORITY_NAMESPACE) {
        signal_authority_source_fd = (int)sys_openat(BSD_AT_FDCWD, "/signal-authority/unrelated", 0, 0);
        if (signal_authority_source_fd < 0) _exit(95);
        signal_authority_close_fd = (int)sys_dup(signal_authority_source_fd);
        operation_fd = (int)sys_openat(BSD_AT_FDCWD, kind == SIGNAL_AUTHORITY_BINDING ?
            "/signal-authority/fd-metadata" : "/signal-authority/directory", 0, 0);
        if (signal_authority_close_fd < 0 || operation_fd < 0) _exit(96);
    }
    int ok;
    if (kind == SIGNAL_AUTHORITY_NAMESPACE) {
        eunion_test_after_prepare = signal_authority_pause;
        operation_fd = (int)sys_openat(BSD_AT_FDCWD, "/signal-authority/namespace-created",
            BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, 0600);
        ok = operation_fd >= 0 && write(operation_fd, "CREATED\n", 8) == 8;
    } else if (kind == SIGNAL_AUTHORITY_BINDING) {
        eunion_test_after_fd_prepare = signal_authority_pause;
        ok = sys_fchmod(operation_fd, 0600) == 0;
    } else {
        char names[1024]; int count;
        eunion_test_directory_locked = signal_authority_pause;
        ok = nameset_merged(operation_fd, names, sizeof(names), &count) == 0 &&
            count == 2 && name_count(names, count, "lower-child") == 1 &&
            name_count(names, count, "upper-child") == 1;
    }
    /* No explicit unblock/sigsuspend here: that would hide a leaked product
     * mask. The pending handler must have run before the real consumer returns. */
    ok = ok && signal_authority_handler_count == 1 && signal_authority_handler_ok;
    if (kind != SIGNAL_AUTHORITY_NAMESPACE)
        ok = ok && sys_lseek(signal_authority_source_fd, 0, SEEK_CUR) == 1;
    if (operation_fd >= 0 && sys_close(operation_fd) != 0) ok = 0;
    if (signal_authority_source_fd >= 0 && sys_close(signal_authority_source_fd) != 0) ok = 0;
    signal_authority_event(ok ? 'c' : 'x');
    _exit(ok ? 0 : 97);
}

static void signal_authority_consumer_case(enum signal_authority_case kind, const char* name) {
    int events[2] = {-1, -1};
    signal_authority_ready[0] = signal_authority_ready[1] = -1;
    signal_authority_release[0] = signal_authority_release[1] = -1;
    int pipes = pipe(signal_authority_ready) == 0 && pipe(signal_authority_release) == 0 && pipe(events) == 0;
    signal_authority_expect(name, "create deterministic scheduling pipes", pipes);
    if (!pipes) {
        int* pairs[] = {signal_authority_ready, signal_authority_release, events};
        for (int i = 0; i < 3; i++)
            for (int j = 0; j < 2; j++) if (pairs[i][j] >= 0) close(pairs[i][j]);
        return;
    }
    pid_t child = fork();
    if (child == 0) {
        close(signal_authority_ready[0]); close(signal_authority_release[1]); close(events[0]);
        signal_authority_event_fd = events[1];
        signal_authority_consumer_child(kind);
    }
    close(signal_authority_ready[1]); close(signal_authority_release[0]); close(events[1]);
    if (child < 0) {
        close(signal_authority_ready[0]); close(signal_authority_release[1]); close(events[0]);
        signal_authority_expect(name, "fork exact signal target", 0);
        return;
    }
    pid_t tid = -1;
    int ready = signal_authority_readable(signal_authority_ready[0], signal_authority_now() + 15000) &&
        read(signal_authority_ready[0], &tid, sizeof(tid)) == sizeof(tid) && tid > 0;
    close(signal_authority_ready[0]);
    /* The target cannot release authority until this write. On RED the pending
     * signal runs before its interrupted pipe read can return to userspace; on
     * GREEN it remains pending until the final authority-free mask restore. Do
     * not wait for handler ack before releasing: GREEN intentionally defers it. */
    int sent = ready && syscall(SYS_tgkill, child, tid, SIGUSR1) == 0;
    int released = sent && write(signal_authority_release[1], "R", 1) == 1;
    close(signal_authority_release[1]);
    if (!released) kill(child, SIGKILL);
    struct signal_authority_observation observation = signal_authority_observe(child, events[0]);
    signal_authority_expect(name, "pause real consumer under owned authority", ready);
    signal_authority_expect(name, "target exact paused thread then independently release", sent && released);
    signal_authority_expect(name, "application handler actually enters", signal_authority_saw(&observation, 'h'));
    signal_authority_expect(name, "signal-safe managed operations reach pipe acknowledgment",
        signal_authority_saw(&observation, 'a') && !signal_authority_saw(&observation, 'f'));
    signal_authority_expect(name, "consumer and handler complete without authority self-deadlock",
        !observation.watchdog && observation.exited_ok && signal_authority_saw(&observation, 'c'));
}

static int signal_authority_native_mask(int how, const uint64_t* mask, uint64_t* old) {
    return (int)syscall(SYS_rt_sigprocmask, how, mask, old, sizeof(uint64_t));
}

static void signal_authority_atomic_child(enum signal_authority_case kind) {
    signal_authority_kind = kind;
    signal_authority_target_tid = (pid_t)syscall(SYS_gettid);
    uint64_t saved;
    if (kind == SIGNAL_AUTHORITY_USR2) {
        if (!signal_authority_install_app(SIGUSR2)) _exit(98);
        signal_authority_event('r');
        signal_authority_in_atomic = 1;
        eunion_signal_test_begin(&saved);
        if (syscall(SYS_tgkill, getpid(), signal_authority_target_tid, SIGUSR2) != 0) _exit(99);
        int deferred = signal_authority_handler_count == 0;
        signal_authority_in_atomic = 0;
        eunion_signal_test_end(&saved);
        int ok = deferred && signal_authority_handler_count == 1 && signal_authority_handler_ok;
        signal_authority_event(ok ? 'c' : 'x');
        _exit(ok ? 0 : 100);
    }
    if (kind == SIGNAL_AUTHORITY_RT_MASK) {
        uint64_t original, during, after;
        if (signal_authority_native_mask(2, NULL, &original) != 0) _exit(101);
        /* Native signals 32/33 are runtime-reserved on glibc; libc sigset APIs
         * intentionally cannot express them. Also retain independent RT bits. */
        uint64_t seeded = original | (UINT64_C(1) << 31) | (UINT64_C(1) << 32) |
            (UINT64_C(1) << 40) | (UINT64_C(1) << 63);
        if (signal_authority_native_mask(2, &seeded, NULL) != 0) _exit(102);
        signal_authority_event('r');
        eunion_signal_test_begin(&saved);
        int observed = signal_authority_native_mask(2, NULL, &during) == 0;
        eunion_signal_test_end(&saved);
        observed = observed && signal_authority_native_mask(2, NULL, &after) == 0;
        uint64_t realtime = UINT64_MAX << 31;
        int untouched = observed && (during & realtime) == (seeded & realtime);
        signal_authority_event(untouched ? 'a' : 'f');
        int ok = untouched && after == seeded;
        signal_authority_event(ok ? 'c' : 'x');
        _exit(ok ? 0 : 103);
    }
    struct sigaction action = {0};
    action.sa_sigaction = signal_authority_fault_handler;
    action.sa_flags = SA_SIGINFO;
    sigemptyset(&action.sa_mask);
    int signum = kind == SIGNAL_AUTHORITY_BUS ? SIGBUS : SIGSYS;
    sigset_t unblocked;
    sigemptyset(&unblocked); sigaddset(&unblocked, signum);
    if (sigaction(signum, &action, NULL) != 0 || sigprocmask(SIG_UNBLOCK, &unblocked, NULL) != 0) _exit(104);
    if (kind == SIGNAL_AUTHORITY_BUS) {
        signal_authority_fault_size = sysconf(_SC_PAGESIZE);
        signal_authority_fault_fd = (int)syscall(SYS_memfd_create, "eunion-signal-fault", MFD_CLOEXEC);
        if (signal_authority_fault_size <= 0 || signal_authority_fault_fd < 0 ||
            ftruncate(signal_authority_fault_fd, signal_authority_fault_size) != 0) _exit(105);
        volatile unsigned char* mapping = mmap(NULL, (size_t)signal_authority_fault_size,
            PROT_READ | PROT_WRITE, MAP_SHARED, signal_authority_fault_fd, 0);
        if (mapping == MAP_FAILED || ftruncate(signal_authority_fault_fd, 0) != 0) _exit(106);
        signal_authority_event('r');
        eunion_signal_test_begin(&saved);
        unsigned char byte = *mapping;
        eunion_signal_test_end(&saved);
        int ok = byte == 0 && signal_authority_handler_count == 1 && signal_authority_handler_ok;
        signal_authority_event(ok ? 'c' : 'x');
        _exit(ok ? 0 : 107);
    }
    struct sock_filter instructions[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = { .len = sizeof(instructions) / sizeof(instructions[0]), .filter = instructions };
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 ||
        prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program) != 0) _exit(108);
    signal_authority_event('r');
    eunion_signal_test_begin(&saved);
    syscall(SYS_getpid);
    eunion_signal_test_end(&saved);
    int ok = signal_authority_handler_count == 1 && signal_authority_handler_ok;
    signal_authority_event(ok ? 'c' : 'x');
    _exit(ok ? 0 : 109);
}

static void signal_authority_atomic_case(enum signal_authority_case kind, const char* name) {
    int events[2];
    int pipes = pipe(events) == 0;
    signal_authority_expect(name, "create subprocess evidence pipe", pipes);
    if (!pipes) return;
    pid_t child = fork();
    if (child == 0) {
        close(events[0]); signal_authority_event_fd = events[1];
        signal_authority_atomic_child(kind);
    }
    close(events[1]);
    if (child < 0) {
        close(events[0]); signal_authority_expect(name, "fork isolated helper consumer", 0); return;
    }
    struct signal_authority_observation observation = signal_authority_observe(child, events[0]);
    signal_authority_expect(name, "real native fault/mask fixture reaches helper", signal_authority_saw(&observation, 'r'));
    signal_authority_expect(name, kind == SIGNAL_AUTHORITY_RT_MASK ?
        "begin leaves runtime and other RT bits unchanged" : "actual native handler runs with correct delivery",
        signal_authority_saw(&observation, 'a') && !signal_authority_saw(&observation, 'f'));
    signal_authority_expect(name, kind == SIGNAL_AUTHORITY_RT_MASK ?
        "end restores the complete original native mask" : "helper preserves synchronous delivery or async deferral",
        !observation.watchdog && observation.exited_ok && signal_authority_saw(&observation, 'c'));
    if (!observation.exited_ok && WIFSIGNALED(observation.status))
        printf("  SA %s target terminated by native signal %d%s\n", name,
            WTERMSIG(observation.status), observation.watchdog ? " (watchdog)" : "");
}

static int signal_authority_file(const char* root, const char* leaf, const char* bytes) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/signal-authority/%s", root, leaf);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    int ok = fd >= 0 && fchmod(fd, 0644) == 0 && write(fd, bytes, strlen(bytes)) == (ssize_t)strlen(bytes);
    if (fd >= 0 && close(fd) != 0) ok = 0;
    return ok;
}

static int signal_authority_text(const char* root, const char* leaf, const char* bytes) {
    char path[4096], observed[64];
    snprintf(path, sizeof(path), "%s/signal-authority/%s", root, leaf);
    return slurp(path, observed, sizeof(observed)) == (long)strlen(bytes) && !strcmp(observed, bytes);
}

static void signal_authority_state_checks(void) {
    char path[4096];
    const char* roots[] = {prefix_path, libexec_path};
    int fixture = 1;
    for (int i = 0; i < 2; i++) {
        snprintf(path, sizeof(path), "%s/signal-authority", roots[i]);
        if (mkdir(path, 0755) != 0 && errno != EEXIST) fixture = 0;
        snprintf(path, sizeof(path), "%s/signal-authority/directory", roots[i]);
        if (mkdir(path, 0755) != 0 && errno != EEXIST) fixture = 0;
    }
    fixture = fixture && signal_authority_file(libexec_path, "read-lower", "SIGNAL\n") &&
        signal_authority_file(libexec_path, "fd-metadata", "METADATA\n") &&
        signal_authority_file(libexec_path, "unrelated", "UNRELATED\n") &&
        signal_authority_file(libexec_path, "directory/lower-child", "LOWER\n") &&
        signal_authority_file(prefix_path, "directory/upper-child", "UPPER\n");
    check("SA dedicated native upper/lower fixtures", fixture);
    if (fixture) {
        signal_authority_consumer_case(SIGNAL_AUTHORITY_NAMESPACE, "namespace EX / handler readonly open-close");
        signal_authority_consumer_case(SIGNAL_AUTHORITY_BINDING, "fd prepare / handler unrelated close-dup");
        signal_authority_consumer_case(SIGNAL_AUTHORITY_DIRECTORY, "getdents registry / handler unrelated close-dup");
        check("SA namespace create commits expected upper bytes",
            signal_authority_text(prefix_path, "namespace-created", "CREATED\n"));
        check("SA signal readonly open leaves original lower bytes",
            signal_authority_text(libexec_path, "read-lower", "SIGNAL\n"));
        snprintf(path, sizeof(path), "%s/signal-authority/read-lower", prefix_path);
        check("SA signal readonly open performs no copyup", access(path, F_OK) == -1 && errno == ENOENT);
        struct stat st;
        snprintf(path, sizeof(path), "%s/signal-authority/fd-metadata", prefix_path);
        check("SA interrupted metadata mutation commits upper mode", stat(path, &st) == 0 && (st.st_mode & 0777) == 0600);
        snprintf(path, sizeof(path), "%s/signal-authority/fd-metadata", libexec_path);
        check("SA interrupted metadata mutation preserves lower mode", stat(path, &st) == 0 && (st.st_mode & 0777) == 0644);
        check("SA interrupted metadata mutation preserves lower bytes",
            signal_authority_text(libexec_path, "fd-metadata", "METADATA\n"));
        check("SA unrelated handler descriptors retain lower bytes",
            signal_authority_text(libexec_path, "unrelated", "UNRELATED\n"));
    }
    signal_authority_atomic_case(SIGNAL_AUTHORITY_BUS, "helper synchronous mmap SIGBUS");
    signal_authority_atomic_case(SIGNAL_AUTHORITY_SYS, "helper synchronous seccomp SIGSYS");
    signal_authority_atomic_case(SIGNAL_AUTHORITY_USR2, "helper asynchronous SIGUSR2");
    signal_authority_atomic_case(SIGNAL_AUTHORITY_RT_MASK, "helper complete native RT mask");
}

#endif

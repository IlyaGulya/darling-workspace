/*
 * E-UNION PoC test runner (dar-test-infra-sp5.8.4.4).
 *
 * Compiles vchroot_userspace.c in TEST mode and drives vchroot_expand()
 * directly with assertions, against a two-layer fake guest root:
 *   $prefix  = writable upper layer  (per-prefix user data)
 *   $libexec = read-only lower layer (shared system template)
 *
 * The union goal: a guest absolute path must resolve to the upper layer
 * if present there, else fall back to the shared lower layer, with no
 * mount / fuse / userns -- purely in the userspace path translator.
 *
 * This runner #includes the .c under test so we can call vchroot_expand()
 * and set prefix_path/libexec_path statics without going through main().
 */
#define TEST 1
#define _GNU_SOURCE 1
#include <stdint.h>
#include <stdarg.h>
#include <time.h>
#include <locale.h>
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <sys/socket.h>
#include <sys/xattr.h>
#include <sys/un.h>
#include <unistd.h>
/* Historical note (bead dar-test-infra-sp5.8.4.4.10, now FIXED): the translator's
 * EXIT_PATH check used strncasecmp_l(..., LC_C_LOCALE) with LC_C_LOCALE ==
 * (locale_t)NULL. glibc's strncasecmp_l DEREFERENCES the locale once the compared
 * bytes match, so a NULL locale SEGV'd precisely when a guest path equalled
 * EXIT_PATH ("/Volumes/SystemRoot") -- the escape hatch that is meant to fire.
 * The fix replaced it with the locale-free eunion_ascii_ncasecmp(); the harness no
 * longer needs to inject a real C locale or suppress -Wnonnull. The EP1-EP4 tests
 * below pin the comparator's contract and the resolver smoke path. */

/* Linux symbolic constants the copy-up code uses, mapped to host values in
 * TEST mode (the file only defines a few of these in its #ifdef TEST block). */
#define LINUX_EEXIST EEXIST
#define LINUX_EPERM  EPERM
#define LINUX_ENOTDIR ENOTDIR
#define LINUX_EIO EIO
#define LINUX_EMFILE EMFILE
#define BSD_O_WRONLY 1
#define BSD_O_RDWR 2
#define BSD_O_CREAT 0x200
#define BSD_O_TRUNC 0x400
#define BSD_O_EXCL 0x800

int eunion_test_perthread_wd = -100;

/* stubs for symbols init_vchroot_path() references but never calls in TEST */
int dserver_rpc_vchroot_path(char* a, unsigned long b, uint64_t* c){(void)a;(void)b;(void)c;return -1;}
void __simple_abort(void){__builtin_trap();}
int __simple_printf(const char* f, ...){(void)f;return 0;}

/* Host console transport boundary only. The harness has no guest console
 * process, so the actual publisher's not-published status selects its ordinary
 * fallback. Filesystem and descriptor effects still reach the native kernel. */
int __dserver_plane_request_ex(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2,
        uint64_t p3, uint64_t* outReply0, uint64_t* outReply1)
{
    (void)op; (void)p0; (void)p1; (void)p2; (void)p3; (void)outReply0; (void)outReply1;
    return -1;
}
void __simple_fprintf(int fd, const char* f, ...){(void)fd;(void)f;}

/* rename the unit-under-test's main so we can supply our own */
#define main vchroot_unit_main
#include "vchroot_userspace.c"
#undef main

extern int eunion_test_fail_whiteout;
extern int eunion_test_fail_xattr;

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <dirent.h>

#include <darling/emulation/xnu_syscall/bsd/helper/network/duct.h>

/*
 * The bind implementation is compiled as a separate production source. These
 * minimal fixture adapters model only the BSD-to-Linux AF_UNIX conversion that
 * sys_bind consumes, then use the real vchroot translator under test.
 */
unsigned long sockaddr_fixup_size_from_bsd(const void* address, int length) {
    (void)address;
    (void)length;
    return sizeof(struct sockaddr_fixup);
}

int sockaddr_fixup_from_bsd(struct sockaddr_fixup* out, const void* address, int length) {
    const struct sockaddr_un* input = address;
    struct vchroot_expand_args expansion;

    if (length < (int)offsetof(struct sockaddr_un, sun_path) + 1 ||
        input->sun_family != AF_UNIX)
        return -EINVAL;
    size_t available = (size_t)length - offsetof(struct sockaddr_un, sun_path);
    if (available > sizeof(input->sun_path))
        available = sizeof(input->sun_path);
    size_t input_length = strnlen(input->sun_path, available);
    if (input_length == available)
        return -ENAMETOOLONG;
    memset(out, 0, sizeof(*out));
    out->linux_family = AF_UNIX;
    strcpy(expansion.path, input->sun_path);
    expansion.flags = VCHROOT_FOLLOW;
    expansion.dfd = -100;
    int status = vchroot_expand(&expansion);
    if (status < 0)
        return status;
    /* The owned fixture prefix is genuinely short. This synthetic ABI adapter
     * neither truncates nor publishes a temporary proc-fd socket address. */
    size_t native_length = strlen(expansion.path);
    if (native_length >= sizeof(out->sun_path))
        return -ENAMETOOLONG;
    memcpy(out->sun_path, expansion.path, native_length + 1);
    return (int)offsetof(struct sockaddr_fixup, sun_path) + (int)native_length + 1;
}

int errno_linux_to_bsd(int error) {
    return error;
}

static void (*eunion_test_after_native_dup2)(int from, int to);
long linux_syscall(long a1, long a2, long a3, long a4, long a5, long a6, int number) {
    long result = syscall(number, a1, a2, a3, a4, a5, a6);
    if (result >= 0 && eunion_test_after_native_dup2 &&
        (number == SYS_dup2 || number == SYS_dup3))
        eunion_test_after_native_dup2((int)a1, (int)a2);
    return result < 0 ? -errno : result;
}

long sys_bind(int fd, const void* name, int socklen);
long sys_mkdirat(int fd, const char* path, unsigned int mode);
long sys_mkdir(const char* path, unsigned int mode);
long sys_rmdir(const char* path);
long sys_renameat(int oldfd, const char* oldpath, int newfd, const char* newpath);
long sys_unlinkat(int fd, const char* path, int flag);
long sys_openat(int fd, const char* path, int flags, unsigned int mode);
long sys_dup(int fd);
long sys_ftruncate(int fd, long long length);
long sys_fchmod(int fd, int mode);
long long sys_lseek(int fd, long long offset, int whence);
long sys_dup2(int from, int to);
long sys_fcntl(int fd, int cmd, long arg);
long sys_close(int fd);
void __simple_kprintf(const char* format, ...) { (void)format; }
void kqueue_dup(int oldfd, int newfd) { (void)oldfd; (void)newfd; }

static int g_tests = 0, g_fail = 0;

/* set the translator's prefix (upper layer) the way __darling_vchroot does */
static void set_prefix(const char* p) {
    strcpy(prefix_path, p);
    prefix_path_len = (int)strlen(p);
}

static const char* expand(const char* guest, char* out) {
    struct vchroot_expand_args a;
    a.dfd = -100;
    a.flags = 0;
    strcpy(a.path, guest);
    int rv = vchroot_expand(&a);
    if (rv != 0) { snprintf(out, 4096, "<rv=%d>", rv); return out; }
    strcpy(out, a.path);
    return out;
}

static void check(const char* what, int ok) {
    g_tests++;
    if (!ok) { g_fail++; printf("  FAIL  %s\n", what); }
    else printf("  ok    %s\n", what);
}

/* count occurrences of `name` in a NUL-separated list of `n` names */
static int name_count(const char* buf, int n, const char* name) {
    int c = 0; const char* p = buf;
    for (int i = 0; i < n; i++) { if (strcmp(p, name) == 0) c++; p += strlen(p) + 1; }
    return c;
}
static int name_in(const char* buf, int n, const char* name) {
    return name_count(buf, n, name) > 0;
}

/* --- raw-vs-merged directory oracles -------------------------------------
 * Drain a directory fd into a NUL-separated name set, either through the union
 * entry point (vchroot_getdents_merge) or through the kernel's own getdents64.
 * A 256-byte page forces several calls, so a multi-page listing is exercised
 * even for a small directory. Nothing is filtered: the comparison below needs
 * the kernel's EXACT set (including "." and "..") to catch a function that
 * returns an empty listing instead of the real one. */

/* append one name; returns 0 when the set buffer is full */
static int nameset_add(char* names, int cap, int* used, int* count, const char* name) {
    int l = (int)strlen(name) + 1;
    if (*used + l > cap) return 0;
    memcpy(names + *used, name, l);
    *used += l;
    (*count)++;
    return 1;
}

static int nameset_merged(int fd, char* names, int cap, int* count) {
    char page[256]; int n, used = 0, guard = 0;
    *count = 0; names[0] = '\0';
    while ((n = vchroot_getdents_merge(fd, page, sizeof(page))) > 0) {
        int pos = 0;
        while (pos < n) {
            struct linux_dirent64* de = (void*)(page + pos);
            pos += de->d_reclen;
            if (!nameset_add(names, cap, &used, count, de->d_name)) return -1;
        }
        if (++guard > 1000) return -1;
    }
    return n < 0 ? -1 : 0;
}

static int nameset_raw(int fd, char* names, int cap, int* count) {
    char page[256]; long n; int used = 0, guard = 0;
    *count = 0; names[0] = '\0';
    while ((n = LINUX_SYSCALL(__NR_getdents64, fd, page, sizeof(page))) > 0) {
        int pos = 0;
        while (pos < (int)n) {
            struct linux_dirent64* de = (void*)(page + pos);
            pos += de->d_reclen;
            if (!nameset_add(names, cap, &used, count, de->d_name)) return -1;
        }
        if (++guard > 1000) return -1;
    }
    return n < 0 ? -1 : 0;
}

/* multiset equality; an EMPTY set never equals anything (the oracle requires a
   non-empty raw listing, so an empty merged listing must lose) */
static int nameset_equal(const char* a, int an, const char* b, int bn) {
    if (an != bn || an <= 0) return 0;
    const char* p = a;
    for (int i = 0; i < an; i++) {
        if (name_count(b, bn, p) != name_count(a, an, p)) return 0;
        p += strlen(p) + 1;
    }
    return 1;
}

/* read whole file into buf, return length or -1 */
static long slurp(const char* path, char* buf, size_t cap) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    long n = read(fd, buf, cap - 1);
    close(fd);
    if (n < 0) return -1;
    buf[n] = '\0';
    return n;
}

/* 1 if host directory `dir` contains an entry named `name`, else 0 */
static int name_in_dir(const char* dir, const char* name) {
    DIR* d = opendir(dir);
    if (!d) return 0;
    struct dirent* de;
    int found = 0;
    while ((de = readdir(d)) != NULL) {
        if (strcmp(de->d_name, name) == 0) { found = 1; break; }
    }
    closedir(d);
    return found;
}

/* assert guest path is reported absent: either expand returns an error, or it
   resolves to a non-existent host path (open would ENOENT). */
static void expect_absent(const char* guest) {
    char got[4096];
    const char* r = expand(guest, got);
    g_tests++;
    int absent = (r[0] == '<') || access(got, F_OK) != 0;
    if (!absent) { g_fail++;
        printf("  FAIL  %-40s should be absent but resolved to existing %s\n", guest, got); }
    else printf("  ok    %-40s correctly absent\n", guest);
}

/* assert that guest path resolves to an existing host file at `want` */
static void expect_resolves_to(const char* guest, const char* want) {
    char got[4096];
    expand(guest, got);
    g_tests++;
    int ok = strcmp(got, want) == 0;
    /* also require the resolved host path to actually exist */
    if (ok) {
        if (access(got, F_OK) != 0) ok = 0;
    }
    if (!ok) {
        g_fail++;
        printf("  FAIL  %-40s\n        got : %s\n        want: %s (exists=%d)\n",
               guest, got, want, want[0] ? access(want, F_OK) == 0 : -1);
    } else {
        printf("  ok    %-40s -> %s\n", guest, got);
    }
}

static void relative_directory_self_checks(void) {
    printf("\n== E-UNION lower directory self-open ==\n");
    int source = (int)sys_openat(-100, "/System/Library/LaunchDaemons", 0, 0);
    struct stat original;
    int valid = source >= 0 && fstat(source, &original) == 0 &&
                S_ISDIR(original.st_mode);
    check("SO lower directory opens before libc-style self-open", valid);
    if (!valid)
        return;
    const char* paths[] = {".", "../LaunchDaemons"};
    const char expected[] = "<<launchd plist>>\n";
    for (unsigned i = 0; i < sizeof(paths) / sizeof(paths[0]); ++i) {
        int reopened = (int)sys_openat(source, paths[i], 0, 0);
        struct stat observed;
        char names[256], content[64], label[160];
        int count = 0;
        int same = reopened >= 0 && fstat(reopened, &observed) == 0 &&
                   observed.st_dev == original.st_dev &&
                   observed.st_ino == original.st_ino;
        int listed = same && nameset_merged(reopened, names, sizeof(names), &count) == 0 &&
                     name_count(names, count, "com.apple.test.plist") == 1;
        int child = reopened >= 0 ?
            (int)sys_openat(reopened, "com.apple.test.plist", 0, 0) : -1;
        long bytes = child >= 0 ? read(child, content, sizeof(content)) : -1;
        snprintf(label, sizeof(label),
                 "SO relative %s retains lower directory identity, entries and child bytes", paths[i]);
        check(label, same && listed && bytes == (long)sizeof(expected) - 1 &&
                     memcmp(content, expected, sizeof(expected) - 1) == 0);
        if (child >= 0) sys_close(child);
        if (reopened >= 0) sys_close(reopened);
    }
    char upper[4096];
    snprintf(upper, sizeof(upper), "%s/System/Library/LaunchDaemons", prefix_path);
    check("SO read-only self-open leaves the lower-only directory unmaterialized",
          access(upper, F_OK) < 0 && errno == ENOENT);
    sys_close(source);
}

enum { LARGE_NAMES = 640, LARGE_HIDDEN = 96, LARGE_NAME_LEN = 255,
       LARGE_BYTES = (LARGE_NAMES - LARGE_HIDDEN) * (LARGE_NAME_LEN + 1) };

struct large_seen { unsigned char names[LARGE_NAMES]; };

static void large_name(unsigned int index, char name[LARGE_NAME_LEN + 1]) {
    int n = snprintf(name, LARGE_NAME_LEN + 1, "entry_%04u_", index);
    memset(name + n, 'x', LARGE_NAME_LEN - n);
    name[LARGE_NAME_LEN] = '\0';
}

/* Independent fixture oracle: reject altered names, hidden names, and duplicates
 * immediately, then require every expected member, regardless of host order. */
static int large_note(struct large_seen* seen, const char* name) {
    unsigned int index;
    char expected[LARGE_NAME_LEN + 1];
    if (strlen(name) != LARGE_NAME_LEN ||
        sscanf(name, "entry_%4u_", &index) != 1 ||
        index < LARGE_HIDDEN || index >= LARGE_NAMES)
        return 0;
    large_name(index, expected);
    if (strcmp(name, expected) != 0 || seen->names[index])
        return 0;
    seen->names[index] = 1;
    return 1;
}

static int large_complete(const struct large_seen* seen) {
    for (int i = LARGE_HIDDEN; i < LARGE_NAMES; i++)
        if (!seen->names[i])
            return 0;
    return 1;
}

static int large_bounded_exact(const char* names, int count, size_t cap) {
    struct large_seen seen = {{0}};
    size_t pos = 0;
    if (count != LARGE_NAMES - LARGE_HIDDEN)
        return 0;
    for (int i = 0; i < count; i++) {
        if (pos >= cap)
            return 0;
        const char* end = memchr(names + pos, '\0', cap - pos);
        if (!end || !large_note(&seen, names + pos))
            return 0;
        pos = (size_t)(end - names) + 1;
    }
    return pos == LARGE_BYTES && large_complete(&seen);
}

static int large_page(int fd, struct large_seen* seen) {
    char page[512];
    int n = vchroot_getdents_merge(fd, page, sizeof(page));
    if (n <= 0)
        return n;
    if (n > (int)sizeof(page))
        return -EIO;
    const size_t header = offsetof(struct linux_dirent64, d_name);
    for (size_t pos = 0; pos < (size_t)n;) {
        unsigned short reclen;
        if ((size_t)n - pos < header + 1)
            return -EIO;
        memcpy(&reclen, page + pos + offsetof(struct linux_dirent64, d_reclen),
               sizeof(reclen));
        if (reclen < header + 1 || reclen > (size_t)n - pos ||
            !memchr(page + pos + header, '\0', reclen - header) ||
            !large_note(seen, page + pos + header))
            return -EIO;
        pos += reclen;
    }
    return n;
}

static int large_drain(int fd, struct large_seen* seen) {
    /* Every positive page must contain at least one new name; this bound catches
     * a stuck pager without hanging the fixture. EOF must remain EOF. */
    for (int pages = 0; pages <= LARGE_NAMES; pages++) {
        int n = large_page(fd, seen);
        if (n <= 0)
            return n == 0 && large_complete(seen) && large_page(fd, seen) == 0;
    }
    return 0;
}

static int large_open(void) {
    char host[4096];
    expand("/largemerge", host);
    return open(host, O_RDONLY | O_DIRECTORY);
}

static void large_close(int fd) {
    if (fd >= 0) {
        sys_close(fd);
    }
}

static void large_directory_checks(void) {
    printf("\n== E-UNION large directory buffers and snapshot lifetime ==\n");
    int whiteouts_ok = 1;
    for (unsigned int i = 0; i < LARGE_HIDDEN; i++) {
        char name[LARGE_NAME_LEN + 1], guest[4096];
        large_name(i, name);
        snprintf(guest, sizeof(guest), "/largemerge/%s", name);
        if (sys_unlinkat(BSD_AT_FDCWD, guest, 0) != 0)
            whiteouts_ok = 0;
    }
    check("GL fixture creates 96 NAME_MAX physical whiteouts", whiteouts_ok);
    if (!whiteouts_ok)
        return;

    char* names = malloc(LARGE_BYTES);
    check("GL fixture allocates complete bounded output", names != NULL);
    if (!names)
        return;
    int n = vchroot_readdir_merge("/largemerge", names, 65536);
    check("GL bounded 64KiB output reports EOVERFLOW, not partial success",
          n == -EOVERFLOW);
    n = vchroot_readdir_merge("/largemerge", names, LARGE_BYTES - 1);
    check("GL bounded one-byte-short output reports EOVERFLOW", n == -EOVERFLOW);
    n = vchroot_readdir_merge("/largemerge", names, LARGE_BYTES);
    check("GL exact-fit bounded output contains all 544 names and no whiteouts",
          large_bounded_exact(names, n, LARGE_BYTES));
    free(names);

    int fd = large_open();
    struct large_seen seen = {{0}};
    char tiny[256];
    n = fd >= 0 ? vchroot_getdents_merge(fd, tiny, sizeof(tiny)) : -1;
    check("GL undersized dirent page reports EINVAL, not false EOF", n == -EINVAL);
    check("GL paging after small-buffer error preserves every NAME_MAX entry",
          fd >= 0 && large_drain(fd, &seen));
    large_close(fd);

    fd = large_open();
    memset(&seen, 0, sizeof(seen));
    int first = fd >= 0 ? large_page(fd, &seen) : -1;
    int rewound = fd >= 0 && sys_lseek(fd, 0, 0) == 0;
    memset(&seen, 0, sizeof(seen));
    check("GL rewind discards partial snapshot and replays the complete set",
          first > 0 && rewound && large_drain(fd, &seen));
    large_close(fd);
    fd = large_open();
    memset(&seen, 0, sizeof(seen));
    check("GL close and reopen yields a fresh complete snapshot",
          fd >= 0 && large_drain(fd, &seen));
    large_close(fd);

    /* Only one descriptor reads after dup: this checks snapshot ownership,
     * not the existing implementation's unresolved shared-OFD cursor policy. */
    for (int close_original = 0; close_original < 2; close_original++) {
        fd = large_open();
        memset(&seen, 0, sizeof(seen));
        first = fd >= 0 ? large_page(fd, &seen) : -1;
        int copy = first > 0 ? (int)sys_dup(fd) : -1;
        int survivor = close_original ? copy : fd;
        large_close(close_original ? fd : copy);
        check(close_original
                  ? "GL dup survives original close with exact remaining names"
                  : "GL original survives dup close with exact remaining names",
              copy >= 0 && large_drain(survivor, &seen));
        large_close(survivor);
    }

    fd = large_open();
    memset(&seen, 0, sizeof(seen));
    first = fd >= 0 ? large_page(fd, &seen) : -1;
    pid_t child = first > 0 ? fork() : -1;
    if (child == 0) {
        alarm(10);
        int ok = large_drain(fd, &seen);
        large_close(fd);
        _exit(ok ? 0 : 1);
    }
    int status = 0;
    pid_t waited = -1;
    if (child > 0) {
        do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    }
    check("GL fork child drains inherited snapshot exactly and releases it",
          waited == child && child > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    /* Rewind explicitly: do not pin independent versus shared fork cursors. */
    rewound = fd >= 0 && sys_lseek(fd, 0, 0) == 0;
    memset(&seen, 0, sizeof(seen));
    check("GL parent can rewind and fully enumerate after child teardown",
          rewound && large_drain(fd, &seen));
    large_close(fd);
}

#include "core_state_cases.h"
#include "descriptor_transition_cases.h"
#include "signal_authority_cases.h"
#include "mutation_cases.h"
#include "xattr_cases.h"

int main(void) {
    char cwd[4096];
    getcwd(cwd, sizeof(cwd));
    char prefix[4096], libexec[4096];
    const char* nested_lower_root = getenv("EUNION_NESTED_LOWER_ROOT");
    snprintf(prefix,  sizeof(prefix),  "%s/prefix",  cwd);
    if (nested_lower_root && strcmp(nested_lower_root, "1") == 0)
        snprintf(libexec, sizeof(libexec), "%s/libexec/darling", prefix);
    else
        snprintf(libexec, sizeof(libexec), "%s/libexec", cwd);

    char p[4096], l[4096];

    /* I0. startup activation switch (step 6): eunion_init_from_prefix() must
           leave the union INERT unless the server provisioned the prefix for
           union (signalled by $prefix/.union-work). Run this BEFORE the global
           set_libexec_path below, since that would mark the union active. */
    {
        printf("== E-UNION startup activation ==\n");
        /* a fresh scratch prefix with NO marker -> union stays inert */
        char sp[4096]; snprintf(sp, sizeof(sp), "%s/scratch-noeunion", cwd);
        mkdir(sp, 0755);
        set_prefix(sp);
        libexec_path[0] = '\0'; libexec_path_len = -1; /* reset to pre-init */
        eunion_init_from_prefix();
        check("I0 no .union-work -> union inert (libexec unset)",
              libexec_path_len <= 0);

        /* now provision it: create the staging marker -> union activates with
           the build-time template path */
        char mk[4096]; snprintf(mk, sizeof(mk), "%s/.union-work", sp);
        mkdir(mk, 0755);
        eunion_init_from_prefix();
        check("I0 .union-work present -> union active (libexec set)",
              libexec_path_len > 0);
        /* and the activated template is the compile-time constant */
        check("I0 activated template == EUNION_LIBEXEC_PATH",
              strcmp(libexec_path, EUNION_LIBEXEC_PATH) == 0);
    }

    set_prefix(prefix);
    /* the PoC will need to know the lower layer; expose via a setter the
       implementation provides. For RED baseline this symbol may not exist
       yet -- guarded below. */
#ifdef HAVE_LIBEXEC_SETTER
    libexec_path[0] = '\0'; libexec_path_len = -1; /* clear I0 state */
    set_libexec_path(libexec);
#endif

    relative_directory_self_checks();

    const char* directory_only = getenv("EUNION_DIRECTORY_ONLY");
    if (directory_only && strcmp(directory_only, "1") == 0) {
        large_directory_checks();
        printf("\n%d tests, %d failed\n", g_tests, g_fail);
        return g_fail ? 1 : 0;
    }

    core_state_checks();
    descriptor_transition_checks();
    signal_authority_state_checks();
    routing_state_checks();
    xattr_state_checks();

    printf("== E-UNION resolver tests ==\n");

    /* 1. upper-only file resolves to upper */
    snprintf(p, sizeof(p), "%s/usr/bin/myecho", prefix);
    expect_resolves_to("/usr/bin/myecho", p);

    /* 2. lower-only file must fall back to lower layer */
    snprintf(l, sizeof(l), "%s/usr/bin/ls", libexec);
    expect_resolves_to("/usr/bin/ls", l);

    /* 3. lower-only directory (the launchd plist dir) falls back to lower */
    snprintf(l, sizeof(l), "%s/System/Library/LaunchDaemons", libexec);
    expect_resolves_to("/System/Library/LaunchDaemons", l);

    /* 4. file present in BOTH layers: upper wins */
    snprintf(p, sizeof(p), "%s/usr/bin/sh", prefix);
    expect_resolves_to("/usr/bin/sh", p);

    /* 5. deep lower-only path: every intermediate dir is lower-only.
          The per-component switch must survive multiple components. */
    snprintf(l, sizeof(l), "%s/usr/lib/system/libsystem_c.dylib", libexec);
    expect_resolves_to("/usr/lib/system/libsystem_c.dylib", l);

    /* 6. intermediate dir in upper, leaf only in lower:
          /usr/local exists in BOTH (writable subtree), but the file lives
          only in the template. After descending upper /usr/local the leaf
          must still fall back to the template. The hard mixed case. */
    snprintf(l, sizeof(l), "%s/usr/local/share/tool.conf", libexec);
    expect_resolves_to("/usr/local/share/tool.conf", l);

    /*
     * B1. sys_bind must use the create policy, not the file-write policy. The
     * guest parent is a lower-only symlink. A naive copy-up leaves an upper
     * symlink whose target does not exist, so the host bind either fails or
     * writes through to the lower template. The real bind implementation must
     * create the socket under the upper target and leave the lower untouched.
     */
    {
        struct sockaddr_un address;
        char upper_target[4096], lower_target[4096];
        int fd;
        long rv;

        memset(&address, 0, sizeof(address));
        address.sun_family = AF_UNIX;
        strcpy(address.sun_path, "/var/bind_link/shellspawn.sock");
        fd = socket(AF_UNIX, SOCK_STREAM, 0);
        rv = fd < 0 ? -1 : sys_bind(fd, &address,
            (int)offsetof(struct sockaddr_un, sun_path) + (int)strlen(address.sun_path) + 1);
        if (fd >= 0)
            close(fd);

        snprintf(upper_target, sizeof(upper_target), "%s/var/bind_real/shellspawn.sock", prefix);
        snprintf(lower_target, sizeof(lower_target), "%s/var/bind_real/shellspawn.sock", libexec);
        check("B1 sys_bind through symlinked parent succeeds", rv == 0);
        check("B1 socket is created in the upper target directory",
              access(upper_target, F_OK) == 0);
        check("B1 lower template remains socket-free", access(lower_target, F_OK) != 0);
        unlink(upper_target);
    }

    /* 7. absent in BOTH layers: must not falsely resolve to an existing host
          path (open() must end up ENOENT). */
    {
        char got[4096];
        expand("/usr/bin/does-not-exist-anywhere", got);
        g_tests++;
        int bad = (got[0] != '<') && access(got, F_OK) == 0;
        if (bad) { g_fail++; printf("  FAIL  %-40s falsely resolved to existing %s\n",
                                    "/usr/bin/does-not-exist-anywhere", got); }
        else printf("  ok    %-40s -> %s (correctly absent)\n",
                    "/usr/bin/does-not-exist-anywhere", got);
    }

    /* 8. .. traversal that climbs out of a lower-only subtree: must not
          crash or escape; lands on the lower-only /usr/lib dir. */
    snprintf(l, sizeof(l), "%s/usr/lib", libexec);
    expect_resolves_to("/usr/lib/system/..", l);

    /* PC (.7a). PER-COMPONENT two-layer lookup. /var/pc has a copied-up parent in
       BOTH layers, an UPPER-only leaf, and a LOWER-only sibling leaf. Both leaves
       must resolve to their correct layer -- this is the per-component union
       contract: at every depth the resolver consults upper THEN lower. (NB: a
       genuinely lower-only intermediate cannot hold an upper child on a real FS --
       any upper write materializes the upper parent chain via
       eunion_mkparents_upper -- so this is the strongest CONSTRUCTIBLE form of the
       mixed case. The unreachable-commit-to-lower fragility is pinned separately
       by PC3 below.) */
    {
        snprintf(p, sizeof(p), "%s/var/pc/leaf", prefix);
        check("PC fixture: upper leaf present", access(p, F_OK) == 0);
        snprintf(l, sizeof(l), "%s/var/pc/lowerleaf", libexec);
        check("PC fixture: lower sibling present", access(l, F_OK) == 0);

        /* upper-only leaf -> upper; lower-only sibling -> lower; at every depth. */
        snprintf(p, sizeof(p), "%s/var/pc/leaf", prefix);
        expect_resolves_to("/var/pc/leaf", p);
        snprintf(l, sizeof(l), "%s/var/pc/lowerleaf", libexec);
        expect_resolves_to("/var/pc/lowerleaf", l);
    }

    /* PC3 (.7a, WHITE-BOX). The latent fragility directly: drive vchroot_run with
       a context that has ALREADY committed to the LOWER root (current_root =
       libexec), then resolve a child that lives in the UPPER layer. The black-box
       path can never reach this state (eunion_mkparents_upper always materializes
       upper ancestors, so the walk never commits to lower while an upper child
       exists) -- so we construct the committed-to-lower context by hand to pin the
       per-component contract: the resolver must consult the UPPER layer for every
       component REGARDLESS of which root earlier components committed to. With the
       pre-refactor resolver (upper-fallback gated on current_root_len ==
       prefix_path_len) the upper child is invisible once committed to lower -> the
       walk yields the non-existent lower path (or an unknown_component) -> RED.
       After the refactor it resolves to the upper copy. */
    {
        struct context ctxt;
        ctxt.unknown_component = false;
        ctxt.symlink_depth = 0;
        ctxt.follow = false;
        /* pretend the walk already descended the lower-only intermediate /var/pc
           and committed to the lower root there. */
        ctxt.current_root = libexec_path;
        ctxt.current_root_len = libexec_path_len;
        snprintf(ctxt.current_path, sizeof(ctxt.current_path), "%s/var/pc", libexec);
        ctxt.current_path_len = (int)strlen(ctxt.current_path);

        int rv = vchroot_run("leaf", &ctxt);
        snprintf(p, sizeof(p), "%s/var/pc/leaf", prefix);
        check("PC3 vchroot_run(committed-to-lower) finds the UPPER child", rv == 0);
        check("PC3 resolved path is the UPPER copy (per-component upper lookup)",
              rv == 0 && strcmp(ctxt.current_path, p) == 0);
        check("PC3 did not get stuck as unknown_component",
              !ctxt.unknown_component);
    }

    /* RESOLVE1. An upper non-directory shadows the whole lower subtree. The
       resolver must return ENOTDIR instead of falling through to lower/child. */
    {
        struct vchroot_expand_args shadow = { .dfd = -100, .flags = 0 };
        strcpy(shadow.path, "/var/non_dir_shadow/child");
        int rv = vchroot_expand(&shadow);
        check("RESOLVE1 upper non-directory blocks lower directory", rv == -LINUX_ENOTDIR);
    }

    printf("\n== E-UNION copy-up tests ==\n");

    char ub[256], lb[256];

    /* C1. copy-up of a lower-only regular file materializes an upper copy
           with identical content, and afterwards the path resolves to upper. */
    {
        int rv = vchroot_copyup("/usr/bin/ls");
        snprintf(p, sizeof(p), "%s/usr/bin/ls", prefix);
        check("C1 copyup lower-only file returns 0", rv == 0);
        check("C1 upper copy exists after copyup", access(p, F_OK) == 0);
        slurp(p, ub, sizeof(ub));
        snprintf(l, sizeof(l), "%s/usr/bin/ls", libexec);
        slurp(l, lb, sizeof(lb));
        check("C1 upper content == lower content", strcmp(ub, lb) == 0);
        expect_resolves_to("/usr/bin/ls", p); /* now resolves to upper */
    }

    /* C2. copy-up preserves mode bits (e.g. executable 0755). */
    {
        snprintf(p, sizeof(p), "%s/usr/bin/ls", prefix);
        struct stat us; stat(p, &us);
        check("C2 upper copy is executable (0755 preserved)",
              (us.st_mode & 0777) == 0755);
    }

    /* C3. copy-up of an already-upper path is a no-op success. */
    {
        int rv = vchroot_copyup("/usr/bin/myecho");
        check("C3 copyup of upper-only path returns 0 (no-op)", rv == 0);
    }

    /* C4. recursive parent copy-up: a deep lower-only file materializes its
           parent dirs in the upper layer too. */
    {
        int rv = vchroot_copyup("/usr/lib/system/libsystem_c.dylib");
        snprintf(p, sizeof(p), "%s/usr/lib/system", prefix);
        check("C4 deep copyup returns 0", rv == 0);
        check("C4 parent dir /usr/lib/system created in upper",
              access(p, F_OK) == 0);
        snprintf(p, sizeof(p), "%s/usr/lib/system/libsystem_c.dylib", prefix);
        check("C4 deep file copied to upper", access(p, F_OK) == 0);
    }

    /* C5. idempotent: copy-up twice succeeds and does not corrupt content. */
    {
        int rv1 = vchroot_copyup("/usr/bin/ls");
        int rv2 = vchroot_copyup("/usr/bin/ls");
        snprintf(p, sizeof(p), "%s/usr/bin/ls", prefix);
        slurp(p, ub, sizeof(ub));
        snprintf(l, sizeof(l), "%s/usr/bin/ls", libexec);
        slurp(l, lb, sizeof(lb));
        check("C5 double copyup both return 0", rv1 == 0 && rv2 == 0);
        check("C5 content intact after double copyup", strcmp(ub, lb) == 0);
    }

    /* C6. copy-up of a path absent in BOTH layers fails (nothing to copy). */
    {
        int rv = vchroot_copyup("/usr/bin/nonesuch");
        check("C6 copyup of absent path returns nonzero", rv != 0);
    }

    /* C8. Unsupported special files are explicit, never silently converted to
           regular files or allowed to mutate the lower template. */
    {
        int rv = vchroot_copyup("/var/log/special_fifo");
        snprintf(p, sizeof(p), "%s/var/log/special_fifo", prefix);
        check("C8 special-file copyup returns explicit EPERM", rv == -LINUX_EPERM);
        check("C8 special-file copyup does not publish an upper object", access(p, F_OK) != 0);
    }

    /* C9. Metadata copy is part of the copy-up transaction. A failed xattr
           transfer must fail the operation and remove staging output, instead
           of publishing a data copy with incomplete metadata. */
    {
        snprintf(p, sizeof(p), "%s/var/log/xattr_error", prefix);
        unlink(p);
        eunion_test_fail_xattr = 1;
        int rv = vchroot_copyup("/var/log/xattr_error");
        eunion_test_fail_xattr = 0;
        check("C9 metadata transfer error reaches copyup caller", rv == -LINUX_EIO);
        check("C9 failed metadata copy is not published", access(p, F_OK) != 0);
    }

    /* C7. CONCURRENT copy-up across SEPARATE PROCESSES sharing one upper layer
           (the launchd + daemons + shell scenario, gVisor spec F.7). N children
           race to copy up the SAME lower-only large file. A reader must NEVER
           observe a partial/empty file (tmp+rename atomic publish), every child
           must succeed, and the final upper copy must byte-match the lower. */
    {
        const char* guest = "/var/db/bigfile";          /* lower-only, large */
        snprintf(p, sizeof(p), "%s/var/db/bigfile", prefix);
        snprintf(l, sizeof(l), "%s/var/db/bigfile", libexec);

        const int N = 16;
        pid_t kids[16];
        for (int k = 0; k < N; k++) {
            pid_t pid = fork();
            if (pid == 0) {
                /* each child also hammers a read to try to catch a partial:
                   if the published file is visible at all, its size (via fstat)
                   must already equal the lower size -- atomic publish means a
                   reader never sees a growing/half file. */
                int rv = vchroot_copyup(guest);
                struct stat ls; stat(l, &ls);
                int bad = 0;
                int fd = open(p, O_RDONLY);
                if (fd >= 0) {
                    struct stat fs; fstat(fd, &fs);
                    if (fs.st_size != ls.st_size) bad = 1;   /* partial visible! */
                    close(fd);
                }
                _exit(bad ? 2 : (rv == 0 ? 0 : 1));
            }
            kids[k] = pid;
        }
        int all_ok = 1, any_partial = 0;
        for (int k = 0; k < N; k++) {
            int st; waitpid(kids[k], &st, 0);
            int code = WIFEXITED(st) ? WEXITSTATUS(st) : 99;
            if (code == 2) any_partial = 1;
            if (code != 0) all_ok = 0;
        }
        check("C7 no child observed a partial/empty file", !any_partial);
        check("C7 all concurrent copyups succeeded", all_ok);
        /* final state: exactly one upper copy, content == lower */
        struct stat us, ls2; stat(p, &us); stat(l, &ls2);
        check("C7 final upper copy size == lower size", us.st_size == ls2.st_size);
        /* and no leftover staging files in the work dir */
        char wd[4096]; snprintf(wd, sizeof(wd), "%s/.union-work", prefix);
        DIR* d = opendir(wd); int leftovers = 0;
        if (d) { struct dirent* e; while ((e = readdir(d))) {
            if (e->d_name[0] != '.') leftovers++;
        } closedir(d); }
        check("C7 no leftover staging files after races", leftovers == 0);
    }

    printf("\n== E-UNION whiteout + opaque tests ==\n");

    /* W1. A real guest unlink hides the lower namesake without changing it. */
    {
        int rv = sys_unlinkat(BSD_AT_FDCWD, "/usr/bin/ls", 0);
        check("W1 unlink hides the lower namesake", rv == 0);
        expect_absent("/usr/bin/ls");
    }

    /* W2. a whiteout placeholder in the upper layer is itself never returned as
           a real file (it must read as ENOENT, not as the marker file). */
    {
        snprintf(p, sizeof(p), "%s/usr/bin/ls", prefix);
        struct stat st;
        check("W2 whiteout marker file physically present in upper",
              lstat(p, &st) == 0);
        expect_absent("/usr/bin/ls"); /* but invisible to the union */
    }


    /* W4. Unlink of a name in BOTH layers replaces its upper copy with a
           whiteout; the lower object must not reappear. */
    {
        int rv = sys_unlinkat(BSD_AT_FDCWD, "/usr/bin/sh", 0);
        check("W4 unlink (both layers) returns 0", rv == 0);
        expect_absent("/usr/bin/sh");
    }

    /* O1. Raw fixture topology: the production resolver must suppress lower
           children of an opaque upper directory, not its upper children. */
    {
        snprintf(p, sizeof(p), "%s/opaquedir", prefix);
        int rv = lsetxattr(p, "user.union.opaque", "y", 1, 0);
        check("O1 fixture installs opaque upper directory", rv == 0);
        /* lowerchild exists only in lower/opaquedir; must be hidden */
        expect_absent("/opaquedir/lowerchild");
        /* but an upper child in the same opaque dir is still visible */
        snprintf(p, sizeof(p), "%s/opaquedir/upperchild", prefix);
        expect_resolves_to("/opaquedir/upperchild", p);
    }

    /* O2. a non-opaque dir present in both still merges (regression guard). */
    {
        /* /usr/local is in both, not opaque; lower-only file shows through */
        snprintf(l, sizeof(l), "%s/usr/local/share/tool.conf", libexec);
        expect_resolves_to("/usr/local/share/tool.conf", l);
    }

    printf("\n== E-UNION whiteout/opaque mutation-resistant (dyra #9) ==\n");
    /* Dedicated pristine deletion fixtures bind a real guest unlink to both
       resolver hiding and the physical upper marker, while checking that the
       lower inode survives. Native opacity topology separately exercises the
       production resolver and marker readback. */
    {
        char got[4096];

        /* WM1. whiteout a pristine LOWER-ONLY file. */
        {
            const char* g = "/var/log/wh_lower";
            snprintf(l, sizeof(l), "%s/var/log/wh_lower", libexec);
            snprintf(p, sizeof(p), "%s/var/log/wh_lower", prefix);
            /* precondition: visible via the lower layer, no upper object yet */
            expand(g, got);
            check("WM1 fixture: resolves to lower before whiteout",
                  strcmp(got, l) == 0 && access(l, F_OK) == 0);
            check("WM1 fixture: no upper object before whiteout",
                  access(p, F_OK) != 0);

            int rv = sys_unlinkat(BSD_AT_FDCWD, g, 0);
            check("WM1 unlink returns 0", rv == 0);
            /* (a) semantic: merged view now ENOENT (no fall-through to lower) */
            expect_absent(g);
            /* (b) structural: placeholder exists AND carries the marker xattr.
                   A no-op whiteout creates no placeholder -> access fails; a bare
                   empty file would lack the xattr -> getxattr fails. Both caught. */
            check("WM1 upper placeholder physically created",
                  access(p, F_OK) == 0);
            char xv[8];
            ssize_t xn = lgetxattr(p, "user.union.whiteout", xv, sizeof(xv));
            check("WM1 placeholder carries user.union.whiteout marker", xn >= 1);
            /* (c) the template victim is UNTOUCHED on disk */
            check("WM1 template victim untouched", access(l, F_OK) == 0);
        }

        /* WM2. whiteout a name present in BOTH layers (upper copy + lower). The
                upper copy must be REPLACED by the marker placeholder, and the
                merged view must report ENOENT (not the upper file, not the lower).*/
        {
            const char* g = "/var/whboth/file";
            snprintf(p, sizeof(p), "%s/var/whboth/file", prefix);
            snprintf(l, sizeof(l), "%s/var/whboth/file", libexec);
            /* precondition: upper wins before whiteout */
            expand(g, got);
            check("WM2 fixture: upper wins before whiteout", strcmp(got, p) == 0);
            int rv = sys_unlinkat(BSD_AT_FDCWD, g, 0);
            check("WM2 unlink (both layers) returns 0", rv == 0);
            expect_absent(g);
            char xv[8];
            ssize_t xn = lgetxattr(p, "user.union.whiteout", xv, sizeof(xv));
            check("WM2 placeholder carries marker (upper copy replaced)", xn >= 1);
            check("WM2 template victim untouched", access(l, F_OK) == 0);
        }


        /* OM1. Inject opaque topology through native fixture metadata, then
                exercise the actual merged resolver and marker readback. */
        {
            const char* dir = "/var/opq";
            snprintf(p, sizeof(p), "%s/var/opq/up_child", prefix);
            /* precondition: lower child visible before opaque */
            snprintf(l, sizeof(l), "%s/var/opq/lo_child", libexec);
            expand("/var/opq/lo_child", got);
            check("OM1 fixture: lower child visible before opaque",
                  strcmp(got, l) == 0 && access(l, F_OK) == 0);

            char updir[4096]; snprintf(updir, sizeof(updir), "%s%s", prefix, dir);
            int rv = lsetxattr(updir, "user.union.opaque", "y", 1, 0);
            check("OM1 fixture installs opaque upper directory", rv == 0);
            /* (a) semantic: lower-only child now hidden */
            expect_absent("/var/opq/lo_child");
            /* upper-only child still visible */
            expect_resolves_to("/var/opq/up_child", p);
            /* (b) structural: the upper dir carries the opaque marker */
            char xv[8];
            ssize_t xn = lgetxattr(updir, "user.union.opaque", xv, sizeof(xv));
            check("OM1 upper dir carries user.union.opaque marker", xn >= 1);
        }

        /* CM2. atime and mtime are independent overlay metadata. Copy-up must
               preserve both source timestamps; using mtime for atime changes
               observable file metadata and breaks tools that rely on atime. */
        {
            snprintf(l, sizeof(l), "%s/var/log/metadata_times", libexec);
            snprintf(p, sizeof(p), "%s/var/log/metadata_times", prefix);
            unlink(p);
            struct stat before, upper_stat;
            int before_ok = stat(l, &before) == 0;
            int rv = vchroot_copyup("/var/log/metadata_times");
            int upper_ok = stat(p, &upper_stat) == 0;
            check("CM2 timestamp fixture is present", before_ok);
            check("CM2 timestamp copyup succeeds", rv == 0);
            check("CM2 atime is preserved independently of mtime",
                  before_ok && upper_ok && upper_stat.st_atime == before.st_atime &&
                  upper_stat.st_atime != upper_stat.st_mtime);
        }

        /* MK3. The guest runtime uses this exact absolute path. Exercise the
           public mkdir/rmdir adapters, not only the internal helper calls. */
        {
            const char* g = "/private/var/tmp/west-eunion-mkdir-opaque/recreate";
            char lower_dir[4096], upper_dir[4096], lower_child[4096];
            snprintf(lower_dir, sizeof(lower_dir), "%s%s", libexec, g);
            snprintf(upper_dir, sizeof(upper_dir), "%s%s", prefix, g);
            snprintf(lower_child, sizeof(lower_child), "%s/stale_a.txt", lower_dir);
            check("MK3 guest-path lower recreate fixture is present",
                  access(lower_child, F_OK) == 0);
            check("MK3 remove first visible child",
                  sys_unlinkat(BSD_AT_FDCWD, "/private/var/tmp/west-eunion-mkdir-opaque/recreate/stale_a.txt", 0) == 0);
            check("MK3 remove second visible child",
                  sys_unlinkat(BSD_AT_FDCWD, "/private/var/tmp/west-eunion-mkdir-opaque/recreate/stale_b.txt", 0) == 0);
            check("MK3 sys_rmdir absolute guest path succeeds", sys_rmdir(g) == 0);
            check("MK3 lower recreate fixture remains intact", access(lower_child, F_OK) == 0);
            int mkdir_rv = (int)sys_mkdir(g, 0755);
            check("MK3 sys_mkdir absolute guest path succeeds", mkdir_rv == 0);
            struct stat mk3st;
            check("MK3 recreated directory is published in upper",
                  stat(upper_dir, &mk3st) == 0 && S_ISDIR(mk3st.st_mode));
        }
    }

    printf("\n== E-UNION readdir-merge tests ==\n");

    /* The merge fills a caller buffer with NUL-separated names. Helper to test
       set-membership and dedup. */
    char names[8192];
    int  ncount;

    /* R1. dir in both layers, no whiteout/opaque: merged set = union of names,
           deduped (a name in both appears once). */
    {
        ncount = vchroot_readdir_merge("/mergedir", names, sizeof(names));
        check("R1 merge returns >= 0", ncount >= 0);
        check("R1 contains upper-only 'up_only'",  name_in(names, ncount, "up_only"));
        check("R1 contains lower-only 'low_only'", name_in(names, ncount, "low_only"));
        check("R1 contains shared 'both' exactly once",
              name_count(names, ncount, "both") == 1);
        check("R1 does not invent '.' or '..' as data entries",
              !name_in(names, ncount, ".") && !name_in(names, ncount, ".."));
    }

    /* R2. a whiteout in the upper hides the lower namesake from the listing. */
    {
        check("R2 real unlink of lower entry succeeds",
              sys_unlinkat(BSD_AT_FDCWD, "/mergedir/low_only", 0) == 0);
        ncount = vchroot_readdir_merge("/mergedir", names, sizeof(names));
        check("R2 whiteouted lower entry absent from listing",
              !name_in(names, ncount, "low_only"));
        check("R2 whiteout placeholder itself not listed",
              name_count(names, ncount, "low_only") == 0);
        check("R2 other entries still present",
              name_in(names, ncount, "up_only") && name_in(names, ncount, "both"));
    }

    /* R3. an opaque dir lists ONLY upper entries (lower not merged). */
    {
        snprintf(p, sizeof(p), "%s/mergedir", prefix);
        check("R3 fixture installs opaque directory",
              lsetxattr(p, "user.union.opaque", "y", 1, 0) == 0);
        ncount = vchroot_readdir_merge("/mergedir", names, sizeof(names));
        check("R3 opaque dir hides remaining lower-only entries",
              !name_in(names, ncount, "low2"));
        check("R3 opaque dir still shows upper entries",
              name_in(names, ncount, "up_only"));
    }

    printf("\n== E-UNION linked write operations ==\n");

    /* Exercise the production syscall consumers against the same two layers.
       Native effects and transaction completion stay in the linked wrappers. */

    /* P1. Opening a lower-only file for writing must materialize an upper copy. */
    {
        /* fresh lower-only file for this test */
        snprintf(l, sizeof(l), "%s/usr/local/share/tool.conf", libexec);
        snprintf(p, sizeof(p), "%s/usr/local/share/tool.conf", prefix);
        /* ensure it isn't already upper from an earlier test */
        unlink(p);
        int fd = (int)sys_openat(BSD_AT_FDCWD, "/usr/local/share/tool.conf",
                                 BSD_O_WRONLY, 0);
        check("P1 openat(lower-only, write) succeeds", fd >= 0);
        if (fd >= 0)
            sys_close(fd);
        check("P1 file now materialized in upper", access(p, F_OK) == 0);
        expect_resolves_to("/usr/local/share/tool.conf", p);
    }

    /* RPATH1. AT_FDCWD relative mutation must resolve the guest cwd before the
       union policy runs. The old helper returned success without copy-up when
       dfd was AT_FDCWD, so the real relative openat/mkdirat path could still
       reach the shared lower template. */
    {
        const char* guest = "/var/log/relative_lower";
        char saved_cwd[4096];
        char upper_path[4096];
        char lower_path[4096];
        getcwd(saved_cwd, sizeof(saved_cwd));
        snprintf(upper_path, sizeof(upper_path), "%s%s", prefix, guest);
        snprintf(lower_path, sizeof(lower_path), "%s%s", libexec, guest);
        unlink(upper_path);
        check("RPATH1 lower relative fixture is pristine", access(lower_path, F_OK) == 0);
        check("RPATH1 chdir to the guest prefix succeeds", chdir(prefix) == 0);
        int fd = (int)sys_openat(BSD_AT_FDCWD, "var/log/relative_lower",
                                 BSD_O_WRONLY, 0);
        check("RPATH1 relative AT_FDCWD writable open succeeds", fd >= 0);
        if (fd >= 0)
            sys_close(fd);
        check("RPATH1 relative mutation materializes the upper copy",
              access(upper_path, F_OK) == 0);
        check("RPATH1 relative mutation leaves the lower template intact",
              access(lower_path, F_OK) == 0);
        check("RPATH1 restore host cwd succeeds", chdir(saved_cwd) == 0);
    }

    /* RSITE1. Exercise the real unlinkat adapter with a lower dirfd. The old
       site only applied E-UNION policy to absolute strings, so Linux unlinkat
       deleted the shared lower inode. */
    {
        char lower_dir[4096], lower_file[4096], upper_file[4096], marker[2];
        snprintf(lower_dir, sizeof(lower_dir), "%s/var/log", libexec);
        snprintf(lower_file, sizeof(lower_file), "%s/syscall_unlink", lower_dir);
        snprintf(upper_file, sizeof(upper_file), "%s/var/log/syscall_unlink", prefix);
        int dirfd = open(lower_dir, O_RDONLY | O_DIRECTORY);
        int rv = dirfd >= 0 ? (int)sys_unlinkat(dirfd, "syscall_unlink", 0) : -1;
        check("RSITE1 lower dirfd opens", dirfd >= 0);
        check("RSITE1 unlinkat(relative) succeeds", rv == 0);
        check("RSITE1 lower unlink target remains intact", access(lower_file, F_OK) == 0);
        marker[0] = '\0';
        ssize_t marker_len = lgetxattr(upper_file, "user.union.whiteout", marker, sizeof(marker));
        check("RSITE1 unlinkat creates an upper whiteout", marker_len == 1 && marker[0] == 'y');
        if (dirfd >= 0)
            close(dirfd);
    }

    /* RSITE2. rmdir has no explicit fd in its BSD ABI, so its relative path is
       resolved against the per-thread working directory. Make that directory a
       lower fd to exercise the same real runtime shape. */
    {
        char lower_dir[4096], lower_target[4096], upper_target[4096], marker[2];
        snprintf(lower_dir, sizeof(lower_dir), "%s/var/log", libexec);
        snprintf(lower_target, sizeof(lower_target), "%s/syscall_rmdir", lower_dir);
        snprintf(upper_target, sizeof(upper_target), "%s/var/log/syscall_rmdir", prefix);
        int dirfd = open(lower_dir, O_RDONLY | O_DIRECTORY);
        eunion_test_perthread_wd = dirfd;
        check("RSITE2 unlink visible child before rmdir",
              sys_unlinkat(dirfd, "syscall_rmdir/child", 0) == 0);
        int rv = dirfd >= 0 ? (int)sys_rmdir("syscall_rmdir") : -1;
        check("RSITE2 rmdir working dir opens", dirfd >= 0);
        check("RSITE2 rmdir(relative) succeeds", rv == 0);
        check("RSITE2 lower rmdir target remains intact", access(lower_target, F_OK) == 0);
        marker[0] = '\0';
        ssize_t marker_len = lgetxattr(upper_target, "user.union.whiteout", marker, sizeof(marker));
        check("RSITE2 rmdir creates an upper whiteout", marker_len == 1 && marker[0] == 'y');
        if (dirfd >= 0)
            close(dirfd);
        eunion_test_perthread_wd = -100;
    }

    /* RSITE3. BSD AT_FDCWD is translated to the per-thread working directory.
       A relative mkdir must copy the lower parent and create only in upper. */
    {
        char lower_dir[4096], lower_parent[4096], upper_parent[4096], lower_created[4096];
        char upper_created[4096];
        snprintf(lower_dir, sizeof(lower_dir), "%s/var/syscall_mkdir_parent", libexec);
        snprintf(lower_parent, sizeof(lower_parent), "%s", lower_dir);
        snprintf(upper_parent, sizeof(upper_parent), "%s/var/syscall_mkdir_parent", prefix);
        snprintf(lower_created, sizeof(lower_created), "%s/created", lower_parent);
        snprintf(upper_created, sizeof(upper_created), "%s/created", upper_parent);
        int dirfd = open(lower_dir, O_RDONLY | O_DIRECTORY);
        eunion_test_perthread_wd = dirfd;
        int rv = dirfd >= 0 ? (int)sys_mkdirat(BSD_AT_FDCWD, "created", 0700) : -1;
        check("RSITE3 mkdir working dir opens", dirfd >= 0);
        check("RSITE3 mkdirat(AT_FDCWD, relative) succeeds", rv == 0);
        check("RSITE3 mkdir parent materializes in upper", access(upper_parent, F_OK) == 0);
        check("RSITE3 mkdir target lands in upper", access(upper_created, F_OK) == 0);
        check("RSITE3 lower mkdir target remains absent", access(lower_created, F_OK) != 0);
        if (dirfd >= 0)
            close(dirfd);
        eunion_test_perthread_wd = -100;
    }

    /* RSITE4. A relative rename must copy the source, publish the destination
       in upper, and leave both lower template names unchanged. */
    {
        char lower_dir[4096], lower_source[4096], lower_dest[4096];
        char upper_source[4096], upper_dest[4096], content[64], marker[2];
        snprintf(lower_dir, sizeof(lower_dir), "%s/var/log", libexec);
        snprintf(lower_source, sizeof(lower_source), "%s/syscall_rename_source", lower_dir);
        snprintf(lower_dest, sizeof(lower_dest), "%s/syscall_rename_destination", lower_dir);
        snprintf(upper_source, sizeof(upper_source), "%s/var/log/syscall_rename_source", prefix);
        snprintf(upper_dest, sizeof(upper_dest), "%s/var/log/syscall_rename_destination", prefix);
        int dirfd = open(lower_dir, O_RDONLY | O_DIRECTORY);
        int rv = dirfd >= 0 ? (int)sys_renameat(dirfd, "syscall_rename_source", dirfd,
                                               "syscall_rename_destination") : -1;
        check("RSITE4 rename working dir opens", dirfd >= 0);
        check("RSITE4 renameat(relative) succeeds", rv == 0);
        check("RSITE4 lower source remains intact", access(lower_source, F_OK) == 0);
        check("RSITE4 lower destination remains intact", access(lower_dest, F_OK) == 0);
        int content_fd = open(upper_dest, O_RDONLY);
        ssize_t content_len = content_fd >= 0 ? read(content_fd, content, sizeof(content) - 1) : -1;
        if (content_fd >= 0)
            close(content_fd);
        if (content_len >= 0)
            content[content_len] = '\0';
        check("RSITE4 rename destination is materialized in upper",
              content_len >= 0 && strcmp(content, "rename source\n") == 0);
        marker[0] = '\0';
        ssize_t marker_len = lgetxattr(upper_source, "user.union.whiteout", marker, sizeof(marker));
        check("RSITE4 rename source is whiteouted in upper", marker_len == 1 && marker[0] == 'y');
        if (dirfd >= 0)
            close(dirfd);
    }

    /* RSITE5. openat must copy an existing lower file before O_TRUNC/write and
       must create a new O_CREAT target in the upper parent when the path is
       relative to BSD AT_FDCWD. */
    {
        char lower_dir[4096], lower_existing[4096], upper_existing[4096];
        char lower_parent[4096], lower_created[4096], upper_parent[4096], upper_created[4096];
        snprintf(lower_dir, sizeof(lower_dir), "%s/var/log", libexec);
        snprintf(lower_existing, sizeof(lower_existing), "%s/syscall_open_existing", lower_dir);
        snprintf(upper_existing, sizeof(upper_existing), "%s/var/log/syscall_open_existing", prefix);
        int dirfd = open(lower_dir, O_RDONLY | O_DIRECTORY);
        int existing_fd = dirfd >= 0 ? (int)sys_openat(dirfd, "syscall_open_existing",
                                                       BSD_O_WRONLY | BSD_O_TRUNC, 0600) : -1;
        ssize_t written = existing_fd >= 0 ? write(existing_fd, "upper write\n", 12) : -1;
        if (existing_fd >= 0)
            sys_close(existing_fd);
        check("RSITE5 openat lower working dir opens", dirfd >= 0);
        check("RSITE5 openat(relative existing, write/truncate) succeeds", existing_fd >= 0);
        check("RSITE5 openat writes to the upper copy", written == 12 && access(upper_existing, F_OK) == 0);
        check("RSITE5 openat leaves existing lower file intact", access(lower_existing, F_OK) == 0);
        if (dirfd >= 0)
            close(dirfd);

        snprintf(lower_parent, sizeof(lower_parent), "%s/var/syscall_open_parent", libexec);
        snprintf(lower_created, sizeof(lower_created), "%s/created", lower_parent);
        snprintf(upper_parent, sizeof(upper_parent), "%s/var/syscall_open_parent", prefix);
        snprintf(upper_created, sizeof(upper_created), "%s/created", upper_parent);
        dirfd = open(lower_parent, O_RDONLY | O_DIRECTORY);
        eunion_test_perthread_wd = dirfd;
        int created_fd = dirfd >= 0 ? (int)sys_openat(BSD_AT_FDCWD, "created",
                                                      BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, 0600) : -1;
        written = created_fd >= 0 ? write(created_fd, "new upper\n", 10) : -1;
        if (created_fd >= 0)
            sys_close(created_fd);
        check("RSITE5 openat AT_FDCWD working dir opens", dirfd >= 0);
        check("RSITE5 openat(AT_FDCWD, relative O_CREAT) succeeds", created_fd >= 0);
        check("RSITE5 openat creates only in upper", written == 10 && access(upper_created, F_OK) == 0);
        check("RSITE5 openat does not create in lower", access(lower_created, F_OK) != 0);
        if (dirfd >= 0)
            close(dirfd);
        eunion_test_perthread_wd = -100;
    }

    /* RSITE6. The host unlink may succeed while the lower namesake still needs
       a whiteout. Inject that marker failure in TEST mode and require the
       syscall adapter to return it instead of reporting a false success. */
    {
        char lower_dir[4096], lower_file[4096], upper_file[4096];
        snprintf(lower_dir, sizeof(lower_dir), "%s/var/log", libexec);
        snprintf(lower_file, sizeof(lower_file), "%s/syscall_whiteout_fail", lower_dir);
        snprintf(upper_file, sizeof(upper_file), "%s/var/log/syscall_whiteout_fail", prefix);
        int dirfd = open(lower_dir, O_RDONLY | O_DIRECTORY);
        eunion_test_fail_whiteout = 1;
        int rv = dirfd >= 0 ? (int)sys_unlinkat(dirfd, "syscall_whiteout_fail", 0) : -1;
        eunion_test_fail_whiteout = 0;
        check("RSITE6 whiteout failure fixture opens", dirfd >= 0);
        check("RSITE6 post-unlink whiteout error reaches caller", rv == -LINUX_EIO);
        check("RSITE6 lower namesake remains intact after marker failure", access(lower_file, F_OK) == 0);
        check("RSITE6 failed removal preserves upper object", access(upper_file, F_OK) == 0);
        if (dirfd >= 0)
            close(dirfd);
    }

    /* P2. Opening an already-upper file must preserve its contents. */
    {
        snprintf(p, sizeof(p), "%s/usr/bin/myecho", prefix);
        char before[256], after[256];
        long before_len = slurp(p, before, sizeof(before));
        int fd = (int)sys_openat(BSD_AT_FDCWD, "/usr/bin/myecho", BSD_O_WRONLY, 0);
        check("P2 openat(upper, write) succeeds", fd >= 0);
        if (fd >= 0)
            sys_close(fd);
        long after_len = slurp(p, after, sizeof(after));
        check("P2 upper contents preserved",
              before_len >= 0 && after_len == before_len &&
              memcmp(before, after, (size_t)before_len) == 0);
    }

    /* P3. O_CREAT on a name absent from both layers creates only in upper. */
    {
        const char* guest = "/usr/bin/brand-new-file";
        int fd = (int)sys_openat(BSD_AT_FDCWD, guest,
                                 BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, 0600);
        check("P3 openat(absent, create) succeeds", fd >= 0);
        if (fd >= 0)
            sys_close(fd);
        snprintf(p, sizeof(p), "%s%s", prefix, guest);
        snprintf(l, sizeof(l), "%s%s", libexec, guest);
        check("P3 new file exists in upper only",
              access(p, F_OK) == 0 && access(l, F_OK) != 0);
    }

    /* P4. Removing a lower name must hide it without touching the template. */
    {
        const char* g = "/System/Library/LaunchDaemons/com.apple.test.plist";
        int rv = (int)sys_unlinkat(BSD_AT_FDCWD, g, 0);
        check("P4 unlinkat(lower-present) succeeds", rv == 0);
        snprintf(l, sizeof(l), "%s%s", libexec, g);
        check("P4 lower file survives unlink", access(l, F_OK) == 0);
        expect_absent(g);
    }

    /* P5. Removing an upper-only name leaves no spurious whiteout. */
    {
        const char* g = "/usr/bin/myecho";
        snprintf(p, sizeof(p), "%s/usr/bin/myecho", prefix);
        int rv = (int)sys_unlinkat(BSD_AT_FDCWD, g, 0);
        check("P5 unlinkat(upper-only) succeeds", rv == 0);
        check("P5 no spurious whiteout placeholder", access(p, F_OK) != 0);
        expect_absent(g);
    }

    /* P6. Recreating a whiteouted name must publish a visible directory. */
    {
        const char* g = "/usr/bin/ls";
        expect_absent(g);
        int rv = (int)sys_mkdir(g, 0755);
        check("P6 mkdir over a whiteout succeeds", rv == 0);
        snprintf(p, sizeof(p), "%s/usr/bin/ls", prefix);
        struct stat created;
        check("P6 whiteout replaced with upper directory",
              lstat(p, &created) == 0 && S_ISDIR(created.st_mode));
        expect_resolves_to(g, p);
    }

    printf("\n== E-UNION unlink/rmdir must not touch the template (dyra #1) ==\n");
    /* These call the real removal wrapper, including its native effect and
       transaction finish; helper return-code policy is not the oracle. */
    {
        const char* g = "/var/log/ulnk_lower";
        snprintf(l, sizeof(l), "%s/var/log/ulnk_lower", libexec);
        struct stat before; int had = (stat(l, &before) == 0);
        check("U1 fixture: lower-only victim present", had);
        int rv = (int)sys_unlinkat(BSD_AT_FDCWD, g, 0);
        check("U1 unlinkat(lower-only) succeeds", rv == 0);
        check("U1 template file UNTOUCHED after unlinkat", stat(l, &before) == 0);
        expect_absent(g);

        snprintf(p, sizeof(p), "%s/var/tmp/ulnk_upper", prefix);
        int up_present = (access(p, F_OK) == 0);
        check("U2 fixture: upper-only victim present", up_present);
        int rv2 = (int)sys_unlinkat(BSD_AT_FDCWD, "/var/tmp/ulnk_upper", 0);
        check("U2 unlinkat(upper-only) succeeds", rv2 == 0);
        check("U2 upper-only object is removed", access(p, F_OK) != 0);
        expect_absent("/var/tmp/ulnk_upper");

        int rv3 = (int)sys_unlinkat(BSD_AT_FDCWD, "/usr/bin/nonesuch", 0);
        check("U3 unlinkat(absent) reports ENOENT", rv3 == -ENOENT);
    }

    printf("\n== E-UNION paged getdents merge (per-fd state) ==\n");

    /* vchroot_getdents_merge(fd, out, cap) is the stateful, PAGED producer the
       getdirentries emulation calls instead of raw getdents64. It emits Linux
       dirent64 records, drains the UPPER fd first (skipping whiteout
       placeholders), then merges LOWER-only names (dedup, opaque-aware),
       maintaining per-fd cursor state across calls. Invariant: the union of all
       pages == the readdir_merge set, with no duplicates, terminating at 0. */

    /* helper: open a guest dir's UPPER host fd the way the emulation would
       (after openat resolved it to the upper layer), then drain via the paged
       merge into a name set using a SMALL buffer to force multiple pages. */
    #define DRAIN_INTO(guestdir, names_out, count_out) do {                    \
        char _h[4096]; expand((guestdir), _h);                                 \
        int _fd = open(_h, O_RDONLY | O_DIRECTORY);                            \
        (count_out) = 0; (names_out)[0] = '\0';                                \
        if (_fd >= 0) {                                                        \
            char _pg[256]; int _n;                                            \
            int _guard = 0;                                                    \
            while ((_n = vchroot_getdents_merge(_fd, _pg, sizeof(_pg))) > 0) { \
                int _p = 0;                                                    \
                while (_p < _n) {                                              \
                    struct linux_dirent64* _de = (void*)(_pg + _p);            \
                    _p += _de->d_reclen;                                       \
                    if (_de->d_name[0]=='.' && (_de->d_name[1]=='\0' ||        \
                        (_de->d_name[1]=='.'&&_de->d_name[2]=='\0'))) continue;\
                    /* append name to NUL-separated buffer */                  \
                    int _l = (int)strlen(_de->d_name)+1;                       \
                    char* _w = (names_out); int _u=0;                          \
                    for (int _i=0;_i<(count_out);_i++) _u+=(int)strlen(_w+_u)+1;\
                    memcpy(_w+_u,_de->d_name,_l); (count_out)++;               \
                }                                                              \
                if (++_guard > 1000) break;                                    \
            }                                                                  \
            sys_close(_fd);                                                    \
        }                                                                      \
    } while (0)

    /* G1. paged merge of a both-layers dir yields the SAME set as readdir_merge,
           deduped, even with a tiny page buffer forcing many getdents calls. */
    {
        /* fresh mergedir state: rebuild expectations from readdir_merge */
        char want[8192]; int wantn = vchroot_readdir_merge("/mergedir", want, sizeof(want));
        char got[8192]; int gotn;
        DRAIN_INTO("/mergedir", got, gotn);
        check("G1 paged merge non-empty", gotn > 0);
        /* every readdir_merge name appears exactly once in the paged output */
        int allmatch = (wantn == gotn);
        const char* wp = want;
        for (int i = 0; i < wantn; i++) {
            if (name_count(got, gotn, wp) != 1) allmatch = 0;
            wp += strlen(wp) + 1;
        }
        check("G1 paged set == readdir_merge set, no dups", allmatch);
    }

    /* G2. WM2's committed unlink created a real production whiteout. It must
       not be emitted when enumerating that directory through the fd pager. */
    {
        char got[8192]; int gotn;
        snprintf(p, sizeof(p), "%s/var/whboth/file", prefix);
        char marker[8];
        check("G2 fixture retains real unlink whiteout",
              lgetxattr(p, "user.union.whiteout", marker, sizeof(marker)) >= 1);
        DRAIN_INTO("/var/whboth", got, gotn);
        check("G2 whiteout absent from paged listing",
              !name_in(got, gotn, "file"));
    }

    /* G3. MANY-PAGE merge: 40 lower-only + 40 upper-only + 10 in-both names,
           drained through a 256-byte page buffer (dozens of getdents calls).
           The merged set must equal readdir_merge exactly with NO duplicates
           across page boundaries -- the per-fd emitted-set is what guarantees
           a both-layers name is not re-emitted when the lower phase starts. */
    {
        char want[16384]; int wantn = vchroot_readdir_merge("/bigmerge", want, sizeof(want));
        char got[16384]; int gotn;
        DRAIN_INTO("/bigmerge", got, gotn);
        check("G3 expected 90 unique names (40+40+10)", wantn == 90);
        check("G3 paged set size == readdir_merge size", gotn == wantn);
        int dup = 0, missing = 0;
        const char* wp = want;
        for (int i = 0; i < wantn; i++) {
            int c = name_count(got, gotn, wp);
            if (c == 0) missing++;
            if (c > 1) dup++;
            wp += strlen(wp) + 1;
        }
        check("G3 every name present exactly once (no cross-page dup)",
              dup == 0 && missing == 0);
        /* dup_* names came from both layers: confirm each appears once */
        check("G3 in-both name 'dup_5' appears exactly once",
              name_count(got, gotn, "dup_5") == 1);
    }

    /* G4 (.7c). The paged merge must emit a VALID d_type and a real d_ino for
       each entry, not the hardcoded DT_UNKNOWN(0)/d_ino=1. overlayfs requires a
       valid d_type (callers like find/ls -F and readdir consumers that trust
       d_type to avoid an lstat get it wrong otherwise). Drain /var/dtcheck and
       look up the type/ino the pager reported for a known SUBDIR and a known
       FILE entry. With the unfixed emitter (d_type=0, d_ino=1) every lookup is
       DT_UNKNOWN with ino 1 -> RED. */
    {
        unsigned char ty_sub = 0xEE, ty_file = 0xEE;
        unsigned long ino_sub = 0, ino_file = 0;
        char _h[4096]; expand("/var/dtcheck", _h);
        int _fd = open(_h, O_RDONLY | O_DIRECTORY);
        if (_fd >= 0) {
            char _pg[256]; int _n, _guard = 0;
            while ((_n = vchroot_getdents_merge(_fd, _pg, sizeof(_pg))) > 0) {
                int _p = 0;
                while (_p < _n) {
                    struct linux_dirent64* _de = (void*)(_pg + _p);
                    _p += _de->d_reclen;
                    if (strcmp(_de->d_name, "a_subdir") == 0) {
                        ty_sub = _de->d_type; ino_sub = (unsigned long)_de->d_ino;
                    } else if (strcmp(_de->d_name, "a_file") == 0) {
                        ty_file = _de->d_type; ino_file = (unsigned long)_de->d_ino;
                    }
                }
                if (++_guard > 1000) break;
            }
            sys_close(_fd);
        }
        check("G4 fixture: dtcheck dir opened and drained", _fd >= 0);
        check("G4 subdir entry reports d_type == DT_DIR (4)", ty_sub == DT_DIR);
        check("G4 file entry reports d_type == DT_REG (8)", ty_file == DT_REG);
        check("G4 subdir entry has a real inode (not synthetic 1)", ino_sub > 1);
        check("G4 file entry has a real inode (not synthetic 1)", ino_file > 1);
    }


    large_directory_checks();

    #undef DRAIN_INTO

    printf("\n== E-UNION fd/path detranslation (lower template -> guest path) ==\n");
    /* The bug that stalled launchctl bootstrap: a fd/path resolving into the
       read-only LOWER template lives OUTSIDE the prefix, so vchroot_fdpath /
       vchroot_unexpand fell through to the EXIT_PATH escape hatch and produced a
       bogus /Volumes/SystemRoot/<template-path> guest path. launchctl F_GETPATHs
       the LaunchDaemons dir fd (resolved to the lower template) to build child
       plist paths; the escape-hatch path made every plist lookup fail -> no
       daemons loaded -> `bootstrap -S System` waited 60s on services that never
       checked in. These tests pin the lower-layer detranslation. */
    {
        char got[4096];
        struct vchroot_fdpath_args fp;

        /* F1. fd opened on a LOWER-only file detranslates to the bare guest path,
               NOT the /Volumes/SystemRoot escape hatch. */
        snprintf(l, sizeof(l), "%s/System/Library/LaunchDaemons", libexec);
        int dfd = open(l, O_RDONLY | O_DIRECTORY);
        check("F1 lower dir opens", dfd >= 0);
        if (dfd >= 0) {
            fp.fd = dfd; fp.path = got; fp.maxlen = sizeof(got);
            int rv = vchroot_fdpath(&fp);
            check("F1 fdpath of lower dir returns 0", rv == 0);
            if (rv == 0) {
                if (strcmp(got, "/System/Library/LaunchDaemons") != 0)
                    printf("  got: %s\n", got);
                check("F1 lower fd -> guest path (no /Volumes/SystemRoot escape)",
                      strcmp(got, "/System/Library/LaunchDaemons") == 0);
            }
            close(dfd);
        }

        /* F2. fd opened on an UPPER file still detranslates correctly. */
        snprintf(p, sizeof(p), "%s/usr/bin/myecho", prefix);
        int ufd = open(p, O_RDONLY);
        if (ufd >= 0) {
            fp.fd = ufd; fp.path = got; fp.maxlen = sizeof(got);
            int rv = vchroot_fdpath(&fp);
            check("F2 upper fd -> guest path",
                  rv == 0 && strcmp(got, "/usr/bin/myecho") == 0);
            close(ufd);
        }

        /* F3. a fd genuinely outside BOTH layers still gets the escape hatch
               (e.g. /etc/hostname on the host) -- detranslation must not
               misclassify it as a guest path. */
        int hfd = open("/dev/null", O_RDONLY);
        if (hfd >= 0) {
            fp.fd = hfd; fp.path = got; fp.maxlen = sizeof(got);
            int rv = vchroot_fdpath(&fp);
            check("F3 out-of-both fd -> /Volumes/SystemRoot escape hatch",
                  rv == 0 && strncmp(got, "/Volumes/SystemRoot", 19) == 0);
            close(hfd);
        }

        /* F3b (7cfb5e16). A DIRECTORY fd outside BOTH layers must not be MERGED.
               The escape hatch names no upper and no lower path, so the union has
               nothing to merge for it and vchroot_getdents_merge must hand back the
               kernel's RAW getdents64 listing. Running the merge anyway looks up
               <upper>/Volumes/SystemRoot/... and <lower>/Volumes/SystemRoot/...,
               finds neither, and reports a directory that has entries as EMPTY
               with no error the caller can tell apart from a genuinely empty one
               (this is what made DVTPlatform's platform scan see zero bundles).
               Fixture: the harness root (the cwd) -- the PARENT of both union
               layers, so its fd is a real escape hatch, the same class of fd F3
               reads. Both arms read the SAME fd (rewound in between): the union
               entry point and LINUX_SYSCALL(__NR_getdents64), and the two name
               sets must match (non-empty), or the union is lying about a
               directory it does not own. */
        {
            char root[4096], mnames[16384], rnames[16384];
            int mcount = 0, rcount = 0;
            getcwd(root, sizeof(root));
            int rdfd = open(root, O_RDONLY | O_DIRECTORY);
            check("F3b fixture: out-of-union dir fd opens", rdfd >= 0);
            if (rdfd >= 0) {
                fp.fd = rdfd; fp.path = got; fp.maxlen = sizeof(got);
                int rv = vchroot_fdpath(&fp);
                check("F3b out-of-union dir fd -> /Volumes/SystemRoot escape hatch",
                      rv == 0 && strncmp(got, "/Volumes/SystemRoot/", 20) == 0);

                int mr = nameset_merged(rdfd, mnames, (int)sizeof(mnames), &mcount);
                lseek(rdfd, 0, SEEK_SET);
                int rr = nameset_raw(rdfd, rnames, (int)sizeof(rnames), &rcount);
                printf("  note: escape-hatch listing had %d merged names, %d raw names\n",
                       mcount, rcount);
                check("F3b escape-hatch dirfd returns the RAW getdents64 name set",
                      mr == 0 && rr == 0 &&
                      nameset_equal(rnames, rcount, mnames, mcount));
                close(rdfd);
            }
        }

        /* F4. vchroot_unexpand (path-string variant) detranslates a lower host
               path to the guest path, mirroring F1. */
        {
            struct vchroot_unexpand_args ua;
            snprintf(ua.path, sizeof(ua.path), "%s/System/Library/LaunchDaemons", libexec);
            int rv = vchroot_unexpand(&ua);
            check("F4 unexpand lower host path -> guest path",
                  rv == 0 && strcmp(ua.path, "/System/Library/LaunchDaemons") == 0);
        }

        /* F5. unexpand of an upper host path -> guest path. */
        {
            struct vchroot_unexpand_args ua;
            snprintf(ua.path, sizeof(ua.path), "%s/usr/bin/myecho", prefix);
            int rv = vchroot_unexpand(&ua);
            check("F5 unexpand upper host path -> guest path",
                  rv == 0 && strcmp(ua.path, "/usr/bin/myecho") == 0);
        }
    }

    printf("\n== E-UNION hardening (dyra #1..#5) ==\n");
    {
        /* H4. copy-up STRIPS setid and PRESERVES xattrs.
               Lower /usr/bin/suid_tool is mode 4755 with user.test.tag=hello. */
        {
            int rv = vchroot_copyup("/usr/bin/suid_tool");
            snprintf(p, sizeof(p), "%s/usr/bin/suid_tool", prefix);
            check("H4 copyup suid file returns 0", rv == 0);
            struct stat us; memset(&us, 0, sizeof us); stat(p, &us);
            check("H4 setuid bit STRIPPED on copy-up (no S_ISUID in upper)",
                  rv == 0 && !(us.st_mode & S_ISUID));
            check("H4 perm bits preserved (0755)",
                  rv == 0 && (us.st_mode & 0777) == 0755);
            char xv[64];
            ssize_t xn = getxattr(p, "user.test.tag", xv, sizeof(xv));
            /* only assert preservation if the fixture could set the xattr at all */
            char lx[64]; snprintf(l, sizeof(l), "%s/usr/bin/suid_tool", libexec);
            ssize_t lxn = getxattr(l, "user.test.tag", lx, sizeof(lx));
            if (lxn > 0)
                check("H4 user.* xattr preserved on copy-up",
                      xn == lxn && memcmp(xv, lx, xn) == 0);
            else
                printf("  skip H4 xattr (fixture could not set user.test.tag)\n");

            /* H4cap. security.capability is setuid-equivalent: copy-up MUST drop
               it (overlay/gVisor clear it on a data copy-up). Only assert if the
               LOWER fixture actually carries it (the kernel/FS may refuse the
               raw setxattr). The upper copy must then have NO such xattr. */
            char cap[64];
            ssize_t lcap = lgetxattr(l, "security.capability", cap, sizeof(cap));
            if (lcap > 0) {
                ssize_t ucap = lgetxattr(p, "security.capability", cap, sizeof(cap));
                check("H4cap security.capability DROPPED on copy-up",
                      ucap < 0); /* absent in upper */
            } else {
                printf("  skip H4cap (fixture could not set security.capability)\n");
            }
        }

        /* H3. copy-up BREAKS hardlinks: lower /usr/lib/hl_a has nlink==2; the
               upper copy must be an independent inode with nlink==1. */
        {
            int rv = vchroot_copyup("/usr/lib/hl_a");
            snprintf(p, sizeof(p), "%s/usr/lib/hl_a", prefix);
            struct stat us; memset(&us, 0, sizeof us); stat(p, &us);
            check("H3 copyup hardlinked file returns 0", rv == 0);
            check("H3 upper copy has nlink == 1 (link broken)",
                  rv == 0 && us.st_nlink == 1);
            /* and it must NOT share the lower inode */
            snprintf(l, sizeof(l), "%s/usr/lib/hl_a", libexec);
            struct stat ls; stat(l, &ls);
            check("H3 upper copy is a distinct inode from the template",
                  rv == 0 && !(us.st_ino == ls.st_ino && us.st_dev == ls.st_dev));
        }

        /* H1. rename of a lower-only DIRECTORY with contents copies up ALL
               descendants. We call the same primitive the renameat wiring uses
               (vchroot_copyup_tree on the source dir) and then verify the whole
               subtree is present in the upper layer. */
        {
            int rv = vchroot_copyup_tree("/rendir");
            check("H1 copyup_tree(lower dir) returns 0", rv == 0);
            snprintf(p, sizeof(p), "%s/rendir/f1", prefix);
            check("H1 top-level child copied up (/rendir/f1)", access(p, F_OK) == 0);
            snprintf(p, sizeof(p), "%s/rendir/sub/f2", prefix);
            check("H1 nested descendant copied up (/rendir/sub/f2)",
                  access(p, F_OK) == 0);
        }

        /* H5. copy-up of a lower-only SYMLINK reproduces it as a symlink (not a
               dereferenced copy), with the same target string. */
        {
            int rv = vchroot_copyup("/usr/lib/lnk");
            snprintf(p, sizeof(p), "%s/usr/lib/lnk", prefix);
            struct stat us; memset(&us, 0, sizeof us);
            lstat(p, &us);
            check("H5 copyup symlink returns 0", rv == 0);
            check("H5 upper object is itself a symlink", rv == 0 && S_ISLNK(us.st_mode));
            char tgt[256]; ssize_t tn = readlink(p, tgt, sizeof(tgt)-1);
            if (tn > 0) tgt[tn] = '\0';
            check("H5 symlink target preserved ('target_file')",
                  rv == 0 && tn > 0 && strcmp(tgt, "target_file") == 0);
        }

        /* H2. A dirfd-relative writable open must resolve the guest path and
               materialize the fresh lower-only target before the native open. */
        {
            /* open the guest dir /usr/lib (resolves into the lower template) */
            char dirhost[4096];
            snprintf(dirhost, sizeof(dirhost), "%s/usr/lib", libexec);
            int dfd = open(dirhost, O_RDONLY | O_DIRECTORY);
            check("H2 guest dir fd opens", dfd >= 0);
            if (dfd >= 0) {
                int fd = (int)sys_openat(dfd, "target_file", BSD_O_WRONLY, 0);
                check("H2 openat(dirfd, relative write) succeeds", fd >= 0);
                if (fd >= 0)
                    sys_close(fd);
                snprintf(p, sizeof(p), "%s/usr/lib/target_file", prefix);
                check("H2 relative-path target materialized in upper",
                      access(p, F_OK) == 0);
                close(dfd);
            }
        }

        /* H2b (7cfb5e16). The other half of the H2 gate: a dirfd that resolves
               OUTSIDE both union layers (the /Volumes/SystemRoot escape hatch)
               must be a NO-OP for relative-path copy-up. The target lives on the
               native host root, so there is nothing to copy up and nothing may be
               materialized under the prefix. The gate used to ask `base[0] != '/'`,
               which a SUCCESSFUL vchroot_fdpath never produces -- the branch was
               dead and never asked the escape-hatch question its comment claimed.
               Fixture: the harness root (cwd), the parent of both layers, whose fd
               is the same hatch class F3/F3b read. */
        {
            char root[4096];
            getcwd(root, sizeof(root));
            int rdfd = open(root, O_RDONLY | O_DIRECTORY);
            check("H2b fixture: out-of-union dir fd opens", rdfd >= 0);
            if (rdfd >= 0) {
                int fd = (int)sys_openat(rdfd, "no_such_union_target", BSD_O_WRONLY, 0);
                check("H2b escape-hatch relative open preserves native ENOENT",
                      fd == -ENOENT);
                if (fd >= 0)
                    sys_close(fd);
                snprintf(p, sizeof(p), "%s/Volumes/SystemRoot", prefix);
                check("H2b escape hatch never materialized into the union",
                      access(p, F_OK) != 0);
                close(rdfd);
            }
        }

        /* CM (.7b). copy-up must PRESERVE the source mtime (and uid/gid where the
               process is privileged enough), like overlayfs -- a build that resets
               mtime to "now" on every copy-up breaks make/ninja/ccache staleness
               checks. Fixture: a DEDICATED lower-only file stamped with a known,
               distinctly-OLD mtime (2001-01-01). After copy-up the upper copy's
               mtime must equal the lower's, NOT the current time. With the unfixed
               copy_regular (no utimensat) the upper mtime is ~now -> RED. */
        {
            snprintf(l, sizeof(l), "%s/var/log/cm_mtime", libexec);
            snprintf(p, sizeof(p), "%s/var/log/cm_mtime", prefix);
            unlink(p); /* pristine: not copied up by an earlier test */
            struct stat ls; int lok = (stat(l, &ls) == 0);
            check("CM fixture: lower-only file present", lok);

            int rv = vchroot_copyup("/var/log/cm_mtime");
            check("CM copyup returns 0", rv == 0);
            struct stat us; int uok = (stat(p, &us) == 0);
            check("CM upper copy materialized", uok);
            /* the decisive assertion: upper mtime == lower mtime (seconds), and it
               is the OLD stamp, not ~now (guards against a same-second fluke). */
            if (lok && uok) {
                check("CM upper mtime preserved (== lower mtime sec)",
                      us.st_mtime == ls.st_mtime);
                time_t now = time(NULL);
                check("CM upper mtime is the OLD stamp, not ~now",
                      (now - us.st_mtime) > 1000000); /* >~11 days old */
                /* uid/gid: unprivileged we cannot chown to a foreign uid, so the
                   lower fixture is owned by us and this just confirms copy-up did
                   not corrupt ownership. A true cross-uid preserve needs root. */
                check("CM upper uid preserved", us.st_uid == ls.st_uid);
                check("CM upper gid preserved", us.st_gid == ls.st_gid);
            }
        }
    }

    printf("\n== E-UNION rename destination must not touch the template (dyra #2) ==\n");
    /* Run the real paired rename transaction: destination parents and replaced
       lower names must remain protected through native rename and finish. */
    {
        /* RN1. dest parent is lower-only: must copy it up so the rename lands in
                the upper layer, template parent left untouched. */
        snprintf(p, sizeof(p), "%s/var/tmp/rename_runner_source", prefix);
        int source = open(p, O_CREAT | O_EXCL | O_WRONLY, 0600);
        check("RN1 upper source fixture created", source >= 0);
        ssize_t written = source >= 0 ? write(source, "rename payload\n", 15) : -1;
        if (source >= 0)
            close(source);
        check("RN1 upper source fixture populated", written == 15);
        int rv = (int)sys_renameat(BSD_AT_FDCWD, "/var/tmp/rename_runner_source",
                                    BSD_AT_FDCWD, "/var/empty_lowerdir/moved");
        check("RN1 rename into lower-only dir succeeds", rv == 0);
        snprintf(p, sizeof(p), "%s/var/empty_lowerdir", prefix);
        check("RN1 dest parent materialized in upper", access(p, F_OK) == 0);
        snprintf(l, sizeof(l), "%s/var/empty_lowerdir", libexec);
        check("RN1 template dest parent untouched", access(l, F_OK) == 0);
        snprintf(p, sizeof(p), "%s/var/empty_lowerdir/moved", prefix);
        char content[256];
        check("RN1 upper destination has source contents",
              slurp(p, content, sizeof(content)) == 15 &&
              strcmp(content, "rename payload\n") == 0);
        snprintf(l, sizeof(l), "%s/var/empty_lowerdir/moved", libexec);
        check("RN1 no destination leaks into lower", access(l, F_OK) != 0);

        /* RN2. Replacing a lower destination publishes the source contents,
                without changing the lower victim, even after later unlink. */
        const char* dst = "/var/log/ren_victim";
        char before[256], after[256];
        snprintf(l, sizeof(l), "%s/var/log/ren_victim", libexec);
        long before_len = slurp(l, before, sizeof(before));
        int rv2 = (int)sys_renameat(BSD_AT_FDCWD, "/var/empty_lowerdir/moved",
                                     BSD_AT_FDCWD, dst);
        check("RN2 rename over lower destination succeeds", rv2 == 0);
        snprintf(p, sizeof(p), "%s/var/log/ren_victim", prefix);
        check("RN2 upper destination has replacement contents",
              slurp(p, content, sizeof(content)) == 15 &&
              strcmp(content, "rename payload\n") == 0);
        expect_resolves_to(dst, p);
        long after_len = slurp(l, after, sizeof(after));
        check("RN2 template dest victim contents untouched",
              before_len >= 0 && after_len == before_len &&
              memcmp(before, after, (size_t)before_len) == 0);
        check("RN2 unlink replacement succeeds",
              sys_unlinkat(BSD_AT_FDCWD, dst, 0) == 0);
        expect_absent(dst);
    }

    printf("\n== E-UNION create must copy up a lower-only parent (dyra #3) ==\n");
    /* Exclusive creation must materialize a lower-only parent and publish the
       child in upper, without leaking a new entry into the shared template. */
    {
        /* This dedicated parent is not touched by the rename tests. */
        const char* g = "/var/createparent/newfile";
        /* guard: parent must NOT pre-exist in the upper layer */
        snprintf(p, sizeof(p), "%s/var/createparent", prefix);
        check("CR1 fixture: parent absent from upper before create",
              access(p, F_OK) != 0);
        int fd = (int)sys_openat(BSD_AT_FDCWD, g,
                                 BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, 0644);
        check("CR1 create in lower-only parent succeeds", fd >= 0);
        if (fd >= 0)
            sys_close(fd);
        check("CR1 parent dir materialized in upper", access(p, F_OK) == 0);
        snprintf(p, sizeof(p), "%s/var/createparent/newfile", prefix);
        check("CR1 new file materialized in upper", access(p, F_OK) == 0);
        snprintf(l, sizeof(l), "%s/var/createparent", libexec);
        check("CR1 template parent untouched", access(l, F_OK) == 0);
        snprintf(l, sizeof(l), "%s/var/createparent/newfile", libexec);
        check("CR1 no new file leaked into template", access(l, F_OK) != 0);
    }

    /* SP1. Creating through a lower-only symlinked parent must materialize
       its real target directory, not follow an upper link to a missing dir.
       Fixture: /var/sp_link -> sp_real (relative), both lower-only. */
    {
        const char* g = "/var/sp_link/newfile";
        /* guards: neither the symlink target nor the symlink itself is upper yet */
        snprintf(p, sizeof(p), "%s/var/sp_real", prefix);
        check("SP1 fixture: target dir absent from upper before create",
              access(p, F_OK) != 0);
        int fd = (int)sys_openat(BSD_AT_FDCWD, g,
                                 BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, 0644);
        check("SP1 create through guest symlink succeeds (no ENOENT)", fd >= 0);
        if (fd >= 0)
            sys_close(fd);
        /* THE FIX: the symlink's real target dir must be a real upper directory */
        struct stat tst;
        snprintf(p, sizeof(p), "%s/var/sp_real", prefix);
        check("SP1 symlink target dir materialized as a real upper dir",
              stat(p, &tst) == 0 && S_ISDIR(tst.st_mode));
        snprintf(p, sizeof(p), "%s/var/sp_real/newfile", prefix);
        check("SP1 created file landed in upper real target dir",
              access(p, F_OK) == 0);
        /* template target dir untouched (no newfile leaked into the template) */
        snprintf(l, sizeof(l), "%s/var/sp_real/newfile", libexec);
        check("SP1 template target dir untouched (no leak)", access(l, F_OK) != 0);
    }

    /* An upper directory masks a lower symlink, including a host-side alias
       whose relative spelling would escape the guest root if interpreted. */
    {
        const char* target = "../../../masked-target";
        snprintf(p, sizeof(p), "%s/var/shadowed_parent", prefix);
        snprintf(l, sizeof(l), "%s/var/shadowed_parent", libexec);
        check("SP2 upper parent directory created", mkdir(p, 0755) == 0);
        check("SP2 lower alias created", symlink(target, l) == 0);
        int rv = (int)sys_mkdirat(BSD_AT_FDCWD, "/var/shadowed_parent/new", 0700);
        check("SP2 mkdir ignores lower symlink masked by upper directory", rv == 0);
        snprintf(p, sizeof(p), "%s/var/shadowed_parent/new", prefix);
        struct stat created;
        check("SP2 new directory lands in upper",
              lstat(p, &created) == 0 && S_ISDIR(created.st_mode));
        char link[64];
        ssize_t length = readlink(l, link, sizeof(link));
        check("SP2 lower symlink remains unchanged",
              length == (ssize_t)strlen(target) && memcmp(link, target, length) == 0);
    }

    /* SAFE1. A lower symlink containing .. must not let a mutating open escape
       the upper root. The sentinel lives beside prefix, where a raw joined path
       such as prefix/var/../../escape-target would reach it. */
    {
        char work_root[4096], outside[4096], upper_target[4096];
        snprintf(work_root, sizeof(work_root), "%s", prefix);
        char* slash = strrchr(work_root, '/');
        if (slash)
            *slash = '\0';
        snprintf(outside, sizeof(outside), "%s/escape-target", work_root);
        snprintf(upper_target, sizeof(upper_target), "%s/escape-target", prefix);
        int fd = (int)sys_openat(BSD_AT_FDCWD, "/var/escape_link/created",
                                 BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, 0600);
        check("SAFE1 symlink .. mutation is rejected", fd < 0);
        if (fd >= 0)
            sys_close(fd);
        check("SAFE1 outside sentinel remains intact", access(outside, F_OK) == 0);
        check("SAFE1 no escaped upper target was created", access(upper_target, F_OK) != 0);
    }

    printf("\n== E-UNION mkdir over a removed lower dir must be opaque (.6) ==\n");
    /* Delete visible children before rmdir. Their lower bytes remain in the
       immutable template; after removing and recreating the parent, opacity
       (not invalid rmdir semantics) must keep those stale children hidden. */
    {
        const char* dir = "/var/recreate";
        snprintf(l, sizeof(l), "%s/var/recreate", libexec);
        snprintf(p, sizeof(p), "%s/var/recreate", prefix);

        /* precondition: lower dir present + populated, no upper object yet */
        check("MK1 fixture: lower dir present", access(l, F_OK) == 0);
        check("MK1 fixture: lower child 'stale_a' present",
              name_in_dir(l, "stale_a"));
        check("MK1 fixture: no upper object before recreate", access(p, F_OK) != 0);
        /* and the merged view initially shows the lower children */
        {
            char nm[8192]; int nc = vchroot_readdir_merge(dir, nm, sizeof(nm));
            check("MK1 fixture: merged view shows lower children before delete",
                  nc >= 0 && name_in(nm, nc, "stale_a") && name_in(nm, nc, "stale_b"));
        }

        check("MK1 unlink first visible child",
              sys_unlinkat(BSD_AT_FDCWD, "/var/recreate/stale_a", 0) == 0);
        check("MK1 unlink second visible child",
              sys_unlinkat(BSD_AT_FDCWD, "/var/recreate/stale_b", 0) == 0);
        check("MK1 remove now-empty merged directory", sys_rmdir(dir) == 0);
        expect_absent(dir);
        check("MK1 recreate removed directory", sys_mkdir(dir, 0755) == 0);

        /* THE ASSERTION: the recreated dir's merged view must be EMPTY -- the old
           lower children must NOT resurrect. With the RED stub (post_mkdir is a
           no-op), the dir is not opaque so the lower children re-merge -> RED. */
        {
            char nm[8192]; int nc = vchroot_readdir_merge(dir, nm, sizeof(nm));
            check("MK1 recreated dir merged view returns >= 0", nc >= 0);
            check("MK1 stale lower child 'stale_a' does NOT resurrect",
                  !name_in(nm, nc, "stale_a"));
            check("MK1 stale lower child 'stale_b' does NOT resurrect",
                  !name_in(nm, nc, "stale_b"));
        }
        /* structural: the upper dir carries the opaque marker */
        {
            char xv[8];
            ssize_t xn = lgetxattr(p, "user.union.opaque", xv, sizeof(xv));
            check("MK1 recreated upper dir carries user.union.opaque marker",
                  xn >= 1);
        }
        /* the shared template dir + its children are UNTOUCHED on disk */
        check("MK1 template dir untouched", access(l, F_OK) == 0);
        {
            char lc[4096]; snprintf(lc, sizeof(lc), "%s/var/recreate/stale_a", libexec);
            check("MK1 template child untouched on disk", access(lc, F_OK) == 0);
        }

        /* MK2. NEGATIVE: a mkdir of a brand-new name with NO lower namesake must
                NOT be marked opaque (post_mkdir is a no-op there). A spurious
                opaque is harmless for emptiness but would wrongly hide future
                lower content and signals an over-broad implementation. */
        {
            const char* fresh = "/var/freshdir";
            snprintf(p, sizeof(p), "%s/var/freshdir", prefix);
            unlink(p); rmdir(p); /* pristine */
            check("MK2 mkdir of fresh name succeeds", sys_mkdir(fresh, 0755) == 0);
            struct stat created;
            check("MK2 fresh upper directory exists",
                  stat(p, &created) == 0 && S_ISDIR(created.st_mode));
            char xv[8];
            ssize_t xn = lgetxattr(p, "user.union.opaque", xv, sizeof(xv));
            check("MK2 fresh dir is NOT opaque (no lower namesake to mask)",
                  xn < 0);
        }
    }

    printf("\n== E-UNION fd-metadata copy-up must not touch the template (dyra #4) ==\n");
    /* Metadata mutations must rebind the caller's open description and every
       preexisting alias to upper, while leaving shared lower metadata intact. */
    {
        /* FD1. The caller keeps using the same read-only descriptor. */
        snprintf(l, sizeof(l), "%s/var/log/fdmeta_lower", libexec);
        struct stat original;
        int original_ok = stat(l, &original) == 0;
        int lfd = (int)sys_openat(BSD_AT_FDCWD, "/var/log/fdmeta_lower", 0, 0);
        check("FD1 fixture: lower-only file opens O_RDONLY", lfd >= 0);
        if (lfd >= 0) {
            check("FD1 fchmod on caller fd succeeds", sys_fchmod(lfd, 0640) == 0);
            snprintf(p, sizeof(p), "%s/var/log/fdmeta_lower", prefix);
            struct stat upper, caller, lower;
            int upper_ok = stat(p, &upper) == 0;
            check("FD1 file materialized in upper", upper_ok);
            check("FD1 SAME caller fd observes upper metadata",
                  upper_ok && fstat(lfd, &caller) == 0 &&
                  caller.st_dev == upper.st_dev && caller.st_ino == upper.st_ino &&
                  (caller.st_mode & 0777) == 0640);
            check("FD1 template metadata untouched",
                  original_ok && stat(l, &lower) == 0 &&
                  lower.st_ino == original.st_ino &&
                  lower.st_mode == original.st_mode);
            sys_close(lfd);
        }

        /* FD2. A duplicate created before copy-up must observe the same new
           upper inode and metadata as the original caller descriptor. */
        {
            snprintf(l, sizeof(l), "%s/var/log/fdmeta_e2e", libexec);
            snprintf(p, sizeof(p), "%s/var/log/fdmeta_e2e", prefix);
            unlink(p);
            struct stat lst0;
            int lower_ok = stat(l, &lst0) == 0;
            check("FD2 fixture: template starts 0644",
                  lower_ok && (lst0.st_mode & 0777) == 0644);

            int fd = (int)sys_openat(BSD_AT_FDCWD, "/var/log/fdmeta_e2e", 0, 0);
            check("FD2 fixture: lower file opens O_RDONLY", fd >= 0);
            if (fd >= 0) {
                int alias = (int)sys_dup(fd);
                check("FD2 preexisting alias opens", alias >= 0);
                check("FD2 fchmod on caller fd succeeds", sys_fchmod(fd, 0600) == 0);
                struct stat upper, caller, duplicate, lower;
                int upper_ok = stat(p, &upper) == 0;
                check("FD2 upper copy materialized", upper_ok);
                check("FD2 upper copy got the new mode (0600)",
                      upper_ok && (upper.st_mode & 0777) == 0600);
                check("FD2 SAME caller fd observes new upper metadata",
                      upper_ok && fstat(fd, &caller) == 0 &&
                      caller.st_dev == upper.st_dev && caller.st_ino == upper.st_ino &&
                      (caller.st_mode & 0777) == 0600);
                check("FD2 preexisting alias observes new upper metadata",
                      upper_ok && alias >= 0 && fstat(alias, &duplicate) == 0 &&
                      duplicate.st_dev == upper.st_dev && duplicate.st_ino == upper.st_ino &&
                      (duplicate.st_mode & 0777) == 0600);
                check("FD2 template mode UNTOUCHED (still 0644)",
                      lower_ok && stat(l, &lower) == 0 &&
                      lower.st_mode == lst0.st_mode && lower.st_ino == lst0.st_ino);
                if (alias >= 0)
                    sys_close(alias);
                sys_close(fd);
            }
        }

        /* FD3. An already-upper open description and its alias stay coherent. */
        {
            snprintf(p, sizeof(p), "%s/var/tmp/fdmeta_upper", prefix);
            int fd = (int)sys_openat(BSD_AT_FDCWD, "/var/tmp/fdmeta_upper", 0, 0);
            check("FD3 fixture: upper-only file opens", fd >= 0);
            if (fd >= 0) {
                int alias = (int)sys_dup(fd);
                check("FD3 preexisting alias opens", alias >= 0);
                check("FD3 fchmod on caller fd succeeds", sys_fchmod(fd, 0600) == 0);
                struct stat upper, caller, duplicate;
                int upper_ok = stat(p, &upper) == 0;
                check("FD3 upper file got the new mode (0600)",
                      upper_ok && (upper.st_mode & 0777) == 0600);
                check("FD3 SAME caller fd retains upper identity and mode",
                      upper_ok && fstat(fd, &caller) == 0 &&
                      caller.st_dev == upper.st_dev && caller.st_ino == upper.st_ino &&
                      (caller.st_mode & 0777) == 0600);
                check("FD3 preexisting alias observes upper metadata",
                      upper_ok && alias >= 0 && fstat(alias, &duplicate) == 0 &&
                      duplicate.st_dev == upper.st_dev && duplicate.st_ino == upper.st_ino &&
                      (duplicate.st_mode & 0777) == 0600);
                if (alias >= 0)
                    sys_close(alias);
                sys_close(fd);
            }
        }

        /* FD4. A host-owned fixture outside both layers must retain native
           metadata semantics and caller identity, without a union copy. */
        {
            const char* host = "fdmeta_unmanaged";
            int fd = open(host, O_CREAT | O_EXCL | O_RDONLY, 0644);
            check("FD4 fixture: unmanaged host file opens", fd >= 0);
            if (fd >= 0) {
                check("FD4 unmanaged descriptor registration succeeds",
                      vchroot_fd_opened(fd) == 0);
                struct stat before, caller, after;
                int before_ok = fstat(fd, &before) == 0;
                check("FD4 native metadata mutation succeeds", sys_fchmod(fd, 0600) == 0);
                check("FD4 SAME caller fd retains native identity and mode",
                      before_ok && fstat(fd, &caller) == 0 &&
                      stat(host, &after) == 0 &&
                      caller.st_dev == before.st_dev && caller.st_ino == before.st_ino &&
                      caller.st_ino == after.st_ino && (caller.st_mode & 0777) == 0600);
                sys_close(fd);
                unlink(host);
            }
        }

        /* Descriptor validation must precede copy-up. A read-only descriptor
           must not acquire write access by being reopened in the upper layer. */
        {
            check("FT negative descriptor reports EBADF",
                  sys_ftruncate(-1, 0) == -EBADF);
            int closed_fd = dup(STDOUT_FILENO);
            check("FT closed-descriptor fixture opens", closed_fd >= 0);
            if (closed_fd >= 0) {
                close(closed_fd);
                check("FT closed descriptor reports EBADF",
                      sys_ftruncate(closed_fd, 0) == -EBADF);
            }

            snprintf(l, sizeof(l), "%s/var/log/ftrunc_lower", libexec);
            snprintf(p, sizeof(p), "%s/var/log/ftrunc_lower", prefix);
            unlink(p);
            struct stat original;
            int original_ok = stat(l, &original) == 0;
            check("FT template starts non-empty",
                  original_ok && original.st_size > 0);

            int fd = open(l, O_RDONLY);
            check("FT read-only lower descriptor opens", fd >= 0);
            if (fd >= 0) {
                check("FT read-only descriptor registration succeeds",
                      vchroot_fd_opened(fd) == 0);
                check("FT read-only descriptor reports EBADF",
                      sys_ftruncate(fd, 0) == -EBADF);
                sys_close(fd);
                check("FT rejected mutation does not copy up",
                      access(p, F_OK) == -1 && errno == ENOENT);
            }

            fd = open(l, O_RDWR);
            check("FT writable lower descriptor opens", fd >= 0);
            if (fd >= 0) {
                check("FT writable descriptor registration succeeds",
                      vchroot_fd_opened(fd) == 0);
                check("FT writable descriptor truncates successfully",
                      sys_ftruncate(fd, 0) == 0);
                sys_close(fd);
                struct stat upper;
                check("FT upper copy is truncated",
                      stat(p, &upper) == 0 && upper.st_size == 0);
            }
            struct stat lower;
            check("FT template contents retain their original size",
                  original_ok && stat(l, &lower) == 0 &&
                  lower.st_size == original.st_size);
        }
    }

    printf("\n== E-UNION xattr marker-namespace isolation (dyra .5) ==\n");
    /* The guest must never forge or observe the union's overlay-private markers.
       The path-based setxattr/removexattr/getxattr/listxattr sites all consult
       vchroot_xattr_is_marker(name) to reject (set/remove -> EPERM) or hide
       (get -> ENOATTR, list -> filtered) any name in the reserved user.union.*
       namespace. These pin the predicate (the policy lives in vchroot_userspace.c,
       not scattered across the syscall sites, exactly like the write-op helpers).
       The dot AFTER "union" is part of the namespace: "user.unionX" is a DIFFERENT,
       guest-legal name and must NOT be treated as a marker (off-by-one boundary). */
    {
        /* XM1. the two concrete markers the union actually writes are matched. */
        check("XM1 user.union.whiteout is a reserved marker",
              vchroot_xattr_is_marker("user.union.whiteout") == 1);
        check("XM1 user.union.opaque is a reserved marker",
              vchroot_xattr_is_marker("user.union.opaque") == 1);

        /* XM2. any name in the user.union. namespace is reserved (future markers),
                INCLUDING the bare namespace prefix with an empty leaf. */
        check("XM2 user.union.anything is reserved",
              vchroot_xattr_is_marker("user.union.future_marker") == 1);
        check("XM2 bare user.union. prefix is reserved",
              vchroot_xattr_is_marker("user.union.") == 1);

        /* XM3. guest-legal xattrs are NOT markers -- the predicate must not
                over-reach and break ordinary guest xattr use. */
        check("XM3 user.test.tag is NOT a marker (guest-legal)",
              vchroot_xattr_is_marker("user.test.tag") == 0);
        check("XM3 security.capability is NOT a marker (handled separately)",
              vchroot_xattr_is_marker("security.capability") == 0);
        check("XM3 plain user. prefix is NOT a marker",
              vchroot_xattr_is_marker("user.") == 0);

        /* XM4. boundary: a name that merely SHARES the "user.union" character run
                without the trailing dot is a distinct guest-legal name, not a
                marker. A naive strncmp(name,"user.union",10) would false-match. */
        check("XM4 user.unionX is NOT a marker (no namespace dot)",
              vchroot_xattr_is_marker("user.unionX") == 0);
        check("XM4 user.union (no dot, exact) is NOT a marker",
              vchroot_xattr_is_marker("user.union") == 0);
        /* defensive: NULL / empty must be safe and non-matching */
        check("XM4 NULL name is not a marker (no crash)",
              vchroot_xattr_is_marker(NULL) == 0);
        check("XM4 empty name is not a marker",
              vchroot_xattr_is_marker("") == 0);

        /* XS1. Exercise the linked setxattr wrapper on a dedicated lower-only
                target; the attribute must land on upper, never the template. */
        {
            const char* g = "/var/log/xattr_lower";
            snprintf(l, sizeof(l), "%s/var/log/xattr_lower", libexec);
            snprintf(p, sizeof(p), "%s/var/log/xattr_lower", prefix);
            unlink(p); /* pristine */
            check("XS1 fixture: lower-only target present", access(l, F_OK) == 0);
            check("XS1 fixture: no upper copy yet", access(p, F_OK) != 0);
            int rv = (int)sys_setxattr(g, "test.runner", "upper", 5, 0, 0);
            check("XS1 setxattr(lower-only target) succeeds", rv == 0);
            check("XS1 target materialized in upper (setxattr lands upper)",
                  access(p, F_OK) == 0);
            expect_resolves_to(g, p);
            char value[16];
            ssize_t size = getxattr(p, "user.test.runner", value, sizeof(value));
            check("XS1 upper copy contains new attribute",
                  size == 5 && memcmp(value, "upper", 5) == 0);
            check("XS1 template does not acquire new attribute",
                  getxattr(l, "user.test.runner", value, sizeof(value)) == -1 &&
                  errno == ENODATA);
            check("XS1 template target untouched on disk", access(l, F_OK) == 0);
        }
    }

    /* === .10 EXIT_PATH compare: locale-free, NULL-locale-safe (bug dar-...4.10) =====
       The translator matched the guest path's tail against EXIT_PATH with
       strncasecmp_l(..., LC_C_LOCALE) where LC_C_LOCALE == (locale_t)NULL. glibc's
       strncasecmp_l dereferences the locale once the compared bytes match, so a
       NULL locale SEGVs PRECISELY when a guest path equals EXIT_PATH
       ("/Volumes/SystemRoot") -- the escape-hatch that is supposed to fire. The fix
       replaces it with eunion_ascii_ncasecmp(), which takes NO locale and folds
       ASCII A-Z itself. These tests pin that comparator's contract; the resolver
       smoke test (EP4) drives the real vchroot_run path that contained the deref. */
    {
        printf("== .10 EXIT_PATH locale-free compare ==\n");

        /* EP1. equal prefixes compare equal (return 0). This is the case that
                crashed under the old NULL-locale strncasecmp_l. */
        check("EP1 identical EXIT_PATH compares equal",
              eunion_ascii_ncasecmp(EXIT_PATH, "/Volumes/SystemRoot",
                                    sizeof(EXIT_PATH) - 1) == 0);

        /* EP2. case-insensitive: the prefix is matched case-folded (HFS+-like), so
                a differently-cased spelling of EXIT_PATH must still match. A naive
                strncmp() fix would WRONGLY reject this -> mutation guard. */
        check("EP2 lowercase /volumes/systemroot matches (case-folded)",
              eunion_ascii_ncasecmp("/volumes/systemroot", EXIT_PATH,
                                    sizeof(EXIT_PATH) - 1) == 0);
        check("EP2 mixed-case /VOLUMES/systemROOT matches",
              eunion_ascii_ncasecmp("/VOLUMES/systemROOT", EXIT_PATH,
                                    sizeof(EXIT_PATH) - 1) == 0);

        /* EP3. genuinely different bytes must NOT match (returns non-zero). Catches
                an always-equal mutation. The differing byte is non-alphabetic so it
                can never be folded away. */
        check("EP3 different path does NOT match",
              eunion_ascii_ncasecmp("/Volumes/SystemXoot", EXIT_PATH,
                                    sizeof(EXIT_PATH) - 1) != 0);
        /* a non-letter vs letter at the same position must not fold to equal */
        check("EP3 digit vs letter does NOT fold to match",
              eunion_ascii_ncasecmp("/V0lumes/SystemRoot", EXIT_PATH,
                                    sizeof(EXIT_PATH) - 1) != 0);
        /* only the first n bytes are compared: bytes past n are ignored */
        check("EP3 compares only first n bytes",
              eunion_ascii_ncasecmp("/Volumes/SystemRoot/extra", EXIT_PATH,
                                    sizeof(EXIT_PATH) - 1) == 0);

        /* EP4. end-to-end through vchroot_run: a guest path that IS the escape
                hatch must resolve to the REAL system root ("" + tail), exercising
                the exact comparison site that held the NULL-locale deref. Under the
                old code this branch SEGV'd on glibc the moment the bytes matched;
                surviving + producing the system-root rewrite proves the deref is
                gone. We assert the resolver does not crash and that EXIT_PATH leaves
                the union (resolves outside both layers). */
        {
            set_prefix(prefix);
            char got[4096];
            /* /Volumes/SystemRoot/usr -> escape hatch -> "/usr" (real root). The
               key property: it must NOT resolve under prefix or libexec. */
            const char* r = expand("/Volumes/SystemRoot/usr", got);
            check("EP4 EXIT_PATH escape resolves without crashing",
                  r != NULL);
            check("EP4 EXIT_PATH escape leaves the union (not under prefix)",
                  strncmp(got, prefix, (size_t)prefix_path_len) != 0);
            check("EP4 EXIT_PATH escape leaves the union (not under libexec)",
                  strncmp(got, libexec, strlen(libexec)) != 0);
            /* case-folded spelling takes the same escape hatch */
            const char* r2 = expand("/volumes/systemroot/usr", got);
            check("EP4 case-folded EXIT_PATH also escapes the union",
                  r2 != NULL && strncmp(got, prefix, (size_t)prefix_path_len) != 0);
        }
    }

    printf("\n%d tests, %d failed\n", g_tests, g_fail);
    return g_fail ? 1 : 0;
}

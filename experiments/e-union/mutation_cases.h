/* Included by runner.c: real syscall wrappers, raw host-layer oracles. */
#include <darling/emulation/xnu_syscall/bsd/impl/time/utimes.h>
#include <darling/emulation/xnu_syscall/bsd/impl/xattr/setattrlistat.h>
#include <darling/emulation/xnu_syscall/bsd/impl/xattr/fsetattrlist.h>

long sys_fchmodat(int, const char*, int, int);
long sys_chmod_extended(const char*, int, int, int, void*);
long sys_truncate(const char*, long long);
long sys_mkfifo(const char*, unsigned int);
long sys_mknod(const char*, int, int);
long sys_linkat(int, const char*, int, const char*, int);
long sys_symlinkat(const char*, int, const char*);
long sys_fchownat(int, const char*, int, int, int);

#define ROUTING_PAYLOAD "ROUTING_LOWER\n"
#define ROUTING_TAG "routing-tag"

struct routing_fixture {
    char lower[4096], upper[4096], guest[4096];
    int dfd;
};

static void routing_path(char* out, const char* root, const char* leaf) {
    snprintf(out, 4096, "%s/%s", root, leaf);
}

static int routing_file(const char* path) {
    int fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0644);
    if (fd < 0) return 0;
    int ok = write(fd, ROUTING_PAYLOAD, sizeof(ROUTING_PAYLOAD) - 1) ==
             sizeof(ROUTING_PAYLOAD) - 1;
    ok = fchmod(fd, 0644) == 0 && ok;
    ok = fsetxattr(fd, "user.test.routing", ROUTING_TAG, sizeof(ROUTING_TAG) - 1, 0) == 0 && ok;
    return close(fd) == 0 && ok;
}

static int routing_setup(struct routing_fixture* f, const char* name) {
    char root[4096], path[4096];
    routing_path(root, libexec_path, "routing-state");
    if (mkdir(root, 0755) != 0 && errno != EEXIST) return 0;
    snprintf(f->guest, sizeof(f->guest), "/routing-state/%s", name);
    snprintf(f->lower, sizeof(f->lower), "%s%s", libexec_path, f->guest);
    snprintf(f->upper, sizeof(f->upper), "%s%s", prefix_path, f->guest);
    if (mkdir(f->lower, 0755) != 0) return 0;
    routing_path(path, f->lower, "file");
    if (!routing_file(path)) return 0;
    routing_path(path, f->lower, "link");
    if (symlink("file", path) != 0) return 0;
    routing_path(path, f->lower, "dangling");
    if (symlink("missing", path) != 0) return 0;
    routing_path(path, f->lower, "dest");
    if (mkdir(path, 0755) != 0) return 0;
    f->dfd = open(f->lower, O_RDONLY | O_DIRECTORY);
    return f->dfd >= 0;
}

static int routing_bytes(const char* path, const char* expected) {
    char bytes[128];
    return slurp(path, bytes, sizeof(bytes)) == (long)strlen(expected) &&
           strcmp(bytes, expected) == 0;
}

static int routing_tag(const char* path) {
    char bytes[64];
    return getxattr(path, "user.test.routing", bytes, sizeof(bytes)) ==
           sizeof(ROUTING_TAG) - 1 && !memcmp(bytes, ROUTING_TAG, sizeof(ROUTING_TAG) - 1);
}

static int routing_mode(const char* path, mode_t mode) {
    struct stat st;
    return stat(path, &st) == 0 && (st.st_mode & 07777) == mode;
}

static int routing_absent(const char* path) {
    struct stat st;
    return lstat(path, &st) < 0 && errno == ENOENT;
}

static int routing_symlink(const char* path, const char* target) {
    char bytes[128];
    ssize_t n = readlink(path, bytes, sizeof(bytes));
    return n == (ssize_t)strlen(target) && !memcmp(bytes, target, n);
}

static int routing_entry_count(const char* path) {
    DIR* d = opendir(path);
    if (!d) return -1;
    int count = 0;
    struct dirent* entry;
    while ((entry = readdir(d)))
        if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, "..")) count++;
    closedir(d);
    return count;
}

static void routing_lower_intact(struct routing_fixture* f, const char* label) {
    char path[4096];
    routing_path(path, f->lower, "file");
    check(label, routing_bytes(path, ROUTING_PAYLOAD) && routing_mode(path, 0644) && routing_tag(path));
    routing_path(path, f->lower, "link");
    int ok = routing_symlink(path, "file");
    routing_path(path, f->lower, "dangling");
    ok = routing_symlink(path, "missing") && ok;
    routing_path(path, f->lower, "dest");
    check("RT lower namespace unchanged (exact entries, symlinks, empty destination)",
          ok && routing_entry_count(f->lower) == 4 && routing_entry_count(path) == 0);
}

static void routing_metadata_checks(void) {
    const char* names[] = {"chmod-dirfd", "chmod-cwd", "utimes-cwd", "extended-cwd", "truncate-cwd"};
    for (int op = 0; op < 5; op++) {
        struct routing_fixture f;
        if (!routing_setup(&f, names[op])) { check("RT metadata fixture setup", 0); continue; }
        int saved = eunion_test_perthread_wd;
        eunion_test_perthread_wd = f.dfd;
        char path[4096];
        struct stat before, after;
        routing_path(path, f.lower, "file");
        stat(path, &before);
        struct bsd_timeval tv[2] = {{123456789, 0}, {123456790, 0}};
        long result;
        if (op == 0) result = sys_fchmodat(f.dfd, "file", 0600, 0);
        else if (op == 1) result = sys_fchmodat(BSD_AT_FDCWD, "file", 0600, 0);
        else if (op == 2) result = sys_utimes("file", tv);
        else if (op == 3) result = sys_chmod_extended("file", -1, -1, 0600, NULL);
        else result = sys_truncate("file", 3);
        eunion_test_perthread_wd = saved;
        char label[160];
        snprintf(label, sizeof(label), "RT %s native mutation succeeds", names[op]);
        check(label, result == 0);
        routing_path(path, f.upper, "file");
        int ok = stat(path, &after) == 0;
        if (op == 2) ok = ok && after.st_mtime == tv[1].tv_sec && routing_bytes(path, ROUTING_PAYLOAD);
        else if (op == 4) ok = ok && routing_bytes(path, "ROU");
        else ok = ok && routing_mode(path, 0600) && routing_bytes(path, ROUTING_PAYLOAD);
        snprintf(label, sizeof(label), "RT %s effect is upper and retains xattr", names[op]);
        check(label, ok && routing_tag(path));
        routing_path(path, f.lower, "file");
        check("RT metadata lower mtime unchanged", stat(path, &after) == 0 && before.st_mtime == after.st_mtime);
        routing_lower_intact(&f, "RT metadata lower bytes/mode/xattr unchanged");
        close(f.dfd);
    }
    struct routing_fixture f;
    if (!routing_setup(&f, "upper-mode-zero")) { check("RT mode-zero fixture setup", 0); return; }
    char root[4096], path[4096];
    routing_path(root, prefix_path, "routing-state");
    int ready = mkdir(root, 0755) == 0 || errno == EEXIST;
    ready = mkdir(f.upper, 0755) == 0 && ready;
    routing_path(path, f.upper, "file");
    ready = routing_file(path) && ready;
    ready = chmod(path, 0000) == 0 && ready;
    check("RT upper unreadable metadata fixture", ready);
    check("RT chmod upper mode000 needs no read permission", ready &&
          sys_fchmodat(f.dfd, "file", 0600, 0) == 0);
    check("RT chmod upper mode000 changes pinned inode", routing_mode(path, 0600) &&
          routing_bytes(path, ROUTING_PAYLOAD) && routing_tag(path));
    routing_lower_intact(&f, "RT upper metadata lower bytes/mode/xattr unchanged");
    close(f.dfd);
}

static int routing_socket_name(int fd, int peer, const char* expected) {
    struct sockaddr_un address;
    socklen_t length = sizeof(address);
    memset(&address, 0, sizeof(address));
    int result = peer ? getpeername(fd, (struct sockaddr*)&address, &length)
                      : getsockname(fd, (struct sockaddr*)&address, &length);
    struct vchroot_unexpand_args logical;
    memset(&logical, 0, sizeof(logical));
    if (result == 0) strcpy(logical.path, address.sun_path);
    return result == 0 && address.sun_family == AF_UNIX &&
           vchroot_unexpand(&logical) == 0 && !strcmp(logical.path, expected);
}

static void routing_creator_checks(void) {
    const char* names[] = {"fifo-cwd", "mknod-cwd", "symlink-dirfd", "bind-cwd"};
    for (int op = 0; op < 4; op++) {
        struct routing_fixture f;
        if (!routing_setup(&f, names[op])) { check("RT creator fixture setup", 0); continue; }
        int saved = eunion_test_perthread_wd;
        int cwd = open(".", O_RDONLY | O_DIRECTORY);
        eunion_test_perthread_wd = f.dfd;
        long result;
        if (op == 0) result = sys_mkfifo("dest/new", 0600);
        else if (op == 1) result = sys_mknod("dest/new", S_IFIFO | 0600, 0);
        else if (op == 2) {
            /* Deliberately unrelated cwd: symlinkat must honor its dirfd. */
            eunion_test_perthread_wd = cwd;
            result = sys_symlinkat("../file", f.dfd, "dest/new");
        } else {
            struct sockaddr_un address;
            memset(&address, 0, sizeof(address));
            address.sun_family = AF_UNIX;
            strcpy(address.sun_path, "dest/new");
            int fd = socket(AF_UNIX, SOCK_STREAM, 0);
            int anchored = fchdir(f.dfd) == 0;
            result = fd >= 0 && anchored ? sys_bind(fd, &address,
                offsetof(struct sockaddr_un, sun_path) + strlen(address.sun_path) + 1) : -1;
            struct sockaddr_un returned;
            socklen_t returned_length = sizeof(returned);
            struct vchroot_unexpand_args logical;
            memset(&returned, 0, sizeof(returned));
            memset(&logical, 0, sizeof(logical));
            int named = result == 0 && getsockname(fd, (struct sockaddr*)&returned, &returned_length) == 0;
            if (named) strcpy(logical.path, returned.sun_path);
            char expected[4096];
            routing_path(expected, f.guest, "dest/new");
            check("RT bind normal native address reverses to canonical guest name",
                  named && vchroot_unexpand(&logical) == 0 && !strcmp(logical.path, expected));
            int listening = result == 0 && listen(fd, 1) == 0;
            int client = listening ? socket(AF_UNIX, SOCK_STREAM, 0) : -1;
            int connected = client >= 0 && named &&
                connect(client, (struct sockaddr*)&returned, returned_length) == 0;
            int accepted = connected ? accept(fd, NULL, NULL) : -1;
            check("RT bind connection setup", connected && accepted >= 0);
            if (fd >= 0) sys_close(fd);
            check("RT client peer name survives listener close",
                  connected && routing_socket_name(client, 1, expected));
            check("RT accepted local name survives listener close",
                  accepted >= 0 && routing_socket_name(accepted, 0, expected));
            if (client >= 0) sys_close(client);
            check("RT accepted local name survives peer close",
                  accepted >= 0 && routing_socket_name(accepted, 0, expected));
            if (accepted >= 0) sys_close(accepted);
        }
        eunion_test_perthread_wd = saved;
        if (cwd >= 0) { check("RT restore host cwd", fchdir(cwd) == 0); close(cwd); }
        char label[160], path[4096];
        snprintf(label, sizeof(label), "RT %s creates successfully", names[op]);
        check(label, result == 0);
        routing_path(path, f.upper, "dest/new");
        struct stat st;
        int ok = lstat(path, &st) == 0;
        if (op < 2) ok = ok && S_ISFIFO(st.st_mode) && (st.st_mode & 0777) == 0600;
        else if (op == 2) ok = ok && routing_symlink(path, "../file");
        else ok = ok && S_ISSOCK(st.st_mode);
        check("RT creator node appears only in upper destination", ok);
        routing_lower_intact(&f, "RT creator lower bytes/mode/xattr unchanged");
        close(f.dfd);
    }
}

static void routing_hardlink_checks(void) {
    for (int cwd_relative = 0; cwd_relative < 2; cwd_relative++) {
        struct routing_fixture f;
        if (!routing_setup(&f, cwd_relative ? "hardlink-cwd" : "hardlink-dirfd")) {
            check("RT hardlink fixture setup", 0); continue;
        }
        int saved = eunion_test_perthread_wd;
        eunion_test_perthread_wd = f.dfd;
        int fd = cwd_relative ? BSD_AT_FDCWD : f.dfd;
        long result = sys_linkat(fd, "file", fd, "dest/new", 0);
        eunion_test_perthread_wd = saved;
        check("RT relative hardlink succeeds", result == 0);
        char source[4096], dest[4096], lower[4096];
        routing_path(source, f.upper, "file");
        routing_path(dest, f.upper, "dest/new");
        routing_path(lower, f.lower, "file");
        struct stat a, b, l;
        check("RT hardlink upper names share inode, distinct from lower",
              stat(source, &a) == 0 && stat(dest, &b) == 0 && stat(lower, &l) == 0 &&
              a.st_dev == b.st_dev && a.st_ino == b.st_ino && a.st_nlink == 2 &&
              (a.st_dev != l.st_dev || a.st_ino != l.st_ino) && l.st_nlink == 1);
        int out = open(dest, O_WRONLY | O_TRUNC);
        int wrote = out >= 0 && write(out, "LINK_UPPER\n", 11) == 11;
        if (out >= 0) close(out);
        check("RT hardlink write is visible through upper source only", wrote && routing_bytes(source, "LINK_UPPER\n"));
        routing_lower_intact(&f, "RT hardlink lower bytes/mode/xattr unchanged");
        close(f.dfd);
    }
}

static void routing_open_checks(void) {
    struct { const char* name; int flags; const char* leaf; int error; } cases[] = {
        {"open-follow", BSD_O_WRONLY | BSD_O_TRUNC, "link", 0},
        {"open-create-follow", BSD_O_WRONLY | BSD_O_CREAT | BSD_O_TRUNC, "link", 0},
        {"open-exclusive", BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, "link", EEXIST},
        {"open-nofollow", BSD_O_WRONLY | 0x100, "link", ELOOP},
        {"open-create-nofollow", BSD_O_WRONLY | BSD_O_CREAT | 0x100, "link", ELOOP},
        {"open-dangling", BSD_O_WRONLY | BSD_O_CREAT, "dangling", 0},
        {"open-dangling-exclusive", BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, "dangling", EEXIST},
        {"open-dangling-missing", BSD_O_WRONLY, "dangling", ENOENT}
    };
    for (unsigned int i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        struct routing_fixture f;
        if (!routing_setup(&f, cases[i].name)) { check("RT open fixture setup", 0); continue; }
        long result = sys_openat(f.dfd, cases[i].leaf, cases[i].flags, 0600);
        char label[180], path[4096];
        snprintf(label, sizeof(label), "RT %s returns native result/errno", cases[i].name);
        check(label, cases[i].error ? result == -cases[i].error : result >= 0);
        if (result >= 0) {
            check("RT final symlink fd accepts upper write", write(result, "FOLLOW_UPPER\n", 13) == 13);
            sys_close(result);
        }
        const char* target = !strcmp(cases[i].leaf, "dangling") ? "missing" : "file";
        routing_path(path, f.upper, target);
        if (!cases[i].error) check("RT final symlink writes its guest target in upper", routing_bytes(path, "FOLLOW_UPPER\n"));
        else check("RT failed symlink open does not materialize target", routing_absent(path));
        routing_lower_intact(&f, "RT final symlink lower bytes/mode/xattr unchanged");
        close(f.dfd);
    }
}

static void routing_chmod_symlink_checks(void) {
    for (int nofollow = 0; nofollow < 2; nofollow++) {
        struct routing_fixture f;
        if (!routing_setup(&f, nofollow ? "chmod-link-nofollow" : "chmod-link-follow")) {
            check("RT chmod symlink fixture setup", 0); continue;
        }
        long result = sys_fchmodat(f.dfd, "link", 0600, nofollow ? 0x20 : 0);
        check("RT chmod symlink follows requested policy", result == 0);
        char path[4096];
        routing_path(path, f.upper, nofollow ? "link" : "file");
        check("RT chmod selects only the requested upper object", nofollow
              ? routing_symlink(path, "file")
              : routing_mode(path, 0600) && routing_bytes(path, ROUTING_PAYLOAD));
        routing_path(path, f.upper, nofollow ? "file" : "link");
        check("RT chmod leaves unselected upper object absent", routing_absent(path));
        routing_lower_intact(&f, "RT chmod symlink lower bytes/mode/xattr unchanged");
        close(f.dfd);
    }
}

static void routing_follow_and_failure_checks(void) {
    for (int nofollow = 0; nofollow < 2; nofollow++) {
        struct routing_fixture f;
        if (!routing_setup(&f, nofollow ? "metadata-nofollow" : "metadata-follow")) {
            check("RT metadata follow fixture setup", 0); continue;
        }
        /* Ownership -1/-1 is an unprivileged real metadata syscall. It still
         * requires writable routing, even when no visible uid/gid changes. */
        check("RT fchownat follow policy accepts symlink", sys_fchownat(f.dfd, "link", -1, -1,
              nofollow ? 0x20 : 0) == 0);
        char path[4096];
        routing_path(path, f.upper, nofollow ? "link" : "file");
        check("RT metadata follow policy selects correct upper object", nofollow
              ? routing_symlink(path, "file") : routing_bytes(path, ROUTING_PAYLOAD));
        routing_path(path, f.upper, nofollow ? "file" : "link");
        check("RT metadata follow policy leaves other upper object absent", routing_absent(path));
        routing_lower_intact(&f, "RT metadata follow lower bytes/mode/xattr unchanged");
        close(f.dfd);
    }
    struct routing_fixture f;
    if (!routing_setup(&f, "failure-preservation")) { check("RT failure fixture setup", 0); return; }
    check("RT negative truncate rejects before changing visibility", sys_truncate("/routing-state/failure-preservation/file", -1) == -EINVAL);
    char path[4096];
    routing_path(path, f.upper, "file");
    check("RT rejected truncate leaves upper file absent", routing_absent(path));
    check("RT invalid chmod flags reject", sys_fchmodat(f.dfd, "file", 0600, 0x40000000) == -EINVAL);
    check("RT missing creator parent returns ENOENT", sys_mkfifo("/routing-state/failure-preservation/absent/new", 0600) == -ENOENT);
    int regular = openat(f.dfd, "file", O_RDONLY);
    check("RT chmod invalid dirfd returns EBADF", sys_fchmodat(-1, "file", 0600, 0) == -EBADF);
    check("RT chmod non-directory dirfd returns ENOTDIR", regular >= 0 && sys_fchmodat(regular, "../file", 0600, 0) == -ENOTDIR);
    if (regular >= 0) close(regular);
    routing_lower_intact(&f, "RT failed operations preserve lower bytes/mode/xattr");
    close(f.dfd);
}

static void routing_attrlist_checks(void) {
    for (int op = 0; op < 3; op++) {
        struct routing_fixture f;
        if (!routing_setup(&f, op == 0 ? "attrlist-path" : op == 1 ? "attrlist-fd" : "attrlist-invalid")) {
            check("RT attrlist fixture setup", 0); continue;
        }
        struct xnu_attrlist attributes = {.bitmapcount = 5, .commonattr = 0x20000};
        int mode = 0600;
        long result;
        if (op == 1) {
            int fd = (int)sys_openat(f.dfd, "file", 0, 0);
            result = fd >= 0 ? sys_fsetattrlist(fd, &attributes, &mode, sizeof(mode), 0) : fd;
            if (fd >= 0) sys_close(fd);
        } else {
            result = sys_setattrlistat(f.dfd, "file", &attributes, &mode,
                op == 2 ? sizeof(mode) - 1 : sizeof(mode), 0);
        }
        check("RT attrlist real syscall result", result == (op == 2 ? -EINVAL : 0));
        char path[4096];
        routing_path(path, f.upper, "file");
        check("RT attrlist metadata effect or failure absence", op == 2 ? routing_absent(path)
              : routing_mode(path, 0600) && routing_bytes(path, ROUTING_PAYLOAD) && routing_tag(path));
        routing_lower_intact(&f, "RT attrlist lower bytes/mode/xattr unchanged");
        close(f.dfd);
    }
}

static void routing_state_checks(void) {
    puts("== E-UNION real mutation routing transitions ==");
    routing_metadata_checks();
    routing_creator_checks();
    routing_hardlink_checks();
    routing_open_checks();
    routing_chmod_symlink_checks();
    routing_follow_and_failure_checks();
    routing_attrlist_checks();
}

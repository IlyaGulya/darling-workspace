/* Included by runner.c: exercise the linked production syscall wrappers. */
#ifndef EUNION_XATTR_CASES_H
#define EUNION_XATTR_CASES_H

long sys_setxattr(const char*, const char*, const void*, unsigned long, unsigned int, int);
long sys_getxattr(const char*, const char*, char*, unsigned long, unsigned int, int);
long sys_listxattr(const char*, char*, unsigned long, int);
long sys_removexattr(const char*, const char*, int);
long sys_fsetxattr(int, const char*, const void*, unsigned long, unsigned int, int);
long sys_fgetxattr(int, const char*, char*, unsigned long, unsigned int, int);
long sys_flistxattr(int, char*, unsigned long, int);
long sys_fremovexattr(int, const char*, int);

static void xa_check(const char* object, const char* transition, int ok)
{
    char label[256];
    snprintf(label, sizeof(label), "XA %s: %s", object, transition);
    check(label, ok);
}

static int xa_raw_value(const char* path, const char* name, const char* expected)
{
    char value[128];
    ssize_t size = getxattr(path, name, value, sizeof(value));
    return size == (ssize_t)strlen(expected) && !memcmp(value, expected, (size_t)size);
}

static int xa_guest_value(const char* path, int fd, const char* name, const char* expected)
{
    char value[128] = {0};
    long size = fd < 0 ? sys_getxattr(path, name, value, sizeof(value), 0, 0) :
        sys_fgetxattr(fd, name, value, sizeof(value), 0, 0);
    return size == (long)strlen(expected) && !memcmp(value, expected, (size_t)size);
}

static int xa_list(const char* path, int fd, int options, const char* wanted)
{
    char names[1024];
    long size = fd < 0 ? sys_listxattr(path, names, sizeof(names), options) :
        sys_flistxattr(fd, names, sizeof(names), options);
    long measured = fd < 0 ? sys_listxattr(path, NULL, 0, options) :
        sys_flistxattr(fd, NULL, 0, options);
    int found = wanted == NULL;
    if (size < 0 || size > (long)sizeof(names) || measured != size)
        return 0;
    for (size_t pos = 0; pos < (size_t)size;) {
        const char* end = memchr(names + pos, 0, (size_t)size - pos);
        if (!end || !strncmp(names + pos, "user.union.", 11) ||
            !strncmp(names + pos, "union.", 6))
            return 0;
        if (wanted && !strcmp(names + pos, wanted))
            found = 1;
        pos = (size_t)(end - names) + 1;
    }
    return found;
}

static int xa_seed(const char* name)
{
    char lower[4096];
    snprintf(lower, sizeof(lower), "%s/xattr-state/%s", libexec_path, name);
    int fd = open(lower, O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (fd < 0)
        return 0;
    int ok = write(fd, "XATTR_LOWER\n", 12) == 12;
    close(fd);
    return ok && setxattr(lower, "user.test.remove", "REMOVE_ME", 9, 0) == 0 &&
        setxattr(lower, "user.test.keep", "KEEP_ME", 7, 0) == 0;
}

static void xa_remove_cycle(const char* guest, const char* leaf, int use_fd)
{
    char lower[4096], upper[4096], value[128];
    snprintf(lower, sizeof(lower), "%s/xattr-state/%s", libexec_path, leaf);
    snprintf(upper, sizeof(upper), "%s/xattr-state/%s", prefix_path, leaf);
    int fd = use_fd ? open(lower, O_RDONLY) : -1;
    xa_check(leaf, "lower fixture readable", !use_fd || fd >= 0);
    if (use_fd && fd < 0)
        return;
    int alias = use_fd ? (int)sys_dup(fd) : -1;
    xa_check(leaf, "duplicate retains opened description", !use_fd || alias >= 0);
    long ret = use_fd ? sys_fremovexattr(fd, "test.remove", 0) :
        sys_removexattr(guest, "test.remove", 0);
    xa_check(leaf, "remove normalized attribute succeeds", ret == 0);
    xa_check(leaf, "only upper attribute removed", getxattr(upper, "user.test.remove", value, sizeof(value)) < 0 && errno == ENODATA);
    xa_check(leaf, "unrelated upper attribute preserved", xa_raw_value(upper, "user.test.keep", "KEEP_ME"));
    xa_check(leaf, "lower removed value preserved", xa_raw_value(lower, "user.test.remove", "REMOVE_ME"));
    xa_check(leaf, "lower unrelated value preserved", xa_raw_value(lower, "user.test.keep", "KEEP_ME"));
    if (use_fd) {
        xa_check(leaf, "same fd observes successful attribute removal",
            sys_fgetxattr(fd, "test.remove", value, sizeof(value), 0, 0) == -ENODATA);
        xa_check(leaf, "preexisting alias observes attribute removal",
            alias >= 0 && sys_fgetxattr(alias, "test.remove", value, sizeof(value), 0, 0) == -ENODATA);
        xa_check(leaf, "same fd preserves unrelated attribute", xa_guest_value(NULL, fd, "test.keep", "KEEP_ME"));
    }
    ret = use_fd ? sys_fremovexattr(fd, "test.remove", 0) : sys_removexattr(guest, "test.remove", 0);
    xa_check(leaf, "second removal reports absent attribute", ret == -ENODATA);
    ret = use_fd ? sys_fsetxattr(fd, "test.remove", "READDED", 7, 0, 0) :
        sys_setxattr(guest, "test.remove", "READDED", 7, 0, 0);
    xa_check(leaf, "absent to present re-add", ret == 0 && xa_guest_value(guest, fd, "test.remove", "READDED"));
    if (use_fd)
        xa_check(leaf, "preexisting alias observes successful re-add", alias >= 0 && xa_guest_value(NULL, alias, "test.remove", "READDED"));
    xa_check(leaf, "list includes guest names and excludes markers", xa_list(guest, fd, 0, "test.remove"));
    ret = use_fd ? sys_fremovexattr(alias >= 0 ? alias : fd, "user.test.remove", 0) : sys_removexattr(guest, "user.test.remove", 0);
    xa_check(leaf, "explicit namespace removes re-added value", ret == 0);
    if (use_fd)
        xa_check(leaf, "removal through alias updates original fd",
            sys_fgetxattr(fd, "test.remove", value, sizeof(value), 0, 0) == -ENODATA);
    xa_check(leaf, "present to absent preserves unrelated value", xa_raw_value(upper, "user.test.keep", "KEEP_ME") &&
        xa_raw_value(lower, "user.test.remove", "REMOVE_ME"));
    if (use_fd) {
        xa_check(leaf, "unlink pathname while descriptors remain open",
            sys_unlinkat(BSD_AT_FDCWD, guest, 0) == 0);
        int replacement = (int)sys_openat(BSD_AT_FDCWD, guest, BSD_O_CREAT | BSD_O_EXCL | BSD_O_RDWR, 0644);
        xa_check(leaf, "create distinct replacement inode", replacement >= 0);
        if (replacement >= 0) {
            xa_check(leaf, "seed replacement attribute",
                sys_fsetxattr(replacement, "test.remove", "REPLACEMENT", 11, 0, 0) == 0);
            xa_check(leaf, "old fd mutation remains bound to detached inode",
                sys_fsetxattr(fd, "test.remove", "DETACHED", 8, 0, 0) == 0);
            xa_check(leaf, "old fd and alias read detached value",
                xa_guest_value(NULL, fd, "test.remove", "DETACHED") &&
                alias >= 0 && xa_guest_value(NULL, alias, "test.remove", "DETACHED"));
            xa_check(leaf, "replacement fd and pathname remain independent",
                xa_guest_value(NULL, replacement, "test.remove", "REPLACEMENT") &&
                xa_guest_value(guest, -1, "test.remove", "REPLACEMENT"));
            xa_check(leaf, "detached mutation leaves lower unchanged",
                xa_raw_value(lower, "user.test.remove", "REMOVE_ME"));
            sys_close(replacement);
        }
        if (alias >= 0)
            sys_close(alias);
        sys_close(fd);
    }
}

static void xa_symlink_fd_cases(void)
{
    char path[4096], names[128];
    struct stat before = {0}, after = {0};
    snprintf(path, sizeof(path), "%s/xattr-state/fd-symlink", prefix_path);
    xa_check("symlink-fd", "create upper symlink to attributed target", symlink("absolute", path) == 0);
    xa_check("symlink-fd", "native link listing establishes empty fixture", llistxattr(path, names, sizeof(names)) == 0);
    int fd = (int)sys_openat(BSD_AT_FDCWD, "/xattr-state/fd-symlink", 0x200000 /* BSD_O_SYMLINK */, 0);
    xa_check("symlink-fd", "production open retains symlink inode", fd >= 0 && fstat(fd, &before) == 0 && S_ISLNK(before.st_mode));
    if (fd < 0)
        return;
    int alias = (int)sys_dup(fd);
    xa_check("symlink-fd", "duplicate symbolic description", alias >= 0);
    xa_check("symlink-fd", "size query inspects symlink rather than target", sys_flistxattr(fd, NULL, 0, 0) == 0);
    xa_check("symlink-fd", "data query inspects symlink rather than target", sys_flistxattr(fd, names, sizeof(names), 0) == 0);
    xa_check("symlink-fd", "unlink opened symlink", sys_unlinkat(BSD_AT_FDCWD, "/xattr-state/fd-symlink", 0) == 0);
    int replacement = (int)sys_openat(BSD_AT_FDCWD, "/xattr-state/fd-symlink", BSD_O_CREAT | BSD_O_EXCL | BSD_O_RDWR, 0644);
    xa_check("symlink-fd", "create distinct regular replacement", replacement >= 0);
    if (replacement >= 0) {
        xa_check("symlink-fd", "seed replacement attribute", sys_fsetxattr(replacement, "test.replacement", "REPLACEMENT", 11, 0, 0) == 0);
        xa_check("symlink-fd", "replacement exposes an independently nonempty list", sys_flistxattr(replacement, names, sizeof(names), 0) > 0);
        xa_check("symlink-fd", "opened descriptor retains symlink identity after replacement", fstat(fd, &after) == 0 && before.st_dev == after.st_dev && before.st_ino == after.st_ino && S_ISLNK(after.st_mode));
        xa_check("symlink-fd", "detached size query never inspects replacement", sys_flistxattr(fd, NULL, 0, 0) == 0);
        xa_check("symlink-fd", "detached data query never inspects replacement", sys_flistxattr(fd, names, sizeof(names), 0) == 0);
        xa_check("symlink-fd", "preexisting alias retains detached symlink listing", alias >= 0 && sys_flistxattr(alias, NULL, 0, 0) == 0 && sys_flistxattr(alias, names, sizeof(names), 0) == 0);
        sys_close(replacement);
    }
    if (alias >= 0)
        sys_close(alias);
    sys_close(fd);
}

static void xattr_state_checks(void)
{
    static const char* const files[] = { "absolute", "fd", "cwd-remove", "thread-remove", "cwd-set", "thread-set", "follow-set", "follow-remove", "nofollow", "whiteout" };
    char lower[4096], upper[4096], path[4096], linkpath[4096], value[128];
    int saved_wd = eunion_test_perthread_wd;
    int cwd = open(".", O_RDONLY | O_DIRECTORY);
    printf("\n== Production xattr state transitions ==\n");
    snprintf(lower, sizeof(lower), "%s/xattr-state", libexec_path);
    snprintf(upper, sizeof(upper), "%s/xattr-state", prefix_path);
    int ready = mkdir(lower, 0755) == 0 && mkdir(upper, 0755) == 0;
    for (size_t i = 0; i < sizeof(files) / sizeof(files[0]); i++)
        ready = xa_seed(files[i]) && ready;
    xa_check("fixture", "creates real lower files with two attributes", ready && cwd >= 0);
    if (!ready || cwd < 0) {
        if (cwd >= 0) close(cwd);
        return;
    }
    xa_remove_cycle("/xattr-state/absolute", "absolute", 0);
    xa_remove_cycle("/xattr-state/fd", "fd", 1);
    xa_symlink_fd_cases();

    int dfd = open(lower, O_RDONLY | O_DIRECTORY);
    xa_check("relative", "open lower directory descriptor", dfd >= 0);
    if (dfd >= 0) {
        xa_check("cwd", "enter lower cwd", fchdir(dfd) == 0);
        eunion_test_perthread_wd = AT_FDCWD;
        xa_remove_cycle("cwd-remove", "cwd-remove", 0);
        xa_check("cwd-set", "relative set succeeds", sys_setxattr("cwd-set", "com.apple.test", "NEW", 3, 0, 0) == 0);
        snprintf(path, sizeof(path), "%s/cwd-set", lower);
        xa_check("cwd-set", "relative set never mutates lower", getxattr(path, "user.com.apple.test", value, sizeof(value)) < 0 && errno == ENODATA);
        snprintf(path, sizeof(path), "%s/cwd-set", upper);
        xa_check("cwd-set", "relative set materializes upper value", xa_raw_value(path, "user.com.apple.test", "NEW"));
        fchdir(cwd);
        eunion_test_perthread_wd = dfd;
        xa_remove_cycle("thread-remove", "thread-remove", 0);
        xa_check("thread-set", "dirfd-authoritative set succeeds", sys_setxattr("thread-set", "com.apple.test", "NEW", 3, 0, 0) == 0);
        snprintf(path, sizeof(path), "%s/thread-set", lower);
        xa_check("thread-set", "dirfd set never mutates lower", getxattr(path, "user.com.apple.test", value, sizeof(value)) < 0 && errno == ENODATA);
        snprintf(path, sizeof(path), "%s/thread-set", upper);
        xa_check("thread-set", "dirfd set materializes upper value", xa_raw_value(path, "user.com.apple.test", "NEW"));
        eunion_test_perthread_wd = saved_wd;
        close(dfd);
    }

    snprintf(linkpath, sizeof(linkpath), "%s/set-link", lower);
    xa_check("follow", "seed final symlinks", symlink("follow-set", linkpath) == 0);
    xa_check("follow", "set follows final symlink", sys_setxattr("/xattr-state/set-link", "test.remove", "CHANGED", 7, 0, 0) == 0);
    snprintf(path, sizeof(path), "%s/follow-set", lower);
    xa_check("follow", "set keeps lower target unchanged", xa_raw_value(path, "user.test.remove", "REMOVE_ME"));
    snprintf(path, sizeof(path), "%s/follow-set", upper);
    xa_check("follow", "set copies target not merely symlink", xa_raw_value(path, "user.test.remove", "CHANGED"));
    snprintf(linkpath, sizeof(linkpath), "%s/remove-link", lower);
    xa_check("follow-remove", "seed final symlink", symlink("follow-remove", linkpath) == 0);
    xa_remove_cycle("/xattr-state/remove-link", "follow-remove", 0);

    snprintf(linkpath, sizeof(linkpath), "%s/nofollow-link", lower);
    xa_check("nofollow", "seed final symlink", symlink("nofollow", linkpath) == 0);
    xa_check("nofollow", "get does not read target attribute", sys_getxattr("/xattr-state/nofollow-link", "test.remove", value, sizeof(value), 0, 1) == -ENODATA);
    xa_check("nofollow", "list does not expose target attributes", sys_listxattr("/xattr-state/nofollow-link", value, sizeof(value), 1) == 0);
    xa_check("nofollow", "remove reports absent link attribute", sys_removexattr("/xattr-state/nofollow-link", "test.remove", 1) == -ENODATA);
    xa_check("nofollow", "set reports native symlink prohibition", sys_setxattr("/xattr-state/nofollow-link", "test.remove", "BAD", 3, 0, 1) == -EPERM);
    snprintf(path, sizeof(path), "%s/nofollow", lower);
    xa_check("nofollow", "all operations leave lower target unchanged", xa_raw_value(path, "user.test.remove", "REMOVE_ME"));
    snprintf(path, sizeof(path), "%s/nofollow", upper);
    xa_check("nofollow", "does not copy target", access(path, F_OK) < 0 && errno == ENOENT);

    snprintf(path, sizeof(path), "%s/opaque", upper);
    xa_check("marker", "create real opaque directory", mkdir(path, 0755) == 0 && setxattr(path, "user.union.opaque", "y", 1, 0) == 0);
    int marker_fd = open(path, O_RDONLY | O_DIRECTORY);
    static const char* const markers[] = { "user.union.opaque", "union.opaque", "user.union.whiteout", "union.whiteout" };
    for (size_t i = 0; i < sizeof(markers) / sizeof(markers[0]); i++) {
        const char* marker = markers[i];
        xa_check(marker, "path marker read hidden", sys_getxattr("/xattr-state/opaque", marker, value, sizeof(value), 0, 0) == -ENODATA);
        xa_check(marker, "path marker set forbidden", sys_setxattr("/xattr-state/opaque", marker, "BAD", 3, 0, 0) == -EPERM);
        xa_check(marker, "path marker removal forbidden", sys_removexattr("/xattr-state/opaque", marker, 0) == -EPERM);
        xa_check(marker, "fd marker read hidden", sys_fgetxattr(marker_fd, marker, value, sizeof(value), 0, 0) == -ENODATA);
        xa_check(marker, "fd marker set forbidden", sys_fsetxattr(marker_fd, marker, "BAD", 3, 0, 0) == -EPERM);
        xa_check(marker, "fd marker removal forbidden", sys_fremovexattr(marker_fd, marker, 0) == -EPERM);
        xa_check(marker, "path and fd lists hide all internal markers", xa_list("/xattr-state/opaque", -1, 0, NULL) && xa_list(NULL, marker_fd, 0, NULL));
        xa_check(marker, "opacity marker survives rejected mutations", xa_raw_value(path, "user.union.opaque", "y"));
        /* Restore real fixture markers between independently failing RED cases. */
        removexattr(path, "user.union.whiteout");
        setxattr(path, "user.union.opaque", "y", 1, 0);
    }
    sys_close(marker_fd);

    xa_check("whiteout", "create real production whiteout", sys_unlinkat(BSD_AT_FDCWD, "/xattr-state/whiteout", 0) == 0);
    snprintf(path, sizeof(path), "%s/whiteout", upper);
    int whiteout_fd = open(path, O_RDONLY);
    xa_check("whiteout", "open physical whiteout descriptor", whiteout_fd >= 0);
    if (whiteout_fd >= 0) {
        xa_check("whiteout", "fd read cannot observe physical whiteout marker", sys_fgetxattr(whiteout_fd, "union.whiteout", value, sizeof(value), 0, 0) == -ENODATA);
        xa_check("whiteout", "fd list cannot expose physical whiteout marker", xa_list(NULL, whiteout_fd, 0, NULL));
        xa_check("whiteout", "fd marker set forbidden", sys_fsetxattr(whiteout_fd, "union.whiteout", "BAD", 3, 0, 0) == -EPERM);
        xa_check("whiteout", "fd marker removal forbidden", sys_fremovexattr(whiteout_fd, "union.whiteout", 0) == -EPERM);
        xa_check("whiteout", "physical marker survives rejected mutations", xa_raw_value(path, "user.union.whiteout", "y"));
        sys_close(whiteout_fd);
    }
    xa_check("whiteout", "path marker read hidden", sys_getxattr("/xattr-state/whiteout", "union.whiteout", value, sizeof(value), 0, 0) == -ENODATA);
    xa_check("whiteout", "path marker removal forbidden", sys_removexattr("/xattr-state/whiteout", "union.whiteout", 0) == -EPERM);
    xa_check("whiteout", "list sees absent namespace entry", sys_listxattr("/xattr-state/whiteout", value, sizeof(value), 0) == -ENOENT);
    snprintf(path, sizeof(path), "%s/whiteout", lower);
    xa_check("whiteout", "lower attributes survive namespace deletion", xa_raw_value(path, "user.test.remove", "REMOVE_ME") && xa_raw_value(path, "user.test.keep", "KEEP_ME"));
    xa_check("domain", "missing path reports ENOENT", sys_removexattr("/xattr-state/missing", "test.remove", 0) == -ENOENT);
    xa_check("domain", "regular parent reports ENOTDIR", sys_setxattr("/xattr-state/absolute/child", "test.keep", "x", 1, 0, 0) == -ENOTDIR);
    xa_check("domain", "invalid fd reports EBADF", sys_fremovexattr(-1, "test.remove", 0) == -EBADF);
    xa_check("options", "CREATE sets an absent attribute", sys_setxattr("/xattr-state/absolute", "test.remove", "OPTIONS", 7, 0, 2) == 0);
    xa_check("options", "CREATE rejects an existing attribute", sys_setxattr("/xattr-state/absolute", "test.remove", "BAD", 3, 0, 2) == -EEXIST);
    xa_check("options", "REPLACE rejects an absent attribute", sys_setxattr("/xattr-state/absolute", "test.absent", "BAD", 3, 0, 4) == -ENODATA);
    snprintf(path, sizeof(path), "%s/absolute", upper);
    int options_fd = open(path, O_RDONLY);
    xa_check("options", "open upper for fd option checks", options_fd >= 0);
    if (options_fd >= 0) {
        xa_check("options", "fd REPLACE changes existing attribute", sys_fsetxattr(options_fd, "test.remove", "REPLACED", 8, 0, 4) == 0 && xa_guest_value(NULL, options_fd, "test.remove", "REPLACED"));
        xa_check("options", "fd CREATE rejects an existing attribute", sys_fsetxattr(options_fd, "test.remove", "BAD", 3, 0, 2) == -EEXIST);
        xa_check("options", "conflicting flags rejected before mutation", sys_fsetxattr(options_fd, "test.remove", "BAD", 3, 0, 6) == -EINVAL);
        xa_check("options", "invalid remove flags preserve value", sys_fremovexattr(options_fd, "test.remove", 2) == -EINVAL && xa_guest_value(NULL, options_fd, "test.remove", "REPLACED"));
        long list_size = sys_flistxattr(options_fd, NULL, 0, 0);
        xa_check("options", "fd list accepts exact guest-sized buffer", list_size > 0 && list_size <= (long)sizeof(value) && sys_flistxattr(options_fd, value, (unsigned long)list_size, 0) == list_size);
        xa_check("options", "remove option-test attribute", sys_fremovexattr(options_fd, "test.remove", 0) == 0);
        sys_close(options_fd);
    }
    eunion_test_perthread_wd = saved_wd;
    fchdir(cwd);
    close(cwd);
}

#endif

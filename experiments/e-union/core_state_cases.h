/* Black-box transition oracles against the production translator and wrappers. */
long sys_symlinkat(const char* target, int fd, const char* link);
long sys_fchown(int fd, int uid, int gid);
static int core_file(const char* root, const char* relative, const char* text) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/core-state/%s", root, relative);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    int ok = fd >= 0 && write(fd, text, strlen(text)) == (ssize_t)strlen(text);
    if (fd >= 0) close(fd);
    return ok;
}
static int core_dir(const char* root, const char* relative) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/core-state/%s", root, relative);
    return mkdir(path, 0755) == 0;
}
static int core_text(const char* root, const char* relative, const char* text) {
    char path[4096], value[128];
    snprintf(path, sizeof(path), "%s/core-state/%s", root, relative);
    return slurp(path, value, sizeof(value)) == (long)strlen(text) && !strcmp(value, text);
}
static int core_visible(const char* relative) {
    char path[4096], host[4096];
    snprintf(path, sizeof(path), "/core-state/%s", relative);
    expand(path, host);
    return host[0] != '<' && access(host, F_OK) == 0;
}

static int core_publish_ready[2], core_publish_release[2];
static void core_pause_publication(const char* destination) {
    (void)destination;
    char byte = 'P';
    if (write(core_publish_ready[1], &byte, 1) != 1 ||
        read(core_publish_release[0], &byte, 1) != 1)
        _exit(90);
}
static void core_publication_checks(void) {
    check("CS publication lower fixture", core_file(libexec_path, "publish", "LOWER\n"));
    int pipes = pipe(core_publish_ready) == 0 && pipe(core_publish_release) == 0;
    check("CS publication scheduler pipes", pipes);
    if (!pipes) return;
    pid_t child = fork();
    if (child == 0) {
        alarm(15);
        close(core_publish_ready[0]); close(core_publish_release[1]);
        eunion_test_before_publish = core_pause_publication;
        int result = vchroot_copyup("/core-state/publish");
        _exit(result == 0 ? 0 : 91);
    }
    close(core_publish_ready[1]); close(core_publish_release[0]);
    if (child < 0) {
        check("CS publication fork", 0);
        close(core_publish_ready[0]); close(core_publish_release[1]);
        return;
    }
    char byte;
    int staged = read(core_publish_ready[0], &byte, 1) == 1;
    /* Child has finished staging lower bytes. The second real guest writer
     * publishes, successfully writes and closes before the first may publish. */
    int writer = staged ? (int)sys_openat(BSD_AT_FDCWD, "/core-state/publish",
        BSD_O_WRONLY | BSD_O_TRUNC, 0) : -1;
    int wrote = writer >= 0 && write(writer, "COMMITTED\n", 10) == 10;
    int closed = writer >= 0 && sys_close(writer) == 0;
    check("CS competing writer commits successfully before delayed publication", staged && wrote && closed);
    int released = staged && write(core_publish_release[1], "R", 1) == 1;
    close(core_publish_ready[0]); close(core_publish_release[1]);
    int status = 0;
    pid_t waited;
    do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    check("CS delayed publisher finishes", released && waited == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    check("CS successful write survives concurrent copyup publication", core_text(prefix_path, "publish", "COMMITTED\n"));
    check("CS publication leaves lower bytes unchanged", core_text(libexec_path, "publish", "LOWER\n"));
}

static int core_work_entries(void) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/.union-work", prefix_path);
    DIR* directory = opendir(path);
    if (!directory) return errno == ENOENT ? 0 : -1;
    int count = 0;
    struct dirent* entry;
    while ((entry = readdir(directory)))
        if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, "..")) count++;
    closedir(directory);
    return count;
}
/* Additional RED/GREEN: a failed native effect cannot erase another writer
 * that observed an already-published reversible copy. Pipe order, not time,
 * distinguishes committed-before-abort from actual kernel lock contention. */
static int core_abort_ready[2] = {-1, -1}, core_abort_release[2] = {-1, -1};
static int core_abort_event[2] = {-1, -1};
static void core_abort_close(int* fd) {
    if (*fd >= 0) close(*fd);
    *fd = -1;
}
static void core_abort_pipes_close(void) {
    int* pipes[] = {core_abort_ready, core_abort_release, core_abort_event};
    for (int i = 0; i < 3; i++) for (int j = 0; j < 2; j++) core_abort_close(&pipes[i][j]);
}
static int core_abort_child_ok(pid_t child) {
    int status = 0; pid_t result;
    do { result = waitpid(child, &status, 0); } while (result < 0 && errno == EINTR);
    return result == child && WIFEXITED(status) && WEXITSTATUS(status) == 0;
}
static void core_abort_pause(void) {
    char byte;
    eunion_test_after_prepare = NULL;
    if (write(core_abort_ready[1], "R", 1) != 1 ||
        read(core_abort_release[0], &byte, 1) != 1) _exit(91);
}
static void core_abort_contended(void) {
    eunion_test_namespace_contended = NULL;
    if (write(core_abort_event[1], "W", 1) != 1) _exit(92);
}
static void core_publication_abort_checks(void) {
    const char* guest = "/core-state/published-abort";
    check("CA fixture is a real unprivileged native ownership denial", geteuid() != 0);
    if (geteuid() == 0) return;
    int fixture = core_file(libexec_path, "published-abort", "LOWER\n");
    check("CA lower fixture", fixture);
    if (!fixture) return;
    int before = core_work_entries();
    int pipes = pipe(core_abort_ready) == 0 && pipe(core_abort_release) == 0 && pipe(core_abort_event) == 0;
    check("CA scheduler pipes", pipes);
    if (!pipes) { core_abort_pipes_close(); return; }
    pid_t owner = fork();
    if (owner == 0) {
        alarm(30); /* Watchdog only; no timeout determines the oracle. */
        core_abort_close(&core_abort_ready[0]); core_abort_close(&core_abort_release[1]);
        core_abort_close(&core_abort_event[0]); core_abort_close(&core_abort_event[1]);
        int fd = (int)sys_openat(BSD_AT_FDCWD, guest, 0, 0);
        eunion_test_after_prepare = core_abort_pause;
        long result = fd >= 0 ? sys_fchown(fd, 0, -1) : fd;
        char data[6];
        int original = fd >= 0 && lseek(fd, 0, SEEK_SET) == 0 &&
            read(fd, data, sizeof(data)) == sizeof(data) && !memcmp(data, "LOWER\n", sizeof(data));
        int closed = fd >= 0 && sys_close(fd) == 0;
        _exit(result == -EPERM && original && closed ? 0 : 93);
    }
    core_abort_close(&core_abort_ready[1]); core_abort_close(&core_abort_release[0]);
    if (owner < 0) { check("CA owner fork", 0); core_abort_pipes_close(); return; }
    char event = 0;
    int prepared = read(core_abort_ready[0], &event, 1) == 1 && event == 'R';
    core_abort_close(&core_abort_ready[0]);
    check("CA real mutator reaches prepared published state", prepared);
    if (!prepared) {
        core_abort_pipes_close();
        check("CA owner preparation completes", core_abort_child_ok(owner));
        return;
    }
    pid_t writer = fork();
    if (writer == 0) {
        alarm(30);
        core_abort_close(&core_abort_release[1]); core_abort_close(&core_abort_event[0]);
        eunion_test_namespace_contended = core_abort_contended;
        int fd = (int)sys_openat(BSD_AT_FDCWD, guest, BSD_O_WRONLY | BSD_O_TRUNC, 0);
        /* A blocked opener's old plan may legitimately become stale on abort.
         * Retry only that pre-write conflict; never retry a lost committed write. */
        if (fd == -ESTALE) fd = (int)sys_openat(BSD_AT_FDCWD, guest, BSD_O_WRONLY | BSD_O_TRUNC, 0);
        int wrote = fd >= 0 && write(fd, "COMMITTED\n", 10) == 10;
        int closed = fd >= 0 && sys_close(fd) == 0;
        char result = wrote && closed ? 'C' : 'F';
        int sent = write(core_abort_event[1], &result, 1) == 1;
        _exit(wrote && closed && sent ? 0 : 94);
    }
    core_abort_close(&core_abort_event[1]);
    if (writer < 0) {
        check("CA writer fork", 0);
        write(core_abort_release[1], "R", 1);
        core_abort_pipes_close();
        check("CA owner release", core_abort_child_ok(owner));
        return;
    }
    event = 0;
    int signaled = read(core_abort_event[0], &event, 1) == 1;
    check("CA writer commits or encounters real native admission contention", signaled && (event == 'C' || event == 'W'));
    int released = write(core_abort_release[1], "R", 1) == 1;
    core_abort_close(&core_abort_release[1]);
    int owner_ok = core_abort_child_ok(owner);
    check("CA original mutator returns real EPERM with original fd intact", released && owner_ok);
    char committed = event;
    if (signaled && event == 'W') signaled = read(core_abort_event[0], &committed, 1) == 1;
    int writer_ok = core_abort_child_ok(writer);
    check("CA actual concurrent writer completes", signaled && committed == 'C' && writer_ok);
    core_abort_pipes_close();
    check("CA successful write survives native failure rollback", core_text(prefix_path, "published-abort", "COMMITTED\n"));
    check("CA rollback never changes lower bytes", core_text(libexec_path, "published-abort", "LOWER\n"));
    check("CA transaction resources are cleaned", before >= 0 && core_work_entries() == before);
}
static void core_readonly_publication_checks(void) {
    const char* guest = "/core-state/readonly-provisional";
    char lower[4096], upper[4096];
    snprintf(lower, sizeof(lower), "%s/core-state/readonly-provisional", libexec_path);
    snprintf(upper, sizeof(upper), "%s/core-state/readonly-provisional", prefix_path);
    int fixture = geteuid() != 0 && core_file(libexec_path, "readonly-provisional", "LOWER\n") && chmod(lower, 0644) == 0;
    check("CR real readonly capability fixture", fixture);
    if (!fixture) return;
    int metadata_release[2] = {-1, -1};
    int pipes = pipe(core_abort_ready) == 0 && pipe(core_abort_release) == 0 &&
        pipe(core_abort_event) == 0 && pipe(metadata_release) == 0;
    check("CR scheduler pipes", pipes);
    if (!pipes) {
        core_abort_pipes_close(); core_abort_close(&metadata_release[0]); core_abort_close(&metadata_release[1]);
        return;
    }
    pid_t owner = fork();
    if (owner == 0) {
        alarm(30);
        core_abort_close(&core_abort_ready[0]); core_abort_close(&core_abort_release[1]);
        core_abort_close(&core_abort_event[0]); core_abort_close(&core_abort_event[1]);
        core_abort_close(&metadata_release[0]); core_abort_close(&metadata_release[1]);
        int fd = (int)sys_openat(BSD_AT_FDCWD, guest, 0, 0);
        eunion_test_after_prepare = core_abort_pause;
        long result = fd >= 0 ? sys_fchown(fd, 0, -1) : fd;
        int closed = fd >= 0 && sys_close(fd) == 0;
        _exit(result == -EPERM && closed ? 0 : 95);
    }
    core_abort_close(&core_abort_ready[1]); core_abort_close(&core_abort_release[0]);
    if (owner < 0) {
        check("CR owner fork", 0); core_abort_pipes_close();
        core_abort_close(&metadata_release[0]); core_abort_close(&metadata_release[1]); return;
    }
    char event = 0;
    int prepared = read(core_abort_ready[0], &event, 1) == 1 && event == 'R';
    core_abort_close(&core_abort_ready[0]);
    check("CR reversible publisher reaches native-validation boundary", prepared);
    if (!prepared) {
        core_abort_pipes_close(); core_abort_close(&metadata_release[0]); core_abort_close(&metadata_release[1]);
        check("CR owner preparation completes", core_abort_child_ok(owner)); return;
    }
    pid_t reader = fork();
    if (reader == 0) {
        alarm(30);
        core_abort_close(&core_abort_release[1]); core_abort_close(&core_abort_event[0]);
        core_abort_close(&metadata_release[1]);
        eunion_test_namespace_contended = core_abort_contended;
        int fd = (int)sys_openat(BSD_AT_FDCWD, guest, 0, 0);
        if (fd == -ESTALE) fd = (int)sys_openat(BSD_AT_FDCWD, guest, 0, 0);
        int alias = fd >= 0 ? (int)sys_dup(fd) : -1;
        int opened = fd >= 0 && alias >= 0;
        char result = opened ? 'O' : 'F', permit;
        if (write(core_abort_event[1], &result, 1) != 1 || !opened) _exit(96);
        if (read(metadata_release[0], &permit, 1) != 1) _exit(97);
        int positioned = lseek(fd, 2, SEEK_SET) == 2;
        long changed = sys_fchmod(alias, 0600);
        struct stat original = {0}, duplicate = {0};
        int stats = fstat(fd, &original) == 0 && fstat(alias, &duplicate) == 0;
        unsigned long long identity[2] = {original.st_dev, original.st_ino};
        int sent = write(core_abort_event[1], identity, sizeof(identity)) == sizeof(identity);
        char bytes[6];
        int intact = positioned && lseek(fd, 0, SEEK_CUR) == 2 &&
            lseek(fd, 0, SEEK_SET) == 0 && read(fd, bytes, sizeof(bytes)) == sizeof(bytes) &&
            !memcmp(bytes, "LOWER\n", sizeof(bytes));
        int coherent = stats && original.st_dev == duplicate.st_dev && original.st_ino == duplicate.st_ino &&
            (original.st_mode & 0777) == 0600 && (duplicate.st_mode & 0777) == 0600;
        int closed = sys_close(alias) == 0;
        closed &= sys_close(fd) == 0;
        _exit(changed == 0 && intact && coherent && sent && closed ? 0 : 98);
    }
    core_abort_close(&core_abort_event[1]); core_abort_close(&metadata_release[0]);
    if (reader < 0) {
        check("CR reader fork", 0); write(core_abort_release[1], "R", 1);
        core_abort_pipes_close(); core_abort_close(&metadata_release[1]);
        check("CR owner release", core_abort_child_ok(owner)); return;
    }
    event = 0;
    int observed = read(core_abort_event[0], &event, 1) == 1;
    check("CR readonly open completes or proves native admission contention", observed && (event == 'O' || event == 'W'));
    int released = write(core_abort_release[1], "R", 1) == 1;
    core_abort_close(&core_abort_release[1]);
    int owner_ok = core_abort_child_ok(owner);
    check("CR original native operation really fails EPERM", released && owner_ok);
    if (observed && event == 'W') observed = read(core_abort_event[0], &event, 1) == 1;
    int opened = observed && event == 'O';
    check("CR readonly open returns a real fd and dup", opened);
    check("CR readonly lower open performs no copyup", opened && access(upper, F_OK) != 0);
    int permitted = opened && write(metadata_release[1], "M", 1) == 1;
    core_abort_close(&metadata_release[1]);
    unsigned long long identity[2] = {0, 0};
    int captured = permitted && read(core_abort_event[0], identity, sizeof(identity)) == sizeof(identity);
    int reader_ok = core_abort_child_ok(reader);
    check("CR fd metadata succeeds with original bytes cursor and aliases", captured && reader_ok);
    char resolved[4096]; struct stat named;
    expand(guest, resolved);
    int named_ok = resolved[0] != '<' && stat(resolved, &named) == 0;
    check("CR fd metadata remains visible through unchanged guest name", named_ok && (named.st_mode & 0777) == 0600);
    check("CR returned readonly capability identifies committed named inode",
        named_ok && captured && (unsigned long long)named.st_dev == identity[0] && (unsigned long long)named.st_ino == identity[1]);
    check("CR original lower bytes remain unchanged", core_text(libexec_path, "readonly-provisional", "LOWER\n"));
    struct stat original_lower;
    check("CR original lower permissions remain unchanged", stat(lower, &original_lower) == 0 && (original_lower.st_mode & 0777) == 0644);
    core_abort_pipes_close();
}


static void core_metadata_publication_checks(void) {
    unsigned char expected[2048], actual[2048];
    for (size_t i = 0; i < sizeof(expected); i++) expected[i] = (unsigned char)(i % 251);
    char lower[4096], upper[4096];
    snprintf(lower, sizeof(lower), "%s/core-state/large-xattr", libexec_path);
    snprintf(upper, sizeof(upper), "%s/core-state/large-xattr", prefix_path);
    check("CS large xattr file fixture", core_file(libexec_path, "large-xattr", "LOWER\n"));
    int source = open(lower, O_RDONLY);
    int seeded = source >= 0 && fsetxattr(source, "user.test.large", expected, sizeof(expected), 0) == 0;
    struct stat before, after;
    int captured = seeded && fstat(source, &before) == 0;
    check("CS lower fd carries exact 2048-byte user xattr", captured &&
        fgetxattr(source, "user.test.large", actual, sizeof(actual)) == sizeof(expected) &&
        !memcmp(expected, actual, sizeof(expected)));
    int staged_before = core_work_entries();
    int writer = (int)sys_openat(BSD_AT_FDCWD, "/core-state/large-xattr", BSD_O_WRONLY, 0);
    int wrote = writer >= 0 && write(writer, "UPPER\n", 6) == 6;
    int closed = writer >= 0 && sys_close(writer) == 0;
    check("CS copyup with 2048-byte xattr permits successful write", wrote && closed);
    check("CS published copy keeps exact 2048-byte xattr",
        getxattr(upper, "user.test.large", actual, sizeof(actual)) == sizeof(expected) &&
        !memcmp(expected, actual, sizeof(expected)));
    check("CS large xattr publication keeps successful content write", core_text(prefix_path, "large-xattr", "UPPER\n"));
    char bytes[8] = {0};
    int same = captured && fstat(source, &after) == 0 &&
        before.st_dev == after.st_dev && before.st_ino == after.st_ino &&
        before.st_mode == after.st_mode && before.st_size == after.st_size &&
        before.st_mtim.tv_sec == after.st_mtim.tv_sec && before.st_mtim.tv_nsec == after.st_mtim.tv_nsec;
    check("CS retained lower inode/data/metadata remain unchanged",
        same && pread(source, bytes, sizeof(bytes), 0) == 6 && !memcmp(bytes, "LOWER\n", 6) &&
        fgetxattr(source, "user.test.large", actual, sizeof(actual)) == sizeof(expected) &&
        !memcmp(expected, actual, sizeof(expected)));
    check("CS large xattr publication cleans staging", staged_before >= 0 && core_work_entries() == staged_before);
    if (source >= 0) close(source);

    check("CS metadata-stage failure fixture", core_file(libexec_path, "metadata-abort", "LOWER\n"));
    snprintf(upper, sizeof(upper), "%s/core-state/metadata-abort", prefix_path);
    staged_before = core_work_entries();
    eunion_test_fail_xattr = 1;
    writer = (int)sys_openat(BSD_AT_FDCWD, "/core-state/metadata-abort", BSD_O_WRONLY, 0);
    eunion_test_fail_xattr = 0;
    if (writer >= 0) sys_close(writer);
    check("CS failed metadata stage reports EIO, never successful publication", writer == -EIO);
    check("CS failed metadata stage leaves no partial upper object", access(upper, F_OK) != 0);
    check("CS failed metadata stage retains lower bytes and guest visibility",
        core_text(libexec_path, "metadata-abort", "LOWER\n") && core_visible("metadata-abort"));
    check("CS failed metadata stage cleans staged file", staged_before >= 0 && core_work_entries() == staged_before);
}

static void core_removal_checks(void) {
    struct removal_case { const char* name; int upper; int lower; int child; int opaque; int error; };
    const struct removal_case cases[] = {
        {"lower-file", 0, 1, 0, 0, ENOTDIR},
        {"upper-file", 1, 0, 0, 0, ENOTDIR},
        {"both-file", 1, 1, 0, 0, ENOTDIR},
        {"lower-full", 0, 2, 1, 0, ENOTEMPTY},
        {"upper-full", 2, 0, 1, 0, ENOTEMPTY},
        {"merged-full", 2, 2, 1, 0, ENOTEMPTY},
        {"lower-empty", 0, 2, 0, 0, 0},
        {"upper-empty", 2, 0, 0, 0, 0},
        {"merged-empty", 2, 2, 0, 0, 0},
        {"opaque-empty", 2, 2, 1, 1, 0},
        {"whiteout-empty", 2, 2, 1, 2, 0},
    };
    for (size_t i = 0; i < sizeof(cases)/sizeof(cases[0]); i++) {
        const struct removal_case* c = &cases[i];
        char guest[4096], relative[256], label[256];
        snprintf(guest, sizeof(guest), "/core-state/%s", c->name);
        int fixture = 1;
        const char* roots[] = {prefix_path, libexec_path};
        int kinds[] = {c->upper, c->lower};
        for (int layer = 0; layer < 2; layer++) {
            if (kinds[layer] == 1) fixture &= core_file(roots[layer], c->name, "KEEP\n");
            if (kinds[layer] == 2) fixture &= core_dir(roots[layer], c->name);
            if (c->child && kinds[layer] == 2 && (!c->opaque || layer == 1)) {
                snprintf(relative, sizeof(relative), "%s/child", c->name);
                fixture &= core_file(roots[layer], relative, "CHILD\n");
            }
        }
        if (c->opaque == 1) {
            char upper[4096]; snprintf(upper, sizeof(upper), "%s%s", prefix_path, guest);
            fixture &= lsetxattr(upper, "user.union.opaque", "y", 1, 0) == 0;
        }
        if (c->opaque == 2) {
            char child[4096]; snprintf(child, sizeof(child), "%s/child", guest);
            fixture &= sys_unlinkat(BSD_AT_FDCWD, child, 0) == 0;
        }
        snprintf(label, sizeof(label), "CS rmdir %s fixture", c->name); check(label, fixture);
        long result = sys_rmdir(guest);
        snprintf(label, sizeof(label), "CS rmdir %s returns required errno/success", c->name);
        check(label, result == -c->error);
        snprintf(label, sizeof(label), "CS rmdir %s preserves visibility on error / removes on success", c->name);
        check(label, core_visible(c->name) == !!c->error);
        if (c->lower == 1) {
            snprintf(label, sizeof(label), "CS rmdir %s leaves lower bytes intact", c->name);
            check(label, core_text(libexec_path, c->name, "KEEP\n"));
        }
        if (c->child && c->lower == 2) {
            snprintf(relative, sizeof(relative), "%s/child", c->name);
            snprintf(label, sizeof(label), "CS rmdir %s leaves lower child intact", c->name);
            check(label, core_text(libexec_path, relative, "CHILD\n"));
        }
        if (!c->error) {
            snprintf(label, sizeof(label), "CS repeated rmdir %s is ENOENT without resurrection", c->name);
            check(label, sys_rmdir(guest) == -ENOENT && !core_visible(c->name));
        }
    }
    check("CS lower directory unlink fixture", core_dir(libexec_path, "unlink-directory"));
    check("CS unlink directory rejects EISDIR without hiding it",
        sys_unlinkat(BSD_AT_FDCWD, "/core-state/unlink-directory", 0) == -EISDIR && core_visible("unlink-directory"));
    check("CS marker abort fixtures", core_file(libexec_path, "abort", "LOWER\n") && core_file(prefix_path, "abort", "UPPER\n"));
    eunion_test_fail_whiteout = 1;
    long result = sys_unlinkat(BSD_AT_FDCWD, "/core-state/abort", 0);
    eunion_test_fail_whiteout = 0;
    check("CS failed removal reports marker error", result == -EIO);
    check("CS failed removal retains upper object, never resurrects lower", core_visible("abort") && core_text(prefix_path, "abort", "UPPER\n"));
    check("CS failed removal preserves lower", core_text(libexec_path, "abort", "LOWER\n"));
    check("CS rename rejection fixtures", core_file(libexec_path, "rename-source", "SOURCE\n") && core_dir(libexec_path, "rename-directory") && core_file(libexec_path, "rename-directory/child", "CHILD\n"));
    result = sys_renameat(BSD_AT_FDCWD, "/core-state/rename-source", BSD_AT_FDCWD, "/core-state/rename-directory");
    check("CS file over lower directory rename rejects EISDIR", result == -EISDIR);
    check("CS failed rename leaves source and destination children visible", core_visible("rename-source") && core_visible("rename-directory/child"));
    check("CS failed rename leaves lower source and child unchanged", core_text(libexec_path, "rename-source", "SOURCE\n") && core_text(libexec_path, "rename-directory/child", "CHILD\n"));
    check("CS failed create fixture", core_file(libexec_path, "create-abort", "LOWER\n"));
    check("CS failed create begins whiteouted",
        sys_unlinkat(BSD_AT_FDCWD, "/core-state/create-abort", 0) == 0 && !core_visible("create-abort"));
    result = sys_symlinkat("", BSD_AT_FDCWD, "/core-state/create-abort");
    check("CS failed create reports native empty-target ENOENT", result == -ENOENT);
    check("CS failed create preserves whiteout without lower resurrection", !core_visible("create-abort"));
    check("CS failed create preserves lower bytes", core_text(libexec_path, "create-abort", "LOWER\n"));
}

static int core_next(int fd, char* name, long long* cookie) {
    char page[32];
    int result = vchroot_getdents_merge(fd, page, sizeof(page));
    if (result <= 0) return result;
    struct linux_dirent64* entry = (void*)page;
    if (entry->d_reclen != result) return -EIO;
    strcpy(name, entry->d_name);
    if (cookie) *cookie = entry->d_off;
    return 1;
}
static int core_stream_open(void) {
    return (int)sys_openat(BSD_AT_FDCWD, "/core-state/stream", 0, 0);
}
static void core_descriptor_checks(void) {
    check("CS directory stream fixture", core_dir(libexec_path, "stream"));
    for (int i = 0; i < 12; i++) {
        char name[64]; snprintf(name, sizeof(name), "stream/n%02d", i);
        check("CS stream child fixture", core_file(libexec_path, name, "N\n"));
    }
    int regular = (int)sys_openat(BSD_AT_FDCWD, "/core-state/publish", 0, 0);
    char page[128];
    check("CS regular fd getdents is ENOTDIR, not empty", regular >= 0 && vchroot_getdents_merge(regular, page, sizeof(page)) == -ENOTDIR);
    if (regular >= 0) sys_close(regular);
    for (int mode = 0; mode < 6; mode++) {
        int kind = mode % 3, before_materialization = mode >= 3;
        int fd = core_stream_open();
        char first[256], second[256], third[256], label[160];
        long long cookie = -1;
        int first_ok = !before_materialization && fd >= 0 && core_next(fd, first, &cookie) == 1;
        int alias = -1;
        if (kind == 0) alias = fd >= 0 ? (int)sys_dup(fd) : -1;
        if (kind == 1) {
            /* Give dup2 a destination with an unrelated, already exhausted cache. */
            alias = (int)sys_openat(BSD_AT_FDCWD, "/core-state/upper-full", 0, 0);
            if (alias >= 0) { int ignored; char names[4096]; nameset_merged(alias, names, sizeof(names), &ignored); }
            if (fd < 0 || alias < 0 || sys_dup2(fd, alias) < 0) { if (alias >= 0) sys_close(alias); alias = -1; }
        }
        if (kind == 2) alias = fd >= 0 ? (int)sys_fcntl(fd, F_DUPFD, 0) : -1;
        if (before_materialization)
            first_ok = fd >= 0 && core_next(fd, first, &cookie) == 1;
        int next = alias >= 0 ? core_next(alias, second, NULL) : -1;
        int original = fd >= 0 ? core_next(fd, third, NULL) : -1;
        snprintf(label, sizeof(label), "CS %s aliases %s materialization share advancing cursor and discard replaced cache",
            kind == 0 ? "dup" : kind == 1 ? "dup2" : "F_DUPFD", before_materialization ? "before" : "after");
        check(label, first_ok && next == 1 && original == 1 && strcmp(first, second) && strcmp(second, third) && strcmp(first, third));
        int seek = alias >= 0 && sys_lseek(alias, cookie, SEEK_SET) == cookie;
        int replay = fd >= 0 ? core_next(fd, third, NULL) : -1;
        check("CS alias seek cookie updates shared cursor", seek && replay == 1 && next == 1 && !strcmp(second, third));
        int rewind = alias >= 0 && sys_lseek(alias, 0, SEEK_SET) == 0;
        replay = fd >= 0 ? core_next(fd, third, NULL) : -1;
        check("CS alias rewind updates shared cursor", rewind && first_ok && replay == 1 && !strcmp(first, third));
        if (alias >= 0) sys_close(alias);
        int count = 0; char names[4096];
        check("CS closing alias preserves surviving stream", fd >= 0 && nameset_merged(fd, names, sizeof(names), &count) == 0 && count == 11);
        check("CS exhausted stream remains EOF", fd >= 0 && vchroot_getdents_merge(fd, page, sizeof(page)) == 0);
        if (fd >= 0) sys_close(fd);
    }
    int fd = core_stream_open(); char name[256];
    int partial = fd >= 0 && core_next(fd, name, NULL) == 1;
    int oldfd = fd;
    if (fd >= 0) sys_close(fd);
    fd = core_stream_open();
    int count = 0; char names[4096];
    check("CS close/reuse starts new snapshot without stale cursor", partial && fd == oldfd && nameset_merged(fd, names, sizeof(names), &count) == 0 && count == 12);
    if (fd >= 0) sys_close(fd);
    check("CS opened inode fixture", core_dir(libexec_path, "identity") && core_file(libexec_path, "identity/original", "ORIGINAL\n"));
    fd = (int)sys_openat(BSD_AT_FDCWD, "/core-state/identity", 0, 0);
    long moved = sys_renameat(BSD_AT_FDCWD, "/core-state/identity", BSD_AT_FDCWD, "/core-state/moved");
    int replaced = sys_mkdir("/core-state/identity", 0755) == 0 && core_file(prefix_path, "identity/replacement", "NEW\n");
    count = 0;
    int listed = fd >= 0 && nameset_merged(fd, names, sizeof(names), &count) == 0;
    check("CS opened directory keeps identity across rename and replacement", moved == 0 && replaced && listed && name_in(names, count, "original") && !name_in(names, count, "replacement"));
    if (fd >= 0) sys_close(fd);
}
static void core_rename_noop_checks(void) {
    check("CN nonempty same-name directory fixture", core_dir(libexec_path, "noop-dir") &&
        core_file(libexec_path, "noop-dir/child", "NOOP\n"));
    char directory[4096], upper[4096];
    snprintf(directory, sizeof(directory), "%s/core-state/noop-dir", libexec_path);
    snprintf(upper, sizeof(upper), "%s/core-state/noop-dir", prefix_path);
    struct stat before = {0}, after = {0};
    int existed = stat(directory, &before) == 0;
    long result = sys_renameat(BSD_AT_FDCWD, "/core-state/noop-dir", BSD_AT_FDCWD, "/core-state/noop-dir");
    check("CN same-name nonempty directory is a no-op without copyup", existed && result == 0 &&
        stat(directory, &after) == 0 && before.st_dev == after.st_dev && before.st_ino == after.st_ino &&
        access(upper, F_OK) != 0 && core_visible("noop-dir/child"));
    for (int layer = 0; layer != 2; ++layer) {
        const char* root = layer ? prefix_path : libexec_path;
        char first[64], second[64], source[4096], target[4096], guest[128], destination[128], got[4096];
        snprintf(first, sizeof(first), "noop-%s-a", layer ? "upper" : "lower");
        snprintf(second, sizeof(second), "noop-%s-b", layer ? "upper" : "lower");
        snprintf(source, sizeof(source), "%s/core-state/%s", root, first);
        snprintf(target, sizeof(target), "%s/core-state/%s", root, second);
        snprintf(guest, sizeof(guest), "/core-state/%s", first);
        snprintf(destination, sizeof(destination), "/core-state/%s", second);
        check("CN distinct hardlink fixture", core_file(root, first, "HARDLINK\n") && link(source, target) == 0);
        before = (struct stat){0};
        int original = stat(source, &before) == 0;
        int fd = (int)sys_openat(BSD_AT_FDCWD, guest, 0, 0);
        result = sys_renameat(BSD_AT_FDCWD, guest, BSD_AT_FDCWD, destination);
        struct stat source_after = {0}, target_after = {0};
        check("CN same-inode rename preserves both hardlink names and inode", original && result == 0 &&
            stat(source, &source_after) == 0 && stat(target, &target_after) == 0 &&
            source_after.st_dev == before.st_dev && source_after.st_ino == before.st_ino &&
            target_after.st_dev == before.st_dev && target_after.st_ino == before.st_ino &&
            source_after.st_nlink == before.st_nlink && target_after.st_nlink == before.st_nlink);
        struct vchroot_fdpath_args path = {.fd = fd, .path = got, .maxlen = sizeof(got)};
        check("CN no-op does not emit a descriptor namespace rename", fd >= 0 &&
            vchroot_fdpath(&path) == 0 && !strcmp(got, guest));
        if (!layer) {
            char selected[4096];
            expand(guest, selected);
            check("CN lower hardlink no-op does not publish a new upper inode", !strcmp(selected, source));
            expand(destination, selected);
            check("CN lower hardlink destination remains the original lower inode", !strcmp(selected, target));
        }
        if (fd >= 0) sys_close(fd);
    }
}
/* These transitions also run unchanged when the fixture root is backed by the
 * production lane filesystem: native inode identity must agree across lookup,
 * retained O_PATH getattr and publication, not just on an ordinary host fs. */
static void core_native_namespace_checks(void) {
    const char* temporary = "/core-state/native-compiler.o-temporary";
    const char* output = "/core-state/native-compiler.o";
    int fd = (int)sys_openat(BSD_AT_FDCWD, temporary,
        BSD_O_RDWR | BSD_O_CREAT | BSD_O_EXCL, 0600);
    struct stat original = {0}, published = {0};
    int created = fd >= 0 && write(fd, "OBJECT\n", 7) == 7 && fstat(fd, &original) == 0;
    int closed = fd >= 0 && sys_close(fd) == 0;
    check("NR compiler temporary file is a real committed upper inode", created && closed);
    long renamed = sys_renameat(BSD_AT_FDCWD, temporary, BSD_AT_FDCWD, output);
    char path[4096];
    snprintf(path, sizeof(path), "%s/core-state/native-compiler.o", prefix_path);
    check("NR compiler temporary rename preserves the native committed inode",
        created && closed && renamed == 0 && stat(path, &published) == 0 &&
        original.st_dev == published.st_dev && original.st_ino == published.st_ino &&
        core_text(prefix_path, "native-compiler.o", "OBJECT\n") &&
        !core_visible("native-compiler.o-temporary"));

    int fixture = core_dir(libexec_path, "retained-parent");
    int parent = fixture ? (int)sys_openat(BSD_AT_FDCWD, "/core-state/retained-parent", 0, 0) : -1;
    check("NR relative removal retains its lower parent before promotion", fixture && parent >= 0);
    int made = sys_mkdir("/core-state/retained-parent/empty", 0755) == 0;
    long removed = parent >= 0 ? sys_unlinkat(parent, "empty", BSD_AT_REMOVEDIR) : -EBADF;
    check("NR relative unlinkat removes valid upper directory through the retained lower parent",
        parent >= 0 && made && removed == 0 && !core_visible("retained-parent/empty"));
    if (parent >= 0) sys_close(parent);
    snprintf(path, sizeof(path), "%s/core-state/retained-parent/empty", libexec_path);
    check("NR relative removal never creates or removes a lower child",
        access(path, F_OK) == -1 && errno == ENOENT);

    int root = (int)sys_openat(BSD_AT_FDCWD, "/", 0, 0);
    check("NR private-namespace test retains the real guest root", root >= 0);
    const char* aliases[] = {".union-work/native-probe", "./.union-work/native-probe",
        ".//.union-work/native-probe"};
    for (unsigned int i = 0; i < sizeof(aliases) / sizeof(aliases[0]); ++i) {
        int probe = root >= 0 ? (int)sys_openat(root, aliases[i],
            BSD_O_RDWR | BSD_O_CREAT | BSD_O_EXCL, 0600) : -EBADF;
        check("NR canonical root-relative aliases cannot create in private namespace", probe == -ENOENT);
        if (probe >= 0) sys_close(probe);
    }
    if (root >= 0) sys_close(root);
    snprintf(path, sizeof(path), "%s/.union-work/native-probe", prefix_path);
    check("NR rejected private aliases have no native creation effect",
        access(path, F_OK) == -1 && errno == ENOENT);
}

static void core_state_checks(void) {
    char upper[4096], lower[4096];
    snprintf(upper, sizeof(upper), "%s/core-state", prefix_path);
    snprintf(lower, sizeof(lower), "%s/core-state", libexec_path);
    check("CS root fixtures", mkdir(upper, 0755) == 0 && mkdir(lower, 0755) == 0);
    core_native_namespace_checks();
    core_publication_checks();
    core_metadata_publication_checks();
    core_publication_abort_checks();
    core_readonly_publication_checks();
    core_removal_checks();
    core_rename_noop_checks();
    core_descriptor_checks();
}

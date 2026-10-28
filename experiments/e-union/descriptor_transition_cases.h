/* Consumer oracles for native replacement versus managed descriptor authority.
 * Included after core_state_cases.h; no private registry layout is inspected. */
#include <pthread.h>

static void descriptor_check(const char* kind, const char* transition, int ok) {
    char label[192];
    snprintf(label, sizeof(label), "DT %s %s", kind, transition);
    check(label, ok);
}

static int descriptor_same_inode(const struct stat* a, const struct stat* b) {
    return a->st_dev == b->st_dev && a->st_ino == b->st_ino;
}

static int descriptor_byte(int fd, char* byte, int writing) {
    ssize_t result;
    do {
        result = writing ? write(fd, byte, 1) : read(fd, byte, 1);
    } while (result < 0 && errno == EINTR);
    return result == 1;
}

struct descriptor_schedule {
    int ready[2], promotion_release[2], duplicate_event[2], native_release[2];
    int original, source, target, mode;
    int promotion_paused, contention_seen, native_seen, event_seen;
    long promoted, duplicated;
    char destination[4096];
};
static struct descriptor_schedule descriptor_race;
static _Thread_local int descriptor_duplicate_thread;

static void descriptor_pause_promotion(const char* destination) {
    struct descriptor_schedule* s = &descriptor_race;
    if (strcmp(destination, s->destination)) return;
    __atomic_store_n(&s->promotion_paused, 1, __ATOMIC_RELEASE);
    char byte = 'P';
    if (!descriptor_byte(s->ready[1], &byte, 1) ||
        !descriptor_byte(s->promotion_release[0], &byte, 0))
        _exit(96);
}

static void descriptor_duplicate_contended(void) {
    struct descriptor_schedule* s = &descriptor_race;
    if (!descriptor_duplicate_thread ||
        __atomic_exchange_n(&s->contention_seen, 1, __ATOMIC_ACQ_REL)) return;
    __atomic_store_n(&s->event_seen, 1, __ATOMIC_RELEASE);
    char byte = 'L';
    if (!descriptor_byte(s->duplicate_event[1], &byte, 1)) _exit(96);
    /* Never pause a lock holder/waiter here: the coordinator releases promotion. */
}

static void descriptor_pause_native_duplicate(int source, int target) {
    struct descriptor_schedule* s = &descriptor_race;
    if (!descriptor_duplicate_thread || source != s->source || target != s->target ||
        __atomic_exchange_n(&s->native_seen, 1, __ATOMIC_ACQ_REL)) return;
    __atomic_store_n(&s->event_seen, 1, __ATOMIC_RELEASE);
    char byte = 'N';
    if (!descriptor_byte(s->duplicate_event[1], &byte, 1) ||
        !descriptor_byte(s->native_release[0], &byte, 0))
        _exit(96);
}

static void* descriptor_promote_thread(void* unused) {
    (void)unused;
    struct descriptor_schedule* s = &descriptor_race;
    s->promoted = sys_fchmod(s->original, s->mode);
    if (!__atomic_load_n(&s->promotion_paused, __ATOMIC_ACQUIRE)) {
        char byte = 'F';
        if (!descriptor_byte(s->ready[1], &byte, 1)) _exit(96);
    }
    return NULL;
}

static void* descriptor_dup2_thread(void* unused) {
    (void)unused;
    struct descriptor_schedule* s = &descriptor_race;
    descriptor_duplicate_thread = 1;
    s->duplicated = sys_dup2(s->source, s->target);
    descriptor_duplicate_thread = 0;
    if (!__atomic_load_n(&s->event_seen, __ATOMIC_ACQUIRE)) {
        char byte = 'F';
        if (!descriptor_byte(s->duplicate_event[1], &byte, 1)) _exit(96);
    }
    return NULL;
}

static void descriptor_schedule_close(void) {
    int* pipes[] = {descriptor_race.ready, descriptor_race.promotion_release,
        descriptor_race.duplicate_event, descriptor_race.native_release};
    for (unsigned int i = 0; i < sizeof(pipes) / sizeof(pipes[0]); i++)
        for (int j = 0; j < 2; j++)
            if (pipes[i][j] >= 0) {
                close(pipes[i][j]);
                pipes[i][j] = -1;
            }
}

static int descriptor_schedule_replace(const char* kind, const char* relative,
        int original, int source, int target, int mode) {
    struct descriptor_schedule* s = &descriptor_race;
    memset(s, 0, sizeof(*s));
    s->ready[0] = s->ready[1] = -1;
    s->promotion_release[0] = s->promotion_release[1] = -1;
    s->duplicate_event[0] = s->duplicate_event[1] = -1;
    s->native_release[0] = s->native_release[1] = -1;
    s->original = original; s->source = source; s->target = target; s->mode = mode;
    s->promoted = s->duplicated = -EIO;
    snprintf(s->destination, sizeof(s->destination), "%s/core-state/%s", prefix_path, relative);
    int pipes = pipe(s->ready) == 0 && pipe(s->promotion_release) == 0 &&
        pipe(s->duplicate_event) == 0 && pipe(s->native_release) == 0;
    descriptor_check(kind, "deterministic scheduler pipes", pipes);
    if (!pipes) { descriptor_schedule_close(); return 0; }

    eunion_test_before_publish = descriptor_pause_promotion;
    eunion_test_fd_contended = descriptor_duplicate_contended;
    eunion_test_after_native_dup2 = descriptor_pause_native_duplicate;
    pthread_t promotion, duplicate;
    alarm(30); /* Watchdog only: pipe events, not elapsed time, choose the order. */
    int promotion_started = pthread_create(&promotion, NULL, descriptor_promote_thread, NULL) == 0;
    descriptor_check(kind, "metadata promotion worker starts", promotion_started);
    int joined = 0, duplicate_started = 0, staged = 0, scheduled = 0;
    char event = 0;
    if (promotion_started) {
        staged = descriptor_byte(s->ready[0], &event, 0) && event == 'P';
        if (staged) {
            duplicate_started = pthread_create(&duplicate, NULL, descriptor_dup2_thread, NULL) == 0;
            if (duplicate_started)
                scheduled = descriptor_byte(s->duplicate_event[0], &event, 0) &&
                    (event == 'N' || event == 'L');
            char release = 'R';
            if (!descriptor_byte(s->promotion_release[1], &release, 1)) _exit(96);
        }
        joined = pthread_join(promotion, NULL) == 0;
        /* Unsafe path: N paused after native replacement. Fixed path: L proved
         * real pre-native lock contention. In BOTH cases promotion must finish
         * before any post-native duplicate publication is allowed to resume. */
        if (duplicate_started) {
            char release = 'R';
            if (!descriptor_byte(s->native_release[1], &release, 1)) _exit(96);
            joined = pthread_join(duplicate, NULL) == 0 && joined;
        }
    }
    alarm(0);
    eunion_test_before_publish = NULL;
    eunion_test_fd_contended = NULL;
    eunion_test_after_native_dup2 = NULL;
    descriptor_schedule_close();
    descriptor_check(kind, "promotion holds authority before duplicate", staged);
    descriptor_check(kind, "real native replacement or real lock contention is observed", scheduled);
    descriptor_check(kind, "promotion and successful dup2 both finish", joined &&
        duplicate_started && s->promoted == 0 && s->duplicated == target);
    return staged && scheduled && joined && duplicate_started &&
        s->promoted == 0 && s->duplicated == target;
}

static void descriptor_file_replacement_checks(void) {
    const char* kind = "file replacement";
    int fixture = core_file(libexec_path, "descriptor-replace-a", "abcdefghijk\n") &&
        core_file(libexec_path, "descriptor-replace-c", "0123456789C\n");
    descriptor_check(kind, "distinct lower fixtures", fixture);
    if (!fixture) return;
    int a = (int)sys_openat(BSD_AT_FDCWD, "/core-state/descriptor-replace-a", 0, 0);
    int b = a >= 0 ? (int)sys_dup(a) : -1;
    int c = (int)sys_openat(BSD_AT_FDCWD, "/core-state/descriptor-replace-c", 0, 0);
    struct stat a_before, c_before;
    int opened = a >= 0 && b >= 0 && c >= 0 &&
        fstat(a, &a_before) == 0 && fstat(c, &c_before) == 0 &&
        !descriptor_same_inode(&a_before, &c_before) &&
        sys_lseek(a, 2, SEEK_SET) == 2 && sys_lseek(b, 0, SEEK_CUR) == 2 &&
        sys_lseek(c, 1, SEEK_SET) == 1 && sys_fcntl(b, F_SETFD, FD_CLOEXEC) == 0 &&
        sys_fcntl(c, F_SETFD, FD_CLOEXEC) == 0;
    descriptor_check(kind, "A/B alias one OFD and C is distinct", opened);
    if (!opened) goto close_files;
    if (!descriptor_schedule_replace(kind, "descriptor-replace-a", a, c, b, 0600)) goto close_files;

    char bytes[2];
    struct stat a_after, b_after, c_after;
    int identities = fstat(a, &a_after) == 0 && fstat(b, &b_after) == 0 && fstat(c, &c_after) == 0;
    descriptor_check(kind, "successful dup2 exposes C identity, never promoted A", identities &&
        descriptor_same_inode(&b_after, &c_after) && descriptor_same_inode(&c_before, &c_after) &&
        !descriptor_same_inode(&a_after, &b_after));
    descriptor_check(kind, "successful dup2 reads C bytes at C position", read(b, bytes, 2) == 2 && !memcmp(bytes, "12", 2));
    descriptor_check(kind, "B/C share the advanced position while A stays independent",
        sys_lseek(b, 0, SEEK_CUR) == 3 && sys_lseek(c, 0, SEEK_CUR) == 3 && sys_lseek(a, 0, SEEK_CUR) == 2);
    descriptor_check(kind, "seeking A cannot move B/C",
        sys_lseek(a, 4, SEEK_SET) == 4 && sys_lseek(b, 0, SEEK_CUR) == 3 && sys_lseek(c, 0, SEEK_CUR) == 3);
    descriptor_check(kind, "seeking B moves C but never A",
        sys_lseek(b, 6, SEEK_SET) == 6 && sys_lseek(c, 0, SEEK_CUR) == 6 && sys_lseek(a, 0, SEEK_CUR) == 4);
    descriptor_check(kind, "dup2 clears only destination CLOEXEC",
        sys_fcntl(b, F_GETFD, 0) == 0 && sys_fcntl(c, F_GETFD, 0) == FD_CLOEXEC);

    long metadata = sys_fchmod(b, 0640);
    identities = fstat(a, &a_after) == 0 && fstat(b, &b_after) == 0 && fstat(c, &c_after) == 0;
    descriptor_check(kind, "later metadata through B promotes C aliases, not A", metadata == 0 && identities &&
        descriptor_same_inode(&b_after, &c_after) && !descriptor_same_inode(&a_after, &b_after) &&
        (a_after.st_mode & 0777) == 0600 && (b_after.st_mode & 0777) == 0640 && (c_after.st_mode & 0777) == 0640);
    descriptor_check(kind, "later metadata retains both independent positions",
        sys_lseek(a, 0, SEEK_CUR) == 4 && sys_lseek(b, 0, SEEK_CUR) == 6 && sys_lseek(c, 0, SEEK_CUR) == 6);
    char path[4096]; struct stat lower_a, lower_c;
    snprintf(path, sizeof(path), "%s/core-state/descriptor-replace-a", libexec_path);
    int protected = stat(path, &lower_a) == 0;
    snprintf(path, sizeof(path), "%s/core-state/descriptor-replace-c", libexec_path);
    protected = stat(path, &lower_c) == 0 && protected;
    descriptor_check(kind, "both lower objects retain bytes and metadata", protected &&
        (lower_a.st_mode & 0777) == 0644 && (lower_c.st_mode & 0777) == 0644 &&
        core_text(libexec_path, "descriptor-replace-a", "abcdefghijk\n") &&
        core_text(libexec_path, "descriptor-replace-c", "0123456789C\n"));
    descriptor_check(kind, "metadata publication retains the correct upper bytes",
        core_text(prefix_path, "descriptor-replace-a", "abcdefghijk\n") &&
        core_text(prefix_path, "descriptor-replace-c", "0123456789C\n"));
close_files:
    if (b >= 0) sys_close(b);
    if (a >= 0) sys_close(a);
    if (c >= 0) sys_close(c);
}

static void descriptor_directory_replacement_checks(void) {
    const char* kind = "directory replacement";
    int fixture = core_dir(libexec_path, "descriptor-dir-a") &&
        core_file(libexec_path, "descriptor-dir-a/a-only", "A\n") &&
        core_dir(libexec_path, "descriptor-dir-c") &&
        core_file(libexec_path, "descriptor-dir-c/c-one", "C1\n") &&
        core_file(libexec_path, "descriptor-dir-c/c-two", "C2\n");
    descriptor_check(kind, "distinct lower directory fixtures", fixture);
    if (!fixture) return;
    int a = (int)sys_openat(BSD_AT_FDCWD, "/core-state/descriptor-dir-a", 0, 0);
    int b = a >= 0 ? (int)sys_dup(a) : -1;
    int c = (int)sys_openat(BSD_AT_FDCWD, "/core-state/descriptor-dir-c", 0, 0);
    int opened = a >= 0 && b >= 0 && c >= 0;
    descriptor_check(kind, "real directory descriptors open", opened);
    if (!opened) goto close_directories;
    char old_names[512]; int old_count = 0;
    int cached = nameset_merged(b, old_names, sizeof(old_names), &old_count) == 0 &&
        old_count == 1 && name_in(old_names, old_count, "a-only");
    descriptor_check(kind, "destination begins with an exhausted unrelated enumeration", cached);
    if (!cached) goto close_directories;
    if (!descriptor_schedule_replace(kind, "descriptor-dir-a", a, c, b, 0700)) goto close_directories;
    struct stat sa, sb, sc;
    descriptor_check(kind, "native replacement and cursor authority identify C",
        fstat(a, &sa) == 0 && fstat(b, &sb) == 0 && fstat(c, &sc) == 0 &&
        descriptor_same_inode(&sb, &sc) && !descriptor_same_inode(&sa, &sb));
    char names[1024], replay[1024], empty[256]; int count = 0, replay_count = 0;
    int listed = nameset_merged(b, names, sizeof(names), &count) == 0;
    descriptor_check(kind, "B enumerates C children, never A or stale destination cache", listed &&
        count == 2 && name_in(names, count, "c-one") && name_in(names, count, "c-two") &&
        !name_in(names, count, "a-only"));
    descriptor_check(kind, "B/C share exhaustion", vchroot_getdents_merge(c, empty, sizeof(empty)) == 0);
    int rewind = sys_lseek(b, 0, SEEK_SET) == 0;
    int replayed = nameset_merged(c, replay, sizeof(replay), &replay_count) == 0;
    descriptor_check(kind, "rewinding B restores the same C enumeration through C", rewind && replayed &&
        replay_count == 2 && name_in(replay, replay_count, "c-one") && name_in(replay, replay_count, "c-two") &&
        !name_in(replay, replay_count, "a-only"));
    descriptor_check(kind, "replacement leaves lower children untouched",
        core_text(libexec_path, "descriptor-dir-a/a-only", "A\n") &&
        core_text(libexec_path, "descriptor-dir-c/c-one", "C1\n") &&
        core_text(libexec_path, "descriptor-dir-c/c-two", "C2\n"));
close_directories:
    if (b >= 0) sys_close(b);
    if (a >= 0) sys_close(a);
    if (c >= 0) sys_close(c);
}

static int descriptor_retention_source, descriptor_retention_target;
static int descriptor_retention_native_replaced;
static void descriptor_observe_retention_native(int source, int target) {
    if (source == descriptor_retention_source && target == descriptor_retention_target)
        descriptor_retention_native_replaced = 1;
}

static void descriptor_preflight_failure_checks(void) {
    const char* kind = "preflight retention failure";
    int fixture = core_file(libexec_path, "descriptor-retain-destination", "DESTINATION\n") &&
        core_dir(libexec_path, "descriptor-retain-source") &&
        core_file(libexec_path, "descriptor-retain-source/source-only", "SOURCE\n");
    descriptor_check(kind, "lower destination and lazy directory source fixtures", fixture);
    if (!fixture) return;
    int destination = (int)sys_openat(BSD_AT_FDCWD, "/core-state/descriptor-retain-destination", 0, 0);
    int destination_alias = destination >= 0 ? (int)sys_fcntl(destination, F_DUPFD, 0) : -1;
    char path[4096];
    snprintf(path, sizeof(path), "%s/core-state/descriptor-retain-source", libexec_path);
    /* Deliberately inherited/raw source: dup2 itself must reserve its missing
     * directory pins before replacing any caller-owned destination. */
    int source = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    int source_alias = source >= 0 ? dup(source) : -1;
    struct stat destination_before, source_before;
    char names[1024]; int count = 0;
    int setup = destination >= 0 && destination_alias >= 0 && source >= 0 && source_alias >= 0 &&
        fstat(destination, &destination_before) == 0 && fstat(source, &source_before) == 0 &&
        sys_lseek(destination, 4, SEEK_SET) == 4 && sys_lseek(destination_alias, 0, SEEK_CUR) == 4 &&
        sys_fcntl(destination, F_SETFD, FD_CLOEXEC) == 0 &&
        sys_fcntl(source_alias, F_SETFD, FD_CLOEXEC) == 0 &&
        nameset_raw(source, names, sizeof(names), &count) == 0 && name_in(names, count, "source-only");
    off_t source_position = source >= 0 ? lseek(source, 0, SEEK_CUR) : -1;
    setup = setup && source_position >= 0 && lseek(source_alias, 0, SEEK_CUR) == source_position;
    descriptor_check(kind, "source aliases and existing destination are usable before fault", setup);
    if (!setup) goto close_preflight;

    struct stat invalid_after;
    descriptor_check(kind, "invalid-source dup2 preserves the existing destination",
        sys_dup2(-1, destination) == -EBADF && fstat(destination, &invalid_after) == 0 &&
        descriptor_same_inode(&destination_before, &invalid_after) &&
        sys_lseek(destination, 0, SEEK_CUR) == 4 && sys_fcntl(destination, F_GETFD, 0) == FD_CLOEXEC);
    descriptor_retention_source = source;
    descriptor_retention_target = destination;
    descriptor_retention_native_replaced = 0;
    eunion_test_after_native_dup2 = descriptor_observe_retention_native;
    eunion_test_fail_retain_fd = 1;
    long result = sys_dup2(source, destination);
    eunion_test_after_native_dup2 = NULL;
    int consumed = eunion_test_fail_retain_fd == 0;
    eunion_test_fail_retain_fd = 0;
    descriptor_check(kind, "one-shot private pin failure is exercised and returned", consumed && result == -EMFILE);
    descriptor_check(kind, "preflight fails before any successful native replacement",
        !descriptor_retention_native_replaced);
    struct stat after, alias_after;
    descriptor_check(kind, "failed dup2 preserves destination identity and its existing alias",
        fstat(destination, &after) == 0 && fstat(destination_alias, &alias_after) == 0 &&
        descriptor_same_inode(&destination_before, &after) && descriptor_same_inode(&after, &alias_after));
    descriptor_check(kind, "failed dup2 preserves destination position and CLOEXEC",
        sys_lseek(destination, 0, SEEK_CUR) == 4 && sys_lseek(destination_alias, 0, SEEK_CUR) == 4 &&
        sys_fcntl(destination, F_GETFD, 0) == FD_CLOEXEC && sys_fcntl(destination_alias, F_GETFD, 0) == 0);
    char bytes[16] = {0};
    descriptor_check(kind, "failed dup2 preserves destination data at its unchanged position",
        read(destination, bytes, 2) == 2 && !memcmp(bytes, "IN", 2) &&
        sys_lseek(destination_alias, 0, SEEK_CUR) == 6);
    descriptor_check(kind, "failed dup2 leaves all destination bytes available through its alias",
        pread(destination_alias, bytes, sizeof(bytes), 0) == 12 && !memcmp(bytes, "DESTINATION\n", 12));
    descriptor_check(kind, "failed dup2 preserves source and source-alias identity",
        fstat(source, &after) == 0 && fstat(source_alias, &alias_after) == 0 &&
        descriptor_same_inode(&source_before, &after) && descriptor_same_inode(&after, &alias_after));
    descriptor_check(kind, "failed dup2 preserves source OFD position and descriptor flags",
        lseek(source, 0, SEEK_CUR) == source_position && lseek(source_alias, 0, SEEK_CUR) == source_position &&
        sys_fcntl(source, F_GETFD, 0) == FD_CLOEXEC && sys_fcntl(source_alias, F_GETFD, 0) == FD_CLOEXEC);
    int rewound = lseek(source_alias, 0, SEEK_SET) == 0;
    count = 0;
    descriptor_check(kind, "source and its alias remain an enumeratable shared native OFD", rewound &&
        lseek(source, 0, SEEK_CUR) == 0 && nameset_raw(source, names, sizeof(names), &count) == 0 &&
        name_in(names, count, "source-only") && lseek(source_alias, 0, SEEK_CUR) == lseek(source, 0, SEEK_CUR));
    descriptor_check(kind, "failure never mutates lower fixture bytes",
        core_text(libexec_path, "descriptor-retain-destination", "DESTINATION\n") &&
        core_text(libexec_path, "descriptor-retain-source/source-only", "SOURCE\n"));
close_preflight:
    eunion_test_fail_retain_fd = 0;
    eunion_test_after_native_dup2 = NULL;
    if (destination >= 0) sys_close(destination);
    if (destination_alias >= 0) sys_close(destination_alias);
    if (source >= 0) sys_close(source);
    if (source_alias >= 0) sys_close(source_alias);
}

static void descriptor_independent_open_checks(void) {
    const char* kind = "independent same-inode opens";
    int fixture = core_file(libexec_path, "descriptor-independent", "0123456789abcdef\n");
    descriptor_check(kind, "lower fixture", fixture);
    if (!fixture) return;
    int a = (int)sys_openat(BSD_AT_FDCWD, "/core-state/descriptor-independent", 0, 0);
    int b = (int)sys_openat(BSD_AT_FDCWD, "/core-state/descriptor-independent", 0, 0);
    int a_alias = a >= 0 ? (int)sys_fcntl(a, F_DUPFD, 0) : -1;
    int b_alias = b >= 0 ? (int)sys_dup(b) : -1;
    struct stat a_before, b_before;
    int setup = a >= 0 && b >= 0 && a_alias >= 0 && b_alias >= 0 &&
        fstat(a, &a_before) == 0 && fstat(b, &b_before) == 0 && descriptor_same_inode(&a_before, &b_before) &&
        sys_lseek(a, 2, SEEK_SET) == 2 && sys_lseek(b, 8, SEEK_SET) == 8 &&
        sys_lseek(a_alias, 0, SEEK_CUR) == 2 && sys_lseek(b_alias, 0, SEEK_CUR) == 8;
    descriptor_check(kind, "same inode has two independent OFDs and explicit duplicate lineages", setup);
    if (!setup) goto close_independent;
    long result = sys_fchmod(a, 0600);
    descriptor_check(kind, "one lineage promotes successfully", result == 0);
    descriptor_check(kind, "one promotion preserves every independent OFD position",
        sys_lseek(a, 0, SEEK_CUR) == 2 && sys_lseek(a_alias, 0, SEEK_CUR) == 2 &&
        sys_lseek(b, 0, SEEK_CUR) == 8 && sys_lseek(b_alias, 0, SEEK_CUR) == 8);
    struct stat sa, sb, saa, sba;
    descriptor_check(kind, "each explicit duplicate lineage preserves object identity after promotion",
        fstat(a, &sa) == 0 && fstat(a_alias, &saa) == 0 && fstat(b, &sb) == 0 && fstat(b_alias, &sba) == 0 &&
        descriptor_same_inode(&sa, &saa) && descriptor_same_inode(&sb, &sba) &&
        (sa.st_mode & 0777) == 0600);
    char bytes[2];
    descriptor_check(kind, "A reads at its original position without advancing B",
        read(a_alias, bytes, 2) == 2 && !memcmp(bytes, "23", 2) &&
        sys_lseek(a, 0, SEEK_CUR) == 4 && sys_lseek(b, 0, SEEK_CUR) == 8);
    descriptor_check(kind, "B reads at its original position without advancing A",
        read(b_alias, bytes, 2) == 2 && !memcmp(bytes, "89", 2) &&
        sys_lseek(b, 0, SEEK_CUR) == 10 && sys_lseek(a, 0, SEEK_CUR) == 4);
    descriptor_check(kind, "seeking either explicit alias affects only its own lineage",
        sys_lseek(a_alias, 5, SEEK_SET) == 5 && sys_lseek(b, 0, SEEK_CUR) == 10 &&
        sys_lseek(b_alias, 11, SEEK_SET) == 11 && sys_lseek(a, 0, SEEK_CUR) == 5);
    descriptor_check(kind, "promotion preserves lower and upper bytes",
        core_text(libexec_path, "descriptor-independent", "0123456789abcdef\n") &&
        core_text(prefix_path, "descriptor-independent", "0123456789abcdef\n"));
close_independent:
    if (a_alias >= 0) sys_close(a_alias);
    if (b_alias >= 0) sys_close(b_alias);
    if (a >= 0) sys_close(a);
    if (b >= 0) sys_close(b);
}

static void descriptor_directory_promotion_checks(void) {
    const char* kind = "directory first promotion";
    const char* guest = "/core-state/descriptor-promote-stream";
    char lower[4096], path[4096];
    snprintf(lower, sizeof(lower), "%s/core-state/descriptor-promote-stream", libexec_path);
    int fixture = core_dir(libexec_path, "descriptor-promote-stream") && chmod(lower, 0755) == 0;
    for (int i = 0; i < 4; ++i) {
        char relative[96];
        snprintf(relative, sizeof(relative), "descriptor-promote-stream/n0%d", i);
        fixture = core_file(libexec_path, relative, "LOWER\n") && fixture;
    }
    descriptor_check(kind, "readable lower directory and four children", fixture);
    if (!fixture) return;
    struct stat lower_before = {0};
    int captured = stat(lower, &lower_before) == 0;
    int original = (int)sys_openat(BSD_AT_FDCWD, guest, 0, 0);
    int alias = original >= 0 ? (int)sys_dup(original) : -1;
    descriptor_check(kind, "actual opened directory and duplicate", captured && original >= 0 && alias >= 0);
    if (!captured || original < 0 || alias < 0) goto close_promoted_directory;
    char name[256];
    long long cookie = -1;
    int first = core_next(original, name, &cookie);
    unsigned int first_bit = first == 1 && name[0] == 'n' && name[1] == '0' &&
        name[2] >= '0' && name[2] <= '3' && !name[3] ? 1u << (name[2] - '0') : 0;
    descriptor_check(kind, "partial materialized stream has a shared valid cookie", first_bit &&
        cookie > 0 && sys_lseek(alias, 0, SEEK_CUR) == cookie);
    long promoted = sys_fchmod(original, 0750);
    struct stat a = {0}, b = {0};
    descriptor_check(kind, "successful first promotion retains two live aliases of the upper inode",
        promoted == 0 && fstat(original, &a) == 0 && fstat(alias, &b) == 0 &&
        descriptor_same_inode(&a, &b) && !descriptor_same_inode(&a, &lower_before) &&
        (a.st_mode & 0777) == 0750 && (b.st_mode & 0777) == 0750);
    descriptor_check(kind, "promotion preserves alias SEEK_CUR instead of reporting stale reuse",
        first_bit && sys_lseek(alias, 0, SEEK_CUR) == cookie && sys_lseek(original, 0, SEEK_CUR) == cookie);
    for (int phase = 0; phase != 2; ++phase) {
        if (phase) {
            int writer = (int)sys_openat(BSD_AT_FDCWD, "/core-state/descriptor-promote-stream/upper",
                BSD_O_WRONLY | BSD_O_CREAT | BSD_O_EXCL, 0644);
            int created = writer >= 0 && write(writer, "UPPER\n", 6) == 6;
            if (writer >= 0) created = sys_close(writer) == 0 && created;
            descriptor_check(kind, "managed creation adds an upper child after exhaustion", created);
            descriptor_check(kind, "rewinding alias resets the shared promoted stream", sys_lseek(alias, 0, SEEK_SET) == 0);
        }
        unsigned int seen = phase ? 0 : first_bit;
        int unique = phase || first_bit, exhausted = 0;
        for (int step = 0; step < 16; ++step) {
            int result = core_next(step & 1 ? alias : original, name, NULL);
            if (result <= 0) { exhausted = result == 0; break; }
            unsigned int bit = !strcmp(name, "upper") ? 16u :
                name[0] == 'n' && name[1] == '0' && name[2] >= '0' &&
                name[2] <= '3' && !name[3] ? 1u << (name[2] - '0') : 0;
            if (!bit || (seen & bit)) unique = 0;
            seen |= bit;
        }
        descriptor_check(kind, phase ? "rewind exposes upper plus all lower children without replay or loss" :
            "alternating promoted aliases continue the saved stream without replay or loss",
            unique && exhausted && seen == (phase ? 31u : 15u));
        char page[32];
        descriptor_check(kind, phase ? "rewound aliases share final exhaustion" : "promoted aliases share exhaustion",
            vchroot_getdents_merge(original, page, sizeof(page)) == 0 &&
            vchroot_getdents_merge(alias, page, sizeof(page)) == 0);
    }
    struct stat lower_after = {0};
    int intact = stat(lower, &lower_after) == 0 && descriptor_same_inode(&lower_before, &lower_after) &&
        (lower_after.st_mode & 0777) == 0755;
    for (int i = 0; i < 4; ++i) {
        char relative[96];
        snprintf(relative, sizeof(relative), "descriptor-promote-stream/n0%d", i);
        intact = core_text(libexec_path, relative, "LOWER\n") && intact;
    }
    snprintf(path, sizeof(path), "%s/upper", lower);
    descriptor_check(kind, "lower directory identity mode and child bytes remain unchanged",
        intact && access(path, F_OK) == -1 && errno == ENOENT);
close_promoted_directory:
    if (alias >= 0) sys_close(alias);
    if (original >= 0) sys_close(original);
}

static void descriptor_transition_checks(void) {
    printf("\n== E-UNION descriptor replacement and OFD transitions ==\n");
    descriptor_file_replacement_checks();
    descriptor_directory_replacement_checks();
    descriptor_preflight_failure_checks();
    descriptor_independent_open_checks();
    descriptor_directory_promotion_checks();
}

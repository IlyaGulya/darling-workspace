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
#define LINUX_AT_FDCWD AT_FDCWD
#define LINUX_AT_SYMLINK_NOFOLLOW AT_SYMLINK_NOFOLLOW

/* stubs for symbols init_vchroot_path() references but never calls in TEST */
int dserver_rpc_vchroot_path(char* a, unsigned long b, uint64_t* c){(void)a;(void)b;(void)c;return -1;}
void __simple_abort(void){__builtin_trap();}
int __simple_printf(const char* f, ...){(void)f;return 0;}

/* rename the unit-under-test's main so we can supply our own */
#define main vchroot_unit_main
#include "vchroot_userspace.c"
#undef main

#include <stdio.h>
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
    memset(out, 0, sizeof(*out));
    out->linux_family = AF_UNIX;
    strcpy(expansion.path, input->sun_path);
    expansion.flags = VCHROOT_FOLLOW;
    expansion.dfd = -100;
    if (vchroot_expand(&expansion) < 0)
        return -ENOENT;
    strncpy(out->sun_path, expansion.path, sizeof(out->sun_path) - 1);
    return (int)offsetof(struct sockaddr_fixup, sun_path) +
        (int)strlen(out->sun_path) + 1;
}

int errno_linux_to_bsd(int error) {
    return error;
}

long linux_syscall(long a1, long a2, long a3, long a4, long a5, long a6, int number) {
    long result = syscall(number, a1, a2, a3, a4, a5, a6);
    return result < 0 ? -errno : result;
}

long sys_bind(int fd, const void* name, int socklen);

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

int main(void) {
    char cwd[4096];
    getcwd(cwd, sizeof(cwd));
    char prefix[4096], libexec[4096];
    snprintf(prefix,  sizeof(prefix),  "%s/prefix",  cwd);
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

    /* W1. whiteout of a lower-only file hides it: resolver reports ENOENT and
           does NOT fall back to the lower layer. */
    {
        int rv = vchroot_whiteout("/usr/bin/ls");   /* ls exists only in lower */
        check("W1 whiteout returns 0", rv == 0);
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

    /* W3. creating the path again (copy-up / write) over a whiteout must clear
           the marker and make the path live again. Here: write a new upper file
           at the whiteouted path and confirm it resolves. */
    {
        int rv = vchroot_unwhiteout("/usr/bin/ls");
        check("W3 unwhiteout returns 0", rv == 0);
        /* now copy it up fresh from lower */
        vchroot_copyup("/usr/bin/ls");
        snprintf(p, sizeof(p), "%s/usr/bin/ls", prefix);
        expect_resolves_to("/usr/bin/ls", p);
    }

    /* W4. whiteout of a path that exists in BOTH layers hides the lower one
           even though the upper copy is removed first. */
    {
        /* /usr/bin/sh is in both; remove upper copy then whiteout */
        snprintf(p, sizeof(p), "%s/usr/bin/sh", prefix);
        unlink(p);
        int rv = vchroot_whiteout("/usr/bin/sh");
        check("W4 whiteout (both layers) returns 0", rv == 0);
        expect_absent("/usr/bin/sh");
    }

    /* O1. opaque directory: lower entries are NOT merged in. Mark /opaquedir
           opaque in the upper; a child that exists only in the lower must be
           invisible. */
    {
        int rv = vchroot_set_opaque("/opaquedir");
        check("O1 set_opaque returns 0", rv == 0);
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
    /* The legacy W1/W2/W4/O1 ran on fixtures (/usr/bin/{ls,sh}, /opaquedir) that
       the copy-up tests above had already mutated; a no-op vchroot_whiteout /
       vchroot_set_opaque still left them "absent" for incidental reasons, so the
       tests could not catch a broken whiteout. These run on DEDICATED pristine
       lower-only fixtures and assert TWO independent things that a no-op breaks:
         (a) the merged resolver reports the name ENOENT (semantic hiding), and
         (b) the upper placeholder physically exists AND carries the marker xattr
             (so we never mistake a real file for a whiteout). */
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

            int rv = vchroot_whiteout(g);
            check("WM1 whiteout returns 0", rv == 0);
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
            int rv = vchroot_whiteout(g);
            check("WM2 whiteout (both layers) returns 0", rv == 0);
            expect_absent(g);
            char xv[8];
            ssize_t xn = lgetxattr(p, "user.union.whiteout", xv, sizeof(xv));
            check("WM2 placeholder carries marker (upper copy replaced)", xn >= 1);
            check("WM2 template victim untouched", access(l, F_OK) == 0);
        }

        /* WM3. unwhiteout WM1's placeholder removes the marker file so the lower
                file is visible again (re-creation path). */
        {
            const char* g = "/var/log/wh_lower";
            snprintf(p, sizeof(p), "%s/var/log/wh_lower", prefix);
            snprintf(l, sizeof(l), "%s/var/log/wh_lower", libexec);
            int rv = vchroot_unwhiteout(g);
            check("WM3 unwhiteout returns 0", rv == 0);
            check("WM3 placeholder removed", access(p, F_OK) != 0);
            expand(g, got);
            check("WM3 lower file visible again after unwhiteout",
                  strcmp(got, l) == 0 && access(l, F_OK) == 0);
        }

        /* OM1. opaque a pristine dir present in both layers: the lower-only child
                must vanish from the merged view, the upper-only child must remain,
                and the dir must carry the opaque marker. A no-op set_opaque leaves
                the lower child visible -> caught. */
        {
            const char* dir = "/var/opq";
            snprintf(p, sizeof(p), "%s/var/opq/up_child", prefix);
            /* precondition: lower child visible before opaque */
            snprintf(l, sizeof(l), "%s/var/opq/lo_child", libexec);
            expand("/var/opq/lo_child", got);
            check("OM1 fixture: lower child visible before opaque",
                  strcmp(got, l) == 0 && access(l, F_OK) == 0);

            int rv = vchroot_set_opaque(dir);
            check("OM1 set_opaque returns 0", rv == 0);
            /* (a) semantic: lower-only child now hidden */
            expect_absent("/var/opq/lo_child");
            /* upper-only child still visible */
            expect_resolves_to("/var/opq/up_child", p);
            /* (b) structural: the upper dir carries the opaque marker */
            char updir[4096]; snprintf(updir, sizeof(updir), "%s/var/opq", prefix);
            char xv[8];
            ssize_t xn = lgetxattr(updir, "user.union.opaque", xv, sizeof(xv));
            check("OM1 upper dir carries user.union.opaque marker", xn >= 1);
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
        vchroot_whiteout("/mergedir/low_only");   /* delete the lower-only entry */
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
        vchroot_set_opaque("/mergedir");
        ncount = vchroot_readdir_merge("/mergedir", names, sizeof(names));
        check("R3 opaque dir hides remaining lower-only entries",
              !name_in(names, ncount, "low2"));
        check("R3 opaque dir still shows upper entries",
              name_in(names, ncount, "up_only"));
    }

    printf("\n== E-UNION write-op wiring helpers ==\n");

    /* These model exactly what an emulation site (openat/unlinkat/...) calls on
       the GUEST path before/after the real syscall, so the syscall lands in the
       upper layer and deletions leave a whiteout. The site logic must be a thin
       call into these helpers -- the union policy lives here, not scattered. */

    /* P1. prepare_write on a lower-only file copies it up so a subsequent expand
           resolves to the WRITABLE upper copy (never the shared template). */
    {
        /* fresh lower-only file for this test */
        snprintf(l, sizeof(l), "%s/usr/local/share/tool.conf", libexec);
        snprintf(p, sizeof(p), "%s/usr/local/share/tool.conf", prefix);
        /* ensure it isn't already upper from an earlier test */
        unlink(p);
        int rv = vchroot_prepare_write("/usr/local/share/tool.conf");
        check("P1 prepare_write(lower-only) returns 0", rv == 0);
        check("P1 file now materialized in upper", access(p, F_OK) == 0);
        expect_resolves_to("/usr/local/share/tool.conf", p);
    }

    /* P2. prepare_write on an already-upper file is a no-op success (no second
           copy, content preserved). */
    {
        snprintf(p, sizeof(p), "%s/usr/bin/myecho", prefix);
        int rv = vchroot_prepare_write("/usr/bin/myecho");
        check("P2 prepare_write(upper) returns 0", rv == 0);
        check("P2 upper file still present", access(p, F_OK) == 0);
    }

    /* P3. prepare_write on a path absent in BOTH layers is a no-op success: an
           O_CREAT-style create will make it fresh in the upper; nothing to copy. */
    {
        int rv = vchroot_prepare_write("/usr/bin/brand-new-file");
        check("P3 prepare_write(absent) returns 0 (create-in-upper later)", rv == 0);
    }

    /* P4. post_unlink on a name that still exists in the LOWER layer drops a
           whiteout so it stays deleted (does not reappear from the template). */
    {
        /* /System/.../com.apple.test.plist exists only in lower */
        const char* g = "/System/Library/LaunchDaemons/com.apple.test.plist";
        int rv = vchroot_post_unlink(g);
        check("P4 post_unlink(lower-present) returns 0", rv == 0);
        expect_absent(g);
    }

    /* P5. post_unlink on a name absent from the lower layer must NOT create a
           spurious whiteout (nothing to hide). */
    {
        /* myecho is upper-only; after a real unlink it's simply gone */
        const char* g = "/usr/bin/myecho";
        snprintf(p, sizeof(p), "%s/usr/bin/myecho", prefix);
        unlink(p); /* simulate the real unlink the site already did */
        int rv = vchroot_post_unlink(g);
        check("P5 post_unlink(upper-only) returns 0", rv == 0);
        /* no whiteout placeholder should have been created */
        check("P5 no spurious whiteout placeholder", access(p, F_OK) != 0);
        expect_absent(g);
    }

    /* P6. pre_mkdir over a whiteouted path clears the marker so the new dir is
           live (mkdir-over-deleted-name). */
    {
        const char* g = "/usr/bin/ls";
        /* whiteout it first (it's been copied up earlier; whiteout replaces) */
        vchroot_whiteout(g);
        expect_absent(g);
        int rv = vchroot_pre_mkdir(g);
        check("P6 pre_mkdir clears whiteout returns 0", rv == 0);
        /* after clearing, a fresh upper object at the path would be visible;
           materialize one to prove the marker is gone */
        snprintf(p, sizeof(p), "%s/usr/bin/ls", prefix);
        check("P6 whiteout placeholder removed", access(p, F_OK) != 0);
    }

    printf("\n== E-UNION unlink/rmdir must not touch the template (dyra #1) ==\n");
    /* CRITICAL: the lower template is a SHARED live directory, not a RO mount.
       A delete syscall on a lower-only path would physically remove it from the
       template for ALL prefixes. vchroot_prepare_unlink(guest) decides the
       policy BEFORE the syscall and tells the site whether to run the host
       delete: lower-only -> drop a whiteout and SKIP the host delete; upper
       present -> PROCEED (site deletes upper, then post_unlink whiteouts if the
       name also exists in the template). Contract codes:
         VCHROOT_UNLINK_SKIP    (1) -> site must NOT call the host syscall
         VCHROOT_UNLINK_PROCEED (0) -> site calls the host delete
         < 0                        -> error */
    {
        /* U1. lower-only file: must SKIP the host delete and template stays. */
        const char* g = "/var/log/ulnk_lower";
        snprintf(l, sizeof(l), "%s/var/log/ulnk_lower", libexec);
        struct stat before; int had = (stat(l, &before) == 0);
        check("U1 fixture: lower-only victim present", had);
        int rv = vchroot_prepare_unlink(g);
        check("U1 prepare_unlink(lower-only) returns SKIP (1)", rv == 1);
        check("U1 template file UNTOUCHED after prepare_unlink",
              stat(l, &before) == 0);
        expect_absent(g); /* now whited-out in the merged view */

        /* U2. upper-only file: must PROCEED (site does the real unlink). */
        snprintf(p, sizeof(p), "%s/var/tmp/ulnk_upper", prefix);
        int up_present = (access(p, F_OK) == 0);
        check("U2 fixture: upper-only victim present", up_present);
        int rv2 = vchroot_prepare_unlink("/var/tmp/ulnk_upper");
        check("U2 prepare_unlink(upper-only) returns PROCEED (0)", rv2 == 0);

        /* U3. absent-in-both: nothing to hide, PROCEED (let the syscall ENOENT). */
        int rv3 = vchroot_prepare_unlink("/usr/bin/nonesuch");
        check("U3 prepare_unlink(absent) returns PROCEED (0)", rv3 == 0);
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
            vchroot_dir_closed(_fd); /* emulation's close() must do this */    \
            close(_fd);                                                        \
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

    /* G2. whiteout placeholder in the UPPER dir is never emitted by the pager. */
    {
        char got[8192]; int gotn;
        /* low_only was whiteouted earlier (R2); mergedir made opaque (R3). Use a
           fresh dir to isolate: whiteout an upper name and confirm it's hidden. */
        vchroot_whiteout("/opaquedir/upperchild"); /* upper file -> whiteout */
        DRAIN_INTO("/opaquedir", got, gotn);
        check("G2 whiteouted upper entry absent from paged listing",
              !name_in(got, gotn, "upperchild"));
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
            vchroot_dir_closed(_fd);
            close(_fd);
        }
        check("G4 fixture: dtcheck dir opened and drained", _fd >= 0);
        check("G4 subdir entry reports d_type == DT_DIR (4)", ty_sub == DT_DIR);
        check("G4 file entry reports d_type == DT_REG (8)", ty_file == DT_REG);
        check("G4 subdir entry has a real inode (not synthetic 1)", ino_sub > 1);
        check("G4 file entry has a real inode (not synthetic 1)", ino_file > 1);
    }

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
        char got[4096];

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

        /* H2. RELATIVE-path copy-up resolution. The write-op sites gate copy-up
               on guest[0]=='/'; a dirfd-relative target is silently skipped.
               vchroot_prepare_write_at(dfd, relpath) must resolve the *at() path
               to guest-absolute (via the dirfd's guest path) and then copy up.
               Use a FRESH lower-only target (/usr/lib/target_file) addressed
               relative to a guest dir fd, so the assertion is uncontaminated. */
        {
            /* open the guest dir /usr/lib (resolves into the lower template) */
            char dirhost[4096];
            snprintf(dirhost, sizeof(dirhost), "%s/usr/lib", libexec);
            int dfd = open(dirhost, O_RDONLY | O_DIRECTORY);
            check("H2 guest dir fd opens", dfd >= 0);
            if (dfd >= 0) {
                int rv = vchroot_prepare_write_at(dfd, "target_file");
                snprintf(p, sizeof(p), "%s/usr/lib/target_file", prefix);
                check("H2 prepare_write_at(dirfd, relpath) returns 0", rv == 0);
                check("H2 relative-path target materialized in upper",
                      access(p, F_OK) == 0);
                close(dfd);
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
    /* rename moves the (copied-up) upper source into the destination. If the
       destination's PARENT is lower-only, the expanded dest path points into the
       shared template, so the host rename would move INTO the template. And if
       the destination NAME already exists only in the lower template, after the
       rename the old lower namesake must be hidden (whiteout) so it does not
       resurrect in the merged view. vchroot_prepare_rename_dest(newpath) handles
       both BEFORE the host rename. */
    {
        /* RN1. dest parent is lower-only: must copy it up so the rename lands in
                the upper layer, template parent left untouched. */
        int rv = vchroot_prepare_rename_dest("/var/empty_lowerdir/moved");
        check("RN1 prepare_rename_dest(into lower-only dir) returns >= 0", rv >= 0);
        snprintf(p, sizeof(p), "%s/var/empty_lowerdir", prefix);
        check("RN1 dest parent materialized in upper", access(p, F_OK) == 0);
        snprintf(l, sizeof(l), "%s/var/empty_lowerdir", libexec);
        check("RN1 template dest parent untouched", access(l, F_OK) == 0);

        /* RN2. dest NAME exists only in lower: prepare must whiteout it so the
                old template content is hidden once the new object takes its name.
                Use a fresh lower-only victim. */
        const char* dst = "/var/log/ren_victim";
        int rv2 = vchroot_prepare_rename_dest(dst);
        check("RN2 prepare_rename_dest(over lower dest) returns >= 0", rv2 >= 0);
        /* after prepare, the lower dest must be hidden in the merged view... */
        char g[4096]; expand(dst, g);
        /* upper now carries a whiteout placeholder for the dest name */
        snprintf(p, sizeof(p), "%s/var/log/ren_victim", prefix);
        check("RN2 whiteout placeholder created for lower dest",
              access(p, F_OK) == 0);
        /* ...and the template victim itself is UNTOUCHED on disk */
        snprintf(l, sizeof(l), "%s/var/log/ren_victim", libexec);
        check("RN2 template dest victim untouched on disk", access(l, F_OK) == 0);
    }

    printf("\n== E-UNION create must copy up a lower-only parent (dyra #3) ==\n");
    /* mkdirat/mknod/mkfifo/symlinkat create a new entry. If the parent dir is
       lower-only, the expanded create path points into the shared template and
       the host syscall would create the entry INSIDE the template. The sites
       must copy up the parent first (only bind.c did). vchroot_prepare_create
       (guest) = copy up the parent dir + clear any whiteout at the new name. */
    {
        /* /var/createparent is a DEDICATED lower-only dir that NO other test
           touches (RN1/RN2 use /var/empty_lowerdir; sharing it made CR1 pass even
           when prepare_create's copy-up was disabled -- the parent was already
           upper from RN1). With a pristine parent, "materialized in upper" can
           only be true if prepare_create itself copied it up. A create of
           /var/createparent/newfile must materialize the parent in the upper
           layer without touching the template. */
        const char* g = "/var/createparent/newfile";
        /* guard: parent must NOT pre-exist in the upper layer */
        snprintf(p, sizeof(p), "%s/var/createparent", prefix);
        check("CR1 fixture: parent absent from upper before prepare",
              access(p, F_OK) != 0);
        int rv = vchroot_prepare_create(g);
        check("CR1 prepare_create(in lower-only parent) returns >= 0", rv >= 0);
        check("CR1 parent dir materialized in upper", access(p, F_OK) == 0);
        /* the new name itself must NOT yet exist (create makes it after) */
        snprintf(p, sizeof(p), "%s/var/createparent/newfile", prefix);
        check("CR1 new name not pre-created by prepare", access(p, F_OK) != 0);
        /* template parent dir untouched (still empty, still present) */
        snprintf(l, sizeof(l), "%s/var/createparent", libexec);
        check("CR1 template parent untouched", access(l, F_OK) == 0);
    }

    /* SP1: create through a SYMLINKED lower-only parent. The real-world failure:
       `touch /tmp/x` inside the guest returned ENOENT under EUNION because /tmp is
       a symlink (-> private/tmp) and prepare_create copied up the SYMLINK pointing
       at a NON-materialized target dir, so the later open(O_CREAT) followed the
       upper symlink into a directory absent from the upper layer.
       Fixture: /var/sp_link -> sp_real (relative), both lower-only.
       prepare_create("/var/sp_link/newfile") MUST materialize the symlink's REAL
       TARGET dir (/var/sp_real) in the upper layer, so a create through the
       symlink resolves to a real upper directory. */
    {
        const char* g = "/var/sp_link/newfile";
        /* guards: neither the symlink target nor the symlink itself is upper yet */
        snprintf(p, sizeof(p), "%s/var/sp_real", prefix);
        check("SP1 fixture: target dir absent from upper before prepare",
              access(p, F_OK) != 0);
        int rv = vchroot_prepare_create(g);
        check("SP1 prepare_create(through symlinked parent) returns >= 0", rv >= 0);
        /* THE FIX: the symlink's real target dir must be a real upper directory */
        struct stat tst;
        snprintf(p, sizeof(p), "%s/var/sp_real", prefix);
        check("SP1 symlink target dir materialized as a real upper dir",
              stat(p, &tst) == 0 && S_ISDIR(tst.st_mode));
        /* end-to-end: a create through the upper symlink path must now SUCCEED and
           land in the upper real target dir (this is what open(O_CREAT) does). */
        snprintf(p, sizeof(p), "%s/var/sp_link/newfile", prefix);
        int fd = open(p, O_CREAT | O_WRONLY, 0644);
        check("SP1 create through upper symlink succeeds (no ENOENT)", fd >= 0);
        if (fd >= 0) close(fd);
        snprintf(p, sizeof(p), "%s/var/sp_real/newfile", prefix);
        check("SP1 created file landed in upper real target dir",
              access(p, F_OK) == 0);
        /* template target dir untouched (no newfile leaked into the template) */
        snprintf(l, sizeof(l), "%s/var/sp_real/newfile", libexec);
        check("SP1 template target dir untouched (no leak)", access(l, F_OK) != 0);
    }

    printf("\n== E-UNION mkdir over a removed lower dir must be opaque (.6) ==\n");
    /* CRITICAL delete-recreate: rmdir of a populated lower-only dir drops a
       whiteout (prepare_unlink); a subsequent mkdir of the SAME name unwhiteouts
       + copies up the parent (prepare_create) and creates a fresh upper dir. If
       that new dir is NOT marked opaque, the resolver/readdir re-merge the OLD
       lower children -> deleted content resurrects. The site must call
       vchroot_post_mkdir(guest) AFTER the mkdir to set opaque whenever the name
       shadows a lower directory. This block models the exact site sequence on a
       DEDICATED lower-only populated dir, then asserts the merged view is EMPTY. */
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

        /* step 1: rmdir -> lower-only dir, prepare_unlink drops a whiteout + SKIP */
        int u = vchroot_prepare_unlink(dir);
        check("MK1 rmdir(lower dir) returns SKIP (whiteout, no host delete)", u == 1);
        expect_absent(dir); /* now whited-out */

        /* step 2: mkdir of the same name. The site sequence is
              prepare_create (unwhiteout + copyup parent)
              -> host mkdir of the upper dir
              -> post_mkdir (set opaque if it shadows a lower dir). */
        int c = vchroot_prepare_create(dir);
        check("MK1 prepare_create returns 0", c == 0);
        /* the real mkdirat would create the upper dir here; do the same on disk */
        check("MK1 host mkdir of upper dir succeeds",
              mkdir(p, 0755) == 0 || access(p, F_OK) == 0);
        int pm = vchroot_post_mkdir(dir);
        check("MK1 post_mkdir returns 0", pm == 0);

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
            int c2 = vchroot_prepare_create(fresh);
            check("MK2 prepare_create(fresh) returns 0", c2 == 0);
            check("MK2 host mkdir of fresh upper dir",
                  mkdir(p, 0755) == 0 || access(p, F_OK) == 0);
            int pm2 = vchroot_post_mkdir(fresh);
            check("MK2 post_mkdir(fresh, no lower namesake) returns 0", pm2 == 0);
            char xv[8];
            ssize_t xn = lgetxattr(p, "user.union.opaque", xv, sizeof(xv));
            check("MK2 fresh dir is NOT opaque (no lower namesake to mask)",
                  xn < 0);
        }
    }

    printf("\n== E-UNION fd-metadata copy-up must not touch the template (dyra #4) ==\n");
    /* fchmod/futimes act on an fd with no path. If the fd was opened O_RDONLY on
       a lower-only file, the inode is the template's; a metadata change would hit
       the template. vchroot_prepare_write_fd(fd) recovers the fd's guest path
       (vchroot_fdpath), copies it up, and returns the guest path so the caller
       can re-open the upper copy. Returns >=0 on success (0 if unmanaged/escape),
       writing the guest path into the provided buffer. */
    {
        /* open a lower-only file O_RDONLY (template inode), then prepare_write_fd:
           the file must materialize in the upper layer and the template stay. */
        snprintf(l, sizeof(l), "%s/var/log/fdmeta_lower", libexec);
        int lfd = open(l, O_RDONLY);
        check("FD1 fixture: lower-only file opens O_RDONLY", lfd >= 0);
        if (lfd >= 0) {
            char gbuf[4096];
            int rv = vchroot_prepare_write_fd(lfd, gbuf, sizeof(gbuf));
            check("FD1 prepare_write_fd returns guest path len > 0", rv > 0);
            check("FD1 recovered guest path is /var/log/fdmeta_lower",
                  rv > 0 && strcmp(gbuf, "/var/log/fdmeta_lower") == 0);
            snprintf(p, sizeof(p), "%s/var/log/fdmeta_lower", prefix);
            check("FD1 file materialized in upper after prepare_write_fd",
                  access(p, F_OK) == 0);
            check("FD1 template file untouched", access(l, F_OK) == 0);
            close(lfd);
        }

        /* FD2. END-TO-END high-level helper. This is what the fchmod/futimes/
           fsetxattr/fremovexattr sites actually call: open a lower-only file,
           hand the fd to vchroot_fd_for_meta_write(), and apply the metadata op
           to the RETURNED fd. The helper must copy the file up and return a FRESH
           fd on the UPPER copy (the original fd still points at the lower inode!).
           A chmod via the returned fd must change the UPPER mode and leave the
           TEMPLATE's 0644 untouched. With the stub (returns the original fd) the
           chmod lands on the lower inode -> template mode changes -> RED. */
        {
            snprintf(l, sizeof(l), "%s/var/log/fdmeta_e2e", libexec);
            snprintf(p, sizeof(p), "%s/var/log/fdmeta_e2e", prefix);
            unlink(p); /* ensure pristine: not copied up by an earlier test */
            struct stat lst0; stat(l, &lst0);
            check("FD2 fixture: template starts 0644",
                  (lst0.st_mode & 0777) == 0644);

            int fd = open(l, O_RDONLY); /* lower inode */
            check("FD2 fixture: lower file opens O_RDONLY", fd >= 0);
            if (fd >= 0) {
                int wfd = vchroot_fd_for_meta_write(fd);
                check("FD2 helper returns a usable fd (>=0)", wfd >= 0);
                check("FD2 helper re-opened a DIFFERENT fd (upper copy)",
                      wfd != fd);
                /* apply the metadata op (chmod 0600) to the returned fd */
                if (wfd >= 0) {
                    int cr = fchmod(wfd, 0600);
                    check("FD2 fchmod via returned fd succeeds", cr == 0);
                    if (wfd != fd) close(wfd);
                }
                close(fd);

                /* the UPPER copy must now be 0600 ... */
                struct stat ust; int us_ok = (stat(p, &ust) == 0);
                check("FD2 upper copy materialized", us_ok);
                check("FD2 upper copy got the new mode (0600)",
                      us_ok && (ust.st_mode & 0777) == 0600);
                /* ... and the TEMPLATE must be UNTOUCHED at 0644 */
                struct stat lst1; stat(l, &lst1);
                check("FD2 template mode UNTOUCHED (still 0644)",
                      (lst1.st_mode & 0777) == 0644);
            }
        }

        /* FD3. UPPER-only fd: copy-up is a no-op, but the object IS union-managed,
           so the helper still hands back a usable fd that targets the upper copy.
           (It may be a fresh fd -- that is fine; the caller's uniform rule is
           "apply the op to the returned fd, and close it if it differs from the
           original".) A mutation via the returned fd must change the upper file. */
        {
            snprintf(p, sizeof(p), "%s/var/tmp/fdmeta_upper", prefix);
            int fd = open(p, O_RDONLY);
            check("FD3 fixture: upper-only file opens", fd >= 0);
            if (fd >= 0) {
                int wfd = vchroot_fd_for_meta_write(fd);
                check("FD3 helper returns a usable fd for an upper-only object",
                      wfd >= 0);
                if (wfd >= 0) {
                    int cr = fchmod(wfd, 0600);
                    check("FD3 fchmod via returned fd succeeds", cr == 0);
                    if (wfd != fd) close(wfd);
                }
                struct stat ust; stat(p, &ust);
                check("FD3 upper file got the new mode (0600)",
                      (ust.st_mode & 0777) == 0600);
                close(fd);
            }
        }

        /* FD4. UNMANAGED fd (escapes the union -- e.g. /dev/null on the host):
           the helper must return the ORIGINAL fd unchanged so the caller operates
           on it directly and does NOT close a fd it did not open here. */
        {
            int fd = open("/dev/null", O_RDONLY);
            check("FD4 fixture: host /dev/null opens", fd >= 0);
            if (fd >= 0) {
                int wfd = vchroot_fd_for_meta_write(fd);
                check("FD4 unmanaged fd -> helper returns the SAME fd",
                      wfd == fd);
                close(fd);
            }
        }

        /* FT (.11). ftruncate is a CONTENT mutation by fd -- structurally identical
           to fchmod/futimes (.4) but it was omitted from that set, so ftruncate.c
           still hit the raw fd with no copy-up. This pins the wiring sys_ftruncate
           must use: hand the fd to vchroot_fd_for_meta_write(), ftruncate the
           RETURNED fd, close it if it differs. On a lower-only file opened (here)
           on the lower inode, the helper copies up and re-opens the UPPER copy; the
           truncate must shrink the UPPER copy and leave the TEMPLATE's bytes intact.
           The buggy code (ftruncate the ORIGINAL fd) truncates the lower inode ->
           template content lost -> RED. Dedicated pristine fixture. */
        {
            snprintf(l, sizeof(l), "%s/var/log/ftrunc_lower", libexec);
            snprintf(p, sizeof(p), "%s/var/log/ftrunc_lower", prefix);
            unlink(p); /* pristine: not copied up by an earlier test */
            struct stat lst0; stat(l, &lst0);
            check("FT fixture: template starts non-empty",
                  lst0.st_size > 0);
            off_t orig_size = lst0.st_size;

            int fd = open(l, O_RDONLY); /* lower inode */
            check("FT fixture: lower file opens", fd >= 0);
            if (fd >= 0) {
                /* exactly the wiring sys_ftruncate must perform under EUNION:
                   the CONTENT-write helper (O_RDWR upper fd), not the meta helper */
                int wfd = vchroot_fd_for_content_write(fd);
                check("FT helper returns a usable fd (>=0)", wfd >= 0);
                check("FT helper re-opened a DIFFERENT fd (upper copy)",
                      wfd != fd);
                if (wfd >= 0) {
                    int tr = ftruncate(wfd, 0);
                    check("FT ftruncate(returned fd, 0) succeeds", tr == 0);
                    if (wfd != fd) close(wfd);
                }
                close(fd);

                /* the UPPER copy must now be truncated to 0 ... */
                struct stat ust; int us_ok = (stat(p, &ust) == 0);
                check("FT upper copy materialized", us_ok);
                check("FT upper copy truncated to 0", us_ok && ust.st_size == 0);
                /* ... and the TEMPLATE must keep its original bytes */
                struct stat lst1; stat(l, &lst1);
                check("FT template size UNTOUCHED (original bytes preserved)",
                      lst1.st_size == orig_size);
            }
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

        /* XS1. the copy-up a path-based setxattr/removexattr site performs: on a
                lower-only target, vchroot_prepare_write copies it up so the
                expanded path resolves to the WRITABLE upper copy and the real
                l/setxattr never mutates the shared template. (The site wiring is
                prepare_write -> expand -> syscall on vc.path; this pins the
                primitive that makes that correct. Dedicated fixture so no earlier
                copy-up test contaminates it.) */
        {
            const char* g = "/var/log/xattr_lower";
            snprintf(l, sizeof(l), "%s/var/log/xattr_lower", libexec);
            snprintf(p, sizeof(p), "%s/var/log/xattr_lower", prefix);
            unlink(p); /* pristine */
            check("XS1 fixture: lower-only target present", access(l, F_OK) == 0);
            check("XS1 fixture: no upper copy yet", access(p, F_OK) != 0);
            int rv = vchroot_prepare_write(g);
            check("XS1 prepare_write(lower-only xattr target) returns 0", rv == 0);
            check("XS1 target materialized in upper (setxattr lands upper)",
                  access(p, F_OK) == 0);
            expect_resolves_to(g, p);
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

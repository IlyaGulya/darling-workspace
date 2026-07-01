# E-UNION design notes (dar-test-infra-sp5.8.4.4)

Union/overlay-by-path-translation inside Darling's vchroot: a writable **upper**
layer (per-prefix `$DPREFIX`) unioned over a shared read-only **lower** template
(`$LIBEXEC`), implemented purely by rewriting guest paths on the client side — no
Linux mount, no fuse, no overlayfs kernel driver, no user namespaces. Works in
any unprivileged container, including default root-docker.

## Provenance & licensing of borrowed ideas

All E-UNION code is **new C written from scratch** for Darling and is covered by
Darling's existing license (GPL-3) — the same as the file it lives in
(`vchroot_userspace.c`) and the surrounding emulation layer. No third-party source
code is copied or translated into the tree; what we reused are *algorithms and
design concepts*, independently reimplemented. The reference projects below were
cloned locally for study only and are **NOT part of Darling source** (they live at
the repo root `references/`, outside both the Darling source tree and this
workspace, and ship with nothing):

- **gVisor** — Apache License 2.0. https://github.com/google/gvisor
  (`pkg/sentry/fsimpl/overlay/`: `copy_up.go`, `directory.go`, `overlay.go`).
  Primary idea source: copy-up trigger set & per-type copy, whiteout/opaque
  encoding, `userxattr` unprivileged fallback, and the merged-readdir algorithm.
  gVisor is Go; our implementation is C using raw Linux syscalls with a different
  cross-process atomicity model (staging-dir + atomic `rename()`), so it is an
  independent reimplementation of the algorithm, not a derivative of gVisor's
  copyrighted code. Apache-2.0 does not require attribution for reimplemented
  ideas; we credit gVisor by file throughout anyway for auditability.
- **PRoot** — GPL-2. https://github.com/proot-me/proot. Studied as the closest
  userspace-path-translation analog. **No PRoot code or PRoot-specific design
  landed in the patches** — our path engine is Darling's preexisting `vchroot`,
  and PRoot's bind-mount-chain model is architecturally unrelated to our
  upper/lower union. Listed here only to record that it was reviewed.
- Linux `Documentation/filesystems/overlayfs.rst` — canonical overlay semantics
  (kernel docs, GPL-2). Used to validate whiteout/opaque/merge behaviour. No code.

Per-patch commit messages cite the specific gVisor file an algorithm was modeled
on (e.g. "Modeled on gVisor overlay copy_up.go"); see the eunion-N patch headers.

## Layering decision

Per-component union resolve (upper wins, fall back to lower). Implemented in
`vchroot_run()` behind `#ifdef EUNION`. Read-only resolve is DONE and tested
(runner.c tests 1-8). Production builds (no -DEUNION) are byte-identical to
baseline — verified.

## The environment constraint that drives every encoding choice (gVisor F.8)

The translator runs as uid 0 *inside* the container but is NOT real host root:
- char-dev(0,0) whiteouts (overlayfs/gVisor default) need **CAP_MKNOD** → unavailable.
- `trusted.overlay.*` xattrs need **CAP_SYS_ADMIN** → unavailable.

gVisor hit the identical wall and falls back to `user.overlay.*` under its
`userxattr` mode (overlay.go:249-256). So OUR encodings must be unprivileged:

- **Opaque dir**: `user.union.opaque="y"` xattr (settable by file owner on ext4,
  no privilege). Mirrors gVisor userxattr.
- **Whiteout**: char-dev is out. Use a regular **sentinel file** carrying
  `user.union.whiteout="y"` (cleanest, no name collisions) — OR classic `.wh.<name>`
  filename convention subtracted in readdir. Decide in proto; xattr-on-placeholder
  preferred.
- Probe `user.*` xattr support on the upper fs at init (gVisor mount-time probe).

## Copy-up (gVisor copy_up.go)

Trigger set — copy-up the lower-only object BEFORE these proceed:
- open with write access (O_WRONLY/O_RDWR; O_TRUNC implies write)
- O_CREAT over a whiteout (copy up *parent*, unlink whiteout, create in upper)
- chmod/chown/utimes/truncate (path + fd variants)
- rename source (+ ALL descendants if dir) + new parent
- link source; setxattr; removexattr
- any create (mkdir/mknod/symlink/create/link) copies up the *parent*
Read-only opens do NOT copy up.

Per-type: REG = open lower RDONLY + create upper O_WRONLY|O_CREAT|O_EXCL(mode) +
copy data + re-SetStat (uid/gid/atime/mtime/mode; data copy clears setid).
DIR = mkdir + SetStat. LNK = readlink + symlink + SetStat. BLK/CHR = mknod(rdev)
+ SetStat. FIFO/socket = EPERM. Preserve mtime (read from lower *before* copy-up),
xattrs (skip overlay-private; security.capability is must-copy). ctime not preservable.

Hardlinks: gVisor does NOT preserve link identity — copy-up breaks the link
(st_nlink=1 on upper). Default: document break-on-copy-up.

## Atomicity / concurrency (gVisor F.6/F.7 — our hardest problem)

Darling runs a SEPARATE translator per client process (launchd + daemons + shell)
all doing copy-up into the SAME upper prefix. gVisor's copyMu is in-process only;
cross-process it relies on `O_EXCL`/`mkdir` atomic-create as winner-takes-all +
post-create re-check. BUT O_EXCL publishes a zero-length file before data copy →
a concurrent reader can see a partial file. gVisor skips the workdir and is NOT
safe against concurrent external readers mid-copy.

Our plan (safer than gVisor because we must share the upper across processes):
  copy fully into `UPPER/.union-work/<unique>` (data+metadata+xattrs)
  → atomic `rename()` into the visible target (atomic on ext4)
  → loser of a race gets EEXIST / clean overwrite; treat as "already copied up".
Markers (whiteout/opaque) publish via atomic mknod-substitute/mkdir/setxattr,
EEXIST handled idempotently.

## Path-translation gotchas we must handle manually (gVisor/PRoot section F)

- F.1/F.4 stale lower fd & dir-fd across copy-up → route writable opens through
  eager copy-up at open time; translate *at() via /proc/self/fd/N re-canon.
- F.2 inode identity: st_ino jumps on copy-up; lower files across host fs collide.
  Maintain (layer,host-dev,host-ino)->synthetic (dev,ino) map; keep dir ino stable.
- F.3 rename atomicity: order copy-up(src+desc)→copy-up(newParent)→host rename→
  whiteout at origin→opaque on dest. Define failure policy (gVisor panics).
- F.5 getcwd/realpath/proc magic links return HOST paths → detranslate; track
  per-process guest cwd. (vchroot_fdpath/unexpand already do part of this.)
- F.9 NEVER translate a write op to a lower path — copy-up first. Audit all ~24
  mutating emulation sites.
- F.10 canonicalize `..`/`.`/symlinks against the UNION view before layer resolve;
  `..` at root clamps to root.

## Status: all four core algorithms prototyped + green (46 assertions)

Implemented in vchroot_userspace.c behind #ifdef EUNION; production builds
(no -DEUNION) are byte-identical to baseline (verified: every E-UNION symbol is
absent without the flag). Test runner = experiments/e-union (run.sh + runner.c).

  DONE  read-only union resolve   vchroot_run() two-layer       8 tests
  DONE  race-safe copy-up         vchroot_copyup()             16 tests (incl C7
        16-way cross-process fork race over 4MiB: no partial, all ok, 1 copy)
  DONE  whiteout                  vchroot_whiteout/unwhiteout   (user.* xattr)
  DONE  opaque dir                vchroot_set_opaque()         12 tests
  DONE  readdir-merge             vchroot_readdir_merge()      10 tests

Key empirical findings:
  * user.* xattr works unprivileged on ext4 (whiteout/opaque encoding viable).
  * the kernel refuses user.* xattr on a mode-0000 file (EACCES) -> whiteout
    placeholder must be owner-writable (0600). (Test caught this.)
  * tmp+rename atomic publish defeats the cross-process copy-up race that gVisor
    (in-process copyMu only) does not guard against.

## Step 6+7: real build + route-2 boot in DEFAULT unprivileged docker

Build switch `DARLING_EUNION` (top-level CMake option, OFF by default; shared by
the darlingserver, libsystem_kernel/emulation, vchroot, and mldr targets). OFF =>
whole tree byte-identical to baseline. ON => `-DEUNION` + `EUNION_LIBEXEC_PATH`
on the emulation; zero-copy prefix branch in darlingserver; loader fallbacks in
vchroot/mldr.

Built the full tree Release+DARLING_EUNION=ON (26420 targets), installed to a
staging tree, ran `DARLING_ROOTLESS=1 darling shell` in a DEFAULT `docker run`
(ubuntu:24.04, NO --privileged / --cap-add / --device /dev/fuse / userns; install
tree mounted :ro so the template is provably never written).

PROVEN end-to-end in that container:
  * zero-copy: prefix bootstrap creates an empty prefix + .union-work marker
    (~156 KB) instead of copying the template (copy-mode prefix = ~524 MB). The
    :ro template mount is never written.
  * activation: eunion_init_from_prefix() enables the union iff $prefix/.union-work
    exists (the server's zero-copy branch creates it); absent => inert = baseline.
  * union resolve + loader: launchd boots THROUGH the union -- dyld and every
    libsystem dylib load from the read-only template (verified via /proc/<pid>/maps),
    launchd copies up /private/etc/passwd etc. and creates /var/run/{utmpx,
    .systemStarterRunning} in the upper layer.
  * readdir-merge on REAL data: the staged template's /System/Library/LaunchDaemons
    (20 plists) merges 20/20 through vchroot_getdents_merge from an empty upper.

Boot-bring-up bugs found and fixed (all #ifdef, prod byte-identical):
  * objc4 `#error mismatch in debug-ness macros` -- baseline, not E-UNION: needs
    `-DCMAKE_BUILD_TYPE=Release` (defines NDEBUG so DEBUG==OBJC_IS_DEBUG_BUILD==0).
  * vchroot.c pre-vchroot access() of the init binary -- fall back to the template
    (DYLD_ROOT_PATH) when the empty upper prefix lacks it.
  * mldr.c load() raw open() of dyld / guest binary -- fall back to the template
    (INSTALL_PREFIX "/libexec/darling") using lr->root_path; no RPC/env dependency.
  * sys_bind AF_UNIX -- copy-up the socket path's parent dir (a bind CREATES a
    file; /var/run must be in the writable upper before binding the socket).
  * vchroot_getdents_merge -- pass through raw getdents64 when the union is
    inactive (don't synthesize d_ino/d_type with nothing to merge).

FORMER WALL (now RESOLVED): the launchctl-bootstrap stall was NOT a Mach reply
path problem -- it was a path-DETRANSLATION bug in the union, found by stracing
the guest tree (launchd=pid1, launchctl=the bootstrap child).

  Symptom: `launchctl bootstrap -S System` opens System/Library/LaunchDaemons
  (the union resolves it into the read-only LOWER template), getdents-merges the
  52 entries correctly, then opens ZERO of them and waits 60s before exit_group(0)
  -- launchd's bootstrap never completes because no System daemon was registered.

  Root cause: vchroot_fdpath() / vchroot_unexpand() only knew the prefix (UPPER).
  A fd/path resolving into the LOWER template lives OUTSIDE the prefix, so both
  fell through to the EXIT_PATH escape hatch -> a bogus guest path
  /Volumes/SystemRoot/<host-template-path>. launchctl F_GETPATHs the LaunchDaemons
  dir fd to build each child plist path; the escape-hatch base made every plist
  lookup ENOENT, so it loaded no daemons. (Trace: repeated
  openat("/.../Volumes/SystemRoot/usr/local/libexec/darling/System/Library/
  LaunchDaemons") = ENOENT just before the 60s wait.)

  Fix (xnu 35025e9, both functions, #ifdef EUNION): when the readlink/path result
  is under libexec_path, strip it and return the bare guest path, exactly as the
  existing prefix_path branch does. Genuinely-out-of-both paths still escape via
  EXIT_PATH. Hermetic suite extended to 76/76 (F1-F5 pin lower/upper/out-of-both
  detranslation for both fdpath and unexpand).

  RESULT -- EUNION zero-copy now boots a FULL guest pipeline in DEFAULT
  unprivileged docker (no --privileged / caps / fuse / userns; template :ro):
      DARLING_ROOTLESS=1 DPREFIX=/tmp/... darling shell echo HELLO_EUNION
        -> HELLO_EUNION
        -> rc=0
        -> prefix ~572 KB  (vs ~524 MB copy-mode)  == true zero copy
  (The earlier "stall reproduces only on zero-copy / copy-mode clears it"
  observation fits: copy-mode has real files at the prefix path so F_GETPATH
  returns a correct in-prefix guest path; only the union's lower-layer fd hit the
  escape hatch.)

  TEST-HYGIENE finding (kept for future runs): the harness default prefix
  /root/.darling-eunion fails with "Cannot access ... Permission denied" because
  /root is mode 0700 and the launcher stat()s the prefix BEFORE any setuid, so a
  0700 ancestor yields EACCES. Use a world-searchable prefix dir (/tmp/...).
  Also: a raw `cp -a` of the template is NOT a bootable prefix (copy-mode `cp -a`
  also stalls) -- only the server's own bootstrap (copyAndSetAttributes, or the
  zero-copy branch) produces one. Compare bootstrap-vs-bootstrap.

## Steps 5/6/7 -- ALL DONE

  5. DONE  copyup/whiteout wired into 13 write-op emulation sites + getdirentries
           routed through vchroot_getdents_merge (xnu 6d9c61e, e1d1957).
  6. DONE  libexec_path plumbed at startup via the $prefix/.union-work marker +
           DARLING_EUNION build switch (xnu 600b763; top-level 5887e4a44).
  7. DONE  full Darling built -DDARLING_EUNION=ON; FULL route-2 guest pipeline
           boots zero-copy in DEFAULT unprivileged docker:
             darling shell echo HELLO_EUNION -> HELLO_EUNION, rc=0, prefix ~568KB.
           Bring-up fixes: loader fallbacks (top-level 4c4f7c313), AF_UNIX bind
           copy-up (d7cd015), getdents passthrough (dd0856b), and the keystone
           fdpath/unexpand lower-template detranslation (35025e9) that cleared the
           launchctl-bootstrap stall. Hermetic suite 76/76.

## Hardening pass (TDD; runner.c H1-H5, suite now 92/92) -- xnu 1f1a93f

The 5 correctness gaps in the write-op API were audited by TDD. Two were real
bugs (RED then fixed); three were already correct (pinned with regression tests):

  H1 RENAME of a lower-only DIR with contents -- BUG, fixed. rename moves the
     upper dir then whiteouts the origin, so the lazy lower-merge stops applying
     and template-only descendants would vanish. Added vchroot_copyup_tree()
     (recursive copy-up of node + all descendants via getdents on the lower dir),
     called from the renameat source. (gVisor copy_up.go F.3.)
  H2 dirfd-RELATIVE write targets -- BUG, fixed. The sites gated on guest[0]=='/',
     so openat(dirfd,"rel",O_WRONLY) into a lower-only dir skipped copy-up. Added
     vchroot_prepare_write_at(dfd,rel) (resolve dirfd->guest via vchroot_fdpath,
     join, prepare_write); wired into sys_openat. Other *at() sites remain
     absolute-only (documented; openat is the dominant write path).
  H3 hardlink-break -- already correct (copy makes a fresh inode, nlink==1).
  H4 setid+xattr on copy-up -- BUG, fixed (later HARDENED, see below). First pass
     changed mode & 07777 -> mode & 0777 and added eunion_copy_xattrs(). NOTE: a
     later mutation audit (eunion-13) found the open()-mode strip is not a reliable
     defense and the xattr copy must DROP security.capability -- see "Mutation
     audit" below.
  H5 symlink copy-up -- already correct (reproduced as a symlink, target kept).

Live zero-copy boot re-verified after the hardening dylib: rc=0, ~572KB prefix.

Patchset laid out via west: eunion-1..11 in patches/homebrew/patches.yml, each on
a clean fix/e-union-N-* branch, all publication-status: blocked (local PoC under
dar-test-infra-sp5.8.4.4). `west patch verify` green (32/32).

## Hardening-2: template-protection on delete/rename/create/fd-meta -- xnu bbf6122, 167ec89

CRITICAL systemic finding: the lower template is a SHARED LIVE DIRECTORY, not a
read-only mount. A mutating syscall that reaches a lower host path does NOT EROFS
-- it silently corrupts the template for ALL prefixes. Every "the syscall will
just fail" assumption was wrong. Added the policy primitives the sites call BEFORE
the real syscall and wired them (all #ifdef EUNION; OFF byte-identical):

  .1 unlink/rmdir   -- vchroot_prepare_unlink: lower-only name -> whiteout + SKIP
        the host delete (else the template entry is physically removed); upper
        present -> PROCEED (+ post_unlink). Sites: unlinkat, rmdir.
  .2 rename dest    -- vchroot_prepare_rename_dest: copy up the dest parent (else
        the rename moves INTO the template) + whiteout a lower-only dest namesake.
        Source uses copyup_tree. Site: renameat.
  .3 create         -- vchroot_prepare_create: copy up a lower-only parent + clear
        a whiteout at the new name. Sites: mkdirat, mknod, mkfifo, symlinkat,
        openat O_CREAT (replacing the partial pre_mkdir).
  .4 fd-metadata    -- vchroot_fd_for_meta_write: copy up the fd's object and
        RE-OPEN the upper copy (the original fd still references the lower inode!),
        returning a fresh fd the site applies the op to. Sites: fchmod,
        fchmod_extended, futimes, fsetxattr, fremovexattr. Also fixed the EXIT_PATH
        escape detection in vchroot_prepare_write_fd.

## Mutation audit of the hermetic suite -- xnu de421db (eunion-13)

A test that still passes when you deliberately break the code is not a test. Ran
mutation testing (break a primitive, demand a test go RED). Found two REAL defects
in the earlier TDD work + one latent product bug:

  * H4 "setuid stripped" was TAUTOLOGICAL: open(O_CREAT, mode & 0777)+rename does
    not reproduce setid on the test FS regardless, so widening 0777->07777 left H4
    green. FIX: explicit fchmod(dfd, mode & 0777) (load-bearing, FS-independent).
  * security.capability was NOT dropped (a file capability is setuid-equivalent).
    FIX: eunion_copy_xattrs drops it. (Unprivileged harness can't set it -> the
    H4cap assertion self-skips; runs on privileged CI. Honest, not faked.)
  * CR1 was CONTAMINATED (shared a fixture with RN1) -> dedicated fixture + guard.
  * LATENT PRODUCT BUG (separate, bead .10): strncasecmp_l(EXIT_PATH, LC_C_LOCALE
    = NULL) SEGVs on glibc when a path equals EXIT_PATH. The harness now uses a
    real C locale (was non-deterministic); the real translator should too.

  CORRECTION recorded for honesty: my first mutation pass used perl '^' without
  /m, so the whiteout/opaque mutations silently did not apply and I briefly
  mis-reported those legacy tests as broken. They are fine; re-run correctly kills
  them. The audit harness bug was real, the test-tautology claim for W/O was not.

Hermetic suite now 149 assertions, deterministic, every primitive mutation-killed.

Patchset extended: eunion-12 (template-protect .1/.2/.3), eunion-13 (setid+cap
strip), eunion-14 (fd-metadata .4), each on a clean fix/e-union-N-* branch,
publication-status: blocked. `west patch verify` green. Beads .1/.2/.3/.4/.9
closed; .5/.6/.7/.8 open; .10 (NULL-locale) open.

## Live-harness pitfalls (cost real time; recorded so they don't recur)

  * PREFIX under /root fails pre-boot: the launcher stat()s the prefix BEFORE any
    setuid (darling.c checkPrefixDir), and /root is mode 0700, so a 0700 ancestor
    yields EACCES ("Cannot access ... Permission denied"). Use a world-searchable
    prefix dir (/tmp/...). NOT an E-UNION bug.
  * `out=$(timeout N darling shell ...)` HANGS past the timeout: darlingserver and
    its respawning daemons reparent to the subreaper and keep the command-
    substitution stdout pipe open, so $(...) never returns even after timeout kills
    the launcher. Redirect to a FILE instead and pkill -9 -f darlingserver/mldr
    after, then read the file. Never capture a guest boot via $(...).
  * Benign libX11.so.6 dlopen-fail spam floods launchd stderr in a headless
    container (a daemon needs X11); suppress with 2>/dev/null. Orthogonal to
    E-UNION; do not chase it.

Full 92-case checklist (rename-across-layers, hardlink-break, symlink edges,
getcwd/proc magic, etc.) captured in the research transcript; remaining items are
hardening beyond the proven core, to be added as assertions when pursued.

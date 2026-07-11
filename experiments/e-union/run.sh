#!/usr/bin/env bash
# E-UNION PoC test loop (dar-test-infra-sp5.8.4.4).
#
# Compiles the real vchroot_userspace.c in TEST mode together with runner.c
# and runs the union-resolver assertions against a freshly built two-layer
# fake guest root. No Darling runtime, no mount/fuse/userns -- pure userspace.
#
# Usage: ./run.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
XNU_SRC_ROOT="${XNU_SRC_ROOT:-/home/ilyagulya/work/darling-dev/darling/src/external/xnu}"
XNU="$XNU_SRC_ROOT/darling/src/libsystem_kernel/emulation"
SRCDIR="$XNU/src/linux_premigration"
INCDIR="$XNU/include"

if [ ! -f "$SRCDIR/vchroot_userspace.c" ]; then
	echo "missing vchroot_userspace.c under XNU_SRC_ROOT=$XNU_SRC_ROOT" >&2
	exit 1
fi

WORK="$(mktemp -d /tmp/eunion-tdd.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# shim so <darling/emulation/linux_premigration/vchroot_expand.h> resolves
mkdir -p "$WORK/shim/darling/emulation/linux_premigration"
cp "$INCDIR/linux_premigration/vchroot_expand.h" \
   "$WORK/shim/darling/emulation/linux_premigration/"

# build the two-layer fixture: prefix (upper) + libexec (lower)
mkdir -p "$WORK/prefix/usr/bin" "$WORK/prefix/var/tmp" "$WORK/prefix/usr/local"
mkdir -p "$WORK/libexec/usr/bin" "$WORK/libexec/System/Library/LaunchDaemons"
mkdir -p "$WORK/libexec/usr/lib/system" "$WORK/libexec/usr/local/share"
echo "prefix-only echo"       > "$WORK/prefix/usr/bin/myecho"
echo "libexec-only ls"        > "$WORK/libexec/usr/bin/ls"
echo "<<launchd plist>>"      > "$WORK/libexec/System/Library/LaunchDaemons/com.apple.test.plist"
echo "shared but in prefix"   > "$WORK/prefix/usr/bin/sh"
echo "shared but in libexec"  > "$WORK/libexec/usr/bin/sh"
echo "deep lower dylib"       > "$WORK/libexec/usr/lib/system/libsystem_c.dylib"
echo "lower-only conf"        > "$WORK/libexec/usr/local/share/tool.conf"
chmod 0755 "$WORK/libexec/usr/bin/ls"  # exercise mode preservation on copy-up
# large lower-only file to widen the copy-up race window (C7)
mkdir -p "$WORK/libexec/var/db"
head -c 4194304 /dev/urandom > "$WORK/libexec/var/db/bigfile"
# opaque-dir fixture: dir in both layers, lower-only child + upper-only child
mkdir -p "$WORK/libexec/opaquedir" "$WORK/prefix/opaquedir"
echo "from lower" > "$WORK/libexec/opaquedir/lowerchild"
echo "from upper" > "$WORK/prefix/opaquedir/upperchild"
# readdir-merge fixture: dir in both layers, overlapping + distinct entries
mkdir -p "$WORK/libexec/mergedir" "$WORK/prefix/mergedir"
echo u > "$WORK/prefix/mergedir/up_only"
echo l > "$WORK/libexec/mergedir/low_only"
echo l2 > "$WORK/libexec/mergedir/low2"
echo bu > "$WORK/prefix/mergedir/both"
echo bl > "$WORK/libexec/mergedir/both"
# big-merge fixture: many entries spread across both layers so the paged
# getdents merge is forced over MANY pages (small page buffer in the test).
mkdir -p "$WORK/libexec/bigmerge" "$WORK/prefix/bigmerge"
for i in $(seq 0 39); do echo x > "$WORK/libexec/bigmerge/low_$i"; done
for i in $(seq 0 39); do echo y > "$WORK/prefix/bigmerge/up_$i"; done
# 10 names present in BOTH layers (upper must win, no dup across pages)
for i in $(seq 0 9); do echo bl > "$WORK/libexec/bigmerge/dup_$i"; echo bu > "$WORK/prefix/bigmerge/dup_$i"; done

# --- hardening fixtures (dyra #1..#5) ---
# #4 setid + xattr: a lower-only setuid file carrying a user.* xattr. copy-up
#    must STRIP setid (never leave a setuid copy) and PRESERVE xattrs.
#    security.capability needs privilege to set, so a user.* xattr is the
#    portable proxy for "xattrs are carried over".
echo "setid payload" > "$WORK/libexec/usr/bin/suid_tool"
chmod 4755 "$WORK/libexec/usr/bin/suid_tool"   # setuid + 0755
setfattr -n user.test.tag -v hello "$WORK/libexec/usr/bin/suid_tool" 2>/dev/null \
  || attr -s test.tag -V hello "$WORK/libexec/usr/bin/suid_tool" >/dev/null 2>&1 || true
# A file capability is setuid-equivalent: copy-up MUST drop security.capability.
# setcap needs CAP_SETFCAP, but a raw setxattr of the security.capability name is
# permitted on a plain file on most kernels (no priv required to WRITE the name,
# only the kernel's interpretation needs priv). Set a syntactically valid v2 cap
# blob so the fixture exercises the eunion_copy_xattrs drop. Best-effort: if the
# FS/kernel refuses, the H4cap assertion self-skips (checks the lower has it first).
python3 - "$WORK/libexec/usr/bin/suid_tool" <<'PY' 2>/dev/null || true
import os, struct, sys
# VFS_CAP_REVISION_2 (0x02000000) + effective bit, all-zero permitted/inheritable
blob = struct.pack('<IIIII', 0x02000000, 0, 0, 0, 0)
try:
    os.setxattr(sys.argv[1], b'security.capability', blob)
except OSError:
    pass
PY
# #3 hardlink-break: a lower-only file with TWO links. copy-up must break the
#    link (upper copy st_nlink == 1), never share an inode with the template.
echo "linked content" > "$WORK/libexec/usr/lib/hl_a"
ln "$WORK/libexec/usr/lib/hl_a" "$WORK/libexec/usr/lib/hl_b"   # nlink == 2 in lower
# #1 rename of a lower-only DIRECTORY with contents: every descendant must
#    survive the rename into the upper layer (not be left behind in the template).
mkdir -p "$WORK/libexec/rendir/sub"
echo r1 > "$WORK/libexec/rendir/f1"
echo r2 > "$WORK/libexec/rendir/sub/f2"
# #5 symlink edge: a lower-only symlink pointing at another lower-only file.
ln -s target_file "$WORK/libexec/usr/lib/lnk"
echo "the target" > "$WORK/libexec/usr/lib/target_file"
# #1 unlink policy (U-tests): dedicated UNTOUCHED fixtures so the prepare_unlink
#    assertions are not contaminated by earlier copy-up/whiteout tests.
mkdir -p "$WORK/libexec/var/log" "$WORK/prefix/var/tmp"
echo "del me lower" > "$WORK/libexec/var/log/ulnk_lower"     # lower-only victim
echo "del me upper" > "$WORK/prefix/var/tmp/ulnk_upper"      # upper-only victim
mkdir -p "$WORK/libexec/var/empty_lowerdir"                   # lower-only empty dir
# #2 rename-dest: a lower-only victim the rename destination will overwrite.
echo "rename victim" > "$WORK/libexec/var/log/ren_victim"
# #4 fd-metadata: a lower-only file opened O_RDONLY then chmod'd via fd.
echo "fd meta victim" > "$WORK/libexec/var/log/fdmeta_lower"
# #4 fd-metadata END-TO-END (FD2): a DEDICATED lower-only file with a known mode
#    (0644). The high-level helper must copy it up and return a fresh fd on the
#    UPPER copy; a chmod via that fd must change the UPPER mode and leave the
#    TEMPLATE's 0644 untouched. Pristine so no other test perturbs the mode.
echo "fd meta e2e" > "$WORK/libexec/var/log/fdmeta_e2e"
chmod 0644 "$WORK/libexec/var/log/fdmeta_e2e"
# FD3: an UPPER-only file -- the helper must return the SAME fd (no copy-up) and
#    a metadata op via it works directly.
mkdir -p "$WORK/prefix/var/tmp"
echo "fd meta upper" > "$WORK/prefix/var/tmp/fdmeta_upper"
chmod 0644 "$WORK/prefix/var/tmp/fdmeta_upper"
# #3 create-parent: a DEDICATED lower-only parent dir for CR1, so the assertion
#    "parent dir materialized in upper" is caused by prepare_create's copy-up and
#    NOT by an earlier test (RN1) that copies up a shared fixture. Must be a dir
#    that no other test touches.
mkdir -p "$WORK/libexec/var/createparent"
# whiteout #9: DEDICATED pristine lower-only files for the whiteout assertions, so
#    a whiteout no-op is caught (the earlier W1/W2/W4 reused /usr/bin/{ls,sh} which
#    the copy-up tests had already mutated, so expect_absent passed for the wrong
#    reason even with whiteout disabled). These are lower-only and touched ONLY by
#    the whiteout tests.
echo "wh lower victim" > "$WORK/libexec/var/log/wh_lower"          # lower-only
mkdir -p "$WORK/libexec/var/whboth" "$WORK/prefix/var/whboth"
echo "wh both lower" > "$WORK/libexec/var/whboth/file"            # present in BOTH
echo "wh both upper" > "$WORK/prefix/var/whboth/file"            #   -> upper copy
# opaque #9: a DEDICATED dir present in both layers with a lower-only child and an
#    upper-only child, touched ONLY by the opaque tests (the earlier O1 reused
#    /opaquedir which other tests mutate).
mkdir -p "$WORK/libexec/var/opq" "$WORK/prefix/var/opq"
echo "opq lower child" > "$WORK/libexec/var/opq/lo_child"        # lower-only
echo "opq upper child" > "$WORK/prefix/var/opq/up_child"         # upper-only
# .11 ftruncate fd copy-up (FT): a DEDICATED pristine lower-only NON-EMPTY file.
#    ftruncate via a lower fd must copy up + truncate the UPPER copy, leaving the
#    template bytes intact. Touched ONLY by the FT test so "template untouched" can
#    only hold if sys_ftruncate routed through vchroot_fd_for_meta_write.
echo "0123456789abcdef this content must survive in the template" > "$WORK/libexec/var/log/ftrunc_lower"
# .5 xattr marker isolation (XS1): a DEDICATED lower-only file the path-based
#    setxattr copy-up test targets, touched ONLY by XS1 so "materialized in upper"
#    can only be caused by XS1's own prepare_write (not an earlier copy-up).
echo "xattr victim" > "$WORK/libexec/var/log/xattr_lower"
# .6 mkdir-opaque delete-recreate (MK1): a DEDICATED lower-only POPULATED dir,
#    present ONLY in the lower layer and touched ONLY by the MK test. After
#    rmdir+mkdir of this name the merged view must be EMPTY (the stale children
#    must not resurrect) -- which can only hold if post_mkdir marked it opaque.
mkdir -p "$WORK/libexec/var/recreate"
echo "stale a" > "$WORK/libexec/var/recreate/stale_a"
echo "stale b" > "$WORK/libexec/var/recreate/stale_b"
# .7c dirent d_type/d_ino (G4): a dir containing a known SUBDIR and a known FILE
#    so the paged merge can be checked for a valid d_type (DT_DIR/DT_REG) and a
#    real d_ino instead of the hardcoded DT_UNKNOWN/1.
mkdir -p "$WORK/libexec/var/dtcheck/a_subdir"
echo "a regular file" > "$WORK/libexec/var/dtcheck/a_file"
# .7b copy-up metadata (CM): a DEDICATED lower-only file stamped with a known
#    distinctly-OLD mtime so copy-up mtime-preservation is checkable (an unfixed
#    copy resets mtime to ~now). Touched ONLY by the CM test.
echo "preserve my mtime" > "$WORK/libexec/var/log/cm_mtime"
touch -d "2001-01-01 00:00:00" "$WORK/libexec/var/log/cm_mtime"
# .7a per-component two-layer lookup (PC): a lower-only intermediate dir /var/pc
#    with a lower-only child (lowerleaf), AND an UPPER-only leaf /var/pc/leaf.
#    Resolving /var/pc/leaf must find the UPPER copy even though the parent
#    committed the walk to the lower root. The upper dir /var/pc must NOT exist on
#    disk yet (lower-only intermediate) -- the upper /var/pc/leaf is materialized
#    by mkdir -p of its parent only in the prefix; to keep /var/pc lower-only on
#    the prefix side we DO need the parent dir to exist in the upper to hold the
#    leaf. NOTE: a real union would have copied up the parent on leaf-create; the
#    point of the test is that the resolver must still check upper for the leaf.
#    We therefore create the upper parent+leaf but the test asserts the parent is
#    lower-only via the LOWER layer (the resolver commits to lower at the parent).
mkdir -p "$WORK/libexec/var/pc"
echo "lower child"  > "$WORK/libexec/var/pc/lowerleaf"
mkdir -p "$WORK/prefix/var/pc"
echo "upper leaf"   > "$WORK/prefix/var/pc/leaf"

# SP1 symlink-parent create (bug: brew install `touch /tmp/x` fails). A lower-only
#    directory /var/sp_real and a lower-only SYMLINK /var/sp_link -> sp_real (a
#    relative target, exactly like the real /tmp -> private/tmp). Creating a NEW
#    file through the symlinked parent (vchroot_prepare_create("/var/sp_link/new"))
#    must materialize the symlink's REAL TARGET DIR (/var/sp_real) in the upper
#    layer -- not just copy the symlink up pointing at a non-materialized target,
#    which leaves a later open(O_CREAT) with ENOENT. Touched ONLY by the SP test.
mkdir -p "$WORK/libexec/var/sp_real"
ln -s sp_real "$WORK/libexec/var/sp_link"

sources=("$HERE/runner.c")
include_dirs=("-I$SRCDIR" "-I$WORK/shim")

# The resolver extraction comes after the historical E-UNION patch under test.
# Keep the source-base RED arm on its old production closure, but link the
# extracted resolver whenever the selected GREEN source tree provides it.
if [ -f "$SRCDIR/eunion_resolver.c" ]; then
	sources+=("$SRCDIR/eunion_resolver.c")
	include_dirs+=("-I$INCDIR")
fi

gcc -Wall -Wno-format-truncation -Wno-unused-function \
    -DEUNION \
    -DEUNION_LIBEXEC_PATH="\"$WORK/libexec\"" \
    "${include_dirs[@]}" \
    -o "$WORK/runner" "${sources[@]}"

cd "$WORK"
./runner

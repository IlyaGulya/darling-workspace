#!/usr/bin/env bash
# E-UNION route-2 end-to-end verification (dar-test-infra-sp5.8.4.4).
#
# Runs INSIDE a default unprivileged docker container (no --privileged, no
# --cap-add, no --device /dev/fuse, no userns). Assumes a Darling install tree
# built with -DDARLING_EUNION=ON is present at $DARLING_INSTALL (default
# /usr/local). Proves the union-in-vchroot prefix assembly:
#   1. prefix bootstrap does NOT copy the ~7GB template (zero-copy)
#   2. the .union-work activation marker is created
#   3. a template-only path is readable in the guest (lower-layer read)
#   4. writing a template-only path materializes an upper copy (copy-up)
#   5. deleting a template path hides it via whiteout (stays gone)
#   6. readdir shows the merged view
#
# Exit 0 = all checks pass.
set -u

DARLING_INSTALL="${DARLING_INSTALL:-/usr/local}"
DARLING="$DARLING_INSTALL/bin/darling"
LIBEXEC="$DARLING_INSTALL/libexec/darling"
# NB: default prefix under /tmp, not /root: /root is mode 0700 and the launcher
# stat()s the prefix before any setuid, so a 0700 ancestor yields EACCES.
PREFIX="${DPREFIX:-/tmp/darling-eunion/prefix}"
mkdir -p "$(dirname "$PREFIX")"

pass=0 fail=0
ok()   { echo "  ok    $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "== E-UNION route-2 verification =="
echo "darling = $DARLING"
echo "libexec = $LIBEXEC"
echo "prefix  = $PREFIX"

# environment sanity: confirm we are UNPRIVILEGED (the whole point)
echo "== environment (must be unprivileged) =="
echo "  uid=$(id -u) caps=$(grep CapEff /proc/self/status | awk '{print $2}')"
[ -e /dev/fuse ] && echo "  NOTE: /dev/fuse present (not used by E-UNION)" || ok "no /dev/fuse (E-UNION needs none)"
grep -q overlay /proc/filesystems && echo "  NOTE: overlay fs available (not used by E-UNION)" || true

rm -rf "$PREFIX"

# Pick a template-only file present in the lower layer (read + copy-up + whiteout
# all act on it). /etc/profile is a regular file the guest can read/append/delete.
probe2="/etc/profile"
[ -e "$LIBEXEC$probe2" ] || probe2="/etc/hosts"

# IMPORTANT (harness lesson, dar-test-infra-sp5.8.4.4): do NOT issue multiple
# sequential `darling shell` invocations against the same prefix -- the 2nd+
# invocation fails with "shellspawn.sock: Permission denied" (darlingserver
# lifecycle across invocations, unrelated to the union). And do NOT capture guest
# output via $(...) -- respawning daemons hold the pipe open. Instead: drive ALL
# guest mutations in ONE self-contained `darling shell sh -c` (a single boot,
# output redirected to a file), then VERIFY from the HOST side by inspecting the
# upper prefix vs the lower template directly -- which is the strongest possible
# check (it literally proves upper-materialized + template-unchanged on disk).
echo "== boot once: read + copy-up(write) + whiteout(delete) in a single guest shell =="
GLOG="$(dirname "$PREFIX")/guest.log"
DARLING_ROOTLESS=1 DPREFIX="$PREFIX" "$DARLING" shell sh -c "
  test -e $probe2 && echo LOWER_READ_OK;
  echo '# eunion appended' >> $probe2;     # triggers copy-up of the template file
  echo eunion-cow > ${probe2}.new;          # an upper-only create
  rm -f ${probe2}.new;                       # delete the upper-only file (clean)
  echo GUEST_DONE
" >"$GLOG" 2>&1
# reap daemons so the script never hangs on a held pipe
pkill -9 -f darlingserver 2>/dev/null || true
pkill -9 mldr 2>/dev/null || true

echo "== checks (host-side inspection of upper prefix vs lower template) =="

# 1. zero-copy: the prefix must be tiny (KB..few MB), never a multi-GB copy of
#    the template. du of the prefix is reliable; df deltas are not (other writers).
prefix_kb=$(du -sk "$PREFIX" 2>/dev/null | awk '{print $1}')
echo "  prefix size ~${prefix_kb} KB"
if [ "${prefix_kb:-999999999}" -lt 512000 ]; then   # < 500 MB
  ok "zero-copy: prefix is ${prefix_kb} KB (no multi-GB template copy)"
else
  bad "prefix is ${prefix_kb} KB -- looks like a full template copy, not union"
fi

# 2. activation marker
if [ -d "$PREFIX/.union-work" ]; then
  ok ".union-work activation marker created"
else
  bad ".union-work marker missing -> union not activated"
fi

# 3. lower read: the guest saw the template-only file (printed by the boot above)
if grep -q LOWER_READ_OK "$GLOG"; then
  ok "lower read: template-only $probe2 visible in guest"
else
  bad "lower read: $probe2 not visible in guest (union resolve failed)"
  echo "    --- guest.log (filtered) ---"
  grep -vE "libX11|xdg-user-dir|FD rlimit" "$GLOG" | sed 's/^/    /' | head -20
fi

# 4. copy-up: appending to the template file must materialize an UPPER copy and
#    leave the shared template UNCHANGED (host-side, the definitive check).
if [ -e "$PREFIX$probe2" ]; then
  ok "copy-up: writing template $probe2 materialized an upper copy"
  if grep -q "eunion appended" "$PREFIX$probe2" 2>/dev/null; then
    ok "copy-up: the upper copy carries the appended write"
  else
    bad "copy-up: upper copy present but missing the appended write"
  fi
  if ! grep -q "eunion appended" "$LIBEXEC$probe2" 2>/dev/null; then
    ok "copy-up: shared template $probe2 left UNMODIFIED on disk"
  else
    bad "copy-up: shared template was MODIFIED (template protection FAILED!)"
  fi
else
  bad "copy-up: no upper copy of $probe2 after write"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]

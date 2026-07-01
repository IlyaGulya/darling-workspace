#!/usr/bin/env bash
# Isolate the /tmp create failure: is it the symlink (/tmp -> private/tmp),
# lower-only-parent copy-up, or something else? Single boot, non-root.
set -u
export HOME=/tmp/h; mkdir -p "$HOME" /tmp/dp 2>/dev/null
PREFIX=/tmp/dp/.darling-eunion; rm -rf "$PREFIX" 2>/dev/null
OUT=/tmp/probe.out
timeout 200 env DARLING_ROOTLESS=1 DPREFIX="$PREFIX" HOME="$HOME" \
  /usr/local/bin/darling shell /bin/bash --login -c '
    echo "GUEST uid=$(id -u)"
    echo "--- what is /tmp ---"; ls -ld /tmp /private/tmp 2>&1
    echo "--- readlink /tmp ---"; readlink /tmp 2>&1
    echo "--- create via /tmp (symlink) ---"
    ( echo hi > /tmp/viatmp ) 2>&1 && echo TMP_VIA_SYMLINK_OK || echo TMP_VIA_SYMLINK_FAIL
    echo "--- create via /private/tmp (real path) ---"
    ( echo hi > /private/tmp/viaprivate ) 2>&1 && echo PRIVATE_TMP_OK || echo PRIVATE_TMP_FAIL
    echo "--- create in /var/tmp (different lower-only dir) ---"
    ( echo hi > /var/tmp/vartmptest ) 2>&1 && echo VAR_TMP_OK || echo VAR_TMP_FAIL
    echo "--- create in /Users (writable set) ---"
    ( echo hi > /Users/utest ) 2>&1 && echo USERS_OK || echo USERS_FAIL
    echo "--- mkdir then create under /tmp ---"
    mkdir -p /tmp/sub 2>&1 && echo MKDIR_TMP_OK || echo MKDIR_TMP_FAIL
    ( echo hi > /tmp/sub/f ) 2>&1 && echo TMP_SUB_OK || echo TMP_SUB_FAIL
    echo "--- where did upper materialize? (host-visible) ---"
    echo GUEST_END
  ' >"$OUT" 2>&1 &
LPID=$!
for i in $(seq 1 200); do grep -q GUEST_END "$OUT" 2>/dev/null && break; kill -0 $LPID 2>/dev/null || break; sleep 1; done
echo "#### PROBE ####"; cat "$OUT"; echo "#### /PROBE ####"
echo "--- upper prefix contents (host side) ---"
find "$PREFIX/private/tmp" "$PREFIX/var/tmp" "$PREFIX/Users" -maxdepth 1 2>/dev/null | head -20
pkill -9 -f darlingserver 2>/dev/null; pkill -9 mldr 2>/dev/null; echo DONE

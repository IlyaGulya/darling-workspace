#!/usr/bin/env bash
# Probe the specific gaps the Homebrew installer hit: missing /usr/bin/stat,
# /tmp touch (lower-only dir copy-up), and CLT detection. Single boot, non-root.
set -u
export HOME=/tmp/h; mkdir -p "$HOME" /tmp/dp 2>/dev/null
PREFIX=/tmp/dp/.darling-eunion; rm -rf "$PREFIX" 2>/dev/null
OUT=/tmp/probe.out
timeout 200 env DARLING_ROOTLESS=1 DPREFIX="$PREFIX" HOME="$HOME" \
  /usr/local/bin/darling shell /bin/bash --login -c '
    echo "GUEST uid=$(id -u)"
    echo "--- stat binary ---"; command -v stat || echo "stat ABSENT"; ls -la /usr/bin/stat 2>&1 | head -1
    echo "--- /tmp touch (lower-only dir, needs copy-up) ---"
    touch /tmp/probe_touch_test 2>&1 && echo "TOUCH_TMP_OK" || echo "TOUCH_TMP_FAIL"
    ls -la /tmp/probe_touch_test 2>&1 | head -1
    echo "--- touch the exact installer path ---"
    touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress 2>&1 && echo "TOUCH_CLT_OK" || echo "TOUCH_CLT_FAIL"
    echo "--- CLT presence (what Homebrew looks for) ---"
    ls -ld /Library/Developer/CommandLineTools 2>&1 | head -1
    /usr/bin/xcode-select -p 2>&1 | head -1
    echo "--- pkgutil CLT receipt (Homebrew uses this) ---"
    /usr/sbin/pkgutil --pkg-info=com.apple.pkg.CLTools_Executables 2>&1 | head -2
    echo GUEST_END
  ' >"$OUT" 2>&1 &
LPID=$!
for i in $(seq 1 200); do grep -q GUEST_END "$OUT" 2>/dev/null && break; kill -0 $LPID 2>/dev/null || break; sleep 1; done
echo "#### PROBE ####"; cat "$OUT"; echo "#### /PROBE ####"
pkill -9 -f darlingserver 2>/dev/null; pkill -9 mldr 2>/dev/null; echo DONE

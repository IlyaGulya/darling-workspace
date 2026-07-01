#!/usr/bin/env bash
# Minimal probe: does `test -f` on a NON-EXISTENT guest path wrongly return true
# under EUNION? Single self-contained darling shell invocation (pitfall #5).
set -u
PREFIX=/tmp/dp/.darling-eunion
mkdir -p /tmp/dp; rm -rf "$PREFIX"
OUT=/tmp/probe.out

timeout 180 env DARLING_ROOTLESS=1 DPREFIX="$PREFIX" \
  /usr/local/bin/darling shell /bin/bash --login -c '
    echo GUEST_BOOT_OK
    probe() {  # path  expect(0=exists,1=absent)
      if [ -f "$1" ]; then r=0; else r=1; fi
      echo "TEST -f $1 -> $r (expect $2)"
    }
    probed() {
      if [ -d "$1" ]; then r=0; else r=1; fi
      echo "TEST -d $1 -> $r (expect $2)"
    }
    probee() {
      if [ -e "$1" ]; then r=0; else r=1; fi
      echo "TEST -e $1 -> $r (expect $2)"
    }
    echo "--- the installer predicate ---"
    probe /etc/homebrew/brew.no_install 1
    probed /etc/homebrew 1
    probee /etc/homebrew/brew.no_install 1
    echo "--- controls: nonexistent under existent dir ---"
    probe /etc/NOPE_does_not_exist 1
    probe /etc/homebrew_NOPE/x 1
    echo "--- controls: real files (should exist) ---"
    probe /etc/hosts 0
    probed /etc 0
    probed /System/Library/LaunchDaemons 0
    echo "--- raw stat + ls for the smoking gun ---"
    ls -la /etc/homebrew 2>&1 | head -3
    stat /etc/homebrew/brew.no_install 2>&1 | head -3
    echo "--- which etc is this ---"
    ls -ld /etc /private/etc 2>&1
    echo GUEST_END
  ' >"$OUT" 2>&1 &
LPID=$!
for i in $(seq 1 180); do grep -q GUEST_END "$OUT" 2>/dev/null && break; kill -0 $LPID 2>/dev/null || break; sleep 1; done
echo "######## PROBE OUTPUT ########"; cat "$OUT"; echo "######## /PROBE ########"
pkill -9 -f darlingserver 2>/dev/null; pkill -9 mldr 2>/dev/null
echo DONE

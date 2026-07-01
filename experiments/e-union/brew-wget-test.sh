#!/usr/bin/env bash
# E-UNION end-to-end: install wget via Homebrew inside a fully-fixed Darling guest,
# DEFAULT unprivileged docker, WITH network, as a NON-ROOT user (Homebrew refuses
# root: "Don't run this as root!"). The guest uid == container uid (darling.c
# pushes g_originalUid to shellspawn), so run the container --user 1000.
#
# Pitfall #5: 2nd+ darling shell on the same prefix => shellspawn.sock perm-denied.
# So drive the WHOLE flow in ONE darling shell invocation.
set -u
export HOME=/tmp/h
mkdir -p "$HOME" /tmp/dp 2>/dev/null
PREFIX=/tmp/dp/.darling-eunion
rm -rf "$PREFIX" 2>/dev/null

echo "== env: uid=$(id -u) HOME=$HOME =="
getent hosts github.com >/dev/null 2>&1 && echo "DNS ok" || echo "DNS FAIL"

OUT=/tmp/brew.out
timeout 3000 env DARLING_ROOTLESS=1 DPREFIX="$PREFIX" HOME="$HOME" \
  /usr/local/bin/darling shell /bin/bash --login -c '
    echo "GUEST_BOOT_OK uid=$(id -u)"; uname -sm
    echo "--- tools ---"; for t in git curl ruby bash make clang; do printf "%s=" "$t"; command -v "$t" || echo MISSING; done

    export NONINTERACTIVE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_AUTO_UPDATE=1

    echo "=== STEP: install Homebrew ==="
    if /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
      echo BREW_INSTALL_DONE
    else
      echo "BREW_INSTALL_FAIL rc=$?"
    fi

    echo "=== STEP: locate brew ==="
    BREW=""
    for p in /usr/local/bin/brew /opt/homebrew/bin/brew "$HOME/.linuxbrew/bin/brew" /home/linuxbrew/.linuxbrew/bin/brew; do
      [ -x "$p" ] && { BREW="$p"; break; }
    done
    echo "BREW_AT=${BREW:-NONE}"
    [ -n "$BREW" ] && eval "$("$BREW" shellenv)" 2>/dev/null
    [ -n "$BREW" ] && { echo "=== brew --version ==="; brew --version; }

    echo "=== STEP: brew install wget ==="
    if [ -n "$BREW" ]; then brew install wget && echo BREW_WGET_DONE || echo "BREW_WGET_FAIL rc=$?"; fi

    echo "=== STEP: verify wget ==="
    if command -v wget >/dev/null 2>&1; then wget --version 2>&1 | head -3; echo WGET_RUNS_OK; else echo WGET_MISSING; fi
    echo GUEST_END
  ' >"$OUT" 2>&1 &
LPID=$!
for i in $(seq 1 3000); do
  grep -qE 'GUEST_END|WGET_RUNS_OK|WGET_MISSING|BREW_WGET_FAIL|BREW_INSTALL_FAIL' "$OUT" 2>/dev/null && break
  kill -0 $LPID 2>/dev/null || break
  sleep 1
done
echo "########## GUEST OUTPUT (tail 120) ##########"
tail -120 "$OUT"
echo "########## /GUEST OUTPUT ##########"
pkill -9 -f darlingserver 2>/dev/null; pkill -9 mldr 2>/dev/null
echo "== DONE =="

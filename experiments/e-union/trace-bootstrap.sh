#!/usr/bin/env bash
# Trace the launchctl-bootstrap Mach-reply stall on the EUNION zero-copy prefix.
# Default unprivileged docker. Prefix under /tmp (world-searchable; /root is 700).
# Captures server debug log + strace -f of the guest tree.
set -u
DARLING="/usr/local/bin/darling"
PREFIX="/tmp/dp/.darling-eunion"
OUT="/trace"
mkdir -p "$OUT" /tmp/dp
rm -rf "$PREFIX"

echo "== environment: uid=$(id -u) caps=$(grep CapEff /proc/self/status | awk '{print $2}') =="

# Server debug log to a file via DSERVER_LOG_STDERR + DEBUG level.
# strace follows the whole tree; record syscalls relevant to Mach RPC + blocking.
timeout 40 strace -f -tt -T -s 120 \
  -e trace=openat,getdents64,getdents,connect,clone,execve,exit_group \
  -o "$OUT/strace.log" \
  env DARLING_ROOTLESS=1 DPREFIX="$PREFIX" \
      "$DARLING" shell echo HELLO_EUNION \
  > "$OUT/boot.stdout" 2> "$OUT/boot.stderr"
echo "  boot exit: $?"

echo "== boot.stdout =="
cat "$OUT/boot.stdout"
echo "== boot.stderr (server log + launcher), last 60 =="
tail -60 "$OUT/boot.stderr"

echo "== execve summary =="
grep -E 'execve\("' "$OUT/strace.log" | sed -E 's/^([0-9]+).*execve\("([^"]+)", \[([^]]*)\].*/\1 \2 :: \3/' | head -40

echo "== unfinished (blocked) syscalls at end, per pid =="
grep -E '<unfinished|epoll_wait|ppoll' "$OUT/strace.log" | tail -25

echo "== last 25 strace lines =="
tail -25 "$OUT/strace.log"
echo "== strace.log: $(wc -l < "$OUT/strace.log") lines =="

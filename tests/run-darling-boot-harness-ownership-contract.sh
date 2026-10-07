#!/usr/bin/env bash
# Host contract for the ownership rule in scripts/darling-boot-run.sh -- the rule that decides whether the next
# boot starts clean, and the one whose blind spot cost a whole diagnostic cycle.
#
# MEASURED DEFECT this pins. A run killed from outside leaves a daemonized darlingserver behind: ppid=1, argv
# "darlingserver 4 3 <prefix-basename> 6 1000 1000 8 0" (the prefix as a directory CAPABILITY, never as a path),
# one process per line on the console of the next run. The harness's ownership rule matched only (a) ancestors of
# itself, (b) /proc/<pid>/exe under the prefix and (c) a cmdline containing the prefix PATH -- so all three arms
# missed it, the check printed "clean: 0 prefix-owned processes", and the next boot met the stale
# .darlingserver.sock / .init.pid of a server that no longer existed and never reached shellspawn readiness.
#
# The arms the contract pins:
#   capability  the runtime's retained prefix DIRECTORY descriptor, compared by device:inode. This is the
#               framework's own rule (west_commands/test_prefix.py::_runtime_retains_prefix); without it a
#               process that scrubs DARLING_* and carries no path anywhere is invisible.
#   initpid     the pid named by the prefix's own .init.pid.
#   stale files .init.pid naming a dead pid, and idle .darlingserver*.sock files, are REPORTED by the
#               diagnostic mode and CLEARED by a real run before it boots.
#
# No prefix is booted: synthetic processes carry exactly the argv and descriptor shapes the real ones do, and the
# diagnostic mode (--list-owned) is the observable surface. A rule that can only be exercised by booting cannot be
# tested, which is why that mode exists.
set -uo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
harness="$workspace_root/scripts/darling-boot-run.sh"
[ -x "$harness" ] || { echo "contract: no harness at $harness" >&2; exit 2; }

tmp="$(mktemp -d)"
prefix="$tmp/prefix-own"
mkdir -p "$prefix/bin"
printf '#!/bin/sh\nexit 0\n' > "$prefix/bin/darling"
chmod 755 "$prefix/bin/darling"
# The fake server must ignore its numeric argv, because the real one's argv carries the prefix as a CAPABILITY
# ("darlingserver 4 3 <basename> 6 ..."). Two measured properties are deliberately kept: the stub is compiled (a
# renamed `sleep` would reject the numeric arguments on the first line) and its NAME IS LONGER THAN 15 CHARACTERS,
# so /proc/<pid>/comm is truncated and only the exe-basename arm can pass the ownership gate. That truncation is the
# defect the first version of this arm had.
printf '#include <unistd.h>\nint main(int argc, char **argv) { (void)argc; (void)argv; for (;;) pause(); }\n' \
	> "$tmp/darlingserver-stub-long.c"
cc -o "$tmp/darlingserver-stub-long" "$tmp/darlingserver-stub-long.c" 2>/dev/null || {
	echo "contract: no C compiler to build the stub server" >&2
	exit 2
}

pids=()
cleanup() {
	for p in "${pids[@]:-}"; do kill -9 "$p" 2>/dev/null; done
	rm -rf "$tmp"
}
trap cleanup EXIT

fail=0
check() { # label expected actual
	if [ "$2" = "$3" ]; then
		echo "ok   $1"
	else
		echo "FAIL $1: expected [$2] got [$3]" >&2
		fail=1
	fi
}
contains() { case "$1" in *"$2"*) echo yes ;; *) echo no ;; esac; }

# 1. The daemonized server: comm=darlingserver, argv shape as measured, and the prefix held as an open directory
#    descriptor with no DARLING_* variable and no prefix path in argv.
python3 -c '
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
os.set_inheritable(fd, True)
os.execv(sys.argv[2], ["darlingserver", "4", "3", os.path.basename(sys.argv[1]), "6", "1000", "1000", "8", "0"])
' "$prefix" "$tmp/darlingserver-stub-long" &
server_pid=$!
pids+=("$server_pid")
sleep 0.5

# 2. A live pid recorded by the prefix itself, and an unrelated process that must never be reported.
sleep 30 & init_pid=$!
pids+=("$init_pid")
sleep 30 & unrelated_pid=$!
pids+=("$unrelated_pid")
printf '%s\n' "$init_pid" > "$prefix/.init.pid"
sleep 0.3

out="$tmp/list-owned.txt"
"$harness" --prefix "$prefix" --list-owned > "$out" 2>&1
rc=$?
check "diagnostic mode exits 0" 0 "$rc"
check "diagnostic mode does not boot" no "$(contains "$(cat "$out")" "== run ==")"

listed="$(grep -E '^[0-9]+$' "$out" | tr '\n' ' ')"
check "capability arm reports the daemonized server" yes "$(contains " $listed " " $server_pid ")"
check "initpid arm reports the prefix's own init pid" yes "$(contains " $listed " " $init_pid ")"
check "unrelated process is not prefix-owned" no "$(contains " $listed " " $unrelated_pid ")"
check "capability arm is named in the witness" yes "$(contains "$(cat "$out")" "arm=capability")"
check "initpid arm is named in the witness" yes "$(contains "$(cat "$out")" "arm=initpid")"

# 3. Stale runtime files: .init.pid naming a dead pid, and an idle socket. Both are reported, and neither hides
#    behind a live owner.
kill -9 "$server_pid" 2>/dev/null
printf '999999\n' > "$prefix/.init.pid"
for sock in .darlingserver.sock .darlingserver.stat.sock; do
	python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.close()' "$prefix/$sock"
done
out2="$tmp/list-owned-stale.txt"
"$harness" --prefix "$prefix" --list-owned > "$out2" 2>&1
stale="$(grep -c '^STALE-RUNTIME-FILE' "$out2")"
check "stale .init.pid and both idle sockets are reported" 3 "$stale"
check "stale .init.pid names the dead pid" yes "$(contains "$(cat "$out2")" "$prefix/.init.pid pid=999999 dead")"
check "stale server socket is reported" yes "$(contains "$(cat "$out2")" "$prefix/.darlingserver.sock")"

if [ "$fail" -eq 0 ]; then
	echo "DARLING-BOOT-HARNESS-OWNERSHIP PASS: capability, initpid and stale-file arms hold"
else
	echo "DARLING-BOOT-HARNESS-OWNERSHIP FAIL" >&2
fi
exit "$fail"

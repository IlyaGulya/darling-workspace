#!/bin/sh
# darling-prefix-map.sh PID -- print the prefixes a process is actually rooted in.
#
# WHY THIS EXISTS: a guest process cannot be identified by path (/proc/<pid>/exe is ENOENT and cmdline is empty),
# so a stale guest of ANOTHER prefix is invisible to every path-based check while it serves this one. Its
# /proc/<pid>/maps IS readable and names the prefix its library cache lives in, which is what this prints.
#
# Usage: darling-prefix-map.sh PID
set -u
pid="$1"
exe=$(readlink "/proc/$pid/exe" 2>/dev/null)
case "$exe" in /tmp/dr-*) echo "${exe%%/*}";; /*) echo "$exe";; esac
grep -oE '/tmp/dr-[a-zA-Z0-9._-]+' "/proc/$pid/maps" 2>/dev/null | sort -u

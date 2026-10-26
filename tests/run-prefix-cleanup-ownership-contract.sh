#!/usr/bin/env bash
# Ownership predicate for scripts/prefix-cleanup.sh.
#
# The predicate must follow executable/runtime identity only:
#   - a process whose /proc/<pid>/exe resolves under the prefix is owned;
#   - an unlinked guest/server whose /proc/<pid>/maps maps a prefix path and whose
#     comm is a runtime name is owned;
#   - a host tool that merely has --prefix <dir> in argv is NOT owned;
#   - a process belonging to another prefix is NOT owned;
#   - a fuse-overlayfs process that serves the prefix's own layer is NOT owned.
#
# Everything below is an isolated fakeprefix and a dry run. No canonical prefix
# is opened, stopped, mounted or repaired.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
pids=()
cleanup() {
	for pid in "${pids[@]}"; do
		kill "$pid" 2>/dev/null || true
		wait "$pid" 2>/dev/null || true
	done
	rm -rf -- "$tmp"
}
trap cleanup EXIT INT TERM

prefix="$tmp/prefix"
other="$tmp/other-prefix"
mkdir -p "$prefix/bin" "$prefix/lib" "$other/bin" "$other/lib"
printf 'prefix payload\n' >"$prefix/lib/mapped.bin"
printf 'other payload\n' >"$other/lib/mapped.bin"
cp /bin/sleep "$prefix/bin/owned-sleeper"
cp /bin/sleep "$other/bin/other-sleeper"

cat >"$tmp/probe.c" <<'PROBE'
#define _GNU_SOURCE
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	if (argc < 3) return 2;
	prctl(PR_SET_NAME, argv[1], 0, 0, 0);
	int fd = open(argv[2], O_RDONLY);
	struct stat st;
	if (fd < 0 || fstat(fd, &st) != 0) return 3;
	void *map = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
	if (map == MAP_FAILED) return 4;
	volatile unsigned char byte = *(volatile unsigned char *)map;
	(void)byte;
	sleep(120);
	return 0;
}
PROBE
cc -O2 -Wall -Wextra -o "$tmp/probe" "$tmp/probe.c"
cp "$tmp/probe" "$tmp/fuse-overlayfs"

"$prefix/bin/owned-sleeper" 120 &
owned=$!
pids+=("$owned")

"$tmp/probe" mldr "$prefix/lib/mapped.bin" &
guest=$!
pids+=("$guest")

"$tmp/probe" darlingserver "$prefix/lib/mapped.bin" &
server=$!
pids+=("$server")

python3 -c 'import time; time.sleep(120)' "--prefix=$prefix" &
observer=$!
pids+=("$observer")

"$tmp/probe" mldr "$other/lib/mapped.bin" &
otherguest=$!
pids+=("$otherguest")

"$tmp/fuse-overlayfs" mldr "$prefix/lib/mapped.bin" "$prefix" &
layer=$!
pids+=("$layer")

sleep 1

out="$tmp/cleanup.out"
"$repo/scripts/prefix-cleanup.sh" --prefix "$prefix" --dry-run >"$out"

has_pid() {
	grep -E -q "would stop pid=$1([[:space:]]|$)" "$out"
}
present() {
	if has_pid "$1"; then
		echo "PASS present $2 pid=$1"
	else
		echo "FAIL missing $2 pid=$1" >&2
		fail=1
	fi
}
absent() {
	if has_pid "$1"; then
		echo "FAIL unexpectedly present $2 pid=$1" >&2
		fail=1
	else
		echo "PASS absent $2 pid=$1"
	fi
}

fail=0
present "$owned" prefix-exe
present "$guest" unlinked-guest-mldr
present "$server" unlinked-server
absent "$observer" host-observer-arg
absent "$otherguest" other-prefix-guest
absent "$layer" layer-server

if [ "$fail" -ne 0 ]; then
	cat "$out" >&2
	exit 1
fi
printf 'PASS prefix-cleanup-ownership-contract\n'

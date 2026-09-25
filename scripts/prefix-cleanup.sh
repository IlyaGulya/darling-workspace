#!/bin/sh
# Prefix-scoped process cleanup for a Darling prefix.
#
# WHY THIS EXISTS: a guest process's /proc/<pid>/cmdline is the IN-GUEST path (e.g. /usr/libexec/shellspawn)
# and does NOT contain the prefix, so a cleanup that matches cmdline against the prefix never sees it. One
# measured run accumulated 893 such processes. This helper matches /proc/<pid>/exe, which always resolves
# under the prefix for a process launched from it, and falls back to cmdline for safety.
#
# Usage: prefix-cleanup.sh --prefix /absolute/path [--settle SECONDS] [--dry-run]
# Exit status is 1 if any prefix-owned process or mount remains afterwards, so it is usable as a gate.

set -u

PREFIX=""
SETTLE=3
DRY=0

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--settle) SETTLE="$2"; shift 2 ;;
		--dry-run) DRY=1; shift ;;
		-h|--help)
			sed -n '2,12p' "$0"
			exit 0
			;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

if [ -z "$PREFIX" ]; then
	echo "usage: $0 --prefix /absolute/path [--settle SECONDS] [--dry-run]" >&2
	exit 2
fi

case "$PREFIX" in
	/*) ;;
	*) echo "prefix must be an absolute path" >&2; exit 2 ;;
esac

if [ ! -d "$PREFIX" ]; then
	echo "not a directory: $PREFIX" >&2
	exit 2
fi

# Enumerate prefix-owned processes by exe first, then by cmdline. Both are needed: exe catches guest
# processes whose cmdline is an in-guest path, and cmdline catches a process whose exe is unreadable.
owned_pids() {
	for d in /proc/[0-9]*; do
		pid=${d#/proc/}
		exe=$(readlink "$d/exe" 2>/dev/null)
		case "$exe" in
			"$PREFIX"|"$PREFIX"/*) echo "$pid"; continue ;;
		esac
		cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
		case "$cmd" in
			*"$PREFIX"*) echo "$pid" ;;
		esac
	done
}

count_owned() { owned_pids | wc -l; }

echo "prefix: $PREFIX"
echo "prefix-owned processes before: $(count_owned)"

if [ "$DRY" = 1 ]; then
	owned_pids | while read -r pid; do
		echo "  would stop pid=$pid exe=$(readlink /proc/$pid/exe 2>/dev/null)"
	done
	echo "dry run: no action taken"
	exit 0
fi

# 1. Ask the runtime to shut down cleanly. A missing or already-dead server is not an error here.
if [ -x "$PREFIX/bin/darling" ]; then
	DPREFIX="$PREFIX" DARLING_PREFIX="$PREFIX" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
		"$PREFIX/bin/darling" --rootless shutdown >/dev/null 2>&1
fi
sleep "$SETTLE"

# 2. SIGKILL whatever is left. Match by exe AND cmdline; never a global pattern.
owned_pids | while read -r pid; do
	kill -9 "$pid" 2>/dev/null
done
sleep "$SETTLE"

# 3. Re-check, then escalate on the leftovers exactly once.
left=$(count_owned)
if [ "$left" -gt 0 ]; then
	echo "still present after SIGKILL: $left, retrying once"
	owned_pids | while read -r pid; do kill -9 "$pid" 2>/dev/null; done
	sleep "$SETTLE"
fi

final=$(count_owned)
mounts=$(mount 2>/dev/null | grep -c "$PREFIX")
echo "prefix-owned processes after: $final"
echo "mounts under prefix: $mounts"

if [ "$final" -gt 0 ] || [ "$mounts" -gt 0 ]; then
	echo "prefix is NOT clean" >&2
	exit 1
fi

echo "prefix is clean"
exit 0

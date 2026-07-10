#!/usr/bin/env bash
set -euo pipefail

: "${BUNDLE:?darling-debug-runner must provide BUNDLE}"
prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
if [[ -z "$prefix" ]]; then
	printf 'DARLING_PREFIX_CAPTURE_SKIPPED: prefix is unset\n' \
		>"$BUNDLE/prefix-processes.txt"
	exit 0
fi

ps_file="$BUNDLE/prefix-processes.txt"
ps -eo pid=,ppid=,pgid=,sid=,stat=,wchan=,args= >"$ps_file"
printf 'DARLING_PREFIX=%s\n' "$prefix" >"$BUNDLE/prefix-capture.txt"

# darlingserver carries its prefix as argv[1]; guest helpers are its descendants.
mapfile -t roots < <(awk -v prefix="$prefix" '
	$0 ~ /darlingserver/ && index($0, prefix) { print $1 }
' "$ps_file")
if ((${#roots[@]} == 0)); then
	printf 'DARLING_PREFIX_CAPTURE: no darlingserver root found\n' \
		>>"$BUNDLE/prefix-capture.txt"
	exit 0
fi

pending=("${roots[@]}")
seen=()
while ((${#pending[@]})); do
	pid="${pending[0]}"
	pending=("${pending[@]:1}")
	[[ " ${seen[*]} " == *" $pid "* ]] && continue
	seen+=("$pid")
	while read -r child; do
		pending+=("$child")
	done < <(awk -v parent="$pid" '$2 == parent { print $1 }' "$ps_file")
done

for pid in "${seen[@]}"; do
	dir="$BUNDLE/prefix-processes/pid-$pid"
	mkdir -p "$dir"
	ps -p "$pid" -o pid=,ppid=,pgid=,sid=,stat=,wchan=,args= >"$dir/ps.txt" 2>&1 || true
	for item in cmdline status wchan stack syscall; do
		cat "/proc/$pid/$item" >"$dir/$item.txt" 2>&1 || true
	done
done
printf 'DARLING_PREFIX_CAPTURE: roots=%s pids=%s\n' \
	"${roots[*]}" "${seen[*]}" >>"$BUNDLE/prefix-capture.txt"

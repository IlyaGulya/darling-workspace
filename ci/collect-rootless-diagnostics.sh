#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:?diagnostic output directory is required}"
prefix="${2:?the tier-owned Darling prefix is required}"

mkdir -p "$output_dir/prefix-files"

{
	printf 'captured-at-utc: '
	date -u +%Y-%m-%dT%H:%M:%SZ
	printf 'prefix: %s\n' "$prefix"
	printf 'runner: %s\n' "${RUNNER_NAME:-unknown}"
	printf '\n[ulimit]\n'
	ulimit -a || true
	printf 'open-files-soft: '
	ulimit -Sn || true
	printf 'open-files-hard: '
	ulimit -Hn || true
	if [[ -r /proc/sys/fs/nr_open ]]; then
		printf '\n[/proc/sys/fs/nr_open]\n'
		cat /proc/sys/fs/nr_open
	fi
} >"$output_dir/host-summary.txt"

if command -v ps >/dev/null 2>&1; then
	ps -eo user,pid,ppid,pgid,sid,stat,etime,args >"$output_dir/process-table.txt" || true
fi
if command -v ss >/dev/null 2>&1; then
	ss -xl >"$output_dir/unix-sockets.txt" || true
fi

{
	for proc in /proc/[0-9]*; do
		pid="${proc##*/}"
		[[ -r "$proc/environ" ]] || continue
		env_text="$(cat "$proc/environ" 2>/dev/null | tr '\0' '\n' 2>/dev/null || true)"
		prefix_value="$(printf '%s\n' "$env_text" | sed -n 's/^DARLING_PREFIX=//p' | head -n 1)"
		[[ "$prefix_value" == "$prefix" ]] || continue
		printf '[pid %s]\n' "$pid"
		printf '%s\n' "$env_text" | grep -E '^(DARLING_PREFIX|DARLING_ROOTLESS|DARLING_LAUNCHER|DARLING_SERVER)' || true
		if command -v ps >/dev/null 2>&1; then
			ps -o user,pid,ppid,pgid,sid,stat,etime,args -p "$pid" || true
		fi
		printf '\n'
	done
} >"$output_dir/prefix-processes.txt"

if [[ -d "$prefix" ]]; then
	find "$prefix/var/run" -maxdepth 3 -printf '%M %p\n' \
		>"$output_dir/prefix-runtime-tree.txt" 2>&1 || true
	find "$prefix/private/var/tmp" -maxdepth 2 -type f -printf '%p\n' \
		>"$output_dir/prefix-temp-files.txt" 2>&1 || true

	while IFS= read -r relative; do
		file="$prefix/$relative"
		[[ -f "$file" && ! -L "$file" ]] || continue
		target="$output_dir/prefix-files/$relative"
		mkdir -p "$(dirname "$target")"
		cp -- "$file" "$target"
	done <<'FILES'
.west-rootless-boot.log
.west-rootless-guest-fd.log
.west-rootless-shellspawn-fast-exit.trace
private/var/tmp/.west-rootless-boot.log
private/var/log/dserver-auxlog.txt
private/var/log/dserver-rpc-trace.log
FILES
fi

printf 'rootless diagnostics captured in %s\n' "$output_dir"

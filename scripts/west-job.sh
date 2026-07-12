#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat >&2 <<'USAGE'
usage:
  west-job.sh start --state-dir DIR -- west test ...
  west-job.sh status --state-dir DIR
  west-job.sh follow --state-dir DIR [--timeout-seconds N]
  west-job.sh wait --state-dir DIR
  west-job.sh cancel --state-dir DIR
  west-job.sh assert-no-live-west-test [--state-root DIR]

Use this only when the caller cannot keep a long west command attached.  DIR
contains command, job/command PIDs, start-times, log, and rc; it is safe to
inspect directly.
USAGE
	exit 2
}

state_dir=
follow_timeout_seconds=0

write_command_record() {
	local target="$1"
	printf '%q ' "${STATE_REST[@]}" >"$target"
	printf '\n' >>"$target"
}

registry_dir_for_state_root() {
	printf '%s/.west-job-registry\n' "$1"
}

registry_entry_name() {
	printf '%s' "$state_dir" | cksum | awk '{print $1 "-" $2}'
}

prepare_job_registry() {
	local state_root registry_dir
	state_root="$(dirname "$state_dir")"
	registry_dir="$(registry_dir_for_state_root "$state_root")"
	if [[ -L "$registry_dir" ]] || { [[ -e "$registry_dir" ]] && [[ ! -d "$registry_dir" ]]; }; then
		echo "west job registry is not a directory: $registry_dir" >&2
		exit 2
	fi
	mkdir -p -m 700 "$registry_dir"
	if [[ "$(stat -c '%u' "$registry_dir")" != "$(id -u)" ]]; then
		echo "west job registry is not owned by this user: $registry_dir" >&2
		exit 2
	fi
	REGISTRY_DIR="$registry_dir"
}

reserve_job_registry_entry() {
	local entry candidate_pid candidate_start_time
	prepare_job_registry
	exec {REGISTRY_LOCK_FD}>"$REGISTRY_DIR/.lock"
	flock "$REGISTRY_LOCK_FD"
	entry="$REGISTRY_DIR/$(registry_entry_name)"
	if ! mkdir "$entry" 2>/dev/null; then
		if [[ -d "$entry" && ! -L "$entry" && -f "$entry/state-dir" ]] &&
			[[ "$(<"$entry/state-dir")" == "$state_dir" ]] &&
			[[ ! -e "$state_dir" ]] &&
			[[ -f "$entry/pid" && -f "$entry/start-time" ]]; then
			candidate_pid="$(<"$entry/pid")"
			candidate_start_time="$(<"$entry/start-time")"
			if [[ "$candidate_pid" =~ ^[0-9]+$ && "$candidate_start_time" =~ ^[0-9]+$ ]] &&
				{ ! kill -0 "$candidate_pid" 2>/dev/null ||
				  [[ "$(pid_start_time "$candidate_pid")" != "$candidate_start_time" ]]; }; then
				rm -rf "$entry"
				mkdir "$entry"
			else
				echo "west job registry entry is still live: $entry" >&2
				exit 2
			fi
		else
			echo "west job registry entry already exists: $entry" >&2
			exit 2
		fi
	fi
	printf '%s\n' "$state_dir" >"$entry/state-dir"
	write_command_record "$entry/command"
	REGISTRY_ENTRY="$entry"
}

record_job_registry_identity() {
	printf '%s\n' "$(<"$state_dir/pid")" >"$REGISTRY_ENTRY/pid"
	printf '%s\n' "$(<"$state_dir/start-time")" >"$REGISTRY_ENTRY/start-time"
	flock -u "$REGISTRY_LOCK_FD"
	exec {REGISTRY_LOCK_FD}>&-
}

parse_state_dir() {
	while (($#)); do
		case "$1" in
			--state-dir)
				state_dir="$2"
				shift 2
				;;
			--)
				shift
				break
				;;
			*)
				usage
				;;
		esac
	done
	if [[ -z "$state_dir" ]]; then
		usage
	fi
	STATE_REST=("$@")
}

parse_follow() {
	while (($#)); do
		case "$1" in
			--state-dir)
				state_dir="$2"
				shift 2
				;;
			--timeout-seconds)
				follow_timeout_seconds="$2"
				shift 2
				;;
			*) usage ;;
		esac
	done
	if [[ -z "$state_dir" ]] || [[ ! "$follow_timeout_seconds" =~ ^[0-9]+$ ]]; then
		usage
	fi
}

parse_state_root() {
	state_root="${TMPDIR:-/tmp}"
	while (($#)); do
		case "$1" in
			--state-root)
				state_root="$2"
				shift 2
				;;
			*)
				usage
				;;
		esac
	done
	STATE_ROOT="$state_root"
}

pid_start_time() {
	local pid="$1"
	[[ -r "/proc/$pid/stat" ]] || return 1
	awk '{print $22}' "/proc/$pid/stat" 2>/dev/null
}

load_live_pid() {
	local pid start_time
	[[ -f "$state_dir/pid" && -f "$state_dir/start-time" ]] || return 1
	pid="$(<"$state_dir/pid")"
	start_time="$(<"$state_dir/start-time")"
	[[ "$pid" =~ ^[0-9]+$ ]] || return 1
	kill -0 "$pid" 2>/dev/null || return 1
	[[ "$(pid_start_time "$pid")" == "$start_time" ]]
}

load_live_command_pid() {
	local pid start_time
	[[ -f "$state_dir/command-pid" && -f "$state_dir/command-start-time" ]] || return 1
	pid="$(<"$state_dir/command-pid")"
	start_time="$(<"$state_dir/command-start-time")"
	[[ "$pid" =~ ^[0-9]+$ ]] || return 1
	kill -0 "$pid" 2>/dev/null || return 1
	[[ "$(pid_start_time "$pid")" == "$start_time" ]]
}

load_live_runner_pid() {
	local pid start_time
	[[ -f "$state_dir/runner-pid" && -f "$state_dir/runner-start-time" ]] || return 1
	pid="$(<"$state_dir/runner-pid")"
	start_time="$(<"$state_dir/runner-start-time")"
	[[ "$pid" =~ ^[0-9]+$ ]] || return 1
	kill -0 "$pid" 2>/dev/null || return 1
	[[ "$(pid_start_time "$pid")" == "$start_time" ]]
}

read_rc() {
	local rc
	[[ -f "$state_dir/rc" ]] || return 1
	rc="$(<"$state_dir/rc")"
	[[ "$rc" =~ ^[0-9]+$ ]] || return 1
	printf '%s\n' "$rc"
}

wait_for_pid_exit() {
	local pid_file="$1"
	# pidwait can report no matching PID when the process exits in the small
	# interval between the liveness check and its lookup. The recorded rc is
	# authoritative after that race.
	pidwait -F "$pid_file" >/dev/null 2>&1 || true
}

wait_for_pid_exit_or_timeout() {
	local pid_file="$1"
	local timeout_seconds="$2"
	timeout --foreground "$timeout_seconds" \
		pidwait -F "$pid_file" >/dev/null 2>&1 || true
}

cancel_grace_seconds() {
	local value="${WEST_JOB_CANCEL_GRACE_SECONDS:-30}"
	if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
		echo "WEST_JOB_CANCEL_GRACE_SECONDS must be a positive integer" >&2
		exit 2
	fi
	printf '%s\n' "$value"
}

snapshot_descendants() {
	local root_pid="$1" output="$2" current child
	: >"$output"
	local -a queue=("$root_pid")
	while ((${#queue[@]})); do
		current="${queue[0]}"
		queue=("${queue[@]:1}")
		while read -r child; do
			[[ "$child" =~ ^[0-9]+$ ]] || continue
			printf '%s %s\n' "$child" "$(pid_start_time "$child")" >>"$output"
			queue+=("$child")
		done < <(pgrep -P "$current" 2>/dev/null || true)
	done
}

signal_snapshotted_descendants() {
	local snapshot="$1" signal="$2" pid start_time
	[[ -f "$snapshot" ]] || return
	while read -r pid start_time; do
		[[ "$pid" =~ ^[0-9]+$ && "$start_time" =~ ^[0-9]+$ ]] || continue
		if kill -0 "$pid" 2>/dev/null && [[ "$(pid_start_time "$pid")" == "$start_time" ]]; then
			kill -"$signal" "$pid" 2>/dev/null || true
		fi
	done <"$snapshot"
}

start_job() {
	if [[ -e "$state_dir" ]]; then
		echo "west job state already exists: $state_dir" >&2
		exit 2
	fi
	if ((${#STATE_REST[@]} == 0)); then
		usage
	fi
	reserve_job_registry_entry
	mkdir -p "$state_dir"
	write_command_record "$state_dir/command"

	nohup setsid --wait bash -c '
		state_dir="$1"
		shift
		export WEST_JOB_ACTIVE=1
		export WEST_JOB_STATE_DIR="$state_dir"
		printf "%s\\n" "$$" >"$state_dir/runner-pid"
		awk "{print \$22}" "/proc/$$/stat" >"$state_dir/runner-start-time"
		finish() {
			local rc="$1"
			if [[ -e "$state_dir/cancel-requested" ]]; then
				rc=143
			fi
			printf "%s\\n" "$rc" >"$state_dir/rc.tmp"
			mv "$state_dir/rc.tmp" "$state_dir/rc"
			exit "$rc"
		}
		forward_cancel() {
			touch "$state_dir/cancel-requested"
			if [[ -n "${command_pid:-}" ]] && kill -0 "$command_pid" 2>/dev/null; then
				kill -INT "$command_pid" 2>/dev/null || true
				wait "$command_pid" || true
			fi
			finish 143
		}
		trap forward_cancel TERM INT HUP
		# Bash launches asynchronous commands with SIGINT ignored. Restore the
		# default disposition before exec so cooperative cancellation reaches the
		# command we are supervising.
		bash -c "trap - INT; exec \"\$@\"" bash "$@" &
		command_pid=$!
		printf "%s\\n" "$command_pid" >"$state_dir/command-pid"
		awk "{print \$22}" "/proc/$command_pid/stat" >"$state_dir/command-start-time"
		wait "$command_pid"
		finish "$?"
	' bash "$state_dir" "${STATE_REST[@]}" \
		>"$state_dir/log" 2>&1 < /dev/null &
	local pid=$!
	printf '%s\n' "$pid" >"$state_dir/pid"
	pid_start_time "$pid" >"$state_dir/start-time"
	record_job_registry_identity
	printf 'started pid=%s state=%s log=%s\n' "$pid" "$state_dir" "$state_dir/log"
}

status_job() {
	if load_live_pid; then
		printf 'running pid=%s state=%s\n' "$(<"$state_dir/pid")" "$state_dir"
		return
	fi
	local rc
	if rc="$(read_rc)"; then
		printf 'completed rc=%s state=%s\n' "$rc" "$state_dir"
		return
	fi
	echo "west job has no live process or recorded exit status: $state_dir" >&2
	exit 1
}

follow_job() {
	local started_at now next_line=1 next_heartbeat rc line_count
	started_at="$(date +%s)"
	next_heartbeat="$started_at"
	while true; do
		if [[ -f "$state_dir/log" ]]; then
			line_count="$(wc -l <"$state_dir/log")"
			if ((line_count >= next_line)); then
				sed -n "${next_line},${line_count}p" "$state_dir/log"
				next_line=$((line_count + 1))
			fi
		fi
		if rc="$(read_rc)"; then
			printf 'completed rc=%s state=%s\n' "$rc" "$state_dir"
			exit "$rc"
		fi
		if ! load_live_pid; then
			# The runner publishes rc atomically as its final action. It can exit in
			# the narrow interval between the read_rc and identity checks; give that
			# publication one bounded foreground turn before declaring corruption.
			wait_for_pid_exit_or_timeout "$state_dir/pid" 1
			if rc="$(read_rc)"; then
				printf 'completed rc=%s state=%s\n' "$rc" "$state_dir"
				exit "$rc"
			fi
			echo "west job has no live process or recorded exit status: $state_dir" >&2
			exit 1
		fi
		now="$(date +%s)"
		if ((follow_timeout_seconds > 0 && now - started_at >= follow_timeout_seconds)); then
			printf 'follow timed out; job remains running pid=%s state=%s\n' \
				"$(<"$state_dir/pid")" "$state_dir" >&2
			exit 124
		fi
		if ((now >= next_heartbeat)); then
			printf 'following pid=%s state=%s\n' "$(<"$state_dir/pid")" "$state_dir"
			next_heartbeat=$((now + 10))
		fi
		# This is a bounded foreground wait on the registered PID, not a detached
		# monitor. A live job wakes us after one second; an exit wakes us at once.
		wait_for_pid_exit_or_timeout "$state_dir/pid" 1
	done
}

wait_job() {
	if [[ "${CODEX_CI:-}" == "1" ]]; then
		echo 'west-job wait is unsafe under CODEX_CI; use west-job.sh status to poll the state directory' >&2
		exit 2
	fi
	if load_live_pid; then
		wait_for_pid_exit "$state_dir/pid"
	fi
	local rc
	if rc="$(read_rc)"; then
		exit "$rc"
	fi
	echo "west job exited without recording rc: $state_dir" >&2
	exit 1
}

cancel_job() {
	if ! load_live_pid; then
		status_job
		return
	fi
	local pid grace_seconds
	pid="$(<"$state_dir/pid")"
	grace_seconds="$(cancel_grace_seconds)"
	touch "$state_dir/cancel-requested"
	if load_live_command_pid; then
		local command_pid
		command_pid="$(<"$state_dir/command-pid")"
		snapshot_descendants "$command_pid" "$state_dir/cancel-descendants"
		kill -INT "$command_pid" 2>/dev/null || true
		# The command may exit before its runner finishes resource cleanup. Wait
		# for the session wrapper so a successful cooperative cancel guarantees
		# that its cleanup handlers have completed.
		wait_for_pid_exit_or_timeout "$state_dir/pid" "$grace_seconds"
		if ! load_live_pid; then
			printf 'cancelling command-pid=%s state=%s\n' "$command_pid" "$state_dir"
			return
		fi
	fi
	# Bounded subprocesses may intentionally create their own sessions. They
	# escape a process-group-only fallback, so terminate the exact identities
	# captured while they were still descendants of the registered command.
	signal_snapshotted_descendants "$state_dir/cancel-descendants" TERM
	wait_for_pid_exit_or_timeout "$state_dir/pid" 2
	signal_snapshotted_descendants "$state_dir/cancel-descendants" KILL
	# A command that still ignores SIGINT after its declared cleanup grace
	# cannot run its own cleanup. The runner is the session leader created by
	# setsid; target only its process group, never the caller's group.
	if load_live_runner_pid; then
		local runner_pid
		runner_pid="$(<"$state_dir/runner-pid")"
		kill -TERM -- "-$runner_pid"
		printf 'cancelling unresponsive command-pid=%s via runner group=%s state=%s\n' \
		"${command_pid:-unknown}" "$runner_pid" "$state_dir"
		return
	fi
	# The session leader may have already exited while its outer waiter is
	# still live. Signalling the waiter itself is safe and cannot affect the
	# caller's process group.
	kill -TERM "$pid" 2>/dev/null || true
	printf 'cancelling runner waiter=%s state=%s\n' "$pid" "$state_dir"
}

live_west_test_state() {
	local registry_dir entry candidate_state candidate_pid candidate_start_time
	registry_dir="$(registry_dir_for_state_root "$STATE_ROOT")"
	[[ -d "$registry_dir" && ! -L "$registry_dir" ]] || return 1
	shopt -s nullglob
	for entry in "$registry_dir"/*; do
		[[ -d "$entry" && ! -L "$entry" ]] || continue
		for required in state-dir pid start-time command; do
			[[ -f "$entry/$required" && ! -L "$entry/$required" ]] || continue 2
		done
		candidate_state="$(<"$entry/state-dir")"
		candidate_pid="$(<"$entry/pid")"
		candidate_start_time="$(<"$entry/start-time")"
		if [[ ! "$candidate_pid" =~ ^[0-9]+$ ]] || [[ ! "$candidate_start_time" =~ ^[0-9]+$ ]]; then
			rm -rf "$entry"
			continue
		fi
		if ! kill -0 "$candidate_pid" 2>/dev/null || [[ "$(pid_start_time "$candidate_pid")" != "$candidate_start_time" ]]; then
			rm -rf "$entry"
			continue
		fi
		if grep -Eq '(^|[[:space:]/])west[[:space:]]+test([[:space:]]|$)' "$entry/command"; then
			printf '%s\n' "$candidate_state"
			return 0
		fi
	done
	return 1
}

assert_no_live_west_test() {
	local live_state
	if live_state="$(live_west_test_state)"; then
		echo "cleanup audit blocked by live west test job: $live_state" >&2
		exit 2
	fi
}

command="${1:-}"
[[ -n "$command" ]] || usage
shift

case "$command" in
	start|status|wait|cancel)
		parse_state_dir "$@"
		"${command}_job"
		;;
	follow)
		parse_follow "$@"
		follow_job
		;;
	assert-no-live-west-test)
		parse_state_root "$@"
		assert_no_live_west_test
		;;
	*) usage ;;
esac

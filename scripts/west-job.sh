#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat >&2 <<'USAGE'
usage:
  west-job.sh start --state-dir DIR -- west test ...
  west-job.sh status --state-dir DIR
  west-job.sh wait --state-dir DIR
  west-job.sh cancel --state-dir DIR

Use this only when the caller cannot keep a long west command attached.  DIR
contains command, job/command PIDs, start-times, log, and rc; it is safe to
inspect directly.
USAGE
	exit 2
}

state_dir=

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

read_rc() {
	local rc
	[[ -f "$state_dir/rc" ]] || return 1
	rc="$(<"$state_dir/rc")"
	[[ "$rc" =~ ^[0-9]+$ ]] || return 1
	printf '%s\n' "$rc"
}

start_job() {
	if [[ -e "$state_dir" ]]; then
		echo "west job state already exists: $state_dir" >&2
		exit 2
	fi
	if ((${#STATE_REST[@]} == 0)); then
		usage
	fi
	mkdir -p "$state_dir"
	printf '%q ' "${STATE_REST[@]}" >"$state_dir/command"
	printf '\n' >>"$state_dir/command"

	nohup setsid bash -c '
		state_dir="$1"
		shift
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
		"$@" &
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

wait_job() {
	while load_live_pid; do
		sleep 1
	done
	for _ in $(seq 1 10); do
		local rc
		if rc="$(read_rc)"; then
			exit "$rc"
		fi
		sleep 1
	done
	echo "west job exited without recording rc: $state_dir" >&2
	exit 1
}

cancel_job() {
	if ! load_live_pid; then
		status_job
		return
	fi
	local pid
	pid="$(<"$state_dir/pid")"
	touch "$state_dir/cancel-requested"
	if load_live_command_pid; then
		local command_pid
		command_pid="$(<"$state_dir/command-pid")"
		kill -INT "$command_pid"
		for _ in $(seq 1 20); do
			if ! load_live_command_pid; then
				printf 'cancelling command-pid=%s state=%s\n' "$command_pid" "$state_dir"
				return
			fi
			sleep 0.25
		done
		# A command that ignores SIGINT cannot run its cleanup. Preserve the
		# existing escape hatch for generic jobs while reporting the escalation.
		kill -TERM -- "-$pid"
		printf 'cancelling unresponsive command-pid=%s via job group=%s state=%s\n' \
			"$command_pid" "$pid" "$state_dir"
		return
	fi
	kill -TERM -- "-$pid"
	printf 'cancelling legacy job group=%s state=%s\n' "$pid" "$state_dir"
}

command="${1:-}"
[[ -n "$command" ]] || usage
shift
parse_state_dir "$@"

case "$command" in
	start) start_job ;;
	status) status_job ;;
	wait) wait_job ;;
	cancel) cancel_job ;;
	*) usage ;;
esac

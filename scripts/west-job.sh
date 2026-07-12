#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat >&2 <<'USAGE'
usage:
  west-job.sh start --state-dir DIR -- west test ...
  west-job.sh status --state-dir DIR
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
	local original_state_dir="$state_dir"
	local candidate
	shopt -s nullglob
	for candidate in "$STATE_ROOT"/*; do
		[[ -d "$candidate" && -f "$candidate/command" ]] || continue
		state_dir="$candidate"
		if load_live_pid && grep -Eq '(^|[[:space:]/])west[[:space:]]+test([[:space:]]|$)' "$candidate/command"; then
			state_dir="$original_state_dir"
			printf '%s\n' "$candidate"
			return 0
		fi
	done
	state_dir="$original_state_dir"
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
	assert-no-live-west-test)
		parse_state_root "$@"
		assert_no_live_west_test
		;;
	*) usage ;;
esac

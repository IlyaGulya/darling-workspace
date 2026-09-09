#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat >&2 <<'USAGE'
usage:
  west-job.sh start --state-dir DIR [--activity-log PATH ...] -- west test ...
  west-job.sh status --state-dir DIR
  west-job.sh follow --state-dir DIR [--timeout-seconds N] [--activity-log PATH ...]
  west-job.sh wait --state-dir DIR
  west-job.sh cancel --state-dir DIR
  west-job.sh assert-no-live-west-test [--state-root DIR]

Use this only when the caller cannot keep a long west command attached.  DIR
contains command, job/command PIDs, start-times, log, and rc; it is safe to
inspect directly.

Follow reports the latest phase marker and log-change age for each log. Optional
activity logs are host paths; start remembers them, while follow adds paths only
for that observer. Children atomically publish NUL-delimited kind/path pairs in
WEST_JOB_STATE_DIR/activity-logs.d/*.logs (file or directory, absolute host path).
Directory registrations follow immediate regular files, not recursive trees.
Missing logs may appear later. Silence is not a hang verdict.

Cancel sends SIGINT to the registered command and waits for owner cleanup.
WEST_JOB_CANCEL_GRACE_SECONDS defaults to 30; only an unresponsive owner
triggers the identity-checked descendant and process-group fallback.
USAGE
	exit 2
}

state_dir=
follow_timeout_seconds=0
activity_logs=()

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
	state_root="${WEST_JOB_REGISTRY_ROOT:-${TMPDIR:-/tmp}}"
	state_root="$(realpath -m -- "$state_root")"
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

registry_identity_live() {
	local root="$1" pid_name="$2" start_name="$3" pid start_time
	[[ -f "$root/$pid_name" && ! -L "$root/$pid_name" ]] || return 1
	[[ -f "$root/$start_name" && ! -L "$root/$start_name" ]] || return 1
	pid="$(<"$root/$pid_name")"
	start_time="$(<"$root/$start_name")"
	[[ "$pid" =~ ^[0-9]+$ && "$start_time" =~ ^[0-9]+$ ]] || return 1
	kill -0 "$pid" 2>/dev/null || return 1
	[[ "$(pid_start_time "$pid")" == "$start_time" ]] || return 1
	REGISTRY_IDENTITY_PID="$pid"
	REGISTRY_IDENTITY_START_TIME="$start_time"
}

publish_registry_identity() {
	local entry="$1" pid_tmp start_tmp
	pid_tmp="$entry/.pid.$$"
	start_tmp="$entry/.start-time.$$"
	printf '%s\n' "$REGISTRY_IDENTITY_PID" >"$pid_tmp"
	printf '%s\n' "$REGISTRY_IDENTITY_START_TIME" >"$start_tmp"
	mv "$pid_tmp" "$entry/pid"
	mv "$start_tmp" "$entry/start-time"
}

reconcile_registry_entry() {
	local entry="$1" candidate_state owner_uid
	owner_uid="$(id -u)"
	[[ -d "$entry" && ! -L "$entry" ]] || return
	[[ "$(stat -c '%u' "$entry")" == "$owner_uid" ]] || return
	if [[ ! -f "$entry/state-dir" || -L "$entry/state-dir" ]]; then
		rm -rf -- "$entry"
		return
	fi
	if registry_identity_live "$entry" pid start-time; then
		return
	fi
	candidate_state="$(<"$entry/state-dir")"
	if [[ ! -d "$candidate_state" || -L "$candidate_state" ]]; then
		rm -rf -- "$entry"
		return
	fi
	[[ "$(stat -c '%u' "$candidate_state")" == "$owner_uid" ]] || return
	if [[ -f "$candidate_state/rc" && ! -L "$candidate_state/rc" ]]; then
		rm -rf -- "$entry"
		return
	fi
	if registry_identity_live "$candidate_state" pid start-time ||
		registry_identity_live "$candidate_state" runner-pid runner-start-time; then
		publish_registry_identity "$entry"
		return
	fi
	rm -rf -- "$entry"
}

prune_dead_registry_entries() {
	local entry
	shopt -s nullglob
	for entry in "$REGISTRY_DIR"/*; do
		reconcile_registry_entry "$entry"
	done
}

cleanup_registry_reservation() {
	local launched_start owner_uid
	owner_uid="$(id -u)"
	if [[ -n "${REGISTRY_ENTRY:-}" ]]; then
		if [[ "${REGISTRY_LAUNCHED_PID:-}" =~ ^[0-9]+$ ]] &&
			kill -0 "$REGISTRY_LAUNCHED_PID" 2>/dev/null &&
			launched_start="$(pid_start_time "$REGISTRY_LAUNCHED_PID")" &&
			[[ "$launched_start" =~ ^[0-9]+$ ]] &&
			[[ -d "$state_dir" && ! -L "$state_dir" ]] &&
			[[ "$(stat -c '%u' "$state_dir")" == "$owner_uid" ]]; then
			printf '%s\n' "$REGISTRY_LAUNCHED_PID" >"$state_dir/pid"
			printf '%s\n' "$launched_start" >"$state_dir/start-time"
			REGISTRY_IDENTITY_PID="$REGISTRY_LAUNCHED_PID"
			REGISTRY_IDENTITY_START_TIME="$launched_start"
			publish_registry_identity "$REGISTRY_ENTRY"
		else
			reconcile_registry_entry "$REGISTRY_ENTRY"
			if [[ "${REGISTRY_STATE_CREATED:-0}" == 1 ]] &&
				[[ -z "${REGISTRY_LAUNCHED_PID:-}" ]] &&
				[[ -d "$state_dir" && ! -L "$state_dir" ]] &&
				[[ "$(stat -c '%u' "$state_dir")" == "$owner_uid" ]]; then
				rm -rf -- "$state_dir"
			fi
		fi
	fi
	if [[ -n "${REGISTRY_LOCK_FD:-}" ]]; then
		flock -u "$REGISTRY_LOCK_FD" 2>/dev/null || true
		exec {REGISTRY_LOCK_FD}>&- 2>/dev/null || true
	fi
}


reserve_job_registry_entry() {
	local entry candidate_pid candidate_start_time
	prepare_job_registry
	exec {REGISTRY_LOCK_FD}>"$REGISTRY_DIR/.lock"
	flock "$REGISTRY_LOCK_FD"
	prune_dead_registry_entries
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
	REGISTRY_ENTRY="$entry"
	printf '%s\n' "$state_dir" >"$entry/state-dir"
	write_command_record "$entry/command"
}

record_job_registry_identity() {
	REGISTRY_IDENTITY_PID="$(<"$state_dir/pid")"
	REGISTRY_IDENTITY_START_TIME="$(<"$state_dir/start-time")"
	publish_registry_identity "$REGISTRY_ENTRY"
	flock -u "$REGISTRY_LOCK_FD"
	exec {REGISTRY_LOCK_FD}>&-
	REGISTRY_ENTRY=
}

parse_state_dir() {
	while (($#)); do
		case "$1" in
			--state-dir)
				state_dir="$2"
				shift 2
				;;
			--activity-log)
				[[ "$command" == start && $# -ge 2 && -n "$2" ]] || usage
				activity_logs+=("$(realpath -m -- "$2")")
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
	state_dir="$(realpath -m -- "$state_dir")"
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
			--activity-log)
				[[ $# -ge 2 && -n "$2" ]] || usage
				activity_logs+=("$(realpath -m -- "$2")")
				shift 2
				;;
			*) usage ;;
		esac
	done
	if [[ -z "$state_dir" ]] || [[ ! "$follow_timeout_seconds" =~ ^[0-9]+$ ]]; then
		usage
	fi
	state_dir="$(realpath -m -- "$state_dir")"
}

parse_state_root() {
	state_root="${WEST_JOB_REGISTRY_ROOT:-${TMPDIR:-/tmp}}"
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
	STATE_ROOT="$(realpath -m -- "$state_root")"
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
	REGISTRY_ENTRY=
	REGISTRY_STATE_CREATED=0
	REGISTRY_LAUNCHED_PID=
	trap cleanup_registry_reservation EXIT
	reserve_job_registry_entry
	mkdir -p "$state_dir"
	mkdir "$state_dir/activity-logs.d"
	REGISTRY_STATE_CREATED=1
	write_command_record "$state_dir/command"
	if ((${#activity_logs[@]})); then
		printf '%s\0' "${activity_logs[@]}" >"$state_dir/activity-logs"
	fi

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
		# An asynchronous Bash child inherits SIGINT ignored. Bash cannot reset
		# a signal ignored on entry; restore it outside the shell before exec.
		env --default-signal=INT -- "$@" &
		command_pid=$!
		printf "%s\\n" "$command_pid" >"$state_dir/command-pid"
		awk "{print \$22}" "/proc/$command_pid/stat" >"$state_dir/command-start-time"
		wait "$command_pid"
		finish "$?"
	' bash "$state_dir" "${STATE_REST[@]}" \
		>"$state_dir/log" 2>&1 < /dev/null &
	REGISTRY_LAUNCHED_PID=$!
	local pid="$REGISTRY_LAUNCHED_PID"
	printf '%s\n' "$pid" >"$state_dir/pid"
	pid_start_time "$pid" >"$state_dir/start-time"
	record_job_registry_identity
	trap - EXIT
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
	# The observer stays attached. Python keeps byte cursors and partial lines in
	# memory instead of recounting and rescanning the entire log on every tick.
	exec python3 - "$state_dir" "$follow_timeout_seconds" "${activity_logs[@]}" <<'PY'
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

state = Path(sys.argv[1])
timeout_seconds = int(sys.argv[2])
started = time.monotonic()
phase = re.compile(
    rb"^\s*((?:runtime|prefix bootstrap|tier) phase (?:start|complete): .+"
    rb"|runtime profile preflight: .+|(?:STOCK_PHASE|WEST_GUEST_STAGE)=.+)\r?$"
)


class Log:
    def __init__(self, path, stream=False):
        self.path = path
        self.stream = stream
        self.identity = None
        self.offset = 0
        self.anchor = b""
        self.pending = b""
        self.stage = "unknown"
        self.mtime = None
        self.available = False

    def update(self):
        self.available = False
        try:
            # O_NONBLOCK prevents an accidentally registered FIFO from wedging
            # the observer; only regular files are followed.
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return False
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                return False
            self.available = True
            self.mtime = info.st_mtime
            identity = (info.st_dev, info.st_ino)
            source.seek(max(0, self.offset - len(self.anchor)))
            anchor = source.read(len(self.anchor))
            if (identity != self.identity or info.st_size < self.offset
                    or anchor != self.anchor):
                self.offset = 0
                self.pending = b""
                self.stage = "unknown"
            self.identity = identity
            source.seek(self.offset)
            # Bounded chunks allow identity/deadline checks even during a flood.
            data = source.read(min(1024 * 1024, max(0, info.st_size - self.offset)))
            if data:
                if self.stream:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                lines = (self.pending + data).split(b"\n")
                # Phase records are short; never retain an unbounded partial line.
                self.pending = lines.pop()[-65536:]
                for line in lines:
                    match = phase.fullmatch(line)
                    if match:
                        self.stage = match[1].decode("utf-8", errors="replace").strip()
                self.offset += len(data)
            source.seek(max(0, self.offset - 64))
            self.anchor = source.read(min(64, self.offset))
            return self.offset < info.st_size

    def progress(self, pid):
        age = (f"{max(0, int(time.time() - self.mtime))}s"
               if self.available else "unavailable")
        stage = self.stage if self.available else "unavailable"
        print(f"following pid={pid} state={str(state)!r} log={self.path!r} "
              f"stage={stage!r} log-change-age={age}", flush=True)


class Directory:
    """Rescan only changed directories, visiting at most 256 entries per tick."""
    def __init__(self, path):
        self.path = path
        self.stamp = None
        self.entries = None

    def update(self, visit):
        if self.entries is None:
            try:
                info = os.stat(self.path, follow_symlinks=False)
                stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                if not stat.S_ISDIR(info.st_mode) or stamp == self.stamp:
                    return False
                self.entries = os.scandir(self.path)
                self.stamp = stamp
            except OSError:
                self.stamp = None
                return False
        for _ in range(256):
            try:
                entry = next(self.entries)
            except (StopIteration, OSError):
                self.entries.close()
                self.entries = None
                try:
                    info = os.stat(self.path, follow_symlinks=False)
                    return self.stamp != (
                        info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                except OSError:
                    self.stamp = None
                    return False
            try:
                if entry.is_file(follow_symlinks=False):
                    visit(entry)
            except OSError:
                pass
        return True


def add_log(path, stream=True):
    path = os.path.abspath(path)
    if path not in logs:
        logs[path] = Log(path, stream=stream)
    elif stream:
        logs[path].stream = True


def register(entry):
    if not entry.name.endswith(".logs") or entry.name in registrations:
        return
    # Publishers rename complete immutable records into this directory. Neither
    # command output nor partial temporary files can introduce watched paths.
    try:
        with open(entry.path, "rb") as source:
            data = source.read(65537)
    except OSError:
        return
    registrations.add(entry.name)
    fields = data.split(b"\0")
    if len(data) > 65536 or fields[-1] or len(fields) % 2 != 1:
        return
    pairs = list(zip(fields[:-1:2], fields[1::2]))
    if any(kind not in (b"file", b"directory") or not path.startswith(b"/")
           for kind, path in pairs):
        return
    for kind, path in pairs:
        path = os.fsdecode(path)
        if kind == b"file":
            add_log(path)
        elif path not in directories:
            directories[path] = Directory(path)


def discover():
    pending = registry.update(register)
    for directory in directories.values():
        pending = directory.update(lambda entry: add_log(entry.path)) or pending
    return pending


def read_rc():
    try:
        value = (state / "rc").read_text().strip()
        return int(value) if value.isascii() and value.isdecimal() else None
    except OSError:
        return None


def live_pid():
    try:
        pid = (state / "pid").read_text().strip()
        expected = (state / "start-time").read_text().strip()
        if not pid.isascii() or not pid.isdecimal():
            return None
        os.kill(int(pid), 0)
        # comm can contain spaces and parentheses; field 22 follows the last ).
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return pid if fields[19] == expected else None
    except (OSError, ValueError, IndexError):
        return None


def wait_for_owner(seconds):
    try:
        subprocess.run(["pidwait", "-F", str(state / "pid")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        pass


paths = [str(state / "log")]
try:
    paths.extend(os.fsdecode(path) for path in (state / "activity-logs").read_bytes().split(b"\0")
                 if path)
except FileNotFoundError:
    pass
paths.extend(sys.argv[3:])
logs = {}
for index, path in enumerate(dict.fromkeys(paths)):
    add_log(path, stream=(index == 0))
registry = Directory(state / "activity-logs.d")
registrations = set()
directories = {}
next_heartbeat = started
last_stages = None
while True:
    backlog = discover()
    for log in logs.values():
        backlog = log.update() or backlog
    rc = read_rc()
    pid = live_pid()
    if rc is None and pid is None:
        # Preserve the bounded final-publication grace from the shell observer.
        wait_for_owner(1)
        rc = read_rc()
        if rc is None:
            print(f"west job has no live process or recorded exit status: {state}", file=sys.stderr)
            sys.exit(1)
    if rc is not None:
        # The runner may have written final output just before publishing rc.
        backlog = discover() or backlog
        for log in logs.values():
            backlog = log.update() or backlog
    now = time.monotonic()
    stages = [(log.available, log.stage) for log in logs.values()]
    if now >= next_heartbeat or stages != last_stages:
        for log in logs.values():
            log.progress(pid or "completed")
        last_stages = stages
        next_heartbeat = now + 10
    if rc is not None and not backlog:
        print(f"completed rc={rc} state={state}", flush=True)
        sys.exit(rc)
    if rc is None and timeout_seconds and now - started >= timeout_seconds:
        print(f"follow timed out; job remains running pid={pid} state={state}", file=sys.stderr)
        sys.exit(124)
    if not backlog:
        remaining = timeout_seconds - (now - started) if timeout_seconds else 1
        wait_for_owner(min(1, max(0.001, remaining)))
PY
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

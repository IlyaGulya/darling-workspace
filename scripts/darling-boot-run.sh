#!/bin/sh
# darling-boot-run.sh -- one measured boot run, with the hygiene that a measured run needs.
#
# WHY THIS EXISTS. Four separate time sinks in one investigation cycle came from running without a harness:
#
#   * launching while the previous run's server was still alive -- the reused server produced a ONE-LINE log and
#     the "measurement" drawn from it was worthless;
#   * stale guest processes accumulating because a cleanup matched /proc/<pid>/cmdline and a guest's cmdline is
#     the IN-GUEST path with no prefix in it (893 processes accumulated that way);
#   * two runs writing one log path, truncating each other;
#   * reading a diagnostic line as a verdict, when the verdict only exists after the workload's own completion.
#
# What it guarantees, in order:
#   1. a clean start: shutdown, then every prefix-owned process by /proc/<pid>/exe AND cmdline, then verify zero;
#   2. a UNIQUE log path per run (never shared, never appended);
#   3. a bounded wait for the workload's declared completion, not an unconditional delay;
#   4. a single VERDICT block at the end: the workload's own STATUS, the presence markers the caller asked for,
#      plus the counters that matter here (per-thread RPC socket creations, denials, urgent timeouts, courier misses);
#   5. a RUN BUNDLE, ON BY DEFAULT, with a console block that announces it.
#
# THE RUN BUNDLE (`--bundle DIR`, `--no-bundle`). One self-describing directory per run, `<log>.bundle` by default.
# It holds, and NAMES on stdout: both logs (the harness's own and only this run's server bytes -- the server's
# stderr never appears in the run log), every guest-process capture found under the prefix with its path PRINTED
# (the xtrace naming `<arm-out-prefix>.trace.<pid>`/`.traceg.<pid>` is announced nowhere else), the child's
# environment and descriptor table read from the HOST while it was alive (a guest cannot read its own /proc), any
# core written during the run, the abort/terminate text lifted WHOLE from wherever it landed, the plane/ledger
# counters, the deployed sha256 of the fixed boot-component inventory (the RUN-ENV line), and the instrument census that NAMES every
# instrument that stayed silent. Every item is a row of the bundle's MANIFEST: PRESENT <path> or MISSING <reason>,
# so a piece the harness could not collect is reported rather than silently absent, and `INCOMPLETE` in the bundle
# marks one that did not finish. It is produced before teardown and on the freeze-on-fail path too.
#
# THE VERDICT RULE -- WHICH DECLARATION ASSERTS SUCCESS. A declaration that names a STRING asserts presence
# only: an absent one is FAIL/HANG and never PASS. A declaration that names a VALUE is a STATUS field, and a
# status field asserts success only by printing an integer 0 -- absent, unparseable or non-zero is FAIL. The
# value-naming declarations are `--status-marker FIELD=`, and any `--marker` or caller-supplied `--poll` whose
# text ends with `=` (which is the spelling the XCB repro used). MEASURED (dar-u4mk): the old rule matched
# `--marker 'XCB_RC='` as a SUBSTRING of `XCB_RC=132` (128+4 = SIGILL, no artifact produced) and printed
# VERDICT: PASS for a workload that had already died, so a status marker never asserts success on its own.
# Ambiguity fails closed: a declaration that names a value but not as a plain literal field (no regex
# metacharacters) is FAIL rather than success. A caller that declares no status field gets the behaviour it
# always had, and the harness's own default --poll is never a status declaration.
# Explicit --poll wins. Otherwise --status-marker waits for ALL declared status fields and replaces the
# fixture's HELLO/FINAL defaults; explicit --marker declarations remain additional required checks.
#
# Usage:
#   darling-boot-run.sh --runtime-root ROOT --prefix PATH --wait SECONDS [--marker NAME]... [--status-marker FIELD=] [--poll REGEX] [--hatch] [--cmd 'shell command'] [--bundle DIR|--no-bundle]
# Exit: 0 only if every declared status field was the integer 0 and every presence marker appeared, 1 otherwise (or if cleanup failed).

set -u

PREFIX=""
RUNTIME_ROOT=""
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DARLING_WORKSPACE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/darling-runtime.sh"
WAIT=""
CMD="echo HELLO=1; echo FINAL=1"
MARKERS=""
HATCH=0
LOG=""
VERIFY_PROBES=""
EXTRA_ENV=""
ASSERT_MAPS=0
LIST_OWNED=0
POLL=""
# Whether the caller supplied --poll itself. Only a CALLER-supplied poll can name a status value; the
# harness's own default poll line (see below) stays a pure stop condition.
POLL_EXPLICIT=0
# `--status-marker FIELD=` declarations: the workload's own status values, judged as integers (verdict block).
STATUS_MARKERS=""
# THE RUN BUNDLE. ON BY DEFAULT: every run writes ONE self-describing bundle beside its log, and the run output
# announces the bundle path, every capture path and the instrument census. MEASURED why: this harness was already
# the only path with a clean start, a unique log and a verdict, yet a diagnosis of a run it produced still had to
# guess where the guest's xtrace captures went (they are `<arm-out-prefix>.trace.<pid>`/`.traceg.<pid>` under the
# prefix, named nowhere in the output), that the server's own log is a SECOND log at
# `<prefix>/private/var/log/dserver.log`, that the child's environment and descriptors can only be read from the
# HOST while the child is alive, and which instruments never spoke. Each of those cost a rung. `--no-bundle`
# turns it off; `--bundle DIR` puts it somewhere else.
BUNDLE=1
BUNDLE_DIR=""
BUNDLE_MANIFEST=""
BUNDLE_CHILD_DIR=""
BUNDLE_CAPTURE_DIR=""
BUNDLE_CORES_DIR=""
BUNDLE_STAMP=""
BUNDLE_SERVER_LOG=""
BUNDLE_SERVER_STATE=""
BUNDLE_FAILED=0
BUNDLE_SERVER_READY=0
BUNDLE_RUN_ENV=""
BUNDLE_CORE_PATTERN=""
BUNDLE_CORE_DIR=""
# Final teardown observations. A retained runtime has no post-cleanup measurements.
CLEANUP_STATE=not-attempted
CLEANUP_PROCESSES=null
CLEANUP_MOUNTS=null
CLEANUP_LAYER_MOUNTS=null
CLEANUP_SHUTDOWN_RC=null
LAUNCHER_RC=null
LAUNCHER_PID=
CLEANUP_RESCUE_RC=null
CLEANUP_RESCUE_PROCESSES=null
CLEANUP_EXTERNAL_RESCUE=false

rescue_owned() {
	python3 -B "$DARLING_WORKSPACE_ROOT/west_commands/test_prefix.py" --prefix "$PREFIX" --rescue
}

parse_args() {
while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--runtime-root) RUNTIME_ROOT="$2"; shift 2 ;;
		--wait) WAIT="$2"; shift 2 ;;
		# A marker declaration. One whose text ends with `=` names a VALUE and is therefore a STATUS field:
		# the workload asserts success only by printing an integer 0 after it (verdict block). Every other
		# marker asserts presence only -- which is why a bare `XCB_RC=` can never be read as a pass.
		--marker) MARKERS="$MARKERS|$2"; shift 2 ;;
		# The explicit form of the same declaration: the field the workload's own status follows, e.g.
		# `--status-marker XCB_RC=`. Value-naming is the whole point, so a plain literal FIELD= is required.
		--status-marker) STATUS_MARKERS="$STATUS_MARKERS|$2"; shift 2 ;;
		--cmd) CMD="$2"; shift 2 ;;
		--hatch) HATCH=1; shift ;;
		--env) EXTRA_ENV="$EXTRA_ENV $2"; shift 2 ;;
		# The workload's own result line, as a regex: the harness stops as soon as it appears in the
		# log instead of sleeping out --wait. Without an explicit poll, status declarations provide completion;
		# callers declaring neither keep the original fixture's poll (see the poll loop below).
		# A CALLER-supplied poll whose text ends with `=` names a VALUE (e.g. `--poll XCB_RC=`) and is
		# judged as a status field in the verdict block; the harness's own default poll is not.
		--poll) POLL="$2"; POLL_EXPLICIT=1; shift 2 ;;
		# DIAGNOSTIC ONLY (default off): when the marker verdict fails, leave the prefix and its
		# processes ALIVE so an external observer (darling-debug, LLDB, host wchan) can inspect the
		# failing incarnation. Acceptance never sets this; it exists because a watchdog stop followed
		# immediately by a shutdown destroys exactly the state the investigation needs.
		--freeze-on-fail) FREEZE_ON_FAIL=1; shift ;;
		--list-owned) LIST_OWNED=1; shift ;;
		--assert-prefix-maps) ASSERT_MAPS=1; shift ;;
		# The run bundle: on by default (a diagnosis must not depend on knowing where to look). `--bundle DIR`
		# chooses the directory, `--no-bundle` suppresses it.
		--bundle) BUNDLE_DIR="$2"; shift 2 ;;
		--no-bundle) BUNDLE=0; shift ;;
		--verify-probe) VERIFY_PROBES="$VERIFY_PROBES
$2"; shift 2 ;;
		--log) LOG="$2"; shift 2 ;;
		# The header block, not a frozen line range: the range used to be a literal 2,22 and silently
		# dropped the "Exit:" line as soon as the header grew.
		-h|--help) sed -n '2,/^$/p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
}
parse_args "$@"

register_harness_output() {
	[ -n "${WEST_JOB_STATE_DIR:-}" ] || return 0
	[ "$LIST_OWNED" != 1 ] || return 0
	set --
	for _fd in 1 2; do
		_output=$(readlink "/proc/$$/fd/$_fd" 2>/dev/null)
		case "$_output" in
			/*) [ ! -f "$_output" ] || set -- "$@" file "$_output" ;;
		esac
	done
	[ "$#" -ne 0 ] || return 0
	"$SCRIPT_DIR/west-job.sh" register --state-dir "$WEST_JOB_STATE_DIR" -- "$@"
}
# Register the real caller-owned console before preflight can fail. A printed
# path is not a registration channel, and no ambient directory is consulted.
register_harness_output || exit $?

[ -n "$PREFIX" ] || { echo "usage: $0 --prefix PATH --wait SECONDS [--marker NAME]..." >&2; exit 2; }
[ -n "$WAIT" ] || [ "$LIST_OWNED" = 1 ] || { echo "usage: $0 --prefix PATH --wait SECONDS [--marker NAME]..." >&2; exit 2; }
# A status field names a VALUE (`XCB_RC=`), never an expected value: `--status-marker XCB_RC=0` would make the
# field text itself the needle and read every real line as MISS. Refuse at the front door -- before any state is
# consumed -- rather than fail closed on a log that never carried the right shape.
oldIFS=$IFS; IFS='|'
for _sf in $STATUS_MARKERS; do
	[ -n "$_sf" ] || continue
	case "$_sf" in
	*=) ;;
	*) echo "usage: --status-marker FIELD= names the workload's own status field, e.g. --status-marker XCB_RC= (got '$_sf')" >&2; exit 2 ;;
	esac
done
IFS=$oldIFS; unset _sf
# Explicit status declarations are sufficient: do not wait for an unrelated fixture's result.
POLL_STATUS=0
if [ "$POLL_EXPLICIT" = 0 ] && [ -n "$STATUS_MARKERS" ]; then
	POLL_STATUS=1
else
	[ -n "$POLL" ] || POLL='RING_MACH_TEST mode=.*pass='
fi

# A run can be served by another prefix's runtime, and then every conclusion drawn from it is about the wrong
# artifacts -- silently, because the log looks normal. MEASURED: a stale shellspawn from a second prefix was
# alive through a whole series of runs, and a probe deployed into this prefix's libsystem_kernel never fired.
# The guard is cheap and refuses before any state is consumed.
if [ "$ASSERT_MAPS" = 1 ]; then
	foreign=""
	for d in /proc/[0-9]*; do
		comm=$(cat "$d/comm" 2>/dev/null)
		case "$comm" in mldr|launchd|vchroot|shellspawn|darlingserver) ;; *) continue ;; esac
		pid=${d#/proc/}
		for m in $("$SCRIPT_DIR/darling-prefix-map.sh" "$pid" 2>/dev/null); do
			case "$m" in
				"$PREFIX"|"$PREFIX"/*) ;;
				/tmp/dr-*)
					foreign="$foreign pid=$pid comm=$comm from=$m"
					;;
			esac
		done
	done
	if [ -n "$foreign" ]; then
		echo "REFUSING: another prefix's runtime is alive and may serve this run:$foreign" >&2
		exit 1
	fi
fi
[ -n "$MARKERS$STATUS_MARKERS" ] || MARKERS="|HELLO=1|FINAL=1"
if [ "$LIST_OWNED" != 1 ]; then
	if [ -z "${WEST_TEST_PREFIX_LEASE:-}" ]; then
		exec python3 -B "$DARLING_WORKSPACE_ROOT/west_commands/test_prefix.py" \
			--prefix "$PREFIX" -- "$0" "$@"
	fi
	[ "$WEST_TEST_PREFIX_LEASE" = "$PREFIX" ] || {
		echo "borrowed prefix lease does not match --prefix" >&2
		exit 2
	}
	python3 -B "$DARLING_WORKSPACE_ROOT/west_commands/test_prefix.py" \
		--prefix "$PREFIX" --validate-lease || exit $?
	darling_resolve_runtime "$RUNTIME_ROOT" "$PREFIX" || exit $?
fi
export DPREFIX="$PREFIX" DARLING_PREFIX="$PREFIX"
[ -n "$LOG" ] || LOG="${DW_STATE_ROOT:-$DARLING_WORKSPACE_ROOT/../.west-state}/boot-runs/darling-boot-$(date +%Y%m%d-%H%M%S)-$$.log"
[ "$LIST_OWNED" = 1 ] || mkdir -p "$(dirname "$LOG")" || exit 2

# Enumerate prefix-owned processes. Two traps, both measured: a guest process's cmdline is the IN-GUEST path
# and contains no prefix (so exe is the arm that finds it), and THIS SCRIPT's own cmdline contains the prefix when
# it is invoked with --prefix <that prefix> (so the self and the parent must be excluded, or the harness kills
# itself -- the same class of defect as a probe that modifies what it measures).
SELF_NAME=${0##*/}

# Every ANCESTOR of this shell, transitively. MEASURED DEFECT: excluding only `self` and the immediate parent left a
# two-level wrapper (suite -> verdict -> runner) inside the cmdline arm, because that wrapper is ALSO invoked with
# --prefix; the runner then killed its own caller three seconds into the first mode (rc=137, SIGKILL) and the wrapper
# reported the workload as a failure. A cleanup must never kill the process tree that asked for it, at ANY depth.
own_ancestors() {
	p=$$
	while [ -n "$p" ] && [ "$p" != "0" ] && [ "$p" != "1" ]; do
		echo "$p"
		p=$(sed 's/.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $2}')
	done
}

# Teardown witness: every destructive decision names ITSELF and the EVIDENCE it acted on, on stderr so the pid lists on
# stdout stay parseable. A caller that dies during teardown (MEASURED: exit 143 = SIGTERM at wait+6 s) cannot be diagnosed
# from a process count: the arm that matched, the ancestry verdict and the command text are the diagnosis.
# `WITNESS_QUIET=1` suppresses it for the enumeration the bundle runs ONCE PER POLL during the workload: the same
# evidence printed hundreds of times is not more evidence, and it would bury the console the bundle announces on.
witness() { [ "${WITNESS_QUIET:-0}" = 1 ] || echo "$@" >&2; }
caller_id() { # pid ppid pgid sid cmd
	local p=$1 st
	st=$(sed 's/.*) //' "/proc/$p/stat" 2>/dev/null)
	printf '%s\n' "$p $(echo "$st" | awk '{print $2}') $(echo "$st" | awk '{print $3}') $(echo "$st" | awk '{print $4}') $(tr '\0\n\r' '   ' < "/proc/$p/cmdline" 2>/dev/null | cut -c1-120)"
}

owned_pids() {
	pids=$(darling_prefix_owned_pids "$PREFIX") || return $?
	ancestors=$(own_ancestors | tr '\n' ' ')
	for pid in $pids; do
		case " $ancestors " in *" $pid "*) continue ;; esac
		witness "OWNED pid=$pid arm=runtime-identity ancestor=no"
		printf '%s\n' "$pid"
	done
}
count_owned() {
	pids=$(owned_pids) || return $?
	if [ -z "$pids" ]; then printf '0\n'; else printf '%s\n' "$pids" | wc -l; fi
}

# The prefix's own runtime files. A killed run leaves them naming a server that is gone, and they are never removed
# while an owner is alive: both callers below only act with zero owners, which is the framework's own precondition
# (west_commands/test_prefix.py::remove_stale_init_pid / remove_stale_server_socket).
stale_init_pid() { [ -r "$PREFIX/.init.pid" ] || return 0; tr -dc '0-9' < "$PREFIX/.init.pid" 2>/dev/null; }
report_stale_runtime_files() {
	ip=$(stale_init_pid)
	if [ -n "$ip" ] && [ ! -d "/proc/$ip" ]; then
		echo "STALE-RUNTIME-FILE $PREFIX/.init.pid pid=$ip dead"
	fi
	for f in .darlingserver.sock .darlingserver.stat.sock; do
		[ -e "$PREFIX/$f" ] && echo "STALE-RUNTIME-FILE $PREFIX/$f"
	done
	return 0
}
clear_stale_runtime_files() {
	ip=$(stale_init_pid)
	if [ -n "$ip" ] && [ ! -d "/proc/$ip" ]; then
		witness "CLEAR stale $PREFIX/.init.pid pid=$ip reason=not-alive"
		rm -f "$PREFIX/.init.pid"
	fi
	for f in .darlingserver.sock .darlingserver.stat.sock; do
		[ -e "$PREFIX/$f" ] || continue
		witness "CLEAR stale $PREFIX/$f reason=no-owner"
		rm -f "$PREFIX/$f"
	done
	unset ip
	return 0
}

# ---------------------------------------------------------------------------------------------------------------------
# THE RUN BUNDLE: one self-describing directory per run.
#
# REQUIREMENT, measured: a diagnosis must not depend on an agent happening to know where to look. The whole reason
# this harness exists is that "run once, read one log" was wrong in four separate ways, so the harness now also
# COLLECTS, per run, in one place: both logs (the harness's own and this run's server bytes), every
# guest-process capture with its path PRINTED, the child's environment and descriptor table read from the HOST, any
# core written during the run, the abort/terminate text wherever it landed, the plane/ledger counters, the deployed
# sha256 of the fixed boot-component inventory, and a census that NAMES the instruments that stayed silent.
#
# The rules it obeys:
#   * every item is a ROW of `MANIFEST`, with PRESENT <path> or MISSING <reason> -- a missing piece is never
#     silently absent, and a row that is not there at all is itself the defect;
#   * nothing is truncated at a small constant: an abort line is copied WHOLE and its length is reported, so the
#     bound is the record's own (the 512-byte cap that cut the very buffer the abort message was in is exactly the
#     failure this replaces);
#   * a bundle that did not finish says so: `INCOMPLETE` exists from the moment the directory is created and is
#     removed only after the manifest is complete;
#   * no extra flags are needed: this is the normal path.
# ---------------------------------------------------------------------------------------------------------------------
BUNDLE_MANIFEST_ROW() { # ITEM STATE PATH DETAIL
	printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$BUNDLE_MANIFEST"
}

bundle_present() { # FILE-ITEM SRC [NOTE]
	BUNDLE_MANIFEST_ROW "$1" PRESENT "$2" "$3"
}

bundle_missing() { # ITEM REASON
	BUNDLE_MANIFEST_ROW "$1" MISSING "-" "$2"
}

# A one-line summary of a file's own size, so a reader sees the truncation bound being the record's own length.
bundle_bytes() { wc -c < "$1" 2>/dev/null | tr -d ' '; }

bundle_begin() {
	[ "$BUNDLE" = 1 ] || return 0
	[ -n "$BUNDLE_DIR" ] || BUNDLE_DIR="$LOG.bundle"
	case "$BUNDLE_DIR" in /*) ;; *) BUNDLE_DIR="$PWD/$BUNDLE_DIR" ;; esac
	if [ -e "$BUNDLE_DIR" ]; then
		if [ -n "$(ls -A "$BUNDLE_DIR" 2>/dev/null)" ]; then
			echo "refusing --bundle $BUNDLE_DIR: it is not empty, and a bundle that holds another run's files reads as this run's evidence" >&2
			exit 2
		fi
	else
		mkdir -p "$BUNDLE_DIR" || { echo "cannot create bundle $BUNDLE_DIR" >&2; exit 2; }
	fi
	BUNDLE_MANIFEST="$BUNDLE_DIR/MANIFEST"
	BUNDLE_CHILD_DIR="$BUNDLE_DIR/children"
	BUNDLE_CAPTURE_DIR="$BUNDLE_DIR/captures"
	BUNDLE_CORES_DIR="$BUNDLE_DIR/cores"
	mkdir -p "$BUNDLE_CHILD_DIR" "$BUNDLE_CAPTURE_DIR" "$BUNDLE_CORES_DIR"
	# From here to the end of the manifest there IS no complete bundle: say so, and remove it only at the end.
	: > "$BUNDLE_DIR/INCOMPLETE"
	# Only files NEWER than this stamp are this run's: the prefix's guest tmp keeps earlier runs' captures, and a
	# copy that is not this run's is worse than no copy.
	BUNDLE_STAMP="$BUNDLE_DIR/run-start.stamp"
	touch "$BUNDLE_STAMP"
	{
		echo "# darling run bundle"
		echo "# log=$LOG prefix=$PREFIX wait=$WAIT"
		echo "# cmd=$CMD"
		echo "# rows are: ITEM <TAB> PRESENT|MISSING <TAB> PATH <TAB> DETAIL"
		echo "# MISSING means the collector could not obtain it; the detail says why. INCOMPLETE next to this file means"
		echo "# collection had a known gap or finalization did not finish, so the bundle is a partial one."
		echo "# File paths are logical originals: after finalization, a missing payload may be stored as PATH.zst."
		echo "# Each directory's immutable evidence-index*.json publications map logical basenames to stored paths, byte sizes and SHA256."
		echo "# Server coverage is bounded by start/finish observations: retained original inode tail plus final current inode."
		echo "# Intermediate rotations/replacements not present at either boundary cannot be recovered; writes after capture are excluded."
	} > "$BUNDLE_MANIFEST"
	echo "BUNDLE dir=$BUNDLE_DIR"
	bundle_present run-log "$LOG" "the harness's own log: the guest's and the loader's fd 1/2 for this run"
	# Retain the original regular file, not a pipe or a full history copy. The FD survives rename rotation;
	# the shared collector snapshots only its identity, size and boundary fingerprint.
	# Resolve directory aliases once for the codec's no-symlink traversal rule. Keep the final component
	# unresolved so a symlink masquerading as the server log is still refused.
	BUNDLE_SERVER_LOG="$(realpath -m -- "$PREFIX/private/var/log")/dserver.log"
	BUNDLE_SERVER_STATE="$BUNDLE_DIR/server-log-state.json"
	if [ -f "$BUNDLE_SERVER_LOG" ] && [ ! -L "$BUNDLE_SERVER_LOG" ] && [ -r "$BUNDLE_SERVER_LOG" ]; then
		exec 9< "$BUNDLE_SERVER_LOG"
	fi
	if python3 "$SCRIPT_DIR/darling-evidence.py" log-start --source "$BUNDLE_SERVER_LOG" \
		--state "$BUNDLE_SERVER_STATE" --original "/proc/$$/fd/9"; then
		BUNDLE_SERVER_READY=1
		bundle_present server-log-boundary "$BUNDLE_SERVER_STATE" "start identity/size/fingerprint only; original regular-file FD retained until collection, including rename rotation"
	else
		BUNDLE_FAILED=1
		bundle_missing server-log-boundary "log-start failed: server bytes cannot be attributed reliably; see $BUNDLE_SERVER_STATE and collector stderr"
	fi
	# DEPLOYED BOOT-COMPONENT FILE IDENTITY. `dwdiag` owns the fixed candidate table.
	# These hashes name configured files, not every workload dependency or the bytes loaded by a live process.
	# Workload-specific executables and libraries need their own identity measurement.
	BUNDLE_RUN_ENV=""
	if [ -x "$SCRIPT_DIR/dwdiag" ]; then
		"$SCRIPT_DIR/dwdiag" env --runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$PREFIX" > "$BUNDLE_DIR/run-env.raw" 2>"$BUNDLE_DIR/run-env.err"
		env_rc=$?
		if [ "$env_rc" -eq 0 ] && [ -s "$BUNDLE_DIR/run-env.raw" ]; then
			BUNDLE_RUN_ENV=$(cat "$BUNDLE_DIR/run-env.raw")
			cp -f "$BUNDLE_DIR/run-env.raw" "$BUNDLE_DIR/run-env.txt"
			bundle_present run-env "$BUNDLE_DIR/run-env.txt" "fixed boot-component files only; not workload or loaded-image identity; $BUNDLE_RUN_ENV"
		else
			rm -f "$BUNDLE_DIR/run-env.raw"
			bundle_missing run-env "dwdiag env --prefix $PREFIX exited $env_rc, so no fixed boot-component copy could be digested (see $BUNDLE_DIR/run-env.err)"
		fi
	else
		bundle_missing run-env "no executable $SCRIPT_DIR/dwdiag: the deployed-copy digest cannot be taken (and the instrument census below will be missing too)"
	fi
	# WHAT CRASH CAPTURE WOULD DO, stated before the run: cores are OFF here by default, so a crash normally leaves
	# no core -- and "no core" must never look like "the fault left no evidence".
	BUNDLE_CORE_PATTERN=$(cat /proc/sys/kernel/core_pattern 2>/dev/null)
	echo "${BUNDLE_CORE_PATTERN:-<unreadable>}" > "$BUNDLE_DIR/core-pattern.txt"
	BUNDLE_CORE_DIR=""
	case "$BUNDLE_CORE_PATTERN" in
	"") ;;
	"|"*) bundle_missing core-capture-allowed "core_pattern pipes cores to a handler ('$BUNDLE_CORE_PATTERN'): NO core file is kept for any process, so a crash this run leaves nothing to read. Fix: dwdiag cores on --dir <dir>" ;;
	*)
		BUNDLE_CORE_DIR=$(dirname "$BUNDLE_CORE_PATTERN")
		if [ ! -d "$BUNDLE_CORE_DIR" ]; then
			bundle_missing core-capture-allowed "core_pattern is '$BUNDLE_CORE_PATTERN' but its directory $BUNDLE_CORE_DIR does not exist, so a core cannot be written"
		fi
		;;
	esac
	# A baseline of the cores that already exist, so the post-run scan reports only THIS run's.
	if [ -n "$BUNDLE_CORE_DIR" ] && [ -d "$BUNDLE_CORE_DIR" ]; then
		ls -1 "$BUNDLE_CORE_DIR" 2>/dev/null > "$BUNDLE_DIR/cores.baseline"
	fi
	# The child's environment and descriptor table exist only while the child is alive, and ONLY the HOST can read
	# them: inside the guest, /proc/<child>/fd and /proc/<child>/environ return nothing (the guest's /proc is a view
	# of the guest's own address space). So they are sampled during the run by `bundle_watch_children`, and the row
	# describing the result is written at the END (a row here would be a claim about a run that has not happened).
	: > "$BUNDLE_CHILD_DIR/pids.txt"
	return 0
}

# Sample the prefix-owned processes seen so far, once per call: every pid whose environment/fd table has not been
# recorded yet gets one. This is the answer to "which file did the workload's stdout go to" -- the guest cannot read
# its own /proc, so without this every investigation re-derived it by hand.
bundle_watch_children() {
	[ "$BUNDLE" = 1 ] || return 0
	[ -d "$BUNDLE_CHILD_DIR" ] || return 0
	WITNESS_QUIET=1 owned_pids | while read -r pid; do
		[ -n "$pid" ] || continue
		[ -e "$BUNDLE_CHILD_DIR/$pid.environ" ] && continue
		comm=$(cat "/proc/$pid/comm" 2>/dev/null)
		{
			echo "# host-side record of pid=$pid, captured the moment this harness first saw it"
			echo "# comm=$comm"
			echo "# exe=$(readlink "/proc/$pid/exe" 2>/dev/null || echo '<unreadable: the image file is gone>')"
			echo "# cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
			echo "# environ follows, one NAME=VALUE per line (read BY THE HOST: the guest's own /proc gives nothing)"
			tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null || echo "<unreadable>"
		} > "$BUNDLE_CHILD_DIR/$pid.environ" 2>/dev/null
		{
			echo "# host-side descriptor table of pid=$pid (fd -> what it points at)"
			ls -l "/proc/$pid/fd" 2>/dev/null || echo "<unreadable>"
			echo "# the workload's own stdout/stderr target, which is NOT the run log:"
			for n in 1 2; do
				echo "#   fd$n -> $(readlink "/proc/$pid/fd/$n" 2>/dev/null || echo '<unreadable>')"
			done
		} > "$BUNDLE_CHILD_DIR/$pid.fd" 2>/dev/null
		echo "$pid" >> "$BUNDLE_CHILD_DIR/pids.txt"
	done
	return 0
}

# The vocabulary of an abort anywhere in the run: the C++ runtime's terminate path, the loader's fatal-signal
# record, the server's panic, and the kernel's own trap line for a NATIVE guest fault. Copied WHOLE, never cut.
BUNDLE_ABORT_PATTERN='libc[+][+]abi|terminating with|terminate called|std\.terminate|uncaught exception|dserver-CRASH|\[sigexc-fatal|GUEST-FAULT|thread .* panicked|CORE-SIGNAL'

bundle_collect_abort_text() {
	out="$BUNDLE_DIR/abort.txt"
	{
		echo "# The abort/terminate text of this run, from EVERY file the bundle holds (the harness log, the server's"
		echo "# run-scoped log, and every guest capture). Lines are copied in full: the bound is the line's own"
		echo "# length, and the longest line found is reported in the MANIFEST for exactly that reason."
	} > "$out"
	scanned=0
	lines=0
	longest=0
	for f in "$LOG" "$BUNDLE_DIR"/*.log "$BUNDLE_DIR"/*.log.1 \
		"$BUNDLE_DIR"/*.log.post "$BUNDLE_DIR"/captures/*; do
		[ -f "$f" ] || continue
		# The run log is scanned through its collected copy, so it is not scanned twice when both exist.
		[ "$f" = "$LOG" ] && [ -f "$BUNDLE_DIR/harness.log" ] && continue
		[ "$f" = "$BUNDLE_DIR/witness-input.log" ] && continue
		scanned=$((scanned + 1))
		hits=$(grep -aEc "$BUNDLE_ABORT_PATTERN" "$f" 2>/dev/null)
		[ -n "$hits" ] || hits=0
		[ "$hits" -gt 0 ] || continue
		grep -aEn "$BUNDLE_ABORT_PATTERN" "$f" 2>/dev/null | while IFS= read -r l; do
			printf '%s: %s\n' "$f" "$l"
		done >> "$out"
		lines=$((lines + hits))
		# The longest matching line: `wc -L` is the file's longest line and is close enough for the report here,
		# which exists to show the bound is not a constant.
		l=$(grep -aE "$BUNDLE_ABORT_PATTERN" "$f" 2>/dev/null | wc -L | tr -d ' ')
		[ -n "$l" ] || l=0
		[ "$l" -gt "$longest" ] && longest=$l
	done
	if [ "$lines" -gt 0 ]; then
		bundle_present abort-text "$out" "$lines matching line(s) in $scanned scanned file(s); longest=${longest} bytes (lines copied whole, no fixed cap)"
	elif [ "$scanned" -gt 0 ]; then
		bundle_missing abort-text "searched $scanned file(s) of this run (the run log, run-scoped server bytes, all captures) and no abort/terminate line was there: the run reached its end without one"
	else
		bundle_missing abort-text "there was nothing to search: no run log and no captures were collected"
	fi
	return 0
}

bundle_finish() {
	[ "$BUNDLE" = 1 ] || return 0
	[ -n "$BUNDLE_MANIFEST" ] || return 0
	# THE HARNESS LOG, complete as far as this run goes.
	if [ -s "$LOG" ]; then
		cp -f "$LOG" "$BUNDLE_DIR/harness.log"
		bundle_present harness-log "$BUNDLE_DIR/harness.log" "the run log this console was reading ($(bundle_bytes "$BUNDLE_DIR/harness.log") bytes)"
	else
		bundle_present harness-log "$LOG" "the run log is empty: the guest printed nothing at all (this is a fact about the run, not a missing file)"
	fi
	# Capture finite old-FD tail and, on rotation, the new path's bytes. A reset or lost boundary is a failure,
	# not a negative count or a claim that all server records survived.
	if [ "$BUNDLE_SERVER_READY" = 1 ] && python3 "$SCRIPT_DIR/darling-evidence.py" log-finish \
		--source "$BUNDLE_SERVER_LOG" --state "$BUNDLE_SERVER_STATE" \
		--original "/proc/$$/fd/9" --output "$BUNDLE_DIR/server.log"; then
		bundle_present server-log "$BUNDLE_DIR/server.log" "only this run's finite server bytes; rotation/reset/missing transitions and capture boundaries in $BUNDLE_SERVER_STATE; writes after collection are outside this measurement"
	else
		BUNDLE_FAILED=1
		bundle_missing server-log-completeness "log collection failed or reported a possible gap: $BUNDLE_SERVER_STATE; any retained server.log is partial, not complete"
		[ ! -f "$BUNDLE_DIR/server.log" ] || bundle_present server-log "$BUNDLE_DIR/server.log" "PARTIAL server bytes retained; see server-log-completeness and $BUNDLE_SERVER_STATE"
	fi
	exec 9<&-
	# OTHER PREFIX LOGS THIS RUN TOUCHED (launchd, fcdaemon, ...): the plural of "the prefix server log(s)".
	other=0
	for f in "$PREFIX"/private/var/log/*.log; do
		[ -f "$f" ] || continue
		[ "$f" = "$PREFIX/private/var/log/dserver.log" ] && continue
		[ "$f" -nt "$BUNDLE_STAMP" ] || continue
		cp -f "$f" "$BUNDLE_DIR/$(basename "$f").post" 2>/dev/null || continue
		other=$((other + 1))
		bundle_present "prefix-log-$(basename "$f")" "$BUNDLE_DIR/$(basename "$f").post" "another prefix log written during this run"
	done
	[ "$other" -gt 0 ] || bundle_missing prefix-log-other "no other log under $PREFIX/private/var/log was written during this run (the server log above is the one this run used)"
	# LAUNCHD'S OWN DEBUG LOG, deliberately: MEASURED, the answer to "which service does the workload actually
	# resolve, which look-ups fail, and which process asked" was in launchd's log -- a DIFFERENT process's log from
	# the one under test -- and nothing collected it or said where it was. It records every Mach-service look-up
	# WITH the asking process. Rotated files are taken too, because a long run crosses the rotation.
	launchd_dir="$PREFIX/private/var/log/com.apple.launchd"
	launchd_any=0
	for f in "$launchd_dir"/launchd-debug*.log "$launchd_dir"/launchd-debug*.log.1; do
		[ -f "$f" ] || continue
		cp -f "$f" "$BUNDLE_DIR/$(basename "$f")" 2>/dev/null || continue
		launchd_any=$((launchd_any + 1))
		bundle_present "launchd-log-$(basename "$f")" "$BUNDLE_DIR/$(basename "$f")" "launchd's own debug log ($(bundle_bytes "$BUNDLE_DIR/$(basename "$f")") bytes): it records every Mach-service look-up WITH the asking process, so the service a workload resolved (and the ones that failed) can be named from HERE"
	done
	lookups="$BUNDLE_DIR/launchd-service-lookups.txt"
	{
		echo "# Every Mach-service look-up launchd recorded for this run, with the asking process. The interesting"
		echo "# shape is: a service the workload asked for that FAILED to resolve, attributed to the process that asked."
	} > "$lookups"
	grep -ah 'Mach service lookup' "$launchd_dir"/launchd-debug*.log "$launchd_dir"/launchd-debug*.log.1 2>/dev/null \
		| sort | uniq -c | sort -rn | head -80 >> "$lookups"
	lookup_lines=$(grep -ac 'Mach service lookup' "$BUNDLE_DIR"/launchd-debug*.log 2>/dev/null | awk -F: '{s += $NF} END {print s + 0}')
	[ -n "$lookup_lines" ] || lookup_lines=0
	resolved=$(grep -ah 'Mach service lookup:' "$BUNDLE_DIR"/launchd-debug*.log 2>/dev/null | wc -l | tr -d ' ')
	failed=$(grep -ah 'Mach service lookup failed' "$BUNDLE_DIR"/launchd-debug*.log 2>/dev/null | wc -l | tr -d ' ')
	if [ "$launchd_any" -gt 0 ]; then
		bundle_present launchd-service-lookups "$lookups" "$resolved resolved and $failed failed look-up line(s) in launchd's log ($lookup_lines total including any other shape)"
	else
		bundle_missing launchd-service-lookups "no launchd debug log under $launchd_dir is readable: without it 'which service did the workload resolve, and which look-up failed' cannot be answered from this bundle at all"
	fi
	# EVERY GUEST-PROCESS CAPTURE, WITH ITS PATH. The guest's xtrace writes one capture per process, named
	# `<arm-out-prefix>.trace.<pid>` (the arm itself) or `<arm-out-prefix>.traceg.<pid>` (its children) under the
	# prefix's guest tmp -- and NOTHING used to announce that, so a reader grepped the arm's stdout for a trace and
	# concluded there was none. Both suffixes are scanned, the base directories are named, and every path found is
	# PRINTED. A path the guest's own output announces is honoured too, if a future xtrace announces it.
	captures=0
	announced=$(grep -aoE 'XTRACE-CAPTURE path=[^ ]+' "$LOG" 2>/dev/null | sed 's/^XTRACE-CAPTURE path=//' | sort -u)
	for d in "$PREFIX/private/var/tmp" "$PREFIX/private/tmp" "$PREFIX/tmp"; do
		# BOTH SUFFIXES xtrace writes (`.trace.<pid>` for the traced process, `.traceg.<pid>` for its children) AND
		# the workload's own redirected stream (`prd-<arm>.out`). MEASURED (darling-diagnostics): the guest's own
		# output does NOT reach the run log -- a launcher redirects the workload's fd 1/2 into a file under the
		# prefix -- so a bundle that collected only the harness log would be missing the workload's own words.
		for f in "$d"/*.trace.* "$d"/*.traceg.* "$d"/*.out; do
			[ -f "$f" ] || continue
			[ "$f" -nt "$BUNDLE_STAMP" ] || continue
			announced="$announced
$f"
		done
	done
	for f in $announced; do
		[ -f "$f" ] || continue
		base=$(basename "$f")
		cp -f "$f" "$BUNDLE_CAPTURE_DIR/$base" 2>/dev/null || { bundle_missing "guest-capture-$base" "$f exists but could not be copied ($(bundle_bytes "$f") bytes)"; continue; }
		captures=$((captures + 1))
		bundle_present "guest-capture-$base" "$BUNDLE_DIR/captures/$base" "captured from $f ($(bundle_bytes "$BUNDLE_DIR/captures/$base") bytes): the child's own in-path records for this run"
	done
	if [ "$captures" -gt 0 ]; then
		BUNDLE_MANIFEST_ROW guest-captures COUNT "$BUNDLE_CAPTURE_DIR" "$captures capture(s) written during this run and younger than its start stamp"
	else
		bundle_missing guest-captures "no *.trace.<pid>/*.traceg.<pid> capture under $PREFIX/private/var/tmp (or private/tmp, or tmp) is younger than this run's start stamp, and no XTRACE-CAPTURE line announced one: either xtrace was not enabled for this run (an instrument that is off is not an instrument that stayed silent) or no guest process was traced"
	fi
	# THE CHILDREN THE HOST SAW. Their environment and descriptor tables, read from the HOST while they were alive:
	# this is the answer to "which file did the workload's stdout go to", which the guest cannot answer about itself.
	child_count=$(wc -l < "$BUNDLE_CHILD_DIR/pids.txt" 2>/dev/null | tr -d ' ')
	[ -n "$child_count" ] || child_count=0
	if [ "$child_count" -gt 0 ]; then
		BUNDLE_MANIFEST_ROW child-records COUNT "$BUNDLE_CHILD_DIR" "$child_count guest process(es) sampled from the HOST while alive; one <pid>.environ and <pid>.fd each (the guest's own /proc gives nothing for these)"
	else
		bundle_missing child-records "no prefix-owned process was observed by this harness during the run, so no child environment/descriptor table could be read. The sampling runs every 2 s while the harness waits, so a process that neither appeared nor was alive at a sample is not seen here; its own capture (if xtrace was on) still is"
	fi
	# ANY CORE WRITTEN DURING THIS RUN.
	cores=0
	if [ -n "$BUNDLE_CORE_DIR" ] && [ -d "$BUNDLE_CORE_DIR" ]; then
		for f in "$BUNDLE_CORE_DIR"/*; do
			[ -f "$f" ] || continue
			grep -qF "$(basename "$f")" "$BUNDLE_DIR/cores.baseline" 2>/dev/null && continue
			ln -sf "$f" "$BUNDLE_CORES_DIR/$(basename "$f")" 2>/dev/null
			cores=$((cores + 1))
			bundle_present "core-$(basename "$f")" "$BUNDLE_CORES_DIR/$(basename "$f")" "core written during this run: $f ($(bundle_bytes "$f") bytes); read it with 'dwdiag core --core $f' (this bundle symlinks it rather than copying ~1 GB)"
		done
	fi
	if [ "$cores" -eq 0 ]; then
		case "$BUNDLE_CORE_PATTERN" in
		"|"*) bundle_missing core "no core was written and none CAN be: core_pattern pipes cores to a handler ('$BUNDLE_CORE_PATTERN'). A fatal fault in this run left no core to read -- use 'dwdiag trap' (the kernel's own line in dmesg) or turn cores on with 'dwdiag cores on'" ;;
		"") bundle_missing core "core_pattern is unreadable, so it cannot be said whether a core would have been kept" ;;
		*) bundle_missing core "no new core under $BUNDLE_CORE_DIR during this run. Either nothing faulted fatally, or the process's own ulimit -c was 0 (MEASURED default on this host) -- the harness's own limit is $(ulimit -c), which is NOT the guest process's" ;;
		esac
	fi
	# THE COUNTERS THE SESSION COUNTS. The four the VERDICT block prints, plus the plane/ledger instruments the
	# rungs read by hand. Server counts read only the run-scoped captured bytes, never cumulative history.
	counters="$BUNDLE_DIR/counters.txt"
	{
		echo "# name<TAB>run-log<TAB>server-run<TAB>what it is"
		printf '%s\t%s\t%s\t%s\n' sockets-created "$(grep -ac 'rpc-socket. created' "$LOG" 2>/dev/null)" "-" "per-thread RPC sockets created"
		printf '%s\t%s\t%s\t%s\n' socket-denials "$(grep -ac 'rpc-socket-DENIED' "$LOG" 2>/dev/null)" "-" "RPC socket DENIED (the guest asked for a transport the server refused)"
		printf '%s\t%s\t%s\t%s\n' urgent-timeouts "$(grep -ac 'urgent-wait-TIMEOUT' "$LOG" 2>/dev/null)" "-" "an urgent call waited past its bound"
		printf '%s\t%s\t%s\t%s\n' courier-misses "$(grep -ac 'fd-courier-recv. MISS' "$LOG" 2>/dev/null)" "-" "a descriptor courier receive found nothing"
		printf '%s\t%s\t%s\t%s\n' plane-publishes "$(grep -ac 'plane-publish' "$LOG" 2>/dev/null)" "$(bundle_server_count 'plane-publish')" "plane operations PUBLISHED by the guest"
		printf '%s\t%s\t%s\t%s\n' plane-doorbell-sent "$(grep -ac 'plane-doorbell-sent' "$LOG" 2>/dev/null)" "$(bundle_server_count 'plane-doorbell-sent')" "doorbells the SERVER sent (server log only)"
		printf '%s\t%s\t%s\t%s\n' plane-refuse "$(grep -ac 'plane-refuse' "$LOG" 2>/dev/null)" "$(bundle_server_count 'plane-refuse')" "plane operations REFUSED"
		printf '%s\t%s\t%s\t%s\n' lane-release "$(grep -ac 'lane-release' "$LOG" 2>/dev/null)" "$(bundle_server_count 'lane-release')" "lane releases"
		printf '%s\t%s\t%s\t%s\n' plane-republish "$(grep -ac 'plane-republish' "$LOG" 2>/dev/null)" "$(bundle_server_count 'plane-republish')" "plane re-publishes"
		printf '%s\t%s\t%s\t%s\n' dserver-crash "$(grep -ac 'dserver-CRASH' "$LOG" 2>/dev/null)" "$(bundle_server_count 'dserver-CRASH')" "the server's own crash record"
	} > "$counters"
	bundle_present counters "$counters" "the four counters the VERDICT prints plus the plane/ledger instruments; server column counts server.log only; UNKNOWN means no captured server bytes"
	# THE INSTRUMENT CENSUS, WHICH NAMES THE SILENT ONES. `dwdiag witness` owns the instrument table; a hand-written
	# grep finds the line it was written for and cannot say which OTHER instrument never spoke.
	witness_out="$BUNDLE_DIR/witness.txt"
	# Give the census both owned streams without permitting prefix-based cumulative discovery.
	cat "$BUNDLE_DIR/harness.log" "$BUNDLE_DIR/server.log" > "$BUNDLE_DIR/witness-input.log" 2>/dev/null
	bundle_present witness-input "$BUNDLE_DIR/witness-input.log" "census input: this run's harness and server bytes only; server collection limits remain in $BUNDLE_SERVER_STATE"
	if [ -x "$SCRIPT_DIR/dwdiag" ]; then
		if "$SCRIPT_DIR/dwdiag" witness --log "$BUNDLE_DIR/witness-input.log" > "$witness_out" 2>"$BUNDLE_DIR/witness.err"; then
			silent=$(sed -n 's/^WITNESS-SILENT //p' "$witness_out" | tr '\n' ',')
			fired=$(sed -n 's/^WITNESS log=.* fired=\([0-9]*\)$/\1/p' "$witness_out" | tail -1)
			bundle_present witness-census "$witness_out" "instruments fired=${fired:-?}; SILENT=[${silent%,}] -- an instrument with zero hits is NAMED here, never inferred from silence"
		else
			bundle_missing witness-census "dwdiag witness failed (rc=$?); see $BUNDLE_DIR/witness.err. The census cannot be taken offline, so 'which instrument stayed silent' is UNKNOWN for this run rather than empty"
		fi
	else
		bundle_missing witness-census "no executable $SCRIPT_DIR/dwdiag: the instrument census cannot be taken, so 'which instrument stayed silent' is UNKNOWN for this run rather than empty"
	fi
	# Instruments that are silent BY BUILD are not the same as instruments that broke: state which the run itself
	# declared unavailable, so the census above cannot be read as "these guards did nothing".
	decl="$BUNDLE_DIR/build-declarations.txt"
	grep -ahE 'unavailable \(|records no |not carried by this build|this server build' "$LOG" "$BUNDLE_DIR"/server.log 2>/dev/null | sort -u | head -40 > "$decl"
	declared=$(wc -l < "$decl" 2>/dev/null | tr -d ' ')
	[ -n "$declared" ] || declared=0
	if [ "$declared" -gt 0 ]; then
		bundle_present build-declarations "$decl" "$declared statement(s) in which this BUILD says a section or instrument is absent/unavailable -- a silent instrument whose instrument is declared unavailable here is SILENT-BY-BUILD, not broken"
	else
		bundle_missing build-declarations "the run's logs carry no 'unavailable'/this-server-build statement, so no instrument can be excused as SILENT-BY-BUILD: every silent instrument above is a real silence for this run"
	fi
	bundle_collect_abort_text
	return 0
}

# Count only captured run bytes. A missing stream is unknown, not a measured zero.
bundle_server_count() {
	if [ ! -f "$BUNDLE_DIR/server.log" ] || { [ "$BUNDLE_FAILED" != 0 ] && [ ! -s "$BUNDLE_DIR/server.log" ]; }; then
		echo UNKNOWN
		return 0
	fi
	grep -ac "$1" "$BUNDLE_DIR/server.log" 2>/dev/null || true
}

# No reader runs after this boundary: verdict/counters/witness/stop classification and the manifest are final.
# Only bundle-owned copies are eligible; the ongoing external $LOG and prefix logs are never passed here.
bundle_finalize() {
	[ "$BUNDLE" = 1 ] || return 0
	if printf '{"state":"%s","processes":%s,"mounts":%s,"layer_mounts":%s,"shutdown_rc":%s,"launcher_rc":%s,"rescue_rc":%s,"rescue_processes":%s,"external_rescue":%s}\n' \
		"$CLEANUP_STATE" "$CLEANUP_PROCESSES" "$CLEANUP_MOUNTS" "$CLEANUP_LAYER_MOUNTS" \
		"$CLEANUP_SHUTDOWN_RC" "$LAUNCHER_RC" "$CLEANUP_RESCUE_RC" "$CLEANUP_RESCUE_PROCESSES" "$CLEANUP_EXTERNAL_RESCUE" > "$BUNDLE_DIR/cleanup.json"; then
		bundle_present cleanup "$BUNDLE_DIR/cleanup.json" "final prefix teardown observations; retained runtimes have null measurements, not measured zeros"
	else
		BUNDLE_FAILED=1
		verdict=1
		bundle_missing cleanup "could not write final teardown observations"
	fi
	printf 'verdict=%s\n' "$verdict" > "$BUNDLE_DIR/verdict.txt"
	bundle_present verdict "$BUNDLE_DIR/verdict.txt" "harness outcome including collection and cleanup; workload status declarations remain in the console/run log"
	if [ "$BUNDLE_FAILED" = 0 ]; then
		printf 'BUNDLE-COLLECTED %s\n' "$(date -Is)" >> "$BUNDLE_MANIFEST"
	else
		printf 'BUNDLE-PARTIAL %s collection-failed-or-possible-gap\n' "$(date -Is)" >> "$BUNDLE_MANIFEST"
	fi
	bundle_report
	if ! python3 "$SCRIPT_DIR/darling-evidence.py" compress --recursive "$BUNDLE_DIR"; then
		BUNDLE_FAILED=1
		verdict=1
		: > "$BUNDLE_DIR/INCOMPLETE"
		printf 'verdict=%s\n' "$verdict" > "$BUNDLE_DIR/verdict.txt"
		bundle_missing evidence-storage "compression failed; uncompressed evidence is retained on failure; inspect per-directory evidence-index*.json for any successful earlier publications"
		printf 'BUNDLE-FINALIZATION-FAILED %s\n' "$(date -Is)" >> "$BUNDLE_MANIFEST"
		echo "BUNDLE-STORAGE-FAILED dir=$BUNDLE_DIR" >&2
	fi
	if [ "$BUNDLE_FAILED" = 0 ]; then
		printf 'BUNDLE-COMPLETE %s\n' "$(date -Is)" >> "$BUNDLE_MANIFEST"
		rm -f "$BUNDLE_DIR/INCOMPLETE"
		echo "BUNDLE-COMPLETE dir=$BUNDLE_DIR"
	fi
}

# THE CONSOLE BLOCK: every path, every missing item and the census, printed as part of the run's own output. A bundle
# a reader cannot find is a bundle that does not exist.
bundle_report() {
	[ "$BUNDLE" = 1 ] || return 0
	echo "== bundle =="
	echo "BUNDLE dir=$BUNDLE_DIR"
	echo "BUNDLE-MANIFEST $BUNDLE_MANIFEST"
	if [ -f "$BUNDLE_DIR/INCOMPLETE" ]; then
		if [ "$BUNDLE_FAILED" = 0 ]; then
			echo "BUNDLE-FINALIZING storage finalization pending; only a later BUNDLE-COMPLETE confirms completion"
		else
			echo "BUNDLE-INCOMPLETE collection has missing boundaries/possible gaps: this bundle is PARTIAL, not a complete one"
		fi
	fi
	# One line per manifest row: paths are printed, MISSING rows carry the reason. A reader never has to guess.
	awk -F'\t' '
		$2 == "PRESENT" { printf "BUNDLE-ITEM %s path=%s%s\n", $1, $3, ($4 != "" ? " note=" $4 : "") }
		$2 == "COUNT"   { printf "BUNDLE-COUNT %s path=%s %s\n", $1, $3, $4 }
		$2 == "MISSING" { printf "BUNDLE-MISSING %s reason=%s\n", $1, $4 }
	' "$BUNDLE_MANIFEST"
	# The census on its own line, because "which instrument stayed silent" must be readable without opening a file.
	silent=$(sed -n 's/^WITNESS-SILENT //p' "$BUNDLE_DIR/witness.txt" 2>/dev/null | tr '\n' ',')
	fired=$(sed -n 's/^WITNESS log=.* fired=\([0-9]*\)$/\1/p' "$BUNDLE_DIR/witness.txt" 2>/dev/null | tail -1)
	if [ -f "$BUNDLE_DIR/witness.txt" ]; then
		echo "BUNDLE-WITNESS fired=${fired:-?} silent=[${silent%,}]"
	fi
	if [ -n "$BUNDLE_RUN_ENV" ]; then
		echo "BUNDLE-RUN-ENV $BUNDLE_RUN_ENV"
	fi
	if [ -f "$BUNDLE_DIR/counters.txt" ]; then
		echo "BUNDLE-COUNTERS $(awk -F'\t' '$1=="sockets-created"||$1=="socket-denials"||$1=="urgent-timeouts"||$1=="courier-misses" { printf "%s=%s ", $1, $2 }' "$BUNDLE_DIR/counters.txt")"
	fi
	# The launchd look-up lines are QUOTED here, not just counted: the measured use of that log is naming the
	# service a workload resolved (and the ones that failed), which is a line, not a number.
	if [ -f "$BUNDLE_DIR/launchd-service-lookups.txt" ]; then
		echo "BUNDLE-LAUNCHD the services launchd recorded for this run (top 5 by count; full list in $BUNDLE_DIR/launchd-service-lookups.txt):"
		grep -v '^#' "$BUNDLE_DIR/launchd-service-lookups.txt" | head -5 | sed 's/^/BUNDLE-LAUNCHD   /'
	fi
	return 0
}

observe_launcher() {
	[ "$LAUNCHER_RC" = null ] || return 0
	[ -n "$LAUNCHER_PID" ] || return 0
	# Observe existence without signaling or consuming the shell's saved status.
	# Running and stopped children both remain present; only wait after exit.
	# jobs filters are not POSIX, and reporting completed jobs can discard their rc.
	ps -p "$LAUNCHER_PID" -o pid= >/dev/null 2>&1 && return 0
	wait "$LAUNCHER_PID"
	LAUNCHER_RC=$?
	printf 'LAUNCHER-EXIT rc=%s\n' "$LAUNCHER_RC" | tee -a "$LOG"
}
# STOP-REASON: the run's OWN evidence names the stop, in one line, before anyone greps for it.
# MEASURED 2026-10-19: an xcodebuild workload died with rc=132 (SIGILL) and no artifact; the missing Swift
# symbol, its referrer and the image that should have supplied it were ALREADY inside this bundle's captures/,
# and the cause was still found by hand-grepping 27 KB -- because the bundle was reported, not read. The
# classifier reads the log plus the bundle's captures/abort text; a passing run prints kind=none, which is a
# statement about the evidence, never about the verdict.
report_stop_reason() {
STAGE=stop-reason
if [ -x "$SCRIPT_DIR/darling-stop-reason.sh" ]; then
	_sr_extra=""
	[ -n "$BUNDLE_DIR" ] && [ -d "$BUNDLE_DIR" ] && _sr_extra="$_sr_extra --bundle=$BUNDLE_DIR"
	[ -n "$LAST_STATUS_VALUE" ] && _sr_extra="$_sr_extra --rc=$LAST_STATUS_VALUE"
	# A later zero field or presence oracle cannot erase an earlier failed status/check.
	[ "$verdict" = 0 ] || _sr_extra="$_sr_extra --runner-rc=$verdict"
	# RUN SCOPING. The prefix's captures and its server log are CUMULATIVE: they hold earlier runs' records, and a
	# matching prefix is not run identity. The classifier admits a capture only when it is at or after this run's
	# own start stamp, counts what it excluded, and says so.
	[ -n "$BUNDLE_STAMP" ] && [ -f "$BUNDLE_STAMP" ] && _sr_extra="$_sr_extra --run-start=$(stat -c %Y "$BUNDLE_STAMP" 2>/dev/null || echo 0)"
	_sr_extra="$_sr_extra --run-id=$(basename "$LOG")"
	# A caller-declared PRESENCE marker is an explicit success oracle: without one, a run whose statuses are both
	# absent is INCONCLUSIVE and must not be called a pass. A `FIELD=` marker is a status declaration judged on
	# its own above, so it is not passed here as an oracle.
	_sr_old_ifs=$IFS; IFS='|'
	for _m in $MARKERS; do
		[ -n "$_m" ] || continue
		case "$_m" in *=) ;; *) _sr_extra="$_sr_extra --oracle=$_m" ;; esac
	done
	IFS=$_sr_old_ifs
	# The workspace paths carry no spaces (documented in that script), so splitting _sr_extra is safe here.
	_stop=$("$SCRIPT_DIR/darling-stop-reason.sh" --log="$LOG" $_sr_extra 2>/dev/null) || true
	if [ -n "$_stop" ]; then
		printf '%s\n' "$_stop"
		printf '%s\n' "$_stop" >> "$LOG"
		[ -n "$BUNDLE_DIR" ] && [ -d "$BUNDLE_DIR" ] && printf '%s\n' "$_stop" > "$BUNDLE_DIR/stop-reason.txt"
	fi
else
	echo "STOP-REASON kind=unknown note=no $SCRIPT_DIR/darling-stop-reason.sh"
fi
}
cleanup_runtime() {
STAGE=cleanup; echo "== cleanup =="
witness "TEARDOWN stage=shutdown-enter self=$$ parent=$PPID harness=$(caller_id $$)"
"$DARLING_LAUNCHER" shutdown >/dev/null 2>&1 8<&-
CLEANUP_SHUTDOWN_RC=$?
witness "TEARDOWN stage=shutdown-exit rc=$CLEANUP_SHUTDOWN_RC self=$$"
sleep 3
rescue_processes=$(count_owned) || exit $?
if [ "$CLEANUP_SHUTDOWN_RC" -ne 0 ] || [ "$rescue_processes" -ne 0 ]; then
	echo "cleanup FAILED: product shutdown rc=$CLEANUP_SHUTDOWN_RC, rescue processes=$rescue_processes" >&2
	verdict=1
fi
rescue_owned
CLEANUP_RESCUE_RC=$?
CLEANUP_RESCUE_PROCESSES=$rescue_processes
if [ "$CLEANUP_RESCUE_RC" -ne 0 ] || [ "$rescue_processes" -ne 0 ]; then
	verdict=1
fi
case "$CLEANUP_RESCUE_RC" in 3|4) CLEANUP_EXTERNAL_RESCUE=true ;; esac
final=$(count_owned) || exit $?
# A runtime with only a matching environment marker cannot be signalled, but
# it is still residue. Do not publish zero merely because kill authority is absent.
residual=$(darling_prefix_residual_pids "$PREFIX") || exit $?
if [ -n "$residual" ]; then
	final=$(printf '%s\n' "$residual" | wc -l)
fi
# Count mounts whose MOUNTPOINT lies under the prefix. A substring match was MEASURED to fail here:
# another lane's overlay instance names this prefix in its lowerdir
# (.west-prefix-instances/probe1/merged lowerdir=<prefix>), which is not a mount under the prefix, yet
# it made every run print "mounts: 1" and clean up as VERDICT: FAIL while the guest verdict was PASS.
# A mount AT the prefix path is excluded only when it is a FUSE overlay: that is the isolation layer the
# run looks THROUGH (an isolation lane, or a fixture's own instance), not residue the run left behind.
# MEASURED 2026-10-18: counting it made every lane run report "mounts: 1" and VERDICT: FAIL with a PASSing
# guest. A kernel `overlay` at the prefix stays counted: in the privileged runtime THAT is the server's own
# bring-up mount, and leaving one behind after shutdown is real residue.
mount_counts=$(darling_prefix_mount_counts "$PREFIX") || exit $?
read mounts layer_mounts <<EOF
$mount_counts
EOF
# Same rule after the run: leave the prefix as a *bootable* clean state, not merely a process-free one.
if [ "$final" -eq 0 ] && [ "$mounts" -eq 0 ]; then
	clear_stale_runtime_files
fi
echo "prefix processes after: $final, mounts: $mounts (plus $layer_mounts prefix layer mount(s))"
CLEANUP_STATE=clean
if [ "$CLEANUP_EXTERNAL_RESCUE" = true ]; then
	CLEANUP_STATE=rescued
elif [ "$CLEANUP_RESCUE_RC" -ne 0 ]; then
	CLEANUP_STATE=unavailable
elif [ "$CLEANUP_SHUTDOWN_RC" -ne 0 ]; then
	CLEANUP_STATE=failed-shutdown
fi
CLEANUP_PROCESSES=$final
CLEANUP_MOUNTS=$mounts
CLEANUP_LAYER_MOUNTS=$layer_mounts
if [ "$final" -gt 0 ] || [ "$mounts" -gt 0 ]; then
	echo "cleanup FAILED" >&2
	CLEANUP_STATE=residue
	verdict=1
fi
observe_launcher
if [ "$LAUNCHER_RC" != null ] && [ "$LAUNCHER_RC" -ne 0 ]; then
	verdict=1
fi
}

HARNESS_STARTED_AT=$(date +%s)
STAGE=harness-start
handle_signal() {
	trap '' TERM INT
	_signal_rc=$1
	witness "HARNESS-SIGNAL sig=$2 stage=$STAGE elapsed=$(($(date +%s) - HARNESS_STARTED_AT))s self=$$ parent=$PPID"
	verdict=1
	LAST_STATUS_VALUE=""
	if [ -n "$BUNDLE_MANIFEST" ]; then
		bundle_finish
	fi
	cleanup_runtime
	if [ -n "$BUNDLE_MANIFEST" ]; then
		report_stop_reason
		bundle_finalize
	fi
	exit "$_signal_rc"
}

# A mode that must NOT boot anything: report what this harness would treat as prefix-owned, then exit. The ownership
# rule is what decides whether the next boot starts clean, and a rule that can only be exercised by booting cannot be
# tested; tests/run-darling-boot-harness-ownership-contract.sh drives this mode with synthetic processes.
if [ "$LIST_OWNED" = 1 ]; then
	owned_pids || exit $?
	report_stale_runtime_files
	exit 0
fi
trap 'handle_signal 143 15' TERM
trap 'handle_signal 130 2' INT

STAGE=clean-start; echo "== clean start =="
witness "TEARDOWN stage=shutdown-enter self=$$ parent=$PPID harness=$(caller_id $$)"
start_shutdown_rc=0
start_processes=$(count_owned) || exit $?
if [ "$start_processes" -ne 0 ]; then
	"$DARLING_LAUNCHER" shutdown >/dev/null 2>&1 8<&-
	start_shutdown_rc=$?
fi
witness "TEARDOWN stage=shutdown-exit rc=$start_shutdown_rc self=$$"
sleep 3
start_rescue=$(count_owned) || exit $?
# Only the shared owner census and pinned/revalidated signal API authorize rescue.
rescue_owned
start_rescue_rc=$?
left=$(count_owned) || exit $?
if [ "$start_shutdown_rc" -ne 0 ] || [ "$start_rescue" -ne 0 ] || [ "$start_rescue_rc" -ne 0 ]; then
	echo "clean start FAILED: product shutdown rc=$start_shutdown_rc, rescue processes=$start_rescue rc=$start_rescue_rc; refusing acceptance after rescue" >&2
	exit 1
fi
if [ "$left" -gt 0 ]; then
	echo "clean start FAILED: $left prefix-owned processes remain after verified rescue" >&2
	owned_pids | head -5 >&2
	exit 1
fi
start_residual=$(darling_prefix_residual_pids "$PREFIX") || exit $?
if [ -n "$start_residual" ]; then
	echo "clean start FAILED: unverified prefix runtime residue remains; markers do not authorize rescue: $start_residual" >&2
	exit 1
fi
echo "clean: 0 prefix-owned processes, starting"
# No process owns the prefix, so the runtime files a killed run left cannot be in use: clear them BEFORE the boot,
# otherwise the new launcher meets a socket/init-pid from a server that no longer exists and never reaches
# shellspawn readiness. The witness names each removal, because a silent delete is indistinguishable from a hang.
clear_stale_runtime_files

# A probe that is not in the artifact cannot fire, and a probe that is in the artifact but not in the log cannot
# be concluded from either. Verify presence in the DEPLOYED copies first, so a silent run is never read as "the code
# path is not taken" when the real cause is that the probe was compiled into a unit the image does not link.
if [ -n "$VERIFY_PROBES" ]; then
	echo "== probe presence in deployed artifacts =="
	missing=0
	echo "$VERIFY_PROBES" | while IFS= read -r t; do
		[ -n "$t" ] || continue
		if "$SCRIPT_DIR/darling-artifact-manifest.sh" --runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$PREFIX" --probe "$t" | grep -q "PRESENT"; then
			"$SCRIPT_DIR/darling-artifact-manifest.sh" --runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$PREFIX" --probe "$t" | sed 's/^/  /'
		else
			echo "  $t: ABSENT FROM EVERY DEPLOYED ARTIFACT"
			exit 3
		fi
	done || missing=$?
	if [ "$missing" = 3 ]; then
		echo "probe presence FAILED: a probe on this tag cannot fire (see above)" >&2
		exit 1
	fi
fi

bundle_begin

echo "== run =="
echo "log: $LOG"
if [ "$HATCH" = 1 ]; then
	HATCH_ENV="DARLING_DISABLE_THREAD_RPC_UDS=1"
else
	HATCH_ENV=""
fi
: > "$LOG"
if [ -n "${WEST_JOB_STATE_DIR:-}" ]; then
	# The log was just reset; snapshot its zero boundary before the first byte.
	"$SCRIPT_DIR/west-job.sh" register --state-dir "$WEST_JOB_STATE_DIR" -- file "$LOG" || {
		echo "run evidence registration failed" >&2
		exit 2
	}
fi
# A RUN'S LOG MUST SAY WHICH PREFIX IT BELONGS TO. `dwdiag` resolves the SERVER log from a `prefix=` field in the
# run log itself (resolve_prefix in tools/darling-debug-runner reads the first `prefix=` token and trims `]`), and
# a `--prefix` on the command line does NOT override it for that purpose. Without this line every log-only tool fell
# back to a DEFAULT path and read ANOTHER prefix's dserver.log -- the same failure class as "a run served by another
# prefix's live runtime", and it is silent. MEASURED 2026-10-19: `dwdiag progress --log <run log> --prefix <P>`
# printed `SERVER-LOG-PATH /tmp/dr-on-matched/private/var/log/dserver.log (from DEFAULT ...)` and dropped every
# server-side fact while the prefix was known. The shape below is the one the tools already parse
# (`[dwdiag-env prefix=<path> ...]`), so ONE line makes the log self-describing for progress, verdict, crash and
# scripts/darling-stop-reason.sh. It is the first line, because the guest's own output is appended after it.
echo "[dwdiag-env prefix=$PREFIX runtime-root=$DARLING_RUNTIME_ROOT launcher=$DARLING_LAUNCHER log=$LOG bundle=$BUNDLE_DIR wait=$WAIT]" >> "$LOG"
# shellcheck disable=SC2086
# THE SHELL VERB, NOT exec. MEASURED: `--rootless shell /bin/sh -c ...` runs the command INSIDE the guest (a
# run on /tmp/dr-on-matched produced mldr-seed attach/seeded, dring-adopt and the command's own output, 5803
# lines, and `ls /private/var/tmp/ring_mach_msg_test` printed the path, so a fixture installed there IS visible),
# while `exec` hands the command to the HOST's shell (/Volumes/SystemRoot/usr/bin/dash reporting "not found" for
# every prefix-local fixture). An earlier edit spliced this comment into the MIDDLE of the command line, so the
# launcher was never started and every run ended in a prefix-state message: keep comments OUTSIDE the command.
# THE LAUNCHER INVOCATION MUST STAY ONE UNBROKEN COMMAND. MEASURED TWICE: an explanatory comment placed between
# the backslash-continued lines of this command is JOINED into it, the trailing backslash is swallowed, and the
# launcher is then executed WITHOUT the DPREFIX/DARLING_PREFIX environment -- so it cannot find the prefix state
# and answers "runtime prefix has no recognized stable state: recreation required", while the same launcher invoked
# by hand with those variables works. Keep every comment above this block.
# Guest transport facts (measured): the verb must be `shell` (`exec` hands the command to the HOST shell reached as
# /Volumes/SystemRoot/usr/bin/dash); the shell must be /bin/bash (a freshly bootstrapped guest has no /bin/sh and the
# failure reads "/bin/bash: /bin/sh: No such file or directory").
env $HATCH_ENV $EXTRA_ENV \
	timeout "$((WAIT + 60))" \
	"$DARLING_LAUNCHER" shell /bin/bash -c "$CMD" >> "$LOG" 2>&1 8<&- 9<&- &
LAUNCHER_PID=$!

# Wait for explicit --poll, or every explicit status field when no poll was supplied.
# Ordinary presence markers are not completion: they may open an observation window before the result.
STAGE=workload-wait
if [ "$POLL_STATUS" = 1 ]; then
	echo "waiting up to ${WAIT}s for all declared status fields: ${STATUS_MARKERS#|}"
else
	echo "waiting up to ${WAIT}s for the workload's own result line (poll='$POLL'; diagnostic lines before it are not verdicts)"
fi
_waited=0
while [ "$_waited" -lt "$WAIT" ]; do
	observe_launcher
	if [ "$LAUNCHER_RC" != null ] && [ "$LAUNCHER_RC" -ne 0 ]; then
		break
	fi
	if [ "$POLL_STATUS" = 1 ]; then
		_complete=1
		_poll_ifs=$IFS; IFS='|'
		for _field in $STATUS_MARKERS; do
			[ -n "$_field" ] || continue
			grep -aFq -- "$_field" "$LOG" 2>/dev/null || _complete=0
		done
		IFS=$_poll_ifs
	else
		_complete=0
		grep -aq -- "$POLL" "$LOG" 2>/dev/null && _complete=1
	fi
	[ "$_complete" = 0 ] || break
	# THE HOST-ONLY RECORD, taken while the child is alive. Every second poll (a 2 s resolution) so the sampling
	# cannot dominate the run. Whatever was missed is reported as a MISSING row with that granularity as the reason.
	if [ "$BUNDLE" = 1 ] && [ $((_waited % 2)) -eq 0 ]; then
		bundle_watch_children
	fi
	sleep 1
	_waited=$((_waited + 1))
done
observe_launcher
# The boundary reached is part of the LOG, not just of this console: a verdict rule that has to tell "the workload
# failed" from "we stopped measuring" reads the log, and a line that only ever reached a terminal cannot be judged
# later (MEASURED: the offline verdict could not see this and kept calling a watchdog stop a CRASH).
echo "waited ${_waited}s of at most ${WAIT}s" | tee -a "$LOG"

STAGE=verdict; echo "== verdict =="
verdict=0
if [ "$LAUNCHER_RC" != null ] && [ "$LAUNCHER_RC" -ne 0 ]; then
	verdict=1
	echo "producer failed: launcher rc=$LAUNCHER_RC (see $LOG)"
fi
# The workload's own status, kept for the STOP-REASON classifier below: 132 is a SIGILL, not a number to read.
LAST_STATUS_VALUE=""
# THE WORKLOAD'S OWN STATUS (dar-u4mk). A declaration that names a VALUE -- `--status-marker FIELD=`, a `--marker`
# ending in `=`, or a caller-supplied `--poll` ending in `=` -- asserts success only by an integer 0. The old rule
# tested presence with grep and never parsed the value, so `--marker 'XCB_RC='` was satisfied by the SUBSTRING in
# `XCB_RC=132` (128+4 = SIGILL, no artifact) and this block printed VERDICT: PASS for a workload that had died.
# Fail closed at every step below: absent, non-numeric and non-zero all end as FAIL.
literal_field() { # name -> 0 when it is a plain literal field name with no regex/shell metacharacters
	case "$1" in ''|*[!A-Za-z0-9_.:-]*) return 1 ;; esac
	return 0
}
# The integer that immediately follows FIELD= on the LAST log line that names it. Empty output means "no parseable
# value" (absent, non-numeric, or the digits are not adjacent to the field) and every caller treats that as FAIL.
# index() keeps the needle literal, so a field name is never reinterpreted as a regular expression here.
status_value() { # FIELD=
	grep -a -F -- "$1" "$LOG" 2>/dev/null | tail -1 | awk -v f="$1" '
	{
		best = ""; rest = $0
		while ((i = index(rest, f)) > 0) {
			tail = substr(rest, i + length(f))
			if (match(tail, /^[0-9]+/)) best = substr(tail, RSTART, RLENGTH)
			rest = tail
		}
		if (best != "") print best
	}'
}
STATUS_SEEN=""
check_status() { # DECL -> records the workload's own status; PASS only for the integer 0
	st="$1"
	case "$st" in *=) ;; *) st="$st=" ;; esac
	case " $STATUS_SEEN " in *" $st "*) return 0 ;; esac	# one field declared twice (marker + poll) is judged once
	STATUS_SEEN="$STATUS_SEEN $st"
	if ! literal_field "${st%=}"; then
		echo "STATUS AMBIGUOUS $st: it names a value, but not as a plain literal field; FAIL (fail closed)"
		verdict=1
		return 0
	fi
	v=$(status_value "$st")
	LAST_STATUS_VALUE="$v"
	if [ -z "$v" ]; then
		echo "STATUS MISS $st: no log line carries an integer immediately after it"
		verdict=1
	elif [ "$v" -ne 0 ]; then
		echo "STATUS FAIL $st value=$v"
		verdict=1
	else
		echo "STATUS ok   $st value=0"
	fi
	return 0
}
# A poll the CALLER supplied that names a value is judged the same way. The harness's own default poll is NOT a
# caller declaration and never becomes a status field -- for every caller that passes no --poll, the verdict is
# exactly what it was before.
if [ "$POLL_EXPLICIT" = 1 ]; then
	case "$POLL" in *=) check_status "$POLL" ;; esac
fi
# MEASURED: a marker containing spaces was split by the old space-separated list, so `--marker 'ITER 0 dropped'`
# became three markers and reported `MISS dropped` for something that was simply never queried as a whole.
# '|' keeps a marker intact; IFS is restored right after the loop.
oldIFS=$IFS; IFS='|'
for f in $STATUS_MARKERS; do
	[ -n "$f" ] || continue
	check_status "$f"
done
for m in $MARKERS; do
	[ -n "$m" ] || continue
	# A marker that names a value is a status field, not a presence check -- this is the XCB repro's own spelling.
	case "$m" in
	*=) check_status "$m"; continue ;;
	esac
	if grep -q -- "$m" "$LOG"; then
		echo "MARKER ok   $m"
	else
		echo "MARKER MISS $m"
		verdict=1
	fi
done
IFS=$oldIFS
echo "sockets created:   $(grep -c 'rpc-socket. created' "$LOG" 2>/dev/null)"
echo "socket denials:    $(grep -c 'rpc-socket-DENIED' "$LOG" 2>/dev/null)"
echo "urgent timeouts:   $(grep -c 'urgent-wait-TIMEOUT' "$LOG" 2>/dev/null)"
echo "courier misses:    $(grep -c 'fd-courier-recv. MISS' "$LOG" 2>/dev/null)"
echo "log lines:         $(wc -l < "$LOG")"

# THE BUNDLE IS COLLECTED BEFORE TEARDOWN, INCLUDING ON FREEZE: retain finite server bytes while the server
# is alive. Finalization/compression waits until every raw-evidence reader and classifier has finished.
STAGE=bundle
bundle_finish
[ "$BUNDLE_FAILED" = 0 ] || verdict=1

if [ "${FREEZE_ON_FAIL:-0}" -eq 1 ] && [ "$verdict" -ne 0 ]; then
	STAGE=freeze; echo "== freeze-on-fail (diagnostic) =="
	echo "FREEZE-ON-FAIL prefix=$PREFIX log=$LOG verdict=$verdict"
	echo "FREEZE-ON-FAIL inspect now, then clean up with: west darling-prefix-repair --prefix $PREFIX --cleanup-mounts"
	echo "FREEZE-ON-FAIL or: DPREFIX=$PREFIX DARLING_RUNTIME_ROOT=$DARLING_RUNTIME_ROOT $DARLING_LAUNCHER shutdown"
	CLEANUP_STATE=retained
	report_stop_reason
	bundle_finalize
	if [ "$LAUNCHER_RC" != null ] && [ "$LAUNCHER_RC" -ne 0 ]; then exit "$LAUNCHER_RC"; fi
	exit 1
fi

cleanup_runtime
report_stop_reason
bundle_finalize

if [ "$verdict" = 0 ]; then
	echo "VERDICT: PASS"
else
	echo "VERDICT: FAIL"
fi
if [ "$LAUNCHER_RC" != null ] && [ "$LAUNCHER_RC" -ne 0 ]; then
	exit "$LAUNCHER_RC"
fi
exit "$verdict"

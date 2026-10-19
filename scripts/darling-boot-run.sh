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
#   3. the workload's real duration: the caller states how long the workload needs, and the harness waits it;
#   4. a single VERDICT block at the end: the workload's own STATUS, the presence markers the caller asked for,
#      plus the counters that matter here (per-thread RPC socket creations, denials, urgent timeouts, courier misses);
#   5. a RUN BUNDLE, ON BY DEFAULT, with a console block that announces it.
#
# THE RUN BUNDLE (`--bundle DIR`, `--no-bundle`). One self-describing directory per run, `<log>.bundle` by default.
# It holds, and NAMES on stdout: both logs (the harness's own and the prefix server's, pre and post -- the server's
# stderr never appears in the run log), every guest-process capture found under the prefix with its path PRINTED
# (the xtrace naming `<arm-out-prefix>.trace.<pid>`/`.traceg.<pid>` is announced nowhere else), the child's
# environment and descriptor table read from the HOST while it was alive (a guest cannot read its own /proc), any
# core written during the run, the abort/terminate text lifted WHOLE from wherever it landed, the plane/ledger
# counters, the deployed sha256 of every runtime copy (the RUN-ENV line), and the instrument census that NAMES every
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
#
# Usage:
#   darling-boot-run.sh --prefix PATH --wait SECONDS [--marker NAME]... [--status-marker FIELD=] [--poll REGEX] [--hatch] [--cmd 'shell command'] [--bundle DIR|--no-bundle]
# Exit: 0 only if every declared status field was the integer 0 and every presence marker appeared, 1 otherwise (or if cleanup failed).

set -u

PREFIX=""
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
BUNDLE_SERVER_PRE=""
BUNDLE_RUN_ENV=""
BUNDLE_CORE_PATTERN=""
BUNDLE_CORE_DIR=""

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
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
		# log instead of sleeping out --wait. Default is the original fixture's line, so every
		# existing caller keeps the exact behaviour it had (see the poll loop below).
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
# Same default the poll loop always used, so an unspecified --poll cannot change any existing run.
[ -n "$POLL" ] || POLL='RING_MACH_TEST mode=.*pass='

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
[ -n "$MARKERS" ] || MARKERS="|HELLO=1|FINAL=1"
[ -x "$PREFIX/bin/darling" ] || { echo "no launcher at $PREFIX/bin/darling" >&2; exit 2; }

[ -n "$LOG" ] || LOG="/tmp/darling-boot-$(date +%H%M%S)-$$.log"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

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
# The harness records its OWN death too: MEASURED, a group-directed SIGTERM killed the harness and the tool that spawned
# it at the same instant, so neither of them reached the teardown witness and the run simply stopped. A trap turns
# "it stopped" into "sig=15 arrived at this point in the script".
STAGE=harness-start
trap 'witness "HARNESS-SIGNAL sig=15 stage=$STAGE elapsed=${SECONDS}s self=$$ parent=$PPID"; exit 143' TERM
trap 'witness "HARNESS-SIGNAL sig=2 stage=$STAGE elapsed=${SECONDS}s self=$$"; exit 130' INT
caller_id() { # pid ppid pgid sid cmd
	local p=$1 st
	st=$(sed 's/.*) //' "/proc/$p/stat" 2>/dev/null)
	echo "$p $(echo "$st" | awk '{print $2}') $(echo "$st" | awk '{print $3}') $(echo "$st" | awk '{print $4}') $(tr "\0" " " < "/proc/$p/cmdline" 2>/dev/null | cut -c1-120)"
}

owned_pids() {
	self=$$
	# space-separated: the membership test is `case " $ancestors " in *" $pid "*`, and a NEWLINE-separated list
	# never matches it -- MEASURED: with `$(own_ancestors)` the guard silently did nothing and the wrapper was killed
	# anyway (rc=137) three seconds in, which is exactly the kind of "instrument that cannot answer" this file keeps
	# recording. The separator is part of the test, not a cosmetic detail.
	ancestors=$(own_ancestors | tr '\n' ' ')
	# The prefix's OWN record of its current init/server. Two measured traps make this a required arm:
	# a killed run leaves the file behind, and the daemonized server it names re-parents to init with an argv of
	# bare numbers ("darlingserver 4 3 <prefix-basename> 6 1000 1000 8 0"), so neither the ancestor arm, nor the
	# exe arm (the loader's exe is under the prefix only for guest processes), nor the cmdline arm (which needs the
	# PREFIX PATH) can see it. MEASURED: exactly that left a prefix wedged and this check still printed
	# "clean: 0 prefix-owned processes", after which the next boot talked into the stale server's socket and never
	# reached shellspawn readiness.
	init_pid_value=""
	[ -r "$PREFIX/.init.pid" ] && init_pid_value=$(tr -dc '0-9' < "$PREFIX/.init.pid" 2>/dev/null)
	# A directory capability is identity, not a pathname: compare the open descriptor's device:inode with the
	# prefix's. This is the framework's own ownership rule
	# (west_commands/test_prefix.py::_runtime_retains_prefix), and the reason it exists is that a rootless guest or
	# server may scrub every DARLING_* variable and carry no prefix path anywhere.
	prefix_id=$(stat -Lc '%d:%i' "$PREFIX" 2>/dev/null)
	for d in /proc/[0-9]*; do
		pid=${d#/proc/}
		[ "$pid" = "$self" ] && continue
		case " $ancestors " in *" $pid "*) witness "OWNED pid=$pid arm=none ancestor=yes ppid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $2}') pgid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $3}') sid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $4}') cmd=$(tr "\0" " " < "$d/cmdline" 2>/dev/null | cut -c1-120)"; continue ;; esac
		if [ -n "$init_pid_value" ] && [ "$pid" = "$init_pid_value" ]; then
			witness "OWNED pid=$pid arm=initpid ancestor=no ppid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $2}') cmd=$(tr "\0" " " < "$d/cmdline" 2>/dev/null | cut -c1-120)"
			echo "$pid"
			continue
		fi
		# arm=capability. The gate is a PREREQUISITE, never ownership, and it must not be the truncated comm: a
		# /proc/<pid>/comm longer than 15 characters is cut ("darlingserver-stub-long" reads "darlingserver-st"),
		# so an exact comm match silently skips the very processes this arm exists for. The gate is therefore a
		# PREFIX of the truncation-free exe basename, with comm only as the fallback for an unreadable exe link.
		exe=$(readlink "$d/exe" 2>/dev/null)
		exe_base=${exe##*/}
		comm=$(cat "$d/comm" 2>/dev/null)
		cap=0
		case "$exe_base" in
		mldr*|launchd*|vchroot*|shellspawn*|darlingserver*) cap=1 ;;
		*) case "$comm" in mldr*|launchd*|vchroot*|shellspawn*|darlingserver*) cap=1 ;; esac ;;
		esac
		cap_hit=0
		if [ "$cap" = 1 ] && [ -n "$prefix_id" ]; then
			for fd in "$d"/fd/*; do
				[ -e "$fd" ] || continue
				if [ "$(stat -Lc '%d:%i' "$fd" 2>/dev/null)" = "$prefix_id" ]; then
					witness "OWNED pid=$pid arm=capability ancestor=no exe_base=$exe_base cmd=$(tr "\0" " " < "$d/cmdline" 2>/dev/null | cut -c1-120)"
					echo "$pid"
					cap_hit=1
					break
				fi
			done
		fi
		# A capability match already reported this pid; falling through would report it twice and inflate every
		# process count the verdict is drawn from.
		[ "$cap_hit" = 1 ] && continue
		case "$exe" in "$PREFIX"|"$PREFIX"/*) witness "OWNED pid=$pid arm=exe ancestor=no ppid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $2}') pgid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $3}') sid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $4}') exe=$exe"; echo "$pid"; continue ;; esac
		cmd=$(tr "\0" " " < "$d/cmdline" 2>/dev/null)
		# The cmdline arm must NOT match this script or its pipeline subshells: they are invoked WITH --prefix,
		# so their own cmdline contains the prefix, and a `kill` loop then kills the very loop doing the killing
		# ("Killed" printed by the script itself). A subshell has a different PID but the same cmdline, so the
		# guard is on the script name, not on the PID.
		case "$cmd" in *"$SELF_NAME"*) continue ;; esac
		# The process that SERVES the prefix's own layer is not a prefix-owned runtime process. When the prefix
		# is itself a mount (an isolation lane's fuse-overlayfs view), that daemon names the prefix in its
		# -o upperdir=/workdir= and in its mountpoint argument, so the cmdline arm below killed it -- which
		# unmounted the prefix and made every later command fail with ENOTCONN. MEASURED 2026-10-18: that is
		# how the first isolation-lane boot died. Identity is the program AND the prefix's own device:inode,
		# not the program name, so a fixture that merely runs fuse-overlayfs under the prefix is still swept.
		case "$exe_base" in
		fuse-overlayfs)
			layer=0
			if [ -n "$prefix_id" ]; then
				for a in $cmd; do
					case "$a" in /*) ;; *) continue ;; esac
					if [ "$(stat -Lc '%d:%i' "$a" 2>/dev/null)" = "$prefix_id" ]; then layer=1; break; fi
				done
			fi
			if [ "$layer" = 1 ]; then
				witness "SKIP pid=$pid arm=layer reason=serves-the-prefix-mountpoint"
				continue
			fi
			;;
		esac
		case "$cmd" in
			*"$PREFIX"*) witness "OWNED pid=$pid arm=cmdline ancestor=no ppid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $2}') pgid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $3}') sid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $4}') cmd=$(echo "$cmd" | cut -c1-120)"; echo "$pid" ;;
		esac
	done
}
count_owned() { owned_pids | wc -l; }

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
# COLLECTS, per run, in one place: both logs (the harness's own and the prefix server's, pre and post), every
# guest-process capture with its path PRINTED, the child's environment and descriptor table read from the HOST, any
# core written during the run, the abort/terminate text wherever it landed, the plane/ledger counters, the deployed
# sha256 of every runtime artifact, and a census that NAMES the instruments that stayed silent.
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
		echo "# the harness did not reach the end of the run, so the bundle is a partial one."
	} > "$BUNDLE_MANIFEST"
	echo "BUNDLE dir=$BUNDLE_DIR"
	bundle_present run-log "$LOG" "the harness's own log: the guest's and the loader's fd 1/2 for this run"
	# THE SERVER'S LOG IS A SECOND LOG. MEASURED: its stderr never appears in the run log (it goes to
	# <prefix>/private/var/log/dserver.log), so a run that "said nothing" about the plane was read as a silent plane
	# while the server's own log carried 52 doorbell records. It is copied on BOTH sides of the run, because it is
	# append-only ACROSS runs: only the delta between the two copies is this run's.
	BUNDLE_SERVER_LOG="$PREFIX/private/var/log/dserver.log"
	if [ -r "$BUNDLE_SERVER_LOG" ]; then
		BUNDLE_SERVER_PRE="$BUNDLE_DIR/server.pre.log"
		cp -f "$BUNDLE_SERVER_LOG" "$BUNDLE_SERVER_PRE"
		bundle_present server-log-pre "$BUNDLE_SERVER_PRE" "copy of $BUNDLE_SERVER_LOG before the boot ($(bundle_bytes "$BUNDLE_SERVER_PRE") bytes; append-only across runs, so only the post-minus-pre delta is this run's)"
	else
		BUNDLE_SERVER_PRE=""
		bundle_missing server-log-pre "no readable server log at $BUNDLE_SERVER_LOG: the prefix has not booted yet (or logs elsewhere), so no server-side rows can be attributed to this run"
	fi
	# THE DEPLOYED ARTIFACT IDENTITY. `dwdiag` owns the candidate table (one list, no second convention), and the
	# line is what answers "which build actually ran" -- two runs served by different copies were compared as if they
	# were the same build twice.
	BUNDLE_RUN_ENV=""
	if [ -x "$SCRIPT_DIR/dwdiag" ]; then
		"$SCRIPT_DIR/dwdiag" env --prefix "$PREFIX" > "$BUNDLE_DIR/run-env.raw" 2>"$BUNDLE_DIR/run-env.err"
		env_rc=$?
		if [ "$env_rc" -eq 0 ] && [ -s "$BUNDLE_DIR/run-env.raw" ]; then
			BUNDLE_RUN_ENV=$(cat "$BUNDLE_DIR/run-env.raw")
			cp -f "$BUNDLE_DIR/run-env.raw" "$BUNDLE_DIR/run-env.txt"
			bundle_present run-env "$BUNDLE_DIR/run-env.txt" "$BUNDLE_RUN_ENV"
		else
			rm -f "$BUNDLE_DIR/run-env.raw"
			bundle_missing run-env "dwdiag env --prefix $PREFIX exited $env_rc, so no deployed copy under this prefix could be digested (see $BUNDLE_DIR/run-env.err). The digest is of DEPLOYED files: without it nothing in this bundle names the build that ran"
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
		echo "# log on both sides, and every guest capture). Lines are copied in full: the bound is the line's own"
		echo "# length, and the longest line found is reported in the MANIFEST for exactly that reason."
	} > "$out"
	scanned=0
	lines=0
	longest=0
	for f in "$LOG" "$BUNDLE_DIR"/server.pre.log "$BUNDLE_DIR"/server.post.log \
		"$BUNDLE_DIR"/*.log "$BUNDLE_DIR"/*.log.1 "$BUNDLE_DIR"/*.log.post "$BUNDLE_DIR"/captures/*; do
		[ -f "$f" ] || continue
		# The run log is scanned through its collected copy, so it is not scanned twice when both exist.
		[ "$f" = "$LOG" ] && [ -f "$BUNDLE_DIR/harness.log" ] && continue
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
		bundle_missing abort-text "searched $scanned file(s) of this run (the run log, both server copies, all captures) and no abort/terminate line was there: the run reached its end without one"
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
	# THE SERVER'S LOG, POST. Copied while the server is still alive (cleanup shuts it down).
	if [ -n "$BUNDLE_SERVER_LOG" ] && [ -r "$BUNDLE_SERVER_LOG" ]; then
		cp -f "$BUNDLE_SERVER_LOG" "$BUNDLE_DIR/server.post.log"
		bundle_present server-log-post "$BUNDLE_DIR/server.post.log" "copy of $BUNDLE_SERVER_LOG after the run; the run's own server rows are post minus pre"
	else
		bundle_missing server-log-post "$BUNDLE_SERVER_LOG is not readable after the run, so the server's own rows for this run cannot be separated from earlier runs'"
	fi
	# OTHER PREFIX LOGS THIS RUN TOUCHED (launchd, fcdaemon, ...): the plural of "the prefix server log(s)".
	other=0
	for f in "$PREFIX"/private/var/log/*.log; do
		[ -f "$f" ] || continue
		[ "$f" = "$BUNDLE_SERVER_LOG" ] && continue
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
	# rungs read by hand. The server's rows come from the DELTA between the two copies of its append-only log.
	counters="$BUNDLE_DIR/counters.txt"
	{
		echo "# name<TAB>run-log<TAB>server-delta<TAB>what it is"
		printf '%s\t%s\t%s\t%s\n' sockets-created "$(grep -ac 'rpc-socket. created' "$LOG" 2>/dev/null)" "-" "per-thread RPC sockets created"
		printf '%s\t%s\t%s\t%s\n' socket-denials "$(grep -ac 'rpc-socket-DENIED' "$LOG" 2>/dev/null)" "-" "RPC socket DENIED (the guest asked for a transport the server refused)"
		printf '%s\t%s\t%s\t%s\n' urgent-timeouts "$(grep -ac 'urgent-wait-TIMEOUT' "$LOG" 2>/dev/null)" "-" "an urgent call waited past its bound"
		printf '%s\t%s\t%s\t%s\n' courier-misses "$(grep -ac 'fd-courier-recv. MISS' "$LOG" 2>/dev/null)" "-" "a descriptor courier receive found nothing"
		printf '%s\t%s\t%s\t%s\n' plane-publishes "$(grep -ac 'plane-publish' "$LOG" 2>/dev/null)" "$(bundle_server_delta 'plane-publish')" "plane operations PUBLISHED by the guest"
		printf '%s\t%s\t%s\t%s\n' plane-doorbell-sent "$(grep -ac 'plane-doorbell-sent' "$LOG" 2>/dev/null)" "$(bundle_server_delta 'plane-doorbell-sent')" "doorbells the SERVER sent (server log only)"
		printf '%s\t%s\t%s\t%s\n' plane-refuse "$(grep -ac 'plane-refuse' "$LOG" 2>/dev/null)" "$(bundle_server_delta 'plane-refuse')" "plane operations REFUSED"
		printf '%s\t%s\t%s\t%s\n' lane-release "$(grep -ac 'lane-release' "$LOG" 2>/dev/null)" "$(bundle_server_delta 'lane-release')" "lane releases"
		printf '%s\t%s\t%s\t%s\n' plane-republish "$(grep -ac 'plane-republish' "$LOG" 2>/dev/null)" "$(bundle_server_delta 'plane-republish')" "plane re-publishes"
		printf '%s\t%s\t%s\t%s\n' dserver-crash "$(grep -ac 'dserver-CRASH' "$LOG" 2>/dev/null)" "$(bundle_server_delta 'dserver-CRASH')" "the server's own crash record"
	} > "$counters"
	bundle_present counters "$counters" "the four counters the VERDICT prints plus the plane/ledger instruments; the server column is the DELTA between server.pre.log and server.post.log (the file is append-only across runs)"
	# THE INSTRUMENT CENSUS, WHICH NAMES THE SILENT ONES. `dwdiag witness` owns the instrument table; a hand-written
	# grep finds the line it was written for and cannot say which OTHER instrument never spoke.
	witness_out="$BUNDLE_DIR/witness.txt"
	if [ -x "$SCRIPT_DIR/dwdiag" ]; then
		if "$SCRIPT_DIR/dwdiag" witness --log "$LOG" > "$witness_out" 2>"$BUNDLE_DIR/witness.err"; then
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
	grep -ahE 'unavailable \(|records no |not carried by this build|this server build' "$LOG" "$BUNDLE_DIR"/server.post.log 2>/dev/null | sort -u | head -40 > "$decl"
	declared=$(wc -l < "$decl" 2>/dev/null | tr -d ' ')
	[ -n "$declared" ] || declared=0
	if [ "$declared" -gt 0 ]; then
		bundle_present build-declarations "$decl" "$declared statement(s) in which this BUILD says a section or instrument is absent/unavailable -- a silent instrument whose instrument is declared unavailable here is SILENT-BY-BUILD, not broken"
	else
		bundle_missing build-declarations "the run's logs carry no 'unavailable'/this-server-build statement, so no instrument can be excused as SILENT-BY-BUILD: every silent instrument above is a real silence for this run"
	fi
	bundle_collect_abort_text
	printf 'BUNDLE-COMPLETE %s\n' "$(date -Is)" >> "$BUNDLE_MANIFEST"
	rm -f "$BUNDLE_DIR/INCOMPLETE"
	return 0
}

# How many times a pattern appears in the DELTA of the server's append-only log: post count minus pre count. A count
# over the whole file would mix in every earlier run's rows, which is exactly how "the server sent 19210 doorbells"
# was once about another prefix's run.
bundle_server_delta() {
	pat="$1"
	pre=0
	post=0
	[ -n "$BUNDLE_SERVER_PRE" ] && [ -f "$BUNDLE_SERVER_PRE" ] && pre=$(grep -ac "$pat" "$BUNDLE_SERVER_PRE" 2>/dev/null)
	[ -f "$BUNDLE_DIR/server.post.log" ] && post=$(grep -ac "$pat" "$BUNDLE_DIR/server.post.log" 2>/dev/null)
	[ -n "$pre" ] || pre=0
	[ -n "$post" ] || post=0
	echo $((post - pre))
}

# THE CONSOLE BLOCK: every path, every missing item and the census, printed as part of the run's own output. A bundle
# a reader cannot find is a bundle that does not exist.
bundle_report() {
	[ "$BUNDLE" = 1 ] || return 0
	echo "== bundle =="
	echo "BUNDLE dir=$BUNDLE_DIR"
	echo "BUNDLE-MANIFEST $BUNDLE_MANIFEST"
	if [ -f "$BUNDLE_DIR/INCOMPLETE" ]; then
		echo "BUNDLE-INCOMPLETE the harness did not reach the end of the run: this bundle is PARTIAL, not a complete one"
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

# A mode that must NOT boot anything: report what this harness would treat as prefix-owned, then exit. The ownership
# rule is what decides whether the next boot starts clean, and a rule that can only be exercised by booting cannot be
# tested; tests/run-darling-boot-harness-ownership-contract.sh drives this mode with synthetic processes.
if [ "$LIST_OWNED" = 1 ]; then
	[ -x "$PREFIX/bin/darling" ] || { echo "no launcher at $PREFIX/bin/darling (still listed below)" >&2; }
	owned_pids | sort -n | uniq
	report_stale_runtime_files
	exit 0
fi

STAGE=clean-start; echo "== clean start =="
witness "TEARDOWN stage=shutdown-enter self=$$ parent=$PPID harness=$(caller_id $$)"
DPREFIX="$PREFIX" DARLING_PREFIX="$PREFIX" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
	"$PREFIX/bin/darling" --rootless shutdown >/dev/null 2>&1
witness "TEARDOWN stage=shutdown-exit rc=$? self=$$"
sleep 3
# Kill, then SETTLE: a process that has just been signalled is still enumerable for a moment (and a zombie whose
# parent has not reaped it stays visible). Declaring failure on the first count produced a false failure the first
# time this script was used -- so re-check in bounded rounds instead of racing the kernel.
owned_pids | while read -r pid; do
	anc=$(own_ancestors | tr '\n' ' ')
	case " $anc " in *" $pid "*) witness "SKIP-KILL pid=$pid reason=ancestor"; continue ;; esac
	witness "KILL pid=$pid sig=9 reason=owned"; kill -9 "$pid" 2>/dev/null
done
left=1
i=0
while [ "$i" -lt 10 ]; do
	sleep 2
	left=$(count_owned)
	[ "$left" -eq 0 ] && break
	owned_pids | while read -r pid; do
	anc=$(own_ancestors | tr '\n' ' ')
	case " $anc " in *" $pid "*) witness "SKIP-KILL pid=$pid reason=ancestor"; continue ;; esac
	witness "KILL pid=$pid sig=9 reason=owned"; kill -9 "$pid" 2>/dev/null
done
	i=$((i + 1))
done
if [ "$left" -gt 0 ]; then
	echo "clean start FAILED: $left prefix-owned processes remain after $i settle rounds" >&2
	owned_pids | head -5 >&2
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
		if "$SCRIPT_DIR/darling-artifact-manifest.sh" --prefix "$PREFIX" --probe "$t" | grep -q "PRESENT"; then
			"$SCRIPT_DIR/darling-artifact-manifest.sh" --prefix "$PREFIX" --probe "$t" | sed 's/^/  /'
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
# A RUN'S LOG MUST SAY WHICH PREFIX IT BELONGS TO. `dwdiag` resolves the SERVER log from a `prefix=` field in the
# run log itself (resolve_prefix in tools/darling-debug-runner reads the first `prefix=` token and trims `]`), and
# a `--prefix` on the command line does NOT override it for that purpose. Without this line every log-only tool fell
# back to a DEFAULT path and read ANOTHER prefix's dserver.log -- the same failure class as "a run served by another
# prefix's live runtime", and it is silent. MEASURED 2026-10-19: `dwdiag progress --log <run log> --prefix <P>`
# printed `SERVER-LOG-PATH /tmp/dr-on-matched/private/var/log/dserver.log (from DEFAULT ...)` and dropped every
# server-side fact while the prefix was known. The shape below is the one the tools already parse
# (`[dwdiag-env prefix=<path> ...]`), so ONE line makes the log self-describing for progress, verdict, crash and
# scripts/darling-stop-reason.sh. It is the first line, because the guest's own output is appended after it.
echo "[dwdiag-env prefix=$PREFIX log=$LOG bundle=$BUNDLE_DIR wait=$WAIT]" >> "$LOG"
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
env DPREFIX="$PREFIX" DARLING_PREFIX="$PREFIX" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
	$HATCH_ENV $EXTRA_ENV \
	nohup timeout "$((WAIT + 60))" \
	"$PREFIX/bin/darling" --rootless shell /bin/bash -c "$CMD" >> "$LOG" 2>&1 &

# WAIT FOR THE WORKLOAD'S OWN RESULT LINE, NOT FOR A TIMER. MEASURED: `sleep "$WAIT"` made every row cost the full
# watchdog -- the acceptance suite ran 9 rows at --wait-base 400 and needed about an hour, which is the difference
# between a measurement you take and one you avoid. The workload prints one machine-readable result line and then
# exits, so the harness polls for a line that carries `pass=` and stops as soon as it sees it, still bounded by WAIT.
# Which line that is comes from `--poll` (default: the first fixture's own, unchanged for existing callers).
# The marker list is deliberately NOT used here: the tool's markers open the window a probe is read in, and exiting on
# one of them would cut a run short (the value of the run is what happens after the marker).
STAGE=workload-wait; echo "waiting up to ${WAIT}s for the workload's own result line (poll='$POLL'; diagnostic lines before it are not verdicts)"
_waited=0
while [ "$_waited" -lt "$WAIT" ]; do
	if grep -aq "$POLL" "$LOG" 2>/dev/null; then
		break
	fi
	# THE HOST-ONLY RECORD, taken while the child is alive. Every second poll (a 2 s resolution) so the sampling
	# cannot dominate the run. Whatever was missed is reported as a MISSING row with that granularity as the reason.
	if [ "$BUNDLE" = 1 ] && [ $((_waited % 2)) -eq 0 ]; then
		bundle_watch_children
	fi
	sleep 1
	_waited=$((_waited + 1))
done
# The boundary reached is part of the LOG, not just of this console: a verdict rule that has to tell "the workload
# failed" from "we stopped measuring" reads the log, and a line that only ever reached a terminal cannot be judged
# later (MEASURED: the offline verdict could not see this and kept calling a watchdog stop a CRASH).
echo "waited ${_waited}s of at most ${WAIT}s" | tee -a "$LOG"

STAGE=verdict; echo "== verdict =="
verdict=0
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

# THE BUNDLE IS PRODUCED HERE, BEFORE ANY TEARDOWN AND ON EVERY PATH: while the server is still alive (so its post
# log can be copied), after the log holds everything the run wrote, and before the freeze exit -- a frozen run is the
# one whose bundle is needed most. The console block that follows is the run's own announcement of where everything is.
STAGE=bundle
bundle_finish
bundle_report

# STOP-REASON: the run's OWN evidence names the stop, in one line, before anyone greps for it.
# MEASURED 2026-10-19: an xcodebuild workload died with rc=132 (SIGILL) and no artifact; the missing Swift
# symbol, its referrer and the image that should have supplied it were ALREADY inside this bundle's captures/,
# and the cause was still found by hand-grepping 27 KB -- because the bundle was reported, not read. The
# classifier reads the log plus the bundle's captures/abort text; a passing run prints kind=none, which is a
# statement about the evidence, never about the verdict.
STAGE=stop-reason
if [ -x "$SCRIPT_DIR/darling-stop-reason.sh" ]; then
	_sr_extra=""
	[ -n "$BUNDLE_DIR" ] && [ -d "$BUNDLE_DIR" ] && _sr_extra="$_sr_extra --bundle=$BUNDLE_DIR"
	[ -n "$LAST_STATUS_VALUE" ] && _sr_extra="$_sr_extra --rc=$LAST_STATUS_VALUE"
	# RUN SCOPING. The prefix's captures and its server log are CUMULATIVE: they hold earlier runs' records, and a
	# matching prefix is not run identity. The classifier admits a capture only when it is at or after this run's
	# own start stamp, counts what it excluded, and says so.
	[ -n "$BUNDLE_STAMP" ] && [ -f "$BUNDLE_STAMP" ] && _sr_extra="$_sr_extra --run-start=$(stat -c %Y "$BUNDLE_STAMP" 2>/dev/null || echo 0)"
	_sr_extra="$_sr_extra --run-id=$(basename "$LOG")"
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

if [ "${FREEZE_ON_FAIL:-0}" -eq 1 ] && [ "$verdict" -ne 0 ]; then
	STAGE=freeze; echo "== freeze-on-fail (diagnostic) =="
	echo "FREEZE-ON-FAIL prefix=$PREFIX log=$LOG verdict=$verdict"
	echo "FREEZE-ON-FAIL inspect now, then clean up with: west darling-prefix-repair --prefix $PREFIX --cleanup-mounts"
	echo "FREEZE-ON-FAIL or: DPREFIX=$PREFIX DARLING_PREFIX=$PREFIX DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 $PREFIX/bin/darling --rootless shutdown"
	exit 1
fi

STAGE=cleanup; echo "== cleanup =="
witness "TEARDOWN stage=shutdown-enter self=$$ parent=$PPID harness=$(caller_id $$)"
DPREFIX="$PREFIX" DARLING_PREFIX="$PREFIX" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
	"$PREFIX/bin/darling" --rootless shutdown >/dev/null 2>&1
witness "TEARDOWN stage=shutdown-exit rc=$? self=$$"
sleep 3
owned_pids | while read -r pid; do
	anc=$(own_ancestors | tr '\n' ' ')
	case " $anc " in *" $pid "*) witness "SKIP-KILL pid=$pid reason=ancestor"; continue ;; esac
	witness "KILL pid=$pid sig=9 reason=owned"; kill -9 "$pid" 2>/dev/null
done
final=1
i=0
while [ "$i" -lt 10 ]; do
	sleep 2
	final=$(count_owned)
	[ "$final" -eq 0 ] && break
	owned_pids | while read -r pid; do
	anc=$(own_ancestors | tr '\n' ' ')
	case " $anc " in *" $pid "*) witness "SKIP-KILL pid=$pid reason=ancestor"; continue ;; esac
	witness "KILL pid=$pid sig=9 reason=owned"; kill -9 "$pid" 2>/dev/null
done
	i=$((i + 1))
done
# Count mounts whose MOUNTPOINT lies under the prefix. A substring match was MEASURED to fail here:
# another lane's overlay instance names this prefix in its lowerdir
# (.west-prefix-instances/probe1/merged lowerdir=<prefix>), which is not a mount under the prefix, yet
# it made every run print "mounts: 1" and clean up as VERDICT: FAIL while the guest verdict was PASS.
# A mount AT the prefix path is excluded only when it is a FUSE overlay: that is the isolation layer the
# run looks THROUGH (an isolation lane, or a fixture's own instance), not residue the run left behind.
# MEASURED 2026-10-18: counting it made every lane run report "mounts: 1" and VERDICT: FAIL with a PASSing
# guest. A kernel `overlay` at the prefix stays counted: in the privileged runtime THAT is the server's own
# bring-up mount, and leaving one behind after shutdown is real residue.
mounts=$(mount 2>/dev/null | awk -v p="$PREFIX" 'index($3, p "/") == 1 || ($3 == p && $5 != "fuse.fuse-overlayfs")' | wc -l)
layer_mounts=$(mount 2>/dev/null | awk -v p="$PREFIX" '$3 == p && $5 == "fuse.fuse-overlayfs"' | wc -l)
# Same rule after the run: leave the prefix as a *bootable* clean state, not merely a process-free one.
clear_stale_runtime_files
echo "prefix processes after: $final, mounts: $mounts (plus $layer_mounts prefix layer mount(s))"
if [ "$final" -gt 0 ] || [ "$mounts" -gt 0 ]; then
	echo "cleanup FAILED" >&2
	verdict=1
fi

if [ "$verdict" = 0 ]; then
	echo "VERDICT: PASS"
else
	echo "VERDICT: FAIL"
fi
exit "$verdict"

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
#   4. a single VERDICT block at the end: the markers the caller asked for, plus the counters that matter here
#      (per-thread RPC socket creations, denials, urgent timeouts, courier misses);
#   5. cleanup afterwards, and a non-zero exit if the prefix is not clean.
#
# Usage:
#   darling-boot-run.sh --prefix PATH --wait SECONDS [--marker NAME]... [--hatch] [--cmd 'shell command']
# Exit: 0 if every --marker appeared in the log, 1 otherwise (or if cleanup failed).

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

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--wait) WAIT="$2"; shift 2 ;;
		--marker) MARKERS="$MARKERS|$2"; shift 2 ;;
		--cmd) CMD="$2"; shift 2 ;;
		--hatch) HATCH=1; shift ;;
		--env) EXTRA_ENV="$EXTRA_ENV $2"; shift 2 ;;
		# DIAGNOSTIC ONLY (default off): when the marker verdict fails, leave the prefix and its
		# processes ALIVE so an external observer (darling-debug, LLDB, host wchan) can inspect the
		# failing incarnation. Acceptance never sets this; it exists because a watchdog stop followed
		# immediately by a shutdown destroys exactly the state the investigation needs.
		--freeze-on-fail) FREEZE_ON_FAIL=1; shift ;;
		--list-owned) LIST_OWNED=1; shift ;;
		--assert-prefix-maps) ASSERT_MAPS=1; shift ;;
		--verify-probe) VERIFY_PROBES="$VERIFY_PROBES
$2"; shift 2 ;;
		--log) LOG="$2"; shift 2 ;;
		-h|--help) sed -n '2,22p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[ -n "$PREFIX" ] || { echo "usage: $0 --prefix PATH --wait SECONDS [--marker NAME]..." >&2; exit 2; }
[ -n "$WAIT" ] || [ "$LIST_OWNED" = 1 ] || { echo "usage: $0 --prefix PATH --wait SECONDS [--marker NAME]..." >&2; exit 2; }

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
witness() { echo "$@" >&2; }
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
		case "$cmd" in
			*"$PREFIX"*) witness "OWNED pid=$pid arm=cmdline ancestor=no ppid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $2}') pgid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $3}') sid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | awk '{print $4}') cmd=$(echo "$cmd" | cut -c1-120)"; echo "$pid" ;;
		esac
	done
}
count_owned() { owned_pids | wc -l; }

# The prefix's own runtime files. A killed run leaves them naming a server that is gone, and they are never removed
# while an owner is alive: both callers below only act with zero owners, which is the framework's own precondition
# (west_commands/test_prefix.py::remove_stale_init_pid / remove_stale_server_socket).
stale_init_pid() { tr -dc '0-9' < "$PREFIX/.init.pid" 2>/dev/null; }
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

echo "== run =="
echo "log: $LOG"
if [ "$HATCH" = 1 ]; then
	HATCH_ENV="DARLING_DISABLE_THREAD_RPC_UDS=1"
else
	HATCH_ENV=""
fi
: > "$LOG"
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
# The marker list is deliberately NOT used here: the tool's markers open the window a probe is read in, and exiting on
# one of them would cut a run short (the value of the run is what happens after the marker).
STAGE=workload-wait; echo "waiting up to ${WAIT}s for the workload's own result line (diagnostic lines before it are not verdicts)"
_waited=0
while [ "$_waited" -lt "$WAIT" ]; do
	if grep -aq "RING_MACH_TEST mode=.*pass=" "$LOG" 2>/dev/null; then
		break
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
# MEASURED: a marker containing spaces was split by the old space-separated list, so `--marker 'ITER 0 dropped'`
# became three markers and reported `MISS dropped` for something that was simply never queried as a whole.
# '|' keeps a marker intact; IFS is restored right after the loop.
oldIFS=$IFS; IFS='|'
for m in $MARKERS; do
	[ -n "$m" ] || continue
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
mounts=$(mount 2>/dev/null | grep -c "$PREFIX")
# Same rule after the run: leave the prefix as a *bootable* clean state, not merely a process-free one.
clear_stale_runtime_files
echo "prefix processes after: $final, mounts: $mounts"
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

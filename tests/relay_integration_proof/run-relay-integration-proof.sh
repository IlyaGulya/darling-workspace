#!/usr/bin/env bash
#
# run-relay-integration-proof.sh -- measure, on the REAL Darling product
# runtime, the premises the recommended relay descriptor architecture depends
# on, and print every measured number.
#
# The standalone model in tests/relay_topology_proof/ proves the relay topology
# in isolation (a synthetic CLONE_VM-without-CLONE_FILES companion, a synthetic
# futex, a synthetic eventfd).  It deliberately does not touch the product.
# This harness closes that gap: it drives real prefixes (real mldr loader, real
# darlingserver, real per-thread RPC sockets, real ring transport, real
# descriptor guard) through a guest fixture compiled inside the guest, and
# measures the loader-owned descriptors from the host through /proc, which is
# the only view that can see them (fcntl(F_GETFD) inside the guest does not:
# the guard hides them).
#
# Claims
# ------
#   I1  defect signature: advertised limits, the raw getdtablesize syscall, and
#       a top-down dup2 walk over the last window of the advertised range.
#       MUST PASS.  This is the integration form of the stock-libunistring
#       assertion that motivated the change: the OFF prefix must accept
#       getdtablesize() - 1 and the ON prefix must refuse it, with the exact
#       numbers printed.  If this does not reproduce, the harness is not
#       looking at the runtime it claims to look at.
#   I2  hidden loader descriptors: host /proc/<pid>/fd inventory versus what the
#       guest itself can enumerate (fcntl F_GETFD over the advertised range plus
#       a /dev/fd listing), with each host-only descriptor's target and, for
#       sockets, its type from /proc/net/unix.  Measurement.  The /dev/fd
#       listing is taken through the guest's own /bin/ls, because the guest's
#       readdir() path returns nothing at all in this runtime (even for an
#       ordinary directory); the fcntl scan remains the authority for which
#       numbers the guest can actually use, and a hidden descriptor that the
#       directory listing does expose is labelled listed_by_guest_fd_dir=1.
#   I3  per-thread cost: descriptors the host gains per additional guest thread
#       that has performed one mach_host_self RPC, against the same process'
#       single-threaded baseline.  Measurement.
#   I4  ring attach cost: descriptors the host gains inside one process when
#       the warm-up mach_host_self calls attach the ring, each new descriptor
#       identified by its readlink target, its /proc/net/unix type and inode
#       when it is a socket, and its /proc/<pid>/fdinfo identity when it is an
#       anonymous inode.  Measurement.
#   I5  real descriptor transfer: AF_UNIX socketpair + SCM_RIGHTS round trip,
#       the received descriptor proven to work, the sender-side descriptor
#       proven untouched, plus the host descriptor delta across the transfer.
#       MUST PASS: it is the product path the architecture must preserve.
#   I6  close_range consequence: use the loader first, then close every
#       descriptor from 3 up to the advertised limit, then use the loader
#       again; report whether the loader still works and how many descriptors
#       it lost.  The guest prints an extra "armed" barrier after it has used
#       the loader and before it closes anything, so the host can tell which
#       descriptors were actually destroyed.  Measurement.
#   I7  transport cost ON versus OFF: a fixed workload of 200000
#       mach_host_self RPCs after ring attachment, measured from the host as
#       voluntary/nonvoluntary context switches, CPU ticks, read/write syscall
#       counters, and a strace -f -c syscall count when ptrace permits
#       attaching.  Measurement.  When the host forbids the attach (yama
#       ptrace_scope=1 denies it because a guest process is a child of the
#       prefix daemon, not of this harness), the syscall count is reported as
#       UNPROVEN with the captured ptrace error and the kernel-side read/write
#       syscall counters, context switches and CPU ticks stand in for it.
#
# Why I1 and I5 are the only must-pass claims: both are contracts the product
# already exhibits today and that the relay architecture must not break, so a
# violation means a regression or a mis-measurement and has to stop the run.
# I2, I3, I4, I6 and I7 measure premises whose numbers may legitimately differ
# from the architectural prediction on a given build, so they are reported with
# their values; each one prints UNPROVEN with the reason when the product or
# the host cannot exhibit it.
#
# Exit status
#   0  every must-pass claim passed (measurements may be UNPROVEN; each states
#      why)
#   1  a must-pass claim failed or could not be measured
#   2  the harness could not run (fixture did not stage or compile, a stage
#      produced no evidence or timed out)
#   3  refusal: a prefix variable is unset or does not look bootstrapped
#
# Environment
#   RELAY_INTEGRATION_PREFIX_ON   bootstrapped prefix with the ring enabled
#   RELAY_INTEGRATION_PREFIX_OFF  matched prefix with the ring disabled
#   RELAY_INTEGRATION_THREADS     I3 thread count (default 32, max 64)
#   RELAY_INTEGRATION_WARMUP      I4 warm-up mach traps (default 36)
#   RELAY_INTEGRATION_RPC         I7 RPC count (default 200000)
#   RELAY_INTEGRATION_PRE_MS      hold before the action (default 2000)
#   RELAY_INTEGRATION_POST_MS     hold after the action (default 2000)
#   RELAY_INTEGRATION_RPC_PRE_MS  I7 pre hold, must cover the strace attach
#                                 (default 6000)
#   RELAY_INTEGRATION_STAGE_TIMEOUT  guest stage timeout, seconds (default 120;
#                                 the close-range stage is bounded to 90)
#   RELAY_INTEGRATION_STRACE      how I7 attaches its syscall counters:
#                                 auto (default) unprivileged strace on the host
#                                 and in the container; sudo uses passwordless
#                                 sudo for the host leg, which is the only way to
#                                 attach at all when
#                                 kernel.yama.ptrace_scope is 1 and the guest is
#                                 a child of the prefix daemon rather than of
#                                 this script; none skips the attach.
#                                 The container leg is never privileged: the
#                                 architecture must hold without capabilities, so
#                                 that leg reports UNPROVEN there instead.
#                                 Counts under strace are sound evidence; the
#                                 timings in the same window are not, because
#                                 strace stops the traced task on every syscall.
#
# Every guest stage is bounded, every stage's launcher runs with a timeout, and
# an interrupted leg shuts its prefix down through the EXIT trap after removing
# its guest artifacts.
#
# The guest fixture sleeps for a fixed window instead of polling a rendezvous
# file: a single-direction protocol needs no guest-side syscalls, so the sample
# the host takes cannot itself perturb the counters.  The host refuses a sample
# whose window has already closed (the next barrier is already in the log), and
# reports that claim as UNPROVEN.
#
# No product source is modified, nothing is committed, and each prefix is shut
# down at the end of its leg with a survivor report.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace="$(cd "$here/../.." && pwd)"
# shellcheck source=../../testkit/scripts/darling-guest-shell.sh
source "$workspace/testkit/scripts/darling-guest-shell.sh"

prefix_on="${RELAY_INTEGRATION_PREFIX_ON:-}"
prefix_off="${RELAY_INTEGRATION_PREFIX_OFF:-}"

refuse() {
	printf 'RELAY_INTEGRATION_REFUSE %s\n' "$*" >&2
	exit 3
}

[ -n "$prefix_on" ] || refuse "RELAY_INTEGRATION_PREFIX_ON is unset"
[ -n "$prefix_off" ] || refuse "RELAY_INTEGRATION_PREFIX_OFF is unset"
[ -x "$prefix_on/bin/darling" ] ||
	refuse "RELAY_INTEGRATION_PREFIX_ON=$prefix_on has no executable bin/darling"
[ -x "$prefix_off/bin/darling" ] ||
	refuse "RELAY_INTEGRATION_PREFIX_OFF=$prefix_off has no executable bin/darling"
[ -d "$prefix_on/usr/lib" ] ||
	refuse "RELAY_INTEGRATION_PREFIX_ON=$prefix_on does not look bootstrapped"
[ -d "$prefix_off/usr/lib" ] ||
	refuse "RELAY_INTEGRATION_PREFIX_OFF=$prefix_off does not look bootstrapped"

threads="${RELAY_INTEGRATION_THREADS:-32}"
warmup="${RELAY_INTEGRATION_WARMUP:-36}"
rpc_count="${RELAY_INTEGRATION_RPC:-200000}"
pre_ms="${RELAY_INTEGRATION_PRE_MS:-2000}"
post_ms="${RELAY_INTEGRATION_POST_MS:-2000}"
rpc_pre_ms="${RELAY_INTEGRATION_RPC_PRE_MS:-6000}"
stage_timeout="${RELAY_INTEGRATION_STAGE_TIMEOUT:-120}"
[ "$threads" -le 64 ] || threads=64

strace_mode="${RELAY_INTEGRATION_STRACE:-auto}"
# The traced window runs fewer requests than the timing window: strace stops the
# traced task on every syscall, so the same 200000-call workload does not finish
# inside any sensible bound.  Counts scale, and the per-request figure is derived
# from this count.
strace_rpc_count="${RELAY_INTEGRATION_STRACE_RPC:-20000}"
strace_stage_timeout="${RELAY_INTEGRATION_STRACE_STAGE_TIMEOUT:-300}"

# I8 reads the server's own ring counters, which is the only way to attribute the
# wake cost without strace and without the observer effect: strace suppresses the
# doorbell because it slows the guest enough that the server keeps polling. The
# tool is the one the ring-mode metadata test already uses.
stat_tool="${RELAY_INTEGRATION_STAT_TOOL:-/home/ilyagulya/work/darling-gwn-resume/source-fixes/ring-comparison-server/tools/darling-stat}"
case "$strace_mode" in
auto | sudo | none) ;;
*) refuse "RELAY_INTEGRATION_STRACE must be auto, sudo or none (got $strace_mode)" ;;
esac
strace_argv=()
strace_kill_argv=()
strace_unavailable="strace is not installed on the host"
if [ "$strace_mode" != "none" ]; then
	if ! command -v strace >/dev/null 2>&1; then
		:
	elif [ "$strace_mode" = "sudo" ]; then
		if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
			strace_argv=(sudo -n strace)
			strace_kill_argv=(sudo -n kill)
			strace_unavailable=""
		else
			strace_unavailable="RELAY_INTEGRATION_STRACE=sudo was requested but passwordless sudo is unavailable"
		fi
	else
		strace_argv=(strace)
		strace_kill_argv=(kill)
		strace_unavailable=""
	fi
fi

export DARLING_ROOTLESS=1
export DARLING_NOOVERLAYFS=1
export DARLING_EUNION=1

work="$(mktemp -d "${TMPDIR:-/tmp}/relay-integration-proof.XXXXXX")"
token="$$.$RANDOM"
guest_src="/private/var/tmp/relay_integration_fixture.$token.c"
guest_bin="/private/var/tmp/relay_integration_fixture.$token"
guest_cc="/Library/Developer/CommandLineTools/usr/bin/clang"
guest_sdk="/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
fixture_source="$here/relay_integration_fixture.c"

GUEST_PID=""
DAEMON_PID=""
leg_active=""
leg_active_prefix=""

remove_guest_artifacts() { # prefix
	set +e
	darling_guest_shell "$1/bin/darling" "$1" 30 \
		"rm -f '$guest_src' '$guest_bin' /private/var/tmp/relay-int-close-range-*" \
		>/dev/null 2>&1
	set -e
}

cleanup() {
	if [ -n "$leg_active" ] && [ -n "$leg_active_prefix" ]; then
		printf 'CLEANUP interrupted leg %s: removing guest artifacts and shutting the prefix down\n' \
			"$leg_active"
		remove_guest_artifacts "$leg_active_prefix"
		shutdown_prefix "$leg_active" "$leg_active_prefix"
	fi
	rm -rf -- "$work"
}
trap cleanup EXIT

must_pass_failed=0

declare -A I1_TOP I1_TOP1_ACCEPTED I1_HIGHEST I1_VERDICT
declare -A I2_HOST I2_GUEST I2_HIDDEN I2_VERDICT
declare -A I3_PRE I3_POST I3_DELTA I3_PER_THREAD I3_VERDICT
declare -A I4_PRE I4_POST I4_DELTA I4_KINDS I4_VERDICT
declare -A I5_DELTA I5_VERDICT
declare -A I6_LOST I6_VERDICT
declare -A I7_VOL I7_NONVOL I7_UTIME I7_STIME I7_SYSCR I7_SYSCW I7_STRACE I7_VERDICT I7_WALL
declare -A I7_CLASS_GUEST I7_CLASS_SERVER I7_TOTAL_GUEST I7_TOTAL_SERVER

note() { printf '%s\n' "$*"; }

is_int() {
	case "${1:-}" in
	'' | *[!0-9-]*) return 1 ;;
	*) return 0 ;;
	esac
}

num() { printf '%s' "${1:-?}"; }

per_request() { # delta count
	awk -v d="${1:-0}" -v n="${2:-0}" 'BEGIN { printf "%.5f", d / (n > 0 ? n : 1) }'
}

# ---------------------------------------------------------------- helpers

field_of() { # file line_pattern key
	awk -v pat="$2" -v key="$3" '
		$0 ~ pat {
			for (i = 1; i <= NF; ++i) {
				if (index($i, key "=") == 1) {
					print substr($i, length(key) + 2)
					exit
				}
			}
		}' "$1"
}

guest_lines() { # log pattern
	grep -E -- "$2" "$1" 2>/dev/null | sed 's/^/guest /' || true
}

# strace -c summaries: the trailing columns are
#   %time seconds usecs/call calls [errors] syscall
# so read the column index of "calls" from the header instead of counting from
# the end, which would pick up the errors column on the lines that carry one.
strace_total() { # file
	awk '
		NR == 1 { for (i = 1; i <= NF; ++i) if ($i == "calls") col = i; next }
		$NF == "total" { if (col > 0 && $col ~ /^[0-9]+$/) { print $col; exit } }
	' "$1" 2>/dev/null
}

strace_class() { # file -> "syscall=count ..." for the busiest syscalls
	awk '
		NR == 1 { for (i = 1; i <= NF; ++i) if ($i == "calls") col = i; next }
		$NF == "total" { next }
		col > 0 && $col ~ /^[0-9]+$/ && $NF ~ /^[a-z_][a-z_0-9]*$/ { print $NF, $col }
	' "$1" 2>/dev/null |
		sort -k2 -nr |
		head -12 |
		awk '{ printf "%s=%s ", $1, $2 }' |
		sed 's/[[:space:]]*$//'
}

ring_counter() { # snapshot key -> integer or empty
	[ -s "${1:-}" ] || return 0
	jq -r --arg k "$2" '(.[$k] // empty) | tostring' "$1" 2>/dev/null || true
}

ring_per_call() { # snapshot call -> integer or empty
	[ -s "${1:-}" ] || return 0
	jq -r --arg k "$2" '(.per_call[$k].count // empty) | tostring' "$1" 2>/dev/null || true
}

ring_snapshot() { # prefix outfile -> rc
	python3 "$stat_tool" "$1" >"$2" 2>"$2.err"
}

now_s() { date +%s; }

start_stage() { # name timeout script
	local name="$1"
	local stage_timeout_seconds="$2"
	local script="$3"

	STAGE_LOG="$work/$name.log"
	: >"$STAGE_LOG"
	stage_start_s="$(now_s)"
	darling_guest_shell "$prefix/bin/darling" "$prefix" "$stage_timeout_seconds" \
		"$script" >"$STAGE_LOG" 2>&1 &
	STAGE_PID=$!
}

wait_marker() { # pattern timeout_s
	local pattern="$1"
	local wait_seconds="$2"
	local deadline=$(( $(now_s) + wait_seconds ))

	while :; do
		if grep -qE -- "$pattern" "$STAGE_LOG" 2>/dev/null; then
			return 0
		fi
		if ! kill -0 "$STAGE_PID" 2>/dev/null; then
			grep -qE -- "$pattern" "$STAGE_LOG" 2>/dev/null && return 0
			return 1
		fi
		if [ "$(now_s)" -ge "$deadline" ]; then
			return 1
		fi
		sleep 0.05
	done
}

window_open() { # next_marker
	! grep -qE -- "$1" "$STAGE_LOG" 2>/dev/null
}

finish_stage() {
	set +e
	wait "$STAGE_PID"
	STAGE_RC=$?
	set -e
	return 0
}

stage_elapsed() { printf '%s' "$(( $(now_s) - stage_start_s ))"; }

guest_pid() { # log
	sed -n 's/^I[0-9] pid=\([0-9][0-9]*\)$/\1/p' "$1" | head -1
}

host_fd_snapshot() { # pid outfile
	local pid="$1"
	local out="$2"
	local entry

	: >"$out"
	GUEST_PID="$pid"
	for entry in /proc/"$pid"/fd/*; do
		if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
			continue
		fi
		printf '%s %s\n' "${entry##*/}" "$(readlink "$entry" 2>/dev/null || printf '?')" \
			>>"$out"
	done
}

fd_count() { wc -l <"$1" | tr -d ' '; }

fd_numbers_sorted() { # file -> fd numbers in comm-friendly order
	LC_ALL=C awk '{print $1}' "$1" | LC_ALL=C sort
}

fd_target() { # file fd
	awk -v f="$2" '$1 == f {print $2; exit}' "$1"
}

unix_socket_type() { # inode
	local inode="$1"
	local source result

	for source in /proc/net/unix /proc/"$GUEST_PID"/net/unix /proc/"$DAEMON_PID"/net/unix; do
		[ -r "$source" ] || continue
		result="$(awk -v ino="$inode" -v ns="$source" 'NR > 1 && $7 == ino {
			t = $5
			name = ""
			if (t == "0001") name = "(SOCK_STREAM)"
			else if (t == "0002") name = "(SOCK_DGRAM)"
			else if (t == "0005") name = "(SOCK_SEQPACKET)"
			printf "unix_type=%s%s unix_path=%s unix_ns=%s\n", t, name,
				($8 == "" ? "-" : $8), ns
			exit
		}' "$source")"
		if [ -n "$result" ]; then
			printf '%s' "$result"
			return 0
		fi
	done
	printf 'unix_type=not-listed-in-proc-net-unix-of-self-guest-or-daemon\n'
	return 0
}

fdinfo_summary() { # fd -> one line of kernel-side identity from /proc/<pid>/fdinfo
	local info="/proc/$GUEST_PID/fdinfo/$1"

	if [ ! -r "$info" ]; then
		printf 'fdinfo=unavailable'
		return 0
	fi
	printf 'fdinfo=%s' "$(tr '\n' ';' <"$info" | tr -s ' ' | cut -c1-160)"
}

describe_fd_target() { # fd target
	local target="$2"

	case "$target" in
	socket:\[*\])
		local inode="${target#socket:[}"

		inode="${inode%]}"
		printf 'fd=%s target=%s %s\n' "$1" "$target" "$(unix_socket_type "$inode")"
		;;
	anon_inode:*)
		printf 'fd=%s target=%s %s\n' "$1" "$target" "$(fdinfo_summary "$1")"
		;;
	*)
		printf 'fd=%s target=%s\n' "$1" "$target"
		;;
	esac
}

proc_counters() { # pid -> "vol nonvol utime stime syscr syscw"
	local pid="$1"
	local values=() key value

	for key in voluntary_ctxt_switches nonvoluntary_ctxt_switches; do
		value="$(awk -v k="$key:" '$1 == k {print $2}' /proc/"$pid"/status 2>/dev/null)"
		values+=("${value:-?}")
	done
	for key in 14 15; do
		value="$(awk -v f="$key" '{print $f}' /proc/"$pid"/stat 2>/dev/null)"
		values+=("${value:-?}")
	done
	for key in syscr syscw; do
		value="$(awk -v k="$key:" '$1 == k {print $2}' /proc/"$pid"/io 2>/dev/null)"
		values+=("${value:-?}")
	done
	printf '%s\n' "${values[*]}"
}

shutdown_prefix() { # label prefix
	local label="$1"
	local target="$2"
	local rc=0
	local daemon_pids=""
	local guests=""
	local first_daemon
	local mounts_self
	local mounts_daemon=0
	local mldr_total
	local p

	set +e
	timeout 120 env "DPREFIX=$target" "DARLING_PREFIX=$target" \
		"$target/bin/darling" shutdown >"$work/shutdown-$label.log" 2>&1
	rc=$?
	set -e

	daemon_pids="$(pgrep -f "darlingserver .*$(basename "$target")( |$)" 2>/dev/null | tr '\n' ' ' || true)"
	if [ -n "$daemon_pids" ]; then
		first_daemon="$(printf '%s' "$daemon_pids" | awk '{print $1}')"
		guests="$(pgrep -P "$first_daemon" 2>/dev/null | tr '\n' ' ' || true)"
	fi
	mounts_self="$(grep -c -F -- "$target" /proc/self/mountinfo 2>/dev/null || true)"
	for p in $daemon_pids; do
		mounts_daemon=$(( mounts_daemon + $(grep -c -F -- "$target" /proc/"$p"/mountinfo 2>/dev/null || true) ))
	done
	mldr_total="$(pgrep -x mldr 2>/dev/null | wc -l | tr -d ' ' || true)"

	printf 'SHUTDOWN prefix=%s rc=%s daemon_pids=[%s] daemon_children=[%s] mounts_self_ns=%s mounts_daemon_ns=%s mldr_processes_total=%s\n' \
		"$label" "$rc" "$daemon_pids" "$guests" "${mounts_self:-0}" \
		"$mounts_daemon" "$mldr_total"
	if [ -n "$daemon_pids" ] || [ -n "$guests" ]; then
		printf 'SHUTDOWN prefix=%s SURVIVORS prefix-owned process(es) remain: daemon=[%s] children=[%s]\n' \
			"$label" "$daemon_pids" "$guests"
	fi
}

# ---------------------------------------------------------------- one leg

run_leg() { # label prefix
	local label="$1"
	local target="$2"
	local pid="" snapshot_prefd="" snapshot_postfd=""
	local pre_count post_count value

	prefix="$target"
	leg_active="$label"
	leg_active_prefix="$target"
	GUEST_PID=""
	DAEMON_PID="$(pgrep -f "darlingserver .*$(basename "$target")( |$)" 2>/dev/null | head -1 || true)"
	note "== leg $label prefix=$target daemon=${DAEMON_PID:-none-yet} =="

	# --- I1 defect signature (must pass) --------------------------------
	start_stage "$label-defect" "$stage_timeout" "'$guest_bin' defect"
	finish_stage
	if ! grep -q '^I1 defect ' "$STAGE_LOG"; then
		I1_VERDICT[$label]="FAIL: no defect measurement (stage_rc=$STAGE_RC)"
		must_pass_failed=1
		printf 'I1 FAIL prefix=%s stage_rc=%s reason=no-defect-evidence\n' \
			"$label" "$STAGE_RC"
		sed 's/^/guest /' "$STAGE_LOG"
	else
		guest_lines "$STAGE_LOG" '^I1 '
		I1_TOP[$label]="$(field_of "$STAGE_LOG" '^I1 defect ' advertised)"
		I1_TOP1_ACCEPTED[$label]="$(field_of "$STAGE_LOG" '^I1 defect ' top_minus_one_accepted)"
		I1_HIGHEST[$label]="$(field_of "$STAGE_LOG" '^I1 defect ' highest_accepted)"
		if [ "$label" = "on" ]; then
			if [ "${I1_TOP1_ACCEPTED[$label]}" = "0" ] &&
				is_int "${I1_HIGHEST[$label]}" &&
				[ "${I1_HIGHEST[$label]}" -lt "$(( I1_TOP[$label] - 1 ))" ]; then
				I1_VERDICT[$label]=PASS
			else
				I1_VERDICT[$label]="FAIL: expected the ON prefix to refuse getdtablesize()-1"
				must_pass_failed=1
			fi
		else
			if [ "${I1_TOP1_ACCEPTED[$label]}" = "1" ]; then
				I1_VERDICT[$label]=PASS
			else
				I1_VERDICT[$label]="FAIL: expected the OFF prefix to accept getdtablesize()-1"
				must_pass_failed=1
			fi
		fi
	fi
	printf 'I1 %s prefix=%s advertised=%s top_minus_one=%s top_minus_one_accepted=%s highest_accepted=%s\n' \
		"${I1_VERDICT[$label]}" "$label" "$(num "${I1_TOP[$label]:-}")" \
		"$(( ${I1_TOP[$label]:-0} - 1 ))" "$(num "${I1_TOP1_ACCEPTED[$label]:-}")" \
		"$(num "${I1_HIGHEST[$label]:-}")"
	printf 'STAGE %s-defect elapsed_s=%s\n' "$label" "$(stage_elapsed)"

	# --- I2 hidden loader descriptors (measurement) ---------------------
	I2_VERDICT[$label]="MEASURED"
	start_stage "$label-hidden" "$stage_timeout" "'$guest_bin' hidden $pre_ms $post_ms"
	if wait_marker '^RI_BARRIER pre$' 90; then
		if ! window_open '^RI_BARRIER action-done$'; then
			I2_VERDICT[$label]="UNPROVEN: sample window closed before the host read it"
		else
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
				I2_VERDICT[$label]="UNPROVEN: no guest pid in the stage log"
			else
				snapshot_prefd="$work/$label-hidden.fds"
				host_fd_snapshot "$pid" "$snapshot_prefd"
				I2_HOST[$label]="$(fd_count "$snapshot_prefd")"
			fi
		fi
	else
		I2_VERDICT[$label]="UNPROVEN: guest never reached its pre barrier"
	fi
	if ! wait_marker '^RI_BARRIER action-done$' "$stage_timeout"; then
		I2_VERDICT[$label]="UNPROVEN: guest never reached its action barrier"
	fi
	if [ "${I2_VERDICT[$label]}" = "MEASURED" ]; then
		I2_GUEST[$label]="$(field_of "$STAGE_LOG" '^I2 visible ' guest_visible_count)"
		{
			field_of "$STAGE_LOG" '^I2 visible ' list |
				tr -d '[]' | tr ',' '\n' | grep -E '^[0-9]+$' | LC_ALL=C sort || true
		} >"$work/$label-hidden.guestlist"
		{
			grep -E '^I2 .*dir=.* list=\[' "$STAGE_LOG" |
				sed -n 's/.*list=\[\([^]]*\)\].*/\1/p' |
				tr ',' '\n' | grep -E '^[0-9]+$' | LC_ALL=C sort -u || true
		} >"$work/$label-hidden.listed"
		fd_numbers_sorted "$snapshot_prefd" >"$work/$label-hidden.hostlist"
		LC_ALL=C comm -23 "$work/$label-hidden.hostlist" \
			"$work/$label-hidden.guestlist" >"$work/$label-hidden.only"
		I2_HIDDEN[$label]="$(fd_count "$work/$label-hidden.only")"
	fi
	# the guest is still holding: describe every host-only descriptor before
	# its loader sockets disappear with the process
	if [ "${I2_VERDICT[$label]}" = "MEASURED" ]; then
		{
			while read -r only_fd; do
				[ -n "$only_fd" ] || continue
				value="$(fd_target "$snapshot_prefd" "$only_fd")"
				listed=0
				LC_ALL=C grep -qx -- "$only_fd" "$work/$label-hidden.listed" && listed=1
				if is_int "${I1_TOP[$label]:-}" && [ "$only_fd" -ge "${I1_TOP[$label]}" ]; then
					above=1
				else
					above=0
				fi
				printf 'I2 hostonly prefix=%s above_advertised_range=%s listed_by_guest_fd_dir=%s %s\n' \
					"$label" "$above" "$listed" \
					"$(describe_fd_target "$only_fd" "$value")"
			done <"$work/$label-hidden.only"
		} >"$work/$label-hidden.hostonly.txt"
	fi
	finish_stage
	guest_lines "$STAGE_LOG" '^I2 '
	if [ "${I2_VERDICT[$label]}" = "MEASURED" ]; then
		printf 'I2 %s prefix=%s host_fd_count=%s guest_visible_count=%s hidden_delta=%s advertised=%s\n' \
			"${I2_VERDICT[$label]}" "$label" "$(num "${I2_HOST[$label]:-}")" \
			"$(num "${I2_GUEST[$label]:-}")" "${I2_HIDDEN[$label]}" \
			"$(num "${I1_TOP[$label]:-}")"
		cat "$work/$label-hidden.hostonly.txt" 2>/dev/null || true
		printf 'I2 guestlist prefix=%s range=[0,%s) list=[%s]\n' "$label" \
			"$(num "${I1_TOP[$label]:-}")" \
			"$(LC_ALL=C sort -n "$work/$label-hidden.guestlist" | paste -sd,)"
		printf 'I2 guestdirs prefix=%s listed_by_guest_fd_dir=[%s]\n' "$label" \
			"$(LC_ALL=C sort -n "$work/$label-hidden.listed" | paste -sd,)"
		printf 'I2 enumeration prefix=%s fcntl_scan_visible=%s guest_readdir_entries=%s guest_ls_entries=%s\n' \
			"$label" "$(num "${I2_GUEST[$label]:-}")" \
			"$(num "$(field_of "$STAGE_LOG" '^I2 devfd ' entries)")" \
			"$(num "$(field_of "$STAGE_LOG" '^I2 devfd .*via=ls' entries)")"
	else
		printf 'I2 %s prefix=%s host_fd_count=%s\n' "${I2_VERDICT[$label]}" \
			"$label" "$(num "${I2_HOST[$label]:-}")"
	fi
	printf 'STAGE %s-hidden elapsed_s=%s\n' "$label" "$(stage_elapsed)"

	# --- I3 per-thread cost (measurement) -------------------------------
	I3_VERDICT[$label]="MEASURED"
	start_stage "$label-threads" "$stage_timeout" \
		"'$guest_bin' threads $threads $pre_ms $post_ms"
	if wait_marker '^RI_BARRIER pre$' 90; then
		if ! window_open '^RI_BARRIER action-done$'; then
			I3_VERDICT[$label]="UNPROVEN: sample window closed before the host read it"
		else
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
				I3_VERDICT[$label]="UNPROVEN: no guest pid in the stage log"
			else
				snapshot_prefd="$work/$label-threads-pre.fds"
				host_fd_snapshot "$pid" "$snapshot_prefd"
				I3_PRE[$label]="$(fd_count "$snapshot_prefd")"
			fi
		fi
	else
		I3_VERDICT[$label]="UNPROVEN: guest never reached its pre barrier"
	fi
	if wait_marker '^RI_BARRIER action-done$' "$stage_timeout"; then
		if [ "${I3_VERDICT[$label]}" = "MEASURED" ]; then
			snapshot_postfd="$work/$label-threads-post.fds"
			host_fd_snapshot "$pid" "$snapshot_postfd"
			I3_POST[$label]="$(fd_count "$snapshot_postfd")"
			I3_DELTA[$label]="$(( I3_POST[$label] - I3_PRE[$label] ))"
			I3_PER_THREAD[$label]="$(per_request "${I3_DELTA[$label]}" "$threads")"
		fi
	else
		I3_VERDICT[$label]="UNPROVEN: guest never reached its action barrier"
	fi
	finish_stage
	guest_lines "$STAGE_LOG" '^I3 '
	printf 'I3 %s prefix=%s threads=%s baseline_scope=same_process_before_threads host_fd_pre=%s host_fd_post=%s delta=%s descriptors_per_thread=%s\n' \
		"${I3_VERDICT[$label]}" "$label" "$threads" "$(num "${I3_PRE[$label]:-}")" \
		"$(num "${I3_POST[$label]:-}")" "$(num "${I3_DELTA[$label]:-}")" \
		"$(num "${I3_PER_THREAD[$label]:-}")"
	printf 'STAGE %s-threads elapsed_s=%s\n' "$label" "$(stage_elapsed)"

	# --- I4 ring attach cost (measurement) ------------------------------
	I4_VERDICT[$label]="MEASURED"
	I4_KINDS[$label]=""
	start_stage "$label-ring" "$stage_timeout" \
		"'$guest_bin' ring $warmup $pre_ms $post_ms"
	if wait_marker '^RI_BARRIER pre$' 90; then
		if ! window_open '^RI_BARRIER action-done$'; then
			I4_VERDICT[$label]="UNPROVEN: sample window closed before the host read it"
		else
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
				I4_VERDICT[$label]="UNPROVEN: no guest pid in the stage log"
			else
				host_fd_snapshot "$pid" "$work/$label-ring-pre.fds"
				I4_PRE[$label]="$(fd_count "$work/$label-ring-pre.fds")"
			fi
		fi
	else
		I4_VERDICT[$label]="UNPROVEN: guest never reached its pre barrier"
	fi
	if wait_marker '^RI_BARRIER action-done$' "$stage_timeout"; then
		if [ "${I4_VERDICT[$label]}" = "MEASURED" ]; then
			host_fd_snapshot "$pid" "$work/$label-ring-post.fds"
			I4_POST[$label]="$(fd_count "$work/$label-ring-post.fds")"
			I4_DELTA[$label]="$(( I4_POST[$label] - I4_PRE[$label] ))"
			fd_numbers_sorted "$work/$label-ring-pre.fds" >"$work/$label-ring-pre.nums"
			fd_numbers_sorted "$work/$label-ring-post.fds" >"$work/$label-ring-post.nums"
			LC_ALL=C comm -13 "$work/$label-ring-pre.nums" "$work/$label-ring-post.nums" \
				>"$work/$label-ring-new.nums"
			{
				while read -r new_fd; do
					[ -n "$new_fd" ] || continue
					value="$(fd_target "$work/$label-ring-post.fds" "$new_fd")"
					printf 'I4 new-fd prefix=%s %s\n' "$label" \
						"$(describe_fd_target "$new_fd" "$value")"
					I4_KINDS[$label]="${I4_KINDS[$label]}${I4_KINDS[$label]:+,}${value}"
				done <"$work/$label-ring-new.nums"
			} >"$work/$label-ring-new.txt"
		fi
	else
		I4_VERDICT[$label]="UNPROVEN: guest never reached its action barrier"
	fi
	finish_stage
	guest_lines "$STAGE_LOG" '^I4 '
	if [ -f "$work/$label-ring-new.txt" ]; then
		cat "$work/$label-ring-new.txt"
	fi
	printf 'I4 %s prefix=%s warmup=%s host_fd_pre=%s host_fd_post=%s delta=%s new_fd_targets=[%s]\n' \
		"${I4_VERDICT[$label]}" "$label" "$warmup" "$(num "${I4_PRE[$label]:-}")" \
		"$(num "${I4_POST[$label]:-}")" "$(num "${I4_DELTA[$label]:-}")" \
		"${I4_KINDS[$label]}"
	printf 'I4 NOTE prefix=%s loader_descriptors_present_at_process_start=%s (from I2 hidden_delta) descriptors_added_by_the_warmup=%s\n' \
		"$label" "$(num "${I2_HIDDEN[$label]:-}")" "$(num "${I4_DELTA[$label]:-}")"
	printf 'STAGE %s-ring elapsed_s=%s\n' "$label" "$(stage_elapsed)"

	# --- I5 descriptor transfer (must pass) ----------------------------
	I5_VERDICT[$label]="FAIL: no transfer evidence"
	start_stage "$label-scm" "$stage_timeout" "'$guest_bin' scm $pre_ms $post_ms"
	if wait_marker '^RI_BARRIER pre$' 90; then
		if ! window_open '^RI_BARRIER action-done$'; then
			I5_VERDICT[$label]="FAIL: sample window closed before the host read it"
		else
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
				I5_VERDICT[$label]="FAIL: no guest pid in the stage log"
			else
				host_fd_snapshot "$pid" "$work/$label-scm-pre.fds"
			fi
		fi
	else
		I5_VERDICT[$label]="FAIL: guest never reached its pre barrier"
	fi
	if wait_marker '^RI_BARRIER action-done$' "$stage_timeout"; then
		if [ -n "$pid" ] && [ -d "/proc/$pid" ] && [ -s "$work/$label-scm-pre.fds" ]; then
			host_fd_snapshot "$pid" "$work/$label-scm-post.fds"
			pre_count="$(fd_count "$work/$label-scm-pre.fds")"
			post_count="$(fd_count "$work/$label-scm-post.fds")"
			I5_DELTA[$label]="$(( post_count - pre_count ))"
		fi
	else
		I5_VERDICT[$label]="FAIL: guest never reached its action barrier"
	fi
	finish_stage
	guest_lines "$STAGE_LOG" '^I5 '
	if grep -qE '^I5 verdict scm_rights_round_trip=1 sender_unaffected=1$' "$STAGE_LOG"; then
		I5_VERDICT[$label]=PASS
	else
		I5_VERDICT[$label]="FAIL: the guest did not confirm the SCM_RIGHTS round trip"
		must_pass_failed=1
	fi
	printf 'I5 %s prefix=%s host_fd_delta_across_transfer=%s\n' \
		"${I5_VERDICT[$label]}" "$label" "$(num "${I5_DELTA[$label]:-}")"
	printf 'STAGE %s-scm elapsed_s=%s\n' "$label" "$(stage_elapsed)"

	# --- I6 close_range consequence (measurement) -----------------------
	I6_VERDICT[$label]="MEASURED"
	local armed_count=""
	pre_count=""
	post_count=""
	start_stage "$label-close-range" 90 "'$guest_bin' close-range $pre_ms $post_ms"
	if wait_marker '^RI_BARRIER pre$' 90; then
		if ! window_open '^RI_BARRIER armed$'; then
			I6_VERDICT[$label]="UNPROVEN: sample window closed before the host read it"
		else
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
				I6_VERDICT[$label]="UNPROVEN: no guest pid in the stage log"
			else
				host_fd_snapshot "$pid" "$work/$label-close-pre.fds"
				pre_count="$(fd_count "$work/$label-close-pre.fds")"
			fi
		fi
	else
		I6_VERDICT[$label]="UNPROVEN: guest never reached its pre barrier"
	fi
	if [ "${I6_VERDICT[$label]}" = "MEASURED" ]; then
		if wait_marker '^RI_BARRIER armed$' 90; then
			if ! window_open '^RI_BARRIER action-done$'; then
				I6_VERDICT[$label]="UNPROVEN: armed sample window closed before the host read it"
			else
				host_fd_snapshot "$pid" "$work/$label-close-armed.fds"
				armed_count="$(fd_count "$work/$label-close-armed.fds")"
			fi
		else
			I6_VERDICT[$label]="UNPROVEN: guest never reached its armed barrier"
		fi
	fi
	if [ "${I6_VERDICT[$label]}" = "MEASURED" ]; then
		if wait_marker '^RI_BARRIER action-done$' 45; then
			host_fd_snapshot "$pid" "$work/$label-close-post.fds"
			post_count="$(fd_count "$work/$label-close-post.fds")"
			I6_LOST[$label]="$(( armed_count - post_count ))"
			fd_numbers_sorted "$work/$label-close-armed.fds" >"$work/$label-close-armed.nums"
			fd_numbers_sorted "$work/$label-close-post.fds" >"$work/$label-close-post.nums"
			LC_ALL=C comm -23 "$work/$label-close-armed.nums" "$work/$label-close-post.nums" \
				>"$work/$label-close-lost.nums"
			{
				field_of "$STAGE_LOG" '^I6 visible ' list |
					tr -d '[]' | tr ',' '\n' | grep -E '^[0-9]+$' | LC_ALL=C sort || true
			} >"$work/$label-close-visible"
			{
				printf 'I6 remaining-targets prefix=%s\n' "$label"
				while read -r keep_fd; do
					[ -n "$keep_fd" ] || continue
					visible=0
					LC_ALL=C grep -qx -- "$keep_fd" "$work/$label-close-visible" && visible=1
					printf 'I6 remaining-fd prefix=%s guest_visible_before_close=%s %s\n' "$label" \
						"$visible" \
						"$(describe_fd_target "$keep_fd" "$(fd_target "$work/$label-close-post.fds" "$keep_fd")")"
				done <"$work/$label-close-post.nums"
				printf 'I6 lost-fds prefix=%s\n' "$label"
				while read -r lost_fd; do
					[ -n "$lost_fd" ] || continue
					visible=0
					LC_ALL=C grep -qx -- "$lost_fd" "$work/$label-close-visible" && visible=1
					printf 'I6 lost-fd prefix=%s guest_visible_before_close=%s %s\n' "$label" \
						"$visible" \
						"$(describe_fd_target "$lost_fd" "$(fd_target "$work/$label-close-armed.fds" "$lost_fd")")"
				done <"$work/$label-close-lost.nums"
			} >"$work/$label-close-detail.txt"
		else
			I6_VERDICT[$label]="UNPROVEN: the guest did not survive to its action barrier (the loader lost or blocked after closing the descriptor band)"
		fi
	fi
	finish_stage
	guest_lines "$STAGE_LOG" '^I6 '
	if [ "${I6_VERDICT[$label]}" = "MEASURED" ]; then
		printf 'I6 %s prefix=%s stage_rc=%s host_fd_baseline_before_rpc=%s host_fd_armed_before_close=%s host_fd_after_close=%s descriptors_lost_by_loader=%s remaining_after_close=[%s] guest_visible_before_close=%s close_sweep_monotonic_ms=%s close_sweep_wallclock_ms=%s monotonic_went_backwards=%s\n' \
			"${I6_VERDICT[$label]}" "$label" "$STAGE_RC" "$(num "${pre_count:-}")" \
			"$armed_count" "$post_count" "${I6_LOST[$label]}" \
			"$(LC_ALL=C sort -n "$work/$label-close-post.nums" | paste -sd,)" \
			"$(field_of "$STAGE_LOG" '^I6 visible ' guest_visible_count)" \
			"$(num "$(field_of "$STAGE_LOG" '^I6 close ' elapsed_ms)")" \
			"$(num "$(field_of "$STAGE_LOG" '^I6 close ' wallclock_ms)")" \
			"$(num "$(field_of "$STAGE_LOG" '^I6 close ' monotonic_went_backwards)")"
		if [ "$(field_of "$STAGE_LOG" '^I6 close ' monotonic_went_backwards)" = "1" ]; then
			printf 'I6 NOTE prefix=%s the guest CLOCK_MONOTONIC stepped backwards across the close sweep, so wallclock_ms is the usable duration; the disagreement is itself a runtime observation, not a harness artifact\n' \
				"$label"
		fi
		cat "$work/$label-close-detail.txt" 2>/dev/null || true
	else
		printf 'I6 %s prefix=%s stage_rc=%s\n' "${I6_VERDICT[$label]}" \
			"$label" "$STAGE_RC"
	fi
	printf 'STAGE %s-close-range elapsed_s=%s\n' "$label" "$(stage_elapsed)"

	# --- I7 transport cost (measurement) --------------------------------
	I7_VERDICT[$label]="MEASURED"
	start_stage "$label-rpc" "$stage_timeout" \
		"'$guest_bin' rpc $rpc_count $rpc_pre_ms $post_ms"
	local strace_pid=""
	local strace_out="$work/$label-strace.txt"
	local strace_err="$work/$label-strace.err"
	local server_strace_pid=""
	local server_strace_out="$work/$label-server-strace.txt"
	local server_strace_err="$work/$label-server-strace.err"
	local server_pid=""
	local attach_argv=()
	local counters_pre="" counters_post=""
	local snap_pre="$work/$label-stat-pre.json" snap_post="$work/$label-stat-post.json"
	local vol="" nonvol="" utime="" stime="" syscr="" syscw=""
	local dvol="" dnonvol="" dutime="" dstime="" dsyscr="" dsyscw=""

	if wait_marker '^RI_BARRIER pre$' 90; then
		if ! window_open '^RI_BARRIER action-done$'; then
			I7_VERDICT[$label]="UNPROVEN: sample window closed before the host read it"
		else
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
				I7_VERDICT[$label]="UNPROVEN: no guest pid in the stage log"
			else
				# This window is deliberately untraced: strace stops the traced
				# task on every syscall, so a traced window cannot measure time.
				# The counts come from the separate traced window below.
				counters_pre="$(proc_counters "$pid")"
				ring_snapshot "$prefix" "$snap_pre" || true
			fi
		fi
	else
		I7_VERDICT[$label]="UNPROVEN: guest never reached its pre barrier"
	fi
	if wait_marker '^RI_BARRIER action-done$' "$stage_timeout"; then
		if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
			counters_post="$(proc_counters "$pid")"
		fi
		ring_snapshot "$prefix" "$snap_post" || true
	else
		I7_VERDICT[$label]="UNPROVEN: guest never reached its action barrier"
	fi
	finish_stage
	guest_lines "$STAGE_LOG" '^I7 '

	vol="$(printf '%s' "$counters_pre" | awk '{print $1}')"
	nonvol="$(printf '%s' "$counters_pre" | awk '{print $2}')"
	utime="$(printf '%s' "$counters_pre" | awk '{print $3}')"
	stime="$(printf '%s' "$counters_pre" | awk '{print $4}')"
	syscr="$(printf '%s' "$counters_pre" | awk '{print $5}')"
	syscw="$(printf '%s' "$counters_pre" | awk '{print $6}')"
	if [ "${I7_VERDICT[$label]}" = "MEASURED" ] && is_int "$vol" && is_int "$utime" &&
		is_int "$syscr" &&
		is_int "$(printf '%s' "$counters_post" | awk '{print $1}')"; then
		dvol="$(( $(printf '%s' "$counters_post" | awk '{print $1}') - vol ))"
		dnonvol="$(( $(printf '%s' "$counters_post" | awk '{print $2}') - nonvol ))"
		dutime="$(( $(printf '%s' "$counters_post" | awk '{print $3}') - utime ))"
		dstime="$(( $(printf '%s' "$counters_post" | awk '{print $4}') - stime ))"
		dsyscr="$(( $(printf '%s' "$counters_post" | awk '{print $5}') - syscr ))"
		dsyscw="$(( $(printf '%s' "$counters_post" | awk '{print $6}') - syscw ))"
		I7_VOL[$label]="$dvol"
		I7_NONVOL[$label]="$dnonvol"
		I7_UTIME[$label]="$dutime"
		I7_STIME[$label]="$dstime"
		I7_SYSCR[$label]="$dsyscr"
		I7_SYSCW[$label]="$dsyscw"
		I7_WALL[$label]="$(field_of "$STAGE_LOG" '^I7 workload ' us_per_call)"
		printf 'I7 %s prefix=%s rpc=%s guest_us_per_call=%s ctxt_vol_delta=%s ctxt_nonvol_delta=%s ctxt_vol_per_request=%s ctxt_nonvol_per_request=%s utime_ticks_per_request=%s stime_ticks_per_request=%s syscr_delta=%s syscw_delta=%s syscw_per_request=%s\n' \
			"${I7_VERDICT[$label]}" "$label" "$rpc_count" \
			"$(num "${I7_WALL[$label]:-}")" "$dvol" "$dnonvol" \
			"$(per_request "$dvol" "$rpc_count")" \
			"$(per_request "$dnonvol" "$rpc_count")" \
			"$(per_request "$dutime" "$rpc_count")" \
			"$(per_request "$dstime" "$rpc_count")" "$dsyscr" "$dsyscw" \
			"$(per_request "$dsyscw" "$rpc_count")"
		printf 'I7 NOTE prefix=%s the workload starts cold: on a ring build the first mach traps of the %s calls attach the transport (see the I4 warm-up), so the attach cost is amortised inside this window, not excluded from it\n' \
			"$label" "$rpc_count"
	else
		printf 'I7 %s prefix=%s rpc=%s\n' "${I7_VERDICT[$label]}" "$label" "$rpc_count"
	fi

	# --- I8 wake attribution from the server's own counters --------------
	# Same untraced window as the I7 timing line above, so the attribution
	# belongs to exactly those numbers. No privilege and no strace: strace slows
	# the guest enough that the server keeps polling, which suppresses the very
	# doorbell this claim is about, while these counters live in the server.
	local s_spin s_door s_wiss s_wsk p_spin p_door p_wiss p_wsk s_served p_served
	local d_spin d_door d_wiss d_wsk d_served rpcs
	s_spin="$(ring_counter "$snap_pre" ring_serviced_spin)"
	s_door="$(ring_counter "$snap_pre" ring_serviced_doorbell)"
	s_wiss="$(ring_counter "$snap_pre" ring_wakes_issued)"
	s_wsk="$(ring_counter "$snap_pre" ring_wakes_skipped)"
	p_spin="$(ring_counter "$snap_post" ring_serviced_spin)"
	p_door="$(ring_counter "$snap_post" ring_serviced_doorbell)"
	p_wiss="$(ring_counter "$snap_post" ring_wakes_issued)"
	p_wsk="$(ring_counter "$snap_post" ring_wakes_skipped)"
	s_served="$(ring_counter "$snap_pre" ring_serviced)"
	p_served="$(ring_counter "$snap_post" ring_serviced)"
	rpcs="$(ring_per_call "$snap_post" dserver_callnum_host_self_trap)"
	[ -n "$rpcs" ] || rpcs="$(ring_per_call "$snap_pre" dserver_callnum_host_self_trap)"

	if is_int "$s_spin" && is_int "$p_spin" && is_int "$s_door" && is_int "$p_door"; then
		d_spin="$(( p_spin - s_spin ))"
		d_door="$(( p_door - s_door ))"
		d_wiss="$(( p_wiss - s_wiss ))"
		d_wsk="$(( p_wsk - s_wsk ))"
		d_served="?"
		if is_int "$s_served" && is_int "$p_served"; then
			d_served="$(( p_served - s_served ))"
		fi
		printf 'I8 MEASURED prefix=%s ring_serviced_delta=%s spin=%s doorbell=%s doorbell_share=%s wakes_issued=%s wakes_skipped=%s rpcs_this_window=%s doorbell_per_request=%s wakes_issued_per_request=%s ctxt_vol_per_request=%s syscw_per_request=%s\n' \
			"$label" "$d_served" "$d_spin" "$d_door" \
			"$(awk -v d="$d_door" -v s="$d_spin" 'BEGIN { t = d + s; printf "%.4f", (t > 0 ? d / t : 0) }')" \
			"$d_wiss" "$d_wsk" "${rpcs:-?}" \
			"$(per_request "$d_door" "${rpcs:-0}")" \
			"$(per_request "$d_wiss" "${rpcs:-0}")" \
			"$(per_request "${I7_VOL[$label]:-0}" "$rpc_count")" \
			"$(per_request "${I7_SYSCW[$label]:-0}" "$rpc_count")"
		printf 'I8 NOTE prefix=%s the doorbell share is the fraction of ring services that took the doorbell path rather than the poll path in this untraced window; compare doorbell_per_request with the write syscalls per request reported on the I7 line, and wakes_issued_per_request with the voluntary context switches per request, since a doorbell is what makes the guest block\n' \
			"$label"
	elif [ -s "$snap_pre" ]; then
		printf 'I8 MEASURED prefix=%s ring_counters=absent_in_this_build rpcs_this_window=%s note=this build exposes no ring counters, so the wake path has nothing to attribute here and the absence is expected rather than a failure\n' \
			"$label" "${rpcs:-?}"
	else
		printf 'I8 UNPROVEN prefix=%s the stat socket could not be read: %s\n' "$label" \
			"$(tr -d '\n' <"$snap_pre.err" 2>/dev/null | head -c 160)"
	fi

	# --- I7 transport classification under trace (counts only) ----------
	# A separate, smaller window: strace stops the traced task on every syscall,
	# so the timing window above must stay untraced and this one must stay small.
	local strace_pid="" strace_out="$work/$label-strace.txt"
	local strace_err="$work/$label-strace.err"
	local server_strace_pid="" server_strace_out="$work/$label-server-strace.txt"
	local server_strace_err="$work/$label-server-strace.err"
	local trace_attach=()

	if [ "$strace_mode" = "none" ]; then
		I7_STRACE[$label]="UNPROVEN: RELAY_INTEGRATION_STRACE=none"
	elif [ "${#strace_argv[@]}" -gt 0 ]; then
		trace_attach=("${strace_argv[@]}")
	else
		I7_STRACE[$label]="UNPROVEN: $strace_unavailable"
	fi

	if [ "${#trace_attach[@]}" -gt 0 ]; then
		start_stage "$label-rpc-traced" "$strace_stage_timeout" \
			"'$guest_bin' rpc $strace_rpc_count $rpc_pre_ms $post_ms"
		if wait_marker '^RI_BARRIER pre$' 90; then
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
				I7_STRACE[$label]="UNPROVEN: no guest pid in the traced stage log"
			elif ! window_open '^RI_BARRIER action-done$'; then
				I7_STRACE[$label]="UNPROVEN: traced window closed before the attach"
			else
				: >"$strace_err"
				"${trace_attach[@]}" -f -c -o "$strace_out" -p "$pid" >"$strace_err" 2>&1 &
				strace_pid=$!
				# The server is the other half of the same round trip.
				server_pid="$(pgrep -f "darlingserver .*$(basename "$prefix")( |$)" 2>/dev/null |
					head -1 || true)"
				if [ -n "$server_pid" ] && [ -d "/proc/$server_pid" ]; then
					: >"$server_strace_err"
					"${trace_attach[@]}" -f -c -o "$server_strace_out" \
						-p "$server_pid" >"$server_strace_err" 2>&1 &
					server_strace_pid=$!
				fi
				sleep 1
			fi
		else
			I7_STRACE[$label]="UNPROVEN: guest never reached its pre barrier in the traced window"
		fi
		if ! wait_marker '^RI_BARRIER action-done$' "$strace_stage_timeout"; then
			I7_STRACE[$label]="UNPROVEN: the traced workload did not finish within ${strace_stage_timeout}s; lower RELAY_INTEGRATION_STRACE_RPC (now $strace_rpc_count)"
		fi
		if [ -n "$strace_pid" ]; then
			set +e
			"${strace_kill_argv[@]:-kill}" -INT "$strace_pid" 2>/dev/null
			wait "$strace_pid" 2>/dev/null
			if [ -n "$server_strace_pid" ]; then
				"${strace_kill_argv[@]:-kill}" -INT "$server_strace_pid" 2>/dev/null
				wait "$server_strace_pid" 2>/dev/null
			fi
			set -e
		fi
		if [ -s "$strace_out" ]; then
			I7_TOTAL_GUEST[$label]="$(strace_total "$strace_out")"
			I7_CLASS_GUEST[$label]="$(strace_class "$strace_out")"
		elif [ -z "${I7_STRACE[$label]:-}" ]; then
			I7_STRACE[$label]="UNPROVEN: strace -f -c -p could not attach: $(tr -d '\n' <"$strace_err" | head -c 200)"
		fi
		if [ -s "$server_strace_out" ]; then
			I7_TOTAL_SERVER[$label]="$(strace_total "$server_strace_out")"
			I7_CLASS_SERVER[$label]="$(strace_class "$server_strace_out")"
		elif [ -n "$server_strace_pid" ]; then
			I7_CLASS_SERVER[$label]="UNPROVEN: server attach produced nothing: $(tr -d '\n' <"$server_strace_err" | head -c 160)"
		fi
		finish_stage
		if [ -n "${I7_TOTAL_GUEST[$label]:-}" ] || [ -n "${I7_TOTAL_SERVER[$label]:-}" ]; then
			printf 'I7 TRACED prefix=%s rpc=%s role=guest syscalls_total=%s syscalls_per_request=%s [%s]\n' \
				"$label" "$strace_rpc_count" "${I7_TOTAL_GUEST[$label]:-UNPROVEN}" \
				"$(per_request "${I7_TOTAL_GUEST[$label]:-0}" "$strace_rpc_count")" \
				"${I7_CLASS_GUEST[$label]:-UNPROVEN}"
			if [ -n "${I7_TOTAL_SERVER[$label]:-}" ]; then
				printf 'I7 TRACED prefix=%s rpc=%s role=server syscalls_total=%s syscalls_per_request=%s [%s]\n' \
					"$label" "$strace_rpc_count" "${I7_TOTAL_SERVER[$label]:-UNPROVEN}" \
					"$(per_request "${I7_TOTAL_SERVER[$label]:-0}" "$strace_rpc_count")" \
					"${I7_CLASS_SERVER[$label]:-UNPROVEN}"
			else
				printf 'I7 TRACED prefix=%s rpc=%s role=server syscalls_total=UNPROVEN [%s]\n' \
					"$label" "$strace_rpc_count" "${I7_CLASS_SERVER[$label]:-not attached}"
			fi
			printf 'I7 TRACED-CAVEAT prefix=%s this window is smaller and traced, so its wall time is not comparable with the untraced timing line above; the counts and the per-request figure derived from them are the evidence, and the class list names the syscalls that carry the transport\n' \
				"$label"
		else
			printf 'I7 TRACED prefix=%s rpc=%s syscalls_total=UNPROVEN: %s\n' \
				"$label" "$strace_rpc_count" "${I7_STRACE[$label]:-not attached}"
		fi
	fi
	printf 'STAGE %s-rpc elapsed_s=%s\n' "$label" "$(stage_elapsed)"

	# --- guest artifact cleanup, then shut the prefix down ---------------
	remove_guest_artifacts "$prefix"
	shutdown_prefix "$label" "$prefix"
	leg_active=""
	leg_active_prefix=""
}

# ------------------------------------------------------------ stage fixture

stage_fixture() { # prefix label
	local target="$1"
	local label="$2"
	local source_literal
	local rc=0

	source_literal="$(printf '%q' "$(<"$fixture_source")")"

	set +e
	darling_guest_shell "$target/bin/darling" "$target" 120 \
		"umask 077; printf '%s' $source_literal > $guest_src" \
		>"$work/stage-$label.log" 2>&1
	rc=$?
	set -e
	if [ "$rc" -ne 0 ]; then
		printf 'RELAY_INTEGRATION_HARNESS_FAIL prefix=%s stage=upload rc=%s log=%s\n' \
			"$label" "$rc" "$work/stage-$label.log" >&2
		cat "$work/stage-$label.log" >&2
		return 1
	fi

	set +e
	darling_guest_shell "$target/bin/darling" "$target" 300 \
		"$guest_cc -isysroot $guest_sdk -O1 -Wno-deprecated-declarations -o $guest_bin $guest_src; printf 'COMPILE_RC=%s\n' \$?" \
		>>"$work/stage-$label.log" 2>&1
	rc=$?
	set -e
	if [ "$rc" -ne 0 ] || ! grep -q '^COMPILE_RC=0$' "$work/stage-$label.log"; then
		printf 'RELAY_INTEGRATION_HARNESS_FAIL prefix=%s stage=compile rc=%s log=%s\n' \
			"$label" "$rc" "$work/stage-$label.log" >&2
		cat "$work/stage-$label.log" >&2
		return 1
	fi
	printf 'STAGED prefix=%s guest_src=%s guest_bin=%s compile=ok\n' "$label" \
		"$guest_src" "$guest_bin"
	return 0
}

note "RELAY_INTEGRATION_PROOF on=$prefix_on off=$prefix_off threads=$threads warmup=$warmup rpc=$rpc_count pre_ms=$pre_ms post_ms=$post_ms"

[ -r "$fixture_source" ] || {
	printf 'RELAY_INTEGRATION_HARNESS_FAIL missing fixture source %s\n' "$fixture_source" >&2
	exit 2
}

harness_failed=0
stage_fixture "$prefix_on" on || harness_failed=1
if [ "$harness_failed" -eq 0 ]; then
	stage_fixture "$prefix_off" off || harness_failed=1
fi
if [ "$harness_failed" -ne 0 ]; then
	shutdown_prefix on "$prefix_on"
	shutdown_prefix off "$prefix_off"
	printf 'RELAY_INTEGRATION_HARNESS_FAIL the guest fixture did not stage or compile\n' >&2
	exit 2
fi

run_leg on "$prefix_on"
run_leg off "$prefix_off"

# ------------------------------------------------------------- comparison

note "== ON versus OFF =="
printf 'I1 COMPARE on_advertised=%s on_top_minus_one_accepted=%s on_highest_accepted=%s off_advertised=%s off_top_minus_one_accepted=%s off_highest_accepted=%s on=%s off=%s\n' \
	"$(num "${I1_TOP[on]:-}")" "$(num "${I1_TOP1_ACCEPTED[on]:-}")" \
	"$(num "${I1_HIGHEST[on]:-}")" "$(num "${I1_TOP[off]:-}")" \
	"$(num "${I1_TOP1_ACCEPTED[off]:-}")" "$(num "${I1_HIGHEST[off]:-}")" \
	"${I1_VERDICT[on]:-}" "${I1_VERDICT[off]:-}"
if [ "${I1_TOP[on]:-}" != "${I1_TOP[off]:-}" ]; then
	printf 'I1 NOTE the two prefixes advertise different limits: on=%s off=%s\n' \
		"$(num "${I1_TOP[on]:-}")" "$(num "${I1_TOP[off]:-}")"
fi
printf 'I2 COMPARE on_host=%s on_guest=%s on_hidden=%s off_host=%s off_guest=%s off_hidden=%s\n' \
	"$(num "${I2_HOST[on]:-}")" "$(num "${I2_GUEST[on]:-}")" \
	"$(num "${I2_HIDDEN[on]:-}")" "$(num "${I2_HOST[off]:-}")" \
	"$(num "${I2_GUEST[off]:-}")" "$(num "${I2_HIDDEN[off]:-}")"
printf 'I3 COMPARE threads=%s on_delta=%s on_per_thread=%s off_delta=%s off_per_thread=%s\n' \
	"$threads" "$(num "${I3_DELTA[on]:-}")" "$(num "${I3_PER_THREAD[on]:-}")" \
	"$(num "${I3_DELTA[off]:-}")" "$(num "${I3_PER_THREAD[off]:-}")"
printf 'I4 COMPARE warmup=%s on_delta=%s on_targets=[%s] off_delta=%s off_targets=[%s]\n' \
	"$warmup" "$(num "${I4_DELTA[on]:-}")" "${I4_KINDS[on]:-}" \
	"$(num "${I4_DELTA[off]:-}")" "${I4_KINDS[off]:-}"
printf 'I5 COMPARE on=%s on_host_fd_delta=%s off=%s off_host_fd_delta=%s\n' \
	"${I5_VERDICT[on]:-}" "$(num "${I5_DELTA[on]:-}")" \
	"${I5_VERDICT[off]:-}" "$(num "${I5_DELTA[off]:-}")"
printf 'I6 COMPARE on_lost=%s on=%s off_lost=%s off=%s\n' \
	"$(num "${I6_LOST[on]:-}")" "${I6_VERDICT[on]:-}" \
	"$(num "${I6_LOST[off]:-}")" "${I6_VERDICT[off]:-}"
printf 'I7 COMPARE rpc=%s on_us_per_call=%s on_ctxt_vol=%s on_ctxt_nonvol=%s on_ctxt_vol_per_request=%s on_syscw_per_request=%s on_utime_ticks_per_request=%s off_us_per_call=%s off_ctxt_vol=%s off_ctxt_nonvol=%s off_ctxt_vol_per_request=%s off_syscw_per_request=%s off_utime_ticks_per_request=%s on_traced_guest_syscalls=%s off_traced_guest_syscalls=%s\n' \
	"$rpc_count" "$(num "${I7_WALL[on]:-}")" "$(num "${I7_VOL[on]:-}")" \
	"$(num "${I7_NONVOL[on]:-}")" "$(per_request "${I7_VOL[on]:-0}" "$rpc_count")" \
	"$(per_request "${I7_SYSCW[on]:-0}" "$rpc_count")" \
	"$(per_request "${I7_UTIME[on]:-0}" "$rpc_count")" \
	"$(num "${I7_WALL[off]:-}")" "$(num "${I7_VOL[off]:-}")" \
	"$(num "${I7_NONVOL[off]:-}")" "$(per_request "${I7_VOL[off]:-0}" "$rpc_count")" \
	"$(per_request "${I7_SYSCW[off]:-0}" "$rpc_count")" \
	"$(per_request "${I7_UTIME[off]:-0}" "$rpc_count")" \
	"${I7_TOTAL_GUEST[on]:-UNPROVEN}" \
	"${I7_TOTAL_GUEST[off]:-UNPROVEN}"

if is_int "${I7_VOL[on]:-}"; then
	if [ "${I7_VOL[on]}" -eq 0 ] && [ "${I7_NONVOL[on]:-0}" -eq 0 ] &&
		[ "${I7_UTIME[on]:-1}" -eq 0 ] && [ "${I7_STIME[on]:-1}" -eq 0 ] &&
		[ "${I7_SYSCR[on]:-1}" -eq 0 ] && [ "${I7_SYSCW[on]:-1}" -eq 0 ]; then
		printf 'I7 HOTPATH prefix=on no per-request syscall or context switch was observed over %s requests (ctxt_vol=%s ctxt_nonvol=%s syscr=%s syscw=%s utime_ticks=%s stime_ticks=%s)\n' \
			"$rpc_count" "${I7_VOL[on]}" "${I7_NONVOL[on]}" "${I7_SYSCR[on]}" \
			"${I7_SYSCW[on]}" "${I7_UTIME[on]}" "${I7_STIME[on]}"
	else
		printf 'I7 HOTPATH prefix=on the hot path is NOT free of kernel work: ctxt_vol_per_request=%s ctxt_nonvol_per_request=%s syscr_delta=%s syscw_delta=%s utime_ticks_per_request=%s stime_ticks_per_request=%s\n' \
			"$(per_request "${I7_VOL[on]}" "$rpc_count")" \
			"$(per_request "${I7_NONVOL[on]:-0}" "$rpc_count")" \
			"$(num "${I7_SYSCR[on]:-}")" "$(num "${I7_SYSCW[on]:-}")" \
			"$(per_request "${I7_UTIME[on]:-0}" "$rpc_count")" \
			"$(per_request "${I7_STIME[on]:-0}" "$rpc_count")"
	fi
fi

# ------------------------------------------------------------- verdict

if [ "$must_pass_failed" -ne 0 ]; then
	printf 'RELAY_INTEGRATION_FAILED must-pass claim failed: I1=%s / %s ; I5=%s / %s\n' \
		"${I1_VERDICT[on]:-}" "${I1_VERDICT[off]:-}" \
		"${I5_VERDICT[on]:-}" "${I5_VERDICT[off]:-}"
	exit 1
fi

printf 'RELAY_INTEGRATION_OK\n'

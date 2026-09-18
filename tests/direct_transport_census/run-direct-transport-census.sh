#!/usr/bin/env bash
#
# run-direct-transport-census.sh -- measure, on the REAL Darling product
# runtime, how much RPC traffic rides the ring transport (Lane 1) versus the
# per-thread UDS socket (Lane 0 / non-ring), per dserver call number, and how
# often the non-ring operations occur per hot-path operation.  The answer
# decides whether slow-path traffic is rare enough to serialize behind ONE
# process-level control endpoint or whether some class is hot enough to keep
# its own per-thread socket.
#
# What it measures, and from where
# --------------------------------
# darlingserver's stat socket exposes, in one JSON snapshot:
#   * per_call.<dserver_callnum_...>.count  -- calls serviced per call number
#                                              (server-lifetime cumulative);
#   * the ring_* counters                  -- ring transport totals;
#   * rpc_heatmap.<callnum>.{total,uds,ring,used_fiber,caller_s2c,verdict},
#     emitted only when the server was started with DARLING_SERVER_RPC_HEATMAP=1.
#     This is the per-callnum TRANSPORT SPLIT: `uds` is exactly the non-ring
#     count and `ring` the ring-served count for that call number.
# All of these are server-lifetime cumulative, so this runner takes a snapshot
# on each side of a defined workload window and reports the DELTAS.
#
# Because the heatmap is armed in the server process' environment, the runner
# refuses to measure a prefix that already has a darlingserver running (that
# server would have been started without the hatch and would report an
# unarmed census), and it verifies rpc_heatmap_on=1 on the snapshot the boot
# produced before it runs any window.
#
# Windows
# -------
#   W-A  hot mach-trap window: DTC_HOT_THREADS guest threads (default 32), each
#        running DTC_HOT_REQUESTS mach_host_self() traps (default 6250), plus
#        one trap per thread before the start gate so every thread's transport
#        is established.  An intermediate sample is taken with every thread
#        parked on the gate, which isolates the per-thread setup traffic from
#        the trap loop; it is reported as window W-A-setup.
#   W-B  process-lifecycle window: DTC_FORK_ITERATIONS (default 40) cycles of
#        guest fork() + exec of this fixture in `noop` mode + waitpid(), with
#        one extra pipe held open across every fork (so the fork RPC carries
#        more than the three stdio descriptors).
#
# Each window reports one CENSUS line with the raw counter deltas and the
# decisive ratio (non-ring calls / hot-path calls), one CENSUS CALL line per
# call number that moved or that belongs to a named expected class (with its
# ring/uds split), the descriptor counts of the guest process seen from both
# sides (the guest's own fcntl scan and the host's /proc/<pid>/fd), and a
# CLASS line naming the expected classes that did NOT appear at all.
#
# Lifecycle
# ---------
# Boots each prefix through the guest shell transport (the server inherits
# DARLING_SERVER_RPC_HEATMAP=1 from the launcher), stages the fixture into the
# guest and compiles it with the in-prefix clang, runs the windows, removes the
# guest artifacts and shuts the prefix down with the supported
# `darling shutdown` path plus a survivor report.  A surviving prefix-owned
# process or a remaining mount reference exits non-zero.
#
# Exit status
#   0  every window produced its counter deltas, no prefix-owned survivor
#   1  a window did not produce its samples, or a counter the census needs was
#      absent (reported as UNPROVEN with the exact key)
#   2  the harness could not run (boot, staging or compilation failed)
#   3  refusal: a prefix is unset, not bootstrapped, already running, or the
#      stat tool / fixture source is missing
#   4  a prefix was shut down but prefix-owned processes or mounts survived
#
# Environment
#   DTC_PREFIX_ON        bootstrapped prefix with the ring transport
#                        (default /tmp/dr-on-matched)
#   DTC_PREFIX_OFF       matched prefix without it (default /tmp/dr-off-matched)
#   DTC_RUN_OFF          1 (default) also runs the OFF leg, 0 skips it
#   DTC_STAT_TOOL        stat client (default the ring-comparison-server one)
#   DTC_HOT_THREADS      W-A guest thread count (default 32, max 64)
#   DTC_HOT_REQUESTS     W-A traps per thread after the gate (default 6250)
#   DTC_HOT_PRE_MS       W-A pre hold (default 1500)
#   DTC_HOT_HOLD_MS      W-A gate hold with every thread parked (default 1500)
#   DTC_HOT_POST_MS      W-A post hold (default 1500)
#   DTC_FORK_ITERATIONS  W-B fork+exec cycles (default 40)
#   DTC_FORK_PRE_MS      W-B pre hold (default 1500)
#   DTC_FORK_POST_MS     W-B post hold (default 1500)
#   DTC_BOOT_TIMEOUT     boot command timeout, seconds (default 300)
#   DTC_STAGE_TIMEOUT    guest stage timeout, seconds (default 240)
#
# No product source is modified and nothing is committed.  Only the prefixes
# named above are touched, and each one is verified free of a pre-existing
# darlingserver before it is booted.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace="$(cd "$here/../.." && pwd)"
# shellcheck source=../../testkit/scripts/darling-guest-shell.sh
source "$workspace/testkit/scripts/darling-guest-shell.sh"

# The launcher and the shutdown path both need the rootless runtime selection;
# the boot's darlingserver additionally inherits DARLING_SERVER_RPC_HEATMAP.
export DARLING_ROOTLESS=1
export DARLING_NOOVERLAYFS=1
export DARLING_EUNION=1

prefix_on="${DTC_PREFIX_ON:-/tmp/dr-on-matched}"
prefix_off="${DTC_PREFIX_OFF:-/tmp/dr-off-matched}"
run_off="${DTC_RUN_OFF:-1}"
stat_tool="${DTC_STAT_TOOL:-/home/ilyagulya/work/darling-gwn-resume/source-fixes/ring-comparison-server/tools/darling-stat}"

hot_threads="${DTC_HOT_THREADS:-32}"
hot_requests="${DTC_HOT_REQUESTS:-6250}"
hot_pre_ms="${DTC_HOT_PRE_MS:-1500}"
hot_hold_ms="${DTC_HOT_HOLD_MS:-1500}"
hot_post_ms="${DTC_HOT_POST_MS:-1500}"
fork_iterations="${DTC_FORK_ITERATIONS:-40}"
fork_pre_ms="${DTC_FORK_PRE_MS:-1500}"
fork_post_ms="${DTC_FORK_POST_MS:-1500}"
boot_timeout="${DTC_BOOT_TIMEOUT:-300}"
stage_timeout="${DTC_STAGE_TIMEOUT:-240}"
[ "$hot_threads" -le 64 ] || hot_threads=64
[ "$hot_threads" -ge 1 ] || hot_threads=1

# The ring transport's Lane-1 allowlist, from the product's single source of
# truth (source-fixes/ring-comparison-server/include/darlingserver/
# rpc-supplement.h, DSERVER_RING_C2S_OPCODES).  A call number in this list MAY
# ride the ring; whether it DID is what the census measures.  Anything not in
# it is UDS-only by construction.
ring_allowlist="dserver_callnum_task_self_trap dserver_callnum_thread_self_trap dserver_callnum_host_self_trap dserver_callnum_mach_reply_port dserver_callnum_mach_port_allocate dserver_callnum_mach_port_insert_right dserver_callnum_uidgid dserver_callnum_set_thread_handles dserver_callnum_started_suspended dserver_callnum_get_tracer dserver_callnum_task_is_64_bit dserver_callnum_mldr_path dserver_callnum_vchroot_path"

# Call-number classes this census is asked about, named so that a class that
# does not appear at all can be reported as such instead of being silently
# missing from a table.
class_lifecycle="dserver_callnum_checkin dserver_callnum_checkout dserver_callnum_fork_wait_for_child dserver_callnum_set_executable_path dserver_callnum_set_dyld_info dserver_callnum_started_suspended dserver_callnum_mldr_path dserver_callnum_vchroot_path dserver_callnum_vchroot dserver_callnum_console_open dserver_callnum_kqchan_proc_open dserver_callnum_kqchan_mach_port_open dserver_callnum_interrupt_enter dserver_callnum_interrupt_exit dserver_callnum_pthread_canceled dserver_callnum_pthread_markcancel dserver_callnum_pthread_kill dserver_callnum_set_thread_handles dserver_callnum_get_tracer dserver_callnum_uidgid dserver_callnum_groups dserver_callnum_tid_for_thread dserver_callnum_task_is_64_bit dserver_callnum_stop_after_exec dserver_callnum_thread_suspended dserver_callnum_sigprocess"
class_destroy="dserver_callnum_mach_port_deallocate dserver_callnum_mach_port_mod_refs dserver_callnum_mach_port_destruct dserver_callnum_mach_port_move_member"
class_ipc="dserver_callnum_mach_msg_overwrite dserver_callnum_mach_vm_allocate dserver_callnum_mach_vm_deallocate dserver_callnum_mach_port_allocate dserver_callnum_mach_port_insert_right"
class_sync="dserver_callnum_psynch_mutexwait dserver_callnum_psynch_mutexdrop dserver_callnum_psynch_cvwait dserver_callnum_psynch_cvsignal dserver_callnum_semaphore_wait dserver_callnum_semaphore_signal dserver_callnum_mk_timer_create dserver_callnum_mk_timer_arm"
class_control="dserver_callnum_ring_attach dserver_callnum_s2c_perform dserver_callnum_push_reply dserver_callnum_invalid"

refuse() {
	printf 'DTC_REFUSE %s\n' "$*" >&2
	exit 3
}

# --------------------------------------------------------------- preflight

[ -x "$prefix_on/bin/darling" ] ||
	refuse "DTC_PREFIX_ON=$prefix_on has no executable bin/darling"
[ -d "$prefix_on/usr/lib" ] ||
	refuse "DTC_PREFIX_ON=$prefix_on does not look bootstrapped"
[ -r "$stat_tool" ] || refuse "DTC_STAT_TOOL=$stat_tool is not readable"
[ -r "$here/direct_transport_census_fixture.c" ] ||
	refuse "$here/direct_transport_census_fixture.c is missing"
case "$run_off" in
0 | 1) ;;
*) refuse "DTC_RUN_OFF must be 0 or 1 (got $run_off)" ;;
esac
if [ "$run_off" = "1" ]; then
	[ -x "$prefix_off/bin/darling" ] ||
		refuse "DTC_PREFIX_OFF=$prefix_off has no executable bin/darling"
	[ -d "$prefix_off/usr/lib" ] ||
		refuse "DTC_PREFIX_OFF=$prefix_off does not look bootstrapped"
fi

legs=()
legs+=(on)
[ "$run_off" = "1" ] && legs+=(off)

daemon_pattern() { # prefix
	printf 'darlingserver .*%s( |$)' "$(basename "$1")"
}

existing_daemon() { # prefix -> pid or empty
	pgrep -f "$(daemon_pattern "$1")" 2>/dev/null | head -1 || true
}

for leg in "${legs[@]}"; do
	eval "target=\$prefix_$leg"
	pid="$(existing_daemon "$target")"
	[ -z "$pid" ] ||
		refuse "prefix $target already has a darlingserver (pid $pid); this runner must boot it itself so the census hatch is armed"
done

work="$(mktemp -d "${TMPDIR:-/tmp}/direct-transport-census.XXXXXX")"
token="$$.$RANDOM"
guest_src="/private/var/tmp/direct_transport_census_fixture.$token.c"
guest_bin="/private/var/tmp/direct_transport_census_fixture.$token"
guest_cc="/Library/Developer/CommandLineTools/usr/bin/clang"
guest_sdk="/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
fixture_source="$here/direct_transport_census_fixture.c"

failures=0
shutdown_failed=0
declare -A CMP_RPCS CMP_RING CMP_NONRING CMP_DENOM CMP_DENOM_NAME CMP_SEEN
active_label=""
active_prefix=""

note() { printf '%s\n' "$*"; }

is_int() {
	case "${1:-}" in
	'' | *[!0-9-]*) return 1 ;;
	*) return 0 ;;
	esac
}

trim() { printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }

fail() { # message
	printf 'DTC_FAIL %s\n' "$*"
	failures=$(( failures + 1 ))
}

now_s() { date +%s; }

# ---------------------------------------------------------------- snapshot

census_snapshot() { # prefix outfile -> rc
	python3 "$stat_tool" "$1" >"$2" 2>"$2.err"
}

json_num() { # file jq_expr -> value or empty
	jq -r "$2 // empty | tostring" "$1" 2>/dev/null || true
}

host_fd_snapshot() { # pid outfile
	local pid="$1"
	local out="$2"
	local entry

	: >"$out"
	for entry in /proc/"$pid"/fd/*; do
		if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
			continue
		fi
		printf '%s %s\n' "${entry##*/}" "$(readlink "$entry" 2>/dev/null || printf '?')" \
			>>"$out"
	done
}

fd_count() { wc -l <"$1" | tr -d ' '; }

guest_pid() { # stage log
	sed -n 's/^DTC \(hot\|forkexec\) pid=\([0-9][0-9]*\).*/\2/p' "$1" | head -1
}

guest_lines() { # log pattern
	grep -E -- "$2" "$1" 2>/dev/null | sed 's/^/guest /' || true
}

# ---------------------------------------------------------------- lifecycle

boot_prefix() { # label prefix -> rc
	local label="$1"
	local target="$2"
	local rc=0
	local log="$work/boot-$label.log"
	local snap="$work/boot-$label.json"
	local deadline pid

	: >"$log"
	set +e
	timeout "$boot_timeout" env "DPREFIX=$target" "DARLING_PREFIX=$target" \
		"DARLING_ROOTLESS=1" "DARLING_NOOVERLAYFS=1" "DARLING_EUNION=1" \
		"DARLING_SERVER_RPC_HEATMAP=1" \
		"$target/bin/darling" shell /bin/bash --login -c 'true' >>"$log" 2>&1
	rc=$?
	set -e
	if [ "$rc" -ne 0 ]; then
		printf 'BOOT prefix=%s rc=%s log=%s reason=the boot command failed\n' \
			"$label" "$rc" "$log"
		return 1
	fi

	deadline=$(( $(now_s) + 60 ))
	while :; do
		pid="$(existing_daemon "$target")"
		if [ -n "$pid" ]; then
			if census_snapshot "$target" "$snap"; then
				if [ "$(json_num "$snap" '.rpc_heatmap_on')" = "1" ]; then
					printf 'BOOT prefix=%s rc=0 server_pid=%s heatmap_on=1 uptime_s=%s rpcs_serviced=%s\n' \
						"$label" "$pid" \
						"$(json_num "$snap" '.uptime_s')" \
						"$(json_num "$snap" '.rpcs_serviced')"
					return 0
				fi
			fi
		fi
		if [ "$(now_s)" -ge "$deadline" ]; then
			printf 'BOOT prefix=%s rc=1 server_pid=%s heatmap_on=%s reason=%s\n' \
				"$label" "${pid:-none}" \
				"$(json_num "$snap" '.rpc_heatmap_on')" \
				"the stat socket did not answer with the rpc census armed" \
				| tee -a "$log"
			return 1
		fi
		sleep 0.3
	done
}

shutdown_prefix() { # label prefix -> rc (0 when no prefix-owned survivor remains)
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
		"DARLING_ROOTLESS=1" "DARLING_NOOVERLAYFS=1" "DARLING_EUNION=1" \
		"$target/bin/darling" shutdown >"$work/shutdown-$label.log" 2>&1
	rc=$?
	set -e

	daemon_pids="$(pgrep -f "$(daemon_pattern "$target")" 2>/dev/null | tr '\n' ' ' || true)"
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
	if [ -n "$daemon_pids" ] || [ -n "$guests" ] ||
		[ "${mounts_self:-0}" -ne 0 ] || [ "$mounts_daemon" -ne 0 ]; then
		printf 'SHUTDOWN prefix=%s SURVIVORS prefix-owned process(es) or mount(s) remain: daemon=[%s] children=[%s] mounts_self_ns=%s mounts_daemon_ns=%s\n' \
			"$label" "$daemon_pids" "$guests" "${mounts_self:-0}" \
			"$mounts_daemon"
		return 1
	fi
	printf 'SHUTDOWN prefix=%s rc=%s no prefix-owned process or mount survived\n' \
		"$label" "$rc"
	return 0
}

remove_guest_artifacts() { # prefix
	set +e
	darling_guest_shell "$1/bin/darling" "$1" 30 \
		"rm -f '$guest_src' '$guest_bin'" >/dev/null 2>&1
	set -e
}

cleanup() {
	if [ -n "$active_label" ] && [ -n "$active_prefix" ]; then
		printf 'CLEANUP interrupted leg %s: removing guest artifacts and shutting the prefix down\n' \
			"$active_label"
		remove_guest_artifacts "$active_prefix"
		shutdown_prefix "$active_label" "$active_prefix" || shutdown_failed=1
	fi
	rm -rf -- "$work"
}
trap cleanup EXIT

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
		printf 'DTC_HARNESS_FAIL prefix=%s stage=upload rc=%s log=%s\n' \
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
		printf 'DTC_HARNESS_FAIL prefix=%s stage=compile rc=%s log=%s\n' \
			"$label" "$rc" "$work/stage-$label.log" >&2
		cat "$work/stage-$label.log" >&2
		return 1
	fi
	printf 'STAGED prefix=%s guest_src=%s guest_bin=%s compile=ok\n' "$label" \
		"$guest_src" "$guest_bin"
	return 0
}

# --------------------------------------------------------------- stages

STAGE_LOG=""
STAGE_PID=""
STAGE_RC=0
stage_start_s=0

start_stage() { # name timeout script
	local name="$1"
	local stage_seconds="$2"
	local script="$3"

	STAGE_LOG="$work/$name.log"
	: >"$STAGE_LOG"
	stage_start_s="$(now_s)"
	darling_guest_shell "$prefix/bin/darling" "$prefix" "$stage_seconds" \
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

# --------------------------------------------------------------- deltas

census_deltas() { # pre.json post.json out.tsv
	jq -r -n --slurpfile pre "$1" --slurpfile post "$2" '
		($pre[0] // {}) as $P | ($post[0] // {}) as $Q |
		def s($v): if $v == null then "absent" else ($v | tostring) end;
		def d($a; $b): if ($a == null) or ($b == null) then "absent"
			else (($b - $a) | tostring) end;
		def callnums: ((($P.per_call // {}) | keys)
			+ (($Q.per_call // {}) | keys)
			+ (($P.rpc_heatmap // {}) | keys)
			+ (($Q.rpc_heatmap // {}) | keys)) | unique;
		(["rpcs_serviced", "replies_sent", "messages_received", "checkins",
		  "forks", "inline_handled", "queued_to_pool", "ring_serviced",
		  "ring_serviced_spin", "ring_serviced_doorbell",
		  "ring_doorbells_received", "ring_wakes_issued", "ring_wakes_skipped",
		  "ring_s2c_full", "ring_duplex_s2c"] | .[] as $k |
			["scalar", $k, s($P[$k]), s($Q[$k]), d($P[$k]; $Q[$k])]),
		(callnums[] as $k |
			["percall", $k, s($P.per_call[$k].count), s($Q.per_call[$k].count),
			 d($P.per_call[$k].count; $Q.per_call[$k].count)],
			["heat_uds", $k, s($P.rpc_heatmap[$k].uds),
			 s($Q.rpc_heatmap[$k].uds),
			 d($P.rpc_heatmap[$k].uds; $Q.rpc_heatmap[$k].uds)],
			["heat_ring", $k, s($P.rpc_heatmap[$k].ring),
			 s($Q.rpc_heatmap[$k].ring),
			 d($P.rpc_heatmap[$k].ring; $Q.rpc_heatmap[$k].ring)],
			["verdict", $k, "-", s($Q.rpc_heatmap[$k].verdict), "-"])
		| @tsv
	' >"$3"
}

tsv_scalar() { # tsv key -> delta or empty
	awk -F'\t' -v k="$1" '$1 == "scalar" && $2 == k { print $5; exit }' "$2"
}

tsv_call_delta() { # tsv kind callnum -> delta or empty
	awk -F'\t' -v k="$1" -v n="$2" '$1 == k && $2 == n { print $5; exit }' "$3"
}

tsv_call_verdict() { # tsv callnum -> verdict or empty
	awk -F'\t' -v n="$1" '$1 == "verdict" && $2 == n { print $4; exit }' "$2"
}

tsv_sum() { # tsv kind -> sum of numeric deltas
	awk -F'\t' -v k="$1" '$1 == k && $5 ~ /^-?[0-9]+$/ { s += $5 } END { printf "%d", s }' "$2"
}

tsv_moved() { # tsv kind -> "name=delta " for non-zero deltas
	awk -F'\t' -v k="$1" '$1 == k && $5 ~ /^-?[0-9]+$/ && $5 + 0 != 0 { printf "%s=%s ", $2, $5 }' "$2"
}

tsv_zero() { # tsv kind -> "name " for present rows whose delta is exactly zero
	awk -F'\t' -v k="$1" '$1 == k && $5 == "0" { printf "%s ", $2 }' "$2"
}

tsv_all() { # tsv kind -> "name " for every row present in the snapshot pair
	awk -F'\t' -v k="$1" '$1 == k { printf "%s ", $2 }' "$2"
}

ratio() { # numerator denominator
	awk -v n="${1:-0}" -v d="${2:-0}" 'BEGIN {
		if (n !~ /^-?[0-9]+$/ || d !~ /^-?[0-9]+$/) { printf "absent"; exit }
		printf "%.6f", (d > 0 ? n / d : 0)
	}'
}

# Report one window's census.  $5/$6 are the hot-path denominator's name and
# value, so the decisive ratio is always printed against a stated denominator.
report_window() { # label prefix window tsv denominator_name denominator_value post_json
	local label="$1"
	local window="$3"
	local tsv="$4"
	local denom_name="$5"
	local denom_value="$6"
	local post_json="$7"
	local rpcs ring_serviced ring_heat ring_spin ring_door doorbells
	local wakes_issued wakes_skipped non_ring non_ring_transport
	local heat_sum per_call_sum nonring_count ringserved_count

	rpcs="$(tsv_scalar rpcs_serviced "$tsv")"
	per_call_sum="$(tsv_sum percall "$tsv")"
	heat_sum="$(tsv_sum heat_uds "$tsv")"
	heat_sum=$(( heat_sum + $(tsv_sum heat_ring "$tsv") ))
	ring_serviced="$(tsv_scalar ring_serviced "$tsv")"
	ring_heat="$(tsv_sum heat_ring "$tsv")"
	ring_spin="$(tsv_scalar ring_serviced_spin "$tsv")"
	ring_door="$(tsv_scalar ring_serviced_doorbell "$tsv")"
	doorbells="$(tsv_scalar ring_doorbells_received "$tsv")"
	wakes_issued="$(tsv_scalar ring_wakes_issued "$tsv")"
	wakes_skipped="$(tsv_scalar ring_wakes_skipped "$tsv")"
	nonring_count="$(awk -F'\t' '$1 == "heat_uds" && $5 ~ /^-?[0-9]+$/ && $5 + 0 != 0 { c++ } END { print c + 0 }' "$tsv")"
	ringserved_count="$(awk -F'\t' '$1 == "heat_ring" && $5 ~ /^-?[0-9]+$/ && $5 + 0 != 0 { c++ } END { print c + 0 }' "$tsv")"

	printf 'CENSUS window=%s prefix=%s rpcs_total=%s per_call_sum=%s heat_total_sum=%s ring_serviced_transport=%s ring_serviced_heatmap_sum=%s ring_spin=%s ring_doorbell=%s ring_doorbells_received=%s ring_wakes_issued=%s ring_wakes_skipped=%s\n' \
		"$window" "$label" "${rpcs:-absent}" "$per_call_sum" "$heat_sum" \
		"${ring_serviced:-absent}" "$ring_heat" "${ring_spin:-absent}" \
		"${ring_door:-absent}" "${doorbells:-absent}" \
		"${wakes_issued:-absent}" "${wakes_skipped:-absent}"

	if is_int "$rpcs"; then
		non_ring=$(( rpcs - ring_heat ))
		if is_int "$ring_serviced"; then
			non_ring_transport=$(( rpcs - ring_serviced ))
		else
			non_ring_transport="absent"
		fi
		CMP_RPCS["$label/$window"]="$rpcs"
		CMP_RING["$label/$window"]="$ring_heat"
		CMP_NONRING["$label/$window"]="$non_ring"
		CMP_DENOM["$label/$window"]="${denom_value:-absent}"
		CMP_DENOM_NAME["$label/$window"]="$denom_name"
		CMP_SEEN["$label/$window"]=1
		printf 'CENSUS RATIO window=%s prefix=%s rpcs_total=%s ring_served=%s ring_share=%s non_ring=%s non_ring_share=%s non_ring_by_transport_counter=%s hot_path_denominator=%s hot_path_calls=%s non_ring_per_hot_path=%s nonring_callnums=%s ringserved_callnums=%s\n' \
			"$window" "$label" "$rpcs" "$ring_heat" \
			"$(ratio "$ring_heat" "$rpcs")" "$non_ring" \
			"$(ratio "$non_ring" "$rpcs")" "$non_ring_transport" \
			"$denom_name" "${denom_value:-absent}" \
			"$(ratio "$non_ring" "${denom_value:-0}")" "$nonring_count" \
			"$ringserved_count"
	else
		printf 'CENSUS RATIO window=%s prefix=%s UNPROVEN missing_key=rpcs_serviced\n' \
			"$window" "$label"
		fail "window $window prefix $label has no rpcs_serviced delta in its snapshots"
	fi

	printf 'CENSUS NONRING window=%s prefix=%s [%s]\n' "$window" "$label" \
		"$(tsv_moved heat_uds "$tsv")"
	printf 'CENSUS RINGSERVED window=%s prefix=%s [%s]\n' "$window" "$label" \
		"$(tsv_moved heat_ring "$tsv")"
	printf 'CENSUS NOCHANGE window=%s prefix=%s present_but_zero_delta=[%s]\n' \
		"$window" "$label" "$(tsv_zero percall "$tsv")"
	printf 'CENSUS CALLNUMS window=%s prefix=%s present_in_snapshot=[%s]\n' \
		"$window" "$label" "$(tsv_all percall "$tsv")"

	if ! is_int "$ring_serviced"; then
		printf 'CENSUS UNPROVEN window=%s prefix=%s missing_key=ring_serviced (ring_* counters absent in this build; every call is non-ring here)\n' \
			"$window" "$label"
	fi
	if [ "$(jq -r '.rpc_heatmap_on // 0' "$post_json" 2>/dev/null)" != "1" ]; then
		printf 'CENSUS UNPROVEN window=%s prefix=%s missing_key=rpc_heatmap_on (the per-callnum transport split is unavailable; non-ring attribution above is UNPROVEN)\n' \
			"$window" "$label"
	fi
}

# One CENSUS CALL line per call number that moved OR that belongs to a named
# expected class, so an expected class that did not appear is visible as
# delta=0/absent rather than missing from the table.
report_calls() { # label window tsv
	local label="$1"
	local window="$2"
	local tsv="$3"
	local name tot ring uds verdict transport
	local absent_ring="" absent_destroy="" absent_ipc="" absent_sync="" present_control=""

	{
		awk -F'\t' '$1 == "percall" && $5 ~ /^-?[0-9]+$/ && $5 + 0 != 0 { print $2 }' "$tsv"
		printf '%s\n' $ring_allowlist $class_destroy $class_ipc $class_sync $class_control
	} | LC_ALL=C sort -u | while read -r name; do
		[ -n "$name" ] || continue
		tot="$(tsv_call_delta percall "$name" "$tsv")"
		ring="$(tsv_call_delta heat_ring "$name" "$tsv")"
		uds="$(tsv_call_delta heat_uds "$name" "$tsv")"
		verdict="$(tsv_call_verdict "$name" "$tsv")"
		[ -n "$tot" ] || tot="absent"
		[ -n "$ring" ] || ring="absent"
		[ -n "$uds" ] || uds="absent"
		[ -n "$verdict" ] || verdict="-"
		# The transport column is the WINDOW's own measured split; the verdict
		# is the server-lifetime static+measured class the snapshot carries.
		if is_int "$ring" && [ "$ring" -gt 0 ] && { ! is_int "$uds" || [ "$uds" -eq 0 ]; }; then
			transport="ring-served"
		elif is_int "$uds" && [ "$uds" -gt 0 ] && { ! is_int "$ring" || [ "$ring" -eq 0 ]; }; then
			transport="non-ring"
		elif is_int "$ring" && [ "$ring" -gt 0 ] && is_int "$uds" && [ "$uds" -gt 0 ]; then
			transport="mixed"
		else
			transport="none-in-window"
		fi
		printf 'CENSUS CALL window=%s prefix=%s name=%s total_delta=%s ring_delta=%s uds_delta=%s transport=%s lifetime_verdict=%s\n' \
			"$window" "$label" "$name" "$tot" "$ring" "$uds" \
			"$transport" "$verdict"
	done

	for name in $ring_allowlist; do
		tot="$(tsv_call_delta percall "$name" "$tsv")"
		{ ! is_int "$tot" || [ "$tot" -eq 0 ]; } && absent_ring="$absent_ring $name"
	done
	for name in $class_destroy; do
		tot="$(tsv_call_delta percall "$name" "$tsv")"
		{ ! is_int "$tot" || [ "$tot" -eq 0 ]; } && absent_destroy="$absent_destroy $name"
	done
	for name in $class_ipc; do
		tot="$(tsv_call_delta percall "$name" "$tsv")"
		{ ! is_int "$tot" || [ "$tot" -eq 0 ]; } && absent_ipc="$absent_ipc $name"
	done
	for name in $class_sync; do
		tot="$(tsv_call_delta percall "$name" "$tsv")"
		{ ! is_int "$tot" || [ "$tot" -eq 0 ]; } && absent_sync="$absent_sync $name"
	done
	for name in $class_control; do
		tot="$(tsv_call_delta percall "$name" "$tsv")"
		is_int "$tot" && [ "$tot" -ne 0 ] && present_control="$present_control $name=$tot"
	done

	printf 'CENSUS CLASS window=%s prefix=%s class=ring-allowlist absent_or_zero=[%s]\n' \
		"$window" "$label" "$(trim "$absent_ring")"
	printf 'CENSUS CLASS window=%s prefix=%s class=destroy-or-caller-s2c absent_or_zero=[%s]\n' \
		"$window" "$label" "$(trim "$absent_destroy")"
	printf 'CENSUS CLASS window=%s prefix=%s class=ipc-and-create absent_or_zero=[%s]\n' \
		"$window" "$label" "$(trim "$absent_ipc")"
	printf 'CENSUS CLASS window=%s prefix=%s class=sync-and-timers absent_or_zero=[%s]\n' \
		"$window" "$label" "$(trim "$absent_sync")"
	printf 'CENSUS CLASS window=%s prefix=%s class=control-plane present=[%s]\n' \
		"$window" "$label" "$(trim "$present_control")"
}

report_fds() { # label prefix window stage hostfds_file threads_live extra
	local label="$1"
	local window="$3"
	local stage="$4"
	local hostfds="$5"
	local threads_live="$6"
	local extra="$7"
	local guest_line host_count

	guest_line="$(grep -E -- "^DTC fds stage=$stage " "$STAGE_LOG" 2>/dev/null | tail -1)"
	host_count="absent"
	[ -s "$hostfds" ] && host_count="$(fd_count "$hostfds")"
	printf 'CENSUS FDS window=%s prefix=%s stage=%s threads_live=%s host_proc_fds=%s%s env_stage=%s\n' \
		"$window" "$label" "$stage" "$threads_live" "$host_count" \
		"$extra" "$(printf '%s' "$guest_line" | sed 's/^DTC fds stage=[^ ]* //')"
}

# --------------------------------------------------------------- windows

safe_snapshot() { # prefix outfile
	if census_snapshot "$1" "$2"; then
		return 0
	fi
	return 1
}

run_window_hot() { # label prefix
	local label="$1"
	local target="$2"
	local window="W-A"
	local pre="$work/$label-$window-pre.json"
	local mid="$work/$label-$window-mid.json"
	local post="$work/$label-$window-post.json"
	local prefd="$work/$label-$window-pre.fds"
	local midfd="$work/$label-$window-mid.fds"
	local postfd="$work/$label-$window-post.fds"
	local pre_tsv="$work/$label-$window-pre-post.tsv"
	local setup_tsv="$work/$label-$window-pre-mid.tsv"
	local loop_tsv="$work/$label-$window-mid-post.tsv"
	local pid="" reason="" pre_ok=0 mid_ok=0 post_ok=0

	prefix="$target"
	start_stage "$label-$window" "$stage_timeout" \
		"'$guest_bin' hot $hot_threads $hot_requests $hot_pre_ms $hot_hold_ms $hot_post_ms"

	if wait_marker '^DTC_BARRIER pre$' 90; then
		if window_open '^DTC_BARRIER threads-live$'; then
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
				if safe_snapshot "$target" "$pre"; then
					pre_ok=1
				else
					reason="the stat snapshot failed at the pre barrier"
				fi
				host_fd_snapshot "$pid" "$prefd"
			else
				reason="no guest pid in the stage log"
			fi
		else
			reason="the pre sample window closed before the host read it"
		fi
	else
		reason="the guest never reached its pre barrier"
	fi

	if wait_marker '^DTC_BARRIER threads-live$' "$stage_timeout"; then
		if window_open '^DTC_BARRIER action-done$'; then
			if safe_snapshot "$target" "$mid"; then
				mid_ok=1
			fi
			host_fd_snapshot "$pid" "$midfd"
		fi
	elif [ -z "$reason" ]; then
		reason="the guest never reached its threads-live barrier"
	fi

	if wait_marker '^DTC_BARRIER action-done$' "$stage_timeout"; then
		if safe_snapshot "$target" "$post"; then
			post_ok=1
		fi
		host_fd_snapshot "$pid" "$postfd"
	elif [ -z "$reason" ]; then
		reason="the workload did not reach its action barrier within ${stage_timeout}s"
	fi
	finish_stage

	printf 'CENSUS STAGE window=%s prefix=%s elapsed_s=%s stage_rc=%s guest_trap_calls=%s hot_threads_requested=%s hot_requests_per_thread=%s\n' \
		"$window" "$label" "$(stage_elapsed)" "$STAGE_RC" \
		"$(field_trap_calls "$STAGE_LOG")" "$hot_threads" "$hot_requests"
	guest_lines "$STAGE_LOG" '^DTC hot'
	guest_lines "$STAGE_LOG" '^DTC fds'

	if [ -z "$reason" ] && [ "$pre_ok" = "1" ] && [ "$post_ok" = "1" ]; then
		census_deltas "$pre" "$post" "$pre_tsv"
		report_window "$label" "$target" "$window" "$pre_tsv" \
			"dserver_callnum_host_self_trap" \
			"$(tsv_call_delta percall dserver_callnum_host_self_trap "$pre_tsv")" \
			"$post"
		report_calls "$label" "$window" "$pre_tsv"
		report_fds "$label" "$target" "$window" baseline "$prefd" 1 ""
		report_fds "$label" "$target" "$window" threads_live "$midfd" \
			"$(( hot_threads + 1 ))" \
			" host_fd_per_extra_thread=$(awk -v a="$(fd_count "$prefd")" -v b="$(fd_count "$midfd")" -v n="$hot_threads" 'BEGIN { printf "%.3f", (b - a) / (n > 0 ? n : 1) }')"
		report_fds "$label" "$target" "$window" after_join "$postfd" 1 ""
	else
		printf 'CENSUS window=%s prefix=%s UNPROVEN reason=%s\n' "$window" \
			"$label" "${reason:-the pre or post snapshot is missing}"
		fail "window $window prefix $label did not produce its samples: ${reason:-unknown}"
	fi

	if [ "$pre_ok" = "1" ] && [ "$mid_ok" = "1" ]; then
		census_deltas "$pre" "$mid" "$setup_tsv"
		report_window "$label" "$target" "${window}-setup" "$setup_tsv" \
			"guest_threads_created" "$hot_threads" "$mid"
	fi
	if [ "$mid_ok" = "1" ] && [ "$post_ok" = "1" ]; then
		census_deltas "$mid" "$post" "$loop_tsv"
		report_window "$label" "$target" "${window}-loop" "$loop_tsv" \
			"dserver_callnum_host_self_trap" \
			"$(tsv_call_delta percall dserver_callnum_host_self_trap "$loop_tsv")" \
			"$post"
	fi
}

field_trap_calls() { # stage log
	sed -n 's/.*rpc_ok=\([0-9][0-9]*\).*/\1/p' "$1" | tail -1
}

run_window_forkexec() { # label prefix
	local label="$1"
	local target="$2"
	local window="W-B"
	local pre="$work/$label-$window-pre.json"
	local post="$work/$label-$window-post.json"
	local prefd="$work/$label-$window-pre.fds"
	local postfd="$work/$label-$window-post.fds"
	local tsv="$work/$label-$window-pre-post.tsv"
	local pid="" reason="" pre_ok=0 post_ok=0 cycles

	prefix="$target"
	start_stage "$label-$window" "$stage_timeout" \
		"'$guest_bin' forkexec $fork_iterations $fork_pre_ms $fork_post_ms"

	if wait_marker '^DTC_BARRIER pre$' 90; then
		if window_open '^DTC_BARRIER action-done$'; then
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
				if safe_snapshot "$target" "$pre"; then
					pre_ok=1
				else
					reason="the stat snapshot failed at the pre barrier"
				fi
				host_fd_snapshot "$pid" "$prefd"
			else
				reason="no guest pid in the stage log"
			fi
		else
			reason="the pre sample window closed before the host read it"
		fi
	else
		reason="the guest never reached its pre barrier"
	fi

	if wait_marker '^DTC_BARRIER action-done$' "$stage_timeout"; then
		if safe_snapshot "$target" "$post"; then
			post_ok=1
		fi
		host_fd_snapshot "$pid" "$postfd"
	elif [ -z "$reason" ]; then
		reason="the workload did not reach its action barrier within ${stage_timeout}s"
	fi
	finish_stage

	cycles="$(sed -n 's/^DTC forkexec iterations=\([0-9][0-9]*\).*fork_ok=\([0-9][0-9]*\).*/\2/p' "$STAGE_LOG" | tail -1)"
	printf 'CENSUS STAGE window=%s prefix=%s elapsed_s=%s stage_rc=%s forkexec_cycles_completed=%s fork_cycles_requested=%s\n' \
		"$window" "$label" "$(stage_elapsed)" "$STAGE_RC" \
		"${cycles:-absent}" "$fork_iterations"
	guest_lines "$STAGE_LOG" '^DTC forkexec'
	guest_lines "$STAGE_LOG" '^DTC fds'

	if [ -z "$reason" ] && [ "$pre_ok" = "1" ] && [ "$post_ok" = "1" ]; then
		census_deltas "$pre" "$post" "$tsv"
		report_window "$label" "$target" "$window" "$tsv" \
			"fork_exec_cycles_completed" "${cycles:-absent}" "$post"
		report_calls "$label" "$window" "$tsv"
		report_fds "$label" "$target" "$window" baseline "$prefd" 1 ""
		report_fds "$label" "$target" "$window" after_loop "$postfd" 1 \
			" host_fd_delta_across_window=$(awk -v a="$(fd_count "$prefd")" -v b="$(fd_count "$postfd")" 'BEGIN { printf "%+d", b - a }')"
	else
		printf 'CENSUS window=%s prefix=%s UNPROVEN reason=%s\n' "$window" \
			"$label" "${reason:-the pre or post snapshot is missing}"
		fail "window $window prefix $label did not produce its samples: ${reason:-unknown}"
	fi
}

# ------------------------------------------------------------------ leg

run_leg() { # label prefix
	local label="$1"
	local target="$2"

	active_label="$label"
	active_prefix="$target"

	note "== leg $label prefix=$target =="
	if ! boot_prefix "$label" "$target"; then
		printf 'DTC_HARNESS_FAIL prefix=%s stage=boot\n' "$label" >&2
		remove_guest_artifacts "$target"
		if shutdown_prefix "$label" "$target"; then :; else shutdown_failed=1; fi
		active_label=""
		active_prefix=""
		fail "prefix $label did not boot with the census armed"
		return 1
	fi

	if ! stage_fixture "$target" "$label"; then
		printf 'DTC_HARNESS_FAIL prefix=%s stage=fixture\n' "$label" >&2
		remove_guest_artifacts "$target"
		if shutdown_prefix "$label" "$target"; then :; else shutdown_failed=1; fi
		active_label=""
		active_prefix=""
		fail "prefix $label did not stage the fixture"
		return 1
	fi

	run_window_hot "$label" "$target"
	run_window_forkexec "$label" "$target"

	remove_guest_artifacts "$target"
	if shutdown_prefix "$label" "$target"; then :; else shutdown_failed=1; fi
	active_label=""
	active_prefix=""
	return 0
}

note "DIRECT_TRANSPORT_CENSUS legs=[${legs[*]}] on=$prefix_on off=$prefix_off hot=${hot_threads}x${hot_requests} fork_iterations=$fork_iterations stat_tool=$stat_tool"

for leg in "${legs[@]}"; do
	eval "target=\$prefix_$leg"
	run_leg "$leg" "$target" || true
done

# -------------------------------------------------- ON versus OFF (same workload)

if [ "${CMP_SEEN[on/W-A]:-}" = "1" ] && [ "${CMP_SEEN[off/W-A]:-}" = "1" ]; then
	note "== the same workload on both prefixes =="
	for window in W-A W-A-setup W-A-loop W-B; do
		[ "${CMP_SEEN[on/$window]:-}" = "1" ] || continue
		[ "${CMP_SEEN[off/$window]:-}" = "1" ] || continue
		printf 'CENSUS COMPARE window=%s denominator=%s denominator_value=%s on_rpcs=%s on_ring=%s on_non_ring=%s on_non_ring_share=%s off_rpcs=%s off_ring=%s off_non_ring=%s off_non_ring_share=%s\n' \
			"$window" "${CMP_DENOM_NAME[on/$window]:-?}" \
			"${CMP_DENOM[on/$window]:-?}" \
			"${CMP_RPCS[on/$window]:-?}" "${CMP_RING[on/$window]:-?}" \
			"${CMP_NONRING[on/$window]:-?}" \
			"$(ratio "${CMP_NONRING[on/$window]:-0}" "${CMP_RPCS[on/$window]:-0}")" \
			"${CMP_RPCS[off/$window]:-?}" "${CMP_RING[off/$window]:-?}" \
			"${CMP_NONRING[off/$window]:-?}" \
			"$(ratio "${CMP_NONRING[off/$window]:-0}" "${CMP_RPCS[off/$window]:-0}")"
	done
fi

# ------------------------------------------------------------- verdict

printf 'DTC_RESULT failures=%s shutdown_failed=%s\n' "$failures" "$shutdown_failed"
if [ "$shutdown_failed" -ne 0 ]; then
	printf 'DTC_FAILED a prefix was shut down but prefix-owned processes or mounts survived\n' >&2
	exit 4
fi
if [ "$failures" -ne 0 ]; then
	printf 'DTC_FAILED %s census failure(s)\n' "$failures" >&2
	exit 1
fi
printf 'DTC_OK every window produced its counter deltas and every prefix was left clean\n'

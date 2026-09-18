#!/usr/bin/env bash
#
# run-lane-lifecycle-census.sh -- measure, on the REAL Darling product runtime,
# the lane/thread-churn behaviour that decides whether a constant-anchor
# transport is viable: descriptor retention across thread exit, behaviour at
# large sequential thread counts, behaviour above the per-process lane cap, and
# the real frequency of blocking Mach RPCs.
#
# What it measures, and from where
# --------------------------------
# Three independent fact sources, all on the same live prefix:
#
#   * darlingserver's stat socket (tools/darling-stat <prefix>), which exposes in
#     one JSON snapshot:
#       - per_call.<callnum>.count                server-serviced calls per callnum
#       - rpc_heatmap.<callnum>.{total,uds,ring,used_fiber,caller_s2c,verdict}
#                                                 per-callnum transport split,
#                                                 emitted only when the server was
#                                                 started with
#                                                 DARLING_SERVER_RPC_HEATMAP=1
#       - ring_* counters                         ring transport totals (absent by
#                                                 construction in a build without
#                                                 the ring transport)
#       - attach_* counters                       ring_attach attempts/successes/
#                                                 rejects + reject reasons,
#                                                 emitted only with
#                                                 DARLING_SERVER_ATTACH_CENSUS=1
#       - residual_reason.<bucket>                why an eligible op went UDS,
#                                                 emitted only with
#                                                 DARLING_SERVER_RESIDUAL_CENSUS=1
#       - residual_total_ring_threads_registered  cumulative per-thread ring
#                                                 registrations -- the server-side
#                                                 "lanes ever acquired" counter
#       - residual_max_ring_threads_per_process   current peak of live ring
#                                                 threads within one process --
#                                                 the server-side "lanes live" gauge
#       - msg_* counters                          mach_msg_overwrite shape census,
#                                                 emitted only with
#                                                 DARLING_SERVER_MSG_CENSUS=1
#     All of these are server-lifetime cumulative except the two gauges named as
#     such, so every window is reported as a DELTA across a barrier-delimited
#     window and every whole-leg trajectory is reported as absolute readings.
#   * the host's /proc/<guest-pid>/fd inventory.  The guest process' getpid()
#     equals the host pid of the mldr process hosting it, and the loader's
#     descriptor guard hides the loader-owned descriptors from the guest, so the
#     host view is the authority for the process' true descriptor total.
#   * the guest's own fcntl(F_GETFD) scan over [0, min(getdtablesize(), 4096)),
#     which is the authority for what the guest itself can see, plus the guest's
#     own lane dump: with DARLING_GUEST_LANE_STATS=1 set on the per-command
#     `darling shell` invocation (never at boot) the guest dylib prints, once per
#     process at clean exit,
#       [dring-lane-stats] pid=N acquired=A exhausted=E reclaimed=R held_now=H max=M
#     `acquired` counts successful per-thread lane attaches in that process,
#     `exhausted` counts claims that found the per-process lane table full (each
#     of which is a permanent per-thread UDS fallback), `reclaimed` counts slots
#     reused after a prior epoch, `held_now` is the live lane count and `max` is
#     the compile-time cap (GR_MAX_LANES).  The lane cap is a GUEST-ONLY,
#     per-process constant; the server's ring-thread registry is unbounded.
#
# The lane cap this harness observes (and does not assume)
# -------------------------------------------------------
# The per-process lane cap is GR_MAX_LANES in the guest dylib's dserver-ring.c.
# Both matched prefixes were built with GR_MAX_LANES=128 (the guest string
# "max=%u" in the dumped line reports the build's own value, and this runner
# prints it rather than hard-coding it).  Enforcement is observed three ways:
# the guest's `exhausted=` counter, the server's residual_reason bucket
# thread_no_ring_proc_has (an eligible op on UDS while the process already has
# ring threads -> this thread could not get a lane), and the per-callnum UDS
# count for the very ops being driven.
#
# Windows
# -------
#   churn   C2: LLC_CHURN_COUNT guest threads (default 1024) created and joined
#           ONE AT A TIME, each performing one ring-eligible trap
#           (mach_host_self), so each thread that can attach a lane does.  The
#           host samples the stat socket, /proc/<pid>/fd and the guest's own
#           descriptor scan every LLC_CHURN_SAMPLE_EVERY threads (default 64)
#           and at the end, while the guest is parked in a sleep.  This answers
#           whether the descriptor count is bounded or grows with the number of
#           threads EVER created.
#   sim     C3: for each N in LLC_SIM_THREADS (default 129 256 512), N
#           simultaneous threads each doing one mach_host_self trap and parking
#           on a start gate, then LLC_SIM_REQUESTS more traps each.  Reports how
#           many actually got a lane, how many fell back to the per-thread UDS
#           socket, the descriptor count, and the failure behaviour.
#   blockrecv  B2: a receiver thread parked in an UNBOUNDED blocking mach_msg
#           receive (MACH_RCV_MSG, no timeout) with a second thread delivering
#           LLC_BLK_ROUNDS simple messages to it; the receive is already pending
#           at the pre barrier.  mach_msg_overwrite is UDS-only (it is not in
#           the ring allowlist), so this is the non-ring slow path.
#   psynch  B2: threads hammering one shared pthread mutex plus a bounded
#           pthread condition-variable phase, to drive whatever pthread
#           mutex/cond operations this runtime maps onto the psynch RPCs.
#
# Every window prints its raw counter deltas, the per-callnum split, the
# descriptor counts from both sides, and the decisive non-ring/hot-path ratio
# against a STATED denominator.  A window whose intended counter did not move
# is reported UNPROVEN with the exact key -- never substituted.
#
# Lifecycle
# ---------
# Boots each prefix through the guest shell transport with all four server
# censuses armed (the server inherits DARLING_SERVER_* from the launcher), stages
# the fixture into the guest and compiles it with the in-prefix clang, runs the
# windows, removes the guest artifacts and shuts the prefix down with the
# supported `darling shutdown` path plus a survivor report.  A surviving
# prefix-owned process or a remaining mount reference exits non-zero.  A prefix
# whose stat socket does not answer with all four censuses armed is refused
# before any window runs: that server was not started by this harness.
#
# Exit status
#   0  every window produced its samples and every prefix was left clean
#   1  a window did not produce its samples, or a counter the census needs was
#      absent (reported as UNPROVEN with the exact key)
#   2  the harness could not run (boot, staging or compilation failed)
#   3  refusal: a prefix is unset, not bootstrapped, already running, or the
#      stat tool / fixture source is missing
#   4  a prefix was shut down but prefix-owned processes or mounts survived
#
# Environment
#   LLC_PREFIX_ON        bootstrapped prefix with the ring transport
#                        (default /tmp/dr-on-matched)
#   LLC_PREFIX_OFF       matched prefix without it (default /tmp/dr-off-matched)
#   LLC_RUN_OFF          1 (default) also runs the OFF leg, 0 skips it
#   LLC_STAT_TOOL        stat client (default the ring-comparison-server one)
#   LLC_WINDOWS          windows to run, space separated
#                        (default "churn sim blockrecv psynch")
#   LLC_CHURN_COUNT      churn threads created and joined one at a time (1024)
#   LLC_CHURN_SAMPLE_EVERY   churn sample period, threads (64)
#   LLC_CHURN_SAMPLE_MS  guest hold at each churn sample point (700)
#   LLC_CHURN_PRE_MS / LLC_CHURN_POST_MS      (500 / 500)
#   LLC_CHURN_TIMEOUT    churn stage timeout, seconds (1800)
#   LLC_SIM_THREADS      sim thread counts (default "129 256 512")
#   LLC_SIM_REQUESTS     traps per thread after the gate (32)
#   LLC_SIM_PRE_MS / LLC_SIM_HOLD_MS / LLC_SIM_POST_MS  (500 / 1000 / 500)
#   LLC_SIM_TIMEOUT      sim stage timeout, seconds (900)
#   LLC_BLK_ROUNDS       blocking-receive rounds (256)
#   LLC_BLK_SETTLE_MS    nap between re-post and send (20)
#   LLC_BLK_PRE_MS / LLC_BLK_POST_MS          (1500 / 500)
#   LLC_BLK_TIMEOUT      blockrecv stage timeout, seconds (600)
#   LLC_PSYNCH_THREADS   psynch threads (48)
#   LLC_PSYNCH_ITERS     mutex lock/unlock iterations per driver thread (2000)
#   LLC_PSYNCH_CROUNDS   condvar rounds (200)
#   LLC_PSYNCH_PRE_MS / LLC_PSYNCH_POST_MS    (500 / 500)
#   LLC_PSYNCH_TIMEOUT   psynch stage timeout, seconds (300)
#   LLC_WORK_DIR         if set, use this directory for snapshots instead of a
#                        fresh mktemp dir, and KEEP it for inspection afterwards
#   LLC_BOOT_TIMEOUT     boot command timeout, seconds (300)
#   LLC_STAGE_TIMEOUT    guest stage (upload + compile) timeout, seconds (300)
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
# the boot's darlingserver additionally inherits the four DARLING_SERVER_* census
# arming variables.
export DARLING_ROOTLESS=1
export DARLING_NOOVERLAYFS=1
export DARLING_EUNION=1

prefix_on="${LLC_PREFIX_ON:-/tmp/dr-on-matched}"
prefix_off="${LLC_PREFIX_OFF:-/tmp/dr-off-matched}"
run_off="${LLC_RUN_OFF:-1}"
stat_tool="${LLC_STAT_TOOL:-/home/ilyagulya/work/darling-gwn-resume/source-fixes/ring-comparison-server/tools/darling-stat}"

windows="${LLC_WINDOWS:-churn sim blockrecv psynch}"
churn_count="${LLC_CHURN_COUNT:-1024}"
churn_every="${LLC_CHURN_SAMPLE_EVERY:-64}"
churn_sample_ms="${LLC_CHURN_SAMPLE_MS:-700}"
churn_pre_ms="${LLC_CHURN_PRE_MS:-500}"
churn_post_ms="${LLC_CHURN_POST_MS:-500}"
churn_timeout="${LLC_CHURN_TIMEOUT:-1800}"
sim_threads="${LLC_SIM_THREADS:-129 256 512}"
sim_requests="${LLC_SIM_REQUESTS:-32}"
sim_pre_ms="${LLC_SIM_PRE_MS:-500}"
sim_hold_ms="${LLC_SIM_HOLD_MS:-1000}"
sim_post_ms="${LLC_SIM_POST_MS:-500}"
sim_timeout="${LLC_SIM_TIMEOUT:-900}"
blk_rounds="${LLC_BLK_ROUNDS:-256}"
blk_settle_ms="${LLC_BLK_SETTLE_MS:-20}"
blk_pre_ms="${LLC_BLK_PRE_MS:-1500}"
blk_post_ms="${LLC_BLK_POST_MS:-500}"
blk_timeout="${LLC_BLK_TIMEOUT:-600}"
psynch_threads="${LLC_PSYNCH_THREADS:-48}"
psynch_iters="${LLC_PSYNCH_ITERS:-2000}"
psynch_crounds="${LLC_PSYNCH_CROUNDS:-200}"
psynch_pre_ms="${LLC_PSYNCH_PRE_MS:-500}"
psynch_post_ms="${LLC_PSYNCH_POST_MS:-500}"
psynch_timeout="${LLC_PSYNCH_TIMEOUT:-300}"
boot_timeout="${LLC_BOOT_TIMEOUT:-300}"
stage_timeout="${LLC_STAGE_TIMEOUT:-300}"

if [ "$churn_every" -lt 1 ]; then churn_every=1; fi

# The ring transport's Lane-1 allowlist, from the product's single source of
# truth (rpc-supplement.h, DSERVER_RING_C2S_OPCODES).  A call number in this
# list MAY ride the ring; whether it DID is what the census measures.
ring_allowlist="dserver_callnum_task_self_trap dserver_callnum_thread_self_trap dserver_callnum_host_self_trap dserver_callnum_mach_reply_port dserver_callnum_mach_port_allocate dserver_callnum_mach_port_insert_right dserver_callnum_uidgid dserver_callnum_set_thread_handles dserver_callnum_started_suspended dserver_callnum_get_tracer dserver_callnum_task_is_64_bit dserver_callnum_mldr_path dserver_callnum_vchroot_path"

# Call-number classes this census is asked about, named so that a class that does
# not appear at all can be reported as such instead of being silently missing.
class_lifecycle="dserver_callnum_checkin dserver_callnum_checkout dserver_callnum_fork_wait_for_child dserver_callnum_set_executable_path dserver_callnum_set_dyld_info dserver_callnum_started_suspended dserver_callnum_mldr_path dserver_callnum_vchroot_path dserver_callnum_vchroot dserver_callnum_console_open dserver_callnum_kqchan_proc_open dserver_callnum_kqchan_mach_port_open dserver_callnum_interrupt_enter dserver_callnum_interrupt_exit dserver_callnum_pthread_canceled dserver_callnum_pthread_markcancel dserver_callnum_pthread_kill dserver_callnum_set_thread_handles dserver_callnum_get_tracer dserver_callnum_uidgid dserver_callnum_groups dserver_callnum_tid_for_thread dserver_callnum_task_is_64_bit dserver_callnum_stop_after_exec dserver_callnum_thread_suspended dserver_callnum_sigprocess"
class_destroy="dserver_callnum_mach_port_deallocate dserver_callnum_mach_port_mod_refs dserver_callnum_mach_port_destruct dserver_callnum_mach_port_move_member"
class_ipc="dserver_callnum_mach_msg_overwrite dserver_callnum_mach_vm_allocate dserver_callnum_mach_vm_deallocate dserver_callnum_mach_port_allocate dserver_callnum_mach_port_insert_right"
class_sync="dserver_callnum_psynch_mutexwait dserver_callnum_psynch_mutexdrop dserver_callnum_psynch_cvwait dserver_callnum_psynch_cvsignal dserver_callnum_semaphore_wait dserver_callnum_semaphore_signal dserver_callnum_mk_timer_create dserver_callnum_mk_timer_arm"
class_control="dserver_callnum_ring_attach dserver_callnum_s2c_perform dserver_callnum_push_reply dserver_callnum_invalid"

# The Mach-RPC side of the blocking census lives in the msg_* counters, whose
# arming switch is DARLING_SERVER_MSG_CENSUS.  The counter that answers the
# blocking question is msg_blocking_receive: a receive that can park unbounded
# (MACH_RCV_MSG with no finite positive timeout).
msg_scalars="msg_total msg_send_msg msg_rcv_msg msg_send_only msg_receive_only msg_send_receive msg_rcv_size_nonzero msg_blocking_receive msg_send_only_simple msg_send_only_complex msg_send_only_ool msg_send_only_port_descriptors msg_census_hdr_read_fail"

# Cumulative counters whose DELTA across a window is the measurement.
counter_scalars="rpcs_serviced replies_sent messages_received checkins forks inline_handled queued_to_pool ring_serviced ring_serviced_spin ring_serviced_doorbell ring_doorbells_received ring_wakes_issued ring_wakes_skipped ring_s2c_full ring_duplex_s2c attach_census_processes attach_census_first_uds_calls attach_census_total_pre_attach_eligible attach_attempts attach_successes attach_rejects attach_mldr_callers attach_dylib_callers attach_no_ring_code residual_total_ring_threads_registered $msg_scalars"

# Values that are gauges or per-snapshot facts: a delta is meaningless, so the
# post-window value is reported instead.
gauge_scalars="residual_max_ring_threads_per_process clients_blocked_in_rpc workers_busy workers_available"

refuse() {
	printf 'LLC_REFUSE %s\n' "$*" >&2
	exit 3
}

# --------------------------------------------------------------- preflight

[ -x "$prefix_on/bin/darling" ] ||
	refuse "LLC_PREFIX_ON=$prefix_on has no executable bin/darling"
[ -d "$prefix_on/usr/lib" ] ||
	refuse "LLC_PREFIX_ON=$prefix_on does not look bootstrapped"
[ -r "$stat_tool" ] || refuse "LLC_STAT_TOOL=$stat_tool is not readable"
[ -r "$here/lane_lifecycle_census_fixture.c" ] ||
	refuse "$here/lane_lifecycle_census_fixture.c is missing"
case "$run_off" in
0 | 1) ;;
*) refuse "LLC_RUN_OFF must be 0 or 1 (got $run_off)" ;;
esac
if [ "$run_off" = "1" ]; then
	[ -x "$prefix_off/bin/darling" ] ||
		refuse "LLC_PREFIX_OFF=$prefix_off has no executable bin/darling"
	[ -d "$prefix_off/usr/lib" ] ||
		refuse "LLC_PREFIX_OFF=$prefix_off does not look bootstrapped"
fi
for window in $windows; do
	case "$window" in
	churn | sim | blockrecv | psynch) ;;
	*) refuse "LLC_WINDOWS contains an unknown window '$window'" ;;
	esac
done

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
		refuse "prefix $target already has a darlingserver (pid $pid); this runner must boot it itself so the censuses are armed"
done

work_keep="${LLC_WORK_DIR:-}"
if [ -n "$work_keep" ]; then
	mkdir -p "$work_keep"
	work="$work_keep"
else
	work="$(mktemp -d "${TMPDIR:-/tmp}/lane-lifecycle-census.XXXXXX")"
fi
token="$$.$RANDOM"
guest_src="/private/var/tmp/lane_lifecycle_census_fixture.$token.c"
guest_bin="/private/var/tmp/lane_lifecycle_census_fixture.$token"
guest_cc="/Library/Developer/CommandLineTools/usr/bin/clang"
guest_sdk="/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
fixture_source="$here/lane_lifecycle_census_fixture.c"

failures=0
shutdown_failed=0
active_label=""
active_prefix=""
# Set from the boot snapshot: whether THIS leg's build carries the ring
# transport.  A build without it has no ring_* keys, no attach counters, no
# live-lane gauge and no guest lane table, so every lane-related number on such
# a leg is ABSENT BY DESIGN and is reported as UNPROVEN rather than as zero.
leg_ring_present="unknown"

note() { printf '%s\n' "$*"; }

is_int() {
	case "${1:-}" in
	'' | *[!0-9-]*) return 1 ;;
	*) return 0 ;;
	esac
}

trim() { printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }

fail() { # message
	printf 'LLC_FAIL %s\n' "$*"
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

# What the host's descriptor inventory is actually made of.  The lane's wake
# descriptor is an eventfd the loader adopts into its high-fd namespace; the
# guest's per-thread RPC transport is an AF_UNIX socket.  Separating them is
# what turns a descriptor COUNT into an attributable descriptor SET.
fd_class_counts() { # fdsfile -> "eventfd=N socket=N other=N"
	awk '{
		if ($2 == "anon_inode:[eventfd]") e++;
		else if ($2 ~ /^socket:/) s++;
		else o++;
	} END { printf "eventfd=%d socket=%d other=%d", e + 0, s + 0, o + 0 }' "$1"
}

fd_eventfds() { # fdsfile -> count of adopted ring wake eventfds
	awk '$2 == "anon_inode:[eventfd]" { e++ } END { printf "%d", e + 0 }' "$1"
}

guest_pid() { # stage log -> the fixture's own pid
	sed -n 's/^DTC [a-z]* pid=\([0-9][0-9]*\).*/\1/p' "$1" | head -1
}

guest_lines() { # log pattern
	grep -E -- "$2" "$1" 2>/dev/null | sed 's/^/guest /' || true
}

# The guest lane dump, for the fixture process only.  The dump carries its own
# pid, and bash (a different process, a different lane table) dumps its own line
# too, so the fixture's line is selected by pid when one is known.
guest_lane_stats() { # stage log pid
	local log="$1"
	local want="${2:-}"

	if [ -n "$want" ]; then
		sed -n "s/.*\[dring-lane-stats\] pid=$want \(.*\)$/\1/p" "$log" | tail -1
	else
		sed -n 's/.*\[dring-lane-stats\] \(.*\)$/\1/p' "$log" | tail -1
	fi
}

# Parse "acquired=A exhausted=E reclaimed=R held_now=H max=M" into one field.
lane_stat_field() { # stats field -> value
	printf '%s\n' "$1" | sed -n "s/.*$2=\([0-9][0-9]*\).*/\1/p"
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
		"DARLING_SERVER_RPC_HEATMAP=1" "DARLING_SERVER_ATTACH_CENSUS=1" \
		"DARLING_SERVER_RESIDUAL_CENSUS=1" "DARLING_SERVER_MSG_CENSUS=1" \
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
				local armed=0 key
				for key in rpc_heatmap_on attach_census_on residual_census_on msg_census_on; do
					[ "$(json_num "$snap" ".$key")" = "1" ] || armed=1
				done
				if [ "$armed" = "0" ]; then
					printf 'BOOT prefix=%s rc=0 server_pid=%s heatmap_on=%s attach_census_on=%s residual_census_on=%s msg_census_on=%s uptime_s=%s rpcs_serviced=%s\n' \
						"$label" "$pid" \
						"$(json_num "$snap" '.rpc_heatmap_on')" \
						"$(json_num "$snap" '.attach_census_on')" \
						"$(json_num "$snap" '.residual_census_on')" \
						"$(json_num "$snap" '.msg_census_on')" \
						"$(json_num "$snap" '.uptime_s')" \
						"$(json_num "$snap" '.rpcs_serviced')"
					if jq -e 'has("ring_serviced")' "$snap" >/dev/null 2>&1; then
						leg_ring_present="present"
					else
						leg_ring_present="absent"
					fi
					printf 'BOOT prefix=%s ring_transport=%s ring_serviced_key=%s\n' \
						"$label" "$leg_ring_present" \
						"$(json_num "$snap" '.ring_serviced')"
					return 0
				fi
			fi
		fi
		if [ "$(now_s)" -ge "$deadline" ]; then
			printf 'BOOT prefix=%s rc=1 server_pid=%s heatmap_on=%s attach_census_on=%s residual_census_on=%s msg_census_on=%s reason=%s\n' \
				"$label" "${pid:-none}" \
				"$(json_num "$snap" '.rpc_heatmap_on')" \
				"$(json_num "$snap" '.attach_census_on')" \
				"$(json_num "$snap" '.residual_census_on')" \
				"$(json_num "$snap" '.msg_census_on')" \
				"the stat socket did not answer with all four censuses armed" \
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
	if [ -z "$work_keep" ]; then
		rm -rf -- "$work"
	else
		printf 'CLEANUP keeping work dir %s (LLC_WORK_DIR set)\n' "$work"
	fi
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
		printf 'LLC_HARNESS_FAIL prefix=%s stage=upload rc=%s log=%s\n' \
			"$label" "$rc" "$work/stage-$label.log" >&2
		cat "$work/stage-$label.log" >&2
		return 1
	fi

	set +e
	darling_guest_shell "$target/bin/darling" "$target" "$stage_timeout" \
		"$guest_cc -isysroot $guest_sdk -O1 -Wno-deprecated-declarations -o $guest_bin $guest_src; printf 'COMPILE_RC=%s\n' \$?" \
		>>"$work/stage-$label.log" 2>&1
	rc=$?
	set -e
	if [ "$rc" -ne 0 ] || ! grep -q '^COMPILE_RC=0$' "$work/stage-$label.log"; then
		printf 'LLC_HARNESS_FAIL prefix=%s stage=compile rc=%s log=%s\n' \
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

# The per-command guest stage.  DARLING_GUEST_LANE_STATS=1 is set HERE, on the
# leaf command only -- never at boot -- so the long-lived daemons started by
# launchd never inherit it and only the explicitly-targeted fixture process
# dumps its lane table.
start_stage() { # name timeout script
	local name="$1"
	local stage_seconds="$2"
	local script="$3"

	STAGE_LOG="$work/$name.log"
	: >"$STAGE_LOG"
	stage_start_s="$(now_s)"
	timeout --kill-after=5 "$stage_seconds" \
		env "DPREFIX=$prefix" "DARLING_PREFIX=$prefix" \
		"DARLING_GUEST_LANE_STATS=1" \
		"$prefix/bin/darling" shell /bin/bash --login -c "$script" \
		>"$STAGE_LOG" 2>&1 &
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

# One TSV row per fact:
#   scalar <name> <pre> <post> <delta>
#   gauge  <name> <pre> <post> -
#   reason <bucket> <pre> <post> <delta>
#   reject <reason-id> <pre> <post> <delta>
#   udsdespitelane <callnum> <pre> <post> <delta>
#   percall <callnum> <pre> <post> <delta>
#   heat_uds <callnum> <pre> <post> <delta>
#   heat_ring <callnum> <pre> <post> <delta>
#   verdict <callnum> - <verdict> -
census_deltas() { # pre.json post.json out.tsv
	jq -r -n --slurpfile pre "$1" --slurpfile post "$2" --arg counters "$counter_scalars" --arg gauges "$gauge_scalars" '
		($pre[0] // {}) as $P | ($post[0] // {}) as $Q |
		def s($v): if $v == null then "absent" else ($v | tostring) end;
		def d($a; $b): if ($a == null) or ($b == null) then "absent"
			else (($b - $a) | tostring) end;
		def callnums: ((($P.per_call // {}) | keys)
			+ (($Q.per_call // {}) | keys)
			+ (($P.rpc_heatmap // {}) | keys)
			+ (($Q.rpc_heatmap // {}) | keys)) | unique;
		def reasons: ((($P.residual_reason // {}) | keys)
			+ (($Q.residual_reason // {}) | keys)) | unique;
		def rejects: ((($P.attach_reject_by_reason // {}) | keys)
			+ (($Q.attach_reject_by_reason // {}) | keys)) | unique;
		def despite: ((($P.residual_uds_despite_lane // {}) | keys)
			+ (($Q.residual_uds_despite_lane // {}) | keys)) | unique;
		(($counters | split(" "))[] | select(length > 0) as $k |
			["scalar", $k, s($P[$k]), s($Q[$k]), d($P[$k]; $Q[$k])]),
		(($gauges | split(" "))[] | select(length > 0) as $k |
			["gauge", $k, s($P[$k]), s($Q[$k]), "-"]),
		(reasons[] as $k |
			["reason", $k, s($P.residual_reason[$k]), s($Q.residual_reason[$k]),
			 d($P.residual_reason[$k]; $Q.residual_reason[$k])]),
		(rejects[] as $k |
			["reject", $k, s($P.attach_reject_by_reason[$k]),
			 s($Q.attach_reject_by_reason[$k]),
			 d($P.attach_reject_by_reason[$k]; $Q.attach_reject_by_reason[$k])]),
		(despite[] as $k |
			["udsdespitelane", $k, s($P.residual_uds_despite_lane[$k]),
			 s($Q.residual_uds_despite_lane[$k]),
			 d($P.residual_uds_despite_lane[$k]; $Q.residual_uds_despite_lane[$k])]),
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

tsv_gauge() { # tsv key -> post value or empty
	awk -F'\t' -v k="$1" '$1 == "gauge" && $2 == k { print $4; exit }' "$2"
}

tsv_reason() { # tsv bucket -> delta or empty
	awk -F'\t' -v k="$1" '$1 == "reason" && $2 == k { print $5; exit }' "$2"
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

tsv_sum_names() { # tsv kind "name name ..." -> sum of numeric deltas for those names
	awk -F'\t' -v k="$1" -v names="$2" '
		BEGIN { n = split(names, a, " ") }
		$1 == k && $5 ~ /^-?[0-9]+$/ {
			for (i = 1; i <= n; ++i) if ($2 == a[i]) { s += $5; break }
		}
		END { printf "%d", s }
	' "$3"
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

# Report one window's census.  $6/$7 are the hot-path denominator's name and
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
		# The per-hot-path ratio is only printed when the denominator is a real
		# number; a window whose denominator counter did not exist must not read as
		# "zero non-ring calls per hot-path call".
		local nrph
		if is_int "${denom_value:-}" && is_int "$non_ring"; then
			nrph="$(ratio "$non_ring" "$denom_value")"
		else
			nrph="absent"
		fi
		printf 'CENSUS RATIO window=%s prefix=%s rpcs_total=%s ring_served=%s ring_share=%s non_ring=%s non_ring_share=%s non_ring_by_transport_counter=%s hot_path_denominator=%s hot_path_calls=%s non_ring_per_hot_path=%s nonring_callnums=%s ringserved_callnums=%s\n' \
			"$window" "$label" "$rpcs" "$ring_heat" \
			"$(ratio "$ring_heat" "$rpcs")" "$non_ring" \
			"$(ratio "$non_ring" "$rpcs")" "$non_ring_transport" \
			"$denom_name" "${denom_value:-absent}" "$nrph" \
			"$nonring_count" "$ringserved_count"
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

	# --- the lane / fallback side of the same window -----------------------
	printf 'CENSUS LANES window=%s prefix=%s attach_attempts=%s attach_successes=%s attach_rejects=%s attach_mldr_callers=%s attach_dylib_callers=%s attach_no_ring_code=%s lanes_registered_cumulative=%s max_ring_threads_per_process=%s reject_by_reason=[%s]\n' \
		"$window" "$label" \
		"$(tsv_scalar attach_attempts "$tsv")" \
		"$(tsv_scalar attach_successes "$tsv")" \
		"$(tsv_scalar attach_rejects "$tsv")" \
		"$(tsv_scalar attach_mldr_callers "$tsv")" \
		"$(tsv_scalar attach_dylib_callers "$tsv")" \
		"$(tsv_scalar attach_no_ring_code "$tsv")" \
		"$(tsv_scalar residual_total_ring_threads_registered "$tsv")" \
		"$(tsv_gauge residual_max_ring_threads_per_process "$tsv")" \
		"$(tsv_moved reject "$tsv")"
	printf 'CENSUS FALLBACK window=%s prefix=%s reason_no_ring_proc_none=%s reason_no_ring_proc_has=%s reason_has_ring=%s reason_control_plane=%s reason_ineligible=%s uds_despite_lane=[%s]\n' \
		"$window" "$label" \
		"$(tsv_reason thread_no_ring_proc_none "$tsv")" \
		"$(tsv_reason thread_no_ring_proc_has "$tsv")" \
		"$(tsv_reason thread_has_ring "$tsv")" \
		"$(tsv_reason control_plane "$tsv")" \
		"$(tsv_reason ineligible "$tsv")" \
		"$(tsv_moved udsdespitelane "$tsv")"
	printf 'CENSUS RETIRED window=%s prefix=%s retired_slot_counter=%s\n' \
		"$window" "$label" \
		"$(if jq -e 'has("ring_retired")' "$post_json" >/dev/null 2>&1; then printf present; else printf 'absent (no retired-lane key in the snapshot; the only slot-reuse counter is the guest-side reclaimed= in the lane dump)'; fi)"

	# --- the blocking-Mach-RPC side ---------------------------------------
	printf 'CENSUS MSG window=%s prefix=%s msg_total=%s msg_send_only=%s msg_receive_only=%s msg_send_receive=%s msg_rcv_size_nonzero=%s msg_blocking_receive=%s blocking_share_of_total=%s msg_send_only_simple=%s msg_send_only_complex=%s msg_send_only_ool=%s msg_send_only_port_desc=%s msg_census_hdr_read_fail=%s\n' \
		"$window" "$label" \
		"$(tsv_scalar msg_total "$tsv")" \
		"$(tsv_scalar msg_send_only "$tsv")" \
		"$(tsv_scalar msg_receive_only "$tsv")" \
		"$(tsv_scalar msg_send_receive "$tsv")" \
		"$(tsv_scalar msg_rcv_size_nonzero "$tsv")" \
		"$(tsv_scalar msg_blocking_receive "$tsv")" \
		"$(ratio "$(tsv_scalar msg_blocking_receive "$tsv")" "$(tsv_scalar msg_total "$tsv")")" \
		"$(tsv_scalar msg_send_only_simple "$tsv")" \
		"$(tsv_scalar msg_send_only_complex "$tsv")" \
		"$(tsv_scalar msg_send_only_ool "$tsv")" \
		"$(tsv_scalar msg_send_only_port_descriptors "$tsv")" \
		"$(tsv_scalar msg_census_hdr_read_fail "$tsv")"

	if ! is_int "$ring_serviced"; then
		printf 'CENSUS UNPROVEN window=%s prefix=%s missing_key=ring_serviced (this build has no ring transport; every ring/lane attribution above is absent by design, NOT zero)\n' \
			"$window" "$label"
	fi
	if [ "$(jq -r '.rpc_heatmap_on // 0' "$post_json" 2>/dev/null)" != "1" ]; then
		printf 'CENSUS UNPROVEN window=%s prefix=%s missing_key=rpc_heatmap_on (the per-callnum transport split is unavailable; non-ring attribution above is UNPROVEN)\n' \
			"$window" "$label"
	fi
	if [ "$(jq -r '.attach_census_on // 0' "$post_json" 2>/dev/null)" != "1" ]; then
		printf 'CENSUS UNPROVEN window=%s prefix=%s missing_key=attach_census_on (the attach counters above are UNPROVEN)\n' \
			"$window" "$label"
	fi
	if [ "$(jq -r '.residual_census_on // 0' "$post_json" 2>/dev/null)" != "1" ]; then
		printf 'CENSUS UNPROVEN window=%s prefix=%s missing_key=residual_census_on (the fallback-reason counters above are UNPROVEN)\n' \
			"$window" "$label"
	fi
	if [ "$(jq -r '.msg_census_on // 0' "$post_json" 2>/dev/null)" != "1" ]; then
		printf 'CENSUS UNPROVEN window=%s prefix=%s missing_key=msg_census_on (the blocking-receive counters above are UNPROVEN)\n' \
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
	local absent_ring="" absent_destroy="" absent_ipc="" absent_sync="" absent_lifecycle=""
	local present_control=""

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
	for name in $class_lifecycle; do
		tot="$(tsv_call_delta percall "$name" "$tsv")"
		{ ! is_int "$tot" || [ "$tot" -eq 0 ]; } && absent_lifecycle="$absent_lifecycle $name"
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
	printf 'CENSUS CLASS window=%s prefix=%s class=lifecycle-and-thread absent_or_zero=[%s]\n' \
		"$window" "$label" "$(trim "$absent_lifecycle")"
	printf 'CENSUS CLASS window=%s prefix=%s class=control-plane present=[%s]\n' \
		"$window" "$label" "$(trim "$present_control")"
}

report_fds() { # label window stage hostfds_file threads_live extra
	local label="$1"
	local window="$2"
	local stage="$3"
	local hostfds="$4"
	local threads_live="$5"
	local extra="$6"
	local guest_line host_count

	guest_line="$(grep -E -- "^DTC fds stage=$stage " "$STAGE_LOG" 2>/dev/null | tail -1)"
	host_count="absent"
	[ -s "$hostfds" ] && host_count="$(fd_count "$hostfds")"
	printf 'CENSUS FDS window=%s prefix=%s stage=%s threads_live=%s host_proc_fds=%s host_fd_classes=[%s]%s guest_scan=%s\n' \
		"$window" "$label" "$stage" "$threads_live" "$host_count" \
		"$(if [ -s "$hostfds" ]; then fd_class_counts "$hostfds"; else printf absent; fi)" \
		"$extra" "$(printf '%s' "$guest_line" | sed 's/^DTC fds stage=[^ ]* //')"
}

# --------------------------------------------------------------- windows

safe_snapshot() { # prefix outfile
	if census_snapshot "$1" "$2"; then
		return 0
	fi
	return 1
}

# --- C2: sequential thread churn ------------------------------------------

# The churn window needs the host to sample WHILE the guest runs, so it cannot
# use the generic single-shot stage helpers above; it is its own driver.
run_window_churn() { # label prefix
	local label="$1"
	local target="$2"
	local window="C2"
	local pid="" reason="" pre_ok=0
	local pre="$work/$label-churn-0.json"
	local prefd="$work/$label-churn-0.fds"
	local post="$work/$label-churn-post.json"
	local postfd="$work/$label-churn-post.fds"
	local settle="$work/$label-churn-settle.json"
	local settlefd="$work/$label-churn-settle.fds"
	local tsv="$work/$label-churn-pre-post.tsv"
	local summary total every
	local point
	local prev_fds=""
	local rows="$work/$label-churn-rows.tsv"

	prefix="$target"
	start_stage "$label-churn" "$churn_timeout" \
		"'$guest_bin' churn $churn_count $churn_every $churn_sample_ms $churn_pre_ms $churn_post_ms"

	: >"$rows"
	if wait_marker '^DTC churn pid=' 120; then
		summary="$(sed -n 's/^DTC churn pid=\([0-9][0-9]*\) count=\([0-9][0-9]*\) sample_every=\([0-9][0-9]*\).*/\1 \2 \3/p' "$STAGE_LOG" | head -1)"
		pid="${summary%% *}"
		local rest="${summary#* }"
		total="${rest%% *}"
		every="${rest##* }"
	fi
	if [ -z "${total:-}" ]; then
		total="$churn_count"
		every="$churn_every"
		printf 'CENSUS CHURN window=%s prefix=%s stage_requested count=%s sample_every=%s note=the fixture did not report its own parameters\n' \
			"$window" "$label" "$total" "$every"
	fi

	if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
		reason="no guest pid in the stage log"
	else
		# point 0: baseline, before any churn thread exists
		if wait_marker '^DTC_BARRIER pre$' 120 && window_open "^DTC_BARRIER churn-point $every\$"; then
			if safe_snapshot "$target" "$pre"; then
				pre_ok=1
				host_fd_snapshot "$pid" "$prefd"
				churn_row "$label" 0 baseline "$pre" "$prefd" "$rows"
				prev_fds="$(fd_count "$prefd")"
			else
				reason="the stat snapshot failed at the churn pre barrier"
			fi
		else
			reason="the churn pre window closed before the host read it"
		fi
	fi

	if [ -z "$reason" ]; then
		point=$every
		while [ "$point" -le "$total" ]; do
			if wait_marker "^DTC_BARRIER churn-point $point\$" "$churn_timeout"; then
				local next=$(( point + every ))
				if window_open "^DTC_BARRIER churn-point $next\$"; then
					local snap="$work/$label-churn-$point.json"
					local snapfd="$work/$label-churn-$point.fds"
					if safe_snapshot "$target" "$snap"; then
						host_fd_snapshot "$pid" "$snapfd"
						churn_row "$label" "$point" "churn_$point" "$snap" "$snapfd" "$rows"
						prev_fds="$(fd_count "$snapfd")"
					else
						printf 'CENSUS CHURN point=%s prefix=%s UNPROVEN missing_key=snapshot (the stat socket did not answer inside the sample window)\n' \
							"$point" "$label"
						fail "churn point $point on prefix $label missed its stat snapshot"
					fi
				else
					printf 'CENSUS CHURN point=%s prefix=%s UNPROVEN missing_key=sample_window (the guest left the sample window before the host sampled it)\n' \
						"$point" "$label"
					fail "churn point $point on prefix $label missed its sample window"
				fi
			else
				printf 'CENSUS CHURN point=%s prefix=%s UNPROVEN missing_key=barrier (the guest never reached this sample point)\n' \
					"$point" "$label"
				fail "churn point $point on prefix $label never arrived"
				break
			fi
			if [ "$point" -ge "$total" ]; then
				break
			fi
			point=$(( point + every ))
		done
	fi

	if wait_marker '^DTC_BARRIER action-done$' "$churn_timeout"; then
		if safe_snapshot "$target" "$post"; then
			host_fd_snapshot "$pid" "$postfd"
			churn_row "$label" "$(( total + 1 ))" after_loop "$post" "$postfd" "$rows"
		fi
	fi
	# A third point a full hold later: it separates "the count is bounded" from
	# "the count is draining after the loop", which is a different claim.
	if wait_marker '^DTC_BARRIER post-settle$' "$churn_timeout"; then
		if safe_snapshot "$target" "$settle"; then
			host_fd_snapshot "$pid" "$settlefd"
			churn_row "$label" "$(( total + 2 ))" post_settle "$settle" "$settlefd" "$rows"
		fi
	fi
	wait_marker '^DTC_BARRIER done$' 60
	finish_stage

	printf 'CENSUS CHURN window=%s prefix=%s elapsed_s=%s stage_rc=%s\n' \
		"$window" "$label" "$(stage_elapsed)" "$STAGE_RC"
	guest_lines "$STAGE_LOG" '^DTC churn'
	guest_lines "$STAGE_LOG" '^DTC fds'
	guest_lines "$STAGE_LOG" '\[dring-lane-stats\]'

	local stats; stats="$(guest_lane_stats "$STAGE_LOG" "$pid")"
	if [ -n "$stats" ]; then
		printf 'CENSUS CHURN LANE_DUMP window=%s prefix=%s pid=%s %s\n' \
			"$window" "$label" "$pid" "$stats"
	elif [ "$leg_ring_present" = "absent" ]; then
		printf 'CENSUS CHURN LANE_DUMP window=%s prefix=%s pid=%s ABSENT-BY-DESIGN (this build has no ring transport, so the guest dylib has no lane table to dump; the lane counters on this leg are UNPROVEN, never zero)\n' \
			"$window" "$label" "${pid:-?}"
	else
		printf 'CENSUS CHURN LANE_DUMP window=%s prefix=%s pid=%s UNPROVEN missing_key=dring-lane-stats (DARLING_GUEST_LANE_STATS=1 was set on the leaf command; the guest dylib either has no lane table or did not reach the clean-exit dump)\n' \
			"$window" "$label" "${pid:-?}"
		fail "the churn leg on prefix $label produced no guest lane dump"
	fi

	# The trajectory table, printed straight from the absolute per-point rows,
	# plus the descriptor slope between consecutive points.
	churn_table "$label" "$rows"

	if [ -n "$reason" ]; then
		printf 'CENSUS CHURN window=%s prefix=%s UNPROVEN reason=%s\n' \
			"$window" "$label" "$reason"
		fail "the churn window on prefix $label did not produce its samples: $reason"
		return 0
	fi

	if [ "$pre_ok" = "1" ] && [ -s "$post" ]; then
		census_deltas "$pre" "$post" "$tsv"
		report_window "$label" "$target" "$window" "$tsv" \
			"guest_churn_threads_created" "$total" "$post"
		report_calls "$label" "$window" "$tsv"
		report_fds "$label" "$window" baseline "$prefd" 0 ""
		report_fds "$label" "$window" after_loop "$postfd" 0 \
			" host_fd_delta_across_window=$(awk -v a="$(fd_count "$prefd")" -v b="$(fd_count "$postfd")" 'BEGIN { printf "%+d", b - a }')"
		churn_verdict "$label" "$rows" "$prefd" "$postfd" "$settlefd" "$total"
	else
		printf 'CENSUS CHURN window=%s prefix=%s UNPROVEN reason=the pre or post snapshot is missing\n' \
			"$window" "$label"
		fail "the churn window on prefix $label did not produce its snapshots"
	fi
}

churn_row() { # label point stage json fdsfile rows
	local label="$1"
	local point="$2"
	local stage="$3"
	local json="$4"
	local fdsfile="$5"
	local rows="$6"
	local guest_visible

	guest_visible="$(sed -n "s/^DTC fds stage=$stage guest_visible=\([0-9][0-9]*\).*/\1/p" "$STAGE_LOG" | tail -1)"
	[ -n "$guest_visible" ] || guest_visible="absent"
	{
		jq -r -n --slurpfile s "$json" --arg p "$label" --arg n "$point" \
			--arg hv "$(fd_count "$fdsfile")" \
			--arg ef "$(fd_eventfds "$fdsfile")" --arg gv "$guest_visible" '
		($s[0] // {}) as $S |
		def v($x): if $x == null then "absent" else ($x | tostring) end;
		def r($k): v($S.residual_reason[$k]);
		[ $n, $hv, $ef, $gv,
		  v($S.rpcs_serviced),
		  v($S.attach_attempts), v($S.attach_successes), v($S.attach_rejects),
		  v($S.residual_total_ring_threads_registered),
		  v($S.residual_max_ring_threads_per_process),
		  r("thread_no_ring_proc_none"), r("thread_no_ring_proc_has"),
		  r("thread_has_ring"), r("control_plane"), r("ineligible"),
		  v($S.per_call["dserver_callnum_host_self_trap"].count),
		  v($S.rpc_heatmap["dserver_callnum_host_self_trap"].uds),
		  v($S.rpc_heatmap["dserver_callnum_host_self_trap"].ring)
		] | @tsv'
	} >>"$rows"
	# The point label in the row is the thread count the guest announced, which
	# is the authoritative "threads ever created so far".
}

churn_table() { # label rows
	local label="$1"
	local rows="$2"

	if [ "$leg_ring_present" = "absent" ]; then
		printf 'CENSUS CHURN LANE COLUMNS prefix=%s ABSENT-BY-DESIGN (attach_*, lanes_registered_cumulative and max_ring_threads_per_process are structurally zero on a build without the ring transport; they are NOT measurements of this workload)\n' \
			"$label"
	fi
	printf 'CENSUS CHURN TABLE prefix=%s columns=point host_fds host_fd_eventfds guest_visible rpcs attach_attempts attach_successes attach_rejects lanes_registered_cumulative max_ring_threads_per_process fb_no_ring_proc_none fb_no_ring_proc_has fb_has_ring fb_control_plane fb_ineligible host_self_total host_self_uds host_self_ring\n'
	local prev="" line
	while IFS= read -r line; do
		[ -n "$line" ] || continue
		local f="${line%%$'\t'*}"
		local host_fds slope="first"
		host_fds="$(printf '%s' "$line" | cut -f2)"
		if [ -n "$prev" ] && is_int "$host_fds" && is_int "$prev"; then
			slope="$(awk -v a="$prev" -v b="$host_fds" 'BEGIN { printf "%+d", b - a }')"
		fi
		printf 'CENSUS CHURN ROW prefix=%s %s host_fd_delta_vs_prev_point=%s\n' \
			"$label" "$(printf '%s' "$line" | tr '\t' ' ')" "$slope"
		prev="$host_fds"
	done <"$rows"
}

churn_verdict() { # label rows prefd postfd settlefd total
	local label="$1"
	local rows="$2"
	local prefd="$3"
	local postfd="$4"
	local settlefd="$5"
	local total="$6"
	local base last mid slope_early slope_late stats ef_base ef_last ef_settle

	stats="$(guest_lane_stats "$STAGE_LOG" "$(guest_pid "$STAGE_LOG")")"
	base="$(fd_count "$prefd")"
	last="$(fd_count "$postfd")"
	ef_base="$(fd_eventfds "$prefd")"
	ef_last="$(fd_eventfds "$postfd")"
	ef_settle="$(fd_eventfds "$settlefd")"
	# The two slopes are measured over the IN-LOOP sample points only.  The rows
	# end with two tail samples (after_loop, post_settle) that are taken after the
	# churn has finished; folding those into the trend would report the post-loop
	# descriptor RELEASE as if it were growth, so they are excluded here and
	# reported separately as the drain.
	local npoints half_half in_loop_last_row
	npoints="$(wc -l <"$rows" | tr -d ' ')"
	in_loop_last_row=$(( npoints - 2 ))
	if [ "$in_loop_last_row" -lt 2 ]; then
		in_loop_last_row="$npoints"
	fi
	if [ "$in_loop_last_row" -ge 4 ]; then
		half_half=$(( in_loop_last_row / 2 ))
		local a b c d
		a="$(sed -n '1p' "$rows" | cut -f2)"
		b="$(sed -n "$(( half_half ))p" "$rows" | cut -f2)"
		c="$(sed -n "$(( half_half + 1 ))p" "$rows" | cut -f2)"
		d="$(sed -n "$(( in_loop_last_row ))p" "$rows" | cut -f2)"
		slope_early="$(awk -v a="$a" -v b="$b" 'BEGIN { printf "%+d", b - a }')"
		slope_late="$(awk -v a="$c" -v b="$d" 'BEGIN { printf "%+d", b - a }')"
	else
		slope_early="absent"
		slope_late="absent"
	fi
	mid="$(sed -n "$(( in_loop_last_row / 2 ))p" "$rows" | cut -f2)"

	# The plain answer to the question the constant-anchor decision turns on:
	# did the descriptor count keep growing over the whole churn, or did it stop?
	local trend
	local first_point_fds
	first_point_fds="$(sed -n '1p' "$rows" | cut -f2)"
	if [ "$slope_late" = "absent" ]; then
		trend="UNPROVEN (too few sample points to compare halves)"
	elif is_int "$first_point_fds" && is_int "$last" && [ "$first_point_fds" -eq "$last" ]; then
		trend="FLAT-THROUGHOUT (the count is identical at the first and last in-loop sample points)"
	elif [ "$slope_late" -eq 0 ]; then
		trend="BOUNDED-BY-THE-LANE-CAP (the count did not grow at all between the midpoint in-loop point and the last in-loop point, i.e. it stopped growing while threads were still being created)"
	else
		trend="STILL-GROWING-at-the-last-in-loop-point"
	fi
	local exhausted
	exhausted="$(lane_stat_field "$stats" exhausted)"
	if [ -n "$exhausted" ] && [ "$exhausted" -gt 0 ]; then
		trend="$trend; the guest lane table filled (exhausted=$exhausted claims), which is what bounds it"
	fi

	printf 'CENSUS CHURN VERDICT prefix=%s threads_ever_created=%s host_fds_baseline=%s host_fds_midpoint=%s host_fds_at_last_point=%s host_fds_after_settle=%s host_fd_slope_first_half=%s host_fd_slope_second_half=%s lane_eventfds_baseline=%s lane_eventfds_at_last_point=%s lane_eventfds_after_settle=%s descriptor_trend=%s guest_lane_stats=[%s] host_fds_after_settle_count=%s\n' \
		"$label" "$total" "$base" "${mid:-absent}" "$last" \
		"$(fd_count "$settlefd")" \
		"${slope_early:-absent}" "${slope_late:-absent}" \
		"${ef_base:-absent}" "${ef_last:-absent}" "${ef_settle:-absent}" \
		"$trend" "${stats:-absent}" "$(fd_count "$settlefd")"

	local drain="absent"
	if is_int "$last" && [ -s "$settlefd" ]; then
		drain="$(awk -v a="$last" -v b="$(fd_count "$settlefd")" 'BEGIN { printf "%+d", b - a }')"
	fi
	printf 'CENSUS CHURN DRAIN prefix=%s host_fds_change_from_last_in_loop_point_to_post_settle=%s (sign is the RELEASE direction; this is why the trend above excludes the tail samples)\n' \
		"$label" "$drain"

	# The two measured facts below are reported together because their
	# combination is the one that matters for a constant-anchor transport: the
	# guest's lane table still reports lanes live after the loop, while the
	# process' open wake-eventfd count has dropped.
	local held
	held="$(lane_stat_field "$stats" held_now)"
	if [ "$leg_ring_present" = "present" ] && is_int "$held" && is_int "$ef_settle"; then
		printf 'CENSUS CHURN RETAINED prefix=%s lanes_held_now=%s lane_eventfds_at_last_in_loop_point=%s lane_eventfds_after_settle=%s eventfds_released_after_the_loop=%s open_table_slots_minus_eventfds=%s (the guest holds %s lanes at exit; %s of their wake descriptors are still open at the post-settle sample, so %s lane slots refer to a descriptor that is no longer open AT THAT INSTANT -- INFERRED, not directly observed, and the harness never saw a doorbell write on such a slot, so the consequence is NOT measured; the release also happens between the last in-loop point and the settle sample, so it is asynchronous rather than immediate)\n' \
			"$label" "$held" "$ef_last" "$ef_settle" \
			"$(( ef_last - ef_settle ))" "$(( held - ef_settle ))" \
			"$held" "$ef_settle" "$(( held - ef_settle ))"
	fi
}

# --- C3: simultaneous threads above the lane cap --------------------------

run_window_sim() { # label prefix
	local label="$1"
	local target="$2"
	local threads="$3"
	local window="C3-$threads"
	local pre="$work/$label-sim$threads-pre.json"
	local mid="$work/$label-sim$threads-mid.json"
	local post="$work/$label-sim$threads-post.json"
	local prefd="$work/$label-sim$threads-pre.fds"
	local midfd="$work/$label-sim$threads-mid.fds"
	local postfd="$work/$label-sim$threads-post.fds"
	local setup_tsv="$work/$label-sim$threads-pre-mid.tsv"
	local loop_tsv="$work/$label-sim$threads-mid-post.tsv"
	local pid="" reason="" pre_ok=0 mid_ok=0 post_ok=0 created="absent"

	prefix="$target"
	start_stage "$label-sim$threads" "$sim_timeout" \
		"'$guest_bin' sim $threads $sim_requests $sim_pre_ms $sim_hold_ms $sim_post_ms"

	if wait_marker '^DTC_BARRIER pre$' 120; then
		if window_open '^DTC_BARRIER threads-live$'; then
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
				if safe_snapshot "$target" "$pre"; then pre_ok=1; else
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

	if wait_marker '^DTC_BARRIER threads-live$' "$sim_timeout"; then
		if window_open '^DTC_BARRIER action-done$'; then
			if safe_snapshot "$target" "$mid"; then mid_ok=1; fi
			host_fd_snapshot "$pid" "$midfd"
		elif [ -z "$reason" ]; then
			reason="the threads-live sample window closed before the host read it"
		fi
	elif [ -z "$reason" ]; then
		reason="the guest never reached its threads-live barrier"
	fi

	if wait_marker '^DTC_BARRIER action-done$' "$sim_timeout"; then
		if safe_snapshot "$target" "$post"; then post_ok=1; fi
		host_fd_snapshot "$pid" "$postfd"
	elif [ -z "$reason" ]; then
		reason="the workload did not reach its action barrier"
	fi
	finish_stage

	created="$(sed -n 's/^DTC sim threads_requested=[0-9]* threads_created=\([0-9][0-9]*\)$/\1/p' "$STAGE_LOG" | tail -1)"
	printf 'CENSUS SIM window=%s prefix=%s threads_requested=%s elapsed_s=%s stage_rc=%s guest_threads_created=%s sim_requests_per_thread=%s\n' \
		"$window" "$label" "$threads" "$(stage_elapsed)" "$STAGE_RC" \
		"${created:-absent}" "$sim_requests"
	guest_lines "$STAGE_LOG" '^DTC sim'
	guest_lines "$STAGE_LOG" '^DTC fds'
	guest_lines "$STAGE_LOG" '\[dring-lane-stats\]'

	local stats; stats="$(guest_lane_stats "$STAGE_LOG" "$pid")"
	if [ -n "$stats" ]; then
		printf 'CENSUS SIM LANE_DUMP window=%s prefix=%s pid=%s %s\n' \
			"$window" "$label" "$pid" "$stats"
	else
		printf 'CENSUS SIM LANE_DUMP window=%s prefix=%s pid=%s absent (this build has no guest lane table; see the UNPROVEN lines below)\n' \
			"$window" "$label" "${pid:-?}"
	fi

	if is_int "$created" && [ "$created" -lt "$threads" ]; then
		printf 'CENSUS SIM FAILURE window=%s prefix=%s threads_requested=%s threads_created=%s shortfall=%s (the prefix did not tolerate this many simultaneous guest threads; the window below measures only the threads that were actually created)\n' \
			"$window" "$label" "$threads" "$created" \
			"$(( threads - created ))"
	fi

	if [ "$pre_ok" = "1" ] && [ "$mid_ok" = "1" ]; then
		census_deltas "$pre" "$mid" "$setup_tsv"
		report_window "$label" "$target" "$window-setup" "$setup_tsv" \
			"guest_threads_created" "${created:-absent}" "$mid"
		report_calls "$label" "$window-setup" "$setup_tsv"
		report_fds "$label" "$window-setup" baseline "$prefd" 0 ""
		report_fds "$label" "$window-setup" threads_live "$midfd" \
			"${created:-absent}" \
			" host_fd_per_extra_thread=$(awk -v a="$(fd_count "$prefd")" -v b="$(fd_count "$midfd")" -v n="${created:-0}" 'BEGIN { printf "%.3f", (n > 0 ? (b - a) / n : 0) }')"
		# The decisive above-the-cap question: how many threads hold a lane and
		# how many of the trap calls fell back.
		local gots lanes
		lanes="$(tsv_gauge residual_max_ring_threads_per_process "$setup_tsv")"
		if is_int "$lanes" && is_int "$created"; then
			gots=$(( created - lanes ))
			[ "$gots" -ge 0 ] || gots=0
			printf 'CENSUS SIM LANES window=%s prefix=%s threads_created=%s lanes_live_max_ring_threads_per_process=%s threads_without_a_lane=%s trap_calls_ring=%s trap_calls_uds=%s fallback_reason_no_ring_proc_has=%s\n' \
				"$window" "$label" "$created" "$lanes" "$gots" \
				"$(tsv_call_delta heat_ring dserver_callnum_host_self_trap "$setup_tsv")" \
				"$(tsv_call_delta heat_uds dserver_callnum_host_self_trap "$setup_tsv")" \
				"$(tsv_reason thread_no_ring_proc_has "$setup_tsv")"
		elif [ "$leg_ring_present" = "absent" ]; then
			printf 'CENSUS SIM LANES window=%s prefix=%s threads_created=%s UNPROVEN missing_key=ring_transport (this build has no ring transport: lanes_live=0 above is structural, not measured; every thread fell back to the per-thread UDS socket by construction, trap_calls_uds=%s)\n' \
				"$window" "$label" "${created:-absent}" \
				"$(tsv_sum_names percall "$ring_allowlist" "$setup_tsv")"
		else
			printf 'CENSUS SIM LANES window=%s prefix=%s threads_created=%s UNPROVEN missing_key=residual_max_ring_threads_per_process (the server-side live lane gauge did not answer with a number)\n' \
				"$window" "$label" "${created:-absent}"
		fi
	else
		printf 'CENSUS SIM window=%s prefix=%s UNPROVEN reason=%s\n' "$window" \
			"$label" "${reason:-the pre or threads-live snapshot is missing}"
		fail "window $window prefix $label did not produce its samples: ${reason:-unknown}"
	fi

	if [ "$mid_ok" = "1" ] && [ "$post_ok" = "1" ]; then
		census_deltas "$mid" "$post" "$loop_tsv"
		report_window "$label" "$target" "$window-loop" "$loop_tsv" \
			"dserver_callnum_host_self_trap" \
			"$(tsv_call_delta percall dserver_callnum_host_self_trap "$loop_tsv")" \
			"$post"
		report_fds "$label" "$window-loop" after_join "$postfd" \
			"${created:-absent}" \
			" host_fd_delta_vs_threads_live=$(awk -v a="$(fd_count "$midfd")" -v b="$(fd_count "$postfd")" 'BEGIN { printf "%+d", b - a }')"
	fi
}

# --- B2: blocking Mach RPC and psynch -------------------------------------

run_window_blockrecv() { # label prefix
	local label="$1"
	local target="$2"
	local window="B2-blockrecv"
	local pre="$work/$label-blk-pre.json"
	local post="$work/$label-blk-post.json"
	local prefd="$work/$label-blk-pre.fds"
	local postfd="$work/$label-blk-post.fds"
	local tsv="$work/$label-blk-pre-post.tsv"
	local pid="" reason="" pre_ok=0 post_ok=0 rounds="absent" recv_ok="absent" sends_ok="absent" blk="absent"

	prefix="$target"
	start_stage "$label-blk" "$blk_timeout" \
		"'$guest_bin' blockrecv $blk_rounds $blk_settle_ms $blk_pre_ms $blk_post_ms"

	if wait_marker '^DTC_BARRIER pre$' 120; then
		if window_open '^DTC_BARRIER receiver-parked$'; then
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
				if safe_snapshot "$target" "$pre"; then pre_ok=1; else
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

	if wait_marker '^DTC_BARRIER action-done$' "$blk_timeout"; then
		if safe_snapshot "$target" "$post"; then post_ok=1; fi
		host_fd_snapshot "$pid" "$postfd"
	elif [ -z "$reason" ]; then
		reason="the workload did not reach its action barrier"
	fi
	finish_stage

	rounds="$(sed -n 's/^DTC blockrecv rounds_requested=\([0-9][0-9]*\).*/\1/p' "$STAGE_LOG" | tail -1)"
	recv_ok="$(sed -n 's/^DTC blockrecv .*recv_ok=\([0-9][0-9]*\).*/\1/p' "$STAGE_LOG" | tail -1)"
	sends_ok="$(sed -n 's/^DTC blockrecv rounds_requested=[0-9]* sends_ok=\([0-9][0-9]*\).*/\1/p' "$STAGE_LOG" | tail -1)"
	printf 'CENSUS BLK window=%s prefix=%s elapsed_s=%s stage_rc=%s rounds_requested=%s guest_recv_ok=%s settle_ms=%s\n' \
		"$window" "$label" "$(stage_elapsed)" "$STAGE_RC" \
		"${rounds:-absent}" "${recv_ok:-absent}" "$blk_settle_ms"
	guest_lines "$STAGE_LOG" '^DTC blockrecv'
	guest_lines "$STAGE_LOG" '^DTC fds'

	if [ -z "$reason" ] && [ "$pre_ok" = "1" ] && [ "$post_ok" = "1" ]; then
		census_deltas "$pre" "$post" "$tsv"
		report_window "$label" "$target" "$window" "$tsv" \
			"dserver_callnum_mach_msg_overwrite" \
			"$(tsv_call_delta percall dserver_callnum_mach_msg_overwrite "$tsv")" \
			"$post"
		report_calls "$label" "$window" "$tsv"
		report_fds "$label" "$window" baseline "$prefd" 0 ""
		report_fds "$label" "$window" after_loop "$postfd" 0 \
			" host_fd_delta_across_window=$(awk -v a="$(fd_count "$prefd")" -v b="$(fd_count "$postfd")" 'BEGIN { printf "%+d", b - a }')"
		# The blocking claim is only a result if the counter actually moved.
		blk="$(tsv_scalar msg_blocking_receive "$tsv")"
		local overwrite
		overwrite="$(tsv_call_delta percall dserver_callnum_mach_msg_overwrite "$tsv")"
		if is_int "$blk" && [ "$blk" -gt 0 ]; then
			printf 'CENSUS BLK PROOF window=%s prefix=%s msg_blocking_receive_delta=%s verdict=the-blocking-receive-counter-MOVED\n' \
				"$window" "$label" "$blk"
			if is_int "$overwrite"; then
				printf 'CENSUS BLK CROSSCHECK window=%s prefix=%s mach_msg_overwrite_percall_delta=%s blocking_share_of_overwrite=%s guest_recv_ok=%s guest_sends_ok=%s\n' \
					"$window" "$label" "$overwrite" \
					"$(ratio "$blk" "$overwrite")" "${recv_ok:-absent}" \
					"${sends_ok:-absent}"
			else
				printf 'CENSUS BLK CROSSCHECK window=%s prefix=%s mach_msg_overwrite_percall_delta=absent UNCROSSCHECKABLE (per_call carries no mach_msg_overwrite row in this build -- recordCall never runs for it, so the msg_* census above is the only counter that sees these calls; the guest-visible outcome is guest_recv_ok=%s guest_sends_ok=%s)\n' \
					"$window" "$label" "${recv_ok:-absent}" \
					"${sends_ok:-absent}"
			fi
		else
			printf 'CENSUS BLK PROOF window=%s prefix=%s msg_blocking_receive_delta=%s UNPROVEN missing_key=msg_blocking_receive (the window did not drive a blocking receive; the delta is %s, so nothing about blocking frequency can be concluded from it)\n' \
				"$window" "$label" "${blk:-absent}" "${blk:-absent}"
			fail "the blocking-receive window on prefix $label did not move msg_blocking_receive"
		fi
	else
		printf 'CENSUS BLK window=%s prefix=%s UNPROVEN reason=%s\n' "$window" \
			"$label" "${reason:-the pre or post snapshot is missing}"
		fail "window $window prefix $label did not produce its samples: ${reason:-unknown}"
	fi
}

run_window_psynch() { # label prefix
	local label="$1"
	local target="$2"
	local window="B2-psynch"
	local pre="$work/$label-psy-pre.json"
	local post="$work/$label-psy-post.json"
	local prefd="$work/$label-psy-pre.fds"
	local postfd="$work/$label-psy-post.fds"
	local tsv="$work/$label-psy-pre-post.tsv"
	local pid="" reason="" pre_ok=0 post_ok=0 created="absent"

	prefix="$target"
	start_stage "$label-psy" "$psynch_timeout" \
		"'$guest_bin' psynch $psynch_threads $psynch_iters $psynch_crounds $psynch_pre_ms $psynch_post_ms"

	if wait_marker '^DTC_BARRIER pre$' 120; then
		if window_open '^DTC_BARRIER action-done$'; then
			pid="$(guest_pid "$STAGE_LOG")"
			if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
				if safe_snapshot "$target" "$pre"; then pre_ok=1; else
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

	if wait_marker '^DTC_BARRIER action-done$' "$psynch_timeout"; then
		if safe_snapshot "$target" "$post"; then post_ok=1; fi
		host_fd_snapshot "$pid" "$postfd"
	elif [ -z "$reason" ]; then
		reason="the workload did not reach its action barrier"
	fi
	finish_stage

	created="$(sed -n 's/^DTC psynch threads_created=\([0-9][0-9]*\)$/\1/p' "$STAGE_LOG" | tail -1)"
	printf 'CENSUS PSYNCH window=%s prefix=%s threads_requested=%s elapsed_s=%s stage_rc=%s guest_threads_created=%s iters=%s crounds=%s\n' \
		"$window" "$label" "$psynch_threads" "$(stage_elapsed)" "$STAGE_RC" \
		"${created:-absent}" "$psynch_iters" "$psynch_crounds"
	guest_lines "$STAGE_LOG" '^DTC psynch'
	guest_lines "$STAGE_LOG" '^DTC fds'

	if [ -z "$reason" ] && [ "$pre_ok" = "1" ] && [ "$post_ok" = "1" ]; then
		census_deltas "$pre" "$post" "$tsv"
		local sync_sum
		sync_sum="$(tsv_sum_names percall "$class_sync" "$tsv")"
		report_window "$label" "$target" "$window" "$tsv" \
			"guest_pthread_sync_ops_nominal" \
			"$(( psynch_iters * psynch_threads + psynch_crounds ))" "$post"
		report_calls "$label" "$window" "$tsv"
		report_fds "$label" "$window" baseline "$prefd" 0 ""
		report_fds "$label" "$window" after_join "$postfd" "${created:-absent}" \
			" host_fd_delta_across_window=$(awk -v a="$(fd_count "$prefd")" -v b="$(fd_count "$postfd")" 'BEGIN { printf "%+d", b - a }')"
		# The psynch claim is only a result if at least one psynch callnum moved.
		local cancel_delta
		cancel_delta="$(tsv_call_delta percall dserver_callnum_pthread_canceled "$tsv")"
		if is_int "$cancel_delta" && [ "$cancel_delta" -gt 0 ]; then
			printf 'CENSUS PSYNCH MECHANISM window=%s prefix=%s pthread_canceled_delta=%s per_mutex_op=%s verdict=the-contended-pthread-path-does-not-issue-psynch-RPCs-in-this-build-it-parks-in-guest-on-ulock-and-brackets-each-park-with-pthread_canceled-cancellation-point-RPCs\n' \
				"$window" "$label" "$cancel_delta" \
				"$(ratio "$cancel_delta" "$(( psynch_iters * psynch_threads ))" )"
		fi
		if [ "$sync_sum" -gt 0 ]; then
			printf 'CENSUS PSYNCH PROOF window=%s prefix=%s sync_callnum_delta_sum=%s moved=[%s] verdict=the-pthread-sync-path-MAPS-onto-psynch-RPCs\n' \
				"$window" "$label" "$sync_sum" "$(tsv_moved percall "$tsv" | tr ' ' '\n' | grep -E 'psynch|semaphore' | tr '\n' ' ')"
		else
			printf 'CENSUS PSYNCH PROOF window=%s prefix=%s sync_callnum_delta_sum=0 UNPROVEN missing_key=psynch_* (no psynch or semaphore callnum moved; this window cannot conclude that pthread mutex/cond map onto the psynch RPCs)\n' \
				"$window" "$label"
			fail "the psynch window on prefix $label did not move any psynch callnum"
		fi
	else
		printf 'CENSUS PSYNCH window=%s prefix=%s UNPROVEN reason=%s\n' "$window" \
			"$label" "${reason:-the pre or post snapshot is missing}"
		fail "window $window prefix $label did not produce its samples: ${reason:-unknown}"
	fi
}

# ------------------------------------------------------------------ leg

run_leg() { # label prefix
	local label="$1"
	local target="$2"
	local window n

	active_label="$label"
	active_prefix="$target"

	note "== leg $label prefix=$target =="
	if ! boot_prefix "$label" "$target"; then
		printf 'LLC_HARNESS_FAIL prefix=%s stage=boot\n' "$label" >&2
		remove_guest_artifacts "$target"
		if shutdown_prefix "$label" "$target"; then :; else shutdown_failed=1; fi
		active_label=""
		active_prefix=""
		fail "prefix $label did not boot with the censuses armed"
		return 1
	fi

	if ! stage_fixture "$target" "$label"; then
		printf 'LLC_HARNESS_FAIL prefix=%s stage=fixture\n' "$label" >&2
		remove_guest_artifacts "$target"
		if shutdown_prefix "$label" "$target"; then :; else shutdown_failed=1; fi
		active_label=""
		active_prefix=""
		fail "prefix $label did not stage the fixture"
		return 1
	fi

	for window in $windows; do
		case "$window" in
		churn) run_window_churn "$label" "$target" ;;
		sim)
			for n in $sim_threads; do
				run_window_sim "$label" "$target" "$n"
			done
			;;
		blockrecv) run_window_blockrecv "$label" "$target" ;;
		psynch) run_window_psynch "$label" "$target" ;;
		esac
	done

	remove_guest_artifacts "$target"
	if shutdown_prefix "$label" "$target"; then :; else shutdown_failed=1; fi
	active_label=""
	active_prefix=""
	return 0
}

note "LANE_LIFECYCLE_CENSUS legs=[${legs[*]}] on=$prefix_on off=$prefix_off windows=[$windows] churn=${churn_count}x/every ${churn_every} sim=[$sim_threads]x$sim_requests blk_rounds=$blk_rounds psynch=${psynch_threads}x$psynch_iters/$psynch_crounds stat_tool=$stat_tool"

for leg in "${legs[@]}"; do
	eval "target=\$prefix_$leg"
	run_leg "$leg" "$target" || true
done

# ------------------------------------------------------------- verdict

printf 'LLC_RESULT failures=%s shutdown_failed=%s\n' "$failures" "$shutdown_failed"
if [ "$shutdown_failed" -ne 0 ]; then
	printf 'LLC_FAILED a prefix was shut down but prefix-owned processes or mounts survived\n' >&2
	exit 4
fi
if [ "$failures" -ne 0 ]; then
	printf 'LLC_FAILED %s census failure(s)\n' "$failures" >&2
	exit 1
fi
printf 'LLC_OK every window produced its samples and every prefix was left clean\n'

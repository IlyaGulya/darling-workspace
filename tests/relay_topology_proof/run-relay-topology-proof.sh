#!/usr/bin/env bash
#
# run-relay-topology-proof.sh -- build the relay topology falsification harness
# and run it on the host and inside ordinary Docker.
#
# The harness (relay-topology-proof.c) proves or breaks the relay-based
# descriptor architecture:
#   guest descriptors stay native and direct, every loader/transport object
#   lives in a companion process created with CLONE_VM but WITHOUT CLONE_FILES,
#   the guest parks on a futex, the relay coalesces cold wakes into ONE eventfd
#   that the server's epoll waits on, and the hot path is shared-memory only.
#
# It prints one line per assertion (A1..A7) in each environment, then this
# runner prints RELAY_TOPOLOGY_OK.  Any failure exits non-zero.
#
# The runner then runs the harness's sustained-load mode in both environments at
# the product's measured cold fraction (225 per 1000 exchanges, the product's
# doorbell_share 0.2248) and adds claims S1..S4, measured by a supervisor that
# this script writes into its temporary directory:
#   S1 (MUST PASS)  N exchanges with zero lost wakes and an unchanged guest
#                   descriptor set.  Without it every cost figure below would
#                   describe a path the exchanges did not actually take.
#   S2 (measurement) the design's doorbell-equivalent rate and cost per
#                   exchange: eventfd writes per exchange, syscw per exchange
#                   and voluntary context switches per exchange, read from
#                   /proc/<pid>/{status,io} for the guest, the relay and the
#                   server in the same window, with the product's three
#                   baseline figures beside them.
#   S3 (measurement) the coalescing factor achieved: exchanges woken per
#                   eventfd write, and the nudges whose write was suppressed.
#   S4 (measurement) the share of exchanges completed with no relay
#                   involvement at all, against the product's 77.52%.
# The comparison block states plainly that the harness models the mechanism and
# does not execute mldr or darlingserver.
#
# The harness must be able to FAIL: after the clean runs this script builds
# three deliberate mutations of the premise in its temporary directory (never
# in the repository) and requires the expected assertions or claims to fail:
#   1. relay created WITH CLONE_FILES          -> A1 and A2 must fail
#   2. futex word sampled at the last moment   -> A4 must fail
#   3. cold phase skips the pending mark       -> S1 must fail
#
# Usage: tests/relay_topology_proof/run-relay-topology-proof.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/relay-topology-proof.c"
work="$(mktemp -d /tmp/relay-topology-proof.XXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

cc="${CC:-gcc}"
cflags=(-std=gnu11 -O2 -Wall -Wextra -Werror -static -fno-stack-protector)
bin="$work/relay-topology-proof"
image="${RELAY_PROOF_DOCKER_IMAGE:-ubuntu:24.04}"
run_timeout="${RELAY_PROOF_TIMEOUT:-900}"

note() { printf 'INFO %s\n' "$*"; }
fail() {
	printf 'INFO FAIL %s\n' "$*" >&2
	exit 1
}

[ -r "$src" ] || fail "missing harness source: $src"

# --------------------------------------------------------------------------
# 1. compile on the host (static, so the same artifact runs in the container)
# --------------------------------------------------------------------------
note "compile: $cc ${cflags[*]} -o $bin $src"
"$cc" "${cflags[@]}" -o "$bin" "$src"

if readelf -lW "$bin" | grep -q 'INTERP'; then
	fail "binary carries PT_INTERP; it must be static"
fi
note "static: program headers contain no PT_INTERP"

calls="$(objdump -d --disassemble=relay_entry "$bin" | grep -cE '\bcall' || true)"
[ "$calls" = "0" ] || fail "relay_entry contains $calls call instruction(s)"
note "relay_entry: 0 call instructions (raw syscalls only, no libc)"
note "sha256: $(sha256sum "$bin" | awk '{print $1}')"

# --------------------------------------------------------------------------
# 2. A3's syscall-count evidence on the host: strace -f -c over an otherwise
#    identical hot phase, at N=1000000 and at N=0.  The setup/teardown syscalls
#    cancel and the difference is the hot path's syscall count.
# --------------------------------------------------------------------------
total_calls() {
	# strace -c summary: columns are
	#   %time seconds usecs/call calls [errors] syscall
	awk '$NF == "total" { if (NF >= 6) print $(NF - 2); else print $(NF - 1) }' "$1"
}

note "measuring the hot phase under: strace -f -c -o <file> $bin hot N"
strace -f -c -o "$work/host_hot_n.txt" "$bin" hot 1000000 >/dev/null 2>&1
strace -f -c -o "$work/host_hot_z.txt" "$bin" hot 0 >/dev/null 2>&1
host_n="$(total_calls "$work/host_hot_n.txt")"
host_z="$(total_calls "$work/host_hot_z.txt")"
host_delta="$((host_n - host_z))"
note "strace -f -c: N=1000000 -> $host_n syscalls, N=0 -> $host_z syscalls, delta=$host_delta"
[ "$host_delta" = "0" ] || fail "hot phase issued $host_delta syscall(s) beyond setup"

# --------------------------------------------------------------------------
# 3. run the assertions on the host
# --------------------------------------------------------------------------
run_leg() {
	local label="$1"
	shift
	local log="$work/$label.log"
	local rc=0 i

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e
	cat "$log"

	[ "$rc" = "0" ] || fail "$label: harness exited $rc"
	for i in 1 2 3 4 5 6 7; do
		if ! grep -qE "^A$i PASS " "$log"; then
			fail "$label: assertion A$i did not pass"
		fi
	done
	note "$label: A1..A7 all PASS"
}

run_leg host env RELAY_PROOF_STRACE_DELTA="$host_delta" "$bin" all

# --------------------------------------------------------------------------
# 4. run the same binary inside ordinary Docker.  The container image is
#    present locally (no network pull); the statically linked binary and the
#    host's strace (plus its shared libraries) are mounted read-only so the
#    same strace -f -c mechanism is available inside the container.
# --------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "docker is required for the container leg"
docker image inspect "$image" >/dev/null 2>&1 ||
	fail "docker image $image is not present locally (this harness never pulls)"

docker_args=(run --rm -u 1000:1000 --cap-drop=ALL --ipc=private -v "$work:/proof:ro")
if command -v strace >/dev/null 2>&1; then
	strace_bin="$(command -v strace)"
	docker_args+=(-v "$strace_bin:$strace_bin:ro")
	while read -r lib; do
		case "$lib" in
		/*) docker_args+=(-v "$lib:$lib:ro") ;;
		esac
	done < <(ldd "$strace_bin" | awk '/=>/ { print $3 }')
else
	note "strace not found on the host; the container leg cannot measure A3"
fi

note "docker: docker ${docker_args[*]} $image /proof/relay-topology-proof all"
run_leg docker docker "${docker_args[@]}" "$image" /proof/relay-topology-proof all

if ! grep -qE '^A3 PASS .*delta=0' "$work/docker.log"; then
	fail "docker: A3 was not established with a zero syscall delta in the container"
fi
if ! grep -qE '^A2 PASS .*close_range' "$work/docker.log"; then
	fail "docker: A2 did not exercise close_range in the container"
fi

# --------------------------------------------------------------------------
# 5. sustained-load measurement at the product's cold fraction.
#
# The harness counts the exchanges by the path that served them and reports its
# own counters; this supervisor opens and closes the external window around
# exactly the exchanges and reads the kernel's counters for the three roles
# (/proc/<pid>/status and /proc/<pid>/io), then prints the S1..S4 claims.  It is
# written here, into the temporary directory, and never committed.
# --------------------------------------------------------------------------
cat >"$work/sustain-sampler.sh" <<'SAMPLER_EOF'
#!/bin/sh
# sustain-sampler.sh -- supervisor for the harness's sustained-load mode.
#
#   * the harness creates $HS/pids and $HS/ready and then blocks;
#   * this script samples /proc/<pid>/status and /proc/<pid>/io for the guest,
#     the relay and the server, then releases the harness with $HS/before;
#   * the harness runs the N exchanges, creates $HS/load_done and blocks;
#   * this script samples the three roles again, releases the harness with
#     $HS/after, and prints the per-role figures and the S1..S4 claims.
#
# Everything in the window comes from the kernel (/proc) or from the harness's
# own shared counters; nothing is strace-based.
#
# usage: sh sustain-sampler.sh LABEL HARNESS [HARNESS-ARGS...]
set -u

label="$1"
shift
bin="$1"
shift

hs="$(mktemp -d /tmp/relay-sustain.XXXXXX)" || exit 3
trap 'rm -rf -- "$hs"' EXIT INT TERM
waits=600          # 600 x 0.05s = 30s per barrier

# give up on a run that cannot finish: kill the harness and whatever roles it
# has already reported, so nothing is left parked on the host
abort_run() {
	kill -9 "${guest:-}" "${relay:-}" "${server:-}" "$hpid" 2>/dev/null
	wait "$hpid" 2>/dev/null
	exit 3
}

sample() {                     # $1 = pid, $2 = output file
	sed -n -e 's/^voluntary_ctxt_switches:[[:space:]]*/ctxt_vol=/p' \
	       -e 's/^nonvoluntary_ctxt_switches:[[:space:]]*/ctxt_nonvol=/p' \
	       "/proc/$1/status" > "$2"
	sed -n -e 's/^syscr:[[:space:]]*/syscr=/p' \
	       -e 's/^syscw:[[:space:]]*/syscw=/p' \
	       "/proc/$1/io" >> "$2"
}

val() {                        # $1 = key, $2 = sampled file
	sed -n "s/^$1=//p" "$2" | head -n 1
}

metric() {                     # $1 = key of the harness's SUSTAIN METRIC line
	sed -n 's/^SUSTAIN METRIC //p' "$hs/log" | head -n 1 |
		tr ' ' '\n' | sed -n "s/^$1=//p" | head -n 1
}

wait_for() {                   # $1 = file, $2 = what, $3 = harness pid
	i=0
	while [ ! -f "$1" ]; do
		i=$((i + 1))
		if [ "$i" -gt "$waits" ]; then
			echo "SAMPLER $label: timed out waiting for $2"
			return 1
		fi
		if [ "$3" -gt 0 ] && ! kill -0 "$3" 2>/dev/null; then
			echo "SAMPLER $label: the harness exited while waiting for $2"
			return 1
		fi
		sleep 0.05
	done
	return 0
}

fmt_milli() {                  # $1 / $2 as a 5-decimal fraction
	[ "$2" -gt 0 ] || { echo "n/a"; return; }
	printf '%d.%05d' "$((($1) / ($2)))" "$((((($1) % ($2)) * 100000) / ($2)))"
}

fmt_pct() {                    # 100 * $1 / $2 as a 2-decimal percentage
	[ "$2" -gt 0 ] || { echo "n/a"; return; }
	printf '%d.%02d' "$((($1) * 100 / ($2)))" "$((($1) * 10000 / ($2) % 100))"
}

RELAY_PROOF_HS_DIR="$hs" "$bin" "$@" >"$hs/log" 2>&1 &
hpid=$!

if ! wait_for "$hs/ready" "the harness to announce its pids" "$hpid"; then
	abort_run
fi

guest="$(val guest "$hs/pids")"
relay="$(val relay "$hs/pids")"
server="$(val server "$hs/pids")"
n="$(val n "$hs/pids")"
permille="$(val cold_permille "$hs/pids")"

for p in "$guest" "$relay" "$server"; do
	case "$p" in
	'' | *[!0-9]*)
		echo "SAMPLER $label: the harness did not report a usable pid ('$p')"
		abort_run
		;;
	esac
	if [ ! -r "/proc/$p/status" ] || [ ! -r "/proc/$p/io" ]; then
		echo "SAMPLER $label: /proc files for pid $p are not readable"
		abort_run
	fi
done

sample "$guest" "$hs/guest.before"
sample "$relay" "$hs/relay.before"
sample "$server" "$hs/server.before"
: > "$hs/before"

if ! wait_for "$hs/load_done" "the exchanges to finish" "$hpid"; then
	abort_run
fi

sample "$guest" "$hs/guest.after"
sample "$relay" "$hs/relay.after"
sample "$server" "$hs/server.after"
: > "$hs/after"

wait "$hpid"
rc=$?
cat "$hs/log"

gvol=0; gnon=0; gsr=0; gsw=0
rvol=0; rnon=0; rsr=0; rsw=0
svol=0; snon=0; ssr=0; ssw=0
for role in guest relay server; do
	b="$hs/$role.before"
	a="$hs/$role.after"
	dvol=$(($(val ctxt_vol "$a") - $(val ctxt_vol "$b")))
	dnon=$(($(val ctxt_nonvol "$a") - $(val ctxt_nonvol "$b")))
	dsr=$(($(val syscr "$a") - $(val syscr "$b")))
	dsw=$(($(val syscw "$a") - $(val syscw "$b")))
	case "$role" in
	guest) p="$guest"; gvol=$dvol; gnon=$dnon; gsr=$dsr; gsw=$dsw ;;
	relay) p="$relay"; rvol=$dvol; rnon=$dnon; rsr=$dsr; rsw=$dsw ;;
	server) p="$server"; svol=$dvol; snon=$dnon; ssr=$dsr; ssw=$dsw ;;
	esac
	echo "ROLE label=$label role=$role pid=$p n=$n ctxt_vol=$dvol ctxt_nonvol=$dnon syscr=$dsr syscw=$dsw ctxt_vol_per_req=$(fmt_milli "$dvol" "$n") ctxt_nonvol_per_req=$(fmt_milli "$dnon" "$n") syscr_per_req=$(fmt_milli "$dsr" "$n") syscw_per_req=$(fmt_milli "$dsw" "$n")"
done
echo "SUSTAIN KERNEL label=$label n=$n guest_ctxt_vol=$gvol guest_ctxt_nonvol=$gnon guest_syscr=$gsr guest_syscw=$gsw relay_ctxt_vol=$rvol relay_ctxt_nonvol=$rnon relay_syscr=$rsr relay_syscw=$rsw server_ctxt_vol=$svol server_ctxt_nonvol=$snon server_syscr=$ssr server_syscw=$ssw"

exchanges="$(metric exchanges)"
cold="$(metric cold)"
hot="$(metric hot)"
bursts="$(metric bursts)"
burst_lanes="$(metric burst_lanes)"
writes="$(metric relay_eventfd_writes)"
bursted="$(metric nudge_bursts)"
moved="$(metric nudge_transitions)"
nudges="$(metric nudges)"
waiters="$(metric futex_waiters_woken)"
guard="$(metric relay_guard_ticks)"
lost="$(metric lost)"
aborted="$(metric aborted)"
cold_svc="$(metric server_cold_services)"
hot_svc="$(metric server_hot_services)"
fdsame="$(metric guest_fds_identical)"
fdleak="$(metric guest_transport_leak)"

for v in "$exchanges" "$cold" "$hot" "$bursts" "$writes" "$nudges" "$waiters" \
	"$guard" "$lost" "$aborted" "$cold_svc" "$hot_svc" "$fdsame" "$fdleak" "$n"; do
	case "$v" in
	'' | *[!0-9]*)
		echo "SAMPLER $label: the harness printed no usable SUSTAIN METRIC line"
		exit 3
		;;
	esac
done

skipped=$((nudges - writes))
all_vol=$((gvol + rvol + svol))
all_syscw=$((gsw + rsw + ssw))
all_syscr=$((gsr + rsr + ssr))

# ---- S1: the must-pass claim -------------------------------------------
s1=1
[ "$lost" -eq 0 ] || s1=0
[ "$aborted" -eq 0 ] || s1=0
[ "$exchanges" -eq "$n" ] || s1=0
[ "$((cold + hot))" -eq "$n" ] || s1=0
[ "$cold_svc" -eq "$cold" ] || s1=0
[ "$hot_svc" -eq "$hot" ] || s1=0
[ "$guard" -eq 0 ] || s1=0
[ "$fdsame" -eq 1 ] || s1=0
[ "$fdleak" -eq 0 ] || s1=0
grep -q '^S1 PASS ' "$hs/log" || s1=0

# ---- S2: the cost measurement, harness counters plus the kernel's ------
s2=1
[ "$writes" -ge 1 ] || s2=0
[ "$writes" -le "$nudges" ] || s2=0
[ "$nudges" -eq "$((bursted + moved))" ] || s2=0
[ "$gvol" -ge 0 ] && [ "$rvol" -ge 0 ] && [ "$svol" -ge 0 ] || s2=0
[ "$gsw" -ge 0 ] && [ "$rsw" -ge 0 ] && [ "$ssw" -ge 0 ] || s2=0
# the relay's only write syscalls are its eventfd writes, so the kernel must
# agree with the harness's own counter
rw=$((rsw - writes))
[ "$rw" -lt 0 ] && rw=$((-rw))
[ "$rw" -le 1 ] || s2=0

# ---- S3: coalescing ----------------------------------------------------
s3=1
[ "$bursts" -ge 1 ] || s3=0
[ "$writes" -ge 1 ] || s3=0
[ "$cold" -ge 1 ] || s3=0

# ---- S4: the hot share -------------------------------------------------
s4=1
[ "$((cold_svc + hot_svc))" -eq "$n" ] || s4=0
[ "$hot_svc" -eq "$hot" ] || s4=0

# the harness evaluates the same claims from the same counters; a claim is only
# PASS here if the two independent evaluations agree
grep -q '^S2 PASS ' "$hs/log" || s2=0
grep -q '^S3 PASS ' "$hs/log" || s3=0
grep -q '^S4 PASS ' "$hs/log" || s4=0

printf 'S1 %s sustained %s: %s/%s exchanges at cold_permille=%s (cold=%s hot=%s), results verified, ' \
	"$([ "$s1" -eq 1 ] && echo PASS || echo FAIL)" "$label" \
	"$exchanges" "$n" "$permille" "$cold" "$hot"
printf 'lost wakes=%s (relay guard-timeout recoveries=%s, aborted=%s), guest descriptor set identical=%s transport leak=%s\n' \
	"$lost" "$guard" "$aborted" "$fdsame" "$fdleak"

printf 'S2 %s cost per request %s (N=%s): relay eventfd writes=%s (%s/req), guest syscw=%s (%s/req), ' \
	"$([ "$s2" -eq 1 ] && echo PASS || echo FAIL)" "$label" "$n" \
	"$writes" "$(fmt_milli "$writes" "$n")" \
	"$gsw" "$(fmt_milli "$gsw" "$n")"
printf "relay syscw=%s (%s/req, the kernel's witness of the eventfd writes), server syscw=%s (%s/req), all-roles syscw=%s (%s/req); " \
	"$rsw" "$(fmt_milli "$rsw" "$n")" \
	"$ssw" "$(fmt_milli "$ssw" "$n")" \
	"$all_syscw" "$(fmt_milli "$all_syscw" "$n")"
printf 'voluntary context switches: guest=%s relay=%s server=%s (%s/req all roles); nudges=%s (%s/req), cold share=%s/1000 (%s/req)\n' \
	"$gvol" "$rvol" "$svol" "$(fmt_milli "$all_vol" "$n")" \
	"$nudges" "$(fmt_milli "$nudges" "$n")" \
	"$((cold * 1000 / n))" "$(fmt_milli "$cold" "$n")"

printf 'S3 %s coalescing %s (N=%s): %s exchanges per eventfd write (%s cold exchanges woke through %s writes); ' \
	"$([ "$s3" -eq 1 ] && echo PASS || echo FAIL)" "$label" "$n" \
	"$(fmt_milli "$cold" "$writes")" "$cold" "$writes"
printf 'mean burst=%s lanes (%s bursts, lane-sum=%s), nudges whose write was suppressed=%s/%s (%s%%)\n' \
	"$(fmt_milli "$cold" "$bursts")" "$bursts" "$burst_lanes" \
	"$skipped" "$nudges" "$(fmt_pct "$skipped" "$nudges")"

printf 'S4 %s hot share %s (N=%s): %s/%s exchanges (%s%%) completed with no relay involvement at all; %s (%s%%) required a relay wake ' \
	"$([ "$s4" -eq 1 ] && echo PASS || echo FAIL)" "$label" "$n" \
	"$hot" "$n" "$(fmt_pct "$hot" "$n")" \
	"$cold" "$(fmt_pct "$cold" "$n")"
printf '(server services: %s polled, %s woken; across the three roles in the same window: syscr=%s syscw=%s)\n' \
	"$hot_svc" "$cold_svc" "$all_syscr" "$all_syscw"

echo "SAMPLER $label: harness exit=$rc"
exit "$rc"
SAMPLER_EOF

sustain_n="${RELAY_PROOF_SUSTAIN_N:-200000}"
sustain_cold="${RELAY_PROOF_SUSTAIN_COLD_PERMILLE:-225}"

metric_of() {  # $1 = log, $2 = key of the harness's SUSTAIN METRIC line
	sed -n 's/^SUSTAIN METRIC //p' "$1" | head -n 1 |
		tr ' ' '\n' | sed -n "s/^$2=//p" | head -n 1
}
kernel_of() {  # $1 = log, $2 = key of the sampler's SUSTAIN KERNEL line
	sed -n 's/^SUSTAIN KERNEL //p' "$1" | head -n 1 |
		tr ' ' '\n' | sed -n "s/^$2=//p" | head -n 1
}
frac5() { awk -v a="$1" -v b="$2" 'BEGIN { if (b == 0) print "n/a"; else printf "%.5f", a / b }'; }

run_sustain_leg() {
	local label="$1"
	shift
	local log="$work/sustain_$label.log"
	local rc=0 c

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e
	sed "s/^/[sustain-$label] /" "$log"
	[ "$rc" = "0" ] || fail "sustain-$label: the harness exited $rc"
	for c in 1 2 3 4; do
		grep -qE "^S$c PASS " "$log" || fail "sustain-$label: claim S$c did not pass"
	done
	grep -qE '^ROLE label=[^ ]+ role=guest ' "$log" ||
		fail "sustain-$label: no guest /proc figures"
	grep -qE '^ROLE label=[^ ]+ role=relay ' "$log" ||
		fail "sustain-$label: no relay /proc figures"
	grep -qE '^ROLE label=[^ ]+ role=server ' "$log" ||
		fail "sustain-$label: no server /proc figures"
	grep -qE '^SUSTAIN KERNEL ' "$log" ||
		fail "sustain-$label: no kernel summary line"
	note "sustain-$label: S1..S4 all PASS with per-role kernel counters"
}

note "sustain host: sh $work/sustain-sampler.sh host $bin sustain $sustain_n $sustain_cold"
run_sustain_leg host sh "$work/sustain-sampler.sh" host "$bin" \
	sustain "$sustain_n" "$sustain_cold"

note "sustain docker: /bin/sh /proof/sustain-sampler.sh docker /proof/relay-topology-proof sustain $sustain_n $sustain_cold"
run_sustain_leg docker docker "${docker_args[@]}" "$image" \
	/bin/sh /proof/sustain-sampler.sh docker /proof/relay-topology-proof \
	sustain "$sustain_n" "$sustain_cold"

# --------------------------------------------------------------------------
# 5b. one comparison block: the harness's S2/S3/S4 figures beside the product
#     baseline measured on a ring-enabled Darling prefix.
# --------------------------------------------------------------------------
hn="$work/sustain_host.log"
dn="$work/sustain_docker.log"

compare_row() {  # $1 = metric, $2 = host, $3 = docker, $4 = product baseline
	printf 'COMPARE %-44s %-30s %-30s %s\n' "$1" "$2" "$3" "$4"
}

note "comparison: harness (this runner) vs the product baseline, same cold fraction"
compare_row "cold fraction (per 1000 exchanges)" \
	"$(metric_of "$hn" cold_permille)" "$(metric_of "$dn" cold_permille)" \
	"224.8 (doorbell_share 0.2248)"
compare_row "eventfd writes / exchange" \
	"$(frac5 "$(metric_of "$hn" relay_eventfd_writes)" "$(metric_of "$hn" n)")" \
	"$(frac5 "$(metric_of "$dn" relay_eventfd_writes)" "$(metric_of "$dn" n)")" \
	"0.22457 (doorbell_per_request)"
compare_row "syscw / exchange (3 roles, /proc)" \
	"$(frac5 "$(( $(kernel_of "$hn" guest_syscw) + $(kernel_of "$hn" relay_syscw) + $(kernel_of "$hn" server_syscw) ))" "$(metric_of "$hn" n)")" \
	"$(frac5 "$(( $(kernel_of "$dn" guest_syscw) + $(kernel_of "$dn" relay_syscw) + $(kernel_of "$dn" server_syscw) ))" "$(metric_of "$dn" n)")" \
	"0.22644 (syscw_per_request)"
compare_row "voluntary ctx switches / exchange (3 roles)" \
	"$(frac5 "$(( $(kernel_of "$hn" guest_ctxt_vol) + $(kernel_of "$hn" relay_ctxt_vol) + $(kernel_of "$hn" server_ctxt_vol) ))" "$(metric_of "$hn" n)")" \
	"$(frac5 "$(( $(kernel_of "$dn" guest_ctxt_vol) + $(kernel_of "$dn" relay_ctxt_vol) + $(kernel_of "$dn" server_ctxt_vol) ))" "$(metric_of "$dn" n)")" \
	"0.44839 (ctxt_vol_per_request; 2 per doorbell)"
compare_row "  guest / relay / server, per exchange" \
	"$(frac5 "$(kernel_of "$hn" guest_ctxt_vol)" "$(metric_of "$hn" n)") / $(frac5 "$(kernel_of "$hn" relay_ctxt_vol)" "$(metric_of "$hn" n)") / $(frac5 "$(kernel_of "$hn" server_ctxt_vol)" "$(metric_of "$hn" n)")" \
	"$(frac5 "$(kernel_of "$dn" guest_ctxt_vol)" "$(metric_of "$dn" n)") / $(frac5 "$(kernel_of "$dn" relay_ctxt_vol)" "$(metric_of "$dn" n)") / $(frac5 "$(kernel_of "$dn" server_ctxt_vol)" "$(metric_of "$dn" n)")" \
	"one process; wakes_issued_per_request 0.44865"
compare_row "exchanges woken / eventfd write" \
	"$(frac5 "$(metric_of "$hn" cold)" "$(metric_of "$hn" relay_eventfd_writes)")" \
	"$(frac5 "$(metric_of "$dn" cold)" "$(metric_of "$dn" relay_eventfd_writes)")" \
	"1.0 (44967 doorbells, one write each)"
compare_row "hot share, no relay involvement" \
	"$(frac5 "$(metric_of "$hn" hot)" "$(metric_of "$hn" n)")" \
	"$(frac5 "$(metric_of "$dn" hot)" "$(metric_of "$dn" n)")" \
	"0.77520 (spin=155033/200000)"
compare_row "nudges that found the target already awake" \
	"$(frac5 "$(( $(metric_of "$hn" nudges) - $(metric_of "$hn" futex_waiters_woken) ))" "$(metric_of "$hn" nudges)")" \
	"$(frac5 "$(( $(metric_of "$dn" nudges) - $(metric_of "$dn" futex_waiters_woken) ))" "$(metric_of "$dn" nudges)")" \
	"0.55082 (wakes_skipped=110164 of 200000 wake attempts)"

note "comparison note: the harness models the mechanism.  It does not execute mldr or darlingserver, so"
note "comparison note: every figure above compares two mechanisms at the same cold fraction, never absolute costs."
note "comparison note: product baseline quoted from the ring-enabled prefix measurement: ring_serviced_delta=200000,"
note "comparison note: spin=155033 (77.52%), doorbell=44967 (doorbell_share 0.2248), wakes_issued=89836 (2 per"
note "comparison note: doorbell), wakes_skipped=110164, doorbell_per_request=0.22457, syscw_per_request=0.22644,"
note "comparison note: ctxt_vol_per_request=0.44839: one write and two voluntary context switches per doorbell."
note "comparison note: the harness's cold exchanges arrive in bursts (S3 histogram), so one eventfd write amortises"
note "comparison note: over the whole burst; a lone cold exchange (size-1 bucket) costs the same one write and the"
note "comparison note: same two voluntary context switches the product pays per doorbell."
note "comparison note: the /proc window covers exactly the exchanges: nothing inside it reads /proc or writes."

# --------------------------------------------------------------------------
# 6. the harness must be able to fail: three deliberate mutations of the
#    premise, built only in the temporary directory, with the expected
#    assertions or claims failing in both environments.
# --------------------------------------------------------------------------
mut_clone_files="$work/mut_clone_files.c"
mut_late_arm="$work/mut_late_arm.c"
mut_no_mark="$work/mut_no_mark.c"

sed 's@^#define CLONE_VM_ONLY 0x00000100UL.*@#define CLONE_VM_ONLY (0x00000100UL | 0x00000400UL) /* MUTATED: adds CLONE_FILES */@' \
	"$src" >"$mut_clone_files"
grep -q '0x00000400UL' "$mut_clone_files" ||
	fail "mutation 1 (CLONE_FILES) did not apply to the source"

sed -e 's@/\*MUT2-ARM\*/ epoch = ARM_EPOCH(s);@epoch = 0; (void)epoch; /* MUTATED: sampled late */@' \
	-e 's@(long)epoch@(long)ARM_EPOCH(s)@g' \
	"$src" >"$mut_late_arm"
grep -q 'MUTATED: sampled late' "$mut_late_arm" ||
	fail "mutation 2 (late epoch sample) did not apply to the source"
if grep -q '(long)epoch' "$mut_late_arm"; then
	fail "mutation 2 left an early epoch sample behind"
fi

"$cc" "${cflags[@]}" -o "$work/mut_clone_files" "$mut_clone_files"
"$cc" "${cflags[@]}" -o "$work/mut_late_arm" "$mut_late_arm"

# mutation 3: the sustained-load cold phase publishes its burst but never marks
# the lanes pending, so no relay wake can deliver them and S1 must fail.
sed 's@^\([[:space:]]*\)mark_pending((int)k);[[:space:]]*/\*MUT3-MARK\*/@\1/* MUTATED: the cold phase skips the pending mark */@' \
	"$src" >"$mut_no_mark"
grep -q 'MUTATED: the cold phase skips the pending mark' "$mut_no_mark" ||
	fail "mutation 3 (cold phase skips the pending mark) did not apply to the source"
if grep -q 'MUT3-MARK' "$mut_no_mark"; then
	fail "mutation 3 left the pending mark in place"
fi
"$cc" "${cflags[@]}" -o "$work/mut_no_mark" "$mut_no_mark"

expect_failure() {
	local label="$1"
	shift
	local wants=()
	while [ "$1" != "--" ]; do
		wants+=("$1")
		shift
	done
	shift

	local log="$work/$label.log"
	local rc=0 want

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e
	printf 'MUTATION %s: exit=%s\n' "$label" "$rc"
	sed 's/^/MUTATION /' "$log" || true

	[ "$rc" != "0" ] || fail "$label: mutated harness still exited 0"
	for want in "${wants[@]}"; do
		if ! grep -qE "^$want FAIL " "$log"; then
			fail "$label: expected $want to fail"
		fi
	done
	note "$label: ${wants[*]} failed as designed"
}

expect_failure mut-clone-files A1 A2 -- "$work/mut_clone_files" all
expect_failure mut-late-arm A4 -- "$work/mut_late_arm" all
expect_failure docker-mut-clone-files A1 A2 -- \
	docker "${docker_args[@]}" "$image" /proof/mut_clone_files all
expect_failure docker-mut-late-arm A4 -- \
	docker "${docker_args[@]}" "$image" /proof/mut_late_arm all

expect_failure mut-no-mark S1 -- sh "$work/sustain-sampler.sh" host \
	"$work/mut_no_mark" sustain "$sustain_n" "$sustain_cold"
expect_failure docker-mut-no-mark S1 -- docker "${docker_args[@]}" "$image" \
	/bin/sh /proof/sustain-sampler.sh docker /proof/mut_no_mark \
	sustain "$sustain_n" "$sustain_cold"

printf 'RELAY_TOPOLOGY_OK\n'

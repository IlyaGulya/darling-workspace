#!/usr/bin/env bash
# darling-fd-slope.sh -- measure the transport descriptor cost against the number of LIVE guest threads.
#
# WHY THIS EXISTS. The Ring-default goal claims transport FD count is O(1) in thread count: the per-thread AF_UNIX
# endpoint is gone, so a thread must not bring a descriptor with it. A single count proves nothing -- the claim is a
# SLOPE, so this holds N threads live inside one guest process and samples the open descriptors of BOTH sides while
# they are live, classifying each one. The holder MUST vary the number of LIVE guest threads. `r2 <N>` does NOT do
# that: its first argument is main-thread ROUND TRIPS while exactly one worker is parked (measured: iters=1 and
# iters=32 both give parked_elapsed ~3.0 s, from the hard-coded 3000 ms sender). A slope taken with r2 is a slope
# against TRAFFIC, not against threads, and it reports 0 for the trivial reason that the thread count never changed.
# The default holder is therefore `stress_pool <N> <iters>` (S1: persistent receiver/sender PAIRS, fixed live thread
# count, no churn). Pass --holder r2 only when the traffic dimension is what you mean to measure.
#
# Usage: darling-fd-slope.sh --prefix PATH --threads 1,8,16,32 [--hold-seconds 30] [--holder stress_pool]
#        [--holder-iters 400] [--harness scripts/darling-boot-run.sh]
#
# The harness owns prefix start/stop, the run log and the counters; this only samples, and it says which side each
# number came from. A descriptor is classified by readlink: socket: (AF_UNIX/anonymous), anon_inode:eventfd, pipe,
# a named file, or other. Descriptors that vanish mid-sample are reported as such, never as zero.

set -u

PREFIX=""; THREADS="1,8,16,32"; HOLD=30; HARNESS="scripts/darling-boot-run.sh"
HOLDER="stress_pool"; HOLDER_ITERS=400
while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--threads) THREADS="$2"; shift 2 ;;
		--hold-seconds) HOLD="$2"; shift 2 ;;
		--holder) HOLDER="$2"; shift 2 ;;
		--holder-iters) HOLDER_ITERS="$2"; shift 2 ;;
		--harness) HARNESS="$2"; shift 2 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[ -n "$PREFIX" ] || { echo "--prefix is required" >&2; exit 2; }

classify() { # $1 = /proc/<pid>/fd/N
	local target
	target=$(readlink "$1" 2>/dev/null) || { echo "vanished"; return; }
	case "$target" in
		socket:*) echo "socket(AF_UNIX/anon)" ;;
		anon_inode:eventfd*) echo "eventfd" ;;
		anon_inode:*) echo "anon_inode(other)" ;;
		pipe:*) echo "pipe" ;;
		*) echo "file" ;;
	esac
}

count_side() { # $1 = label, $2 = pid
	local label="$1" pid="$2" total=0 line cls
	declare -A byclass=()
	for fd in /proc/"$pid"/fd/*; do
		[ -e "$fd" ] || continue
		cls=$(classify "$fd")
		total=$((total + 1))
		byclass["$cls"]=$(( ${byclass["$cls"]:-0} + 1 ))
	done
	line=""
	for cls in "${!byclass[@]}"; do line="$line $cls=${byclass[$cls]}"; done
	echo "FD-SAMPLE side=$label pid=$pid total=$total$line"
}

# The server is found by its EXE (a guest cmdline carries the in-guest path, the rule this project paid for once).
find_server() { local p; for p in /proc/[0-9]*; do [ "$(readlink "$p/exe" 2>/dev/null)" = "$PREFIX/bin/darlingserver" ] && { echo "${p#/proc/}"; return; }; done; }
find_guest() {
	# exe AND cmdline. MEASURED: an exe-only match found nothing on any size (guest_peak=0) -- the holder's exe is not
	# the workload binary in this setup, while its cmdline names it. This project's own rule for identifying a process
	# is exactly this pair, because either half alone has produced a wrong answer before.
	local p
	for p in /proc/[0-9]*; do
		case "$(readlink "$p/exe" 2>/dev/null)" in *ring_mach_msg_test*) echo "${p#/proc/}"; return;; esac
		case "$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)" in *ring_mach_msg_test*) echo "${p#/proc/}"; return;; esac
	done
}


for n in ${THREADS//,/ }; do
	LOG="/tmp/darling-fd-slope-${n}.txt"
		# The HOLDER's OWN line, not FINAL=1: a custom --cmd never prints the boot markers, so waiting for one guarantees a
	# timeout and a sample taken after the processes are gone (MEASURED: guest_peak=0, server total=0, VERDICT FAIL on
	# all four sizes). The holder announces itself with its OWN machine-readable line while its threads are live,
	# which is the window to sample; `stress_pool <N> <iters>` is used for that reason (see the header for why
	# `r2 <N>` cannot serve as the thread-count holder).
		# THE PROVEN INVOCATION (directive section 14). `--cmd "shell -c ..."` was measured never to run the holder at all
	# (guest_peak=0 on every size, VERDICT FAIL x4) and a custom --cmd prints none of the workload's own lines, so the
	# wait could only time out. The tool owns prefix start/stop, hands the mode and its args to the guest workload and
	# judges that workload's OWN line.
	scripts/dwdiag verdict --prefix "$PREFIX" --mode "$HOLDER" --args "$n $HOLDER_ITERS" --wait "$HOLD" >"$LOG" 2>&1 &
	hz=$!
	server=""; guest=""; peak_server=0; peak_guest=0; samples=0
	while kill -0 "$hz" 2>/dev/null; do
		[ -z "$server" ] && server=$(find_server)
		[ -z "$guest" ] && guest=$(find_guest)
		if [ -n "$server" ]; then t=$(ls /proc/"$server"/fd 2>/dev/null | wc -l); [ "$t" -gt "$peak_server" ] && peak_server=$t; fi
		if [ -n "$guest" ]; then g=$(ls /proc/"$guest"/fd 2>/dev/null | wc -l); [ "$g" -gt "$peak_guest" ] && peak_guest=$g; fi
		samples=$((samples + 1))
		sleep 0.2
	done
	wait "$hz" || true
	echo "FD-SLOPE threads=$n server_peak=$peak_server guest_peak=$peak_guest samples=$samples"
	[ -n "$server" ] && count_side "server" "$server"
	[ -n "$guest" ] && count_side "guest" "$guest"
	tail -3 "$LOG"
done

echo "FD-SLOPE-DONE the slope is (peak at the largest N - peak at the smallest N) / (largest N - smallest N);"
echo "FD-SLOPE-DONE a per-thread AF_UNIX endpoint would show as +1 guest socket per thread."

#!/usr/bin/env bash
# poll-vs-event CPU comparison (directive section 3A / section 6 second half).
# The 2 ms management poll is a two-line difference, so the historical comparison IS available cheaply -- but it must
# not leave the deployed prefix on the old build, hence the restore trap: whatever happens, the event-driven build is
# rebuilt and redeployed before this script exits.
set -u
SRC=/home/ilyagulya/work/procctl-src/src/external/darlingserver/src/server.cpp
BUILD=/home/ilyagulya/work/ringmm-build
PREFIX=/tmp/dr-on-matched
WS=/home/ilyagulya/work/darling-gwn-resume/darling-workspace
BLOCKING='_stallDumpEnabled ? 500 : -1'
POLLING='_stallDumpEnabled ? 500 : 2'

sample_cpu() { # $1 = label; samples the server CPU across one verdict run
	local label="$1"
	# The verdict runs in the BACKGROUND with its output in a file: capturing a background command's output through a
	# command substitution is a shell error (MEASURED: `out: unbound variable`, and the second arm never ran).
	local vlog="/tmp/poll-vs-event-$label.log"
	"$WS/scripts/dwdiag" verdict --prefix "$PREFIX" --mode sem_ready --args 2 --wait 150 --repeat 1 >"$vlog" 2>&1 &
	local verdict_pid=$!
	local p="" i
	for i in $(seq 1 600); do p=$(pgrep -x darlingserver | head -1); [ -n "$p" ] && break; sleep 0.1; done
	local t0="" t1=""
	if [ -n "$p" ]; then
		read -r _ _ _ _ _ _ _ _ _ _ _ _ _ U1 S1 _ < /proc/$p/stat
		sleep 10
		read -r _ _ _ _ _ _ _ _ _ _ _ _ _ U2 S2 _ < /proc/$p/stat
		echo "CPU[$label] pid=$p ticks=$(( (U2+S2)-(U1+S1) )) over 10s"
	fi
	wait $verdict_pid
	echo "VERDICT[$label] $(tail -2 "$vlog" | head -1)"
}

restore() {
	echo "RESTORE: rebuilding and redeploying the event-driven build"
	sed -i "s/$POLLING/$BLOCKING/" "$SRC"
	( cd "$BUILD" && ninja darlingserver >/dev/null 2>&1 ) && install -m 0755 "$BUILD/src/external/darlingserver/darlingserver" "$PREFIX/bin/darlingserver"
	echo "RESTORE done: $(sha256sum "$PREFIX/bin/darlingserver" | cut -c1-16) (poll text present: $(grep -c "$POLLING" "$SRC"))"
}
trap restore EXIT

echo "=== ARM A: 2 ms poll (historical) ==="
sed -i "s/$BLOCKING/$POLLING/" "$SRC"
grep -c "$POLLING" "$SRC"
( cd "$BUILD" && ninja darlingserver >/dev/null 2>&1 ) || { echo "build failed"; exit 1; }
install -m 0755 "$BUILD/src/external/darlingserver/darlingserver" "$PREFIX/bin/darlingserver"
sample_cpu poll-2ms

echo "=== ARM B: event-driven ==="
sed -i "s/$POLLING/$BLOCKING/" "$SRC"
( cd "$BUILD" && ninja darlingserver >/dev/null 2>&1 ) || { echo "build failed"; exit 1; }
install -m 0755 "$BUILD/src/external/darlingserver/darlingserver" "$PREFIX/bin/darlingserver"
sample_cpu event-driven

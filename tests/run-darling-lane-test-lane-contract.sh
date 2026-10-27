#!/usr/bin/env bash
# run-darling-lane-test-lane-contract.sh -- `west test --lane NAME`, two lanes at once.
#
# WHY THIS EXISTS. The two-guest concurrency contract proves the RUNTIME isolation with
# `scripts/darling-boot-run.sh`. This one drives the interface an agent actually types: the existing
# `west test` runner, twice, in two different lanes, at the same time. What it verifies is what the
# interface has to get right and a direct workload does not exercise:
#
#   1. distinct prefixes, state roots, build directories and cgroup units per lane;
#   2. distinct source identities when the lanes were created with different `--source` checkouts;
#   3. independent logs and receipts: every run writes inside its own lane, and neither lane's tree
#      names the other lane's path;
#   4. no cross-lane interference: the lanes hold live processes at the same time and every run
#      reaches its own exit code;
#   5. EXACT real tool exit codes, captured per lane, not merely "non-zero on failure";
#   6. the base is unchanged by both runs, by content (sha256 baseline) and by tree identity.
#
# Checks counted: the number of checks that actually ran is compared against a pinned literal, so a
# phase that silently did not execute fails this contract instead of passing it.
#
# THE OPTIONAL GUEST PHASE (`--guest-workload NAME`). The default mode drives the runner's
# prefix lifecycle concurrently. A named flat workload additionally proves real guest verdicts
# and retained evidence through the same selected installation, without source materialization.
# `rootless_shutdown_active_guest` preserves the active-session shutdown oracle. Other workloads
# require their own matching --label and --guest-marker, plus their declared installed prerequisites.
#
# Usage:
#   run-darling-lane-test-lane-contract.sh --runtime-root ROOT --base PREFIX [--root DIR]
#       [--source-a DIR] [--source-b DIR] [--iterations N]
#       [--guest-workload NAME] [--label LABEL] [--guest-marker MARKER] [--keep]
#
# Run it through the job owner:
#   scripts/west-job.sh start --state-dir "$DW_STATE_ROOT/jobs/lane-interface" -- \
#       tests/run-darling-lane-test-lane-contract.sh --runtime-root /abs/install --base /abs/guest-state --keep
#   scripts/west-job.sh follow --state-dir "$DW_STATE_ROOT/jobs/lane-interface"
#
# Exit: 0 only when every check passed AND the pinned number of checks ran; 2 on a usage error or a
# missing prerequisite.
# Last line: LANE-TEST-LANE: PASS or FAIL.

set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BASE=""
RUNTIME_ROOT=""
ROOT=""
SOURCE_A=""
SOURCE_B=""
GUEST_WORKLOAD=""
LABEL="name:rootless_shutdown_active_guest"
GUEST_MARKER="WEST_ROOTLESS_SHUTDOWN_SESSION_OK"
ITERATIONS=12
KEEP=0
failures=0
checks_run=0

# Pinned: a phase that silently does not run must fail this contract, not pass it. Two numbers, one
# per mode, because the optional phase is a decision the caller makes and both totals are literals.
EXPECTED_CHECKS=33
EXPECTED_CHECKS_GUEST=46

# By marker, not by line number: the block above is documentation and moves.
usage() { sed -n '/^# Usage:/,/^# Last line:/p' "$0"; exit 2; }

while [ $# -gt 0 ]; do
	case "$1" in
		--base) BASE=$2; shift 2 ;;
		--runtime-root) RUNTIME_ROOT=$2; shift 2 ;;
		--root) ROOT=$2; shift 2 ;;
		--source-a) SOURCE_A=$2; shift 2 ;;
		--source-b) SOURCE_B=$2; shift 2 ;;
		--guest-workload) GUEST_WORKLOAD=$2; shift 2 ;;
		--guest-marker) GUEST_MARKER=$2; shift 2 ;;
		--label) LABEL=$2; shift 2 ;;
		--iterations) ITERATIONS=$2; shift 2 ;;
		--keep) KEEP=1; shift ;;
		-h|--help) usage ;;
		*) echo "unknown argument: $1" >&2; usage ;;
	esac
done

[ -n "$BASE" ] || usage
case "$ITERATIONS" in
	''|*[!0-9]*) echo "--iterations must be a positive integer" >&2; exit 2 ;;
esac
[ "$ITERATIONS" -ge 1 ] && [ "$ITERATIONS" -le 200 ] ||
	{ echo "--iterations must be between 1 and 200" >&2; exit 2; }
DARLING_WORKSPACE_ROOT=$REPO
. "$REPO/scripts/darling-runtime.sh"
darling_resolve_runtime "$RUNTIME_ROOT" "$BASE" || exit $?
RUNTIME_ROOT=$DARLING_RUNTIME_ROOT
BASE=$(cd "$BASE" 2>/dev/null && pwd) || { echo "base prefix '$BASE' is not a directory" >&2; exit 2; }
for tool in fuse-overlayfs fusermount3 systemctl systemd-run; do
	command -v "$tool" >/dev/null || { echo "$tool is required and was not found" >&2; exit 2; }
done
[ -e /dev/fuse ] || { echo "/dev/fuse is required and is absent" >&2; exit 2; }

MISE=$(command -v mise || true)
if [ -z "$MISE" ] && [ -x "$HOME/.local/bin/mise" ]; then MISE="$HOME/.local/bin/mise"; fi
[ -n "$MISE" ] || { echo "mise is required and was not found on PATH or in ~/.local/bin" >&2; exit 2; }


if [ -z "$ROOT" ]; then
	STATE_ROOT="${DW_STATE_ROOT:-${REPO%/*}/.west-state}/lane-contracts"
	mkdir -p "$STATE_ROOT" || exit 2
	ROOT=$(mktemp -d "$STATE_ROOT/test-lane-XXXXXX") || exit 2
fi
python3 -B -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/west_commands")
from runtime_install import resolve_runtime
resolve_runtime(sys.argv[2]).environment(sys.argv[3])
root = Path(sys.argv[3])
if root.exists() and any(root.iterdir()):
    raise SystemExit("lane contract --root must be absent or empty")
' "$REPO" "$RUNTIME_ROOT" "$ROOT" || exit 2
mkdir -p "$ROOT" || exit 2
export DW_LANE_ROOT="$ROOT"
export WEST_TEST_VERDICT_CACHE=off
LANE_A="$ROOT/lane-a"
LANE_B="$ROOT/lane-b"

note() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
require() {
	local detail=$1
	shift
	checks_run=$((checks_run + 1))
	if "$@"; then
		printf 'PASS-CHECK  %s\n' "$detail"
	else
		printf 'FAIL-CHECK  %s\n' "$detail" >&2
		failures=$((failures + 1))
	fi
}

west() { ( cd "$REPO" && "$MISE" -C . run west -- "$@" ); }

clone_is_bound() {
	python3 -B - "$BASE" "$1" <<'PY'
import os
import sys
from pathlib import Path
def state(prefix):
    lines = (prefix / ".darling-prefix-state-v2").read_text().splitlines()
    assert lines[0] == "DARLING_PREFIX_STATE_V2"
    return dict(line.split("=", 1) for line in lines[1:])
base, clone = map(Path, sys.argv[1:])
original, copied = state(base), state(clone)
metadata = clone.stat()
assert int(copied["prefix_device"]) == metadata.st_dev
assert int(copied["prefix_inode"]) == metadata.st_ino
assert int(copied["owner_uid"]) == os.getuid()
assert int(copied["owner_gid"]) == os.getgid()
assert int(copied["generation"]) == int(original["generation"]) + 1
PY
}

verification_passed() {
	[ "$2" -eq 0 ] && grep -q "$3" "$1"
}

no_foreign_reference() {
	python3 -B - "$REPO" "$1" "$2" <<'PY'
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/west_commands")
from evidence_storage import open_payload
root = Path(sys.argv[2])
needle = os.fsencode(sys.argv[3])
for path in root.rglob("*"):
    if path.is_symlink():
        assert needle not in os.fsencode(os.readlink(path)), path
    elif path.is_file():
        with open_payload(path) as source:
            carry = b""
            while chunk := source.read(1024 * 1024):
                data = carry + chunk
                assert needle not in data, path
                carry = data[-(len(needle) - 1):]
PY
}

has_guest_bundle() {
	python3 -B -c '
import sys
from pathlib import Path
root = Path(sys.argv[1]) / "state" / "bundles"
assert root.is_dir()
assert any(path.is_file() and not path.is_symlink() and path.stat().st_size for path in root.rglob("*"))
' "$1"
}

# One field of one lane's status, straight from the tool that owns the layout.
lane_field() { # lane-name key
	west dev lane status "$1" --root "$ROOT" --json 2>/dev/null |
		python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["status"][sys.argv[1]])
except Exception:
    print("")' "$2"
}

# The PIDs the lane's own cgroup unit holds: liveness is read from the ownership handle.
scope_pids() { # unit reference
	local cg
	cg=$(systemctl --user show --property=ControlGroup --value "$1" 2>/dev/null)
	[ -n "$cg" ] || return 0
	cat "/sys/fs/cgroup/${cg#/}/cgroup.procs" 2>/dev/null
}

# The lane's own fuse daemon count, read from the tool that owns the identity, for a lane directory
# that may already be gone. `pgrep -f` cannot answer this: the `bash -c` running it carries the
# pattern in its own argv and matches itself.
lane_daemons() { # lane directory
	python3 -B -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2] + "/west_commands")
from lane import fuse_daemon_pids
print(len(fuse_daemon_pids(Path(sys.argv[1]))))
' "$1" "$REPO"
}

# One run of `west test --lane`, with its console and its exit code kept inside that lane.
run_west_test() { # lane-name phase args...
	local lane=$1 phase=$2
	shift 2
	( cd "$REPO" && "$MISE" -C . run west -- test --lane "$lane" --runtime-root "$RUNTIME_ROOT" \
		--runtime-evidence-root "$ROOT/$lane/state/runtime-evidence" "$@" ) \
		>"$ROOT/$lane/logs/west-test-$phase.log" 2>&1
	local rc=$?
	echo "$rc" >"$ROOT/$lane/logs/west-test-$phase.rc"
	return "$rc"
}

# N sequential runs in one lane, appended to one log per lane. Sequential on purpose: what must overlap
# is the two LANES, so each lane must be a single stream of runs.
loop_west_test() { # lane iterations log
	local lane=$1 n=$2 log=$3 i=0
	while [ "$i" -lt "$n" ]; do
		i=$((i + 1))
		( cd "$REPO" && "$MISE" -C . run west -- test --lane "$lane" --runtime-root "$RUNTIME_ROOT" \
			--runtime-evidence-root "$ROOT/$lane/state/runtime-evidence" --cleanup-prefix ) \
			>>"$log" 2>&1 || return 1
	done
}

finalize() {
	local exit_status=$?
	local lane
	if [ "$exit_status" -eq 0 ] && [ "$failures" -eq 0 ] && [ "$KEEP" -eq 0 ]; then
		for lane in lane-a lane-b; do
			west dev lane destroy "$lane" --root "$ROOT" >/dev/null 2>&1
		done
		rm -rf "$ROOT"
		return
	fi
	note "lane root kept for inspection: $ROOT"
	note "remove it with: west dev lane destroy lane-a --root $ROOT; west dev lane destroy lane-b --root $ROOT; rm -rf $ROOT"
}
trap finalize EXIT

note "base prefix:  $BASE"
note "lane root:    $ROOT"
note "runtime:      $RUNTIME_ROOT"
note "mode:         $([ -n "$GUEST_WORKLOAD" ] && echo "interface + guest workload $GUEST_WORKLOAD" || echo interface)"

create_args_a=(--base "$BASE" --runtime-root "$RUNTIME_ROOT" --root "$ROOT")
create_args_b=(--base "$BASE" --runtime-root "$RUNTIME_ROOT" --root "$ROOT")
[ -n "$SOURCE_A" ] && create_args_a+=(--source "$SOURCE_A")
[ -n "$SOURCE_B" ] && create_args_b+=(--source "$SOURCE_B")
west dev lane create lane-a "${create_args_a[@]}" >"$ROOT/create-a.log" 2>&1 ||
	{ warn "cannot create lane A: $(cat "$ROOT/create-a.log")"; exit 1; }
west dev lane create lane-b "${create_args_b[@]}" >"$ROOT/create-b.log" 2>&1 ||
	{ warn "cannot create lane B: $(cat "$ROOT/create-b.log")"; exit 1; }

note ""
note "== identity: what makes these two lanes two lanes =="
PREFIX_A=$(lane_field lane-a prefix)
PREFIX_B=$(lane_field lane-b prefix)
STATE_A=$(lane_field lane-a state_root)
STATE_B=$(lane_field lane-b state_root)
UNIT_A=$(lane_field lane-a scope_unit)
UNIT_B=$(lane_field lane-b scope_unit)
require "lane A has a product-bound clone identity and generation" clone_is_bound "$PREFIX_A"
require "lane B has a product-bound clone identity and generation" clone_is_bound "$PREFIX_B"
require "the two prefixes are different directories" bash -c "[ '$PREFIX_A' != '$PREFIX_B' ]"
require "the two state roots are different directories" bash -c "[ -n '$STATE_A' ] && [ '$STATE_A' != '$STATE_B' ]"
require "the two lane build directories are different" bash -c "[ '$LANE_A/build' != '$LANE_B/build' ]"
require "the two lanes own different cgroup units" bash -c "[ -n '$UNIT_A' ] && [ '$UNIT_A' != '$UNIT_B' ]"

path_a=$(python3 -c 'import json,sys
r = json.load(open(sys.argv[1])).get("source") or {}
print(r.get("path") or "")' "$LANE_A/lane.json")
path_b=$(python3 -c 'import json,sys
r = json.load(open(sys.argv[1])).get("source") or {}
print(r.get("path") or "")' "$LANE_B/lane.json")
expect_a=""; expect_b=""
[ -n "$SOURCE_A" ] && expect_a=$(cd "$SOURCE_A" && pwd)
[ -n "$SOURCE_B" ] && expect_b=$(cd "$SOURCE_B" && pwd)
require "lane A's recorded source is its own checkout (or none was recorded)" test "$path_a" = "$expect_a"
require "lane B's recorded source is its own checkout (or none was recorded)" test "$path_b" = "$expect_b"
if [ -n "$expect_a" ] && [ -n "$expect_b" ]; then
	head_a=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["manifest_head"])' "$LANE_A/lane.json")
	head_b=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["manifest_head"])' "$LANE_B/lane.json")
	require "the two lanes record different source revisions" bash -c "[ '$head_a' != '$head_b' ]"
else
	require "neither lane invented a source it was not given" bash -c "[ -z '$path_a' ] && [ -z '$path_b' ]"
fi
# The inner shell is a file, not a nested-quoted string: two levels of quoting in one argument is how
# this check once died with "cd: $REPO: No such file or directory" instead of reporting.
cat >"$ROOT/lane-a/logs/source-env.sh" <<'INNER'
printf '%s\n' "$DW_LANE_SOURCE"
INNER
got=$( cd "$REPO" && "$MISE" -C . run west -- dev lane exec --root "$ROOT" lane-a -- \
	sh "$ROOT/lane-a/logs/source-env.sh" 2>/dev/null | tail -1 )
require "DW_LANE_SOURCE handed to a lane command is the recorded path" test "$got" = "$expect_a"
if [ "$failures" -ne 0 ]; then
	warn "LANE-TEST-LANE: FAIL (identity admission)"
	exit 1
fi

note ""
note "== phase 1: both lanes drive the runner's prefix lifecycle =="
run_west_test lane-a lifecycle --cleanup-prefix &
pid_a=$!
run_west_test lane-b lifecycle --cleanup-prefix &
pid_b=$!
wait "$pid_a"; rc_a=$?
wait "$pid_b"; rc_b=$?
require "lane A's lifecycle run exited with exactly 0" test "$rc_a" -eq 0
require "lane B's lifecycle run exited with exactly 0" test "$rc_b" -eq 0
require "lane A's console names lane A's own prefix" grep -qF "$PREFIX_A" "$LANE_A/logs/west-test-lifecycle.log"
require "lane A's console never names lane B's prefix" bash -c "! grep -qF '$PREFIX_B' '$LANE_A/logs/west-test-lifecycle.log'"
require "lane B's console names lane B's own prefix" grep -qF "$PREFIX_B" "$LANE_B/logs/west-test-lifecycle.log"
require "lane B's console never names lane A's prefix" bash -c "! grep -qF '$PREFIX_A' '$LANE_B/logs/west-test-lifecycle.log'"
require "lane A's guest-state layer survived the runner's cleanup" test -f "$LANE_A/merged/.darling-prefix-state-v2"
require "lane B's guest-state layer survived the runner's cleanup" test -f "$LANE_B/merged/.darling-prefix-state-v2"
if [ "$failures" -ne 0 ]; then
	warn "LANE-TEST-LANE: FAIL (initial lifecycle); refusing repeated cleanup after failure"
	exit 1
fi

note ""
note "== phase 2: $ITERATIONS runner runs per lane, both lanes live at once =="
loop_west_test lane-a "$ITERATIONS" "$LANE_A/logs/west-test-loop.log" &
pid_a=$!
loop_west_test lane-b "$ITERATIONS" "$LANE_B/logs/west-test-loop.log" &
pid_b=$!
# Sampled, not read once: a lifecycle run's scope exists only while the run does, so a single read
# would race. The failure this check must catch is "the two lanes never overlapped", so it fails only
# when no sample saw both scopes non-empty for the whole phase.
overlap=0
while :; do
	[ -n "$(scope_pids "$UNIT_A")" ] && [ -n "$(scope_pids "$UNIT_B")" ] && { overlap=1; break; }
	kill -0 "$pid_a" 2>/dev/null || kill -0 "$pid_b" 2>/dev/null || break
	sleep 0.2
done
wait "$pid_a"; rc_a=$?
wait "$pid_b"; rc_b=$?
require "lane A ran every iteration with exit 0" test "$rc_a" -eq 0
require "lane B ran every iteration with exit 0" test "$rc_b" -eq 0
require "both lanes held live processes at the same time" test "$overlap" -eq 1
require "lane A's loop console names lane A's own prefix" grep -qF "$PREFIX_A" "$LANE_A/logs/west-test-loop.log"
require "lane A's loop console never names lane B's prefix" bash -c "! grep -qF '$PREFIX_B' '$LANE_A/logs/west-test-loop.log'"
require "lane B's loop console names lane B's own prefix" grep -qF "$PREFIX_B" "$LANE_B/logs/west-test-loop.log"
require "lane B's loop console never names lane A's prefix" bash -c "! grep -qF '$PREFIX_A' '$LANE_B/logs/west-test-loop.log'"
if [ "$failures" -ne 0 ]; then
	warn "LANE-TEST-LANE: FAIL (repeated lifecycle)"
	exit 1
fi

if [ -n "$GUEST_WORKLOAD" ]; then
	note ""
	note "== phase 2b: two named guest workload selections against one installed runtime =="
	run_west_test lane-a guest --workload "$GUEST_WORKLOAD" --label "$LABEL" --bundle-root "$LANE_A/state/bundles" &
	pid_a=$!
	for _ in $(seq 1 300); do
		[ -n "$(scope_pids "$UNIT_A")" ] && break
		kill -0 "$pid_a" 2>/dev/null || break
		sleep 1
	done
	run_west_test lane-b guest --workload "$GUEST_WORKLOAD" --label "$LABEL" --bundle-root "$LANE_B/state/bundles" &
	pid_b=$!
	for _ in $(seq 1 300); do
		[ -n "$(scope_pids "$UNIT_B")" ] && break
		kill -0 "$pid_b" 2>/dev/null || break
		sleep 1
	done
	require "lane A's guest run is live in its own cgroup" test -n "$(scope_pids "$UNIT_A")"
	require "lane B's guest run is live in its own cgroup" test -n "$(scope_pids "$UNIT_B")"
	wait "$pid_a"; rc_a=$?
	wait "$pid_b"; rc_b=$?
	require "lane A's guest run exited with exactly 0" test "$rc_a" -eq 0
	require "lane B's guest run exited with exactly 0" test "$rc_b" -eq 0
	require "lane A's guest run reached its own verdict marker" grep -qF "$GUEST_MARKER" "$LANE_A/logs/west-test-guest.log"
	require "lane B's guest run reached its own verdict marker" grep -qF "$GUEST_MARKER" "$LANE_B/logs/west-test-guest.log"
	require "lane A's run retained actual bundle files inside lane A" has_guest_bundle "$LANE_A"
	require "lane B's run retained actual bundle files inside lane B" has_guest_bundle "$LANE_B"
	require "lane A's raw/zstd evidence does not name lane B" no_foreign_reference "$LANE_A/state" "$LANE_B"
	require "lane B's raw/zstd evidence does not name lane A" no_foreign_reference "$LANE_B/state" "$LANE_A"
	require "lane A's writable layer does not name lane B" no_foreign_reference "$LANE_A/upper" "$LANE_B/merged"
	require "lane B's writable layer does not name lane A" no_foreign_reference "$LANE_B/upper" "$LANE_A/merged"
	require "neither guest selection copied a runtime installation into guest state" \
		bash -c '! test -e "$1/bin/darling" && ! test -e "$2/bin/darling" && ! test -e "$1/libexec/darling" && ! test -e "$2/libexec/darling"' _ "$PREFIX_A" "$PREFIX_B"
	EXPECTED_CHECKS=$EXPECTED_CHECKS_GUEST
fi

note ""
note "== phase 3: the base, and the lane root, after both runs =="
west dev lane verify lane-a --root "$ROOT" >"$ROOT/verify-a.log" 2>&1
verify_a_rc=$?
west dev lane verify lane-b --root "$ROOT" >"$ROOT/verify-b.log" 2>&1
verify_b_rc=$?
require "lane A reports the installed runtime's content baseline intact" verification_passed "$ROOT/verify-a.log" "$verify_a_rc" 'content matches its sha256 baseline'
require "lane B reports the same installed runtime's content baseline intact" verification_passed "$ROOT/verify-b.log" "$verify_b_rc" 'content matches its sha256 baseline'
require "lane A reports the base's tree identity intact" verification_passed "$ROOT/verify-a.log" "$verify_a_rc" 'tree identity is unchanged'
require "lane B reports the base's tree identity intact" verification_passed "$ROOT/verify-b.log" "$verify_b_rc" 'tree identity is unchanged'

mkdir -p "$ROOT/evidence/lane-a" "$ROOT/evidence/lane-b"
cp -a "$LANE_A/logs" "$LANE_A/receipts" "$LANE_A/lane.json" "$LANE_A/state" "$ROOT/evidence/lane-a/" || exit 1
cp -a "$LANE_B/logs" "$LANE_B/receipts" "$LANE_B/lane.json" "$LANE_B/state" "$ROOT/evidence/lane-b/" || exit 1
west dev lane destroy lane-a --root "$ROOT" >"$ROOT/destroy-a.log" 2>&1
west dev lane destroy lane-b --root "$ROOT" >"$ROOT/destroy-b.log" 2>&1
require "lane A was removed" bash -c "! test -e '$LANE_A'"
require "lane B was removed" bash -c "! test -e '$LANE_B'"
require "no lane mount is left" bash -c "! mount | grep -qF '$ROOT'"
require "no lane fuse daemon is left" test "$(lane_daemons "$LANE_A")$(lane_daemons "$LANE_B")" = "00"

note "CHECKS-RUN ran=$checks_run pinned=$EXPECTED_CHECKS"

if [ "$checks_run" -ne "$EXPECTED_CHECKS" ]; then
	warn "LANE-TEST-LANE: FAIL (ran $checks_run checks, expected $EXPECTED_CHECKS)"
	exit 1
fi
if [ "$failures" -eq 0 ]; then
	note "LANE-TEST-LANE: PASS"
	exit 0
fi
warn "LANE-TEST-LANE: FAIL ($failures check(s) failed)"
exit 1

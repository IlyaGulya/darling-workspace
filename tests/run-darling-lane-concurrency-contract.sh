#!/usr/bin/env bash
# run-darling-lane-concurrency-contract.sh -- two Darling experiments at once, in isolation lanes.
#
# WHY THIS EXISTS. "Two agents can work at the same time" is not a property of the lane tooling; it
# is a property of two real guest runtimes booting and running at the same time, each on its own
# prefix, while one of them is running code the other must not see and is then stopped mid-flight.
# This runs exactly that, on the snapshotter's rootless FUSE views of one frozen parent,
# checking the six properties below:
#
#   1. no cross-prefix contamination: A's writable guest payload is visible only in A, absent from
#      B's guest, B's prefix and the inert base;
#   2. no socket/courier collision: both boots reach their workload in the same window with zero
#      rpc-socket denials and zero courier misses in their own counters;
#   3. no writable runtime image: both lanes select the same installed launcher and the selected
#      installation's artifact hashes remain unchanged;
#   4. product shutdown of A does not affect B: A is shut down while B's guest is running;
#   5. cleanup is lane-scoped: A's lane layer and daemon survive the stop, the repository's own
#      prefix-scoped cleaner reports A's prefix clean, and it never names B;
#   6. retirement removes guest/storage state with no mount, daemon or process remaining, while
#      preserving the parent and persistent lease inode for safe same-name reuse.
#
# Usage:
#   run-darling-lane-concurrency-contract.sh --runtime-root ROOT --base PREFIX [--root DIR] [--wait SECONDS] [--keep]
#
# It boots two guest runtimes twice over, so run it under the job owner:
#   scripts/west-job.sh start --state-dir "$DW_STATE_ROOT/jobs/lane-acceptance" -- \
#       tests/run-darling-lane-concurrency-contract.sh --runtime-root /abs/install --base /abs/guest-state --keep
#   scripts/west-job.sh follow --state-dir "$DW_STATE_ROOT/jobs/lane-acceptance"
#
# Exit: 0 only when every check passed. The last line is LANE-CONCURRENCY: PASS or FAIL.

set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BASE=""
RUNTIME_ROOT=""
ROOT=""
WAIT=20
KEEP=0
failures=0
checks_run=0

# The count is PINNED, because a run that silently skips a check is indistinguishable from a run that
# passed them: this contract once printed a PASS while a whole phase's assertions never executed.
# Change this literal only with the check it counts.
EXPECTED_CHECKS=49

usage() { sed -n '2,32p' "$0"; exit 2; }

while [ $# -gt 0 ]; do
	case "$1" in
		--base) BASE=$2; shift 2 ;;
		--runtime-root) RUNTIME_ROOT=$2; shift 2 ;;
		--root) ROOT=$2; shift 2 ;;
		--wait) WAIT=$2; shift 2 ;;
		--keep) KEEP=1; shift ;;
		-h|--help) usage ;;
		*) echo "unknown argument: $1" >&2; usage ;;
	esac
done

[ -n "$BASE" ] || usage
DARLING_WORKSPACE_ROOT=$REPO
. "$REPO/scripts/darling-runtime.sh"
darling_resolve_runtime "$RUNTIME_ROOT" "$BASE" || exit $?
RUNTIME_ROOT=$DARLING_RUNTIME_ROOT
BASE=$(cd "$BASE" 2>/dev/null && pwd) || { echo "base prefix '$BASE' is not a directory" >&2; exit 2; }
case "$WAIT" in ''|*[!0-9]*) echo "--wait must be an integer of at least 5 seconds" >&2; exit 2 ;; esac
[ "$WAIT" -ge 5 ] || { echo "--wait must be at least 5 seconds" >&2; exit 2; }
[ -e "$BASE/.darling-prefix-state-v2" ] || {
	echo "base '$BASE' has no .darling-prefix-state-v2; it is not a bootstrapped rootless prefix" >&2
	exit 2
}

for tool in fusermount3 systemctl systemd-run; do
	command -v "$tool" >/dev/null || { echo "$tool is required and was not found" >&2; exit 2; }
done
[ -e /dev/fuse ] || { echo "/dev/fuse is required and is absent" >&2; exit 2; }

# A shell this project's configuration has not activated has no `mise` on PATH, and a job started by
# scripts/west-job.sh is exactly such a shell. MEASURED: without this the whole contract ran with
# rc=127 for every guest run and reported six failures that were all "command not found".
MISE=$(command -v mise || true)
if [ -z "$MISE" ] && [ -x "$HOME/.local/bin/mise" ]; then MISE="$HOME/.local/bin/mise"; fi
[ -n "$MISE" ] || { echo "mise is required and was not found on PATH or in ~/.local/bin" >&2; exit 2; }

if [ -z "$ROOT" ]; then
	STATE_ROOT="${DW_STATE_ROOT:-${REPO%/*}/.west-state}/lane-contracts"
	mkdir -p "$STATE_ROOT" || exit 2
	ROOT=$(mktemp -d "$STATE_ROOT/concurrency-XXXXXX") || exit 2
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

# The source lifecycle record is an actual guest-state artifact, not an installed launcher.
base_hashes() {
	sha256sum "$BASE/.darling-prefix-state-v2"
}

clone_is_bound() {
	python3 -B - "$ROOT/lane-parent/merged" "$1" <<'PY'
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

lane_uses_runtime() {
	python3 -B -c '
import json,sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text())["runtime"]
assert record["root"] == sys.argv[2]
assert record["launcher"] == sys.argv[3]
' "$1/lane.json" "$RUNTIME_ROOT" "$DARLING_LAUNCHER"
}

clean_guest_bundle() {
	python3 -B -c '
import json,sys
from pathlib import Path
cleanup = json.loads(Path(sys.argv[1]).read_text())
assert cleanup["state"] == "clean"
assert cleanup["shutdown_rc"] == 0
assert cleanup["processes"] == 0 and cleanup["mounts"] == 0
' "$1.bundle/cleanup.json"
}

guest_passed() {
	[ "$2" -eq 0 ] &&
	grep -q '^VERDICT: PASS' "$1.console" &&
	clean_guest_bundle "$1"
}

runtime_unchanged() {
	[ "$RUNTIME_LAUNCHER_BEFORE" = "$(sha256sum "$DARLING_LAUNCHER")" ] &&
	"$REPO/scripts/darling-artifact-manifest.sh" --runtime-root "$RUNTIME_ROOT" \
		--check "$ROOT/runtime-before.tsv"
}

# Identity of the whole base tree (path, size, mtime, inode, mode) without reading it: the same
# derivation `west dev lane verify` uses, so this contract and the tool cannot disagree.
base_tree_digest() {
	python3 -B -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2] + "/west_commands")
from lane import tree_identity
print(tree_identity(Path(sys.argv[1])))
' "$1" "$REPO"
}

# `pgrep -f <pattern>` cannot answer any of the questions below: the `bash -c` that runs it carries
# the pattern in its own argv, so it matches itself. These use the same identity the tooling uses.
# The lane's cgroup unit name is the tool's to decide (it carries a digest of the lane's own path,
# so two lane roots cannot claim one unit). Ask the tool for it instead of spelling it here.
lane_unit() {
	west dev lane status "$1" --root "$ROOT" --json 2>/dev/null |
		python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["status"]["scope_unit"])
except Exception:
    pass'
}

scope_processes() {  # unit reference
	local cg
	cg=$(systemctl --user show --property=ControlGroup --value "$1" 2>/dev/null)
	[ -n "$cg" ] || return 0
	cat "/sys/fs/cgroup/${cg#/}/cgroup.procs" 2>/dev/null
}

lane_count() {
	python3 -B -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2] + "/west_commands")
from lane import fuse_daemon_pids, lane_prefix, mount_targets
from test_prefix import prefix_process_ownership_snapshot
kind, target = sys.argv[1], Path(sys.argv[3])
if kind == "daemons":
    print(len(fuse_daemon_pids(target)))
elif kind == "mounted":
    print(len(mount_targets(lane_prefix(target))))
else:
    print(len(prefix_process_ownership_snapshot(lane_prefix(target))))
' "$1" "$REPO" "$2"
}

snapshotter_stopped() {
	python3 -B - "$REPO" "$ROOT" <<'PY'
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/west_commands")
from lane_snapshotter import SnapshotStore
store = SnapshotStore(Path(sys.argv[2]))
for unit in [store.unit, *(store.mount_unit(store.lane_root / name / "merged") for name in ("lane-a", "lane-b", "lane-parent"))]:
    result = subprocess.run(["systemctl", "--user", "show", "--property=ActiveState,MainPID", unit], capture_output=True, text=True)
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert properties.get("ActiveState") == "inactive", (unit, properties)
    assert properties.get("MainPID") == "0", (unit, properties)
assert not store.socket.exists()
PY
}

# Persist identities outside guest data; destruction must not split an existing lock domain.
lane_coordination() { # capture | retired NAME | stable NAME
	python3 -B - "$REPO" "$ROOT" "$@" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
sys.path.insert(0, sys.argv[1] + "/west_commands")
from lane import RETIRED_MARKER, list_lanes, read_record
from test_prefix import prefix_lease_path
root = Path(sys.argv[2])
mode = sys.argv[3]
baseline = root / "coordination-before.json"
def identity(path):
    metadata = path.lstat()
    assert metadata.st_uid == os.getuid() and not path.is_symlink(), path
    return [metadata.st_dev, metadata.st_ino]
if mode == "capture":
    records = {}
    for name in ("lane-a", "lane-b", "lane-parent"):
        lane = root / name
        files = [lane / ".execution.lock", prefix_lease_path(lane / "merged"),
                 *sorted(lane.glob(".darling-prefix-lock-v2-*"))]
        records[name] = {"directory": identity(lane),
                         "files": {path.name: identity(path) for path in files}}
    baseline.write_text(json.dumps(records, sort_keys=True) + "\n")
else:
    name = sys.argv[4]
    lane = root / name
    saved = json.loads(baseline.read_text())[name]
    if mode == "stable":
        assert identity(lane) == saved["directory"], lane
        for filename, expected in saved["files"].items():
            assert identity(lane / filename) == expected, filename
    elif mode == "retired":
        assert read_record(lane) is None and lane not in list_lanes(root)
        assert not (lane / "merged").exists() and not (lane / "lane.json").exists()
        metadata = lane.stat()
        assert json.loads((lane / RETIRED_MARKER).read_text()) == {
            "schema": 1, "name": name, "root": str(root), "uid": os.getuid(),
            "device": metadata.st_dev, "inode": metadata.st_ino,
        }
        allowed = {RETIRED_MARKER, ".execution.lock", prefix_lease_path(lane / "merged").name}
        for path in lane.iterdir():
            metadata = path.lstat()
            assert path.name in allowed or path.name.startswith(".darling-prefix-lock-v2-"), path
            assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid(), path
    else:
        raise AssertionError(mode)
PY
}

lane_owned_processes() {
	west dev lane status "$1" --root "$ROOT" --json 2>/dev/null |
		python3 -c 'import json,sys
try:
    print(len(json.load(sys.stdin)["status"]["processes"]))
except Exception:
    print("unknown")'
}

# One measured guest run inside a lane, on its own lane log and its own lane cgroup.
run_guest() {
	local lane_name=$1 log=$2 wait_seconds=$3 marker=$4 cmd=$5
	# --root must precede the lane name: `exec` takes its command as argparse REMAINDER, so an
	# option written after the name is part of the command, and `env` then tries to execute it.
	( cd "$REPO" && "$MISE" -C . run west -- dev lane exec --root "$ROOT" "$lane_name" -- \
		./scripts/darling-boot-run.sh \
		--prefix "$ROOT/$lane_name/merged" \
		--runtime-root "$RUNTIME_ROOT" \
		--wait "$wait_seconds" \
		--log "$log" \
		--bundle "$log.bundle" \
		--status-marker LANE_RC= \
		--marker "$marker" \
		--cmd "$cmd" ) >"$log.console" 2>&1
	local rc=$?
	if [ "$rc" -ne 0 ]; then
		note "lane $lane_name guest run exited rc=$rc; console says:"
		sed -n '1,12p' <"$log.console" 2>/dev/null | sed 's/^/    /'
	fi
	return "$rc"
}

# The safety net. A failing run keeps its lanes, its logs and its console captures: the failure is
# the thing worth inspecting, and removing it would destroy the only evidence of it. Only a run in
# which every check passed clears its own scratch, unless --keep asked for it to stay.
finalize() {
	local exit_status=$?
	if [ "$exit_status" -eq 0 ] && [ "$failures" -eq 0 ] && [ "$KEEP" -eq 0 ]; then
		rm -rf "$ROOT/evidence"
		rm -f "$ROOT/"*.log "$ROOT/runtime-before.tsv" "$ROOT/coordination-before.json"
		note "retired lane parents and persistent coordination retained: $ROOT"
		return
	fi
	note "lane root kept for inspection: $ROOT"
	note "remove children before parent: west dev lane destroy lane-a --root $ROOT; west dev lane destroy lane-b --root $ROOT; west dev lane destroy lane-parent --root $ROOT"
}
trap finalize EXIT

note "base prefix:  $BASE"
note "lane root:    $ROOT"
note "runtime:      $RUNTIME_ROOT"
BASE_HASHES_BEFORE=$(base_hashes)
BASE_DIGEST_BEFORE=$(base_tree_digest "$BASE")
RUNTIME_LAUNCHER_BEFORE=$(sha256sum "$DARLING_LAUNCHER") || exit 2
"$REPO/scripts/darling-artifact-manifest.sh" --runtime-root "$RUNTIME_ROOT" \
	--save "$ROOT/runtime-before.tsv" >"$ROOT/runtime-baseline.log" || exit 2

west dev lane create lane-parent --base "$BASE" --runtime-root "$RUNTIME_ROOT" --root "$ROOT" >"$ROOT/create-parent.log" 2>&1 ||
	{ warn "cannot create parent: $(cat "$ROOT/create-parent.log")"; exit 1; }
mkdir -p "$ROOT/lane-parent/merged/private/var/tmp" || exit 1
printf 'frozen parent input\n' >"$ROOT/lane-parent/merged/private/var/tmp/lane-parent.txt" || exit 1
west dev lane freeze lane-parent --root "$ROOT" >"$ROOT/freeze-parent.log" 2>&1 ||
	{ warn "cannot freeze parent: $(cat "$ROOT/freeze-parent.log")"; exit 1; }
west dev lane exec --root "$ROOT" --no-scope lane-parent -- sh -c 'printf forbidden > "$1"' _ "$ROOT/frozen-executed" >"$ROOT/frozen-exec.log" 2>&1
frozen_exec_rc=$?
require "frozen exec refuses before running the command even without a scope" \
	bash -c 'test "$1" -ne 0 && ! test -e "$2"' _ "$frozen_exec_rc" "$ROOT/frozen-executed"
west test --lane lane-parent --runtime-root "$RUNTIME_ROOT" --cleanup-prefix >"$ROOT/frozen-test.log" 2>&1
frozen_test_rc=$?
require "west test refuses a frozen lane" test "$frozen_test_rc" -ne 0
west dev lane create lane-a --parent lane-parent --root "$ROOT" >"$ROOT/create-a.log" 2>&1 ||
	{ warn "cannot create lane A: $(cat "$ROOT/create-a.log")"; exit 1; }
west dev lane create lane-b --parent lane-parent --root "$ROOT" >"$ROOT/create-b.log" 2>&1 ||
	{ warn "cannot create lane B: $(cat "$ROOT/create-b.log")"; exit 1; }
west dev lane destroy lane-parent --root "$ROOT" >"$ROOT/guard-parent.log" 2>&1
parent_destroy_rc=$?
require "removing a referenced frozen parent is refused" test "$parent_destroy_rc" -ne 0
require "refused parent removal leaves its retained mount intact" mountpoint -q "$ROOT/lane-parent/merged"

require "lane A has a product-bound clone identity and generation" clone_is_bound "$LANE_A/merged"
require "lane B has a product-bound clone identity and generation" clone_is_bound "$LANE_B/merged"
if [ "$failures" -ne 0 ]; then
	warn "LANE-CONCURRENCY: FAIL (clone identity admission)"
	exit 1
fi

# A owns only writable guest workload input; no launcher or runtime library is copied or replaced.
mkdir -p "$LANE_A/merged/private/var/tmp"
printf 'lane experiment payload\n' >"$LANE_A/merged/private/var/tmp/lane-experiment.txt"

note ""
note "== phase 1: two guest experiments at once =="
run_guest lane-a "$LANE_A/logs/concurrent.log" "$WAIT" EXPERIMENT_VISIBLE=1 \
	'if grep -q "lane experiment payload" /private/var/tmp/lane-experiment.txt && grep -q "frozen parent input" /private/var/tmp/lane-parent.txt; then echo EXPERIMENT_VISIBLE=1; rc=0; else rc=1; fi; echo A_ALIVE=1; sleep 1; echo LANE_RC=$rc' &
pid_a=$!
run_guest lane-b "$LANE_B/logs/concurrent.log" "$WAIT" B_IS_CLEAN=1 \
	'if test ! -e /private/var/tmp/lane-experiment.txt && grep -q "frozen parent input" /private/var/tmp/lane-parent.txt; then echo B_IS_CLEAN=1; rc=0; else rc=1; fi; echo B_ALIVE=1; sleep 1; echo LANE_RC=$rc' &
pid_b=$!
wait "$pid_a"; rc_a=$?
wait "$pid_b"; rc_b=$?

require "lane A completed its guest workload with clean product shutdown (rc=$rc_a)" guest_passed "$LANE_A/logs/concurrent.log" "$rc_a"
require "lane B completed its guest workload with clean product shutdown (rc=$rc_b)" guest_passed "$LANE_B/logs/concurrent.log" "$rc_b"
require "A's guest saw its own writable payload" grep -q 'EXPERIMENT_VISIBLE=1' "$LANE_A/logs/concurrent.log"
require "B's guest never saw it" grep -q 'B_IS_CLEAN=1' "$LANE_B/logs/concurrent.log"
require "A's guest did not see B's workload marker" bash -c "! grep -q 'B_IS_CLEAN=1' '$LANE_A/logs/concurrent.log'"
# The counters line is CONSOLE output (BUNDLE-COUNTERS), not a line of the run log; reading the log
# here reported a failure for two runs that both printed 0. It is the evidence that two runtimes can
# publish and check in at the same time without one denying the other's transport.
require "both boots reported zero socket denials, urgent timeouts and courier misses" bash -c "
	grep -qF 'socket-denials=0 urgent-timeouts=0 courier-misses=0' '$LANE_A/logs/concurrent.log.console' &&
	grep -qF 'socket-denials=0 urgent-timeouts=0 courier-misses=0' '$LANE_B/logs/concurrent.log.console'"
require "lane A selected the installed runtime without a copied launcher" lane_uses_runtime "$LANE_A"
require "lane B selected the same installed runtime" lane_uses_runtime "$LANE_B"
require "A's workload input is absent from B's prefix" bash -c "! test -e '$LANE_B/merged/private/var/tmp/lane-experiment.txt'"
require "A's workload input is absent from the base" bash -c "! test -e '$BASE/private/var/tmp/lane-experiment.txt'"

UNIT_A=$(lane_unit lane-a)
UNIT_B=$(lane_unit lane-b)
require "the tool names lane A's cgroup unit" bash -c "[ -n \"$UNIT_A\" ]"
require "the tool names lane B's cgroup unit" bash -c "[ -n \"$UNIT_B\" ]"
require "the two lanes own different cgroup units" bash -c "[ \"$UNIT_A\" != \"$UNIT_B\" ]"
if [ "$failures" -ne 0 ]; then
	warn "LANE-CONCURRENCY: FAIL (first guest phase); no prefix is rebooted after failed cleanup"
	exit 1
fi

note ""
note "== phase 2: stopping lane A while lane B runs =="
run_guest lane-a "$LANE_A/logs/stopped.log" 90 EXPERIMENT_VISIBLE=1 \
	'if grep -q "lane experiment payload" /private/var/tmp/lane-experiment.txt; then echo EXPERIMENT_VISIBLE=1; rc=0; else rc=1; fi; echo A_ALIVE=1; sleep 120; echo LANE_RC=$rc' &
pid_a=$!
a_pids=""
for _ in $(seq 1 60); do
	a_pids=$(scope_processes "$UNIT_A")
	[ -n "$a_pids" ] && grep -q 'A_ALIVE=1' "$LANE_A/logs/stopped.log" 2>/dev/null && break
	sleep 0.5
done
require "lane A's experiment is running inside its own cgroup" test -n "$a_pids"
require "lane A's unit says which lane it belongs to" \
	bash -c "systemctl --user show --property=Description --value '$UNIT_A' | grep -qF '$LANE_A'"

run_guest lane-b "$LANE_B/logs/after-stop.log" "$WAIT" B_IS_CLEAN=1 \
	"if test ! -e /private/var/tmp/lane-experiment.txt; then echo B_IS_CLEAN=1; rc=0; else rc=1; fi; echo B_ALIVE=1; sleep $((WAIT * 3 / 4)); echo LANE_RC=\$rc" &
pid_b=$!

# Waiting for B's OWN cgroup to be live is the premise of this whole phase. A fixed sleep was
# MEASURED wrong: the previous phase leaves its scope loaded and empty, so B's start spends that
# time releasing the name, and B was not yet running when the check assumed it was.
b_live=""
for _ in $(seq 1 60); do
	b_live=$(scope_processes "$UNIT_B")
	[ -n "$b_live" ] && grep -q 'B_ALIVE=1' "$LANE_B/logs/after-stop.log" 2>/dev/null && break
	sleep 0.5
done
require "lane B's experiment runs in its own cgroup before A is stopped" test -n "$b_live"

DPREFIX="$LANE_A/merged" DARLING_PREFIX="$LANE_A/merged" "$DARLING_LAUNCHER" shutdown \
	>"$ROOT/shutdown-a.log" 2>&1
shutdown_a_rc=$?
require "B's cgroup still holds its guest while A is shut down" test -n "$(scope_processes "$UNIT_B")"
wait "$pid_a"; stopped_a_rc=$?
require "A was interrupted through successful product shutdown, never host rescue" \
	bash -c 'test "$1" -eq 0 && test "$2" -ne 0 && test -z "$3"' _ \
	"$shutdown_a_rc" "$stopped_a_rc" "$(scope_processes "$UNIT_A")"
if ! clean_guest_bundle "$LANE_A/logs/stopped.log"; then
	warn "lane A teardown was not clean product shutdown"
	failures=$((failures + 1))
fi
require "A's experiment is gone" test "$(lane_owned_processes lane-a)" = 0
wait "$pid_b"; rc_b_after=$?
require "lane B still completed with clean product shutdown while A stopped (rc=$rc_b_after)" guest_passed "$LANE_B/logs/after-stop.log" "$rc_b_after"
require "B's guest still saw no experiment" grep -q 'B_IS_CLEAN=1' "$LANE_B/logs/after-stop.log"
require "A's guest-state layer survived product shutdown" test -f "$LANE_A/merged/.darling-prefix-state-v2"
require "A's lane is still mounted and owned by its own daemon" 	bash -c "test \"\$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[2] + \"/west_commands\"); from pathlib import Path; from lane import fuse_daemon_pids, lane_prefix, mount_targets; print(len(fuse_daemon_pids(Path(sys.argv[1]))) + len(mount_targets(lane_prefix(Path(sys.argv[1])))))' '$LANE_A' '$REPO')\" = 2"
require "B's lane is still mounted and owned by its own daemon" \
	test "$(lane_count daemons "$LANE_B")" = 1
require "B's lane is still mounted" test "$(lane_count mounted "$LANE_B")" = 1

note ""
note "== the repository's own prefix-scoped cleaner, on A's prefix =="
cleaner_output=$(cd "$REPO" && ./scripts/prefix-cleanup.sh --prefix "$LANE_A/merged" 2>&1)
cleaner_rc=$?
note "$cleaner_output"
require "prefix-cleanup.sh reports A's prefix clean (rc=$cleaner_rc)" test "$cleaner_rc" -eq 0
require "it says nothing about lane B" bash -c "! printf '%s' \"\$1\" | grep -q 'lane-b'" _ "$cleaner_output"

note ""
note "== phase 3: the base is untouched, and nothing is left behind =="
require "base file hashes are identical" test "$BASE_HASHES_BEFORE" = "$(base_hashes)"
require "base tree identity is identical" test "$BASE_DIGEST_BEFORE" = "$(base_tree_digest "$BASE")"
west dev lane verify lane-a --root "$ROOT" >"$ROOT/verify-a.log" 2>&1
verify_a_rc=$?
require "lane A's managed layers, retained mounts and runtime verify" test "$verify_a_rc" -eq 0

mkdir -p "$ROOT/evidence/lane-a" "$ROOT/evidence/lane-b"
cp -a "$LANE_A/logs" "$LANE_A/receipts" "$LANE_A/lane.json" "$ROOT/evidence/lane-a/" || exit 1
cp -a "$LANE_B/logs" "$LANE_B/receipts" "$LANE_B/lane.json" "$ROOT/evidence/lane-b/" || exit 1
lane_coordination capture || exit 1
west dev lane destroy lane-a --root "$ROOT" >"$ROOT/destroy-a.log" 2>&1
destroy_a_rc=$?
west dev lane destroy lane-b --root "$ROOT" >"$ROOT/destroy-b.log" 2>&1
destroy_b_rc=$?
west dev lane destroy lane-parent --root "$ROOT" >"$ROOT/destroy-parent.log" 2>&1
destroy_parent_rc=$?
require "the frozen parent retires after its children" \
	bash -c 'test "$1" -eq 0' _ "$destroy_parent_rc"
require "lane A has no active record or prefix, only minimal retirement state" lane_coordination retired lane-a
require "lane B has no active record or prefix, only minimal retirement state" lane_coordination retired lane-b
require "the parent has no active record or prefix, only minimal retirement state" lane_coordination retired lane-parent
require "lane A retains its parent, admission and prefix lease inodes" lane_coordination stable lane-a
require "lane B retains its parent, admission and prefix lease inodes" lane_coordination stable lane-b
require "the parent retains its parent, admission and prefix lease inodes" lane_coordination stable lane-parent
require "no lane mount is left" bash -c "! mount | grep -qF '$ROOT'"
require "no lane fuse daemon is left" test "$(lane_count daemons "$LANE_A")$(lane_count daemons "$LANE_B")$(lane_count daemons "$ROOT/lane-parent")" = "000"
require "no lane process is left" test "$(lane_count processes "$LANE_A")$(lane_count processes "$LANE_B")" = "00"
require "both child removals succeeded" bash -c 'test "$1" -eq 0 && test "$2" -eq 0' _ "$destroy_a_rc" "$destroy_b_rc"
require "the selected installed runtime is intact after both lanes" runtime_unchanged
require "the empty snapshotter store has no active service" snapshotter_stopped

note "CHECKS-RUN ran=$checks_run pinned=$EXPECTED_CHECKS"
if [ "$checks_run" -ne "$EXPECTED_CHECKS" ]; then
	warn "FAIL-CHECK  the contract ran $checks_run checks, not the pinned $EXPECTED_CHECKS: a check was skipped"
	failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
	note "LANE-CONCURRENCY: PASS"
	exit 0
fi
warn "LANE-CONCURRENCY: FAIL ($failures check(s) failed)"
exit 1

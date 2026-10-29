#!/usr/bin/env bash
# run-darling-lane-test-lane-contract.sh -- `west test --lane NAME`, two lanes at once.
#
# WHY THIS EXISTS. The two-guest concurrency contract proves the RUNTIME isolation with
# `scripts/darling-boot-run.sh`. This one drives the interface an agent actually types: the existing
# `west test` runner, twice, in two different lanes, at the same time. What it verifies is what the
# interface has to get right and a direct workload does not exercise:
#
#   1. distinct prefixes, state roots and cgroup units per lane;
#   2. both children inherit real guest-state input from one mounted frozen parent;
#   3. independent logs and receipts: every run writes inside its own lane, and neither lane's tree
#      names the other lane's path;
#   4. no cross-lane interference: the lanes hold live processes at the same time and every run
#      reaches its own exit code;
#   5. EXACT real tool exit codes, captured per lane, not merely "non-zero on failure";
#   6. the base is unchanged by both runs, by content (sha256 baseline) and by tree identity;
#   7. real same-name recreation reaches Ready and product shutdown, and retained old/current
#      prefix lease descriptions still exclude each other across both retirements.
#
# Checks counted: the number of checks that actually ran is compared against a pinned literal, so a
# phase that silently did not execute fails this contract instead of passing it.
#
# THE OPTIONAL WORKLOAD PHASE (`--guest-workload NAME`). The default mode drives concurrent
# prefix lifecycle, then a narrow real guest file/Ready/shutdown smoke on the recreated lane.
# A named flat workload additionally proves its own guest verdicts and retained evidence through
# the same selected installation, without source materialization.
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
EXPECTED_CHECKS=42
EXPECTED_CHECKS_GUEST=55

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
for tool in fusermount3 systemctl systemd-run; do
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

same_name_recreation() {
	python3 -B - "$REPO" "$ROOT" "$MISE" "$RUNTIME_ROOT" "$BASE" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
repo, root, mise, runtime, base = sys.argv[1:]
sys.path.insert(0, repo + "/west_commands")
from lane import RETIRED_MARKER, read_record
from test_prefix import prefix_lease, prefix_lease_path, prefix_process_ownership_snapshot
lane = Path(root) / "lane-a"
prefix = lane / "merged"
lease = prefix_lease_path(prefix)
def identity(metadata):
    return metadata.st_dev, metadata.st_ino
def cli(log, *args):
    with (Path(root) / log).open("w") as output:
        result = subprocess.run(
            [mise, "-C", repo, "run", "west", "--", *map(str, args)],
            cwd=repo, stdout=output, stderr=subprocess.STDOUT,
        )
    assert result.returncode == 0, (args, log, result.returncode)
def lifecycle_state(path):
    lines = (path / ".darling-prefix-state-v2").read_text().splitlines()
    assert lines[0] == "DARLING_PREFIX_STATE_V2"
    return dict(line.split("=", 1) for line in lines[1:])
source = lifecycle_state(Path(base))
# An unlocked, CLOEXEC descriptor survives ordinary CLI destruction and recreation without
# borrowing a lease or allowing a runtime child to retain the test controller's descriptor.
old = os.open(lease, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
try:
    original = identity(os.fstat(old))
    directory = identity(lane.stat())
    admission = identity((lane / ".execution.lock").stat())
    cli("destroy-a.log", "dev", "lane", "destroy", "lane-a", "--root", root)
    assert read_record(lane) is None and not prefix.exists()
    assert (lane / RETIRED_MARKER).is_file()
    assert identity(lease.stat()) == original == identity(os.fstat(old))
    cli("recreate-a.log", "dev", "lane", "create", "lane-a",
        "--root", root, "--base", base, "--runtime-root", runtime)
    assert identity(lane.stat()) == directory
    assert identity((lane / ".execution.lock").stat()) == admission
    assert identity(lease.stat()) == original == identity(os.fstat(old))
    assert not (lane / RETIRED_MARKER).exists()
    record = read_record(lane)
    assert record["mode"] == "active" and record["runtime"]["root"] == runtime
    assert prefix.is_mount()
    copied = lifecycle_state(prefix)
    metadata = prefix.stat()
    assert int(copied["prefix_device"]) == metadata.st_dev
    assert int(copied["prefix_inode"]) == metadata.st_ino
    assert int(copied["owner_uid"]) == os.getuid()
    assert int(copied["owner_gid"]) == os.getgid()
    assert int(copied["generation"]) == int(source["generation"]) + 1
    # A distinct controller must conflict with an old open-file description retained from
    # BEFORE destroy. A delete-and-recreate implementation wrongly acquires another inode.
    fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = subprocess.run([sys.executable, "-B", "-c", """
import sys
sys.path.insert(0, sys.argv[1] + "/west_commands")
from test_prefix import prefix_lease
try:
    with prefix_lease(sys.argv[2], blocking=False):
        raise AssertionError("current controller escaped retained old lease")
except BlockingIOError:
    pass
""", repo, str(prefix)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (result.stdout, result.stderr)
    finally:
        fcntl.flock(old, fcntl.LOCK_UN)
    # Also prove the converse against the normal helper's separately opened description.
    with prefix_lease(prefix, blocking=False):
        try:
            fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(old, fcntl.LOCK_UN)
            raise AssertionError("retained old controller escaped current lease")
    log = Path(root) / "recreated-guest.log"
    bundle = Path(root) / "recreated-guest.bundle"
    cli("recreated-guest.console.log", "dev", "lane", "exec", "--root", root,
        "lane-a", "--", "./scripts/darling-boot-run.sh",
        "--runtime-root", runtime, "--prefix", prefix, "--wait", "20",
        "--log", log, "--bundle", bundle, "--status-marker", "RECREATED_RC=",
        "--cmd", "printf 'recreated guest state\\n' > /private/var/tmp/lane-recreated.txt; "
        "rc=$?; test \"$(cat /private/var/tmp/lane-recreated.txt)\" = 'recreated guest state' "
        "|| rc=1; echo RECREATED_RC=$rc")
    assert (prefix / "private/var/tmp/lane-recreated.txt").read_text() == "recreated guest state\n"
    cleanup = json.loads((bundle / "cleanup.json").read_text())
    assert cleanup["state"] == "clean" and cleanup["shutdown_rc"] == 0
    assert cleanup["processes"] == cleanup["mounts"] == 0
    assert not prefix_process_ownership_snapshot(prefix)
    cli("destroy-recreated-a.log", "dev", "lane", "destroy", "lane-a", "--root", root)
    assert read_record(lane) is None and not prefix.exists()
    assert identity(lease.stat()) == original == identity(os.fstat(old))
finally:
    os.close(old)
PY
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
	if [ "$exit_status" -eq 0 ] && [ "$failures" -eq 0 ] && [ "$KEEP" -eq 0 ]; then
		rm -rf "$ROOT/evidence" "$ROOT/recreated-guest.bundle"
		rm -f "$ROOT/"*.log "$ROOT/coordination-before.json"
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
note "mode:         $([ -n "$GUEST_WORKLOAD" ] && echo "interface + guest workload $GUEST_WORKLOAD" || echo interface)"

BASE_HASH_BEFORE=$(sha256sum "$BASE/.darling-prefix-state-v2") || exit 2
BASE_TREE_BEFORE=$(python3 -B -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1] + "/west_commands"); from lane import tree_identity; print(tree_identity(Path(sys.argv[2])))' "$REPO" "$BASE") || exit 2
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
create_args_a=(--parent lane-parent --root "$ROOT")
create_args_b=(--parent lane-parent --root "$ROOT")
[ -n "$SOURCE_A" ] && create_args_a+=(--source "$SOURCE_A")
[ -n "$SOURCE_B" ] && create_args_b+=(--source "$SOURCE_B")
west dev lane create lane-a "${create_args_a[@]}" >"$ROOT/create-a.log" 2>&1 ||
	{ warn "cannot create lane A: $(cat "$ROOT/create-a.log")"; exit 1; }
west dev lane create lane-b "${create_args_b[@]}" >"$ROOT/create-b.log" 2>&1 ||
	{ warn "cannot create lane B: $(cat "$ROOT/create-b.log")"; exit 1; }
west dev lane destroy lane-parent --root "$ROOT" >"$ROOT/guard-parent.log" 2>&1
parent_destroy_rc=$?
require "removing a referenced frozen parent is refused" test "$parent_destroy_rc" -ne 0
require "refused removal leaves the frozen parent mounted" mountpoint -q "$ROOT/lane-parent/merged"
for lane in lane-a lane-b; do
	grep -qF 'frozen parent input' "$ROOT/$lane/merged/private/var/tmp/lane-parent.txt" || exit 1
	printf '%s\n' "$lane private input" >"$ROOT/$lane/merged/private/var/tmp/$lane-private.txt" || exit 1
done

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
require "the two lanes own different cgroup units" bash -c "[ -n '$UNIT_A' ] && [ '$UNIT_A' != '$UNIT_B' ]"

if [ "$failures" -ne 0 ]; then
	warn "LANE-TEST-LANE: FAIL (identity admission)"
	exit 1
fi

admission_is_private() {
	west dev lane exec --root "$ROOT" --no-scope lane-a -- python3 -B - "$MISE" "$REPO" "$ROOT" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
mise, repo, root = sys.argv[1:]
lane = Path(root) / "lane-a"
lease = (lane / ".execution.lock").stat()
for entry in Path("/proc/self/fd").iterdir():
    try:
        metadata = entry.stat()
    except FileNotFoundError:
        continue
    assert (metadata.st_dev, metadata.st_ino) != (lease.st_dev, lease.st_ino), entry
result = subprocess.run(
    [mise, "-C", repo, "run", "west", "--", "dev", "lane", "freeze",
     "lane-a", "--root", root, "--json"],
    capture_output=True, text=True, timeout=30,
)
assert result.returncode != 0, result.stdout
assert json.loads((lane / "lane.json").read_text())["mode"] == "active"
PY
}
require "an unscoped command cannot inherit admission or freeze its executing lane" admission_is_private
west dev lane exec --root "$ROOT" lane-a -- "$MISE" -C "$REPO" run west -- test \
	--prefix "$PREFIX_A" --runtime-root "$RUNTIME_ROOT" --cleanup-prefix >"$LANE_A/logs/nested-controller.log" 2>&1
nested_controller_rc=$?
require "a nested prefix controller completes without borrowing an arbitrary exec lease" test "$nested_controller_rc" -eq 0
if [ "$failures" -ne 0 ]; then
	warn "LANE-TEST-LANE: FAIL (execution admission)"
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
	require "lane A never sees lane B's private input" test ! -e "$PREFIX_A/private/var/tmp/lane-b-private.txt"
	require "lane B never sees lane A's private input" test ! -e "$PREFIX_B/private/var/tmp/lane-a-private.txt"
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
require "lane A's managed layers, retained mounts and runtime verify" test "$verify_a_rc" -eq 0
require "lane B's managed layers, retained mounts and runtime verify" test "$verify_b_rc" -eq 0

mkdir -p "$ROOT/evidence/lane-a" "$ROOT/evidence/lane-b"
cp -a "$LANE_A/logs" "$LANE_A/receipts" "$LANE_A/lane.json" "$LANE_A/state" "$ROOT/evidence/lane-a/" || exit 1
cp -a "$LANE_B/logs" "$LANE_B/receipts" "$LANE_B/lane.json" "$LANE_B/state" "$ROOT/evidence/lane-b/" || exit 1
lane_coordination capture || exit 1
require "same-name Ready recreation preserves retained/current OFD exclusion and clean product lifecycle" same_name_recreation
if [ "$failures" -ne 0 ]; then
	warn "LANE-TEST-LANE: FAIL (same-name recreation); preserving lifecycle evidence"
	exit 1
fi
west dev lane destroy lane-b --root "$ROOT" >"$ROOT/destroy-b.log" 2>&1 || exit 1
west dev lane destroy lane-parent --root "$ROOT" >"$ROOT/destroy-parent.log" 2>&1
destroy_parent_rc=$?
require "the frozen parent retires after its children" test "$destroy_parent_rc" -eq 0
require "lane A has no active record or prefix, only minimal retirement state" lane_coordination retired lane-a
require "lane B has no active record or prefix, only minimal retirement state" lane_coordination retired lane-b
require "the parent has no active record or prefix, only minimal retirement state" lane_coordination retired lane-parent
require "lane A retains its parent, admission and prefix lease inodes across recreation" lane_coordination stable lane-a
require "lane B retains its parent, admission and prefix lease inodes" lane_coordination stable lane-b
require "the parent retains its parent, admission and prefix lease inodes" lane_coordination stable lane-parent
require "no lane mount is left" bash -c "! mount | grep -qF '$ROOT'"
require "no lane fuse daemon is left" test "$(lane_daemons "$LANE_A")$(lane_daemons "$LANE_B")$(lane_daemons "$ROOT/lane-parent")" = "000"
require "the empty snapshotter store has no active service" snapshotter_stopped
require "both lane scopes have no remaining processes" test -z "$(scope_pids "$UNIT_A")$(scope_pids "$UNIT_B")"
require "the original base lifecycle content is unchanged" test "$BASE_HASH_BEFORE" = "$(sha256sum "$BASE/.darling-prefix-state-v2")"
BASE_TREE_AFTER=$(python3 -B -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1] + "/west_commands"); from lane import tree_identity; print(tree_identity(Path(sys.argv[2])))' "$REPO" "$BASE")
require "the original base tree identity is unchanged" test "$BASE_TREE_BEFORE" = "$BASE_TREE_AFTER"

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

#!/usr/bin/env bash
# Real console progress while main is blocked in Mach receive.
# Both bad and fixed images use the same consumer oracle: two working console
# descriptors must deliver their payloads before the worker releases main.
# Exit 0 = PASS, 1 = consumer failure, 2 = invalid/incomplete evidence.

set -uo pipefail

original_args=("$@")
prefix=""
runtime_root=""
fixture=""
run_dir=""
repeats=1
wait_seconds=90
expected_sha=""
name="plane_deferred_arm_guest"

while [ $# -gt 0 ]; do
	case "$1" in
	--prefix) prefix="$2"; shift 2 ;;
	--runtime-root) runtime_root="$2"; shift 2 ;;
	--fixture) fixture="$2"; shift 2 ;;
	--run-dir) run_dir="$2"; shift 2 ;;
	--repeat) repeats="$2"; shift 2 ;;
	--expected-server-sha) expected_sha="$2"; shift 2 ;;
	--wait) wait_seconds="$2"; shift 2 ;;
	-h|--help)
		cat <<'HELP'
Usage: run-plane-deferred-arm-contract.sh --runtime-root R --prefix RAW_P
       [--fixture MACH_O] [--repeat N] [--expected-server-sha SHA]
       [--run-dir D] [--wait S]

R is one ordinary immutable installation, P is separate prepared guest state.
Without --fixture, guest compilation requires darling-command-line-tools;
provision it explicitly with west darling-bootstrap --build-dir B --runtime-root R
--prefix P --no-build --guest-toolchain darling-command-line-tools.
Evidence is retained outside /tmp; --run-dir must be absent or empty.
Two real console writes must finish before main's receive is released.
The identical consumer oracle applies to bad and fixed images.
HELP
		exit 0 ;;
	*) echo "plane-deferred-arm: unknown argument: $1" >&2; exit 2 ;;
	esac
done

case "$repeats" in ''|*[!0-9]*) echo "plane-deferred-arm: --repeat must be a positive integer" >&2; exit 2 ;; esac
[ "$repeats" -ge 1 ] || { echo "plane-deferred-arm: --repeat must be >= 1" >&2; exit 2; }

here="$(cd "$(dirname "$0")" && pwd)"
workspace="$(cd "$here/.." && pwd)"

# ---------------------------------------------------------------------------
# Live mode: isolation preconditions.
# ---------------------------------------------------------------------------
boot_run="$workspace/scripts/darling-boot-run.sh"
src="$here/plane_deferred_arm_guest.c"
[ -x "$boot_run" ] || { echo "plane-deferred-arm: INVALID: missing $boot_run" >&2; exit 2; }
[ -n "$prefix" ] && [ -d "$prefix" ] || {
	echo "plane-deferred-arm: INVALID: --prefix must name separately bootstrapped guest state" >&2; exit 2; }
DARLING_WORKSPACE_ROOT="$workspace"
. "$workspace/scripts/darling-runtime.sh"
darling_resolve_runtime "$runtime_root" "$prefix" || exit $?
# Canonicalize inherited ownership to FD 8 as well as acquiring absent ownership.
# Runtime children close this descriptor; nested managed helpers may borrow it.
if [ -z "${WEST_TEST_PREFIX_LEASE:-}" ] || [ "${WEST_TEST_PREFIX_LEASE_FD:-8}" != 8 ]; then
	exec python3 -B "$workspace/west_commands/test_prefix.py" \
		--prefix "$prefix" -- "$workspace/tests/run-plane-deferred-arm-contract.sh" "${original_args[@]}"
fi
python3 -B "$workspace/west_commands/test_prefix.py" \
	--prefix "$prefix" --validate-lease || exit $?
owned="$(darling_prefix_owned_pids "$prefix")" || exit $?
[ -z "$owned" ] || {
	echo "plane-deferred-arm: INVALID: selected prefix already has an owned runtime; stop its owner or select an isolated lane" >&2
	exit 2
}
case "$prefix" in
	*prefix-verify14*)
		echo "plane-deferred-arm: INVALID: refusing prefix-verify14: it is shared with the active Xcode workload (the previous gate run interfered with it)" >&2
		exit 2 ;;
	*/prefix-ios|*/prefix-ios/)
		echo "plane-deferred-arm: INVALID: refusing the canonical shared prefix-ios" >&2
		exit 2 ;;
esac
[ -f "$src" ] || { echo "plane-deferred-arm: INVALID: missing fixture source $src" >&2; exit 2; }

run_dir="$(python3 -B - "$workspace" "$run_dir" "$DARLING_RUNTIME_ROOT" "$prefix" <<'PY'
import os
import sys
import tempfile
from pathlib import Path

workspace, supplied, runtime, prefix = sys.argv[1:]
root = Path(supplied).absolute() if supplied else Path(
    os.environ.get("DW_STATE_ROOT", str(Path(workspace).parent / ".west-state"))
) / "diagnostics"
resolved = root.resolve()
if not root.is_absolute() or resolved.is_relative_to(Path("/tmp")):
    raise SystemExit("plane-deferred-arm: INVALID: evidence needs an absolute durable root outside /tmp")
if any(resolved.is_relative_to(Path(value).resolve()) for value in (runtime, prefix)):
    raise SystemExit("plane-deferred-arm: INVALID: evidence must be outside runtime and guest state")
if supplied:
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise SystemExit("plane-deferred-arm: INVALID: --run-dir must be absent or empty")
else:
    root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="plane-deferred-arm-", dir=root))
print(root)
PY
)" || exit 2
runtime_started=0
finish_runtime() {
	local rc=$?
	trap - EXIT INT TERM
	if [ "$runtime_started" = 1 ]; then
		"$workspace/scripts/prefix-cleanup.sh" --runtime-root "$DARLING_RUNTIME_ROOT" \
			--prefix "$prefix" > "$run_dir/failure-cleanup.log" 2>&1
		local cleanup_rc=$?
		if [ "$cleanup_rc" -ne 0 ]; then
			echo "PLANE-DEFERRED-ARM: INVALID: failed normal shutdown/rescue (rc=$cleanup_rc); see $run_dir/failure-cleanup.log" >&2
			[ "$rc" -ne 0 ] || rc=2
		fi
	fi
	exit "$rc"
}
trap finish_runtime EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

deployed_sha="$(sha256sum "$DARLING_RUNTIME_ROOT/bin/darlingserver" | cut -d' ' -f1)" || exit 2
printf 'runtime-root=%s\nprefix=%s\nserver-sha256=%s\n' \
	"$DARLING_RUNTIME_ROOT" "$prefix" "$deployed_sha" > "$run_dir/selection.txt"
if [ -n "$expected_sha" ] && [ "$deployed_sha" != "$expected_sha" ]; then
	echo "PLANE-DEFERRED-ARM: INVALID: deployed darlingserver sha256=$deployed_sha, expected $expected_sha: the runtime changed since the RED claim was established" >&2
	exit 2
fi

# ---------------------------------------------------------------------------
# Materialize only workload inputs; compilation itself executes in the guest.
# ---------------------------------------------------------------------------
guest_path="/private/var/tmp/$name"
check_bundle() { # <tag> -- complete evidence, actual image and clean product teardown
	local tag="$1" bundle="$run_dir/$1.log.bundle"
	[ ! -e "$bundle/INCOMPLETE" ] || return 1
	python3 "$workspace/scripts/darling-evidence.py" cat "$bundle/MANIFEST" \
		| grep -a '^BUNDLE-COMPLETE ' >/dev/null || return 1
	python3 "$workspace/scripts/darling-evidence.py" cat "$bundle/cleanup.json" \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("state")=="clean" and d.get("shutdown_rc")==0 and d.get("processes")==0 and d.get("mounts")==0 else 1)' || return 1
	python3 "$workspace/scripts/darling-evidence.py" cat "$bundle/run-env.txt" \
		> "$run_dir/$tag.run-env" || return 1
	grep -aqF -- "RUN-ENV runtime-root=$DARLING_RUNTIME_ROOT prefix=$prefix " \
		"$run_dir/$tag.run-env" || return 1
	[ "$(sha256sum "$DARLING_RUNTIME_ROOT/bin/darlingserver" | cut -d' ' -f1)" = "$deployed_sha" ] || return 1
}

if [ -z "$fixture" ]; then
	cp "$src" "$run_dir/$name.c" || exit 2
	compile_body="$(python3 -B - "$workspace" "$prefix" "$DARLING_SYSTEM_ROOT" "$run_dir/$name.c" "$guest_path" <<'PY'
import hashlib
import shlex
import sys
from pathlib import Path

workspace, prefix, system_root, source, output = sys.argv[1:]
sys.path.insert(0, str(Path(workspace) / "west_commands"))
from guest_toolchain import DEFAULT_GUEST_CC, DEFAULT_GUEST_CFLAGS, SELECTED_COMMAND_LINE_TOOLS_CLANG
from prefix_repair import guest_c_fixture_prerequisite_problems

problems = guest_c_fixture_prerequisite_problems(
    Path(prefix), DEFAULT_GUEST_CC, DEFAULT_GUEST_CFLAGS, system_root=Path(system_root),
)
if problems:
    raise SystemExit("plane-deferred-arm: INVALID: prerequisite darling-command-line-tools: "
                     + "; ".join(problems)
                     + ". Provision with west darling-bootstrap --build-dir B --runtime-root R "
                       "--prefix P --no-build --guest-toolchain darling-command-line-tools")
payload = Path(source).read_text()
delimiter = "WEST_PLANE_SOURCE_" + hashlib.sha256(payload.encode()).hexdigest()
while delimiter in payload.splitlines():
    delimiter += "_"
guest_source = output + ".c"
print("mkdir -p /private/var/tmp || exit $?")
print(f"cat > {shlex.quote(guest_source)} <<'{delimiter}'")
print(payload, end="" if payload.endswith("\n") else "\n")
print(delimiter)
print("upload_rc=$?; [ \"$upload_rc\" -eq 0 ] || exit \"$upload_rc\"")
print("guest_cc=" + shlex.quote(DEFAULT_GUEST_CC))
print('compiler_version="$("$guest_cc" --version 2>&1)"; version_rc=$?')
print('printf "%s\\n" "$compiler_version"')
print('[ "$version_rc" -eq 0 ] || exit "$version_rc"')
print('case "$compiler_version" in *' + shlex.quote(SELECTED_COMMAND_LINE_TOOLS_CLANG)
      + '*) ;; *) printf "prerequisite darling-command-line-tools: unexpected guest compiler\\n" >&2; exit 127 ;; esac')
print(shlex.join([DEFAULT_GUEST_CC, *shlex.split(DEFAULT_GUEST_CFLAGS), "-std=gnu11",
                  "-o", output, guest_source, "-lpthread"]))
print('compile_rc=$?; printf "PDA_COMPILE_RC=%s\\n" "$compile_rc"')
print('[ "$compile_rc" -eq 0 ] || exit "$compile_rc"')
print('printf "PDA_COMPILE_DONE=1\\n"')
PY
	)" || exit 2
	printf '%s\n' "$compile_body" > "$run_dir/compile.guest-command.sh"
	runtime_started=1
	"$boot_run" --runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$prefix" --wait "$wait_seconds" \
		--status-marker 'PDA_COMPILE_RC=' --poll 'PDA_COMPILE_DONE=1' \
		--log "$run_dir/compile.log" --bundle "$run_dir/compile.log.bundle" \
		--cmd "$compile_body" > "$run_dir/compile.harness" 2>&1
	compile_rc=$?
	printf '%s\n' "$compile_rc" > "$run_dir/compile.harness-rc"
	if [ "$compile_rc" -ne 0 ] || ! check_bundle compile; then
		echo "PLANE-DEFERRED-ARM: INVALID: guest compilation or normal teardown failed; see $run_dir/compile.harness" >&2
		exit 2
	fi
	runtime_started=0
else
	# Supplied binaries are inputs, not runtime pieces or host-executed fixtures.
	python3 -B - "$fixture" "$run_dir/fixture-input.txt" "$run_dir/fixture.input" <<'PY'
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = path.read_bytes()
if payload[:4] not in (b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
                       b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                       b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"):
    raise SystemExit("plane-deferred-arm: INVALID: --fixture must be a Mach-O workload")
copied = Path(sys.argv[3])
copied.write_bytes(payload)
copied_payload = copied.read_bytes()
original_sha = hashlib.sha256(payload).hexdigest()
copied_sha = hashlib.sha256(copied_payload).hexdigest()
if copied_sha != original_sha:
    raise SystemExit("plane-deferred-arm: INVALID: fixture copy digest mismatch")
copied.chmod(0o700)
Path(sys.argv[2]).write_text(
    f"path={path.absolute()}\nsha256={original_sha}\n"
    f"copied-path={copied.absolute()}\ncopied-sha256={copied_sha}\n")
PY
	[ "$?" -eq 0 ] || exit 2
	guest_path="/Volumes/SystemRoot$run_dir/fixture.input"
	printf 'guest-path=%s\n' "$guest_path" >> "$run_dir/fixture-input.txt"
fi
guest_command="$(python3 -c 'import shlex,sys; print(shlex.join([sys.argv[1], "progress"]))' "$guest_path")" || exit 2

# ---------------------------------------------------------------------------
# One live arm. Records statuses; classifies infrastructure failure as INVALID.
# ---------------------------------------------------------------------------
arm_status=""; arm_invalid=""
run_arm() { # <tag>
	local tag="$1"
	local log="$run_dir/$tag.log"
	local bundle="$log.bundle"
	local rc=0
	runtime_started=1
	"$boot_run" --runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$prefix" --wait "$wait_seconds" \
		--env DSERVER_LOG_LEVEL=debug \
		--env DARLING_SERVER_COURIER_LOG=1 \
		--marker 'PDA result' --status-marker 'PDA_STATUS=' --poll 'PDA end' \
		--log "$log" --bundle "$bundle" --cmd "$guest_command" \
		> "$run_dir/$tag.harness" 2>&1 || rc=$?
	echo "$rc" > "$run_dir/$tag.harness-rc"
	arm_status="rc=$rc"; arm_invalid=""

	local harness_ok=0
	grep -aq 'VERDICT: PASS' "$run_dir/$tag.harness" && harness_ok=1

	# The completed bundle binds this arm to its installed image and raw prefix.
	if ! check_bundle "$tag"; then
		arm_invalid="per-run installed provenance, bundle completion or clean product shutdown missing"
		return 0
	fi
	runtime_started=0
	log="$run_dir/$tag.guest.log"
	if ! python3 "$workspace/scripts/darling-evidence.py" cat "$bundle/harness.log" > "$log"; then
		arm_invalid="unreadable guest output in the completed bundle"
		return 0
	fi
	if ! python3 "$workspace/scripts/darling-evidence.py" cat "$bundle/server.log" > "$run_dir/$tag.server-delta.log"; then
		arm_invalid="per-run server provenance: unreadable run-scoped server log in $bundle"
		return 0
	fi

	local pid
	pid="$(sed -n 's/.*PDA begin mode=[a-z]* pid=\([0-9]*\).*/\1/p' "$log" | tail -1)"
	if [ -z "$pid" ]; then
		if grep -aqE 'Cannot prepare Darling prefix lifecycle|Cannot clear stale shellspawn|runtime prefix state belongs to another prefix|no recognized stable state|did not become ready' \
			"$log" "$run_dir/$tag.harness"; then
			arm_invalid="the prefix did not boot (see $run_dir/$tag.harness and $log)"
		else
			arm_invalid="the workload never started (no PDA begin record)"
		fi
		return 0
	fi
	echo "$pid" > "$run_dir/$tag.pid"

	local status
	status="$(sed -n 's/.*PDA_STATUS=\([0-9-]*\).*/\1/p' "$log" | tail -1)"
	if [ -z "$status" ]; then
		if grep -aqE 'dserver-CRASH|duct-tape panic|dserver-TERMINATE|panic backtrace' "$log" "$run_dir/$tag.server-delta.log"; then
			arm_invalid="the server crashed or panicked during the run (unrelated defect); the workload produced no status"
		else
			arm_status="$arm_status guest_status=absent"
		fi
		return 0
	fi
	arm_status="$arm_status guest_status=$status harness_verdict=$harness_ok"

	if ! grep -aq -- "$pid" "$run_dir/$tag.server-delta.log"; then
		arm_invalid="the run's server delta carries no record for pid $pid (provenance mismatch)"
		return 0
	fi
	return 0
}

# A consumer failure is meaningful only with complete identified evidence and
# clean teardown. Never turn absent workload output into a behavioral RED.
for iteration in $(seq 1 "$repeats"); do
	tag="progress$iteration"
	run_arm "$tag"
	if [ -n "$arm_invalid" ]; then
		echo "PLANE-DEFERRED-ARM: INVALID: $tag: $arm_invalid" >&2
		exit 2
	fi
	status="$(sed -n 's/.*PDA_STATUS=\([0-9-]*\).*/\1/p' "$run_dir/$tag.guest.log" | tail -1)"
	if [ -z "$status" ]; then
		echo "PLANE-DEFERRED-ARM: INVALID: console progress produced no consumer verdict" >&2
		exit 2
	fi
	if [ "$status" != 0 ]; then
		echo "PLANE-DEFERRED-ARM: FAIL: console work did not finish before main release ($arm_status)" >&2
		exit 1
	fi
	if [ "$arm_status" != "rc=0 guest_status=0 harness_verdict=1" ]; then
		echo "PLANE-DEFERRED-ARM: INVALID: successful consumer status without successful harness ($arm_status)" >&2
		exit 2
	fi
	pid="$(cat "$run_dir/$tag.pid")"
	for i in 1 2; do
		if ! grep -aqF "PDA progress-open$i io=1" "$run_dir/$tag.guest.log" ||
		   ! grep -aqF "PDA console-progress pid=$pid open=$i" "$run_dir/$tag.server-delta.log"; then
			echo "PLANE-DEFERRED-ARM: FAIL: console descriptor $i did not deliver its real payload" >&2
			exit 1
		fi
	done
done
echo "PLANE-DEFERRED-ARM: PASS: two real console descriptors delivered before main release (runtime_root=$DARLING_RUNTIME_ROOT, server_sha=$deployed_sha, run-dir=$run_dir)"
exit 0

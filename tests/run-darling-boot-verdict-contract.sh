#!/usr/bin/env bash
# Host contract for the VERDICT RULE in scripts/darling-boot-run.sh: which declaration asserts success.
#
# MEASURED DEFECT this pins (dar-u4mk). `--marker 'XCB_RC='` was a SUBSTRING test, never a parse. The guest's own
# status line said `XCB_RC=132` (128+4 = SIGILL -- the workload had died and no artifact was produced), the
# substring was found, the harness printed `MARKER ok   XCB_RC=` and `VERDICT: PASS`, and the surrounding job
# exited 0 for a workload that died. A declaration that names a VALUE must be parsed as an integer and may pass
# only on zero.
#
# No guest or Darling build is involved. Only the installed launcher is replaced
# by a host payload executor; lifecycle, polling, bundles and verdicts run unchanged.
# Invoke the executable harness directly, including subprocess boundaries below:
# its declared /bin/sh must observe running, stopped and exited producers correctly.
#
# RED ARM (reproducible against the source-base harness, which is what the rule was fixed from):
#   mkdir -p /tmp/verdict-red/scripts
#   git show HEAD:scripts/darling-boot-run.sh > /tmp/verdict-red/scripts/darling-boot-run.sh
#   chmod +x /tmp/verdict-red/scripts/darling-boot-run.sh
#   bash tests/run-darling-boot-verdict-contract.sh --workspace /tmp/verdict-red   # must FAIL, arm 1
#   bash tests/run-darling-boot-verdict-contract.sh                                # must PASS
set -uo pipefail

# Synthetic host captures belong to this fixture, not an enclosing transport job.
unset WEST_JOB_STATE_DIR WEST_JOB_ACTIVE

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
while [ $# -gt 0 ]; do
	case "$1" in
		# The workspace root that holds scripts/darling-boot-run.sh. Exists so a RED proof can point this
		# contract at a source-base copy of the harness instead of faking one here.
		--workspace) workspace_root="$2"; shift 2 ;;
		-h|--help) sed -n '2,/^$/p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
harness="$workspace_root/scripts/darling-boot-run.sh"
[ -x "$harness" ] || { echo "contract: no executable harness at $harness" >&2; exit 2; }

state="${DW_STATE_ROOT:-$workspace_root/../.west-state}/contracts/boot-verdict"
mkdir -p "$state"
tmp="$(mktemp -d "$state/run.XXXXXX")"
cleanup_contract() {
	if [ -n "${prefix:-}" ] && [ -d "$prefix" ]; then
		python3 -B "$(dirname "$0")/../west_commands/test_prefix.py" \
			--prefix "$prefix" --rescue >/dev/null 2>&1 || true
	fi
	rm -rf "$tmp"
}
trap cleanup_contract EXIT
prefix="$tmp/prefix-verdict"
runtime="$tmp/runtime with space"
mkdir -p "$prefix" "$runtime/bin" "$runtime/libexec/darling"
# Only the launcher is stubbed: the harness lifecycle and verdict run unchanged.
# The installed runtime is deliberately outside the writable guest state.
cat > "$runtime/bin/darling" <<'STUB'
#!/bin/sh
case "$1" in
	shell) ;;
	shutdown)
		if [ -n "${DARLING_TEST_SHUTDOWN_MARKER:-}" ]; then
			: > "$DARLING_TEST_SHUTDOWN_MARKER"
		fi
		exit "${DARLING_TEST_SHUTDOWN_RC:-0}" ;;
	*) exit 2 ;;
esac
[ $# -ge 4 ] || { echo "stub launcher: unexpected argv: $*" >&2; exit 2; }
[ "$DPREFIX" = "$DARLING_PREFIX" ] || exit 2
printf '%s\n' "$DPREFIX" > "$DARLING_RUNTIME_ROOT/selected-prefix"
shift		# shell
shift 2		# /bin/bash -c
exec /bin/bash -c "$1"
STUB
chmod 755 "$runtime/bin/darling"
cp "$runtime/bin/darling" "$runtime/bin/darlingserver"

fail=0
n=0
out=""
rc=0
check() { # label expected actual
	if [ "$2" = "$3" ]; then
		echo "ok   $1"
	else
		echo "FAIL $1: expected '$2', got '$3'" >&2
		fail=1
	fi
}
check_nonzero() { # label actual
	if [ "$2" -ne 0 ] 2>/dev/null; then
		echo "ok   $1 (rc=$2)"
	else
		echo "FAIL $1: expected a non-zero exit status, got '$2'" >&2
		fail=1
	fi
}
contains() { case "$1" in *"$2"*) echo yes ;; *) echo no ;; esac; }
verdict_of() { printf '%s\n' "$1" | sed -n 's/^VERDICT: //p' | tail -1; }

# One harness run. stdout: the harness's own console; status: the harness's exit status.
run_harness() { # args...
	n=$((n + 1))
	out="$("$harness" --runtime-root "$runtime" --prefix "$prefix" --log "$tmp/run-$n.log" --no-bundle "$@" 2>&1)"
	rc=$?
	printf '%s' "$out"
	return "$rc"
}

# 1. THE RED ARM. The workload prints its status field carrying a non-zero value -- the guest's own SIGILL status
#    (128+4) -- and the harness must call that FAIL. A source-base harness tests the marker by substring, finds
#    `XCB_RC=` inside `XCB_RC=132`, reports `MARKER ok   XCB_RC=` and exits 0: this assertion is what fails there.
out="$(run_harness --wait 15 --poll 'XCB_RC=' --marker 'XCB_RC=' --cmd 'echo XCB_RC=132')"; rc=$?
check "arm 1: non-zero workload status is FAIL" FAIL "$(verdict_of "$out")"
check_nonzero "arm 1: non-zero workload status exits non-zero" "$rc"

# 2. The same declarations with a zero status: PASS, and exit 0.
out="$(run_harness --wait 15 --poll 'XCB_RC=' --marker 'XCB_RC=' --cmd 'echo XCB_RC=0')"; rc=$?
check "arm 2: zero workload status is PASS" PASS "$(verdict_of "$out")"
check "arm 2: zero workload status exits 0" 0 "$rc"
check "selected installation receives isolated guest prefix" "$prefix" "$(cat "$runtime/selected-prefix")"

# A successful workload cannot hide a failed product shutdown behind rescue.
export DARLING_TEST_SHUTDOWN_RC=7
out="$(run_harness --wait 2 --status-marker 'XCB_RC=' --cmd 'echo XCB_RC=0')"; rc=$?
check "failed product shutdown prevents PASS" FAIL "$(verdict_of "$out")"
check_nonzero "failed product shutdown exits nonzero" "$rc"
unset DARLING_TEST_SHUTDOWN_RC

# 3. The explicit declaration form, with a non-zero value.
out="$(run_harness --wait 15 --status-marker 'XCB_RC=' --cmd 'echo HELLO=1; echo FINAL=1; echo XCB_RC=4')"; rc=$?
check "arm 3: --status-marker with a non-zero value is FAIL" FAIL "$(verdict_of "$out")"
check_nonzero "arm 3: --status-marker with a non-zero value exits non-zero" "$rc"

# 4. The explicit declaration form, with a zero value.
out="$(run_harness --wait 15 --status-marker 'XCB_RC=' --cmd 'echo HELLO=1; echo FINAL=1; echo XCB_RC=0')"; rc=$?
check "arm 4: --status-marker with a zero value is PASS" PASS "$(verdict_of "$out")"
check "arm 4: --status-marker with a zero value exits 0" 0 "$rc"

# 5. Fail closed on an absent status field: the workload never printed it, so nothing proves success.
out="$(run_harness --wait 15 --status-marker 'XCB_RC=' --cmd 'echo HELLO=1; echo FINAL=1')"; rc=$?
check "arm 5: an absent status field is FAIL, never PASS" FAIL "$(verdict_of "$out")"
check_nonzero "arm 5: an absent status field exits non-zero" "$rc"

# 6. Fail closed on an unparseable value: a field that carries something other than an integer.
out="$(run_harness --wait 15 --status-marker 'XCB_RC=' --cmd 'echo HELLO=1; echo FINAL=1; echo XCB_RC=unknown')"; rc=$?
check "arm 6: an unparseable status value is FAIL" FAIL "$(verdict_of "$out")"
check_nonzero "arm 6: an unparseable status value exits non-zero" "$rc"

# 7. Fail closed on an ambiguous declaration: a value is named, but not as a plain literal field. The safe reading
#    is a refusal, because the harness cannot tell which integer the caller meant.
out="$(run_harness --wait 15 --marker 'rc] =' --cmd 'echo "rc] =0"')"; rc=$?
check "arm 7: an ambiguous value-naming declaration is FAIL" FAIL "$(verdict_of "$out")"

# 8. Unchanged semantics for a declaration that names a STRING: presence is the assertion, and it is satisfied.
out="$(run_harness --wait 15 --marker 'PRESENT-OK' --cmd 'echo PRESENT-OK')"; rc=$?
check "arm 8: a presence marker that appears is PASS" PASS "$(verdict_of "$out")"
check "arm 8: a presence marker that appears exits 0" 0 "$rc"

# 9. ... and an absent presence marker is still FAIL/HANG, never PASS.
out="$(run_harness --wait 15 --marker 'PRESENT-OK' --cmd 'echo SOMETHING-ELSE')"; rc=$?
check "arm 9: an absent presence marker is FAIL" FAIL "$(verdict_of "$out")"
check_nonzero "arm 9: an absent presence marker exits non-zero" "$rc"

# 10. A caller that declares nothing at all keeps the behaviour it always had: the default presence markers
#     (HELLO=1, FINAL=1) appear and the harness's own default poll is a STOP CONDITION, not a status field --
#     reading it as one would make every default run ambiguous and fail.
out="$(run_harness --wait 4 --cmd 'echo HELLO=1; echo FINAL=1')"; rc=$?
check "arm 10: the defaults still PASS with no declaration" PASS "$(verdict_of "$out")"
check "arm 10: the defaults still exit 0" 0 "$rc"

# 11. The front door: a status field names a VALUE, never an expected value, and the misuse is refused before any
#     state is consumed rather than misread later.
out="$(run_harness --wait 15 --status-marker 'XCB_RC=0' --cmd 'echo XCB_RC=0')"; rc=$?
check "arm 11: an expected value in --status-marker is a usage error" 2 "$rc"

# 12. A typed status is sufficient: custom workloads need neither fixture markers nor an explicit poll.
out="$(run_harness --wait 15 --status-marker 'COMPILE_RC=' --cmd 'echo COMPILE_RC=0')"; rc=$?
check "arm 12: status-only completion passes without fixture output" PASS "$(verdict_of "$out")"
check "arm 12: status-only completion exits zero" 0 "$rc"
check "arm 12: the product report agrees with successful guest completion" yes "$(contains "$out" "PRODUCT_VERDICT=PASS")"

# 13. Completion requires every declared field, not just the first early success.
out="$(run_harness --wait 15 --status-marker 'BUILD_RC=' --status-marker 'RUN_RC=' --cmd 'echo BUILD_RC=0; sleep 2; echo RUN_RC=0')"; rc=$?
check "arm 13: delayed final status is awaited" PASS "$(verdict_of "$out")"
check "arm 13: all successful statuses exit zero" 0 "$rc"

# 14. A later zero cannot erase an earlier failed status in either verdict.
out="$(run_harness --wait 15 --status-marker 'BUILD_RC=' --status-marker 'RUN_RC=' --cmd 'echo BUILD_RC=9; echo RUN_RC=0')"; rc=$?
check "arm 14: any failed declared status fails the workload" FAIL "$(verdict_of "$out")"
check_nonzero "arm 14: earlier failure exits nonzero" "$rc"
check "arm 14: product report retains the earlier failure" yes "$(contains "$out" "PRODUCT_VERDICT=FAIL")"

# 15. Explicit presence checks remain required alongside a successful status.
out="$(run_harness --wait 15 --status-marker 'COMPILE_RC=' --marker 'REQUIRED-END' --cmd 'echo COMPILE_RC=0')"; rc=$?
check "arm 15: a missing additional oracle is not replaced by status zero" FAIL "$(verdict_of "$out")"
check_nonzero "arm 15: missing additional oracle exits nonzero" "$rc"
check "arm 15: product report retains the missing oracle failure" yes "$(contains "$out" "PRODUCT_VERDICT=FAIL")"

# 16. An explicit poll owns the completion boundary even when a status appears earlier.
out="$(run_harness --wait 15 --poll 'WORK-DONE' --status-marker 'COMPILE_RC=' --marker 'WORK-DONE' --cmd 'echo COMPILE_RC=0; sleep 2; echo WORK-DONE')"; rc=$?
check "arm 16: explicit completion poll takes precedence" PASS "$(verdict_of "$out")"
check "arm 16: explicit completion poll exits zero" 0 "$rc"

# A definite producer failure ends oracle polling promptly, preserving its status
# and cause in a finalized bundle instead of sleeping the full oracle deadline.
started=$SECONDS
out="$("$harness" --runtime-root "$runtime" --prefix "$prefix" \
	--log "$tmp/early.log" --bundle "$tmp/early.bundle" --wait 30 \
	--status-marker 'BUILD_RC=' --cmd 'echo ACTUAL_EARLY_COMPILER_ERROR >&2; exit 23' 2>&1)"; rc=$?
check "early producer exit is preserved" 23 "$rc"
check "early producer failure is FAIL" FAIL "$(verdict_of "$out")"
check "early failure does not consume oracle deadline" yes "$([ $((SECONDS - started)) -lt 25 ] && echo yes || echo no)"
python3 - "$tmp/early.bundle/cleanup.json" <<'PY'
import json, sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text())
assert record["launcher_rc"] == 23 and record["shutdown_rc"] == 0, record
assert record["processes"] == 0 and record["external_rescue"] is False, record
PY
check "early failure cleanup is recorded" 0 "$?"
check "actual producer cause is retained" yes "$(grep -q ACTUAL_EARLY_COMPILER_ERROR "$tmp/early.log" && echo yes || echo no)"

# A live or stopped producer must not turn observation into a blocking wait.
# The stub product shutdown releases these payloads; no external rescue can pass.
# A producer that already exited zero still needs the entire missing-oracle window.
python3 - "$harness" "$runtime" "$prefix" "$tmp" <<'PY'
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

harness, runtime, prefix, scratch = sys.argv[1:]
payload = Path(scratch) / "producer.py"
payload.write_text("""
import json
import os
from pathlib import Path
import signal
import sys
import time

mode, directory = sys.argv[1:]
root = Path(directory)
shutdown = root / "shutdown"
if mode == "stopped":
    # Retain our own handle before forking; never reopen a PID to signal it.
    producer = os.getpid()
    handle = os.pidfd_open(producer)
    reader, writer = os.pipe()
    helper = os.fork()
    if helper == 0:
        os.close(reader)
        os.setpgrp()
        os.write(writer, b"ready")
        os.close(writer)
        while True:
            try:
                stopped = (Path("/proc") / str(producer) / "stat").read_text().rsplit(")", 1)[1].split()[0] == "T"
            except FileNotFoundError:
                os._exit(0)
            if shutdown.exists() and stopped:
                break
            time.sleep(0.01)
        signal.pidfd_send_signal(handle, signal.SIGCONT)
        os._exit(0)
    os.close(writer)
    assert os.read(reader, 5) == b"ready"
    os.close(reader)
    # GNU timeout owns this group, not the harness or its lifecycle owner.
    assert os.getpgrp() == os.getppid()
(root / "started.json").write_text(json.dumps({
    "producer": os.getpid(), "launcher": os.getppid(), "time": time.time()
}))
if mode == "stopped":
    os.kill(0, signal.SIGSTOP)
    os.kill(0, signal.SIGCONT)
    os.waitpid(helper, 0)
    os.close(handle)
elif mode == "running" or mode.startswith("cancel-"):
    while not shutdown.exists():
        time.sleep(0.01)
sys.exit(0 if mode == "zero" else 23)
""")

def state(pid):
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return None

for mode in ("zero", "running", "stopped", "cancel-term", "cancel-int"):
    root = Path(scratch) / mode
    root.mkdir()
    shutdown = root / "shutdown"
    process = subprocess.Popen(
        [harness, "--runtime-root", runtime, "--prefix", prefix,
         "--log", str(root / "run.log"), "--no-bundle", "--wait",
         "30" if mode.startswith("cancel-") else "3",
         "--status-marker", "NEVER_RC=", "--cmd",
         shlex.join([sys.executable, str(payload), mode, str(root)])],
        env=dict(os.environ, DARLING_TEST_SHUTDOWN_MARKER=str(shutdown)),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            started = root / "started.json"
            if started.exists():
                try:
                    record = json.loads(started.read_text())
                except json.JSONDecodeError:
                    continue
                if mode != "stopped" or (
                    state(record["producer"]) == "T" and state(record["launcher"]) == "T"
                ):
                    break
            assert process.poll() is None, f"{mode}: harness exited before producer observation"
            assert time.monotonic() < deadline, f"{mode}: producer did not reach its declared state"
            time.sleep(0.01)
        if mode == "running":
            assert state(record["producer"]) not in (None, "Z", "T"), record
            assert state(record["launcher"]) not in (None, "Z", "T"), record
        if mode.startswith("cancel-"):
            process.send_signal(signal.SIGTERM if mode == "cancel-term" else signal.SIGINT)
        output, _ = process.communicate(timeout=15)
    except BaseException:
        # Release only this fixture on failure, then reap; this path cannot pass.
        shutdown.touch()
        output, _ = process.communicate(timeout=15)
        print(output, file=sys.stderr)
        raise
    if mode.startswith("cancel-"):
        assert process.returncode == (143 if mode == "cancel-term" else 130), (mode, process.returncode, output)
    else:
        assert process.returncode == (1 if mode == "zero" else 23), (mode, process.returncode, output)
        assert "VERDICT: FAIL" in output, (mode, output)
    observed = shutdown.stat().st_mtime - record["time"]
    assert (0 <= observed < 10) if mode.startswith("cancel-") else (2.5 <= observed < 10), (mode, observed, output)
    assert state(record["producer"]) is None, (mode, "unreaped producer", record)
    assert state(record["launcher"]) is None, (mode, "unreaped launcher", record)
PY
check "declared shell preserves producer states, status and oracle deadline" 0 "$?"

# A real host process retains the prefix capability beyond a successful producer.
# Its pidfd rescue may make the prefix idle, never make the product verdict PASS.
cp /bin/sleep "$runtime/bin/mldr"
out="$("$harness" --runtime-root "$runtime" --prefix "$prefix" \
	--log "$tmp/rescue.log" --bundle "$tmp/rescue.bundle" --wait 5 \
	--status-marker 'BUILD_RC=' \
	--cmd '"$DARLING_RUNTIME_ROOT/bin/mldr" 60 3<"$DPREFIX" >/dev/null 2>&1 & echo BUILD_RC=0' 2>&1)"; rc=$?
check_nonzero "forced rescue exits nonzero" "$rc"
check "forced rescue is FAIL" FAIL "$(verdict_of "$out")"
python3 - "$tmp/rescue.bundle/cleanup.json" <<'PY'
import json, sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text())
assert record["shutdown_rc"] == 0, record
assert record["state"] == "rescued" and record["external_rescue"] is True, record
assert record["processes"] == 0 and record["rescue_rc"] == 3, record
PY
check "forced rescue remains classified after zero residue" 0 "$?"
check "forced rescue never announces product PASS" no "$(contains "$out" "PRODUCT_VERDICT=PASS")"

# Marker-only residue blocks boot, but cannot grant authority to signal it.
python3 - "$harness" "$runtime" "$prefix" "$tmp" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
harness, runtime, prefix, root = sys.argv[1:]
process = subprocess.Popen([str(Path(runtime) / "bin/mldr"), "60"],
                           env=dict(os.environ, DPREFIX=prefix, DARLING_PREFIX=prefix))
try:
    result = subprocess.run(
        [harness, "--runtime-root", runtime, "--prefix", prefix,
         "--log", str(Path(root) / "marker-refusal.log"), "--no-bundle",
         "--wait", "1", "--cmd", "echo MUST_NOT_RUN"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0, result.stdout
    assert "== run ==" not in result.stdout, result.stdout
    assert process.poll() is None, "marker-only residue was signalled"
finally:
    process.terminate()
    process.wait(timeout=5)
PY
check "marker-only candidate blocks boot without being killed" 0 "$?"

if [ "$fail" -eq 0 ]; then
	echo "darling-boot-verdict: PASS"
else
	echo "darling-boot-verdict: FAIL" >&2
fi
exit "$fail"

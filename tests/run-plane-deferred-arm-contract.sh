#!/usr/bin/env bash
# run-plane-deferred-arm-contract.sh -- RED/GREEN oracle for the management-plane
# "refusal without disarm" defect (Thread::dispatchOrQueuePlaneCall) and its correction.
#
# THE PHASES (per request: tid = pid for the plane's non-retargetable op, callnum 16)
#
#   P1 initial temporary arming   setPendingCall-parked ... number=16 ... pending=0
#                                 The product fix does NOT remove this: `callFromMessage`
#                                 arms the Call before the dispatcher sees it. A correct
#                                 fix still produces this record.
#   P2 dispatch refusal           (no existing record; the pass refuses because the target
#                                 is parked / running)
#   P3 disarming of the refused   (no existing record unless the product adds
#                                 `plane-call-disarmed`)
#   P4 retention after refusal    setPendingCall-parked ... number=16 ... pending=1
#                                 A later publication finds the slot occupied. THIS is the
#                                 observable consequence of a refusal without disarm.
#   P5 deferred-queue insertion   call-deferred site=doWork-parked ... number=16
#   P6 late execution             (no existing record; the deferred Call runs out of band)
#   P7 completion publication     a deferred Call's suppressed reply is dropped when
#                                 `planeCompletion().armed()` is false: `plane-call-unowned`
#                                 (product diagnostic); `plane-deferred-completion` records
#                                 a deferred Call that DID publish.
#
# RED (unmodified server):  P1 >= 1, P4 >= 1, P5 >= 1, the rejection record >= 1,
#                           P7 `plane-deferred-completion` == 0, `plane-call-unowned` == 0
#                           (it has no diagnostic in the unmodified build).
# GREEN (disarm-on-refusal): P1 >= 1 (still armed - NOT asserted zero),
#                           P4 == 0, rejection == 0, P5 == 0,
#                           product oracle only: `plane-call-disarmed` >= 1 and
#                           `plane-call-unowned` == 0.
#
# CORRELATION is by request identity: the run's own `pid` (printed by the fixture, equal to
# the main thread's tid) plus the call number. Aggregate counts alone are not accepted: every
# positive assertion names the record and the pid. The rejection record carries no pid in
# the unmodified build (`call.cpp`), so it is counted from the run-scoped server delta and
# the handoff asks the product agent to add `tid=` to it.
#
# MODES
#   live     (default) boot a private prefix, run the fixture, analyse the server delta.
#   --analyse FILE --pid N    analyse an existing server-delta file (offline oracle
#                             self-test; no boot, no build).
#
# EXIT CODES
#   0 PASS   the expected phase signature was observed
#   1 FAIL   the workload ran and the signature does not match (product verdict)
#   2 INVALID infrastructure could not produce a trustworthy run (boot failure, missing
#            provenance, changed server binary, crash unrelated to the defect).
#            An infrastructure failure is NEVER reported as PASS.
#
# ISOLATION (enforced)
#   * refuses a prefix matching prefix-verify14 (shared with the active Xcode workload)
#   * refuses the canonical shared prefix-ios
#   * prints the raw guest prefix and installed R/bin/darlingserver sha256 in the verdict
#   * never deploys; boots only the named prefix/lane and uses the harness's prefix-scoped
#     cleanup
#
# USAGE
#   run-plane-deferred-arm-contract.sh --runtime-root R --prefix P [--fixture F]
#       [--expect red|green] [--oracle existing|product] [--repeat N]
#       [--expected-server-sha SHA] [--run-dir D] [--wait S]
#   Without --fixture, compiles the retained C source inside Darling using the
#   explicitly provisioned darling-command-line-tools prerequisite, never a host
#   cross compiler. --fixture accepts an already built Mach-O workload input.
#   run-plane-deferred-arm-contract.sh --analyse FILE --pid N --expect red|green
#       [--oracle existing|product]

set -uo pipefail

original_args=("$@")
prefix=""
runtime_root=""
fixture=""
run_dir=""
expect="red"
oracle="existing"
repeats=1
wait_seconds=90
expected_sha=""
analyse=""
analyse_pid=""
name="plane_deferred_arm_guest"

while [ $# -gt 0 ]; do
	case "$1" in
	--prefix) prefix="$2"; shift 2 ;;
	--runtime-root) runtime_root="$2"; shift 2 ;;
	--fixture) fixture="$2"; shift 2 ;;
	--run-dir) run_dir="$2"; shift 2 ;;
	--expect) expect="$2"; shift 2 ;;
	--oracle) oracle="$2"; shift 2 ;;
	--repeat) repeats="$2"; shift 2 ;;
	--expected-server-sha) expected_sha="$2"; shift 2 ;;
	--wait) wait_seconds="$2"; shift 2 ;;
	--analyse) analyse="$2"; shift 2 ;;
	--pid) analyse_pid="$2"; shift 2 ;;
	-h|--help)
		cat <<'HELP'
Usage: run-plane-deferred-arm-contract.sh --runtime-root R --prefix RAW_P
       [--fixture MACH_O] [--expect red|green] [--oracle existing|product]
       [--repeat N] [--expected-server-sha SHA] [--run-dir D] [--wait S]
       run-plane-deferred-arm-contract.sh --analyse FILE --pid N
       [--expect red|green] [--oracle existing|product]

R is one ordinary immutable installation, P is separate prepared guest state.
Without --fixture, guest compilation requires darling-command-line-tools;
provision it explicitly with west darling-bootstrap --build-dir B --runtime-root R
--prefix P --no-build --guest-toolchain darling-command-line-tools.
Evidence is retained outside /tmp; --run-dir must be absent or empty.
The same phase/request-identity oracle serves independent RED and GREEN images.
HELP
		exit 0 ;;
	*) echo "plane-deferred-arm: unknown argument: $1" >&2; exit 2 ;;
	esac
done

case "$expect" in red|green) ;; *) echo "plane-deferred-arm: --expect red|green" >&2; exit 2 ;; esac
case "$oracle" in existing|product) ;; *) echo "plane-deferred-arm: --oracle existing|product" >&2; exit 2 ;; esac
if [ "$expect" = green ] && [ "$oracle" != product ]; then
	echo "plane-deferred-arm: INVALID: GREEN acceptance requires --oracle product: an absent diagnostic must never be read as zero violations" >&2
	exit 2
fi
case "$repeats" in ''|*[!0-9]*) echo "plane-deferred-arm: --repeat must be a positive integer" >&2; exit 2 ;; esac
[ "$repeats" -ge 1 ] || { echo "plane-deferred-arm: --repeat must be >= 1" >&2; exit 2; }

here="$(cd "$(dirname "$0")" && pwd)"
workspace="$(cd "$here/.." && pwd)"

# ---------------------------------------------------------------------------
# Oracle (pure log analysis; shared by live and offline modes)
# ---------------------------------------------------------------------------
P1=0; P4=0; REJECT=0; P5=0; PDEFERDONE=0; DISARMED=0; UNOWNED=0

analyse_delta() { # <delta-file> <pid> <oracle>
	local delta="$1" pid="$2" orc="$3"
	local who="tid=$pid"
	[ -f "$delta" ] || return 1
	# Normal product diagnostics include their bounded per-thread n= ordinal.
	# Saved deltas without the ordinal retain the identical phase/PID semantics.
	P1=$(grep -acE "call-deferred (n=[0-9]+ )?site=setPendingCall-parked .*${who} number=16 .*pending=0" "$delta" || true)
	P4=$(grep -acE "call-deferred (n=[0-9]+ )?site=setPendingCall-parked .*${who} number=16 .*pending=1" "$delta" || true)
	REJECT=$(grep -acE 'setPendingCall rejected call \(number 16\)' "$delta" || true)
	P5=$(grep -acE "call-deferred (n=[0-9]+ )?site=doWork-parked .*${who} number=16" "$delta" || true)
	PDEFERDONE=$(grep -acE 'plane-deferred-completion' "$delta" || true)
	DISARMED=$(grep -acE "plane-call-disarmed .*${who} .*call=16" "$delta" || true)
	# Request-sequence correlation (server side). A disarm names the plane sequence it refused; a
	# completion names the sequence it executed. A refused sequence that later appears as an execution
	# is a Call that ran after being refused -- the defect this gate exists to catch.
	DISARM_SEQS=$(sed -nE "s/.*plane-call-disarmed .*${who} .*seq=([0-9]+).*call=16.*/\1/p" "$delta" | sort -un | tr '\n' ' ')
	EXEC_SEQS=$(sed -nE 's/.*plane-deferred-completion .*seq=([0-9]+).*/\1/p' "$delta" | sort -un | tr '\n' ' ')
	EXEC_AFTER_REFUSAL=0
	for sq in $DISARM_SEQS; do
		case " $EXEC_SEQS " in *" $sq "*) EXEC_AFTER_REFUSAL=$((EXEC_AFTER_REFUSAL + 1)) ;; esac
	done
	UNOWNED=$(grep -acE 'plane-call-unowned .*call=16' "$delta" || true)
	if [ "$orc" = product ]; then
		# the product oracle requires the diagnostics to be present in the build:
		# a green verdict with zero `plane-call-disarmed` is "the instrument is absent",
		# not "the Call was never armed". The caller treats that as INVALID.
		:
	fi
	return 0
}

report_phases() {
	echo "PLANE-DEFERRED-ARM: phases pid=$1 P1_arm_pending0=$P1 P4_retained_pending1=$P4" \
	     "refused_rejections=$REJECT P5_deferred_insert=$P5" \
	     "P7_deferred_completion=$PDEFERDONE disarmed=$DISARMED unowned=$UNOWNED" \
	     "disarmed_seqs=[$DISARM_SEQS] executed_seqs=[$EXEC_SEQS] executed_after_refusal=$EXEC_AFTER_REFUSAL"
}

# Signature verdict for one analysed run. Sets VERDICT (PASS|FAIL) and WHY.
VERDICT=""; WHY=""
judge() { # <expect> <oracle>
	local exp="$1" orc="$2"
	if [ "$exp" = red ]; then
		if [ "$P1" -lt 1 ]; then
			VERDICT=FAIL; WHY="no initial arm record (pending=0) for callnum 16 on this pid: the reproducer did not arm the plane Call on the parked thread"
		elif [ "$P4" -lt 1 ]; then
			VERDICT=FAIL; WHY="no retained-slot record (pending=1): the refusal disarmed (or never armed) the Call; this is not the defective server"
		elif [ "$REJECT" -lt 1 ]; then
			VERDICT=FAIL; WHY="no re-publication met the retained slot: no setPendingCall rejection for callnum 16"
		elif [ "$P5" -lt 1 ]; then
			VERDICT=FAIL; WHY="no doWork-parked deferral for callnum 16: the retained Call did not reach the deferred queue"
		elif [ "$PDEFERDONE" -ne 0 ]; then
			VERDICT=FAIL; WHY="a plane-deferred-completion record exists: the deferred Call had a completion owner"
		else
			VERDICT=PASS; WHY="defective lifecycle observed (arm-on-parked, retention, rejection, deferred insertion, no owned completion)"
		fi
	else
		if [ "$P1" -lt 1 ]; then
			VERDICT=FAIL; WHY="no initial arm record (pending=0): the fixed path should still arm before refusing (the fix disarms, it does not stop arming)"
		elif [ "$P4" -ne 0 ]; then
			VERDICT=FAIL; WHY="the pending slot retained a refused Call (pending=1): the disarm did not happen"
		elif [ "$REJECT" -ne 0 ]; then
			VERDICT=FAIL; WHY="a publication was rejected because the pending slot was occupied: the refused Call was retained"
		elif [ "$P5" -ne 0 ]; then
			VERDICT=FAIL; WHY="a refused Call was inserted into the deferred queue: it can execute later"
		elif [ "$PDEFERDONE" -ne 0 ]; then
			VERDICT=FAIL; WHY="a deferred completion was published for a refused Call"
		elif [ "$orc" = product ] && [ "$DISARMED" -lt 1 ]; then
			VERDICT=INVALID; WHY="product oracle requested but the deployed build has no plane-call-disarmed record: the GREEN ownership assertion cannot be evaluated"
		elif [ "$orc" = product ] && [ -z "$DISARM_SEQS" ]; then
			VERDICT=INVALID; WHY="product oracle: no disarmed request sequence was recorded for callnum 16, so server-side exactly-once cannot be correlated"
		elif [ "$EXEC_AFTER_REFUSAL" -ne 0 ]; then
			VERDICT=FAIL; WHY="a refused request sequence later executed (disarmed_seqs=[$DISARM_SEQS] executed_seqs=[$EXEC_SEQS]): the refused Call ran out of band"
		elif [ "$orc" = product ] && [ "$UNOWNED" -ne 0 ]; then
			VERDICT=FAIL; WHY="a reply-suppressed Call completed without a completion owner (plane-call-unowned): completion ownership is wrong"
		else
			VERDICT=PASS; WHY="fixed lifecycle observed (arm, no retention, no rejection, no deferred insertion, no unowned completion)"
		fi
	fi
}

# ---------------------------------------------------------------------------
# Offline mode: analyse a saved server delta (no boot, no build).
# ---------------------------------------------------------------------------
if [ -n "$analyse" ]; then
	[ -n "$analyse_pid" ] || { echo "plane-deferred-arm: --analyse needs --pid" >&2; exit 2; }
	analyse_delta "$analyse" "$analyse_pid" "$oracle" || {
		echo "PLANE-DEFERRED-ARM: INVALID: cannot read $analyse" >&2; exit 2; }
	report_phases "$analyse_pid"
	judge "$expect" "$oracle"
	case "$VERDICT" in
	PASS) echo "PLANE-DEFERRED-ARM: PASS ($expect/$oracle): $WHY"; exit 0 ;;
	INVALID) echo "PLANE-DEFERRED-ARM: INVALID ($expect/$oracle): $WHY" >&2; exit 2 ;;
	*) echo "PLANE-DEFERRED-ARM: FAIL ($expect/$oracle): $WHY" >&2; exit 1 ;;
	esac
fi

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
if [ "$oracle" = product ]; then
	for witness in plane-call-disarmed plane-call-unowned; do
		if ! grep -aqF -- "$witness" "$DARLING_RUNTIME_ROOT/bin/darlingserver"; then
			echo "PLANE-DEFERRED-ARM: INVALID: the deployed darlingserver carries no '$witness' literal: the product oracle cannot be evaluated, and an absent instrument is not a zero" >&2
			exit 2
		fi
	done
fi
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
Path(sys.argv[2]).write_text(f"path={path.absolute()}\nsha256={hashlib.sha256(payload).hexdigest()}\n")
Path(sys.argv[3]).write_bytes(payload)
PY
	[ "$?" -eq 0 ] || exit 2
	. "$workspace/testkit/scripts/darling-guest-shell.sh"
	runtime_started=1
	darling_guest_shell "$DARLING_LAUNCHER" "$prefix" "$wait_seconds" \
		"mkdir -p /private/var/tmp && cat > '$guest_path' && chmod 755 '$guest_path'" \
		< "$run_dir/fixture.input" > "$run_dir/upload.log" 2>&1 8<&-
	upload_rc=$?
	"$workspace/scripts/prefix-cleanup.sh" --runtime-root "$DARLING_RUNTIME_ROOT" \
		--prefix "$prefix" > "$run_dir/upload-cleanup.log" 2>&1
	cleanup_rc=$?
	if [ "$upload_rc" -ne 0 ] || [ "$cleanup_rc" -ne 0 ]; then
		echo "PLANE-DEFERRED-ARM: INVALID: fixture upload rc=$upload_rc shutdown rc=$cleanup_rc; see $run_dir/upload.log and upload-cleanup.log" >&2
		exit 2
	fi
	runtime_started=0
fi

# ---------------------------------------------------------------------------
# One live arm. Records statuses; classifies infrastructure failure as INVALID.
# ---------------------------------------------------------------------------
arm_status=""; arm_invalid=""
run_arm() { # <mode> <tag>
	local mode="$1" tag="$2"
	local log="$run_dir/$tag.log"
	local bundle="$log.bundle"
	local rc=0
	runtime_started=1
	"$boot_run" --runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$prefix" --wait "$wait_seconds" \
		--env DSERVER_LOG_LEVEL=debug \
		--marker 'PDA result' --status-marker 'PDA_STATUS=' --poll 'PDA end' \
		--log "$log" --bundle "$bundle" --cmd "$guest_path $mode" \
		> "$run_dir/$tag.harness" 2>&1 || rc=$?
	echo "$rc" > "$run_dir/$tag.harness-rc"
	arm_status="rc=$rc"; arm_invalid=""
	if [ "$rc" -ne 0 ]; then
		arm_invalid="the harness exited rc=$rc (a VERDICT marker alone is not a successful harness exit; see $run_dir/$tag.harness)"
		return 0
	fi

	local harness_ok=0
	grep -aq 'VERDICT: PASS' "$run_dir/$tag.harness" && harness_ok=1
	if [ "$harness_ok" -ne 1 ]; then
		if grep -aqE 'VERDICT: FAIL|VERDICT: INVALID' "$run_dir/$tag.harness"; then
			arm_invalid="the harness did not complete successfully (VERDICT: PASS absent; see $run_dir/$tag.harness)"
		else
			arm_invalid="the harness never emitted a verdict (run truncated; see $run_dir/$tag.harness)"
		fi
		return 0
	fi

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

# ---------------------------------------------------------------------------
# Control arm (once): without a parked main thread, both opens must succeed.
# ---------------------------------------------------------------------------
FAILED=0; INVALID=0
run_arm control control
if [ -n "$arm_invalid" ]; then
	echo "PLANE-DEFERRED-ARM: INVALID: control arm: $arm_invalid" >&2; exit 2
fi
ctl_status="$(sed -n 's/.*PDA_STATUS=\([0-9-]*\).*/\1/p' "$run_dir/control.guest.log" | tail -1)"
if [ "$ctl_status" != "0" ]; then
	echo "PLANE-DEFERRED-ARM: INVALID: control arm guest status=${ctl_status:-absent} ($arm_status): the environment cannot serve the control, so no RED/GREEN claim is possible" >&2
	exit 2
fi
echo "PLANE-DEFERRED-ARM: ok: control arm ($arm_status)"

# ---------------------------------------------------------------------------
# Park arm(s).
# ---------------------------------------------------------------------------
for i in $(seq 1 "$repeats"); do
	tag="park$i"; [ "$repeats" -eq 1 ] && tag="park"
	run_arm park "$tag"
	if [ -n "$arm_invalid" ]; then
		echo "PLANE-DEFERRED-ARM: INVALID: $tag: $arm_invalid" >&2; exit 2
	fi
	status="$(sed -n 's/.*PDA_STATUS=\([0-9-]*\).*/\1/p' "$run_dir/$tag.guest.log" | tail -1)"
	if [ "$status" != "0" ]; then
		echo "PLANE-DEFERRED-ARM: FAIL: $tag guest status=${status:-absent} ($arm_status): the fixture did not complete with both opens succeeding" >&2
		exit 1
	fi
	opens1="$(grep -acE '^PDA open1 fd=' "$run_dir/$tag.guest.log" || true)"
	opens2="$(grep -acE '^PDA open2 fd=' "$run_dir/$tag.guest.log" || true)"
	if [ "${opens1:-0}" -ne 1 ] || [ "${opens2:-0}" -ne 1 ]; then
		echo "PLANE-DEFERRED-ARM: FAIL: $tag: the two requests did not each produce exactly one outcome (open1=$opens1 open2=$opens2): exactly-once is not established" >&2
		exit 1
	fi
	pid="$(cat "$run_dir/$tag.pid")"
	analyse_delta "$run_dir/$tag.server-delta.log" "$pid" "$oracle"
	report_phases "$pid"
	judge "$expect" "$oracle"
	echo "PLANE-DEFERRED-ARM: $VERDICT: $tag: $WHY"
	case "$VERDICT" in
	PASS) ;;
	INVALID) INVALID=1 ;;
	*) FAILED=1 ;;
	esac
done

if [ "$INVALID" -ne 0 ]; then
	echo "PLANE-DEFERRED-ARM: VERDICT INVALID (expect=$expect, oracle=$oracle, runtime_root=$DARLING_RUNTIME_ROOT, prefix=$prefix, server_sha=$deployed_sha, run-dir=$run_dir)" >&2
	exit 2
fi
if [ "$FAILED" -ne 0 ]; then
	echo "PLANE-DEFERRED-ARM: VERDICT FAIL (expect=$expect, oracle=$oracle, runtime_root=$DARLING_RUNTIME_ROOT, prefix=$prefix, server_sha=$deployed_sha, run-dir=$run_dir)" >&2
	exit 1
fi
echo "PLANE-DEFERRED-ARM: VERDICT PASS (expect=$expect, oracle=$oracle, runtime_root=$DARLING_RUNTIME_ROOT, prefix=$prefix, server_sha=$deployed_sha, run-dir=$run_dir)"
exit 0

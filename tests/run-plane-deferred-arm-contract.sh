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
#   * prints the prefix identity and the deployed darlingserver sha256 in the verdict
#   * never deploys; boots only the named prefix/lane and uses the harness's prefix-scoped
#     cleanup
#
# USAGE
#   run-plane-deferred-arm-contract.sh --prefix P [--build-dir B] [--fixture F]
#       [--expect red|green] [--oracle existing|product] [--repeat N]
#       [--expected-server-sha SHA] [--run-dir D] [--wait S]
#   run-plane-deferred-arm-contract.sh --analyse FILE --pid N --expect red|green
#       [--oracle existing|product]

set -uo pipefail

prefix=""
build_dir=""
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
	--build-dir) build_dir="$2"; shift 2 ;;
	--fixture) fixture="$2"; shift 2 ;;
	--run-dir) run_dir="$2"; shift 2 ;;
	--expect) expect="$2"; shift 2 ;;
	--oracle) oracle="$2"; shift 2 ;;
	--repeat) repeats="$2"; shift 2 ;;
	--expected-server-sha) expected_sha="$2"; shift 2 ;;
	--wait) wait_seconds="$2"; shift 2 ;;
	--analyse) analyse="$2"; shift 2 ;;
	--pid) analyse_pid="$2"; shift 2 ;;
	-h|--help) sed -n '2,60p' "$0"; exit 0 ;;
	*) echo "plane-deferred-arm: unknown argument: $1" >&2; exit 2 ;;
	esac
done

case "$expect" in red|green) ;; *) echo "plane-deferred-arm: --expect red|green" >&2; exit 2 ;; esac
case "$oracle" in existing|product) ;; *) echo "plane-deferred-arm: --oracle existing|product" >&2; exit 2 ;; esac
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
	P1=$(grep -acE "call-deferred site=setPendingCall-parked .*${who} number=16 .*pending=0" "$delta" || true)
	P4=$(grep -acE "call-deferred site=setPendingCall-parked .*${who} number=16 .*pending=1" "$delta" || true)
	REJECT=$(grep -acE 'setPendingCall rejected call \(number 16\)' "$delta" || true)
	P5=$(grep -acE "call-deferred site=doWork-parked .*${who} number=16" "$delta" || true)
	PDEFERDONE=$(grep -acE 'plane-deferred-completion' "$delta" || true)
	DISARMED=$(grep -acE "plane-call-disarmed .*${who} .*call=16" "$delta" || true)
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
	     "P7_deferred_completion=$PDEFERDONE disarmed=$DISARMED unowned=$UNOWNED"
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
[ -n "$prefix" ] && [ -x "$prefix/bin/darling" ] || {
	echo "plane-deferred-arm: INVALID: --prefix must name a booted Darling prefix" >&2; exit 2; }
case "$prefix" in
	*prefix-verify14*)
		echo "plane-deferred-arm: INVALID: refusing prefix-verify14: it is shared with the active Xcode workload (the previous gate run interfered with it)" >&2
		exit 2 ;;
	*/prefix-ios|*/prefix-ios/)
		echo "plane-deferred-arm: INVALID: refusing the canonical shared prefix-ios" >&2
		exit 2 ;;
esac
[ -f "$src" ] || { echo "plane-deferred-arm: INVALID: missing fixture source $src" >&2; exit 2; }

if [ -z "$run_dir" ]; then
	run_dir="$(mktemp -d "${TMPDIR:-/tmp}/plane-deferred-arm.XXXXXX")"
fi
mkdir -p "$run_dir"

deployed_sha="$(sha256sum "$prefix/bin/darlingserver" 2>/dev/null | cut -d' ' -f1)"
if [ -n "$expected_sha" ] && [ "$deployed_sha" != "$expected_sha" ]; then
	echo "PLANE-DEFERRED-ARM: INVALID: deployed darlingserver sha256=$deployed_sha, expected $expected_sha: the runtime changed since the RED claim was established" >&2
	exit 2
fi

# ---------------------------------------------------------------------------
# Build the guest fixture without adding a product target.
# ---------------------------------------------------------------------------
if [ -z "$fixture" ]; then
	[ -n "$build_dir" ] || { echo "plane-deferred-arm: INVALID: need --fixture or --build-dir" >&2; exit 2; }
	[ -f "$build_dir/build.ninja" ] || { echo "plane-deferred-arm: INVALID: $build_dir is not a ninja build dir" >&2; exit 2; }
	obj="$run_dir/$name.o"; out="$run_dir/$name"
	cc_line="$(ninja -C "$build_dir" -t commands ring_mach_msg_test 2>/dev/null | grep 'ring_mach_msg_test.c.o' | head -1)"
	link_line="$(ninja -C "$build_dir" -t commands ring_mach_msg_test 2>/dev/null | tail -1)"
	[ -n "$cc_line" ] && [ -n "$link_line" ] || {
		echo "plane-deferred-arm: INVALID: cannot extract the guest toolchain commands from $build_dir" >&2; exit 2; }
	cc_new="$(printf '%s' "$cc_line" \
		| sed -e 's#-MD -MT [^ ]*\.o -MF [^ ]*\.o\.d ##' \
		      -e "s#-o [^ ]*ring_mach_msg_test\.c\.o#-o $obj#" \
		      -e "s#-c .*ring_mach_msg_test\.c#-c $src#")"
	link_new="$(printf '%s' "$link_line" \
		| sed -e "s#[^ ]*ring_mach_msg_test\.c\.o#$obj#" \
		      -e "s#-o [^ ]*ring_mach_msg_test#-o $out#")"
	( cd "$build_dir" && eval "$cc_new" ) || { echo "plane-deferred-arm: INVALID: fixture compile failed" >&2; exit 2; }
	( cd "$build_dir" && eval "$link_new" ) || { echo "plane-deferred-arm: INVALID: fixture link failed" >&2; exit 2; }
	fixture="$out"
fi
[ -x "$fixture" ] || { echo "plane-deferred-arm: INVALID: fixture not executable: $fixture" >&2; exit 2; }

guest_path="/private/var/tmp/$name"
install -m755 "$fixture" "$prefix$guest_path" || {
	echo "plane-deferred-arm: INVALID: cannot install the fixture into $prefix" >&2; exit 2; }

# ---------------------------------------------------------------------------
# One live arm. Records statuses; classifies infrastructure failure as INVALID.
# ---------------------------------------------------------------------------
arm_status=""; arm_invalid=""
run_arm() { # <mode> <tag>
	local mode="$1" tag="$2"
	local log="$run_dir/$tag.log" bundle="$log.bundle"
	local rc=0
	"$boot_run" --prefix "$prefix" --wait "$wait_seconds" \
		--marker 'PDA result' --status-marker 'PDA status=' --poll 'PDA end' \
		--log "$log" --bundle "$bundle" --cmd "$guest_path $mode" \
		> "$run_dir/$tag.harness" 2>&1 || rc=$?
	echo "$rc" > "$run_dir/$tag.harness-rc"
	arm_status="rc=$rc"; arm_invalid=""

	local harness_ok=0
	grep -aq 'VERDICT: PASS' "$run_dir/$tag.harness" && harness_ok=1

	# Provenance: the run must be served by the prefix we named.
	if ! grep -aq "RUN-ENV prefix=$prefix" "$run_dir/$tag.harness"; then
		arm_invalid="per-run server provenance: the harness did not name $prefix in RUN-ENV"
		return 0
	fi
	if [ ! -f "$bundle/server.pre.log" ] || [ ! -f "$bundle/server.post.log" ]; then
		arm_invalid="per-run server provenance: no server.pre/post.log pair in $bundle"
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
	status="$(sed -n 's/.*PDA status=\([0-9-]*\).*/\1/p' "$log" | tail -1)"
	if [ -z "$status" ]; then
		if grep -aqE 'dserver-CRASH|duct-tape panic|dserver-TERMINATE|panic backtrace' "$log" "$bundle/server.post.log"; then
			arm_invalid="the server crashed or panicked during the run (unrelated defect); the workload produced no status"
		else
			arm_status="$arm_status guest_status=absent"
		fi
		return 0
	fi
	arm_status="$arm_status guest_status=$status harness_verdict=$harness_ok"

	# The server delta for this run: post minus the pre line count.
	local pre
	pre="$(wc -l < "$bundle/server.pre.log")"
	tail -n "+$((pre + 1))" "$bundle/server.post.log" > "$run_dir/$tag.server-delta.log"
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
ctl_status="$(sed -n 's/.*PDA status=\([0-9-]*\).*/\1/p' "$run_dir/control.log" | tail -1)"
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
	status="$(sed -n 's/.*PDA status=\([0-9-]*\).*/\1/p' "$run_dir/$tag.log" | tail -1)"
	if [ "$status" != "0" ]; then
		echo "PLANE-DEFERRED-ARM: FAIL: $tag guest status=${status:-absent} ($arm_status): the fixture did not complete with both opens succeeding" >&2
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
	echo "PLANE-DEFERRED-ARM: VERDICT INVALID (expect=$expect, oracle=$oracle, prefix=$prefix, server_sha=$deployed_sha, run-dir=$run_dir)" >&2
	exit 2
fi
if [ "$FAILED" -ne 0 ]; then
	echo "PLANE-DEFERRED-ARM: VERDICT FAIL (expect=$expect, oracle=$oracle, prefix=$prefix, server_sha=$deployed_sha, run-dir=$run_dir)" >&2
	exit 1
fi
echo "PLANE-DEFERRED-ARM: VERDICT PASS (expect=$expect, oracle=$oracle, prefix=$prefix, server_sha=$deployed_sha, run-dir=$run_dir)"
exit 0

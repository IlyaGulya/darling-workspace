#!/usr/bin/env bash
# Host contract for the VERDICT RULE in scripts/darling-boot-run.sh: which declaration asserts success.
#
# MEASURED DEFECT this pins (dar-u4mk). `--marker 'XCB_RC='` was a SUBSTRING test, never a parse. The guest's own
# status line said `XCB_RC=132` (128+4 = SIGILL -- the workload had died and no artifact was produced), the
# substring was found, the harness printed `MARKER ok   XCB_RC=` and `VERDICT: PASS`, and the surrounding job
# exited 0 for a workload that died. A declaration that names a VALUE must be parsed as an integer and may pass
# only on zero.
#
# No prefix, no guest and no Darling build is involved. The harness reads the workload's status out of the LOG --
# it backgrounds the launcher with nohup and never waits for it, so the workload's wait status is not even
# observable -- which is exactly why the log line is the whole subject here. The contract therefore drives the
# harness offline against a stub `<prefix>/bin/darling` that runs the `--cmd` payload on the HOST, and the arms
# below are the harness's own console plus its exit status.
#
# RED ARM (reproducible against the source-base harness, which is what the rule was fixed from):
#   mkdir -p /tmp/verdict-red/scripts
#   git show HEAD:scripts/darling-boot-run.sh > /tmp/verdict-red/scripts/darling-boot-run.sh
#   chmod +x /tmp/verdict-red/scripts/darling-boot-run.sh
#   bash tests/run-darling-boot-verdict-contract.sh --workspace /tmp/verdict-red   # must FAIL, arm 1
#   bash tests/run-darling-boot-verdict-contract.sh                                # must PASS
set -uo pipefail

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

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
prefix="$tmp/prefix-verdict"
mkdir -p "$prefix/bin"
# The stub stands in for <prefix>/bin/darling. The harness calls it twice per run: `--rootless shutdown` (a no-op
# here) and `--rootless shell /bin/bash -c CMD`, which must run CMD on the host so its output reaches the log the
# harness judges. Nothing here emulates Darling; the harness under test is the shell script above the launcher.
cat > "$prefix/bin/darling" <<'STUB'
#!/bin/sh
case "$*" in
	*" shell "*) ;;
	*) exit 0 ;;
esac
[ $# -ge 5 ] || { echo "stub launcher: unexpected argv: $*" >&2; exit 2; }
shift 2		# --rootless shell
shift 2		# /bin/bash -c
exec /bin/bash -c "$1"
STUB
chmod 755 "$prefix/bin/darling"

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
	out="$("$harness" --prefix "$prefix" --log "$tmp/run-$n.log" --no-bundle "$@" 2>&1)"
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
check "arm 1: the status line names the value it judged" yes "$(contains "$out" "STATUS FAIL XCB_RC= value=132")"
check "arm 1: a value-naming marker is never reported as a plain marker hit" no "$(contains "$out" "MARKER ok")"

# 2. The same declarations with a zero status: PASS, and exit 0.
out="$(run_harness --wait 15 --poll 'XCB_RC=' --marker 'XCB_RC=' --cmd 'echo XCB_RC=0')"; rc=$?
check "arm 2: zero workload status is PASS" PASS "$(verdict_of "$out")"
check "arm 2: zero workload status exits 0" 0 "$rc"
check "arm 2: the status line names the value it judged" yes "$(contains "$out" "STATUS ok   XCB_RC= value=0")"

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
check "arm 5: the absent field is named" yes "$(contains "$out" "STATUS MISS XCB_RC=")"

# 6. Fail closed on an unparseable value: a field that carries something other than an integer.
out="$(run_harness --wait 15 --status-marker 'XCB_RC=' --cmd 'echo HELLO=1; echo FINAL=1; echo XCB_RC=unknown')"; rc=$?
check "arm 6: an unparseable status value is FAIL" FAIL "$(verdict_of "$out")"
check_nonzero "arm 6: an unparseable status value exits non-zero" "$rc"

# 7. Fail closed on an ambiguous declaration: a value is named, but not as a plain literal field. The safe reading
#    is a refusal, because the harness cannot tell which integer the caller meant.
out="$(run_harness --wait 15 --marker 'rc] =' --cmd 'echo "rc] =0"')"; rc=$?
check "arm 7: an ambiguous value-naming declaration is FAIL" FAIL "$(verdict_of "$out")"
check "arm 7: the ambiguity is named" yes "$(contains "$out" "STATUS AMBIGUOUS")"

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
check "arm 10: the default poll is not judged as a status field" no "$(contains "$out" "STATUS")"

# 11. The front door: a status field names a VALUE, never an expected value, and the misuse is refused before any
#     state is consumed rather than misread later.
out="$(run_harness --wait 15 --status-marker 'XCB_RC=0' --cmd 'echo XCB_RC=0')"; rc=$?
check "arm 11: an expected value in --status-marker is a usage error" 2 "$rc"
check "arm 11: the usage error is stated" yes "$(contains "$out" "--status-marker FIELD=")"
check "arm 11: no boot happens on a usage error" no "$(contains "$out" "== run ==")"

if [ "$fail" -eq 0 ]; then
	echo "darling-boot-verdict: PASS"
else
	echo "darling-boot-verdict: FAIL" >&2
fi
exit "$fail"

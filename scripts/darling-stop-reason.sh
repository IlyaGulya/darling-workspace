#!/bin/sh
# darling-stop-reason.sh -- name a run's stop cause from the evidence the run already leaves behind.
#
# WHY THIS EXISTS. MEASURED 2026-10-19 (iOS lane): an `xcodebuild` workload died with rc=132 (128+4 = SIGILL)
# and produced no artifact. The cause was one missing Swift symbol, written into the run's OWN evidence:
#
#   Symbol not found: _$s9CryptoKit13SecureEnclaveO4P256O7SigningO10PrivateKeyV06publicH0AaDOAFO06PublicH0Vvg
#   Referenced from: .../SwiftPM.framework/Versions/A/SwiftPM  (which was built for Mac OS X 14.0)
#   Expected in: /System/Library/Frameworks/CryptoKit.framework/Versions/A/CryptoKit
#
# followed by `DVTPlugInExtensionFaulting` -> `_fireExtensionFaultAssertingOnBundleLoadError` -> SIGILL.
# The harness bundle HAD collected that output (`captures/ioci-xcb-clean.out`, `abort.txt`); nothing spoke it.
# The rule this tool applies: the run REMEMBERS, the tool must SPEAK.
#
# THREE THINGS IT REPORTS, AND WHY THEY ARE SEPARATE LINES
#
#   RUN-RECORD       which run this evidence belongs to: identity, start/end, prefix, the EXACT capture paths,
#                    the process incarnations seen, and how many files were EXCLUDED as not this run's.
#   STOP-REASON      the deepest recognizable stop, one line.
#   PRODUCT-VERDICT  the run's own outcome, kept apart from the harness's: a probe can COMPLETE (harness rc 0,
#                    its own completion marker present) while the product it ran FAILED. MEASURED: the xcb-clean
#                    arm swallows xcodebuild's status and printed IOCI_RC=0, so the harness said PASS over a dead
#                    xcodebuild. `GUEST_XCB_RC` preserves the real value and `PRODUCT_VERDICT` stays FAIL.
#
# RUN SCOPING IS NOT A PREFIX MATCH. A prefix's server log and its guest tmp are CUMULATIVE: they hold earlier
# runs' records. Matching on the prefix would attribute those to this run and invent a stop that this run never
# had. This tool therefore reads run-scoped evidence only: capture files at or after `--run-start`, plus the run's
# own log. Anything older is counted and reported as `excluded=`, never silently dropped.
#
# Usage: darling-stop-reason.sh --log FILE [--bundle DIR] [--rc N] [--run-start EPOCH] [--run-id ID] [--json]
#   --log        the harness/run log (the launcher trace and the workload's own status lines)
#   --bundle     the run bundle; its captures/ and abort.txt are read as well (a guest workload's own stdout
#                lands there -- NOT in --log, MEASURED: the missing-symbol line was 0 hits in --log)
#   --rc         the workload's own exit status, when known (>=128 is reported as a signal)
#   --run-start  epoch seconds; capture files older than it belong to an EARLIER run and are excluded
#   --run-id     the run's identity, echoed in RUN-RECORD (default: the log's basename)
# Exit 0 when a cause was named, 3 when nothing recognizable is present (the run may simply have passed).
#
# WHAT IT DOES NOT DO. It does not decide the harness's success, it does not read source text, and it never
# infers a property from an AGGREGATE count: a count over a cumulative log is not evidence about one run.

set -u

LOG=""
BUNDLE=""
RC=""
RUN_START=""
RUN_ID=""
JSON=0

while [ $# -gt 0 ]; do
	case "$1" in
		--log) LOG="$2"; shift 2 ;;
		--bundle) BUNDLE="$2"; shift 2 ;;
		--rc) RC="$2"; shift 2 ;;
		--run-start) RUN_START="$2"; shift 2 ;;
		--run-id) RUN_ID="$2"; shift 2 ;;
		--log=*) LOG="${1#*=}"; shift ;;
		--bundle=*) BUNDLE="${1#*=}"; shift ;;
		--rc=*) RC="${1#*=}"; shift ;;
		--run-start=*) RUN_START="${1#*=}"; shift ;;
		--run-id=*) RUN_ID="${1#*=}"; shift ;;
		--json) JSON=1; shift ;;
		-h|--help) sed -n '2,48p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "darling-stop-reason: unknown argument: $1" >&2; exit 2 ;;
	esac
done

[ -n "$LOG" ] || { echo "darling-stop-reason: --log is required" >&2; exit 2; }
[ -n "$RUN_ID" ] || RUN_ID=$(basename "$LOG")

# --------------------------------------------------------------------------- run-scoped evidence set
# The log itself is this run's (the harness opens it with `: >`). The captures are NOT: they live in the prefix
# and accumulate across runs, so each one is admitted only when it is at or after --run-start.
EVID=""
CAPTURES=""
CAPTURE_COUNT=0
EXCLUDED=0
add_evid() { [ -n "$1" ] && [ -f "$1" ] && EVID="$EVID $1"; return 0; }
add_evid "$LOG"
[ -n "$LOG" ] && [ -f "$LOG" ] && RUN_MTIME=$(stat -c %Y "$LOG" 2>/dev/null || echo 0)
RUN_MTIME=${RUN_MTIME:-0}

admit_capture() { # path -> admitted only when it is this run's
	_f="$1"
	[ -f "$_f" ] || return 0
	if [ -n "$RUN_START" ]; then
		_m=$(stat -c %Y "$_f" 2>/dev/null || echo 0)
		if [ "$_m" -lt "$RUN_START" ]; then
			EXCLUDED=$((EXCLUDED + 1))
			return 0
		fi
	fi
	EVID="$EVID $_f"
	CAPTURES="$CAPTURES$_f,"
	CAPTURE_COUNT=$((CAPTURE_COUNT + 1))
	return 0
}

if [ -n "$BUNDLE" ] && [ -d "$BUNDLE" ]; then
	add_evid "$BUNDLE/abort.txt"
	for _f in "$BUNDLE"/captures/* "$BUNDLE"/*.out; do
		admit_capture "$_f"
	done
fi
[ -n "$EVID" ] || {
	if [ "$JSON" = 1 ]; then
		printf '{"kind":"none","note":"no-evidence","path":"%s","product_verdict":"PASS"}\n' "$LOG"
	else
		echo "STOP-REASON kind=none note=no-evidence path=$LOG"
	fi
	exit 3
}

# The paths carry no spaces in this workspace (a prefix is /home/.../prefix-*), so word splitting is safe here and
# is what lets one `grep` see every evidence file. A path with a space would be reported as read-missing.
egrep_a() { grep -ah "$@" $EVID 2>/dev/null; }

# The prefix the run belongs to, when the run's own evidence says so. This IDENTIFIES the run; it never SELECTS
# evidence (a prefix match is not run scoping -- see the header).
PREFIX_HINT=$(egrep_a -o -m1 'prefix=[^ ]*' 2>/dev/null | head -1 | sed 's/^prefix=//; s/\]$//')

# The prefix a piece of evidence belongs to, when the evidence itself says so. Used to detect an attribution
# hazard: a capture that names a DIFFERENT prefix than the run's is reported, not read as this run's.
capture_prefixes=""
for _c in $(printf '%s' "$CAPTURES" | tr ',' ' '); do
	_p=$(grep -ah -o -m1 'prefix=[^ ]*' "$_c" 2>/dev/null | head -1 | sed 's/^prefix=//; s/\]$//')
	[ -n "$_p" ] && case ",$capture_prefixes," in *",$_p,"*) ;; *) capture_prefixes="$capture_prefixes,$_p" ;; esac
done
capture_prefixes=$(printf '%s' "$capture_prefixes" | sed 's/^,//')

# Process incarnations this run's evidence names (bounded, and reported as such).
PIDS=$(egrep_a -o 'pid=[0-9]*' 2>/dev/null | sed 's/^pid=//' | sort -u | head -8 | tr '\n' ',' | sed 's/,$//')

sig_name() { # number -> the usual name (one place, so the harness status and the product status agree)
	case "$1" in
		4) printf 'SIGILL' ;;
		6) printf 'SIGABRT' ;;
		7) printf 'SIGBUS' ;;
		8) printf 'SIGFPE' ;;
		9) printf 'SIGKILL' ;;
		11) printf 'SIGSEGV' ;;
		15) printf 'SIGTERM' ;;
		*) printf 'SIG%s' "$1" ;;
	esac
}

# A signal is only a symptom; the text rules below are the cause. Both are reported when both are known.
SIGFIELD=""
SIGNAME=""
case "${RC:-}" in
	''|*[!0-9]*) ;;
	*)
		if [ "$RC" -ge 128 ]; then
			_n=$((RC - 128))
			_s=$(sig_name "$_n")
			SIGNAME="$_s"
			SIGFIELD="signal=$_s rc=$RC"
		fi
		;;
esac

# A loader diagnostic interleaved into a workload's own output is its own defect (MEASURED: a probe's
# `ws-size=` line was cut in half by `[release-drops-pending] site=mldr.c:1347`), and it is also how a
# diagnostic can end up INSIDE a data file a workload writes.
POLLUTED=""
if egrep_a -q -E '^\[(mldr|srv-|add |pcreate|dthread|release-drops|afunix|rpc-)'; then
	POLLUTED="note=workload-output-carries-loader-diagnostics"
fi

# The product's OWN status, when the workload printed one. An arm that wraps a tool must not hide it: the value
# is reported even when the probe's completion marker says the arm finished.
guest_rc() {
	_g=""
	for _pat in 'xcb_rc=[0-9]*' 'XCB_RC=[0-9]*' 'IOCI_XCB_RC=[0-9]*' 'guest_rc=[0-9]*'; do
		_g=$(egrep_a -o -m1 "$_pat" 2>/dev/null | head -1 | sed 's/.*=//')
		[ -n "$_g" ] && { printf '%s' "$_g"; return 0; }
	done
	printf '%s' "absent"
}
GUEST_RC=$(guest_rc)

# The harness's bounded wait is visible in its own log: `waited Ns of at most Ms`. When the two agree the budget
# was EXHAUSTED -- which is a distinct outcome from a stop, and the one a reader most easily mistakes for a PASS
# over a workload that never printed anything.
WAIT_EXHAUSTED=0
_w=$(egrep_a -o -m1 'waited [0-9]*s of at most [0-9]*s' 2>/dev/null | tail -1)
if [ -n "$_w" ]; then
	_wn=$(printf '%s' "$_w" | sed 's/^waited \([0-9]*\)s.*/\1/')
	_wm=$(printf '%s' "$_w" | sed 's/.*at most \([0-9]*\)s/\1/')
	if [ -n "$_wn" ] && [ -n "$_wm" ] && [ "$_wn" = "$_wm" ]; then WAIT_EXHAUSTED=1; fi
fi

KIND="none"
FIELDS=""

emit() { # kind FIELDS...
	_kind="$1"; shift
	KIND="$_kind"
	FIELDS="$*"
	if [ "$JSON" != 1 ]; then
		printf 'STOP-REASON kind=%s' "$_kind"
		for _kv in "$@"; do printf ' %s' "$_kv"; done
		[ -n "$PREFIX_HINT" ] && printf ' prefix=%s' "$PREFIX_HINT"
		[ -n "$SIGFIELD" ] && printf ' %s' "$SIGFIELD"
		[ -n "$POLLUTED" ] && printf ' %s' "$POLLUTED"
		printf '\n'
	fi
}

# 1. A missing SYMBOL inside a dlopened image: dyld's own line, and the two lines it names. The most specific
#    cause dyld prints, so it is checked first -- a missing symbol and a missing image are different defects.
_sym=$(egrep_a -o -m1 'Symbol not found: [^ ]*' 2>/dev/null | head -1 | sed 's/^Symbol not found: //')
if [ -n "$_sym" ]; then
	_ctx=$(egrep_a -m1 -A3 -F "Symbol not found: $_sym" 2>/dev/null | head -5)
	_ref=$(printf '%s\n' "$_ctx" | grep -ah -m1 'Referenced from:' 2>/dev/null | sed 's/.*Referenced from: *//; s/ (which was built for.*//')
	_exp=$(printf '%s\n' "$_ctx" | grep -ah -m1 'Expected in:' 2>/dev/null | sed 's/.*Expected in: *//')
	emit missing-symbol "symbol=$_sym" "referrer=${_ref:-?}" "expected_in=${_exp:-?}" "guest_rc=$GUEST_RC" "stage=loader-dlopen"
else
# 2. A whole missing image.
_img=$(egrep_a -o -m1 'Library not loaded: [^ ]*' 2>/dev/null | head -1 | sed 's/^Library not loaded: //')
if [ -n "$_img" ]; then
	_ref=$(egrep_a -m1 -A2 -F "Library not loaded: $_img" 2>/dev/null | grep -ah -m1 'Referenced from:' | sed 's/.*Referenced from: *//')
	emit missing-image "image=$_img" "referrer=${_ref:-?}" "guest_rc=$GUEST_RC" "stage=loader-load"
else
# 3. An extension fault raised by a failed plug-in/bundle load: the image above did not load, and Xcode's
#    DVTExtension faulting path turns that into an abort.
if egrep_a -q -E 'DVTPlugInExtensionFaulting|_fireExtensionFaultAssertingOnBundleLoadError'; then
	_plug=$(egrep_a -o -m1 'plug-in [A-Za-z0-9_.-]*' 2>/dev/null | head -1 | sed 's/^plug-in //')
	_ext=$(egrep_a -o -m1 "extension '[A-Za-z0-9_.-]*'" 2>/dev/null | head -1 | sed "s/^extension '//; s/'$//")
	emit extension-fault "plug_in=${_plug:-?}" "extension=${_ext:-?}" "guest_rc=$GUEST_RC" "stage=plugin-load"
else
# 4. `error opening!: <errno>` plus the path that errno belongs to.
if egrep_a -q 'error opening!:'; then
	_errno=$(egrep_a -o -m1 'error opening!: [0-9]*' 2>/dev/null | head -1 | sed 's/.*: *//')
	_path=$(egrep_a -o -m1 'Could not create database queue for path [^ ]*' 2>/dev/null | head -1 | sed 's/.*for path //')
	emit open-errno "errno=${_errno:-?}" "path=${_path:-unknown}" "guest_rc=$GUEST_RC" "stage=open"
else
# 5. The launcher's own bounded wait: the guest never brought the service up.
if egrep_a -q 'shellspawn did not become ready'; then
	emit launch-timeout "stage=shellspawn" "guest_rc=absent"
else
# 6. The harness waited its whole budget and the workload never printed a status. This is the KNOWN timeout, and
#    it must not be reported as kind=none: "nothing matched" and "the run never ended" are different facts, and
#    conflating them is how a watchdog timeout reads as an absence of problems.
if [ "$WAIT_EXHAUSTED" = 1 ] && [ "$GUEST_RC" = absent ]; then
	emit watchdog-timeout "guest_rc=absent" "stage=unresolved"
else
# 7. THE PRODUCT EXITED NONZERO with no textual stop above. This is NOT `kind=none`: the workload under test
#    failed, and a harness contract can be met while the product failed (MEASURED: the xcb-clean arm swallowed
#    xcodebuild's 132 and printed its own IOCI_RC=0). `kind=none` is reserved for evidence with no failure in it.
case "$GUEST_RC" in
	0) emit none "guest_rc=0" ;;
	''|absent)
		if [ -n "$SIGFIELD" ]; then
			emit crash "guest_rc=${RC:-absent}" "stage=crash"
		else
			emit none "guest_rc=absent"
		fi
		;;
	*[!0-9]*) emit none "guest_rc=$GUEST_RC" ;;
	*)
		_gs=$((GUEST_RC - 128))
		if [ "$GUEST_RC" -ge 128 ]; then
			_w=$(sig_name "$_gs")
			SIGNAME="$_w"
			emit guest-nonzero-exit "guest_rc=$GUEST_RC" "signal=$_w" "stage=product"
		else
			emit guest-nonzero-exit "guest_rc=$GUEST_RC" "stage=product"
		fi
		;;
esac
fi
fi
fi
fi
fi
fi

# --------------------------------------------------------------------------- the run record and the verdict
# THE PRODUCT VERDICT IS NOT THE HARNESS'S. The harness judges ITS declarations (a probe that completed is a
# completed probe); the product's own status says whether the thing under test worked. Both are printed, and the
# harness's value is carried here so a reader never has to reconcile two tools by hand.
PRODUCT=PASS
case "$GUEST_RC" in
	''|absent) [ "$KIND" = none ] || PRODUCT=FAIL ;;
	*[!0-9]*) PRODUCT=FAIL ;;
	0) [ "$KIND" = none ] || PRODUCT=FAIL ;;
	*) PRODUCT=FAIL ;;
esac
[ "$KIND" = watchdog-timeout ] && PRODUCT=FAIL

_ended=$(stat -c %Y "$LOG" 2>/dev/null || echo 0)
_started=${RUN_START:-unknown}

if [ "$JSON" = 1 ]; then
	# ONE object for the whole answer: the stop, the run record and the product verdict are one observation set,
	# and three objects on one stream are not loadable by a composing caller.
	printf '{"kind":"%s"' "$KIND"
	for _kv in $FIELDS; do printf ',"%s":"%s"' "${_kv%%=*}" "${_kv#*=}"; done
	[ -n "$PREFIX_HINT" ] && printf ',"prefix":"%s"' "$PREFIX_HINT"
	[ -n "$SIGNAME" ] && printf ',"signal":"%s"' "$SIGNAME"
	[ -n "$RC" ] && printf ',"rc":"%s"' "$RC"
	[ -n "$POLLUTED" ] && printf ',"pollution":"1"'
	printf ',"run":"%s","started":"%s","ended":"%s","harness_rc":"%s","guest_xcb_rc":"%s"' \
		"$RUN_ID" "$_started" "$_ended" "${RC:-absent}" "$GUEST_RC"
	printf ',"product_verdict":"%s","captures":"%s","capture_count":"%s","excluded":"%s","pids":"%s"}\n' \
		"$PRODUCT" "$CAPTURES" "$CAPTURE_COUNT" "$EXCLUDED" "$PIDS"
else
	printf 'RUN-RECORD run=%s started=%s ended=%s prefix=%s harness_rc=%s captures=%s capture_count=%s excluded=%s pids=%s\n' \
		"$RUN_ID" "$_started" "$_ended" "${PREFIX_HINT:-unknown}" "${RC:-absent}" "${CAPTURES:-none}" \
		"$CAPTURE_COUNT" "$EXCLUDED" "${PIDS:-none}"
	[ -n "$capture_prefixes" ] && [ -n "$PREFIX_HINT" ] && \
		case ",$capture_prefixes," in
			*",$PREFIX_HINT,"*) ;;
			*) printf 'RUN-RECORD-WARNING capture-prefix=%s run-prefix=%s -- a capture naming another prefix is not run-scoped evidence\n' \
				"$capture_prefixes" "$PREFIX_HINT" ;;
		esac
	printf 'PRODUCT-VERDICT PRODUCT_VERDICT=%s HARNESS_RC=%s GUEST_XCB_RC=%s stop=%s run=%s prefix=%s\n' \
		"$PRODUCT" "${RC:-absent}" "$GUEST_RC" "$KIND" "$RUN_ID" "${PREFIX_HINT:-unknown}"
fi

[ "$KIND" = none ] && exit 3
exit 0

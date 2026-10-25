#!/usr/bin/env bash
# Contract for scripts/darling-stop-reason.sh (and the `Symbol not found` form in scripts/rung-driver.py).
#
# WHY THESE ASSERTIONS. Every fixture below is a stop form that a real run produced, and each one cost a
# hand-grep before a tool named it:
#   * the missing-SYMBOL form (2026-10-19): xcodebuild died rc=132 with the cause in the run's own bundle
#     captures; the harness printed counters and VERDICT and never read what it had collected;
#   * the SQLITE code/path PAIRING: `error opening!: 14` and the path it belongs to are separate messages,
#     and 14 is `sqlite3_open`'s result code (SQLITE_CANTOPEN), NOT an errno;
#   * the CAPTURE-ONLY case: the cause was 0 hits in the harness log and present in the bundle's captures/;
#   * `rung-driver.py stop` answered "the run did not stop in dyld" for the missing-symbol form.
#
# The A/B/C cases are the acceptance cases for this tool:
#   A. a diagnostic probe COMPLETES while the product it ran FAILS -- harness rc may be 0, the guest's real
#      status must survive, and the product verdict must stay FAIL;
#   B. a canonical workload reaches its watchdog with no status at all -- a KNOWN timeout, which must not be
#      reported as kind=none;
#   C. CUMULATIVE evidence: a prefix's captures and server log hold earlier runs' records, and a matching
#      prefix is NOT run identity, so an old capture must never be attributed to the current run.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$repo/.west-state/supervisor-tooling-20261025"
mkdir -p "$scratch"
tmp="$(mktemp -d "$scratch/stop-reason-contract.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
run="$repo/scripts/darling-stop-reason.sh"

SYM='_$s9CryptoKit13SecureEnclaveO4P256O7SigningO10PrivateKeyV06publicH0AaDOAFO06PublicH0Vvg'
REF='/Volumes/SystemRoot/home/u/xcode/Xcode.app/Contents/SharedFrameworks/SwiftPM.framework/Versions/A/SwiftPM'
EXP='/System/Library/Frameworks/CryptoKit.framework/Versions/A/CryptoKit'

# 1. the missing-symbol form, with the workload's own status naming the signal
cat >"$tmp/symbol.log" <<EOF
}, dyldError = dlopen(/p/IDESwiftPackageCore, 0): Symbol not found: $SYM
  Referenced from: $REF (which was built for Mac OS X 14.0)
  Expected in: $EXP
DVTPlugInExtensionFaulting: Failed to fire fault for extension Xcode.FileDataTypeDetector.SwiftPackageFileTypeDetector
XCB_RC=132
EOF
out="$("$run" --log "$tmp/symbol.log" --rc 132)"
printf '%s\n' "$out"
grep -F -q "kind=missing-symbol" <<<"$out"
grep -F -q "symbol=$SYM" <<<"$out"
grep -F -q "expected_in=$EXP" <<<"$out"
grep -F -q "signal=SIGILL rc=132" <<<"$out"

# 2. --json composes (and the signal is a NAME, not "SIGILL rc=132")
json="$("$run" --log "$tmp/symbol.log" --rc 132 --json)"
python3 -B -c '
import json,sys
d=json.loads(sys.argv[1])
assert d["kind"]=="missing-symbol", d
assert d["signal"]=="SIGILL", d
assert d["rc"]=="132", d
' "$json"

# 3. the SQLite result code and the path it belongs to are reported together, in the SQLite domain.
#    Source: FMDatabase.m:176-178 (sqlite3_open) and FMDatabaseQueue.m:91; 14 is SQLITE_CANTOPEN, not errno.
cat >"$tmp/sqlite.log" <<'EOF'
2026-10-19 08:07:06.912 xcodebuild[2415866:3e8] error opening!: 14
2026-10-19 08:07:06.913 xcodebuild[2415866:3e8] Could not create database queue for path /private/var/db/launchservices.db
EOF
out="$("$run" --log "$tmp/sqlite.log")"
printf '%s\n' "$out"
grep -F -q "kind=sqlite-open-failed code=14 path=/private/var/db/launchservices.db" <<<"$out"
! grep -F -q "errno=14" <<<"$out"
! grep -F -q "open-errno" <<<"$out"

# 3b. THE XCODE LICENSE REFUSAL IS THE FATAL GATE and outranks the preceding SQLite warning. The captured
#     canonical minproj run printed the two FMDB lines and then the license text, exiting 69; the classifier
#     must name the license, must not call the SQLite code an errno, and must keep the SQLite line visible as
#     context rather than dropping it.
cat >"$tmp/license.log" <<'EOF'
2026-10-23 20:43:39.767 xcodebuild[2103369:3e8] error opening!: 14
2026-10-23 20:43:39.767 xcodebuild[2103369:3e8] Could not create database queue for path /private/var/db/launchservices.db
You have not agreed to the Xcode license agreements. Please run 'sudo xcodebuild -license' from within a Terminal window to review and agree to the Xcode and Apple SDKs license agreements.
XCB_RC=69
EOF
out="$("$run" --log "$tmp/license.log" --rc 69)"
printf '%s\n' "$out"
grep -F -q "kind=license-required stage=license" <<<"$out"
grep -F -q "STOP-CONTEXT kind=sqlite-open-failed code=14 path=/private/var/db/launchservices.db" <<<"$out"
! grep -F -q "open-errno" <<<"$out"
! grep -F -q "errno=14" <<<"$out"
json="$("$run" --log "$tmp/license.log" --rc 69 --json)"
python3 -B -c '
import json,sys
d=json.loads(sys.argv[1])
assert d["kind"]=="license-required", d
assert d["context_kind"]=="sqlite-open-failed", d
assert d["context_code"]=="14", d
assert "errno" not in d, d
' "$json"

# 4. a missing IMAGE is its own kind (not folded into the symbol rule)
cat >"$tmp/image.log" <<'EOF'
dyld: Library not loaded: /System/Library/Frameworks/Foo.framework/Versions/A/Foo
  Referenced from: /p/x
  Reason: image not found
EOF
out="$("$run" --log "$tmp/image.log")"
printf '%s\n' "$out"
grep -F -q "kind=missing-image image=/System/Library/Frameworks/Foo.framework/Versions/A/Foo" <<<"$out"

# 5. the launcher's bounded wait is a LAUNCH stop, never a workload verdict
printf 'Rootless shellspawn did not become ready within 30000ms (/proc/self/fd/8/shellspawn.sock)\n' >"$tmp/launch.log"
out="$("$run" --log "$tmp/launch.log")"
printf '%s\n' "$out"
grep -F -q "kind=launch-timeout stage=shellspawn" <<<"$out"

# 6. THE CAPTURE-ONLY CASE: the cause is in the bundle's captures and NOT in the log. A classifier that reads
#    only --log must not answer as if the evidence were complete.
printf 'harness: waited 42s of at most 240s\nVERDICT: PASS\n' >"$tmp/bundle-only.log"
mkdir -p "$tmp/bundle/captures"
cp "$tmp/symbol.log" "$tmp/bundle/captures/probe.out"
out="$("$run" --log "$tmp/bundle-only.log" --bundle "$tmp/bundle" --rc 132)"
printf '%s\n' "$out"
grep -F -q "kind=missing-symbol" <<<"$out"
log_only="$("$run" --log "$tmp/bundle-only.log" || true)"
grep -F -q "kind=none" <<<"$log_only"

# 7. evidence with no recognizable stop is NOT a failure claim: kind=none, exit 3
printf 'VERDICT: PASS\nMARKER ok HELLO=1\nwaited 60s of at most 300s\n' >"$tmp/pass.log"
out="$("$run" --log "$tmp/pass.log" --oracle='MARKER ok HELLO=1')" && rc=0 || rc=$?
printf '%s\n' "$out"
[ "$rc" -eq 3 ]
grep -F -q "kind=none" <<<"$out"
grep -F -q "PRODUCT_VERDICT=PASS" <<<"$out"

# 8. a run's log NAMES ITS OWN PREFIX and the classification carries it through. Without this the log is not
#    self-describing and a log-only tool resolves the SERVER log from a DEFAULT path -- MEASURED: `dwdiag progress`
#    printed `(from DEFAULT ...)` while the prefix was known, i.e. it would have read another prefix's dserver.log.
printf '[dwdiag-env prefix=/tmp/pfx log=%s wait=300]\n' "$tmp/pass.log" >"$tmp/stamped.log"
printf 'Rootless shellspawn did not become ready within 30000ms (/proc/self/fd/8/shellspawn.sock)\n' >>"$tmp/stamped.log"
out="$("$run" --log "$tmp/stamped.log")"
printf '%s\n' "$out"
grep -F -q "prefix=/tmp/pfx" <<<"$out"
json="$("$run" --log "$tmp/stamped.log" --json)"
python3 -B -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["prefix"]=="/tmp/pfx", d' "$json"

# 9. rung-driver.py no longer discards the missing-SYMBOL form. It is the same defect class one layer up:
#    the stop was never "absent", it was unfiltered.
rd="$(python3 -B "$repo/scripts/rung-driver.py" stop --log "$tmp/symbol.log" 2>&1 || true)"
printf '%s\n' "$rd" | grep -F -q "Symbol not found: $SYM"
printf '%s\n' "$rd" | grep -F -q "$EXP"

# --------------------------------------------------------------------------- A, B, C (acceptance cases)

# A. THE PROBE COMPLETES; THE PRODUCT FAILS. The harness's contract is met (its completion marker is present) and
#    its rc MAY be 0 -- and that must not erase the product's own status or turn its verdict into a pass.
mkdir -p "$tmp/a"
printf 'IOCI install-clean RC=0 wall=30s\nIOCI_PROBE_DONE=1\nIOCI_RC=0\n' >"$tmp/a/a.log"
cat >"$tmp/a/captures-xcb.out" <<'EOF'
IOCI xcb-clean xcb_rc=132 killed=no out=/private/var/tmp/ioci-xcb-clean.out
IOCI xcb-clean artifact=none
IOCI xcb-clean RC=0 wall=23s
EOF
mkdir -p "$tmp/a/bundle/captures"
cp "$tmp/a/captures-xcb.out" "$tmp/a/bundle/captures/ioci-xcb-clean.out"
out="$("$run" --log "$tmp/a/a.log" --bundle "$tmp/a/bundle" --rc 0)"
printf '%s\n' "$out"
grep -F -q "GUEST_XCB_RC=132" <<<"$out"
grep -F -q "PRODUCT_VERDICT=FAIL" <<<"$out"
grep -F -q "HARNESS_RC=0" <<<"$out"

# B. THE CANONICAL WORKLOAD REACHES ITS WATCHDOG WITH NO STATUS. This is a KNOWN timeout and must be named as
#    one: `kind=none` here would read as "nothing was wrong".
printf '[dwdiag-env prefix=/tmp/pfx log=x wait=300]\n' >"$tmp/b.log"
printf 'srv-attach-dispatch pid=2495401 seq=18 fd=23\n' >>"$tmp/b.log"
printf 'waited 300s of at most 300s\n' >>"$tmp/b.log"
out="$("$run" --log "$tmp/b.log" --run-id=canonical-minproj)"
printf '%s\n' "$out"
grep -F -q "kind=watchdog-timeout" <<<"$out"
grep -F -q "guest_rc=absent" <<<"$out"
grep -F -q "stage=unresolved" <<<"$out"
grep -F -q "PRODUCT_VERDICT=FAIL" <<<"$out"
! grep -F -q "kind=none" <<<"$out"

# C. CUMULATIVE EVIDENCE IS NOT THIS RUN'S. An earlier run's capture (older than --run-start) carries a stop the
#    current run never had; a matching prefix is NOT run identity, so that stop must not be attributed here, and
#    the exclusion must be REPORTED rather than silent.
mkdir -p "$tmp/c/bundle/captures"
cp "$tmp/symbol.log" "$tmp/c/bundle/captures/old-run.out"
printf 'waited 60s of at most 300s\nVERDICT: PASS\n' >"$tmp/c/current.log"
printf 'plain current-run output\n' >"$tmp/c/bundle/captures/current-run.out"
touch -d '2020-01-01 00:00:00' "$tmp/c/bundle/captures/old-run.out"
now="$(date +%s)"
out="$("$run" --log "$tmp/c/current.log" --bundle "$tmp/c/bundle" --run-start="$now" --run-id=run-c || true)"
printf '%s\n' "$out"
! grep -F -q "kind=missing-symbol" <<<"$out"
grep -F -q "excluded=1" <<<"$out"
grep -F -q "capture_count=1" <<<"$out"
grep -F -q "current-run.out" <<<"$out"
# and a capture that names ANOTHER prefix is reported, not read as this run's
mkdir -p "$tmp/c2/bundle/captures"
printf '[dwdiag-env prefix=/tmp/other-prefix log=y]\nplain\n' >"$tmp/c2/bundle/captures/other.out"
printf '[dwdiag-env prefix=/tmp/pfx log=%s wait=60]\nwaited 60s of at most 300s\n' "$tmp/c2/current.log" >"$tmp/c2/current.log"
out="$("$run" --log "$tmp/c2/current.log" --bundle "$tmp/c2/bundle" --run-start="$(date +%s)" --run-id=run-c2 || true)"
printf '%s\n' "$out"
grep -F -q "RUN-RECORD-WARNING capture-prefix=/tmp/other-prefix run-prefix=/tmp/pfx" <<<"$out"

# E. ABSENCE OF EVIDENCE IS NOT SUCCESS. Both the harness status and the guest status absent, and no explicit
#    success oracle: the product verdict must be INCONCLUSIVE, never PASS. An earlier revision of this tool
#    called exactly this shape `PRODUCT_VERDICT=PASS` (the XCB_RC=absent canonical run), which invents success
#    from a missing status. `RUNNER_RC` is preserved separately when the caller knows it.
printf '[dwdiag-env prefix=/tmp/pfx log=x wait=300]\n' >"$tmp/e.log"
printf 'plain line\nwaited 60s of at most 300s\n' >>"$tmp/e.log"
out="$("$run" --log "$tmp/e.log" --run-id=run-e || true)"
printf '%s\n' "$out"
grep -F -q "PRODUCT_VERDICT=INCONCLUSIVE" <<<"$out"
! grep -F -q "PRODUCT_VERDICT=PASS" <<<"$out"
grep -F -q "HARNESS_RC=absent" <<<"$out"
grep -F -q "GUEST_XCB_RC=absent" <<<"$out"
grep -F -q "RUNNER_RC=absent" <<<"$out"
grep -F -q "oracle=0" <<<"$out"
# a declared oracle that is present makes PASS legitimate; one that is absent does not
printf 'BUILD_OK=1\n' >>"$tmp/e.log"
out="$("$run" --log "$tmp/e.log" --oracle='BUILD_OK=1' --runner-rc=0 || true)"
printf '%s\n' "$out"
grep -F -q "PRODUCT_VERDICT=PASS" <<<"$out"
grep -F -q "oracle=1" <<<"$out"
grep -F -q "RUNNER_RC=0" <<<"$out"
out="$("$run" --log "$tmp/e.log" --oracle='NEVER_PRINTED=1' || true)"
grep -F -q "PRODUCT_VERDICT=INCONCLUSIVE" <<<"$out"
json="$("$run" --log "$tmp/e.log" --json || true)"
python3 -B -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["product_verdict"]=="INCONCLUSIVE", d' "$json"

# D. run identity, timestamps and exact capture paths are preserved
out="$("$run" --log "$tmp/c/current.log" --bundle "$tmp/c/bundle" --run-start="$now" --run-id=run-c || true)"
grep -F -q "RUN-RECORD run=run-c" <<<"$out"
grep -F -q "started=$now" <<<"$out"
grep -F -q "ended=" <<<"$out"
grep -F -q "$tmp/c/bundle/captures/current-run.out" <<<"$out"

# Behavioral status precedence, compressed logical evidence and unavailable-reader cases.
python3 -B "$repo/tests/west_test_contracts/stop_reason_contract.py"

printf 'PASS darling-stop-reason-contract\n'

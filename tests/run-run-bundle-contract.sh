#!/usr/bin/env bash
# Host contract for the RUN BUNDLE `scripts/darling-boot-run.sh` writes on every run.
#
# MEASURED DEFECTS this pins (each cost a rung of the Xcode ladder):
#   * the server's terminate/abort backtrace lands in the HARNESS log while the prefix server log holds only the
#     server's own records -- a run's health was misread because only one of the two was grepped;
#   * xtrace writes ONE capture per guest process (`<arm-out-prefix>.trace.<pid>` / `.traceg.<pid>`) and NOTHING
#     announced those paths, so a reader concluded "there is no trace" from reading the arm's stdout;
#   * a missing item was silently absent;
#   * the instrument census (which instrument never spoke) existed as a tool nobody was told to run;
#   * launchd's own debug log -- where the answer to "which Mach service did the workload resolve" actually was --
#     was collected by nothing.
#
# No prefix, no guest and no Darling build is involved: the harness reads the workload's status out of the LOG it
# was given, so this drives it offline against a stub `<prefix>/bin/darling` that runs the `--cmd` payload on the
# HOST (the same technique as tests/run-darling-boot-verdict-contract.sh).
set -uo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
harness="$workspace_root/scripts/darling-boot-run.sh"
[ -x "$harness" ] || { echo "contract: no executable harness at $harness" >&2; exit 2; }

scratch="$workspace_root/.west-state/supervisor-tooling-20261025"
mkdir -p "$scratch"
tmp="$(mktemp -d "$scratch/run-bundle-contract.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
# Exercise shell collection without triggering the Rust build shim. The shared codec is real.
mkdir -p "$tmp/scripts"
cp "$harness" "$tmp/scripts/darling-boot-run.sh"
harness="$tmp/scripts/darling-boot-run.sh"
export EVIDENCE_HELPER="$workspace_root/scripts/darling-evidence.py"
cat > "$tmp/scripts/darling-evidence.py" <<'CODEC'
import os
import sys
if os.environ.get("EVIDENCE_FAIL_COMPRESS") and sys.argv[1] == "compress":
    sys.exit(23)
os.execv(sys.executable, [sys.executable, os.environ["EVIDENCE_HELPER"], *sys.argv[1:]])
CODEC
cat > "$tmp/scripts/dwdiag" <<'DIAG'
#!/bin/sh
case "$1" in
	env) echo '[dwdiag-env fixture=host-contract]' ;;
	witness)
		[ "$2" = --log ] || exit 2
		[ -r "$3" ] || exit 1
		printf 'WITNESS log=%s fired=0\nWITNESS-SILENT fixture-instrument\n' "$3"
		;;
	*) exit 2 ;;
esac
DIAG
chmod 755 "$tmp/scripts/dwdiag"
if [ -x "$workspace_root/scripts/darling-stop-reason.sh" ]; then
	cp "$workspace_root/scripts/darling-stop-reason.sh" "$tmp/scripts/darling-stop-reason.sh"
	mkdir -p "$tmp/west_commands"
	cp "$workspace_root/west_commands/stop_reason.py" "$workspace_root/west_commands/evidence_storage.py" "$tmp/west_commands/"
fi
evidence_cat() { python3 "$EVIDENCE_HELPER" cat "$1"; }
prefix="$tmp/prefix-bundle"
mkdir -p "$prefix/bin" "$prefix/private/var/log/com.apple.launchd"
: > "$prefix/private/var/log/dserver.log"
cat > "$prefix/bin/darling" <<'STUB'
#!/bin/sh
for a in "$@"; do last="$a"; done
case "$*" in *shutdown*) exit 0 ;; esac
eval "$last"
exit $?
STUB
chmod 755 "$prefix/bin/darling"
# launchd's own debug log, which the bundle must collect and ANNOUNCE: the service a workload resolved is a line in
# THIS file, in a process other than the one under test.
printf 'Mach service lookup: com.apple.FileCoordination from pid 4242\nMach service lookup failed: com.example.Absent from pid 4242\n' \
	> "$prefix/private/var/log/com.apple.launchd/launchd-debug.system.log"

# An abort line LONGER than the 512-byte cap that once truncated exactly this record. The bundle must carry it WHOLE.
long_body="$(printf 'x%.0s' $(seq 1 900))"
log="$tmp/run.log"
out="$("$harness" --prefix "$prefix" --wait 12 --log "$log" \
	--cmd "printf 'HELLO=1\nFINAL=1\nlibc++abi: terminating with uncaught exception: %s\n' '$long_body'" 2>"$tmp/stderr.txt")"; rc=$?
bundle="$log.bundle"

fail=0
check() { # label expected actual
	if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected $2, got $3"; fail=1; fi
}
contains() { case "$1" in *"$2"*) echo yes ;; *) echo no ;; esac; }

# 1. ONE COMMAND, ONE BUNDLE, ANNOUNCED. The run's own output names the directory and the manifest.
check "arm 1: the run announces its bundle directory" yes "$(contains "$out" "BUNDLE dir=$bundle")"
check "arm 1: the run announces the manifest" yes "$(contains "$out" "BUNDLE-MANIFEST $bundle/MANIFEST")"
check "arm 1: the bundle exists" yes "$([ -d "$bundle" ] && echo yes || echo no)"
check "arm 1: a finished bundle is not marked INCOMPLETE" no "$([ -e "$bundle/INCOMPLETE" ] && echo yes || echo no)"
check "arm 1: the manifest closes with BUNDLE-COMPLETE" yes "$(grep -q '^BUNDLE-COMPLETE ' "$bundle/MANIFEST" && echo yes || echo no)"

# 2. BOTH LOGS. The harness's own log is collected, and the server/prefix logs are named as items or as MISSING with
#    a reason -- never silently absent.
check "arm 2: the run log is in the bundle" yes "$(evidence_cat "$bundle/harness.log" >/dev/null && echo yes || echo no)"
check "arm 2: the run-scoped server log is an item or a MISSING row" yes \
	"$(grep -qE '^server-log\s' "$bundle/MANIFEST" && echo yes || echo no)"
check "arm 2: MISSING rows carry a reason" yes \
	"$(awk -F'\t' '$2=="MISSING" && ($4=="" || $4=="-") {bad=1} END {print (bad ? "no" : "yes")}' "$bundle/MANIFEST")"

# 3. With no xtrace capture in this offline run, capture absence must be explicit and carry a reason.
check "arm 3: absent captures are explicitly recorded" yes \
	"$(awk -F'\t' '$1=="guest-captures" && $2=="MISSING" && length($4)>0 {ok=1} END {print ok ? "yes" : "no"}' "$bundle/MANIFEST")"

# 4. THE CENSUS NAMES THE SILENT INSTRUMENTS (this is what makes "silent" a fact instead of an inference).
check "arm 4: the run prints an instrument census" yes "$(contains "$out" "BUNDLE-WITNESS fired=")"
check "arm 4: the census names silent instruments" yes "$(contains "$out" "silent=[")"

# 5. THE ABORT TEXT IS CARRIED WHOLE. The written record must contain the full line, and the item must report its
#    own length -- the bound is the record's, not a constant.
check "arm 5: the abort text is found" yes "$(evidence_cat "$bundle/abort.txt" | grep -q 'libc++abi: terminating with uncaught exception' && echo yes || echo no)"
check "arm 5: the whole >512-byte abort line is present" yes \
	"$(evidence_cat "$bundle/abort.txt" | grep -qF "$long_body" && echo yes || echo no)"
check "arm 5: the item reports the longest line found" yes "$(contains "$out" "longest=")"
check "arm 5: the longest line is over 512 bytes (the old cap)" yes \
	"$(awk -F'longest=' '/longest=/{split($2,a," "); print (a[1]+0 > 512) ? "yes" : "no"}' <<<"$out")"

# 6. LAUNCHD'S OWN LOG, and its look-up lines quoted in the summary.
check "arm 6: launchd's debug log is announced" yes "$(contains "$out" "launchd-log-launchd-debug.system.log")"
check "arm 6: the resolved look-up line is quoted" yes "$(contains "$out" 'Mach service lookup: com.apple.FileCoordination')"
check "arm 6: the FAILED look-up line is quoted" yes "$(contains "$out" 'Mach service lookup failed: com.example.Absent')"

# 7. THE COUNTERS AND THE DEPLOYED-BUILD IDENTITY are rows of the bundle, and the run says so.
check "arm 7: the counters file is announced" yes "$(contains "$out" "BUNDLE-ITEM counters")"
check "arm 7: the four VERDICT counters are printed" yes "$(contains "$out" "BUNDLE-COUNTERS sockets-created=")"
check "arm 7: the deployed-copy digest is an item or a MISSING row" yes \
	"$(grep -qE '^run-env\s' "$bundle/MANIFEST" && echo yes || echo no)"

# 8. HONESTY IN THE OTHER DIRECTION: a bundle directory that already holds another run's files is REFUSED, because a
#    mixed bundle reads as this run's evidence.
mkdir -p "$tmp/used"; : > "$tmp/used/old-evidence"
"$harness" --prefix "$prefix" --wait 5 --log "$tmp/run2.log" --bundle "$tmp/used" --cmd 'true' >"$tmp/out2.txt" 2>&1; rc2=$?
check "arm 8: a non-empty --bundle directory is refused" 2 "$rc2"
check "arm 8: refused bundle keeps original evidence untouched" yes "$([ -f "$tmp/used/old-evidence" ] && [ ! -e "$tmp/used/MANIFEST" ] && echo yes || echo no)"

verdict_of() { printf '%s\n' "$1" | sed -n 's/^VERDICT: //p' | tail -1; }
check_nonzero() {
	if [ "$2" -ne 0 ] 2>/dev/null; then
		echo "ok   $1 (rc=$2)"
	else
		echo "FAIL $1: expected non-zero exit, got '$2'" >&2
		fail=1
	fi
}

# Bundle behavior uses the real shared codec, but never launches a guest. Each run owns its log/bundle;
# the prefix server file intentionally retains stale failures to expose cumulative-history contamination.
server="$prefix/private/var/log/dserver.log"
mkdir -p "$(dirname "$server")"
bundle_run() { # case-name command
	local name="$1" command="$2"
	bundle="$tmp/bundle-$name"
	out="$("$harness" --prefix "$prefix" --log "$tmp/$name.log" --bundle "$bundle" \
		--wait 5 --poll 'DONE=' --marker 'DONE=' --cmd "$command" 2>&1)"
	rc=$?
}
server_count() { evidence_cat "$bundle/counters.txt" | awk -F'\t' -v name="$1" '$1==name {print $3}'; }
state_field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$bundle/server-log-state.json" "$1"; }

printf 'dserver-CRASH historical\nplane-doorbell-sent historical\nthis server build: unavailable (historical)\n' > "$server"
bundle_run append 'printf "plane-doorbell-sent current\nplane-doorbell-sent current\n" >> "$DPREFIX/private/var/log/dserver.log"; echo DONE=0'
check "bundle: historical failures do not alter current workload verdict" PASS "$(verdict_of "$out")"
check "bundle: appended server markers count only current bytes" 2 "$(server_count plane-doorbell-sent)"
check "bundle: historical crash counter is zero" 0 "$(server_count dserver-crash)"
check "bundle: server payload excludes historical bytes" no "$(contains "$(evidence_cat "$bundle/server.log")" historical)"
check "bundle: abort extraction excludes historical failure" no "$(contains "$(evidence_cat "$bundle/abort.txt")" historical)"
check "bundle: declarations exclude historical unavailability" no "$(contains "$(evidence_cat "$bundle/build-declarations.txt")" historical)"
check "bundle: census input excludes historical failure" no "$(contains "$(evidence_cat "$bundle/witness-input.log")" historical)"
check "bundle: classifier excludes historical failure" no "$(contains "$(evidence_cat "$bundle/stop-reason.txt")" 'kind=crash')"
check "bundle: no whole-history pre snapshot" no "$([ -e "$bundle/server.pre.log" ] && echo yes || echo no)"
check "bundle: no whole-history post snapshot" no "$([ -e "$bundle/server.post.log" ] && echo yes || echo no)"
check "bundle: append collection is complete" True "$(state_field complete)"

printf 'historical rotation boundary\n' > "$server"
bundle_run rotate 's="$DPREFIX/private/var/log/dserver.log"; printf "plane-doorbell-sent old-tail\n" >> "$s"; mv "$s" "$s.rotated"; printf "plane-doorbell-sent new-inode\n" > "$s"; printf "plane-doorbell-sent late-old-tail\n" >> "$s.rotated"; echo DONE=0'
check "rotation: retained old FD and new inode both counted" 3 "$(server_count plane-doorbell-sent)"
check "rotation: historical bytes excluded" no "$(contains "$(evidence_cat "$bundle/server.log")" historical)"
check "rotation: old tail after rename survives" yes "$(contains "$(evidence_cat "$bundle/server.log")" late-old-tail)"
check "rotation: collection remains complete" True "$(state_field complete)"
check "rotation: no known gap" False "$(state_field gap)"
check "rotation: workload remains PASS" PASS "$(verdict_of "$out")"

printf 'historical long boundary which will be truncated\n' > "$server"
bundle_run truncate 'printf "plane-doorbell-sent reset\n" > "$DPREFIX/private/var/log/dserver.log"; echo DONE=0'
check_nonzero "truncate: collection failure surfaces" "$rc"
check "truncate: no workload PASS fabricated over evidence gap" FAIL "$(verdict_of "$out")"
check "truncate: reset bytes are retained and count is nonnegative" 1 "$(server_count plane-doorbell-sent)"
check "truncate: gap recorded" True "$(state_field gap)"
check "truncate: not complete" False "$(state_field complete)"
check "truncate: bundle remains explicitly incomplete" yes "$([ -f "$bundle/INCOMPLETE" ] && echo yes || echo no)"
check "truncate: manifest does not claim completion" no "$(contains "$(cat "$bundle/MANIFEST")" BUNDLE-COMPLETE)"

printf 'old-boundary\n' > "$server"
bundle_run regrow 'python3 -c '\''import os; p=os.environ["DPREFIX"]+"/private/var/log/dserver.log"; open(p,"w").write("plane-doorbell-sent regrown\n"*512)'\''; echo DONE=0'
check_nonzero "regrowth: rewritten boundary is not mistaken for append" "$rc"
check "regrowth: reset payload retained in full" 512 "$(server_count plane-doorbell-sent)"
check "regrowth: gap recorded" True "$(state_field gap)"
check "regrowth: cannot claim complete" False "$(state_field complete)"

: > "$server"
bundle_run compressed 'python3 -c '\''import os; p=os.environ["DPREFIX"]+"/private/var/log/dserver.log"; open(p,"a").write("plane-doorbell-sent compressible-proof\n"*4096)'\''; echo DONE=0'
check "compression: successful workload verdict survives" PASS "$(verdict_of "$out")"
check "compression: finalized server payload physically compressed" yes "$([ -f "$bundle/server.log.zst" ] && [ ! -e "$bundle/server.log" ] && echo yes || echo no)"
check "compression: logical server path remains re-readable" 4096 "$(evidence_cat "$bundle/server.log" | grep -ac compressible-proof)"
check "compression: counters consumed raw evidence before compression" 4096 "$(server_count plane-doorbell-sent)"
check "compression: external ongoing run log stays raw" yes "$([ -f "$tmp/compressed.log" ] && [ ! -e "$tmp/compressed.log.zst" ] && echo yes || echo no)"
check "compression: ongoing prefix server stays raw" yes "$([ -f "$server" ] && [ ! -e "$server.zst" ] && echo yes || echo no)"
python3 - "$bundle" <<'INDEX'
import hashlib, json, pathlib, subprocess, sys
bundle = pathlib.Path(sys.argv[1])
entry = json.loads((bundle / "evidence-index.json").read_text())["files"]["server.log"]
stored = bundle / entry["stored_path"]
raw = subprocess.check_output([sys.executable, str(pathlib.Path(__import__("os").environ["EVIDENCE_HELPER"])), "cat", str(bundle / "server.log")])
assert stored.is_file()
assert entry["original_bytes"] == len(raw)
assert entry["stored_bytes"] == stored.stat().st_size
assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
assert entry["stored_sha256"] == hashlib.sha256(stored.read_bytes()).hexdigest()
assert entry["stored_bytes"] < entry["original_bytes"]
INDEX
check "compression: index resolves physical path, sizes and hashes" 0 "$?"

rm -f "$server"
bundle_run created 'printf "plane-doorbell-sent first-boot\n" > "$DPREFIX/private/var/log/dserver.log"; echo DONE=0'
check "creation: previously absent server captured from zero" 1 "$(server_count plane-doorbell-sent)"
check "creation: normal first log is complete" True "$(state_field complete)"
check "creation: first log does not invent a gap" False "$(state_field gap)"
check "creation: workload remains PASS" PASS "$(verdict_of "$out")"

rm -f "$server"
bundle_run missing 'echo DONE=0'
check_nonzero "missing: unavailable server collection surfaces" "$rc"
check "missing: bundle does not claim complete" False "$(state_field complete)"
check "missing: unavailable server counter is not measured zero" UNKNOWN "$(server_count plane-doorbell-sent)"
check "missing: manifest cannot claim completion" no "$(contains "$(cat "$bundle/MANIFEST")" BUNDLE-COMPLETE)"

: > "$server"
export EVIDENCE_FAIL_COMPRESS=1
bundle_run storage-failure 'python3 -c '\''import os; open(os.environ["DPREFIX"]+"/private/var/log/dserver.log","a").write("plane-doorbell-sent retained-proof\n"*4096)'\''; echo DONE=0'
unset EVIDENCE_FAIL_COMPRESS
check_nonzero "compression failure: finalization exits nonzero" "$rc"
check "compression failure: no PASS fabricated" FAIL "$(verdict_of "$out")"
check "compression failure: raw evidence retained" 4096 "$(evidence_cat "$bundle/server.log" | grep -ac retained-proof)"
check "compression failure: incomplete marker remains" yes "$([ -f "$bundle/INCOMPLETE" ] && echo yes || echo no)"
check "compression failure: manifest records storage failure" yes \
	"$(awk -F'\t' '$1=="evidence-storage" && $2=="MISSING" && length($4)>0 {ok=1} END {print ok ? "yes" : "no"}' "$bundle/MANIFEST")"

: > "$server"
bundle="$tmp/bundle-frozen"
out="$("$harness" --prefix "$prefix" --log "$tmp/frozen.log" --bundle "$bundle" --freeze-on-fail \
	--wait 5 --poll 'DONE=' --marker 'DONE=' --cmd \
	'python3 -c '\''import os; open(os.environ["DPREFIX"]+"/private/var/log/dserver.log","a").write("plane-doorbell-sent frozen-proof\n"*4096)'\''; echo DONE=9' 2>&1)"
rc=$?
check_nonzero "freeze: workload failure remains failure" "$rc"
check "freeze: finalized finite server copy compressed" yes "$([ -f "$bundle/server.log.zst" ] && echo yes || echo no)"
check "freeze: compressed evidence re-readable" 4096 "$(evidence_cat "$bundle/server.log" | grep -ac frozen-proof)"
check "freeze: external server never compressed" yes "$([ -f "$server" ] && [ ! -e "$server.zst" ] && echo yes || echo no)"

# A real prefix may alias its log directory. The codec must receive the stable real directory without
# allowing the generic prefix-log collector to copy the cumulative server history through the alias.
mv "$prefix/private/var/log" "$prefix/private/var/log-real"
ln -s log-real "$prefix/private/var/log"
bundle_run aliased-directory 'printf "plane-doorbell-sent aliased-current\n" >> "$DPREFIX/private/var/log/dserver.log"; echo DONE=0'
check "directory alias: only current appended server bytes count" 1 "$(server_count plane-doorbell-sent)"
check "directory alias: collection remains complete" True "$(state_field complete)"
check "directory alias: workload remains PASS" PASS "$(verdict_of "$out")"
check "directory alias: no cumulative prefix-log copy" no "$([ -e "$bundle/dserver.log.post" ] || [ -e "$bundle/dserver.log.post.zst" ] && echo yes || echo no)"

if [ "$fail" -eq 0 ]; then
	echo "RUN-BUNDLE-CONTRACT: PASS"
else
	echo "RUN-BUNDLE-CONTRACT: FAIL" >&2
	exit 1
fi

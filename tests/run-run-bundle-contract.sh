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

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
prefix="$tmp/prefix-bundle"
mkdir -p "$prefix/bin" "$prefix/private/var/log/com.apple.launchd"
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
check "arm 2: the run log is in the bundle" yes "$([ -f "$bundle/harness.log" ] && echo yes || echo no)"
check "arm 2: the server log is an item or a MISSING row" yes \
	"$(grep -qE '^server-log-(pre|post)\s' "$bundle/MANIFEST" && echo yes || echo no)"
check "arm 2: MISSING rows carry a reason" yes \
	"$(awk -F'\t' '$2=="MISSING" && ($4=="" || $4=="-") {bad=1} END {print (bad ? "no" : "yes")}' "$bundle/MANIFEST")"

# 3. EVERY CAPTURE PATH IS PRINTED. With no xtrace capture in this offline run the item must be MISSING **with the
#    directories that were searched** in the reason, so a reader cannot look in the wrong place.
check "arm 3: the capture item names the directories searched" yes \
	"$(contains "$out" "private/var/tmp")"

# 4. THE CENSUS NAMES THE SILENT INSTRUMENTS (this is what makes "silent" a fact instead of an inference).
check "arm 4: the run prints an instrument census" yes "$(contains "$out" "BUNDLE-WITNESS fired=")"
check "arm 4: the census names silent instruments" yes "$(contains "$out" "silent=[")"

# 5. THE ABORT TEXT IS CARRIED WHOLE. The written record must contain the full line, and the item must report its
#    own length -- the bound is the record's, not a constant.
check "arm 5: the abort text is found" yes "$(grep -q 'libc++abi: terminating with uncaught exception' "$bundle/abort.txt" && echo yes || echo no)"
check "arm 5: the whole >512-byte abort line is present" yes \
	"$(grep -qF "$long_body" "$bundle/abort.txt" && echo yes || echo no)"
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
check "arm 8: the refusal says why" yes "$(contains "$(cat "$tmp/out2.txt")" "reads as this run's evidence")"

if [ "$fail" -eq 0 ]; then
	echo "RUN-BUNDLE-CONTRACT: PASS"
else
	echo "RUN-BUNDLE-CONTRACT: FAIL" >&2
	exit 1
fi

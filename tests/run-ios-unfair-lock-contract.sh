#!/usr/bin/env bash
# Focused guest runtime contract for the unfair-lock reproducer behind the T4 failure.
#
# T4 (tests/run-ios-toolchain-contract.sh) fails when the real Apple ld is asked to link: the guest
# traps inside Darling's own libsystem_platform at __os_unfair_lock_recursive_abort, and the core
# shows the faulting worker already owning the lock word (0x...0b03, its own
# __TSD_MACH_THREAD_SELF token). A linker nesting its own lock would be fatal on macOS too, so the
# surviving reading is a lost unlock or damaged word inside Darling's emulation. This contract
# reproduces that with no Apple toolchain involved: a small guest Mach-O does the same sequences and
# must survive them.
#
# Mechanism reused (no new framework): the Darling build's host cross toolchain. The fixture is built
# by replaying the recorded compile/link commands of an existing guest Mach-O target of the same
# build and substituting this source, exactly as the DTAPE runtime contracts do.
#
# Inputs (required):
#   DARLING_BUILD_DIR   a configured Darling product build (has build.ninja)
#   DPREFIX/DARLING_PREFIX   a booted prefix whose runtime is under test
# Optional:
#   DARLING_GUEST_MACHO_REFERENCE_TARGET   default darling_bzero_return_regress
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
fixture_src="$workspace_root/tests/ios_unfair_lock_guest.c"
build_dir="${DARLING_BUILD_DIR:-}"
prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
reference_target="${DARLING_GUEST_MACHO_REFERENCE_TARGET:-darling_bzero_return_regress}"
boot_run="$workspace_root/scripts/darling-boot-run.sh"

[ -n "$build_dir" ] && [ -f "$build_dir/build.ninja" ] || {
	echo "ios-unfair-lock: DARLING_BUILD_DIR must name a configured build" >&2
	exit 2
}
[ -n "$prefix" ] && [ -x "$prefix/bin/darling" ] || {
	echo "ios-unfair-lock: DPREFIX must name a booted Darling prefix" >&2
	exit 2
}
[ -f "$fixture_src" ] || { echo "ios-unfair-lock: fixture source not found: $fixture_src" >&2; exit 2; }

workdir="$(mktemp -d)"
trap '[ -n "${DARLING_KEEP_WORKDIR:-}" ] || rm -rf "$workdir"' EXIT
[ -n "${DARLING_KEEP_WORKDIR:-}" ] && echo "ios-unfair-lock: workdir retained: $workdir" >&2 || true

ninja -C "$build_dir" -t commands "$reference_target" > "$workdir/cmds" 2>/dev/null || {
	echo "ios-unfair-lock: cannot read $reference_target commands from $build_dir" >&2
	exit 2
}

python3 -B - "$workdir/cmds" "$fixture_src" "$workdir" "$build_dir" "$reference_target" <<'PY'
import re
import subprocess
import sys
from pathlib import Path

cmds_path, fixture_src, workdir, build_dir, reference_target = sys.argv[1:6]
lines = Path(cmds_path).read_text().splitlines()
obj = str(Path(workdir) / "fixture.o")
binary = str(Path(workdir) / "fixture")

compile_line = next((line for line in lines
                     if re.search(r"-c\s+\S*%s\.c\b" % re.escape(reference_target), line)), None)
link_line = next((line for line in lines
                  if "-fuse-ld=" in line and reference_target in line), None)
if compile_line is None or link_line is None:
    raise SystemExit("ios-unfair-lock: reference target has no recorded guest Mach-O build")

compile_cmd = re.sub(r"-o\s+\S*%s\.c\.o" % re.escape(reference_target), "-o " + obj, compile_line)
compile_cmd = re.sub(r"-c\s+\S*%s\.c\b" % re.escape(reference_target), "-c " + fixture_src, compile_cmd)
subprocess.run(compile_cmd, shell=True, check=True, cwd=build_dir)

segment = [part.strip() for part in link_line.split("&&") if "-fuse-ld=" in part][0]
segment = re.sub(r"\S*%s\.c\.o" % re.escape(reference_target), obj, segment)
segment = re.sub(r"-o\s+\S*%s(\s|$)" % re.escape(reference_target), "-o " + binary + " ", segment)
subprocess.run(segment, shell=True, check=True, cwd=build_dir)
print(binary)
PY

binary="$workdir/fixture"
[ -x "$binary" ] || { echo "ios-unfair-lock: fixture was not built" >&2; exit 1; }

install -m755 "$binary" "$prefix/private/var/tmp/ios_unfair_lock_guest"

log="$workdir/run.log"
harness="$(bash "$boot_run" --prefix "$prefix" --wait "${IOS_UNFAIR_LOCK_WAIT:-60}" \
	--marker 'IOS-UNFAIR-LOCK pass=1' \
	--env DSERVER_LOG_LEVEL=debug --env DSERVER_LOG_STDERR=1 \
	--log "$log" \
	--cmd 'exec /private/var/tmp/ios_unfair_lock_guest' 2>&1)" || true
printf '%s\n' "$harness" | tail -12

fail=0
printf '%s\n' "$harness" | grep -q 'VERDICT: PASS' || fail=1
if ! grep -q 'IOS-UNFAIR-LOCK pass=1' "$log"; then
	fail=1
	echo "ios-unfair-lock: the fixture did not report pass=1"
	echo "ios-unfair-lock: trap evidence from the run log:"
	grep -aE 'SIGILL|Illegal instruction|os_unfair_lock|recursive|abort' "$log" | tail -6 || true
fi

if [ "$fail" -eq 0 ]; then
	echo "IOS-UNFAIR-LOCK PASS: single-thread lock/unlock/lock, contention and 1000 repetitions all survive"
else
	echo "IOS-UNFAIR-LOCK FAIL"
fi
exit "$fail"

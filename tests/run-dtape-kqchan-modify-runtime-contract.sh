#!/usr/bin/env bash
# dar-dtape-explicit-context-6to3.4a focused guest runtime gate.
#
# Proves that the kqchan Mach-port MODIFY (touch) path still completes with the
# impersonate()/impersonate(nullptr) pair removed. It does NOT compile in the
# guest: the fixture is a prebuilt guest Mach-O built on the host with the
# Darling product's own cross toolchain, and the prefix only executes it.
#
# Mechanism reused (no new framework): the Darling build's host cross-toolchain.
# `add_darling_executable` builds guest Mach-O with host clang -target
# x86_64-apple-darwin20 linked by the in-tree cctools-port ld64. Rather than
# create a CMake target, this replays the recorded compile/link commands of an
# existing guest Mach-O test target of the SAME build, substituting the fixture
# source. That keeps the flags identical to the product's own guest builds.
#
# Path coverage, not a generic kqueue smoke: libkqueue (src/common/kevent.c)
# routes a re-registration of an existing knote to filt->kn_modify ONLY for a
# bare EV_ADD, and evfilt_machport_knote_modify returns 0 only after the server
# answers dserver_kqchan_msgnum_mach_port_modify. The gate additionally requires
# the server's own debug lines for the modify request.
#
# Inputs (required):
#   DARLING_BUILD_DIR  a configured Darling product build (has build.ninja)
#   DPREFIX/DARLING_PREFIX  a booted prefix whose darlingserver is under test
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
fixture_src="$workspace_root/tests/kqchan_modify_guest.c"
build_dir="${DARLING_BUILD_DIR:-}"
prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
reference_target="${DARLING_GUEST_MACHO_REFERENCE_TARGET:-darling_bzero_return_regress}"

if [ -z "$build_dir" ] || [ ! -f "$build_dir/build.ninja" ]; then
	echo "dtape-kqchan-modify-runtime: DARLING_BUILD_DIR must name a configured Darling build" >&2
	exit 2
fi
if [ -z "$prefix" ] || [ ! -x "$prefix/bin/darling" ]; then
	echo "dtape-kqchan-modify-runtime: DPREFIX must name a booted Darling prefix" >&2
	exit 2
fi
if [ ! -f "$fixture_src" ]; then
	echo "dtape-kqchan-modify-runtime: fixture source not found: $fixture_src" >&2
	exit 2
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

ninja -C "$build_dir" -t commands "$reference_target" > "$workdir/cmds" 2>/dev/null || {
	echo "dtape-kqchan-modify-runtime: cannot read $reference_target commands from $build_dir" >&2
	exit 2
}

python3 -B - "$workdir/cmds" "$fixture_src" "$workdir" "$build_dir" <<'PY'
import re
import subprocess
import sys
from pathlib import Path

cmds_path, fixture_src, workdir, build_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]
lines = Path(cmds_path).read_text().splitlines()
obj = str(workdir / "fixture.o")
binary = str(workdir / "fixture")

compile_line = next((line for line in lines
                     if re.search(r"-c\s+\S*darling_bzero_return_regress\.c\b", line)), None)
link_line = next((line for line in lines
                  if "-fuse-ld=" in line and re.search(r"darling_bzero_return_regress", line)), None)
if compile_line is None or link_line is None:
    raise SystemExit("dtape-kqchan-modify-runtime: reference target has no recorded guest Mach-O build")

compile_cmd = re.sub(r"-o\s+\S*darling_bzero_return_regress\.c\.o", "-o " + obj, compile_line)
compile_cmd = re.sub(r"-c\s+\S*darling_bzero_return_regress\.c\b", "-c " + fixture_src, compile_cmd)
subprocess.run(compile_cmd, shell=True, check=True, cwd=build_dir)

segment = [part.strip() for part in link_line.split("&&") if "-fuse-ld=" in part][0]
segment = re.sub(r"\S*darling_bzero_return_regress\.c\.o", obj, segment)
segment = re.sub(r"-o\s+\S*darling_bzero_return_regress(\s|$)", "-o " + binary + " ", segment)
subprocess.run(segment, shell=True, check=True, cwd=build_dir)
print(binary)
PY

binary="$workdir/fixture"
[ -x "$binary" ] || { echo "dtape-kqchan-modify-runtime: fixture was not built" >&2; exit 1; }

# Fresh server so the debug level applies, then execute the prebuilt Mach-O in
# the guest. The prefix only runs it; nothing is compiled there.
env DPREFIX="$prefix" DARLING_PREFIX="$prefix" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
	timeout 120 "$prefix/bin/darling" --rootless shutdown >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
	pgrep -f "darlingserver.*$prefix" >/dev/null 2>&1 || break
	sleep 1
done
pkill -f "$prefix/bin/darling" >/dev/null 2>&1 || true
pgrep -f "darlingserver.*$prefix" >/dev/null 2>&1 && {
	echo "dtape-kqchan-modify-runtime: prefix darlingserver still running; cannot guarantee a fresh server" >&2
	exit 1
}
install -m755 "$binary" "$prefix/private/var/tmp/kqchan_modify_guest"

log="$workdir/run.log"
env DPREFIX="$prefix" DARLING_PREFIX="$prefix" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
	DSERVER_LOG_LEVEL=debug DSERVER_LOG_STDERR=1 \
	timeout --kill-after=5 120 "$prefix/bin/darling" shell /bin/bash --login -c 'exec /private/var/tmp/kqchan_modify_guest' \
	> "$log" 2>&1 || true

fail=0
for marker in \
	'KQCHAN_MODIFY_OK=1' \
	'Received modification request' \
	'Handling modification request in microthread'; do
	if grep -q "$marker" "$log"; then
		echo "dtape-kqchan-modify-runtime: saw $marker"
	else
		echo "dtape-kqchan-modify-runtime: MISSING $marker" >&2
		fail=1
	fi
done

if [ "$fail" -ne 0 ]; then
	echo "DTAPE-KQCHAN-MODIFY-RUNTIME FAIL (log: $log)" >&2
	exit 1
fi
echo "DTAPE-KQCHAN-MODIFY-RUNTIME PASS: prebuilt guest Mach-O completed the kqchan modify path"

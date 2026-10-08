#!/usr/bin/env bash
# Deterministic host contract for the dtape_mutex OWNERSHIP contract (bead dar-lf4b).
#
# The T4 abort is
#   darlingserver duct-tape panic: "Lock assertion failed (not owned but expected to be owned)"
# inside dtape_mutex_unlock. The panic proves an ownership mismatch, not its origin. This contract
# compiles the REAL product implementation (duct-tape/src/locks.c) unchanged and drives it through
# the legal and illegal acquire/release transitions, so the mechanism is reproducible in
# milliseconds instead of waiting for a rare run:
#
#   legal   active-thread pair                         -> no panic
#   legal   no-active-thread (fallback) pair           -> no panic
#   ILLEGAL fallback acquire, active-thread release    -> the measured panic
#   ILLEGAL acquire as guest thread A, release as B    -> the measured panic
#   legal   contention with suspension and wake        -> no panic, waiter wakes
#
# It does not change any lock behaviour and does not weaken the assertion; it establishes the
# vocabulary required before touching the lock code.
#
# Compiling XNU-flavoured kernel objects needs the product build's generated headers, so this
# contract is registered in ci/run-host-tier.py EXCLUDED_CONTRACTS (like its sibling) and is run
# from the prefix-backed lane with DARLING_BUILD_DIR.
#
# Inputs:
#   DARLING_BUILD_DIR   a configured Darling product build dir (has build.ninja). Required.
#   DARLING_SRC_ROOT    darlingserver source root to compile (default ../darling/src/external/
#                       darlingserver). Point it at another checkout to prove the RED arm.
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
darling_root="${DARLING_SRC_ROOT:-$workspace_root/../darling/src/external/darlingserver}"
build_dir="${DARLING_BUILD_DIR:-}"

if [ -z "$build_dir" ] || [ ! -f "$build_dir/build.ninja" ]; then
	echo "dtape-mutex-ownership: DARLING_BUILD_DIR must name a configured product build" >&2
	exit 2
fi
if [ ! -f "$darling_root/duct-tape/src/locks.c" ]; then
	echo "dtape-mutex-ownership: darlingserver source not found: $darling_root" >&2
	exit 2
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

ninja -C "$build_dir" -t commands \
	src/external/darlingserver/duct-tape/CMakeFiles/darlingserver_duct_tape.dir/src/locks.c.o \
	2>/dev/null | tail -1 > "$workdir/template.cmd" || {
	echo "dtape-mutex-ownership: build dir has no recorded duct-tape compile for locks.c" >&2
	exit 2
}

python3 -B - "$workspace_root" "$darling_root" "$build_dir" "$workdir" <<'PY'
import subprocess
import sys
from pathlib import Path

workspace_root, darling_root, build_dir, workdir = (Path(p) for p in sys.argv[1:5])
tests = workspace_root / "tests"

template = (workdir / "template.cmd").read_text().strip()
if not template or "locks.c" not in template:
    raise SystemExit("dtape-mutex-ownership: could not recover the locks.c compile command")

marker = "/duct-tape/defines"
build_src_root = None
for token in template.split():
    if token.startswith("-I") and token.endswith(marker):
        build_src_root = token[2:-len(marker)]
        break
if build_src_root is None:
    raise SystemExit("dtape-mutex-ownership: cannot recover the darlingserver source root from the build")

ARCHIVE_OBJ = "src/external/darlingserver/duct-tape/CMakeFiles/darlingserver_duct_tape.dir/src/locks.c.o"
ARCHIVE_SRC = "duct-tape/src/locks.c"

def substitute(cmd: str, src: Path, obj: Path) -> str:
    cmd = cmd.replace(build_src_root, str(darling_root))
    cmd = cmd.replace(ARCHIVE_OBJ + ".d", str(obj) + ".d")
    cmd = cmd.replace(ARCHIVE_OBJ, str(obj))
    cmd = cmd.replace(str(darling_root / ARCHIVE_SRC), str(src))
    return cmd

def run_script(body: str, name: str) -> int:
    path = workdir / name
    path.write_text("set -e\n" + body + "\n")
    return subprocess.run(["sh", str(path)], cwd=build_dir).returncode

sources = [
    (darling_root / ARCHIVE_SRC, workdir / "locks.o"),
    (tests / "dtape_mutex_ownership_host.c", workdir / "host.o"),
]

for index, (src, obj) in enumerate(sources):
    cmd = substitute(template, src, obj)
    rc = run_script(cmd, f"compile_{index}.sh")
    if rc != 0:
        label = "RED: source tree rejected" if darling_root != build_src_root else "product source failed to compile"
        raise SystemExit(f"dtape-mutex-ownership: {label} ({src.name})")

objects = sorted(str(p) for p in workdir.glob("*.o"))
# locks.c uses libsimple_lock_*, whose implementation lives in darling/src/libsimple. Link the
# product archive for the host variant rather than recompiling it, so the contract exercises the
# same library the server does.
libsimple = build_dir / "src/libsimple-linux/liblibsimple_darlingserver.a"
extra = [str(libsimple)] if libsimple.exists() else []
link = subprocess.run(["ccache", "cc", "-Wl,--gc-sections", "-o", str(workdir / "contract")] + objects + extra,
        cwd=build_dir)
if link.returncode != 0:
    raise SystemExit("dtape-mutex-ownership: contract link failed")

run = subprocess.run([str(workdir / "contract")], capture_output=True, text=True)
sys.stdout.write(run.stdout)
sys.stderr.write(run.stderr)
if run.returncode != 0:
    raise SystemExit(f"dtape-mutex-ownership: FAIL (exit {run.returncode})")
# The contract writes to fd 2 by hand (it cannot include stdio.h next to the Darwin headers), so the
# marker is searched for in both streams rather than only in stdout.
if "DTAPE-MUTEX-OWNERSHIP PASS" not in run.stdout + run.stderr:
    raise SystemExit("dtape-mutex-ownership: FAIL: PASS marker missing")
PY

echo "DTAPE-MUTEX-OWNERSHIP: host contract PASS"

#!/usr/bin/env bash
# dar-dtape-explicit-context-6to3.5 deterministic host contract for the kqchan Mach-port READ/FILL path.
#
# Two distinct execution contexts are modelled:
#   K = the kernelAsync microthread the read actually runs on (the ambient thread)
#   R = the guest requester thread the read is performed for (the explicit requester)
# with K != R. The contract compiles the REAL product objects the read path runs on
#
#   dtape_kqchan_mach_port_fill(kqchan, R, ...)        (duct-tape/src/kqchan.c)
#     -> filt_machportprocess_on_thread(kn, kev, R)    (duct-tape/xnu ipc_pset.c)
#          -> ipc_mqueue_receive_on_thread(..., R)
#               -> ipc_mqueue_select_on_thread(...)    (duct-tape/xnu ipc_mqueue.c)
#          -> mach_msg_receive_results_on_thread(&size, R) (duct-tape/xnu mach_msg.c)
#
# and drives them with K installed as current_thread() and with K's task/ith_* poisoned.
# It asserts every semantic result came from R (kevent_ctx, ith_*, the receive task's
# messages_received, and the task/space/map used for copyout) and that K was never the
# source. It also asserts the path never asserted-waited on the mqueue and never touched
# the turnstile-proxy/inheritor machinery (both live on the blocking tail this path cannot
# reach, because MACH_RCV_TIMEOUT with a zero timeout returns before them).
#
# This is NOT a source/text audit: the changed functions are compiled and executed. A
# reverted implementation that substituted the ambient thread fails the run.
#
# Compiling XNU-flavoured kernel objects needs the product build's generated headers, so
# this contract is registered in ci/run-host-tier.py EXCLUDED_CONTRACTS (like the sibling
# runtime gate) and is run from the prefix-backed lane with DARLING_BUILD_DIR.
#
# Inputs:
#   DARLING_BUILD_DIR   a configured Darling product build dir (has build.ninja) whose
#                       variant compiled duct-tape/xnu. Required.
#   DARLING_SRC_ROOT    darlingserver source root to compile (default: ../darling/src/
#                       external/darlingserver). Point it at a pre-refactor checkout to
#                       demonstrate the RED arm (the harness then cannot build against the
#                       old ambient API).
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
darling_root="${DARLING_SRC_ROOT:-$workspace_root/../darling/src/external/darlingserver}"
build_dir="${DARLING_BUILD_DIR:-}"

if [ -z "$build_dir" ] || [ ! -f "$build_dir/build.ninja" ]; then
	echo "dtape-kqchan-fill-context: DARLING_BUILD_DIR must name a configured build dir (has build.ninja)" >&2
	exit 2
fi
if [ ! -f "$darling_root/duct-tape/src/kqchan.c" ]; then
	echo "dtape-kqchan-fill-context: darlingserver source not found: $darling_root" >&2
	exit 2
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

ninja -C "$build_dir" -t commands \
	src/external/darlingserver/duct-tape/CMakeFiles/darlingserver_duct_tape.dir/xnu/osfmk/ipc/ipc_mqueue.c.o \
	2>/dev/null | tail -1 > "$workdir/template.cmd" || {
	echo "dtape-kqchan-fill-context: build dir has no recorded duct-tape compile for ipc_mqueue.c" >&2
	exit 2
}

python3 -B - "$workspace_root" "$darling_root" "$build_dir" "$workdir" <<'PY'
import subprocess
import sys
from pathlib import Path

workspace_root, darling_root, build_dir, workdir = (Path(p) for p in sys.argv[1:5])
tests = workspace_root / "tests"

template = (workdir / "template.cmd").read_text().strip()
if not template or "ipc_mqueue.c" not in template:
    raise SystemExit("dtape-kqchan-fill-context: could not recover the ipc_mqueue.c compile command")

# The build's own darlingserver source root, recovered from a source-tree include flag.
marker = "/duct-tape/defines"
build_src_root = None
for token in template.split():
    if token.startswith("-I") and token.endswith(marker):
        build_src_root = token[2:-len(marker)]
        break
if build_src_root is None:
    raise SystemExit("dtape-kqchan-fill-context: cannot recover the darlingserver source root from the build")

ARCHIVE_OBJ = "src/external/darlingserver/duct-tape/CMakeFiles/darlingserver_duct_tape.dir/xnu/osfmk/ipc/ipc_mqueue.c.o"
ARCHIVE_SRC = "duct-tape/xnu/osfmk/ipc/ipc_mqueue.c"

def substitute(cmd: str, src: Path, obj: Path) -> str:
    # Point the recorded command at an arbitrary source tree and output object.
    cmd = cmd.replace(build_src_root, str(darling_root))
    cmd = cmd.replace(ARCHIVE_OBJ + ".d", str(obj) + ".d")
    cmd = cmd.replace(ARCHIVE_OBJ, str(obj))
    cmd = cmd.replace(str(darling_root / ARCHIVE_SRC), str(src))
    return cmd

sources = [
    (darling_root / "duct-tape/src/kqchan.c", workdir / "kqchan.o"),
    (darling_root / "duct-tape/xnu/osfmk/ipc/ipc_pset.c", workdir / "ipc_pset.o"),
    (darling_root / "duct-tape/xnu/osfmk/ipc/ipc_mqueue.c", workdir / "ipc_mqueue.o"),
    (darling_root / "duct-tape/xnu/osfmk/ipc/mach_msg.c", workdir / "mach_msg.o"),
    (tests / "dtape_kqchan_fill_context_host.c", workdir / "host.o"),
    (tests / "dtape_kqchan_fill_context_stubs.c", workdir / "stubs.o"),
]

def run_script(body: str, name: str, cwd: Path) -> int:
    path = workdir / name
    path.write_text("set -e\n" + body + "\n")
    return subprocess.run(["sh", str(path)], cwd=cwd).returncode

for index, (src, obj) in enumerate(sources):
    cmd = substitute(template, src, obj)
    rc = run_script(cmd, f"compile_{index}.sh", build_dir)
    if rc != 0:
        label = "RED: pre-refactor source lacks the explicit-requester API" if darling_root != build_src_root else "product source failed to compile"
        raise SystemExit(f"dtape-kqchan-fill-context: {label} ({src.name})")

# The two macro-colliding stubs compile without any XNU header.
bare = subprocess.run(["ccache", "cc", "-c", "-O0", "-o", str(workdir / "stubs_bare.o"), str(tests / "dtape_kqchan_fill_context_stubs_bare.c")], cwd=build_dir)
if bare.returncode != 0:
    raise SystemExit("dtape-kqchan-fill-context: bare stubs failed to compile")

objects = sorted(str(p) for p in workdir.glob("*.o"))
link = subprocess.run(["ccache", "cc", "-Wl,--gc-sections", "-o", str(workdir / "contract")] + objects, cwd=build_dir)
if link.returncode != 0:
    raise SystemExit("dtape-kqchan-fill-context: contract link failed")

run = subprocess.run([str(workdir / "contract")], capture_output=True, text=True)
sys.stdout.write(run.stdout)
sys.stderr.write(run.stderr)
if run.returncode != 0:
    raise SystemExit(f"dtape-kqchan-fill-context: FAIL (exit {run.returncode})")
if "DTAPE-KQCHAN-FILL-CONTEXT PASS" not in run.stdout:
    raise SystemExit("dtape-kqchan-fill-context: FAIL: PASS marker missing")
PY

echo "DTAPE-KQCHAN-FILL-CONTEXT: host contract PASS"

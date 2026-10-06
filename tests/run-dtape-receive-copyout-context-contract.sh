#!/usr/bin/env bash
# dar-dtape-explicit-context-6to3.6 deterministic host contract for the receive-copyout requester.
#
# .4b threaded the requester into the descriptor copyout, but the receive copyout STILL selected the
# turnstile knote and the immovable-receive guard message address from the ambient thread. This
# contract compiles the REAL changed objects
#
#   ipc_object_copyout_on_thread(..., self)   (duct-tape/xnu/osfmk/ipc/ipc_object.c)
#     -> filt_machport_turnstile_prepare_lazily(self->ith_knote, ...)
#   ipc_right_copyout_on_thread(..., self)    (duct-tape/xnu/osfmk/ipc/ipc_right.c)
#     -> ipc_port_adjust_special_reply_port_locked(self->ith_knote, ...)
#     -> port->ip_context = self->ith_msg_addr
#
# and drives them with two distinct contexts K != R, K installed as current_thread() and K given a
# live (valid) knote so a substitution of the ambient thread cannot hide behind an invalid one. It
# asserts the knote handed to both leaves is R's, never K's.
#
# This is NOT a source/text audit: the changed functions are compiled and executed. RED arm: point
# DARLING_SRC_ROOT at a pre-.6 checkout (for example the cd897eb worktree); the harness then cannot
# build against the explicit-requester API, which is exactly the ambient implementation it fixes.
#
# Compiling XNU-flavoured kernel objects needs the product build's generated headers, so this
# contract is registered in ci/run-host-tier.py EXCLUDED_CONTRACTS and is run from the
# prefix-backed lane with DARLING_BUILD_DIR.
#
# Inputs:
#   DARLING_BUILD_DIR   a configured Darling product build dir (has build.ninja). Required.
#   DARLING_SRC_ROOT    darlingserver source root to compile (default: ../darling/src/
#                       external/darlingserver).
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
darling_root="${DARLING_SRC_ROOT:-$workspace_root/../darling/src/external/darlingserver}"
build_dir="${DARLING_BUILD_DIR:-}"

if [ -z "$build_dir" ] || [ ! -f "$build_dir/build.ninja" ]; then
	echo "dtape-receive-copyout-context: DARLING_BUILD_DIR must name a configured build dir (has build.ninja)" >&2
	exit 2
fi
if [ ! -f "$darling_root/duct-tape/xnu/osfmk/ipc/ipc_object.c" ]; then
	echo "dtape-receive-copyout-context: darlingserver source not found: $darling_root" >&2
	exit 2
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

ninja -C "$build_dir" -t commands \
	src/external/darlingserver/duct-tape/CMakeFiles/darlingserver_duct_tape.dir/xnu/osfmk/ipc/ipc_mqueue.c.o \
	2>/dev/null | tail -1 > "$workdir/template.cmd" || {
	echo "dtape-receive-copyout-context: build dir has no recorded duct-tape compile for ipc_mqueue.c" >&2
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
    raise SystemExit("dtape-receive-copyout-context: could not recover the ipc_mqueue.c compile command")

marker = "/duct-tape/defines"
build_src_root = None
for token in template.split():
    if token.startswith("-I") and token.endswith(marker):
        build_src_root = token[2:-len(marker)]
        break
if build_src_root is None:
    raise SystemExit("dtape-receive-copyout-context: cannot recover the darlingserver source root from the build")

ARCHIVE_OBJ = "src/external/darlingserver/duct-tape/CMakeFiles/darlingserver_duct_tape.dir/xnu/osfmk/ipc/ipc_mqueue.c.o"
ARCHIVE_SRC = "duct-tape/xnu/osfmk/ipc/ipc_mqueue.c"

def substitute(cmd: str, src: Path, obj: Path) -> str:
    cmd = cmd.replace(build_src_root, str(darling_root))
    cmd = cmd.replace(ARCHIVE_OBJ + ".d", str(obj) + ".d")
    cmd = cmd.replace(ARCHIVE_OBJ, str(obj))
    cmd = cmd.replace(str(darling_root / ARCHIVE_SRC), str(src))
    return cmd

sources = [
    (darling_root / "duct-tape/xnu/osfmk/ipc/ipc_object.c", workdir / "ipc_object.o"),
    (darling_root / "duct-tape/xnu/osfmk/ipc/ipc_right.c", workdir / "ipc_right.o"),
    (tests / "dtape_receive_copyout_context_host.c", workdir / "host.o"),
    (tests / "dtape_receive_copyout_context_stubs.c", workdir / "stubs.o"),
]

def run_script(body: str, name: str, cwd: Path) -> int:
    path = workdir / name
    path.write_text("set -e\n" + body + "\n")
    return subprocess.run(["sh", str(path)], cwd=cwd).returncode

for index, (src, obj) in enumerate(sources):
    cmd = substitute(template, src, obj)
    rc = run_script(cmd, f"compile_{index}.sh", build_dir)
    if rc != 0:
        label = "RED: pre-.6 source lacks the explicit-requester API" if darling_root != build_src_root else "product source failed to compile"
        raise SystemExit(f"dtape-receive-copyout-context: {label} ({src.name})")

objects = sorted(str(p) for p in workdir.glob("*.o"))
link = subprocess.run(["ccache", "cc", "-Wl,--gc-sections", "-o", str(workdir / "contract")] + objects, cwd=build_dir)
if link.returncode != 0:
    raise SystemExit("dtape-receive-copyout-context: contract link failed (RED if DARLING_SRC_ROOT is a pre-.6 tree)")

run = subprocess.run([str(workdir / "contract")], capture_output=True, text=True)
sys.stdout.write(run.stdout)
sys.stderr.write(run.stderr)
if run.returncode != 0:
    raise SystemExit(f"dtape-receive-copyout-context: FAIL (exit {run.returncode})")
if "DTAPE-RECEIVE-COPYOUT-CONTEXT PASS" not in run.stdout:
    raise SystemExit("dtape-receive-copyout-context: FAIL: PASS marker missing")
PY

echo "DTAPE-RECEIVE-COPYOUT-CONTEXT: host contract PASS"

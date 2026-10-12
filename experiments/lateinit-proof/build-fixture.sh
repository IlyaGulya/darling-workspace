#!/usr/bin/env bash
# Build the guest Mach-O dlopen fixture by replaying the recorded compile/link
# commands of an existing guest executable target in the Darling build.
# Usage: build-fixture.sh [BUILD_DIR] [REFERENCE_TARGET] [OUT]
set -euo pipefail
build_dir="${1:-/home/ilyagulya/work/wr-fresh/build/verify14}"
ref="${2:-darling_bzero_return_regress}"
out="${3:-$(pwd)/lateinit_probe}"
src="$(cd "$(dirname "$0")" && pwd)/lateinit_dlopen.c"
work="$(mktemp -d)"
test -f "$build_dir/build.ninja" || { echo "no build.ninja in $build_dir" >&2; exit 2; }
ninja -C "$build_dir" -t commands "$ref" > "$work/cmds"
python3 -B - "$work/cmds" "$src" "$work" "$build_dir" "$ref" "$out" <<'PY'
import re, subprocess, sys
from pathlib import Path
cmds, fixture_src, workdir, build_dir, ref, out = sys.argv[1:7]
lines = Path(cmds).read_text().splitlines()
obj = str(Path(workdir) / "fixture.o")
compile_line = next((l for l in lines if re.search(r"-c\s+\S*%s\.c\b" % re.escape(ref), l)), None)
link_line = next((l for l in lines if "-fuse-ld=" in l and ref in l), None)
if compile_line is None or link_line is None:
    raise SystemExit("no recorded guest Mach-O build for %s" % ref)
subprocess.run(re.sub(r"-c\s+\S*%s\.c\b" % re.escape(ref), "-c " + fixture_src,
               re.sub(r"-o\s+\S*%s\.c\.o" % re.escape(ref), "-o " + obj, compile_line)),
               shell=True, check=True, cwd=build_dir)
seg = [p.strip() for p in link_line.split("&&") if "-fuse-ld=" in p][0]
seg = re.sub(r"-o\s+\S*%s(\s|$)" % re.escape(ref), "-o " + out + " ",
             re.sub(r"\S*%s\.c\.o" % re.escape(ref), obj, seg))
subprocess.run(seg, shell=True, check=True, cwd=build_dir)
PY
file "$out"

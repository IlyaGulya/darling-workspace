#!/usr/bin/env bash
# Clean-base reproduction of the transport work (directive section 3E).
#
# What this proves and what it does not: it rebuilds the PRODUCT from a clean clone of the recorded base revision plus
# the recorded source archive, with a FRESH configure into a FRESH build directory and a SEPARATE install prefix. It does
# not touch the live prefix, and it deliberately does not reuse the dirty build tree.
set -u
SRC_REPO=/home/ilyagulya/work/procctl-src
BASE=8f33c0cd89728f17ea8700ede8edcb2b40129952
CLEAN=/home/ilyagulya/work/r1-clean-base
CLEAN_BUILD=/home/ilyagulya/work/r1-clean-build
CLEAN_PREFIX=/tmp/r1-clean-prefix
EVID=/home/ilyagulya/work/darling-dev/evidence
ARCHIVE=$(ls -1t "$EVID"/r1-transport-sources-*.tar.gz 2>/dev/null | head -1)

echo "== 0. regenerate the durable artifact from the CURRENT tree =="
python3 -B - "$SRC_REPO" "$EVID" <<'PY'
import hashlib, os, pathlib, subprocess, sys, tarfile, time
src, evid = sys.argv[1], sys.argv[2]
files = []
for root, dirs, names in os.walk(src):
    dirs[:] = [d for d in dirs if d not in ('.git', 'build')]
    for n in names:
        p = os.path.join(root, n)
        try:
            if os.path.getmtime(p) >= time.mktime(time.strptime('2026-09-27', '%Y-%m-%d')):
                files.append(p)
        except OSError:
            pass
files.sort()
stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
arch = os.path.join(evid, f'r1-transport-sources-{stamp}.tar.gz')
with tarfile.open(arch, 'w:gz') as tf:
    for f in files:
        tf.add(f, arcname=os.path.relpath(f, src), recursive=False)
h = hashlib.sha256(open(arch, 'rb').read()).hexdigest()
man = arch.replace('.tar.gz', '.sha256')
with open(man, 'w') as fh:
    for f in files:
        fh.write(f"{hashlib.sha256(open(f,'rb').read()).hexdigest()}  {os.path.relpath(f, src)}\n")
base = subprocess.run(['git','log','--format=%H','-1'], cwd=src, capture_output=True, text=True).stdout.strip()
print(f"archive {arch} files={len(files)} bytes={os.path.getsize(arch)} sha256={h}")
print(f"manifest {man}")
print(f"base commit {base}")
open(os.path.join(evid, 'r1-transport-sources-LATEST'), 'w').write(f"{arch}\nsha256={h}\nbase={base}\n")
PY

echo "== 1. clean clone of the base revision =="
rm -rf "$CLEAN"
git clone --quiet "$SRC_REPO" "$CLEAN" || { echo "clone failed"; exit 1; }
git -C "$CLEAN" checkout --quiet "$BASE" || { echo "checkout $BASE failed"; exit 1; }
echo "clean tree at $(git -C "$CLEAN" rev-parse --short HEAD), files=$(git -C "$CLEAN" ls-files | wc -l)"

echo "== 2. apply the recorded archive =="
ARCHIVE=$(head -1 "$EVID/r1-transport-sources-LATEST")
tar -xzf "$ARCHIVE" -C "$CLEAN" || { echo "apply failed"; exit 1; }
echo "applied $(tar -tzf "$ARCHIVE" | wc -l) paths from $(basename "$ARCHIVE")"

echo "== 3. FRESH configure (identity extracted from the working build tree) =="
rm -rf "$CLEAN_BUILD" "$CLEAN_PREFIX"; mkdir -p "$CLEAN_PREFIX"
cmake -S "$CLEAN" -B "$CLEAN_BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug -DCMAKE_INSTALL_PREFIX="$CLEAN_PREFIX" \
  -DDARLING_EUNION=ON -DDARLING_MALLOC_REGION_TEST=ON -DDARLING_RING_TRANSPORT=ON \
  -DDARLING_ROOTLESS_HOMEBREW=ON -DDARLING_ROOTLESS_TOOLCHAIN=ON -DDARLING_SKIP_DRIFT_GATE=ON \
  > "$CLEAN_BUILD.configure.log" 2>&1 || { echo "CONFIGURE FAILED (log: $CLEAN_BUILD.configure.log)"; tail -20 "$CLEAN_BUILD.configure.log"; exit 1; }
echo "configure OK -> $CLEAN_BUILD"

echo "== 4. build the three artifacts that the transport work touches =="
( cd "$CLEAN_BUILD" && ninja mldr darlingserver libsystem_kernel.dylib dyld ) > "$CLEAN_BUILD.build.log" 2>&1
rc=$?
echo "BUILD rc=$rc (log: $CLEAN_BUILD.build.log)"
if [ $rc -eq 0 ]; then
  for f in src/startup/mldr/mldr src/external/darlingserver/darlingserver src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib src/external/dyld/dyld; do
    [ -f "$CLEAN_BUILD/$f" ] && echo "  built $(sha256sum "$CLEAN_BUILD/$f" | cut -c1-16) $f"
  done
fi
echo "CLEAN-BASE-DONE rc=$rc"

#!/usr/bin/env bash
# Clean-base reproduction of the transport work (directive section 3E).
#
# Proves: the PRODUCT rebuilds from a clean materialization of the pinned upstream revision plus a CONTENT-based delta of
# the working tree, with a FRESH configure into a FRESH build directory and a SEPARATE install prefix.
# Does not touch the live prefix, does not reuse the dirty build tree, does not copy any working tree.
#
# Two lessons are baked in, both measured:
#   * the local product commit is EMPTY, so "base revision" cannot mean it: the base is the pinned west revision.
#   * submodules have submodules: the walk recurses (a one-level walk left metal/deps/indium empty and configure failed).
#   * a delta selected by modification time is INCOMPLETE: the tree carries untracked, build-required files
#     (e.g. src/startup/mldr/signal_atomic.h). The delta is therefore diff-based, which is complete by construction.
set -u

SRC_REPO=/home/ilyagulya/work/procctl-src
DARLING=/home/ilyagulya/work/darling-gwn-resume/darling
PINNED=5f2d7401d878455cf3c3c0865ee5a4290dfa03f0
REF=/home/ilyagulya/work/r1-clean-ref
CLEAN=/home/ilyagulya/work/r1-clean-base
CLEAN_BUILD=/home/ilyagulya/work/r1-clean-build
CLEAN_PREFIX=/tmp/r1-clean-prefix
EVID=/home/ilyagulya/work/darling-dev/evidence

mat_one() { # $1 repository dir, $2 commit, $3 destination
	local repo="$1" sha="$2" dest="$3" sha_n path dir
	git -C "$repo" archive "$sha" 2>/dev/null | tar -x -C "$dest" || { echo "  ARCHIVE FAILED $repo@$sha"; return 1; }
	while read -r sha_n path; do
		[ -n "$path" ] || continue
		dir="$repo/$path"
		if git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
			mkdir -p "$dest/$path"
			mat_one "$dir" "$sha_n" "$dest/$path"
		else
			echo "  no local repository for nested submodule: $path @ $sha_n"
		fi
	done < <(git -C "$repo" ls-tree -r "$sha" | awk '$2 == "commit" {print $3" "$4}')
}

echo "== 0a. reference materialization of the base =="
rm -rf "$REF"; mkdir -p "$REF"
mat_one "$DARLING" "$PINNED" "$REF"
echo "reference files=$(find "$REF" -type f | wc -l)"

echo "== 0b. CONTENT-based delta (every differing or missing path) =="
python3 -B - "$SRC_REPO" "$REF" "$EVID" <<'PY'
import hashlib, os, subprocess, sys, tarfile, time
src, ref, evid = sys.argv[1], sys.argv[2], sys.argv[3]
out = subprocess.run(['diff', '-rq', '--no-dereference', ref, src], capture_output=True, text=True).stdout
changed = set()
for line in out.splitlines():
    if line.startswith('Files ') and ' differ' in line:
        changed.add(os.path.relpath(line.split(' and ')[1].rsplit(' differ', 1)[0], src))
    elif line.startswith('Only in '):
        head, name = line[len('Only in '):].split(': ', 1)
        p = os.path.join(head, name)
        # A path that exists only in the REFERENCE is not part of the delta (the delta is what the working tree adds or
        # changes). MEASURED: without this guard the script tried to archive a base-only path relative to the working
        # tree and died on a path that cannot exist.
        if not p.startswith(src + os.sep):
            if os.path.isdir(p):
                for root, _dirs, names in os.walk(p):
                    for n in names:
                        changed.add(os.path.relpath(os.path.join(root, n), src)) if False else None
            continue
        if os.path.isfile(p):
            changed.add(os.path.relpath(p, src))
        elif os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in names:
                    changed.add(os.path.relpath(os.path.join(root, n), src))
changed = sorted(changed)
stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
arch = os.path.join(evid, f'r1-transport-sources-{stamp}.tar.gz')
man = arch.replace('.tar.gz', '.sha256')
# Symlinks are skipped: the SDK stubs are dangling links by design (libSystem.dylib -> a versioned file that the tree
# does not carry), MEASURED to abort the tar step, and a link is not a product change. If one ever matters, the build
# will name it and it can be handled explicitly instead of guessed at.
skipped_links = [rel for rel in changed if os.path.islink(os.path.join(src, rel))]
changed = [rel for rel in changed if not os.path.islink(os.path.join(src, rel))]
missing = [rel for rel in changed if not os.path.isfile(os.path.join(src, rel))]
changed = [rel for rel in changed if os.path.isfile(os.path.join(src, rel))]
if skipped_links:
    print(f"skipped {len(skipped_links)} symlink path(s), e.g. {skipped_links[0]}")
if missing:
    print(f"skipped {len(missing)} non-regular path(s), e.g. {missing[0]}")
with tarfile.open(arch, 'w:gz') as tf:
    for rel in changed:
        tf.add(os.path.join(src, rel), arcname=rel, recursive=False)
with open(man, 'w') as fh:
    for rel in changed:
        fh.write(f"{hashlib.sha256(open(os.path.join(src, rel), 'rb').read()).hexdigest()}  {rel}\n")
open(os.path.join(evid, 'r1-transport-sources-LATEST'), 'w').write(
    f"{arch}\npaths={len(changed)}\nbase={ref}\npinned={os.environ.get('PINNED_FOR_MANIFEST', '')}\n")
print(f"delta paths={len(changed)} bytes={os.path.getsize(arch)}")
print(f"manifest {man}")
print(f"includes signal_atomic.h: {'src/startup/mldr/signal_atomic.h' in changed}")
PY

echo "== 1. materialize a FRESH tree at the pinned revision =="
rm -rf "$CLEAN"; mkdir -p "$CLEAN"
mat_one "$DARLING" "$PINNED" "$CLEAN"
echo "clean files=$(find "$CLEAN" -type f | wc -l)"

echo "== 2. apply the delta =="
ARCHIVE=$(head -1 "$EVID/r1-transport-sources-LATEST")
tar -xzf "$ARCHIVE" -C "$CLEAN" || { echo "apply failed"; exit 1; }
echo "applied $(tar -tzf "$ARCHIVE" | wc -l) paths from $(basename "$ARCHIVE")"

echo "== 3. FRESH configure =="
rm -rf "$CLEAN_BUILD" "$CLEAN_PREFIX"; mkdir -p "$CLEAN_PREFIX"
cmake -S "$CLEAN" -B "$CLEAN_BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug -DCMAKE_INSTALL_PREFIX="$CLEAN_PREFIX" \
  -DDARLING_EUNION=ON -DDARLING_MALLOC_REGION_TEST=ON -DDARLING_RING_TRANSPORT=ON \
  -DDARLING_ROOTLESS_HOMEBREW=ON -DDARLING_ROOTLESS_TOOLCHAIN=ON -DDARLING_SKIP_DRIFT_GATE=ON \
  > "$CLEAN_BUILD.configure.log" 2>&1 || { echo "CONFIGURE FAILED"; tail -15 "$CLEAN_BUILD.configure.log"; exit 1; }
echo "configure OK -> $CLEAN_BUILD"

echo "== 4. build the artifacts the transport work touches =="
( cd "$CLEAN_BUILD" && ninja mldr darlingserver libsystem_kernel.dylib dyld ) > "$CLEAN_BUILD.build.log" 2>&1
rc=$?
echo "BUILD rc=$rc (log: $CLEAN_BUILD.build.log)"
[ $rc -ne 0 ] && grep -m4 -iE "error|fatal" "$CLEAN_BUILD.build.log" | cut -c1-180
for f in src/startup/mldr/mldr src/external/darlingserver/darlingserver src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib src/external/dyld/dyld; do
	[ -f "$CLEAN_BUILD/$f" ] && echo "  built $(sha256sum "$CLEAN_BUILD/$f" | cut -c1-16) $f"
done
echo "CLEAN-BASE-DONE rc=$rc"

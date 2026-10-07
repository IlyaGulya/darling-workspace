#!/usr/bin/env bash
# Declared runtime additions for the iOS toolchain lane.
#
# A freshly bootstrapped Darling prefix carries the minimal runtime, which is enough to boot but
# not enough for the Apple toolchain: Xcode 26.3's clang, ld and the frameworks they load need
# the zlib dylib, the full Foundation closure and a libc++.1.dylib that exports the ABI those
# tools were built against. This script installs exactly those artifacts, each through its own
# project's CMake install target -- never by copying a built file into place -- and records a
# receipt. Extend the list only from a real dyld error naming the missing dependency.
#
# Two prefix facts decide how the install has to finish:
#   * a prefix keeps two runtime copies: <prefix>/libexec/darling/... and the guest-visible
#     <prefix>/usr/lib (and <prefix>/System/Library/Frameworks). When both exist, the
#     guest-visible one shadows the other, so an install that only writes libexec/darling has
#     no effect; the guest-visible copy must be refreshed too.
#   * therefore, for every installed file that also exists in the guest-visible tree, this
#     script copies the freshly installed bytes over it.
#
# usage: ios-toolchain-runtime-additions.sh --build-dir BUILD --prefix PREFIX [--receipt FILE]
set -uo pipefail

build_dir=""
prefix=""
receipt=""

while [ $# -gt 0 ]; do
	case "$1" in
	--build-dir) build_dir="$2"; shift 2 ;;
	--prefix) prefix="$2"; shift 2 ;;
	--receipt) receipt="$2"; shift 2 ;;
	*) echo "ios-additions: unknown argument: $1" >&2; exit 2 ;;
	esac
done

[ -n "$build_dir" ] && [ -f "$build_dir/build.ninja" ] || {
	echo "ios-additions: --build-dir must name a configured build" >&2
	exit 2
}
[ -n "$prefix" ] && [ -x "$prefix/bin/darling" ] || {
	echo "ios-additions: --prefix must name a booted prefix" >&2
	exit 2
}

# Each entry is the CMake install target of the project that owns the artifact. The comment
# records the dyld error that asked for it, so the list can be audited against evidence.
targets=(
	src/external/zlib/install                       # /usr/lib/libz.1.dylib missing
	src/external/libcxx/install                     # libc++ ABI: verbose abort + std::__fs::filesystem
	src/external/foundation/install                 # Foundation.framework
	src/external/cfnetwork/src/install              # CFNetwork.framework
	src/private-frameworks/AppleSauce/install       # AppleSauce.framework
	src/external/security/OSX/install               # Security.framework
	src/external/configd/SystemConfiguration.fproj/install
	src/frameworks/CryptoTokenKit/install
	src/external/IOKitUser/install
	src/frameworks/LocalAuthentication/install
	src/private-frameworks/AppleFSCompression/install
	src/libDiagnosticMessagesClient/install
	src/libMobileGestalt/install
	src/external/bzip2/install
	src/external/coretls/install
	src/external/energytrace/install
	src/external/openpam/install
	src/external/sqlite/install
	src/external/xar/install
	src/external/xnu/libkern/kxld/install
)

echo "ios-additions: installing ${#targets[@]} declared additions into $prefix"
( cd "$build_dir" && ninja "${targets[@]}" ) || {
	echo "ios-additions: ninja failed" >&2
	exit 1
}

# Refresh the guest-visible copy of everything libexec/darling now holds and the prefix already
# exposes: the two trees only shadow each other where both have the file.
refreshed_list="$(mktemp)"
( cd "$prefix/libexec/darling" && find . -type f -print ) 2>/dev/null | while read -r rel; do
	src="$prefix/libexec/darling/$rel"
	dst="$prefix/$rel"
	if [ -e "$dst" ]; then
		install -m "$(stat -c '%a' "$src")" "$src" "$dst" 2>/dev/null && echo "$rel" || true
	fi
done >"$refreshed_list"
refreshed="$(wc -l <"$refreshed_list")"
echo "ios-additions: refreshed $refreshed guest-visible file(s)"

if [ -n "$receipt" ]; then
	python3 - "$prefix" "$build_dir" "$receipt" "$refreshed" "${targets[@]}" <<'PYEOF'
import hashlib, json, os, sys
prefix, build_dir, receipt, refreshed = sys.argv[1:5]
targets = sys.argv[5:]
def digest(rel):
    path = os.path.join(prefix, rel)
    if not os.path.isfile(path): return None
    return hashlib.sha256(open(path,'rb').read()).hexdigest()
want = [
    "libexec/darling/usr/lib/libc++.1.dylib",
    "libexec/darling/usr/lib/libz.1.dylib",
    "libexec/darling/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation",
]
open(receipt,'w').write(json.dumps({
    "prefix": prefix, "build_dir": build_dir, "install_targets": targets,
    "refreshed_guest_visible_files": int(refreshed),
    "artifacts": [{"path": r, "sha256": digest(r)} for r in want if digest(r)],
}, indent=2, sort_keys=True) + "\n")
PYEOF
	echo "ios-additions: receipt written to $receipt"
fi

rm -f "$refreshed_list"
echo "ios-additions: done"

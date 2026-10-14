#!/usr/bin/env bash
# Declared runtime additions for the iOS toolchain lane.
#
# A freshly bootstrapped Darling prefix carries the minimal runtime, which is enough to boot but
# not enough for the Apple toolchain: Xcode 26.3's clang, ld and the frameworks they load need
# the zlib dylib, the full Foundation closure and a libc++.1.dylib that exports the ABI those
# tools were built against. This script installs exactly those artifacts, each through its own
# project's CMake install target -- never by copying a built file into place -- and records a
# receipt. Extend the list only from a real dyld error naming the missing dependency, or from a
# runtime demand the LOADER never names (the two documented cases below: ICU's data file, which is
# read by path, and libobjc-trampolines.dylib, which libobjc dlopens by absolute name).
#
# THE EXCEPTION, MEASURED (2026-10-13, dar-liaq; prefix-verify14 + build/verify14): the ICU DATA
# file is a runtime demand the LOADER never names. ICU is built with
# -DICU_DATA_DIR="/usr/share/icu/" (darling/src/external/icu/icuSources/CMakeLists.txt:20) and its
# CMake install ships icudt66l.dat to libexec/darling/usr/share/icu, but libicucore.A.dylib alone
# resolves fine, so a prefix that carries only the dylib never produces a dyld error. The failure
# is silent and later: `udat_open` returns NULL with U_MEMORY_ALLOCATION_ERROR (7), CoreFoundation's
# CFDateFormatterCreate returns NULL for it (CFDateFormatter.c:892-896), Foundation's
# -[NSDateFormatter stringFromDate:] then passes that NULL formatter into
# CFDateFormatterCreateStringWithAbsoluteTime, which dereferences it
# (`movq 0x10(%rax),%rdi`, formatter->_df at offset 0x10 with %rax NULL) -- the SIGSEGV the Xcode
# ladder showed in Xcode3Core's error-printing path. So the dependency is real but invisible to the
# loader heuristic this list was written around, and this entry is the audit of that measurement,
# not a relaxation of the rule.
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
	src/external/icu/icuSources/install             # /usr/share/icu/icudt66l.dat missing, a NON-dyld demand (see below)
	src/external/libxml2/install                    # /usr/lib/libxml2.2.dylib missing (T4: Foundation loads it, Apple ld aborts)
	src/external/libcxx/install                     # libc++ ABI: verbose abort + std::__fs::filesystem
	src/external/foundation/install                 # Foundation.framework
	src/external/cfnetwork/src/install              # CFNetwork.framework
	src/private-frameworks/AppleSauce/install       # AppleSauce.framework
	src/private-frameworks/CoreAnalytics/install    # /System/Library/PrivateFrameworks/CoreAnalytics.framework/Versions/A/CoreAnalytics missing (T4: Apple ld)
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
	# /usr/lib/libobjc-trampolines.dylib missing (T4, 2026-10-14): libobjc dlopens this image BY
	# ABSOLUTE NAME at src/external/objc4/runtime/objc-block-trampolines.mm:173 the first time a
	# block trampoline is used, and its _objc_fatal arm aborts (abort_with_payload; both xcodebuild
	# workloads returned XCB_RC=134). Nothing links it, so no dyld "Library not loaded" error ever
	# named it and the deploy closure cannot reach it -- the same invisible-to-the-loader class as
	# the ICU data file above. The tree builds and installs the image; it was simply never declared
	# here, and its own LC_ID is exactly the demanded /usr/lib/libobjc-trampolines.dylib.
	src/external/objc4/runtime/install
	# /System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice missing (T4,
	# 2026-10-14): Xcode's IDEiOSSupportCore has a STRONG LC_LOAD_DYLIB for exactly that path (current
	# version 1818.60.2, compatibility 1.0.0 -- the artifact declares compatibility 1.0.0, which is the
	# value dyld checks) and binds twelve strong eager names from it, so dlopen_preflight refuses the
	# plug-in with "image not found". Nothing links it FROM THIS TREE, so the deploy closure reaches it
	# only through a declaration -- the same class as objc-trampolines above. The tree builds and
	# installs the image; its own LC_ID is exactly the demanded path.
	src/private-frameworks/MobileDevice/install
)

echo "ios-additions: installing ${#targets[@]} declared additions into $prefix"
( cd "$build_dir" && ninja "${targets[@]}" ) || {
	echo "ios-additions: ninja failed" >&2
	exit 1
}

# Refresh the guest-visible copy of everything libexec/darling now holds and the prefix already
# exposes: the two trees only shadow each other where both have the file.
#
# A dependency named by the toolchain's OWN dyld error may not be in the guest-visible tree yet, and
# the walk below cannot create it: it only refreshes files both trees already have. These are the
# exceptions. The real Apple ld aborted with "Library not loaded: /usr/lib/libxml2.2.dylib" while
# loading Foundation, and the file existed only under libexec/darling, which the guest does not
# search for /usr/lib. Declared here, they are installed into the guest-visible tree and then
# refreshed with everything else, so the two copies cannot drift.
guest_visible_required=(
	"usr/lib/libxml2.2.dylib"
	# MEASURED 2026-10-10: the T1-T4 gate's first command aborted with
	# "dyld: Library not loaded: /usr/lib/libz.1.dylib ... Reason: image not found"
	# while the file existed only under libexec/darling, which the guest does not
	# search for /usr/lib. Declared here for the same reason as libxml2.2.
	"usr/lib/libz.1.dylib"
	# MEASURED 2026-10-14: libobjc's dlopen names the GUEST path /usr/lib/libobjc-trampolines.dylib
	# and that name resolves in the guest-visible tree, which shadows libexec/darling. The install
	# target above writes libexec/darling/usr/lib/libobjc-trampolines.dylib; without this the guest
	# copy would not exist and the dlopen would fail exactly as it did before.
	# MEASURED 2026-10-14: Xcode's IDEiOSSupportCore hard-links the CONCRETE framework path
	# System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice, and the install
	# target above writes it under libexec/darling. A System path may not exist in the guest-visible
	# root at all, and the refresh walk below cannot create a name that is missing there, so it is
	# declared: without this the outer root would not carry the image the loader is asked for.
	"System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice"
)
required_list="$(mktemp)"
printf '%s\n' "${guest_visible_required[@]}" >"$required_list"
refreshed_list="$(mktemp)"
( cd "$prefix/libexec/darling" && find . -type f -print ) 2>/dev/null | while read -r rel; do
	rel="${rel#./}"
	src="$prefix/libexec/darling/$rel"
	dst="$prefix/$rel"
	if [ -e "$dst" ] || grep -Fxq "$rel" "$required_list"; then
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
    "usr/lib/libxml2.2.dylib",
    "libexec/darling/usr/lib/libobjc-trampolines.dylib",
    "usr/lib/libobjc-trampolines.dylib",
    "libexec/darling/System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice",
    "System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice",
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

rm -f "$refreshed_list" "$required_list"
echo "ios-additions: done"

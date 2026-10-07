#!/usr/bin/env bash
# iOS toolchain regression gate (rungs T1/T2 of the Xcode compatibility ladder).
#
# T1: the REAL Xcode clang runs under Darling -- `clang --version` exits 0 and reports the
#     Apple toolchain. Before this gate existed an Apple clang died in dyld with
#     "Symbol not found: __ZNSt3__122__libcpp_verbose_abortEPKcz", because Darling's libc++ 13
#     predates that libc++ >= 16 entry point; the fix lives in the pinned darling/libcxx, and
#     this gate is what keeps it honest.
# T2: that same compiler produces real arm64 iPhoneOS Mach-O objects from the real iPhoneOS
#     SDK -- a C translation unit and an Objective-C one that imports UIKit.
#
# T4: the same toolchain LINKS those objects against the real SDK, i.e. the Apple driver invokes the
#     real arm64 Apple ld, which resolves libSystem from the SDK's .tbd stubs and writes a minimal
#     platform-ios arm64 Mach-O. This is the rung that previously hung, and it is also the rung that
#     needs the runtime's libc++ (T3): Apple's ld references std::__fs::filesystem. The gate requires
#     the link to RETURN -- a hang is a failure, not a slow pass -- and the linked product to be an
#     arm64 iOS Mach-O that defines _main.
#
# Nothing here is compiled by the Linux host compiler: the prefix only executes the Apple
# toolchain. The fixture sources live in this repository and the objects are written back into
# it, which is what lets the architecture/platform assertions below run on the host.
#
# Inputs (required):
#   DPREFIX / DARLING_PREFIX   a booted prefix whose runtime is under test
#   XCODE_APP                  host path of Xcode.app (Contents/Developer must exist)
# Optional:
#   IOS_TARGET        clang -target value (default: arm64-apple-ios26.2)
#   IOS_TOOLCHAIN_WAIT  harness bound per run, seconds (default: 60)
set -uo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
xcode_app="${XCODE_APP:-}"
target="${IOS_TARGET:-arm64-apple-ios26.2}"
fixture="$repo_root/tests/ios_toolchain_fixture"
boot_run="$repo_root/scripts/darling-boot-run.sh"
sdk_rel="Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk"
clang_rel="Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"

[ -n "$prefix" ] && [ -x "$prefix/bin/darling" ] || {
	echo "ios-toolchain: DPREFIX must name a booted Darling prefix" >&2
	exit 2
}
[ -n "$xcode_app" ] || {
	echo "ios-toolchain: XCODE_APP must name the Xcode.app tree" >&2
	exit 2
}
[ -x "$xcode_app/$clang_rel" ] || {
	echo "ios-toolchain: no clang under $xcode_app ($clang_rel)" >&2
	exit 2
}
[ -e "$xcode_app/$sdk_rel/SDKSettings.json" ] || {
	echo "ios-toolchain: iPhoneOS SDK missing under $xcode_app/$sdk_rel" >&2
	exit 2
}

# The prefix maps the host root at /Volumes/SystemRoot (SYSTEM_ROOT in Darling's config), so a
# host path is named to the guest by prefixing that. Paths already inside the guest namespace
# are used unchanged.
to_guest() {
	case "$1" in
	/Volumes/SystemRoot/*) printf '%s' "$1" ;;
	*) printf '/Volumes/SystemRoot%s' "$1" ;;
	esac
}
guest_xcode="$(to_guest "$xcode_app")"
guest_fixture="$(to_guest "$fixture")"

mkdir -p "$fixture/out"
rm -f "$fixture/out/hello_c.o" "$fixture/out/hello_m.o" "$fixture/out/hello_main.o" "$fixture/out/hello_ios"

probe_host="$prefix/private/var/tmp/ios-toolchain-probe.sh"
out_guest="/private/var/tmp/ios-toolchain-contract.out"
out_host="$prefix/private/var/tmp/ios-toolchain-contract.out"
: >"$out_host"

cat >"$probe_host" <<PROBE
#!/bin/bash
OUT="$out_guest"
: > "\$OUT"
export DEVELOPER_DIR="$guest_xcode/Contents/Developer"
run() { printf '\\n== %s\\n' "\$*" >>"\$OUT"; "\$@" >>"\$OUT" 2>>"\$OUT"; printf 'rc=%d\\n' \$? >>"\$OUT"; }
run "$guest_xcode/$clang_rel" --version
run "$guest_xcode/$clang_rel" -target $target -isysroot "$guest_xcode/$sdk_rel" -c "$guest_fixture/hello.c" -o "$guest_fixture/out/hello_c.o"
run "$guest_xcode/$clang_rel" -target $target -isysroot "$guest_xcode/$sdk_rel" -fobjc-arc -c "$guest_fixture/hello.m" -o "$guest_fixture/out/hello_m.o"
run "$guest_xcode/$clang_rel" -target $target -isysroot "$guest_xcode/$sdk_rel" -c "$guest_fixture/hello_main.c" -o "$guest_fixture/out/hello_main.o"
run "$guest_xcode/$clang_rel" -target $target -isysroot "$guest_xcode/$sdk_rel" "$guest_fixture/out/hello_c.o" "$guest_fixture/out/hello_main.o" -o "$guest_fixture/out/hello_ios"
printf 'IOS-TOOLCHAIN-LINK-DONE\n' >>"\$OUT"
echo IOS-TOOLCHAIN-LINK-DONE
printf 'IOS-TOOLCHAIN-PROBE-DONE\n' >>"\$OUT"
echo IOS-TOOLCHAIN-PROBE-DONE
PROBE
chmod +x "$probe_host"

fail=0
say() { printf '%s\n' "$*"; }

harness="$(bash "$boot_run" --prefix "$prefix" --wait "${IOS_TOOLCHAIN_WAIT:-120}" \
	--marker 'IOS-TOOLCHAIN-PROBE-DONE' \
	--marker 'IOS-TOOLCHAIN-LINK-DONE' \
	--cmd "/bin/bash /private/var/tmp/$(basename "$probe_host")" 2>&1)"
printf '%s\n' "$harness" | grep -q 'VERDICT: PASS' || {
	say "ios-toolchain: harness verdict was not PASS"
	say "$harness" | tail -5
	fail=1
}

# The guest's own diagnostic marks interleave on fd 2; keep the real output lines only.
real_out="$(grep -av '^\[' "$out_host" 2>/dev/null | grep -avE '^\s*$')"
printf '%s\n' "$real_out"

# T1: the FIRST rc= after the clang --version banner must be 0.
clang_rc="$(printf '%s\n' "$real_out" | awk '/clang --version/{seen=1} seen && /^rc=/{sub(/^rc=/,""); print; exit}')"
if [ "${clang_rc:-1}" != "0" ]; then
	say "ios-toolchain: T1 FAIL -- the real Xcode clang did not return 0 (rc=${clang_rc:-?})"
	fail=1
fi
printf '%s\n' "$real_out" | grep -q 'Apple clang version' || {
	say "ios-toolchain: T1 FAIL -- clang --version did not report the Apple toolchain"
	fail=1
}

# T2: both objects exist and carry iOS/arm64/SDK metadata.
for pair in "hello_c.o:hello.c" "hello_m.o:hello.m"; do
	obj="$fixture/out/${pair%%:*}"
	src="${pair##*:}"
	if [ ! -s "$obj" ]; then
		say "ios-toolchain: T2 FAIL -- $src produced no object"
		fail=1
		continue
	fi
	info="$(llvm-readobj --macho-version-min "$obj" 2>&1)"
	printf '%s\n' "$info" | grep -q 'Format: Mach-O arm64' ||
		{ say "ios-toolchain: T2 FAIL -- $src is not arm64 Mach-O"; fail=1; }
	printf '%s\n' "$info" | grep -q 'Platform: ios' ||
		{ say "ios-toolchain: T2 FAIL -- $src is not an iOS object"; fail=1; }
	printf '%s\n' "$info" | grep -q 'SDK: 26\.' ||
		{ say "ios-toolchain: T2 FAIL -- $src does not record the iPhoneOS 26.x SDK"; fail=1; }
	printf '%s\n' "$info" | grep -q 'Version: 26\.2' ||
		{ say "ios-toolchain: T2 FAIL -- $src minos is not the requested $target"; fail=1; }
done

# T4: the link returned, and the product is a platform-ios arm64 Mach-O that defines _main.
link_rc="$(printf '%s\n' "$real_out" | awk 'index($0,"hello_ios"){seen=1; next} seen && /^rc=/{sub(/^rc=/,""); print; exit}')"
if [ "${link_rc:-1}" != "0" ]; then
	say "ios-toolchain: T4 FAIL -- the link did not return 0 (rc=${link_rc:-?}; no rc at all means it never returned)"
	fail=1
fi
linked="$fixture/out/hello_ios"
if [ ! -s "$linked" ]; then
	say "ios-toolchain: T4 FAIL -- the link produced no product"
	fail=1
else
	link_info="$(llvm-readobj --macho-version-min "$linked" 2>&1)"
	printf '%s\n' "$link_info" | grep -q 'Format: Mach-O arm64' ||
		{ say "ios-toolchain: T4 FAIL -- the linked product is not arm64 Mach-O"; fail=1; }
	printf '%s\n' "$link_info" | grep -q 'Platform: ios' ||
		{ say "ios-toolchain: T4 FAIL -- the linked product is not an iOS image"; fail=1; }
	printf '%s\n' "$link_info" | grep -q 'SDK: 26\.' ||
		{ say "ios-toolchain: T4 FAIL -- the linked product does not record the iPhoneOS 26.x SDK"; fail=1; }
	llvm-nm "$linked" 2>/dev/null | grep -qE ' T _main$' ||
		{ say "ios-toolchain: T4 FAIL -- the linked product does not define _main"; fail=1; }
fi

[ "$fail" -eq 0 ] || exit 1
say "ios-toolchain: PASS (T1 real clang runs; T2 real arm64 iPhoneOS objects compiled from the real SDK; T4 linked by the real Apple ld)"

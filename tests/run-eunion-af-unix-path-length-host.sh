#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
xnu="${XNU_SRC_ROOT:-$root/../darling/src/external/xnu}"
emulation="$xnu/darling/src/libsystem_kernel/emulation"
work="$(mktemp -d /tmp/eunion-af-unix-length.XXXXXX)"
cleanup() {
	rm -rf -- "$work"
}
trap cleanup EXIT

test -f "$emulation/src/xnu_syscall/bsd/helper/network/duct.c"
mkdir -p "$work/shim/darling/emulation/linux_premigration"
cp "$emulation/include/linux_premigration/vchroot_expand.h" \
	"$work/shim/darling/emulation/linux_premigration/"
mkdir -p "$work/shim/darling/emulation/common/bsdthread"
cp "$root/experiments/e-union/shim/darling/emulation/common/bsdthread/per_thread_wd.h" \
	"$work/shim/darling/emulation/common/bsdthread/"
mkdir -p "$work/include/darling"
ln -s "$emulation/include" "$work/include/darling/emulation"

mkdir -p "$work/root/prefix/libexec/darling"
printf 'LOWER_SENTINEL\n' >"$work/root/prefix/libexec/darling/sentinel"
lower_before="$(sha256sum "$work/root/prefix/libexec/darling/sentinel")"

gcc -std=gnu11 -Wall -Wextra \
	-Wno-format-overflow -Wno-unused-variable -Wno-unused-function \
	-DEUNION -DEFAULT=14 \
	-DEUNION_LIBEXEC_PATH="\"$work/root/prefix/libexec/darling\"" \
	-I"$emulation/src/linux_premigration" \
	-I"$emulation/include" -I"$work/shim" -I"$work/include" \
	-o "$work/fixture" \
	"$root/tests/eunion_af_unix_path_length_host.c" \
	"$root/experiments/e-union/whiteout_hook_fallback.c" \
	"$emulation/src/linux_premigration/eunion_resolver.c" \
	"$emulation/src/conversion/network/duct.c" \
	"$emulation/src/xnu_syscall/bsd/helper/network/duct.c"

"$work/fixture" "$work/root"

lower_after="$(sha256sum "$work/root/prefix/libexec/darling/sentinel")"
test "$lower_before" = "$lower_after"
if find "$work/root" -type s -print -quit | grep -q .; then
	echo "AF_UNIX fixture left a socket behind" >&2
	exit 1
fi
printf 'PASS eunion-af-unix-path-length-host\n'

#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
xnu="${XNU_SRC_ROOT:-$root/../darling/src/external/xnu}"
emulation="$xnu/darling/src/libsystem_kernel/emulation"
darling="${DARLING_SRC:-$(cd "$xnu/../../.." && pwd)}"
for input in "$emulation/src/linux_premigration/vchroot_userspace.c" \
	"$emulation/src/linux_premigration/eunion_resolver.c" \
	"$emulation/src/xnu_syscall/bsd/helper/network/duct.c" \
	"$darling/src/external/darlingserver/include/darlingserver/rpc-supplement.h" \
	"$darling/src/startup/mldr/elfcalls/elfcalls.h" \
	"$darling/src/external/darling-dmg/src/DADeviceProtocol.h"; do
	test -f "$input" || { echo "missing selected-source input: $input" >&2; exit 2; }
done
state="${DW_STATE_ROOT:-$(cd "$root/.." && pwd)/.west-state}"
case "$state" in
	/*) ;;
	*) echo 'DW_STATE_ROOT must be absolute' >&2; exit 2 ;;
esac
test -n "${state//\/}" || { echo 'DW_STATE_ROOT must not be the filesystem root' >&2; exit 2; }
mkdir -p "$state/h"
work="$(mktemp -d "$state/h/sXXXXXXX")"
cleanup() {
	rm -rf -- "$work"
}
trap cleanup EXIT
mkdir -p "$work/include/darling"
ln -s "$emulation/include" "$work/include/darling/emulation"

mkdir -p "$work/root/prefix/libexec/darling"
printf 'LOWER_SENTINEL\n' >"$work/root/prefix/libexec/darling/sentinel"
lower_before="$(sha256sum "$work/root/prefix/libexec/darling/sentinel")"

"${CC:-gcc}" -std=gnu11 -Wall -Wextra -Ulinux \
	-Wno-format-overflow -Wno-unused-variable -Wno-unused-function \
	-DTEST=1 -DEUNION -DEFAULT=14 \
	-ffunction-sections -fdata-sections -Wl,--gc-sections \
	-DEUNION_LIBEXEC_PATH="\"$work/root/prefix/libexec/darling\"" \
	-I"$emulation/src/linux_premigration" \
	-I"$emulation/src" \
	-I"$emulation/include" -I"$root/experiments/e-union/shim" -I"$work/include" \
	-I"$darling/src/external/darlingserver/include" \
	-I"$darling/src/startup/mldr/elfcalls" \
	-I"$darling/src/external/darling-dmg/src" \
	-o "$work/fixture" \
	"$root/tests/eunion_af_unix_path_length_host.c" \
	"$root/experiments/e-union/simple_format_host.c" \
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

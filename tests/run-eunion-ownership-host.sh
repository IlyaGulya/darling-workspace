#!/usr/bin/env bash
# Compile the selected production syscall/vchroot closure; no guest boot needed.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
xnu="${XNU_SRC_ROOT:-$root/../darling/src/external/xnu}"
emulation="$xnu/darling/src/libsystem_kernel/emulation"
darling="${DARLING_SRC:-$(cd "$xnu/../../.." && pwd)}"
for input in "$emulation/src/linux_premigration/vchroot_userspace.c" \
    "$emulation/src/linux_premigration/eunion_resolver.c" \
    "$darling/src/external/darlingserver/include/darlingserver/rpc-supplement.h" \
    "$darling/src/startup/mldr/elfcalls/elfcalls.h"; do
    test -f "$input" || { echo "missing selected-source input: $input" >&2; exit 2; }
done
state="${DW_STATE_ROOT:-$(cd "$root/.." && pwd)/.west-state}"
case "$state" in
    /*) ;;
    *) echo 'DW_STATE_ROOT must be absolute' >&2; exit 2 ;;
esac
test -n "${state//\/}" || { echo 'DW_STATE_ROOT must not be the filesystem root' >&2; exit 2; }
mkdir -p "$state/h"
work="$(mktemp -d "$state/h/oXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/include/darling"
ln -s "$emulation/include" "$work/include/darling/emulation"
sources=(
    "$root/tests/eunion_ownership_host.c"
    "$root/experiments/e-union/simple_format_host.c"
    "$emulation/src/xnu_syscall/bsd/helper/misc/common_at.c"
    "$emulation/src/conversion/errno.c"
    "$emulation/src/linux_premigration/eunion_resolver.c"
)
for name in chown fchown fchownat lchown; do
    sources+=("$emulation/src/xnu_syscall/bsd/impl/unistd/$name.c")
done
"${CC:-gcc}" -std=gnu11 -Wall -Wextra -Wno-unused-function -Wno-unused-variable \
    -Wno-format-truncation -Ulinux -DTEST=1 -DEUNION -DEFAULT=14 \
    -ffunction-sections -fdata-sections -Wl,--gc-sections \
    -I"$emulation/src" -I"$emulation/src/linux_premigration" -I"$emulation/include" \
    -I"$root/experiments/e-union/shim" -I"$work/include" \
    -I"$darling/src/external/darlingserver/include" \
    -I"$darling/src/startup/mldr/elfcalls" \
    -o "$work/fixture" "${sources[@]}"
# The second layout mirrors rootless Darling: LOWER is nested beneath UPPER.
mkdir -p "$work/sibling/upper" "$work/sibling/lower" \
    "$work/nested/upper/libexec/darling"
"$work/fixture" "$work/sibling/upper" "$work/sibling/lower"
"$work/fixture" "$work/nested/upper" "$work/nested/upper/libexec/darling"
printf 'PASS eunion-ownership-host\n'

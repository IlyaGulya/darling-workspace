#!/usr/bin/env bash
# Compile the selected production syscall/vchroot closure; no guest boot needed.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
xnu="${XNU_SRC_ROOT:-$root/../darling/src/external/xnu}"
emulation="$xnu/darling/src/libsystem_kernel/emulation"
work="$(mktemp -d /tmp/eunion-ownership.XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/include/darling" "$work/shim/darling/emulation/common/bsdthread" \
    "$work/shim/darling/emulation/other/mach"
ln -s "$emulation/include" "$work/include/darling/emulation"
cp "$root/experiments/e-union/shim/darling/emulation/common/bsdthread/per_thread_wd.h" \
    "$work/shim/darling/emulation/common/bsdthread/"
cp "$root/experiments/e-union/shim/darling/emulation/other/mach/lkm.h" \
    "$work/shim/darling/emulation/other/mach/"
sources=(
    "$root/tests/eunion_ownership_host.c"
    "$root/experiments/e-union/whiteout_hook_fallback.c"
    "$emulation/src/xnu_syscall/bsd/helper/misc/common_at.c"
    "$emulation/src/conversion/errno.c"
)
for name in chown fchown fchownat lchown; do
    sources+=("$emulation/src/xnu_syscall/bsd/impl/unistd/$name.c")
done
# Historical guard source-base may predate the resolver extraction. Both arms
# compile their actual production closure and execute the same ownership oracle.
if test -f "$emulation/src/linux_premigration/eunion_resolver.c"; then
    sources+=("$emulation/src/linux_premigration/eunion_resolver.c")
fi
"${CC:-gcc}" -std=gnu11 -Wall -Wextra -Wno-unused-function -Wno-unused-variable \
    -Wno-format-truncation -DEUNION -DEFAULT=14 \
    -I"$emulation/src/linux_premigration" -I"$emulation/include" \
    -I"$work/shim" -I"$work/include" -o "$work/fixture" "${sources[@]}"
# The second layout mirrors rootless Darling: LOWER is nested beneath UPPER.
mkdir -p "$work/sibling/upper" "$work/sibling/lower" \
    "$work/nested/upper/libexec/darling"
"$work/fixture" "$work/sibling/upper" "$work/sibling/lower"
"$work/fixture" "$work/nested/upper" "$work/nested/upper/libexec/darling"
printf 'PASS eunion-ownership-host\n'

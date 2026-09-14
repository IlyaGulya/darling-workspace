#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./apply-and-check.sh /path/to/darlingserver /path/to/darling-xnu
#
# This script deliberately refuses to apply the patches to an unexpected base.

server=${1:?darlingserver checkout required}
xnu=${2:?darling-xnu checkout required}
here=$(cd -- "$(dirname -- "$0")" && pwd)

server_base=82f41e2e0dc352c18dd8910963d908395c94afda
xnu_base=88dcbf670cd4d1c000dd7f7d95324784bafb0dca

check_clean_base() {
    local repo=$1 expected=$2 label=$3
    test -d "$repo/.git" || { echo "$label: not a git checkout: $repo" >&2; exit 2; }
    test -z "$(git -C "$repo" status --porcelain)" || { echo "$label: checkout is dirty" >&2; exit 2; }
    actual=$(git -C "$repo" rev-parse HEAD)
    test "$actual" = "$expected" || {
        echo "$label: expected base $expected, got $actual" >&2
        exit 2
    }
}

check_clean_base "$server" "$server_base" darlingserver
check_clean_base "$xnu" "$xnu_base" darling-xnu

git -C "$server" apply --check "$here/darlingserver.patch"
git -C "$xnu" apply --check "$here/darling-xnu.patch"

git -C "$server" apply "$here/darlingserver.patch"
git -C "$xnu" apply "$here/darling-xnu.patch"

python3 -m py_compile "$server/scripts/generate-rpc-wrappers.py"

# Structural assertions before a full Darling build.
python3 - "$server" "$xnu" <<'PY'
import pathlib, sys
server = pathlib.Path(sys.argv[1])
xnu = pathlib.Path(sys.argv[2])
gen = (server / 'scripts/generate-rpc-wrappers.py').read_text()
call = (server / 'src/call.cpp').read_text()
proc = (server / 'src/process.cpp').read_text()
hdr = (server / 'internal-include/darlingserver/process.hpp').read_text()
guest = (xnu / 'darling/src/libsystem_kernel/emulation/src/linux_premigration/vchroot_userspace.c').read_text()

vstart = gen.index("('vchroot', [")
vend = gen.index("], []),", vstart)
vblock = gen[vstart:vend]
assert '@fd' not in vblock
assert "('path', 'const char*', 'uint64_t')" in vblock
assert "('path_size', 'uint64_t')" in vblock
assert 'setVchrootDirectory' not in call + proc + hdr
assert '_vchrootDescriptor' not in proc + hdr
assert 'setVchrootPath' in call + proc + hdr
assert 'dserver_rpc_vchroot(path_snapshot, (uint64_t)rv)' in guest
assert 'dserver_rpc_vchroot(dfd)' not in guest
print('static checks: PASS')
PY

echo
printf 'Patches applied. Next mandatory step: regenerate RPC wrappers, build darlingserver + xnu, and run guest acceptance.\n'

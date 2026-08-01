#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
server_root="${DSERVER_SRC_ROOT:-$workspace_root/../darling/src/external/darlingserver}"
work="$(mktemp -d /tmp/dserver-runtime-mode-contract.XXXXXX)"

cleanup() {
	rm -rf -- "$work"
}
trap cleanup EXIT

c++ -std=c++17 -Wall -Wextra -Werror \
	-I"$server_root/include" \
	"$server_root/tests/runtime_mode_test.cpp" \
	"$server_root/src/runtime-mode.cpp" \
	-o "$work/runtime-mode-test"

test "$("$work/runtime-mode-test")" = "DSERVER_RUNTIME_MODE_CONTRACT_OK"

python3 -B - "$server_root" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
source = (root / "src/darlingserver.cpp").read_text()
selection = source.index("requireRuntimeModeFromEnvironment(")
marker = source.index("anchorRuntimeModePrefix(")
credentials = source.index("validateRootlessProcessCredentials(")
subreaper = source.index("prctl(PR_SET_CHILD_SUBREAPER")
home = source.index("setupUserHome(prefixFD, originalUID)")
namespace = source.index("unshare(CLONE_NEWNS)")
if not selection < marker < credentials < subreaper < home < namespace:
    raise SystemExit(
        "darlingserver mutates runtime state before typed/credential validation"
    )
if "shouldUseOverlayFs" in source or "shouldUseEunionPrefix" in source:
    raise SystemExit("darlingserver retained a boolean mode decision")
if 'getenv("DARLING_ROOTLESS")' in source:
    raise SystemExit("darlingserver retained legacy rootless selection")
if 'setenv("__mldr_runtime_mode"' not in source:
    raise SystemExit("darlingserver does not publish typed launchd bootstrap mode")
if 'unsetenv("DARLING_RUNTIME_MODE")' not in source:
    raise SystemExit("darlingserver does not isolate the mldr special boundary")
for token in (
    "if (argc != 11)",
    "InheritedRuntimePrefix inherited(",
    'parseInheritedFD(argv[1], "prefix")',
    'parseInheritedFD(argv[2], "prefix parent")',
    'parseInheritedFD(argv[4], "prefix workdir")',
    'parseInheritedFD(argv[5], "prefix sidecar")',
    'parseInheritedFD(argv[6], "prefix lifecycle lock")',
    "anchored.prefixProcPath()",
    "anchored.workdirProcPath()",
    "runtimePrefix.sidecarFD()",
    "makeDescriptorCloseOnExec(prefixFD",
    "makeDescriptorCloseOnExec(sidecarFD",
    "makeDescriptorCloseOnExec(lifecycleLockFD",
    "setupUserHome(prefixFD",
    "setupEunionPrefix(prefixFD)",
    "darlingPreInit(prefixFD)",
    "copyDirectoryContentsAt(LIBEXEC_PATH, prefixFD",
    "fixPermissionsRecursiveFD(prefixFD",
    'unlinkat(prefixFD, ".darlingserver.sock"',
):
    if token not in source and token not in (root / "src/server.cpp").read_text():
        raise SystemExit(f"fd-relative server lifecycle is incomplete: {token}")
if "prefix = argv[1]" in source:
    raise SystemExit("darlingserver still reopens the launcher prefix pathname")
for forbidden in (
    "setupUserHome(prefix,",
    "darlingPreInit(prefix)",
    "setupEunionPrefix(prefix)",
    "copyAndSetAttributes(fromPath, toPath",
    "fixPermissionsRecursive(prefix,",
):
    if forbidden in source:
        raise SystemExit(f"server retained prefix pathname mutation: {forbidden}")

runtime_mode = (root / "src/runtime-mode.cpp").read_text()
for token in (
    "getresuid(&realUID, &effectiveUID, &savedUID)",
    "getresgid(&realGID, &effectiveGID, &savedGID)",
    "realUID != expectedUID",
    "effectiveUID != expectedUID",
    "savedUID != expectedUID",
    "realGID != expectedGID",
    "effectiveGID != expectedGID",
    "savedGID != expectedGID",
    "anchorRuntimeModePrefix(",
    "fstat(prefixFD, &opened)",
    "fstat(parentFD, &parent)",
    "fstatat(parentFD, leaf, &named",
    "AT_SYMLINK_NOFOLLOW",
    "named.st_ino != opened.st_ino",
    "fstat(workdirFD, &workdirOpened)",
    "workdirNamed.st_ino != workdirOpened.st_ino",
    "fstat(sidecarFD, &sidecarOpened)",
    "sidecarNamed.st_ino != sidecarOpened.st_ino",
    "fstat(lifecycleLockFD, &lifecycleLockOpened)",
    "lifecycleLockNamed.st_ino != lifecycleLockOpened.st_ino",
    "flock(lifecycleLockFD, LOCK_SH | LOCK_NB)",
    "readPrefixState(",
    "kStateName",
    "schema_version=",
    "prefix_device=",
    "prefix_inode=",
    "sidecar_device=",
    "sidecar_inode=",
    "owner_uid=",
    "owner_gid=",
    "runtime prefix state uses a newer schema",
    'return "/proc/self/fd/" + std::to_string(prefixFD)',
):
    if token not in runtime_mode:
        raise SystemExit(f"rootless server credential gate is incomplete: {token}")

prefix_test = (root / "tests/runtime_mode_test.cpp").read_text()
for token in (
    "!std::is_copy_constructible_v<InheritedRuntimePrefix>",
    "std::is_nothrow_move_constructible_v<InheritedRuntimePrefix>",
    "!std::is_copy_constructible_v<RuntimePrefixCapability>",
    "std::is_nothrow_move_constructible_v<RuntimePrefixCapability>",
    "rename-swap runtime prefix symlink was accepted",
    "rename-swap rejection mutated its target",
    "rename-swap redirected retained prefix alias",
    "rename-swap runtime workdir symlink was accepted",
    "runtime marker symlink was accepted",
    "retained prefix alias does not name exact descriptor",
):
    if token not in prefix_test:
        raise SystemExit(f"server prefix fail-closed fixture is missing: {token}")

runtime_header = (root / "include/darlingserver/runtime-mode.hpp").read_text()
for token in (
    "class InheritedRuntimePrefix final",
    "class RuntimePrefixCapability final",
    "InheritedRuntimePrefix(const InheritedRuntimePrefix&) = delete",
    "RuntimePrefixCapability(const RuntimePrefixCapability&) = delete",
):
    if token not in runtime_header:
        raise SystemExit(f"server prefix capability is not move-only: {token}")
if "validateRuntimeModePrefixFD(" in runtime_header:
    raise SystemExit("raw inherited descriptors cross the anchoring boundary")

server = (root / "src/server.cpp").read_text()
logging = (root / "src/logging.cpp").read_text()
header = (root / "internal-include/darlingserver/server.hpp").read_text()
call = (root / "src/call.cpp").read_text()
vchroot_reply = call[call.index("void DarlingServer::Call::VchrootDirectory::processCall()"):
    call.index("void DarlingServer::Call::TaskSelfTrap::processCall()")]
for token in (
    "F_DUPFD_CLOEXEC",
    "process->_vchrootDescriptor->fd()",
    "code = -errno",
):
    if token not in vchroot_reply:
        raise SystemExit(
            f"retained vchroot reply does not transfer duplicate ownership: {token}"
        )
if "directoryFD = process->_vchrootDescriptor->fd();" in vchroot_reply:
    raise SystemExit("retained vchroot reply lends and closes the process-owned fd")
for token in (
    "int _prefixFD;",
    "int prefixFD() const;",
):
    if token not in header:
        raise SystemExit(f"server does not retain the trusted prefix fd: {token}")
for token in (
    'unlinkat(prefixFD, ".darlingserver.sock", 0)',
    'unlinkat(_prefixFD, ".darlingserver.sock", 0)',
):
    if token not in server:
        raise SystemExit(f"server socket lifecycle is not fd-relative: {token}")
if "unlink(_socketPath.c_str())" in server:
    raise SystemExit("server socket cleanup still reopens a pathname")
for token in (
    "openLogDirectoryAt(Server::sharedInstance().prefixFD())",
    "fstatat(current, component, &status, AT_SYMLINK_NOFOLLOW)",
    "openat(\n\t\t\tcurrent, component,",
    "O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW",
    'openat(\n\t\t\tdirectory, "dserver.log"',
):
    if token not in logging:
        raise SystemExit(f"server logging lifecycle is not fd-relative: {token}")
if "std::filesystem" in logging:
    raise SystemExit("server logging reintroduced pathname-based prefix mutation")

cmake = (root / "CMakeLists.txt").read_text()
if "DARLING_RUNTIME_EUNION_CAPABLE=1" in cmake:
    raise SystemExit("darlingserver unconditionally advertises E-UNION capability")
if "DARLING_RUNTIME_EUNION_CAPABLE=$<BOOL:${DARLING_EUNION}>" not in cmake:
    raise SystemExit("darlingserver capability is not bound to DARLING_EUNION")
if (
    'option(DARLING_EUNION' not in cmake
    or '"Enable E-UNION union-in-vchroot prefix assembly (experimental)"\n\tOFF)'
    not in cmake
):
    raise SystemExit("darlingserver E-UNION capability is not default-off")
PY

printf 'PASS rootless-runtime-server\n'

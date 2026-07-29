#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
darling_root="${DARLING_SRC_ROOT:-$workspace_root/../darling}"
startup="$darling_root/src/startup"
work="$(mktemp -d /tmp/darling-runtime-mode-contract.XXXXXX)"

cleanup() {
	rm -rf -- "$work"
}
trap cleanup EXIT

for source in \
	"$startup/runtime_mode.c" \
	"$startup/runtime_mode.h" \
	"$startup/runtime_credentials.c" \
	"$startup/runtime_credentials.h" \
	"$startup/runtime_mode_prefix.c" \
	"$startup/runtime_mode_prefix.h" \
	"$startup/tests/runtime_mode_test.c"
do
	test -f "$source"
done

cc -std=gnu11 -Wall -Wextra -Werror \
	-I"$startup" \
	"$startup/runtime_credentials.c" \
	"$startup/runtime_mode.c" \
	"$startup/runtime_mode_prefix.c" \
	"$startup/tests/runtime_mode_test.c" \
	-o "$work/runtime-mode-test"

test "$("$work/runtime-mode-test")" = "DARLING_RUNTIME_MODE_CONTRACT_OK"

python3 -B - "$darling_root" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
launcher = (root / "src/startup/darling.c").read_text()


def function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise SystemExit(f"unterminated function: {signature}")


parser = launcher.index("darling_runtime_mode_parse_cli(")
selection = launcher.index("darling_runtime_mode_select_process(")
privilege = launcher.index("if (!rootless && geteuid() != 0)")
credential_drop = launcher.index("darling_runtime_drop_rootless_credentials(")
publish = launcher.index("darling_runtime_mode_publish(")
prefix = launcher.index("prefix = getenv(\"DPREFIX\")")
inspect = launcher.index("darling_runtime_mode_open_prefix(")
setup = launcher.index("setupPrefix();")
dispatch = launcher.index("const int commandIndex = cli.command_index;")
if not parser < selection < privilege < credential_drop < publish < prefix < inspect < setup < dispatch:
    raise SystemExit(
        "single CLI parse/mode/credential/prefix/dispatch order changed"
    )
if launcher.count("darling_runtime_mode_parse_cli(") != 1:
    raise SystemExit("launcher parses its CLI more than once")
if "getopt_long" in launcher or "<getopt.h>" in launcher or "optind" in launcher:
    raise SystemExit("launcher retained the abbreviation-permitting second parser")
if "darling_runtime_mode_select_process(\n\t\t\t&cli," not in launcher:
    raise SystemExit("typed mode selection does not consume the parsed CLI")
if (
    "prefixDirectoryIsEmpty" in launcher
    or "checkPrefixDir" in launcher
):
    raise SystemExit("launcher retained the mutation-before-real-prefix preflight")
if 'getenv("DARLING_ROOTLESS")' in launcher:
    raise SystemExit("launcher re-decides rootless mode after normalization")
if "darling_runtime_mode_validate_prefix_marker(" not in launcher:
    raise SystemExit("existing prefix is not bound to the typed mode")
setup_start = launcher.index("void setupPrefix()")
setup_end = launcher.index("\npid_t getInitProcess()", setup_start)
setup_body = launcher[setup_start:setup_end]
if "darling_runtime_mode_setup_prefix(" not in setup_body:
    raise SystemExit("production setupPrefix does not use retained-fd setup")
for forbidden in ("createDir(prefix)", "fopen(", "mkdir(", "stat("):
    if forbidden in setup_body:
        raise SystemExit(
            f"production setupPrefix retained path-based mutation: {forbidden}"
        )
for function in ("putInitPid", "setupPrefix"):
    body = function_body(launcher, f"void {function}")
    if body.count("if (!rootlessModeEnabled())") < 2:
        raise SystemExit(
            f"{function} can regain root credentials in rootless mode"
        )

runtime_mode = (root / "src/startup/runtime_mode.c").read_text()
for legacy in ("DARLING_ROOTLESS", "DARLING_NOOVERLAYFS", "DARLING_EUNION"):
    if legacy not in runtime_mode:
        raise SystemExit(f"launcher compatibility input missing: {legacy}")
if "long options must be exact" not in runtime_mode:
    raise SystemExit("exact long-option policy is not enforced")

credentials = (root / "src/startup/runtime_credentials.c").read_text()
gid_drop = credentials.index("setresgid(gid, gid, gid)")
uid_drop = credentials.index("setresuid(uid, uid, uid)")
verify = credentials.index("darling_runtime_verify_rootless_credentials(", uid_drop)
if not gid_drop < uid_drop < verify:
    raise SystemExit("permanent rootless credential drop/verification order changed")
for token in (
    "real_uid != uid",
    "effective_uid != uid",
    "saved_uid != uid",
    "real_gid != gid",
    "effective_gid != gid",
    "saved_gid != gid",
):
    if token not in credentials:
        raise SystemExit(f"rootless credential verification is incomplete: {token}")

prefix_mode = (root / "src/startup/runtime_mode_prefix.c").read_text()
for token in (
    "darling_runtime_mode_open_prefix(",
    "fstatat(current, components[index], &before",
    "AT_SYMLINK_NOFOLLOW",
    "openat(current, components[index]",
    "O_NOFOLLOW",
    "opened.st_ino != before->st_ino",
    "handle->parent_fd = current",
    "materialize_prefix(",
    "fstatat(handle->parent_fd, handle->leaf, &named",
    "named.st_ino != opened.st_ino",
    "create_and_open_directory(handle->parent_fd, handle->leaf",
    "renameat2(parent_fd, temporary, parent_fd, name",
    "RENAME_NOREPLACE",
    "open_relative_directory(handle->directory_fd",
    "created_fd = create_and_open_directory(",
    "openat(handle->directory_fd, DARLING_RUNTIME_MODE_MARKER_NAME",
    "darling_runtime_mode_prepare_workdir(",
    "darling_runtime_mode_verify_prefix_name(",
    "darling_runtime_mode_write_relative_atomic(",
    "darling_runtime_mode_make_fd_inheritable(",
):
    if token not in prefix_mode:
        raise SystemExit(f"real-prefix preflight is incomplete: {token}")
prefix_test = (root / "src/startup/tests/runtime_mode_test.c").read_text()
for token in (
    "intermediate prefix symlink was accepted before mutation",
    "production prefix setup followed a post-inspection replacement",
    "replacement target received the first setup mutation",
    "renamed inspected directory was mutated after path replacement",
    "missing prefix race was accepted by setup",
    "missing-prefix race target received a setup mutation",
    "runtime mode marker symlink was accepted",
    "rejected prefix path mutated its target",
    "fd-relative workdir preparation failed",
    "fd-relative state publication failed",
    "fd-relative state cleanup failed",
    "fd-relative open followed an intermediate symlink",
    "prefix lifecycle left a temporary directory or file",
):
    if token not in prefix_test:
        raise SystemExit(f"prefix fail-closed fixture is missing: {token}")

handoff = launcher[launcher.index("pid_t spawnInitProcess(void)") :]
for token in (
    "g_runtimePrefix.directory_fd",
    "g_runtimePrefix.parent_fd",
    "g_runtimePrefix.workdir_fd",
    "g_runtimePrefix.leaf",
    "darling_runtime_mode_make_fd_inheritable(",
):
    if token not in handoff:
        raise SystemExit(f"launcher/server retained-fd handoff is incomplete: {token}")
if 'execl(INSTALL_PREFIX "/bin/darlingserver", "darlingserver",\n\t\t\tprefix,' in handoff:
    raise SystemExit("launcher still passes the original prefix path to darlingserver")
for function in (
    "removeRuntimeStateFiles",
    "connectToShellspawn",
    "putInitPid",
    "getInitProcess",
):
    body = function_body(
        launcher,
        ("static void " if function == "removeRuntimeStateFiles" else
         "int " if function == "connectToShellspawn" else
         "void " if function == "putInitPid" else "pid_t ") + function,
    )
    if function != "connectToShellspawn" and "prefix" in body:
        # Prefix wording in diagnostics is fine; pathname construction is not.
        for forbidden in (
            'strcat(prefix',
            'snprintf(path',
            'fopen(prefix',
            'unlink(prefix',
            'stat(prefix',
        ):
            if forbidden in body:
                raise SystemExit(
                    f"{function} retained prefix pathname mutation: {forbidden}"
                )

top_cmake = (root / "CMakeLists.txt").read_text()
if (
    'option(DARLING_EUNION' not in top_cmake
    or '"Enable E-UNION union-in-vchroot prefix assembly (experimental)"\n\tOFF)'
    not in top_cmake
):
    raise SystemExit("top-level E-UNION capability is not explicit and default-off")
cmake_paths = (
    "src/startup/CMakeLists.txt",
    "src/startup/mldr/CMakeLists.txt",
    "src/launchd/src/CMakeLists.txt",
    "src/shellspawn/CMakeLists.txt",
)
for path in cmake_paths:
    source = (root / path).read_text()
    if "DARLING_RUNTIME_EUNION_CAPABLE=1" in source:
        raise SystemExit(f"{path} unconditionally advertises E-UNION capability")
    if "DARLING_RUNTIME_EUNION_CAPABLE=$<BOOL:${DARLING_EUNION}>" not in source:
        raise SystemExit(f"{path} is not bound to the real E-UNION build option")

mldr = (root / "src/startup/mldr/mldr.c").read_text()
stack = (root / "src/startup/mldr/stack.c").read_text()
launchd = (root / "src/launchd/src/launchd.c").read_text()
launchd_runtime = (root / "src/launchd/src/runtime.c").read_text()
shellspawn = (root / "src/shellspawn/shellspawn.c").read_text()
if '__mldr_runtime_mode' not in mldr or 'DARLING_RUNTIME_MODE_ENV' not in stack:
    raise SystemExit("mldr typed-mode bootstrap propagation is incomplete")
if (
    "memcpy(runtime_mode_env_user, runtime_mode_env, runtime_mode_env_size);"
    not in stack
    or "__put_user((user_long_t) runtime_mode_env_user, envp++)" not in stack
    or "__put_user((user_long_t) runtime_mode_env, envp++)" in stack
):
    raise SystemExit(
        "mldr typed mode is not copied into guest stack memory before publication"
    )
if "__mldr_rootless_pid1" not in mldr:
    raise SystemExit("mldr does not explicitly reject the obsolete marker")
for name, source in (
    ("launchd", launchd + launchd_runtime),
    ("shellspawn", shellspawn),
):
    if "darling_runtime_mode_require_canonical_process(" not in source:
        raise SystemExit(f"{name} does not require canonical runtime mode")
    if 'getenv("DARLING_ROOTLESS")' in source:
        raise SystemExit(f"{name} re-decides rootless mode")

constructor_mode = launchd_runtime.index("launchd_runtime_mode_initialize();")
constructor_pid1 = launchd_runtime.index("if (getpid() == 1 || darling_rootless)")
if constructor_mode >= constructor_pid1:
    raise SystemExit("launchd constructor selects PID-1 semantics before typed mode")
if "launchd_runtime_mode_preflight(&runtime_mode_error)" not in launchd:
    raise SystemExit("launchd main does not consume cached constructor preflight")
if "darling_runtime_mode_require_canonical_process(" in launchd:
    raise SystemExit("launchd main repeats constructor mode parsing")
PY

printf 'PASS rootless-opt-in-cli\n'

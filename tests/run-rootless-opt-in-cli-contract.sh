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
	-DDARLING_RUNTIME_PREFIX_LIFECYCLE_TESTING=1 \
	-I"$startup" \
	"$startup/runtime_credentials.c" \
	"$startup/runtime_mode.c" \
	"$startup/runtime_mode_prefix.c" \
	"$startup/tests/runtime_mode_test.c" \
	-o "$work/runtime-mode-test"

test "$("$work/runtime-mode-test")" = "DARLING_RUNTIME_MODE_CONTRACT_OK"
for focused_case in lock-race durability capability interruptions
do
	DARLING_RUNTIME_PREFIX_TEST_CASE="$focused_case" \
		"$work/runtime-mode-test"
done

cat >"$work/capability-transfer-good.c" <<'EOF'
#include "runtime_mode_prefix.h"

int main(void)
{
	darling_runtime_prefix source = DARLING_RUNTIME_PREFIX_INITIALIZER;
	darling_runtime_prefix destination = DARLING_RUNTIME_PREFIX_INITIALIZER;
	char error[64] = {0};
	return darling_runtime_prefix_move(destination, source,
		error, sizeof(error));
}
EOF
cc -std=gnu11 -Wall -Wextra -Werror -c -I"$startup" \
	"$work/capability-transfer-good.c" -o "$work/capability-transfer-good.o"

cat >"$work/capability-direct-copy-bad.c" <<'EOF'
#include "runtime_mode_prefix.h"

int main(void)
{
	darling_runtime_prefix source = DARLING_RUNTIME_PREFIX_INITIALIZER;
	darling_runtime_prefix destination = source;
	(void)destination;
	return 0;
}
EOF
if cc -std=gnu11 -Wall -Wextra -Werror -c -I"$startup" \
	"$work/capability-direct-copy-bad.c" -o "$work/capability-direct-copy-bad.o" \
	>"$work/capability-direct-copy-bad.stdout" \
	2>"$work/capability-direct-copy-bad.stderr"
then
	printf 'assignment-resistant prefix capability accepted direct copy-initialization\n' >&2
	exit 1
fi

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
prefix = launcher.index("requested_prefix = getenv(\"DPREFIX\")")
inspect = launcher.index("darling_runtime_mode_open_prefix(")
lifecycle = launcher.index("darling_runtime_prefix_prepare(")
dispatch = launcher.index("const int commandIndex = cli.command_index;")
if not parser < selection < privilege < credential_drop < publish < prefix < inspect < lifecycle < dispatch:
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
if launcher.count("darling_runtime_prefix_prepare(") != 1:
    raise SystemExit("launcher does not enter the typed prefix lifecycle once")
after_anchor = launcher[inspect:dispatch]
if (
    "requested_prefix = NULL;" not in after_anchor
    or "setupPrefix();" in after_anchor
    or "darling_runtime_mode_validate_prefix_marker(" in after_anchor
):
    raise SystemExit("launcher retained a raw-path or legacy marker branch after anchoring")
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
    "DARLING_RUNTIME_PREFIX_STATE_NAME",
    "darling_runtime_prefix_prepare(",
    "darling_runtime_prefix_recreate(",
    "darling_runtime_prefix_delete(",
    "darling_runtime_prefix_move(",
    "LIFECYCLE_STABLE_CURRENT_V2",
    "LIFECYCLE_PHASE_REPLACEMENT_STAGED",
    "recovery_disposition(",
    "advance_transaction_phase(",
    "flock(fd, LOCK_EX)",
    "fstatat(handle->parent_fd, names->lock, &named",
    "named.st_dev != locked.st_dev",
    "named.st_ino != locked.st_ino",
    "fsync_directory(",
    "cannot persist prefix initialization file",
    "staged prefix root",
    "darling_runtime_mode_prepare_workdir(",
    "darling_runtime_mode_verify_prefix_name(",
    "darling_runtime_mode_write_relative_atomic(",
    "darling_runtime_mode_make_fd_inheritable(",
):
    if token not in prefix_mode:
        raise SystemExit(f"real-prefix preflight is incomplete: {token}")
if "names.lock" in prefix_mode:
    raise SystemExit("delete path still unlinks the persistent lifecycle lock")
prefix_header = (root / "src/startup/runtime_mode_prefix.h").read_text()
for token in (
    "} darling_runtime_prefix[1];",
    "Transfer ownership only with",
    "darling_runtime_prefix_move()",
):
    if token not in prefix_header:
        raise SystemExit(f"assignment-resistant C capability contract is missing: {token}")
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
    "move did not invalidate source capability",
    "stable-state/journal-phase recovery matrix mismatch",
    "recovery matrix did not cover every combination",
    "interrupted upgrade did not reach a valid stable state",
    "newer prefix schema was accepted",
    "cross-prefix typed state was accepted",
    "hostile state metadata mode was accepted",
    "multiply linked state metadata was accepted",
    "state metadata symlink was accepted",
    "truncated state metadata was accepted",
    "third process acquired a replacement lifecycle lock concurrently",
    "delete unlinked the persistent lifecycle lock while held",
    "staged tree did not fsync files and directories bottom-up",
    "staged root was not fsynced after nested directories",
    "real create/recreate/delete interruption matrix was incomplete",
    "open_prefix accepted an already-owned capability",
):
    if token not in prefix_test:
        raise SystemExit(f"prefix fail-closed fixture is missing: {token}")

handoff = launcher[launcher.index("pid_t spawnInitProcess(void)") :]
for token in (
    "g_runtimePrefix->directory_fd",
    "g_runtimePrefix->parent_fd",
    "g_runtimePrefix->workdir_fd",
    "g_runtimePrefix->leaf",
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

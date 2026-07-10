import sys
import os
import io
import signal
import shutil
import subprocess
import tempfile
import time
import types
from contextlib import contextmanager
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

import west_commands.test as west_test_module
from west_commands.test import DarlingTest
from west_commands.test_runtime import (
    describe_runtime_deploy_plan,
    runtime_build_targets,
    runtime_deploy_targets,
)


def make_test():
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(Path.cwd())
    test.inf_messages = []
    test.err_messages = []
    test.inf = lambda message: test.inf_messages.append(message)
    test.wrn = lambda message: None
    test.err = lambda message: test.err_messages.append(message)
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._resolve_darling_launcher = lambda _prefix: None
    test._kill_dserver_for_prefix = lambda _prefix: None
    test._prefix_process_snapshot = lambda _prefix: []
    test._missing_requirements = lambda _invocation: []
    test._execution_env = lambda _invocation: {"DPREFIX": test._prefix}
    return test


with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    workspace = tempdir / "workspace"
    source_module = tempdir / "source-module"
    workspace.mkdir()
    (source_module / "include").mkdir(parents=True)
    (source_module / "include" / "module_contract.h").write_text(
        "#pragma once\n#define WEST_SOURCE_ROOT_MODULE_VALUE 42\n"
    )
    script = workspace / "fixture.c"
    script.write_text(
        "#include <module_contract.h>\n"
        "int main(void) { return WEST_SOURCE_ROOT_MODULE_VALUE == 42 ? 0 : 1; }\n"
    )

    source_root_test = make_test()
    source_root_test._project_path = lambda ref: {
        "workspace": workspace,
        "source-module": source_module,
    }[ref]
    assert source_root_test._run_c_fixture(
        {
            "name": "source_root_module_contract",
            "diag": "bare",
            "cwd": workspace,
            "script_path": script,
            "cc": os.environ.get("CC", "cc"),
            "compile_flags": ["-std=gnu11", "-Wall", "-Wextra", "-Werror"],
            "fixture_include_dirs": [],
            "include_dirs": ["include"],
            "stub_headers": [],
            "generated_headers": {},
            "source_files": [],
            "source_root_module": "source-module",
        }
    ) == 0


proof_plan = {
    "bad-profile": "current-minus-patch",
    "source-modules": ["darling/src/external/darlingserver"],
    "runtime-artifacts": [
        {
            "module": "darling/src/external/xnu",
            "build-targets": ["system_kernel", "system_kernel"],
            "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
        },
        {
            "module": "darling/src/external/darlingserver",
            "build-targets": ["darlingserver"],
            "deploy": ["bin/darlingserver"],
        },
    ],
}
assert runtime_build_targets(proof_plan) == ["system_kernel", "darlingserver"]
assert describe_runtime_deploy_plan(proof_plan) == (
    "guest-runtime-deploy [current-minus-patch] "
    "sources:darling/src/external/darlingserver: "
    "darling/src/external/xnu[build:system_kernel,system_kernel; "
    "deploy:usr/lib/system/libsystem_kernel.dylib]; "
    "darling/src/external/darlingserver[build:darlingserver; deploy:bin/darlingserver]"
)
prefix_for_targets = Path("/tmp/prefix")
assert runtime_deploy_targets(prefix_for_targets, "usr/lib/system/libsystem_kernel.dylib") == [
    prefix_for_targets / "libexec/darling/usr/lib/system/libsystem_kernel.dylib",
    prefix_for_targets / "usr/lib/system/libsystem_kernel.dylib",
]
assert runtime_deploy_targets(prefix_for_targets, "bin/darlingserver") == [
    prefix_for_targets / "bin/darlingserver",
]
deploy_test = make_test()
deploy_test._resolve_darling_launcher = (
    lambda _prefix: "/opt/darling-test/bin/darling"
)
assert deploy_test._runtime_red_deploy_targets(
    prefix_for_targets,
    "bin/darlingserver",
) == [
    prefix_for_targets / "bin/darlingserver",
    Path("/opt/darling-test/bin/darlingserver"),
]
assert deploy_test._runtime_red_deploy_targets(
    prefix_for_targets,
    "usr/libexec/darling/mldr",
) == [
    prefix_for_targets / "libexec/darling/usr/libexec/darling/mldr",
    prefix_for_targets / "usr/libexec/darling/mldr",
    Path("/opt/darling-test/usr/libexec/darling/mldr"),
    Path("/opt/darling-test/libexec/darling/usr/libexec/darling/mldr"),
]
assert deploy_test._runtime_red_deploy_targets(
    prefix_for_targets,
    "usr/lib/dyld",
) == [
    prefix_for_targets / "libexec/darling/usr/lib/dyld",
    prefix_for_targets / "usr/lib/dyld",
    Path("/opt/darling-test/usr/lib/dyld"),
    Path("/opt/darling-test/libexec/darling/usr/lib/dyld"),
]
assert deploy_test._runtime_red_deploy_targets(
    prefix_for_targets,
    "usr/lib/system/libsystem_kernel.dylib",
) == [
    prefix_for_targets / "libexec/darling/usr/lib/system/libsystem_kernel.dylib",
    prefix_for_targets / "usr/lib/system/libsystem_kernel.dylib",
    Path("/opt/darling-test/usr/lib/system/libsystem_kernel.dylib"),
    Path("/opt/darling-test/libexec/darling/usr/lib/system/libsystem_kernel.dylib"),
]
try:
    runtime_deploy_targets(prefix_for_targets, "/absolute/bad")
except ValueError as exc:
    assert "must be relative" in str(exc)
else:
    raise AssertionError("absolute deploy path was accepted")

with tempfile.TemporaryDirectory() as temp:
    bundle_root = Path(temp)
    bundle = bundle_root / "20260709T000000Z-west-test-runtime_red_reason"
    bundle.mkdir()
    (bundle / "stdout.log").write_text("old runtime failure\nerrno=14\n")
    (bundle / "stderr.log").write_text("WEST_GUEST_STAGE=run\n")
    (bundle / "exit-status.txt").write_text("exit status: 1\n")

    test = make_test()
    test._bundle_root = str(bundle_root)
    invocation = {"name": "runtime_red_reason"}
    assert test._check_guest_runtime_red_failure(
        {"expect-output-contains": ["old runtime failure", "errno=14"]},
        invocation,
        since=time.time() - 10,
    )
    assert not test._check_guest_runtime_red_failure(
        {"expect-output-contains": ["different failure"]},
        invocation,
        since=time.time() - 10,
    )

test = make_test()
test._bundle_root = "/tmp/west-test-contract-no-bundles"
invocation = {"name": "runtime_red_reason_without_bundle"}
assert test._check_guest_runtime_red_failure(
    {"expect-output-contains": ["captured old runtime symptom"]},
    invocation,
    since=time.time() - 10,
    captured_output="runner stderr\ncaptured old runtime symptom\n",
)
assert not test._check_guest_runtime_red_failure(
    {"expect-output-contains": ["different failure"]},
    invocation,
    since=time.time() - 10,
    captured_output="runner stderr\ncaptured old runtime symptom\n",
)

test = make_test()
assert not test._guest_runtime_red_has_positive_reason({})
assert not test._guest_runtime_red_has_positive_reason({"expect-output-contains": []})
assert not test._guest_runtime_red_has_positive_reason({"expect-output-contains": [""]})
assert test._guest_runtime_red_has_positive_reason({"expect-output-contains": "old runtime symptom"})
assert test._guest_runtime_red_has_positive_reason({"expect-output-contains": ["old runtime symptom"]})
assert test._patch_subject_from_text(
    "From abc Mon Sep 17 00:00:00 2001\n"
    "Subject: [PATCH] thread/call/server: contain exceptions that can terminate the\n"
    " server\n"
    "\n"
    "body\n"
) == "thread/call/server: contain exceptions that can terminate the server"
missing_reasons = test._runtime_red_reason_audit(
    [
        (
            {"path": "xnu/missing.patch"},
            {
                "name": "missing_reason",
                "red-proof": {"mode": "guest-runtime-deploy"},
            },
        ),
        (
            {"path": "xnu/with_reason.patch"},
            {
                "name": "with_reason",
                "red-proof": {
                    "mode": "guest-runtime-deploy",
                    "expect-output-contains": ["old runtime symptom"],
                },
            },
        ),
        (
            {"path": "xnu/source_base.patch"},
            {
                "name": "source_base",
                "red-proof": {"mode": "source-base"},
            },
        ),
    ]
)
assert missing_reasons == [
    "xnu/missing.patch: missing_reason "
    "guest-runtime-deploy RED proof needs expect-output-contains"
], missing_reasons

test = make_test()
test._prefix = "/tmp/prefix"
try:
    test._run_guest_runtime_deploy_proof(
        {"path": "xnu/missing.patch"},
        {"mode": "guest-runtime-deploy", "runtime-artifacts": []},
        {"name": "missing_reason", "guest_c_fixture": True},
    )
except SystemExit as exc:
    assert "guest-runtime-deploy RED proof needs expect-output-contains" in str(exc)
else:
    raise AssertionError("guest-runtime-deploy proof accepted without reason matcher")


with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    build_dir = tempdir / "build"
    build_dir.mkdir()
    (build_dir / "CMakeCache.txt").write_text(
        "\n".join(
            [
                "CMAKE_GENERATOR:INTERNAL=Ninja",
                "CMAKE_BUILD_TYPE:STRING=Debug",
                "CMAKE_C_COMPILER:FILEPATH=/usr/bin/clang",
                "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++",
                "DARLING_EUNION:BOOL=ON",
                "DARLING_RING_TRANSPORT:BOOL=ON",
                "DARLING_RPC_SLEEP_ACCOUNT:BOOL=OFF",
                "DARLING_GUEST_RECVSPIN:STRING=512",
                "DSERVER_RING_TRANSPORT:BOOL=ON",
            ]
        )
        + "\n"
    )
    old_build_dir = os.environ.get("DARLING_BUILD_DIR")
    os.environ["DARLING_BUILD_DIR"] = str(build_dir)
    try:
        args = make_test()._runtime_red_configure_args(
            {"runtime-artifacts": [{"deploy": ["usr/lib/system/libsystem_kernel.dylib"]}]},
            tempdir / "prefix",
        )
        ring_args = make_test()._runtime_red_configure_args(
            {
                "inherit-cmake-cache": [
                    "DARLING_RING_TRANSPORT",
                    "DSERVER_RING_TRANSPORT",
                ],
                "runtime-artifacts": [{"deploy": ["usr/lib/system/libsystem_kernel.dylib"]}],
            },
            tempdir / "prefix",
        )
        host_test = make_test()
        host_test._resolve_darling_launcher = (
            lambda _prefix: str(tempdir / "install/bin/darling")
        )
        host_args = host_test._runtime_red_configure_args(
            {"runtime-artifacts": [{"deploy": ["bin/darlingserver"]}]},
            tempdir / "prefix",
        )
        tool_args = make_test()._runtime_red_configure_args(
            {
                "cmake-defines": {
                    "DSERVER_TOOLS": True,
                    "WEST_EMPTY_DEFINE": None,
                    "WEST_STRING_DEFINE": "value",
                },
                "runtime-artifacts": [{"deploy": ["bin/dserverdbg"]}],
            },
            tempdir / "prefix",
        )
    finally:
        if old_build_dir is None:
            os.environ.pop("DARLING_BUILD_DIR", None)
        else:
            os.environ["DARLING_BUILD_DIR"] = old_build_dir
    assert "-DDARLING_EUNION=ON" in args, args
    assert "-DDARLING_RING_TRANSPORT=OFF" in args, args
    assert "-DDARLING_RPC_SLEEP_ACCOUNT=OFF" in args, args
    assert "-DDARLING_GUEST_RECVSPIN=512" in args, args
    assert "-DDSERVER_RING_TRANSPORT=OFF" in args, args
    assert "-DDARLING_RING_TRANSPORT=ON" in ring_args, ring_args
    assert "-DDSERVER_RING_TRANSPORT=ON" in ring_args, ring_args
    assert f"-DCMAKE_INSTALL_PREFIX={tempdir / 'prefix'}" in args, args
    assert f"-DCMAKE_INSTALL_PREFIX={tempdir / 'install'}" in host_args, host_args
    assert "-DDSERVER_TOOLS=ON" in tool_args, tool_args
    assert "-DWEST_EMPTY_DEFINE=" in tool_args, tool_args
    assert "-DWEST_STRING_DEFINE=value" in tool_args, tool_args

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    bad_bundle = tempdir / "bad-bundle"
    good_bundle = tempdir / "good-bundle"
    bad_bundle.mkdir()
    good_bundle.mkdir()
    (bad_bundle / "stdout.log").write_text("WEST_GUEST_SHELL_RC=1\n")
    (good_bundle / "stdout.log").write_text("GREEN_OK\nORACLE_RC=0\n")
    test = make_test()
    test._latest_debug_bundle = lambda _invocation, *, since: bad_bundle
    assert not test._check_guest_runtime_green_success(
        {"name": "green_reason_contract", "ok_marker": "GREEN_OK"},
        since=time.time(),
    )
    assert "GREEN output missing 'GREEN_OK'" in test.err_messages[-1]
    assert test._check_guest_runtime_green_success(
        {
            "name": "green_reason_contract",
            "ok_marker": "GREEN_OK",
            "host_trace_oracle": True,
        },
        since=time.time(),
    )
    test._latest_debug_bundle = lambda _invocation, *, since: good_bundle
    assert test._check_guest_runtime_green_success(
        {"name": "green_reason_contract", "ok_marker": "GREEN_OK"},
        since=time.time(),
    )

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    test = make_test()
    test._prefix = str(prefix)
    test._prefix_env = {"DARLING_NOOVERLAYFS": "1"}
    test._resolve_darling_launcher = lambda _prefix: "/tmp/fake-darling"
    invocation = {"requires_resources": ["darling-prefix"]}
    env = DarlingTest._execution_env(test, invocation)
    assert env["DPREFIX"] == str(prefix), env
    assert env["DARLING_PREFIX"] == str(prefix), env
    assert env["DARLING_NOOVERLAYFS"] == "1", env
    assert env["DARLING"] == "/tmp/fake-darling", env
    assert env["DARLING_LAUNCHER"] == "/tmp/fake-darling", env

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    test = make_test()
    test.topdir = str(tempdir)
    source_root = tempdir / "source"
    source_root.mkdir()
    calls = []
    old_run = west_test_module.subprocess.run

    def quiet_success_run(args, **kwargs):
        calls.append((list(args), kwargs.get("capture_output"), kwargs.get("text")))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="\n".join(f"noisy stdout {index}" for index in range(300)),
            stderr="\n".join(f"noisy stderr {index}" for index in range(300)),
        )

    west_test_module.subprocess.run = quiet_success_run
    stderr = io.StringIO()
    try:
        with redirect_stderr(stderr):
            build_root = test._runtime_red_build_artifacts(
                source_root,
                {
                    "runtime-artifacts": [
                        {"build-targets": ["target-a"], "deploy": ["bin/a"]},
                        {"build-targets": ["target-a", "target-b"], "deploy": ["bin/b"]},
                    ]
                },
                tempdir / "prefix",
                tempdir / "scratch",
                label="GREEN",
            )
    finally:
        west_test_module.subprocess.run = old_run
    assert build_root == tempdir / "scratch/build"
    assert stderr.getvalue() == "", stderr.getvalue()
    assert len(calls) == 2, calls
    assert calls[0][1:] == (True, True), calls
    assert calls[1][0][-2:] == ["target-a", "target-b"], calls

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    test = make_test()
    result = subprocess.CompletedProcess(
        ["fake"],
        7,
        stdout="\n".join(f"old stdout {index}" for index in range(150)),
        stderr="\n".join(f"tail stderr {index}" for index in range(120)),
    )
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        test._dump_command_tail("RED build", result)
    dumped = stderr.getvalue()
    assert "old stdout 0" not in dumped, dumped
    assert "old stdout 69" not in dumped, dumped
    assert "old stdout 70" in dumped, dumped
    assert "tail stderr 119" in dumped, dumped
    assert test.err_messages == ["RED build failed with rc 7"], test.err_messages

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    init_pid = tempdir / ".init.pid"
    init_pid.write_text("12345\n")
    old_checker = west_test_module.darling_init_pid_is_usable
    west_test_module.darling_init_pid_is_usable = lambda pid: pid != 12345
    try:
        make_test()._remove_stale_init_pid(tempdir)
    finally:
        west_test_module.darling_init_pid_is_usable = old_checker
    assert not init_pid.exists()

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    root_copy = prefix / "usr/lib/system/libsystem_kernel.dylib"
    base_copy = prefix / "libexec/darling/usr/lib/system/libsystem_kernel.dylib"
    root_copy.parent.mkdir(parents=True)
    base_copy.parent.mkdir(parents=True)
    root_copy.write_text("ORIGINAL\n")
    base_copy.write_text("ORIGINAL\n")

    test = make_test()
    test._prefix = str(prefix)
    calls = []

    @contextmanager
    def fake_source_forest(patch, proof, *, omit_patch):
        calls.append(("source", patch["module"], proof["mode"], omit_patch))
        yield tempdir / "source/darling"

    def fake_build(source_root, proof, build_prefix, scratch_root, *, label="RED"):
        calls.append(("build", source_root, build_prefix, scratch_root.exists(), label))
        output = scratch_root / "build/xnu/libsystem_kernel.dylib"
        output.parent.mkdir(parents=True)
        output.write_text(f"{label}\n")
        return scratch_root / "build"

    def fake_run(invocation, env=None):
        env = env or {}
        calls.append((
            "run",
            invocation["name"],
            root_copy.read_text(),
            base_copy.read_text(),
            env.get("RESOURCE_CONTEXT_MARKER"),
        ))
        return 77 if len([call for call in calls if call[0] == "run"]) == 1 else 0

    @contextmanager
    def fake_resource_context(invocation, env):
        merged = dict(env or {})
        merged["RESOURCE_CONTEXT_MARKER"] = invocation["name"]
        yield merged

    test._guest_runtime_source_forest = fake_source_forest
    test._runtime_red_build_artifacts = fake_build
    test._resource_context = fake_resource_context
    test._run_invocation = fake_run
    test._check_guest_runtime_red_failure = (
        lambda _proof, _invocation, *, since, captured_output=None: True
    )

    patch = {"path": "xnu/example.patch", "module": "darling/src/external/xnu"}
    proof = {
        "mode": "guest-runtime-deploy",
        "expect-output-contains": ["old runtime symptom"],
        "runtime-artifacts": [
            {
                "module": "darling/src/external/xnu",
                "build-targets": ["libsystem_kernel"],
                "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
            }
        ],
    }
    invocation = {"guest_c_fixture": True, "name": "runtime_red_contract"}

    rc = test._run_guest_runtime_deploy_proof(patch, proof, invocation)
    assert rc == 0, rc
    assert root_copy.read_text() == "ORIGINAL\n"
    assert base_copy.read_text() == "ORIGINAL\n"
    assert calls[0] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", True), calls
    assert calls[1][0] == "build" and calls[1][3] is True and calls[1][4] == "RED", calls
    assert calls[2] == (
        "run",
        "runtime_red_contract",
        "RED\n",
        "RED\n",
        "runtime_red_contract",
    ), calls
    assert calls[3] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", False), calls
    assert calls[4][0] == "build" and calls[4][3] is True and calls[4][4] == "GREEN", calls
    assert calls[5] == (
        "run",
        "runtime_red_contract",
        "GREEN\n",
        "GREEN\n",
        "runtime_red_contract",
    ), calls

    root_copy.write_text("ORIGINAL\n")
    base_copy.write_text("ORIGINAL\n")
    calls.clear()

    def fake_run_prepared(invocation, env=None):
        env = env or {}
        calls.append(
            (
                "run",
                invocation["name"],
                root_copy.read_text(),
                base_copy.read_text(),
                env.get("WEST_GUEST_C_FIXTURE_PREPARE_ONLY"),
                env.get("WEST_GUEST_C_FIXTURE_RUN_ONLY"),
                env.get("WEST_GUEST_C_FIXTURE_ID"),
                env.get("RESOURCE_CONTEXT_MARKER"),
            )
        )
        if env.get("WEST_GUEST_C_FIXTURE_PREPARE_ONLY") == "1":
            return 0
        if env.get("WEST_GUEST_C_FIXTURE_RUN_ONLY") == "1":
            return 77
        return 0

    test._run_invocation = fake_run_prepared
    prepared_proof = dict(proof)
    prepared_proof["prepare-fixture-before-deploy"] = True
    rc = test._run_guest_runtime_deploy_proof(patch, prepared_proof, invocation)
    assert rc == 0, rc
    assert root_copy.read_text() == "ORIGINAL\n"
    assert base_copy.read_text() == "ORIGINAL\n"
    assert calls[0] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", True), calls
    assert calls[1][0] == "build" and calls[1][4] == "RED", calls
    assert calls[2][0:6] == (
        "run",
        "runtime_red_contract",
        "ORIGINAL\n",
        "ORIGINAL\n",
        "1",
        None,
    ), calls
    assert calls[3][0:6] == (
        "run",
        "runtime_red_contract",
        "RED\n",
        "RED\n",
        None,
        "1",
    ), calls
    assert calls[2][6] and calls[2][6] == calls[3][6], calls
    assert calls[2][7] == "runtime_red_contract", calls
    assert calls[3][7] == "runtime_red_contract", calls
    assert calls[4] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", False), calls
    assert calls[5][0] == "build" and calls[5][4] == "GREEN", calls
    assert calls[6][0:6] == (
        "run",
        "runtime_red_contract",
        "GREEN\n",
        "GREEN\n",
        None,
        None,
    ), calls

    root_copy.write_text("ORIGINAL\n")
    base_copy.write_text("ORIGINAL\n")
    calls.clear()
    red_runner_script = tempdir / "red-oracle.sh"
    red_runner_script.write_text("#!/usr/bin/env sh\nexit 1\n")
    test._project_path = lambda repo=None: tempdir

    def fake_run_with_red_runner(invocation, env=None):
        env = env or {}
        calls.append(
            (
                "run",
                invocation["name"],
                invocation.get("runner"),
                root_copy.read_text(),
                base_copy.read_text(),
                env.get("WEST_RUNTIME_SOURCE_ROOT"),
                env.get("WEST_GUEST_C_FIXTURE_RUN_ONLY"),
                list(invocation.get("requires_resources", [])),
                env.get("RESOURCE_CONTEXT_MARKER"),
            )
        )
        return 77 if invocation["name"] == "runtime_red_contract_red" else 0

    test._run_invocation = fake_run_with_red_runner
    red_runner_proof = dict(proof)
    red_runner_proof["red-runner"] = {
        "runner": "script",
        "repo": ".",
        "script": "red-oracle.sh",
    }
    rc = test._run_guest_runtime_deploy_proof(patch, red_runner_proof, invocation)
    assert rc == 0, rc
    assert calls[0] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", True), calls
    assert calls[1][0] == "build" and calls[1][4] == "RED", calls
    assert calls[2][0:5] == (
        "run",
        "runtime_red_contract_red",
        "script",
        "RED\n",
        "RED\n",
    ), calls
    assert calls[2][5] == str(tempdir / "source/darling"), calls
    assert calls[2][6] is None, calls
    assert "darling-prefix" in calls[2][7], calls
    assert calls[2][8] == "runtime_red_contract_red", calls
    assert calls[3] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", False), calls
    assert calls[4][0] == "build" and calls[4][4] == "GREEN", calls
    assert calls[5][0:3] == ("run", "runtime_red_contract", None), calls
    assert calls[5][3:5] == ("GREEN\n", "GREEN\n"), calls

    symlink_parent = tempdir / "forest/darling/src/external/parent"
    symlink_parent.parent.mkdir(parents=True)
    symlink_parent.symlink_to(prefix, target_is_directory=True)
    nested_target = symlink_parent / "nested/project"
    assert test._has_symlink_parent(nested_target, tempdir / "forest/darling")

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    launcher = tempdir / "fake-darling"
    hold_pid = tempdir / "hold.pid"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = shutdown ]; then
\texit 0
fi
if [ "${1:-}" != shell ]; then
\texit 64
fi
if [ "${DARLING_PREFIX:-}" != "${DPREFIX:-}" ]; then
\tprintf 'bad DARLING_PREFIX=%s DPREFIX=%s\\n' "${DARLING_PREFIX:-}" "${DPREFIX:-}" >&2
\texit 65
fi
(sleep 30) &
echo "$!" > "${WEST_FAKE_HOLD_PID:?}"
printf 'FAKE_GUEST_COMMAND_DONE\\n' >&2
exit 0
"""
    )
    launcher.chmod(0o755)

    test = make_test()
    test._prefix = str(prefix)
    invocation = {
        "name": "guest_command_inherited_fd_contract",
        "cwd": tempdir,
        "guest_command_fixture": True,
        "guest_command": "/usr/bin/true",
        "guest_env_vars": {},
        "timeout_seconds": 1,
        "expect": {
            "returncode": 0,
            "output-contains": ["FAKE_GUEST_COMMAND_DONE"],
        },
    }
    env = os.environ.copy()
    env["DPREFIX"] = str(prefix)
    env["DARLING_LAUNCHER"] = str(launcher)
    env["WEST_FAKE_HOLD_PID"] = str(hold_pid)
    started = time.monotonic()
    rc = test._run_guest_command_fixture(invocation, env=env)
    elapsed = time.monotonic() - started
    try:
        if hold_pid.exists():
            os.kill(int(hold_pid.read_text()), signal.SIGKILL)
    except ProcessLookupError:
        pass
    assert rc == 0, (rc, test.err_messages)
    assert elapsed < 5, elapsed

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    launcher = tempdir / "fake-darling-any"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = shutdown ]; then
\texit 0
fi
if [ "${DARLING_PREFIX:-}" != "${DPREFIX:-}" ]; then
\tprintf 'bad DARLING_PREFIX=%s DPREFIX=%s\\n' "${DARLING_PREFIX:-}" "${DPREFIX:-}" >&2
\texit 65
fi
printf 'ANY_RETURN_MARKER\\n' >&2
exit 7
"""
    )
    launcher.chmod(0o755)

    test = make_test()
    test._prefix = str(prefix)
    invocation = {
        "name": "guest_command_any_returncode_contract",
        "cwd": tempdir,
        "guest_command_fixture": True,
        "guest_command": "/usr/bin/true",
        "guest_env_vars": {},
        "timeout_seconds": 1,
        "expect": {
            "returncode": "any",
            "output-contains": ["ANY_RETURN_MARKER"],
        },
    }
    env = os.environ.copy()
    env["DPREFIX"] = str(prefix)
    env["DARLING_LAUNCHER"] = str(launcher)
    rc = test._run_guest_command_fixture(invocation, env=env)
    assert rc == 0, (rc, test.err_messages)

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    fixture = tempdir / "fixture.c"
    fixture.write_text('int main(void) { return 0; }\\n')
    launcher = tempdir / "fake-darling"
    launcher.write_text("#!/usr/bin/env bash\nexit 0\n")
    launcher.chmod(0o755)
    test = make_test()
    test._prefix = str(prefix)
    old_run = west_test_module.subprocess.run
    inspected = []

    def inspect_guest_c_runner(args, **kwargs):
        del kwargs
        runner = Path(args[-1])
        content = runner.read_text()
        inspected.append(content)
        return subprocess.CompletedProcess(args, 0)

    west_test_module.subprocess.run = inspect_guest_c_runner
    try:
        rc = test._run_guest_c_fixture(
            {
                "name": "guest_c_namespace_diagnostic_contract",
                "key": "guest-c-namespace-diagnostic",
                "display": "guest-c-namespace-diagnostic",
                "cwd": tempdir,
                "script_path": fixture,
                "guest_cc": "/usr/bin/clang",
                "guest_cflags": "",
                "guest_prelude": "",
                "guest_env_vars": {},
                "compile_flags": [],
                "link_flags": [],
                "run_args": [],
                "ok_marker": "",
                "host_trace_files": [],
                "host_temp_files": [],
                "host_stat_deltas": [],
                "host_trace_oracle": False,
                "timeout_seconds": 1,
                "diag": "bare",
            },
            env={"DPREFIX": str(prefix), "DARLING_LAUNCHER": str(launcher)},
        )
    finally:
        west_test_module.subprocess.run = old_run
    assert rc == 0, rc
    assert inspected, "guest-c runner was not generated"
    generated = inspected[0]
    assert "dump_namespace_state()" in generated, generated
    assert "WEST_GUEST_NAMESPACE_INIT_PID" in generated, generated
    assert "WEST_GUEST_NAMESPACE_MNT" in generated, generated
    assert "Cannot open mnt namespace file" in generated, generated
    assert "clear_stale_init_pid()" in generated, generated
    assert generated.count("clear_stale_init_pid") >= 3, generated
    assert '"$launch" shutdown >/dev/null 2>&1 || true' in generated, generated
    cleanup_pos = generated.index('WEST_GUEST_STAGE=cleanup')
    upload_pos = generated.index('WEST_GUEST_STAGE=upload')
    assert cleanup_pos < upload_pos, generated
    assert '"$launch" shutdown >/dev/null 2>&1 || true' in generated[
        cleanup_pos:upload_pos
    ], generated
    assert "dump_runtime_process_state()" in generated, generated
    assert "snapshot=\"$(mktemp /tmp/west-dserver-ps.XXXXXX)\"" in generated, generated
    assert "WEST_GUEST_DSERVER_EXE_SHA256" in generated, generated
    assert "WEST_GUEST_DSERVER_ARGS" in generated, generated
    assert "dump_runtime_file_state()" in generated, generated
    assert 'dump_file_sha launcher_server "$launcher_dir/darlingserver"' in generated, generated
    assert 'dump_file_sha install_dyld "$install_root/usr/lib/dyld"' in generated, generated
    assert 'dump_file_sha install_libsystem_kernel "$install_root/usr/lib/system/libsystem_kernel.dylib"' in generated, generated
    assert 'dump_file_sha prefix_libsystem_kernel "$DPREFIX/usr/lib/system/libsystem_kernel.dylib"' in generated, generated
    assert 'dump_file_sha prefix_dyld "$DPREFIX/usr/lib/dyld"' in generated, generated
    assert "WEST_GUEST_RPC_CLIENT_LOG_BEGIN" in generated, generated
    assert ": > /tmp/dserver-client-rpc.log" in generated, generated
    assert 'DARLING_PREFIX="$DPREFIX"' in generated, generated

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    test = make_test()
    test._prefix = str(prefix)
    before = set(Path(tempfile.gettempdir()).glob("west-red-proof-runtime-*"))

    @contextmanager
    def fake_source_forest(_patch, _proof, *, omit_patch):
        assert omit_patch is True
        yield tempdir / "source/darling"

    def failing_build(_source_root, _proof, _build_prefix, scratch_root, *, label="RED"):
        assert label == "RED"
        (scratch_root / "diagnostic.txt").write_text("kept\n")
        raise RuntimeError("forced build failure")

    test._guest_runtime_source_forest = fake_source_forest
    test._runtime_red_build_artifacts = failing_build

    try:
        test._run_guest_runtime_deploy_proof(
            {"path": "xnu/failing.patch", "module": "darling/src/external/xnu"},
            {
                "mode": "guest-runtime-deploy",
                "expect-output-contains": ["old runtime symptom"],
                "runtime-artifacts": [],
            },
            {"guest_c_fixture": True, "name": "runtime_red_keep_scratch"},
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced runtime-red build failure unexpectedly passed")

    after = set(Path(tempfile.gettempdir()).glob("west-red-proof-runtime-*"))
    kept = list(after - before)
    assert len(kept) == 1, kept
    assert (kept[0] / "diagnostic.txt").read_text() == "kept\n"
    shutil.rmtree(kept[0])

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    target = tempdir / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=target, check=True)
    (target / "file.txt").write_text("base\n")
    subprocess.run(["git", "add", "file.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=target, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (target / "file.txt").write_text("base\nskipped\n")
    subprocess.run(["git", "add", "file.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "skipped patch"], cwd=target, check=True)
    skipped_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    skipped_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (target / "dependent.txt").write_text("dependent\n")
    subprocess.run(["git", "add", "dependent.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "dependent patch"], cwd=target, check=True)
    dependent_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (target / "other.txt").write_text("kept\n")
    subprocess.run(["git", "add", "other.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "kept patch"], cwd=target, check=True)
    kept_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=target, check=True)
    (target / "rerolled.txt").write_text("rerolled\n")
    subprocess.run(["git", "add", "rerolled.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "skipped patch"], cwd=target, check=True)
    rerolled_subject_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    profile_dir = tempdir / "patches/runtime"
    skipped_patch_file = profile_dir / "x/skipped.patch"
    rerolled_subject_patch_file = profile_dir / "x/rerolled-subject.patch"
    dependent_patch_file = profile_dir / "x/dependent.patch"
    kept_patch_file = profile_dir / "x/kept.patch"
    skipped_patch_file.parent.mkdir(parents=True)
    skipped_patch_file.write_text(skipped_patch)
    rerolled_subject_patch_file.write_text(rerolled_subject_patch)
    dependent_patch_file.write_text(dependent_patch)
    kept_patch_file.write_text(kept_patch)
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=target, check=True)

    test = make_test()
    test.manifest = types.SimpleNamespace(repo_abspath=str(tempdir))
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "x/skipped.patch", "module": "module"},
            {
                "path": "x/rerolled-subject.patch",
                "module": "module",
                "source-commit": "0000000000000000000000000000000000000000",
            },
            {"path": "x/dependent.patch", "module": "module"},
            {"path": "x/kept.patch", "module": "module"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"
    test._apply_profile_module_patches(
        "runtime",
        "module",
        target,
        skip_patch_paths={"x/skipped.patch", "x/dependent.patch"},
    )
    assert (target / "file.txt").read_text() == "base\n"
    assert not (target / "dependent.txt").exists()
    assert (target / "other.txt").read_text() == "kept\n"

    subprocess.run(["git", "reset", "--hard", "-q", skipped_rev], cwd=target, check=True)
    test = make_test()
    test.manifest = types.SimpleNamespace(repo_abspath=str(tempdir))
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "x/skipped.patch", "module": "module"},
            {
                "path": "x/rerolled-subject.patch",
                "module": "module",
                "source-commit": "0000000000000000000000000000000000000000",
            },
            {"path": "x/dependent.patch", "module": "module"},
            {"path": "x/kept.patch", "module": "module"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"
    test._apply_profile_module_patches("runtime", "module", target)
    assert (target / "file.txt").read_text() == "base\nskipped\n"
    assert not (target / "rerolled.txt").exists()
    assert (target / "dependent.txt").read_text() == "dependent\n"
    assert (target / "other.txt").read_text() == "kept\n"

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    repo = tempdir / "darling"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def independent_patch(path, contents, message):
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        (repo / path).write_text(contents)
        subprocess.run(["git", "add", path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
        return subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    skipped_patch = independent_patch("skipped.txt", "skipped\n", "skipped patch")
    kept_patch = independent_patch("kept.txt", "kept\n", "kept patch")
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)

    profile_dir = tempdir / "patches/runtime/darling"
    profile_dir.mkdir(parents=True)
    (profile_dir / "skipped.patch").write_text(skipped_patch)
    (profile_dir / "kept.patch").write_text(kept_patch)

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(
                name="darling",
                path="darling",
                abspath=str(repo),
                revision=base_rev,
            )
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "darling/skipped.patch", "module": "darling"},
            {"path": "darling/kept.patch", "module": "darling"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    patch = {
        "path": "darling/skipped.patch",
        "module": "darling",
        "source-base": base_rev,
    }
    proof = {"mode": "guest-runtime-deploy", "bad-profile": "current-minus-patch"}
    with test._guest_runtime_source_forest(patch, proof, omit_patch=True) as source_root:
        assert (source_root / "base.txt").read_text() == "base\n"
        assert not (source_root / "skipped.txt").exists()
        assert (source_root / "kept.txt").read_text() == "kept\n"

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    repo = tempdir / "darling"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "fixed.txt").write_text("fixed\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "profile_contract.sh").write_text(
        "#!/usr/bin/env sh\n"
        "test -n \"$SRC_ROOT\"\n"
        "test -f \"$SRC_ROOT/fixed.txt\"\n"
    )
    (repo / "tests" / "profile_contract.sh").chmod(0o755)
    subprocess.run(["git", "add", "fixed.txt", "tests/profile_contract.sh"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixed patch"], cwd=repo, check=True)
    fixed_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)

    profile_dir = tempdir / "patches/runtime/darling"
    profile_dir.mkdir(parents=True)
    (profile_dir / "fixed.patch").write_text(fixed_patch)

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(
                name="darling",
                path="darling",
                abspath=str(repo),
                revision=base_rev,
            )
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "darling/fixed.patch", "module": "darling"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"
    test._execution_env = lambda _invocation: {}
    seen = []
    seen_assets = []

    def run_invocation(_invocation, env=None):
        if _invocation.get("runner") == "source-profile-script":
            script_path = _invocation["script_path"]
            assert script_path.is_file(), script_path
            assert script_path.parent.parent == _invocation["cwd"], _invocation
            seen_assets.append(script_path.read_text())
        source_root = Path(env["SRC_ROOT"])
        has_fixed = (source_root / "fixed.txt").exists()
        seen.append(has_fixed)
        if not has_fixed:
            print("old source failure")
        return 0 if has_fixed else 1

    test._run_invocation = run_invocation
    rc = test._run_source_base_proof(
        {"path": "darling/fixed.patch", "module": "darling", "source-base": base_rev},
        {
            "mode": "source-base",
            "source-env": "SRC_ROOT",
            "expect-output-contains": ["old source failure"],
        },
        {"name": "source_profile_green", "shell": False},
    )
    assert rc == 0
    assert seen == [False, True], seen
    assert not (repo / "fixed.txt").exists(), "live checkout should not be the GREEN proof source"
    assert not (repo / "tests" / "profile_contract.sh").exists(), "live checkout should not contain the profile test asset"

    seen.clear()
    rc = test._run_source_base_proof(
        {"path": "darling/fixed.patch", "module": "darling", "source-base": base_rev},
        {
            "mode": "source-base",
            "source-env": "SRC_ROOT",
            "expect-output-contains": ["old source failure"],
        },
        {
            "name": "source_profile_script",
            "shell": False,
            "runner": "source-profile-script",
            "repo": "darling",
            "script": "tests/profile_contract.sh",
            "cwd": repo,
            "script_path": repo / "tests" / "profile_contract.sh",
        },
    )
    assert rc == 0
    assert seen == [False, True], seen
    assert seen_assets == [
        "#!/usr/bin/env sh\n"
        "test -n \"$SRC_ROOT\"\n"
        "test -f \"$SRC_ROOT/fixed.txt\"\n",
        "#!/usr/bin/env sh\n"
        "test -n \"$SRC_ROOT\"\n"
        "test -f \"$SRC_ROOT/fixed.txt\"\n",
    ], seen_assets

    seen.clear()
    rc = test._run_source_base_proof(
        {"path": "darling/fixed.patch", "module": "darling", "source-base": base_rev},
        {
            "mode": "source-base",
            "source-env": "SRC_ROOT",
            "expect-output-contains": ["different failure"],
        },
        {"name": "source_profile_green", "shell": False},
    )
    assert rc == 1
    assert any(
        "RED failure output missing 'different failure'" in message
        for message in test.err_messages
    ), test.err_messages

    subprocess.run(["git", "am", str(profile_dir / "fixed.patch")], cwd=repo, check=True)
    test._profile_is_applied = lambda _profile: True
    seen.clear()
    rc = test._run_source_base_proof(
        {"path": "darling/fixed.patch", "module": "darling", "source-base": base_rev},
        {
            "mode": "source-base",
            "source-env": "SRC_ROOT",
            "expect-output-contains": ["old source failure"],
        },
        {"name": "source_profile_green", "shell": False},
    )
    assert rc == 0
    assert seen == [False, True], seen

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    darling_repo = tempdir / "darling"
    xnu_repo = tempdir / "xnu"
    dserver_repo = tempdir / "darlingserver"
    for repo in (darling_repo, xnu_repo, dserver_repo):
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
        (repo / "base.txt").write_text(f"{repo.name} base\n")
        subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    xnu_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=xnu_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dserver_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=dserver_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def patch_from(repo, base_rev, path, contents, message):
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        (repo / path).write_text(contents)
        subprocess.run(["git", "add", path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        patch_text = subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        return patch_text, commit

    skipped_patch, _ = patch_from(xnu_repo, xnu_base, "skipped.txt", "skipped\n", "skipped patch")
    dserver_patch, dserver_commit = patch_from(dserver_repo, dserver_base, "ring_abi.txt", "profile abi\n", "profile abi")

    profile_dir = tempdir / "patches/runtime"
    (profile_dir / "xnu").mkdir(parents=True)
    (profile_dir / "darlingserver").mkdir(parents=True)
    (profile_dir / "xnu/skipped.patch").write_text(skipped_patch)
    (profile_dir / "darlingserver/ring-abi.patch").write_text(dserver_patch)

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(name="darling", path="darling", abspath=str(darling_repo), revision="HEAD"),
            types.SimpleNamespace(name="xnu", path="darling/src/external/xnu", abspath=str(xnu_repo), revision=xnu_base),
            types.SimpleNamespace(name="darlingserver", path="darling/src/external/darlingserver", abspath=str(dserver_repo), revision=dserver_base),
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "xnu/skipped.patch", "module": "darling/src/external/xnu"},
            {
                "path": "darlingserver/ring-abi.patch",
                "module": "darling/src/external/darlingserver",
                "source-commit": dserver_commit,
            },
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    with test._guest_runtime_source_forest(
        {
            "path": "xnu/skipped.patch",
            "module": "darling/src/external/xnu",
            "source-base": xnu_base,
        },
        {
            "mode": "guest-runtime-deploy",
            "bad-profile": "current-minus-patch",
            "source-modules": ["darling/src/external/darlingserver"],
        },
        omit_patch=True,
    ) as source_root:
        assert not (source_root / "src/external/xnu/skipped.txt").exists()
        assert (source_root / "src/external/darlingserver/ring_abi.txt").read_text() == "profile abi\n"

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    repo = tempdir / "darling"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(
                name="darling",
                path="darling",
                abspath=str(repo),
                revision=base_rev,
            )
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {"patches": []}
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    before = set(Path(tempfile.gettempdir()).glob("west-red-proof-source-*"))
    try:
        with test._guest_runtime_source_forest(
            {"path": "darling/example.patch", "module": "darling", "source-base": base_rev},
            {"mode": "guest-runtime-deploy", "bad-profile": "current-minus-patch"},
            omit_patch=True,
        ) as source_root:
            assert (source_root / "base.txt").read_text() == "base\n"
            raise RuntimeError("forced downstream failure")
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced downstream failure unexpectedly passed")

    after = set(Path(tempfile.gettempdir()).glob("west-red-proof-source-*"))
    kept = list(after - before)
    assert len(kept) == 1, kept
    assert (kept[0] / "darling/base.txt").read_text() == "base\n"
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(kept[0] / "darling")],
        cwd=repo,
        check=True,
    )
    shutil.rmtree(kept[0])

print("PASS west-test-runtime-red-contract")

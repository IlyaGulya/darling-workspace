"""CMake fixture failure handling and source-build execution contracts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
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

import west_commands.test as test_module  # noqa: E402
from west_commands.test import DarlingTest  # noqa: E402
from west_commands.test_execution import ProcessResult  # noqa: E402


def new_test(**attributes) -> DarlingTest:
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = "/workspace"
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.wrn = lambda _message: None
    test._failure_phase = None
    test._record_failure_phase = lambda invocation, phase: setattr(test, "_failure_phase", phase)

    def die(message: str):
        raise SystemExit(message)

    test.die = die
    for name, value in attributes.items():
        setattr(test, name, value)
    return test


# --- _run_testkit_build_command ------------------------------------------
# The runner must be resolved through the facade namespace, because focused
# contracts replace ``west_commands.test.run_bounded`` to intercept execution.
calls: list[dict] = []
original_run_bounded = test_module.run_bounded
test_module.run_bounded = lambda args, **kwargs: (
    calls.append({"args": list(args), **kwargs}) or ProcessResult(0)
)
try:
    test = new_test()
    assert test._run_testkit_build_command("configure", ["cmake", "-S", "x"]) is None
    assert calls[0]["args"] == ["cmake", "-S", "x"]
    assert calls[0]["timeout_seconds"] == 1800

    os.environ["WEST_TEST_BUILD_TIMEOUT_SECONDS"] = "5"
    try:
        errors: list[str] = []
        test_module.run_bounded = lambda args, **kwargs: ProcessResult(124, True)
        test = new_test(err=errors.append, _dump_command_tail=lambda label, result: None)
        try:
            test._run_testkit_build_command("build", ["ninja", "-C", "x"])
        except SystemExit as exc:
            assert "testkit build failed with rc 124" in str(exc), exc
        else:
            raise AssertionError("a timed-out testkit build must fail the command")
        assert errors == ["testkit build timed out after 5s"], errors
    finally:
        del os.environ["WEST_TEST_BUILD_TIMEOUT_SECONDS"]

    test_module.run_bounded = lambda args, **kwargs: ProcessResult(3, stderr="boom")
    test = new_test()
    try:
        test._run_testkit_build_command("configure", ["cmake", "-S", "x"])
    except SystemExit as exc:
        assert "testkit configure failed with rc 3" in str(exc), exc
    else:
        raise AssertionError("a failed testkit configure must fail the command")
finally:
    test_module.run_bounded = original_run_bounded

# --- _run_cmake_configure_fixture ----------------------------------------
test = new_test()
try:
    test._run_cmake_configure_fixture({"name": "fixture", "diag": "guarded"})
except SystemExit as exc:
    assert "cmake-configure-fixture currently supports diag:bare only" in str(exc), exc
else:
    raise AssertionError("a guarded cmake-configure fixture must be refused")

with tempfile.TemporaryDirectory(prefix="west-cmake-fixture-") as raw:
    root = Path(raw)
    source = root / "source"
    source.mkdir()
    errors: list[str] = []
    test = new_test(err=errors.append)
    invocation = {"name": "fixture", "cwd": source, "env": dict(os.environ)}
    assert test._run_cmake_configure_fixture(invocation) == 1
    assert errors and "CMakeLists.txt not found" in errors[0], errors

    (source / "CMakeLists.txt").write_text("project(fixture)\n")
    fake_cmake = {
        "stdout": "fake cmake configured\n",
        "returncode": 0,
        "log_args": True,
    }
    invocation = {
        "name": "fixture",
        "cwd": source,
        "env": dict(os.environ),
        "configure_args": ["-DFIXTURE_FEATURE=ON"],
        "fake_tools": {"cmake": fake_cmake},
        "marker_files": [{"path": "markers/one.txt", "content": "marker\n"}],
        "expect": {
            "returncode": 0,
            "output-contains": ["fake cmake configured"],
            "tool-args-contains": {"cmake": ["-DFIXTURE_FEATURE=ON"]},
        },
    }
    assert test._run_cmake_configure_fixture(invocation) == 0
    assert (source / "markers/one.txt").read_text() == "marker\n"

    failing = dict(invocation)
    failing["fake_tools"] = {"cmake": {"stderr": "no toolchain\n", "returncode": 68, "log_args": True}}
    failing["expect"] = {"returncode": 68}
    assert test._run_cmake_configure_fixture(failing) == 0, "the declared rc must be accepted"

    failing["expect"] = {"returncode": "nonzero"}
    assert test._run_cmake_configure_fixture(failing) == 0

    passing = dict(invocation)
    passing["fake_tools"] = {"cmake": {"stdout": "ok\n", "returncode": 0, "log_args": True}}
    passing["expect"] = {"returncode": "nonzero"}
    errors.clear()
    assert test._run_cmake_configure_fixture(passing) == 1
    assert any("succeeded unexpectedly" in message for message in errors), errors
    assert test._failure_phase == "configure"

    missing_output = dict(invocation)
    missing_output["expect"] = {"returncode": 0, "output-contains": ["not printed"]}
    errors.clear()
    assert test._run_cmake_configure_fixture(missing_output) == 1
    assert any("cmake output missing" in message for message in errors), errors

# --- _run_source_build_fixture -------------------------------------------
with tempfile.TemporaryDirectory(prefix="west-source-build-") as raw:
    root = Path(raw)
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "west-test@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    fixture = repo / "fixture.sh"
    fixture.write_text("#!/bin/sh\nprintf 'fixture\\n'\n")
    (repo / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "add", "fixture.sh", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    messages: list[str] = []
    test = new_test(inf=lambda message: messages.append(message))
    invocation = {
        "name": "source build",
        "cwd": repo,
        "script_path": fixture,
        "env": dict(os.environ),
        "build_commands": ["test -f tracked.txt"],
        # The archived tree must contain the fixture and the env must point at it.
        "run_commands": ['test -f "$WEST_TEST_SOURCE_ROOT/fixture.sh" && test -d "$WEST_TEST_TMP"'],
    }
    assert test._run_source_build_fixture(invocation) == 0
    assert messages == [
        "  source-build-fixture: test -f tracked.txt",
        '  source-build-fixture: test -f "$WEST_TEST_SOURCE_ROOT/fixture.sh" && test -d "$WEST_TEST_TMP"',
    ], messages

    failing = dict(invocation)
    failing["build_commands"] = []
    failing["run_commands"] = ["exit 7"]
    assert test._run_source_build_fixture(failing) == 7
    assert test._failure_phase == "run"

    failing["run_commands"] = []
    failing["build_commands"] = ["exit 9"]
    assert test._run_source_build_fixture(failing) == 9
    assert test._failure_phase == "build"

    missing = dict(invocation)
    missing["script_path"] = repo / "absent.sh"
    try:
        test._run_source_build_fixture(missing)
    except SystemExit as exc:
        assert "fixture not found" in str(exc), exc
    else:
        raise AssertionError("a missing source fixture must be refused")

# Execute the normal configure/compile/run closure. Changes to the selected
# working source must change the target's observable outcome.
with tempfile.TemporaryDirectory(prefix="west-current-cmake-") as raw:
    selected = Path(raw) / "current source"
    selected.mkdir()
    (selected / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.13)\n"
        "project(current_source C)\n"
        "add_executable(consumer consumer.c)\n"
        "add_custom_target(consumer_run COMMAND $<TARGET_FILE:consumer> DEPENDS consumer)\n"
    )
    consumer = selected / "consumer.c"
    consumer.write_text("int main(void) { return 0; }\n")
    invocation = {"name": "current source", "cwd": selected,
                  "cmake_targets": ["consumer_run"], "env": dict(os.environ)}
    test = new_test()
    assert test._run_source_build_fixture(invocation) == 0
    consumer.write_text("int main(void) { return 42; }\n")
    assert test._run_source_build_fixture(invocation) != 0
    assert test._failure_phase == "build"

test = new_test(_project_path=lambda repo: Path("/selected") / repo,
                _resolved_diag=lambda metadata: "bare")
patch = {"module": "darling", "path": "obsolete build"}
try:
    test._test_invocation(patch, {"runner": "west-build", "target": "system_loader"})
except SystemExit:
    pass
else:
    raise AssertionError("obsolete build runner was accepted")

print("PASS cmake-fixture-backend-contract")

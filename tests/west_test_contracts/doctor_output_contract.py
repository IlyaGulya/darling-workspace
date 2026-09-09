"""Focused contract for bounded and machine-readable doctor output."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
import types
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    def __init__(self, name: str, help_text: str, description: str, **_kwargs) -> None:
        self.name = name
        self.help = help_text
        self.description = description


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.doctor import DEFAULT_PROBLEM_LIMIT, DarlingDoctor


def arguments(
    root: Path, *, full: bool = False, json_output: bool = False, scope: str = "all"
):
    return argparse.Namespace(
        prefix=str(root / "prefix with space"),
        build_dir=str(root / "build's output"),
        expect_dyld_md5=None,
        expect_mldr_md5=None,
        expect_dserver_md5=None,
        no_baseline_file=False,
        allow_drift=["-project"],
        extra_prefix=[],
        full=full,
        json=json_output,
        scope=scope,
    )


def run_case(
    root: Path, checker, *, full: bool = False, json_output: bool = False,
    scope: str = "all", runtime_checker=None,
):
    doctor = DarlingDoctor.__new__(DarlingDoctor)
    doctor.topdir = str(root / "workspace")
    doctor.manifest = SimpleNamespace(repo_abspath=str(root / "manifest"))
    messages: list[tuple[str, str]] = []
    doctor.inf = lambda message: messages.append(("info", message))
    doctor.wrn = lambda message: messages.append(("warning", message))
    doctor.err = lambda message: messages.append(("error", message))

    def first(self, _topdir, _args):
        checker(self)

    def remaining_with_topdir(self, _topdir, _args):
        if runtime_checker is not None:
            runtime_checker(self)

    def remaining(self, _args):
        if runtime_checker is not None:
            runtime_checker(self)
        if checker is operational and not hasattr(self, "_contract_continued"):
            self._contract_continued = True
            self._section("later independent section")
            self._ok("later section still ran")

    doctor._check_manifest_drift = MethodType(first, doctor)
    doctor._check_build_prefix = MethodType(remaining_with_topdir, doctor)
    doctor._check_prefix_boot_prereqs = MethodType(remaining, doctor)
    doctor._check_baseline = MethodType(remaining, doctor)
    doctor._check_extra_prefixes = MethodType(remaining, doctor)
    exit_code = 0
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch(
            "west_commands.doctor._west_argv",
            return_value=["/opt/west launcher"],
        ),
    ):
        try:
            doctor.do_run(
                arguments(root, full=full, json_output=json_output, scope=scope), []
            )
        except SystemExit as error:
            exit_code = int(error.code)
    return exit_code, messages


def green(doctor: DarlingDoctor) -> None:
    doctor._section("fixture")
    doctor._ok("all fixture checks passed")


def many_problems(doctor: DarlingDoctor) -> None:
    doctor._section("fixture")
    for index in range(DEFAULT_PROBLEM_LIMIT + 4):
        if index % 3 == 0:
            doctor._warn(f"warning {index}")
        else:
            doctor._problem(f"problem {index}")


def operational(_doctor: DarlingDoctor) -> None:
    raise OSError("fixture authority unavailable")


with tempfile.TemporaryDirectory(prefix="doctor-output-contract-") as temporary:
    root = Path(temporary)

    green_rc, green_messages = run_case(root, green, json_output=True)
    assert green_rc == 0
    assert len(green_messages) == 1 and green_messages[0][0] == "info"
    green_payload = json.loads(green_messages[0][1])
    assert green_payload["schema_version"] == 1
    assert green_payload["operation"] == "doctor"
    assert green_payload["state"] == "healthy"
    assert green_payload["returncode"] == 0
    assert green_payload["summary"] == {
        "checks": 1,
        "ok": 1,
        "problem": 0,
        "warning": 0,
        "operational_error": 0,
    }

    problem_rc, problem_messages = run_case(root, many_problems, json_output=True)
    assert problem_rc == 1
    assert len(problem_messages) == 1
    problem_payload = json.loads(problem_messages[0][1])
    assert problem_payload["state"] == "problems"
    assert problem_payload["returncode"] == 1
    assert len(problem_payload["results"]) == DEFAULT_PROBLEM_LIMIT + 4

    error_rc, error_messages = run_case(root, operational, json_output=True)
    assert error_rc == 1
    assert len(error_messages) == 1
    error_payload = json.loads(error_messages[0][1])
    assert error_payload["state"] == "operational_error"
    assert error_payload["returncode"] == 1
    assert error_payload["error"] == {
        "type": "OSError",
        "message": "fixture authority unavailable",
    }
    assert error_payload["summary"]["operational_error"] == 1
    assert error_payload["summary"]["ok"] == 1
    assert error_payload["errors"] == [
        {
            "section": "1. West manifest revision vs working-tree HEAD",
            "type": "OSError",
            "message": "fixture authority unavailable",
        }
    ]
    assert any(
        result["state"] == "ok" and result["message"] == "later section still ran"
        for result in error_payload["results"]
    )

    default_rc, default_messages = run_case(root, many_problems)
    assert default_rc == 1
    actionable = [
        message
        for level, message in default_messages
        if level == "info" and message.startswith(("FAIL ", "WARN "))
    ]
    assert len(actionable) == DEFAULT_PROBLEM_LIMIT
    assert all(level == "info" for level, _message in default_messages)
    detail_lines = [
        message
        for _level, message in default_messages
        if message.startswith("details: ")
    ]
    assert len(detail_lines) == 1
    assert default_messages[1] == ("info", detail_lines[0])
    assert shlex.split(detail_lines[0].removeprefix("details: ")) == [
        "/opt/west launcher",
        "darling-doctor",
        f"--prefix={root / 'prefix with space'}",
        "--build-dir=" + str(root / "build's output"),
        "--allow-drift=-project",
        "--full",
    ]

    full_rc, full_messages = run_case(root, many_problems, full=True)
    assert full_rc == 1
    assert not any("omitted" in message for _level, message in full_messages)
    assert not any(message.startswith("details: ") for _level, message in full_messages)
    assert sum(level in {"warning", "error"} for level, _message in full_messages) == 13

    command = DarlingDoctor()
    root_parser = argparse.ArgumentParser()
    parser_adder = root_parser.add_subparsers(dest="command", required=True)
    doctor_parser = command.do_add_parser(parser_adder)
    parsed = root_parser.parse_args(["darling-doctor", "--full"])
    assert parsed.full is True and parsed.json is False
    try:
        doctor_parser.parse_args(["--full", "--json"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("--full and --json were not mutually exclusive")

    for scope, workspace_checker, runtime_checker, expected_rc in (
        ("workspace", green, operational, 0),
        ("workspace", many_problems, None, 1),
        ("runtime", many_problems, None, 0),
        ("runtime", green, operational, 1),
        ("all", many_problems, None, 1),
        ("all", green, operational, 1),
    ):
        rc, messages = run_case(
            root, workspace_checker, scope=scope,
            runtime_checker=runtime_checker, json_output=True,
        )
        assert rc == expected_rc, (scope, messages)
        payload = json.loads(messages[0][1])
        detail = shlex.split(payload["detail_command"])
        replay = root_parser.parse_args(detail[1:])
        assert replay.scope == scope
        assert payload["inputs"]["scope"] == scope
        if scope == "runtime" and runtime_checker is operational:
            assert len(payload["errors"]) == 4
            assert all(error["type"] == "OSError" for error in payload["errors"])

print("PASS doctor-output-contract")

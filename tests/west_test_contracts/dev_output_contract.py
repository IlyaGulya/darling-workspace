"""Focused contract for bounded ``west dev`` output and package verification dispatch."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    def __init__(self, name: str, help_text: str, description: str, **_kwargs) -> None:
        self.name = name
        self.help = help_text
        self.description = description

    def die(self, message: str, exit_code: int = 1) -> None:
        raise SystemExit(f"{exit_code}: {message}")


class DevCheckError(RuntimeError):
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

for module_name, attributes in {
    "dev_check": {
        "DevCheckError": DevCheckError,
        "build_check_plan": lambda **_kwargs: {},
        "build_package_plan": lambda **_kwargs: {},
        "execute_check": lambda _plan: {},
        "execute_package": lambda _plan: {},
        "verify_package": lambda _package: {},
    },
    "dev_start": {
        "build_start_plan": lambda **_kwargs: {},
        "execute_start": lambda _plan: {},
        "recover_start": lambda _evidence: {},
    },
    "dev_status": {"collect_status": lambda **_kwargs: {}},
}.items():
    module = types.ModuleType(module_name)
    for name, value in attributes.items():
        setattr(module, name, value)
    sys.modules.setdefault(module_name, module)

from west_commands import dev


class Output:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def inf(self, message: str) -> None:
        self.lines.append(message)


with tempfile.TemporaryDirectory(prefix="dev-output-contract-") as temporary:
    root = Path(temporary)
    output = Output()
    dirty = [
        {"name": f"dirty-{index}", "dirty": True}
        for index in range(dev._HUMAN_PREVIEW_LIMIT + 4)
    ]
    active = [str(root / f"active {index}") for index in range(dev._HUMAN_PREVIEW_LIMIT + 3)]
    status_payload = {
        "schema_version": 1,
        "operation": "status",
        "state": "in_progress",
        "transaction_id": None,
        "inputs": {
            "profile": "-profile with space",
            "bead": "dar-test.1",
            "prefix": str(root / "prefix with space"),
            "build_dir": None,
            "west_argv": ["/recorded/west launcher"],
        },
        "results": {
            "git": {"health": "healthy", "repositories": dirty},
            "deployment_transactions": {"health": "busy", "active": active},
        },
        "next_safe_action": "wait",
    }
    dev._render_human(output, status_payload)
    git_line = next(line for line in output.lines if " git " in f" {line} ")
    assert "dirty-7" in git_line and "dirty-8" not in git_line
    assert "dirty_omitted=4" in git_line
    active_line = next(line for line in output.lines if "deployment_transactions" in line)
    assert "active 7" in active_line and "active 8" not in active_line
    assert "active_omitted=3" in active_line
    status_detail = next(line for line in output.lines if line.startswith("details: "))
    assert output.lines[1] == status_detail
    assert shlex.split(status_detail.removeprefix("details: ")) == [
        "/recorded/west launcher",
        "dev",
        "status",
        "--profile=-profile with space",
        "--bead=dar-test.1",
        f"--prefix={root / 'prefix with space'}",
        "--json",
    ]

    output = Output()
    evidence = root / "evidence with 'quote'.json"
    planned_payload = {
        "schema_version": 1,
        "operation": "check",
        "state": "planned",
        "transaction_id": "abc",
        "inputs": {
            "tier": "canonical",
            "profile": "-profile with space",
            "bead": None,
            "patch": "-patch one",
            "prefix": None,
            "build_dir": None,
            "evidence": str(evidence),
            "west_argv": ["/recorded/west launcher"],
        },
        "steps": [
            {"name": f"mutation-{index}", "argv": ["tool", f"arg {index}"], "mutating": True}
            for index in range(dev._HUMAN_PREVIEW_LIMIT + 3)
        ],
        "results": [],
        "returncode": None,
        "next_safe_action": "execute",
    }
    dev._render_human(output, planned_payload)
    mutation_lines = [line for line in output.lines if line.startswith("mutates: ")]
    assert len(mutation_lines) == dev._HUMAN_PREVIEW_LIMIT
    assert any("3 additional mutation previews omitted" in line for line in output.lines)
    plan_detail = next(line for line in output.lines if line.startswith("details: "))
    assert shlex.split(plan_detail.removeprefix("details: ")) == [
        "/recorded/west launcher",
        "dev",
        "check",
        "canonical",
        "--profile=-profile with space",
        "--patch=-patch one",
        f"--evidence={evidence}",
        "--dry-run",
        "--json",
    ]
    assert output.lines[1] == plan_detail

    for operation in ("check", "package"):
        output = Output()
        completed = {
            "schema_version": 1,
            "operation": operation,
            "state": "committed",
            "transaction_id": "abc",
            "inputs": {"evidence": str(evidence)},
            "steps": [],
            "results": [],
            "returncode": 0,
        }
        dev._render_human(output, completed)
        detail = next(line for line in output.lines if line.startswith("details: "))
        assert output.lines[1] == detail
        assert shlex.split(detail.removeprefix("details: ")) == [
            "cat",
            "--",
            str(evidence),
        ]
        assert "west dev" not in detail.removeprefix("details: ")

    command = dev.DarlingDev()
    root_parser = argparse.ArgumentParser()
    parser_adder = root_parser.add_subparsers(dest="command", required=True)
    dev_parser = command.do_add_parser(parser_adder)
    assert "verify-package" in dev_parser.format_help()
    package = root / "published package"
    parsed = root_parser.parse_args(["dev", "verify-package", str(package), "--json"])
    assert parsed.action == "verify-package"
    assert parsed.package == package
    package_args = root_parser.parse_args(
        [
            "dev",
            "package",
            "--receipt",
            str(root / "acceptance.json"),
            "--output",
            str(root / "review package"),
            "--evidence",
            str(root / "package.json"),
        ]
    )
    assert package_args.required_tier == "acceptance"
    assert parsed.json is True
    errors: list[str] = []
    command.err = errors.append
    try:
        command.do_run(parsed, ["--unknown"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("unknown dev argument did not exit 2")
    assert errors == ["unknown arguments: --unknown"]

    observed: list[Path] = []

    def verify(candidate: Path):
        observed.append(candidate)
        return {
            "schema_version": 1,
            "operation": "package-verify",
            "state": "valid",
            "returncode": 0,
            "package": str(candidate),
        }

    dev.verify_package = verify
    dev._west_argv = lambda: ["west"]
    command.topdir = str(root)
    command.manifest = SimpleNamespace(repo_abspath=str(root))
    emitted: list[str] = []
    command.inf = emitted.append
    command.do_run(parsed, [])
    assert observed == [package.absolute()]
    assert len(emitted) == 1
    verified = json.loads(emitted[0])
    assert verified["operation"] == "package-verify"
    assert verified["state"] == "valid"

    def invalid_result(candidate: Path):
        return {
            "schema_version": 1,
            "operation": "package-verify",
            "state": "invalid",
            "returncode": 1,
            "package": str(candidate),
            "error": {"type": "DevCheckError", "message": "invalid fixture"},
        }

    emitted.clear()
    dev.verify_package = invalid_result
    try:
        command.do_run(parsed, [])
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("invalid JSON result did not exit 1")
    assert len(emitted) == 1
    returned_failure = json.loads(emitted[0])
    assert returned_failure["state"] == "invalid"

    for raised in (
        DevCheckError("package closure is invalid"),
        OSError("package cannot be read"),
    ):
        def reject(_candidate: Path, error=raised):
            raise error

        emitted.clear()
        dev.verify_package = reject
        try:
            command.do_run(parsed, [])
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError(f"{type(raised).__name__} did not exit 1")
        assert len(emitted) == 1
        rejected = json.loads(emitted[0])
        assert rejected == {
            "schema_version": 1,
            "operation": "package-verify",
            "state": "invalid",
            "returncode": 1,
            "package": str(package.absolute()),
            "action": "verify-package",
            "error": {
                "type": type(raised).__name__,
                "message": str(raised),
            },
        }

    def reject_check(**_kwargs):
        raise DevCheckError("acceptance inputs are invalid")

    check_args = root_parser.parse_args(
        [
            "dev",
            "check",
            "acceptance",
            "--evidence",
            str(root / "check.json"),
            "--json",
        ]
    )
    emitted.clear()
    dev.build_check_plan = reject_check
    try:
        command.do_run(check_args, [])
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("check JSON validation error did not exit 1")
    assert len(emitted) == 1
    check_failure = json.loads(emitted[0])
    assert check_failure == {
        "schema_version": 1,
        "operation": "check",
        "state": "invalid",
        "returncode": 1,
        "action": "check",
        "error": {
            "type": "DevCheckError",
            "message": "acceptance inputs are invalid",
        },
    }

print("PASS dev-output-contract")

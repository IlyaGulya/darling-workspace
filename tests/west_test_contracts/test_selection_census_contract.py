"""Behavioral declaration census over canonical workload ids and selectors.

Uses only fixture files and an empty PATH: the default audit cannot configure,
materialize sources, launch a prefix or execute the declared workload scripts.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CENSUS = ROOT / "scripts" / "audit-test-selection.py"

HOST_TIER = '''\
from typing import NamedTuple
class HostCommand(NamedTuple):
    name: str
    argv: list[str]
    cacheable: bool
COMMANDS = (
    HostCommand("host-workloads", ["west", "test", "--env", "host", "--label", "name:alpha_host"], False),
)
'''
TEST_TIER = '''\
#!/usr/bin/env bash
case "${1:-}" in
 guest) west test --env darling --workload alpha-guest --prefix "$prefix" ;;
esac
'''
OPERATOR_DOC = '''\
# Fixture operator commands
```sh
mise run west test --workload beta-host --env host
mise run west test --bead dar-probe.1 --list
mise run west test --all
```
'''


def build_fixture(root: Path, *, probe: bool = False) -> None:
    for directory in ("testkit", "ci", "docs"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    entries = [
        {"id": "alpha-host", "module": "darling", "name": "alpha_host", "env": "host", "runner": "python", "script": "tests/alpha_host.py"},
        # The same display name does not merge distinct declarations.
        {"id": "alpha-host-second", "module": "darling", "name": "alpha_host", "env": "host", "runner": "python", "script": "tests/alpha_host.py"},
        {"id": "alpha-guest", "module": "darling", "name": "alpha_guest", "env": "darling", "runner": "guest-command-fixture", "guest-command": "true"},
        {"id": "beta-host", "module": "darling", "name": "beta_host", "env": "host", "runner": "python", "script": "tests/beta_host.py"},
        {"id": "gamma-native", "module": "darling", "bead": "dar-probe.1", "name": "gamma_native", "env": "macos", "ctest-name": "gamma/native_reference"},
    ]
    if probe:
        entries.append({"id": "unselected-guest", "module": "darling", "name": "unselected_guest", "env": "darling", "runner": "guest-command-fixture", "guest-command": "false"})
    (root / "testkit" / "workloads.yml").write_text(json.dumps({"version": 1, "workloads": entries}))
    (root / "ci" / "run-host-tier.py").write_text(HOST_TIER)
    (root / "ci" / "run-test-tier.sh").write_text(TEST_TIER)
    (root / "docs" / "test-infra.md").write_text(OPERATOR_DOC)


def run_census(workspace: Path) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-B", str(CENSUS), "--workspace", str(workspace)],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={"PATH": "", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return completed.returncode, completed.stdout


def expect_line(output: str, *parts: str) -> None:
    assert any(all(part in line for part in parts) for line in output.splitlines()), (parts, output)


def attribution(output: str, declaration: str) -> list[str]:
    prefix = f"  {declaration} <- "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].split("; ")
    raise AssertionError((declaration, output))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="selection-census-contract-") as raw:
        fixture = Path(raw)
        build_fixture(fixture)
        returncode, output = run_census(fixture)
        assert returncode == 0, output
        expect_line(output, "selection census: 5 declared workload(s)")
        expect_line(output, "selected by an invocation: 5")
        expect_line(output, "unselected: 0")
        for identifier in ("alpha-host", "alpha-host-second"):
            assert attribution(output, f"{identifier}: alpha_host [env:host]") == [
                "ci/run-host-tier.py: west test --env host --label name:alpha_host",
            ], output
        assert attribution(output, "alpha-guest: alpha_guest [env:darling]") == [
            "ci/run-test-tier.sh: west test --env darling --workload alpha-guest --prefix $prefix",
        ], output
        assert attribution(output, "beta-host: beta_host [env:host]") == [
            "docs/test-infra.md: west test --workload beta-host --env host",
        ], output
        assert attribution(output, "gamma-native: gamma_native [env:macos]") == [
            "docs/test-infra.md: west test --bead dar-probe.1 --list",
        ], output
        expect_line(output, "unpinned, selects the default suite: docs/test-infra.md: west test --all")

        build_fixture(fixture, probe=True)
        returncode, output = run_census(fixture)
        assert returncode != 0, output
        expect_line(output, "selection census: 6 declared workload(s)")
        expect_line(output, "selected by an invocation: 5")
        expect_line(output, "unselected: 1")
        expect_line(output, "by environment: darling 1")
        expect_line(output, "unselected-guest: unselected_guest [env:darling]", "NOTHING SELECTS THIS")
        expect_line(output, "unselected-guest: unselected_guest [env:darling]", "every invocation for env:darling is narrower")
        expect_line(output, "selection census failed: 1 declared test(s) are selected by no invocation")
    print("PASS test-selection-census-contract")


if __name__ == "__main__":
    main()

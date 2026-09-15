"""Behavioral contract for the declared-test selection census.

The census must report which invocation selects each declared test and fail when
a declaration is selected by nothing. This contract drives it against its own
fixture workspace - a small profile tree with one host sweep, one label-pinned
tier phase, one documented profile command and one documented ``--bead`` batch -
so the assertions do not depend on the repository's own metadata, which other
work is free to edit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CENSUS = ROOT / "scripts" / "audit-test-selection.py"

ALPHA_PROFILE = """\
version: 1
test-profiles:
  host-contract:
    kind: contract
    coverage-tier: host
    runs: host
    diag: bare
    runner: python
  guest-contract:
    kind: guest
    coverage-tier: runtime
    runs: guest
    diag: guarded
    runner: guest-command-fixture
patches:
- path: alpha/host-contract.patch
  module: darling
  tests:
  - use: host-contract
    name: alpha_host_contract
    script: tests/alpha_host_contract.py
- path: alpha/guest-contract.patch
  module: darling
  tests:
  - use: guest-contract
    name: alpha_guest_contract
    script: tests/alpha_guest_contract.sh
"""

ALPHA_PROBE_PATCH = """\
- path: alpha/unselectable-probe.patch
  module: darling
  tests:
  - use: guest-contract
    name: alpha_probe_selected_by_nothing
    script: tests/alpha_probe_selected_by_nothing.sh
"""

BETA_PROFILE = """\
version: 1
patches:
- path: beta/host-gate.patch
  module: darling
  tests:
  - name: beta_host_gate
    env: host
    diag: bare
    runner: python
    script: tests/beta_host_gate.py
"""

# The bead batch is the only invocation that reaches gamma: nothing sweeps that
# profile, so a reported selection here proves the documented --bead batch counts.
GAMMA_PROFILE = """\
version: 1
patches:
- path: gamma/native-reference.patch
  module: darling
  bead: dar-probe.1
  tests:
  - ctest-name: gamma/native_reference
    env: macos
    diag: bare
    coverage-tier: runtime
"""

SWEEP_TIER = '''\
#!/usr/bin/env python3
"""Fixture host tier: one metadata sweep over the alpha profile."""
from typing import NamedTuple


class HostCommand(NamedTuple):
    name: str
    argv: list[str]
    cacheable: bool


COMMANDS = (
    HostCommand(
        "alpha-host-metadata",
        ["west", "test", "--profile", "alpha", "--env", "host", "--materialize-profile"],
        False,
    ),
)
'''

TEST_TIER = """\
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
\thost)
\t\twest test --profile alpha \\
\t\t\t--env darling \\
\t\t\t--label 'name:alpha_guest_contract' \\
\t\t\t--prefix "$prefix"
\t\t;;
esac
"""

OPERATOR_DOC = """\
# Fixture test infrastructure

Declared host cases are swept with the profile, and the native reference runs
from the bead batch:

```sh
mise run west test --profile beta --env host
mise run west test --bead dar-probe.1 --list
mise run west test --all
```
"""


def build_fixture(root: Path, *, probe: bool = False) -> None:
    """Write the fixture workspace the census reads."""
    for directory in ("patches/alpha", "patches/beta", "patches/gamma", "ci", "docs"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    alpha = ALPHA_PROFILE
    if probe:
        alpha = f"{alpha}{ALPHA_PROBE_PATCH}"
    (root / "patches" / "alpha" / "patches.yml").write_text(alpha)
    (root / "patches" / "beta" / "patches.yml").write_text(BETA_PROFILE)
    (root / "patches" / "gamma" / "patches.yml").write_text(GAMMA_PROFILE)
    (root / "ci" / "run-host-tier.py").write_text(SWEEP_TIER)
    (root / "ci" / "run-test-tier.sh").write_text(TEST_TIER)
    (root / "docs" / "test-infra.md").write_text(OPERATOR_DOC)


def run_census(workspace: Path) -> tuple[int, str]:
    """Run the census against one workspace with no external command available.

    The empty ``PATH`` is deliberate: the default census must be a set of file
    reads with no prefix, no West invocation and no test execution behind it.
    """
    completed = subprocess.run(
        [sys.executable, "-B", str(CENSUS), "--workspace", str(workspace)],
        cwd=str(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={"PATH": "", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return completed.returncode, completed.stdout


def expect_line(output: str, *parts: str) -> None:
    """Require one output line containing every part."""
    for line in output.splitlines():
        if all(part in line for part in parts):
            return
    raise AssertionError(f"no line contains {parts!r}:\n{output}")


def attribution(output: str, declaration: str) -> list[str]:
    """Return the invocations the census attributed to one declaration."""
    prefix = f"  {declaration} <- "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].split("; ")
    raise AssertionError(f"no attribution for {declaration!r}:\n{output}")


def main() -> None:
    """Drive both paths of the census against its own fixture."""
    with tempfile.TemporaryDirectory(prefix="selection-census-contract-") as raw:
        fixture = Path(raw)
        clean_path(fixture)
        probe_path(fixture)
    print("PASS test-selection-census-contract")


def clean_path(fixture: Path) -> None:
    """The fixture with every declaration selected: the census must pass."""
    build_fixture(fixture)
    returncode, output = run_census(fixture)

    assert returncode == 0, output
    expect_line(output, "selection census: 4 declared test(s) in 3 profile(s)")
    expect_line(output, "selected by an invocation: 4")
    expect_line(output, "unselected: 0")
    # Every declaration is attributed to the invocation that selects it.
    assert attribution(output, "alpha alpha/host-contract.patch: alpha_host_contract [env:host]") == [
        "ci/run-host-tier.py: west test --profile alpha --env host --materialize-profile",
    ], output
    assert attribution(
        output, "alpha alpha/guest-contract.patch: alpha_guest_contract [env:darling]"
    ) == [
        "ci/run-test-tier.sh: west test --profile alpha --env darling "
        "--label name:alpha_guest_contract --prefix $prefix",
    ], output
    assert attribution(output, "beta beta/host-gate.patch: beta_host_gate [env:host]") == [
        "docs/test-infra.md: west test --profile beta --env host",
    ], output
    # A --bead batch counts only because a documented invocation names the bead.
    assert attribution(
        output, "gamma gamma/native-reference.patch: gamma/native_reference [env:macos]"
    ) == [
        "docs/test-infra.md: west test --bead dar-probe.1 --list",
    ], output
    # A blanket sweep pins nothing, so the census reports it instead of crediting it.
    expect_line(output, "unpinned, selects the default suite: docs/test-infra.md: west test --all")
    assert "west test --all <-" not in output, output


def probe_path(fixture: Path) -> None:
    """The fixture plus a declaration nothing selects: the census must fail."""
    build_fixture(fixture, probe=True)
    returncode, output = run_census(fixture)

    assert returncode != 0, output
    expect_line(output, "selection census: 5 declared test(s) in 3 profile(s)")
    expect_line(output, "selected by an invocation: 4")
    expect_line(output, "unselected: 1")
    expect_line(output, "  by profile: alpha 1")
    expect_line(
        output,
        "alpha alpha/unselectable-probe.patch: alpha_probe_selected_by_nothing [env:darling]",
        "NOTHING SELECTS THIS",
    )
    expect_line(
        output,
        "alpha alpha/unselectable-probe.patch: alpha_probe_selected_by_nothing [env:darling]",
        "every invocation for profile 'alpha' and this environment is narrower",
    )
    expect_line(
        output,
        "selection census failed: 1 declared test(s) are selected by no invocation",
    )


if __name__ == "__main__":
    main()

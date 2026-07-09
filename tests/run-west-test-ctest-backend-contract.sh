#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
from pathlib import Path

import sys

sys.path.insert(0, "west_commands")
from test_ctest import (
    ctest_command,
    ctest_label_args,
    ctest_label_display,
    ctest_selector_label_args,
)

build = Path("/tmp/build dir")
assert ctest_label_args(build, "bead:dar-gwn.5") == [
    "ctest",
    "--test-dir",
    "/tmp/build dir",
    "--output-on-failure",
    "-L",
    "bead:dar-gwn.5",
]
assert ctest_label_display(build, "bead:dar-gwn.5") == (
    "ctest --test-dir '/tmp/build dir' --output-on-failure -L bead:dar-gwn.5"
)

labels = ctest_selector_label_args(
    bead="dar-gwn.5",
    env="host",
    diag="guarded",
    label="macos:15",
    changed_submodules=["xnu", "darlingserver"],
)
assert labels == [
    "-L",
    "bead:dar-gwn.5",
    "-L",
    "env:host",
    "-L",
    "diag:guarded",
    "-L",
    "macos:15",
    "-L",
    "submod:xnu|submod:darlingserver",
]

command = ctest_command(
    build,
    label_args=labels,
    list_only=True,
    passthrough=["-j4", "--output-junit", "junit.xml"],
)
assert command[:4] == ["ctest", "--test-dir", "/tmp/build dir", "--output-on-failure"]
assert command[4:4 + len(labels)] == labels
assert command[4 + len(labels)] == "--show-only"
assert command[-3:] == ["-j4", "--output-junit", "junit.xml"]
PY

printf 'PASS west-test-ctest-backend-contract\n'

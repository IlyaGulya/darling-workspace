from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "west_commands"))
from test_ctest import (
    ctest_command,
    ctest_label_args,
    ctest_label_display,
    ctest_selector_label_args,
    ctest_submodule_label_name,
)

build = Path("/tmp/build dir")
assert ctest_submodule_label_name("darling/src/external/xnu") == "xnu"
assert ctest_submodule_label_name("xnu") == "xnu"

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
    submodules=["darling/src/external/libplatform", "xnu"],
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
    "submod:xnu|submod:darlingserver|submod:libplatform",
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

import sys
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "west_commands"))
from test_ctest import (
    ctest_command,
    ctest_label_args,
    ctest_label_display,
    ctest_selection_command,
    ctest_selector_label_args,
    ctest_submodule_label_name,
    ctest_uses_prefix,
)
from test_cmake import archive_git_tree_to, archive_source_to

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
assert ctest_selection_command(build, label_args=["-L", "env:darling"]) == [
    "ctest",
    "--test-dir",
    "/tmp/build dir",
    "--show-only=json-v1",
    "-L",
    "env:darling",
]

labels = ctest_selector_label_args(
    bead="dar-gwn.5",
    env="host",
    diag="guarded",
    label="macos:15",
    fuzz=True,
    stress=True,
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
    "fuzz:",
    "-L",
    "stress:",
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

assert ctest_uses_prefix(env="darling", list_only=False)
assert not ctest_uses_prefix(env="darling", list_only=True)
assert not ctest_uses_prefix(env="host", list_only=False)

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    source = root / "source"
    destination = root / "copy"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "west-test@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "West test"], cwd=source, check=True)
    (source / "fixture.txt").write_text("ARCHIVE_COPY_OK\n")
    subprocess.run(["git", "add", "fixture.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=source, check=True)

    assert archive_source_to(source, destination, timeout_seconds=5) == 0
    assert (destination / "fixture.txt").read_text() == "ARCHIVE_COPY_OK\n"

    (source / "tools").mkdir()
    (source / "tools" / "closure.txt").write_text("DCC_ARCHIVE_OK\n")
    subprocess.run(["git", "add", "tools/closure.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "tool fixture"], cwd=source, check=True)
    subset = archive_git_tree_to(
        source,
        root / "subset",
        paths=["tools"],
        timeout_seconds=5,
    )
    assert subset.returncode == 0, subset
    assert (root / "subset/tools/closure.txt").read_text() == "DCC_ARCHIVE_OK\n"

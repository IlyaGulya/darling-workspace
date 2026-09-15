import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from rootless_debug_cleanup import (
    PREFIX_STATE_MARKER,
    cleanup_rootless_debug_tree,
    validate_rootless_debug_tree,
    validate_rootless_disposable_prefix,
)


with tempfile.TemporaryDirectory(dir="/tmp", prefix="darling-rootless-contract-debug-") as temp:
    tree = Path(temp)
    (tree / "artifact").write_text("done\n")
    result = cleanup_rootless_debug_tree(
        tree,
        mount_targets=lambda _path: [],
        processes_for_path=lambda _path: [],
    )
    assert result.success and result.removed, result
    assert not tree.exists()

with tempfile.TemporaryDirectory(dir="/tmp", prefix="darling-rootless-contract-debug-") as temp:
    tree = Path(temp)
    mounted = cleanup_rootless_debug_tree(
        tree,
        mount_targets=lambda path: [path / "mounted"],
        processes_for_path=lambda _path: [],
    )
    assert not mounted.success and tree.exists(), mounted
    live = cleanup_rootless_debug_tree(
        tree,
        mount_targets=lambda _path: [],
        processes_for_path=lambda _path: ["42 darlingserver /tmp/prefix"],
    )
    assert not live.success and tree.exists(), live

with tempfile.TemporaryDirectory(dir="/tmp", prefix="darling-rootless-contract-debug-") as temp:
    tree = Path(temp)

    def denied(_path):
        raise PermissionError("owned by root")

    denied_result = cleanup_rootless_debug_tree(
        tree,
        remover=denied,
        mount_targets=lambda _path: [],
        processes_for_path=lambda _path: [],
    )
    assert not denied_result.success and "--sudo" in denied_result.problems[0], denied_result

    def sudo_runner(args, **_kwargs):
        assert args[:4] == ["sudo", "rm", "-rf", "--one-file-system"], args
        shutil.rmtree(tree)
        return subprocess.CompletedProcess(args, 0, "", "")

    sudo_result = cleanup_rootless_debug_tree(
        tree,
        allow_sudo=True,
        remover=denied,
        runner=sudo_runner,
        mount_targets=lambda _path: [],
        processes_for_path=lambda _path: [],
    )
    assert sudo_result.success and sudo_result.removed, sudo_result

for invalid in (Path("/tmp/darling-rootless-nomount"), Path("/tmp/other-debug-20260711")):
    try:
        validate_rootless_debug_tree(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError(f"unsafe cleanup target was accepted: {invalid}")

print("PASS rootless-debug-cleanup-contract")

with tempfile.TemporaryDirectory(dir="/tmp", prefix="darling-rootless-contract-boot-") as temp:
    prefix = Path(temp)
    (prefix / PREFIX_STATE_MARKER).write_text("{}\n")
    assert validate_rootless_disposable_prefix(prefix) == prefix.resolve()
    removed = cleanup_rootless_debug_tree(
        prefix,
        mount_targets=lambda _path: [],
        processes_for_path=lambda _path: [],
    )
    assert removed.success and removed.removed, removed
    assert not prefix.exists()

with tempfile.TemporaryDirectory(dir="/tmp", prefix="darling-rootless-contract-boot-") as temp:
    # The same shape without the marker is refused: a name that merely looks like a
    # prefix is not proof that the framework owns it.
    prefix = Path(temp)
    try:
        validate_rootless_disposable_prefix(prefix)
    except ValueError as error:
        assert PREFIX_STATE_MARKER in str(error), error
    else:
        raise AssertionError("an unmarked look-alike prefix was accepted")
    assert prefix.exists(), "a refused prefix must still be there"

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from rootless_debug_cleanup import cleanup_rootless_debug_tree, validate_rootless_debug_tree


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

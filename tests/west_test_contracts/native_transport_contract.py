"""Exercise native transport cleanup ownership and immutable-result boundaries."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "ci/native-transport.py"


def invoke(*arguments, expected, env=None):
    result = subprocess.run([sys.executable, "-B", str(RUNNER), *map(str, arguments)],
                            text=True, capture_output=True, timeout=15, env=env)
    assert result.returncode == expected, result.stdout + result.stderr


with tempfile.TemporaryDirectory(prefix="native-transport-contract-") as scratch:
    work = Path(scratch)
    untouched = work / "unrelated"
    untouched.write_text("keep")
    with tempfile.TemporaryDirectory(prefix="darling-native.owned_", dir="/tmp") as temporary:
        owned = Path(temporary)
        (owned / ".owner").write_text("owner-token")
        sentinel = owned / "owned-file"
        sentinel.write_text("keep until authenticated cleanup")
        invoke("cleanup", owned, "wrong-token", expected=2)
        assert sentinel.exists() and untouched.read_text() == "keep"
        active = owned / "results/.running"
        active.parent.mkdir()
        active.touch()
        invoke("cleanup", owned, "owner-token", expected=2)
        assert sentinel.exists(), "cleanup deleted an active runner's workspace"
        active.unlink()
        alias = owned.with_name(owned.name + "_alias")
        alias.symlink_to(owned, target_is_directory=True)
        try:
            invoke("cleanup", alias, "owner-token", expected=2)
            assert sentinel.exists(), "cleanup followed an alias into another directory"
        finally:
            alias.unlink()
        invoke("cleanup", owned, "owner-token", expected=0)
        assert not owned.exists() and untouched.read_text() == "keep"

    # A rejected evidence destination must not mutate the bundle first.
    bundle = work / "bundle"
    bundle.mkdir()
    (bundle / "CTestTestfile.cmake").write_text("")
    (bundle / "native-build.json").write_text("{}")
    (bundle / "testcase").mkdir()
    internal = bundle / "results"
    invoke("local", bundle, expected=2,
           env=dict(os.environ, DARLING_NATIVE_RESULTS_DIR=str(internal)))
    assert not internal.exists(), "rejected results path mutated the immutable bundle"
    existing = work / "existing-results"
    existing.mkdir()
    (existing / "sentinel").write_text("do not merge")
    invoke("local", bundle, expected=2,
           env=dict(os.environ, DARLING_NATIVE_RESULTS_DIR=str(existing)))
    assert sorted(path.name for path in existing.iterdir()) == ["sentinel"]
    assert (existing / "sentinel").read_text() == "do not merge"

print("PASS native-transport-contract")

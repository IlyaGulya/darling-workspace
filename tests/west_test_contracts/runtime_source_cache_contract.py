"""Contract for immutable patch-identity cache keys and invalidation."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "west_commands"))

from west_commands.test_runtime_source import RuntimeSourceMaterializer


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    workspace = root / "workspace"
    repo = root / "repo"
    workspace.mkdir()
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "value").write_text("base\n")
    subprocess.run(["git", "add", "value"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "value").write_text("fixed\n")
    subprocess.run(["git", "commit", "-qam", "fixed"], cwd=repo, check=True)
    fixed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    infos = []
    host = types.SimpleNamespace(
        manifest=types.SimpleNamespace(repo_abspath=str(workspace)),
        inf=infos.append,
    )
    materializer = RuntimeSourceMaterializer(host)
    patch = {"source-commit": fixed}
    assert materializer.profile_patch_is_already_applied(repo, root / "unused.patch", patch)
    assert not infos
    assert materializer.profile_patch_is_already_applied(repo, root / "unused.patch", patch)
    assert any("patch identity cache hit" in message for message in infos), infos

    subprocess.run(["git", "switch", "--detach", base], cwd=repo, check=True, capture_output=True)
    infos.clear()
    assert not materializer.profile_patch_is_already_applied(repo, root / "unused.patch", patch)
    assert not infos, "different HEAD reused a stale identity result"

print("PASS runtime-source-cache-contract")

"""Contracts for disposable fresh-prefix creation and removal."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from fresh_prefix import create_fresh_prefix, disposable_prefix_path, remove_fresh_prefix


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    baseline = root / "baseline"
    baseline.mkdir()
    (baseline / "bin").mkdir()
    original = baseline / "bin" / "darling"
    original.write_text("baseline launcher\n")
    target = root / "darling-fresh-prefix-contract"

    created = create_fresh_prefix(
        baseline,
        destination=target,
        temp_root=root,
        reserve_bytes=0,
    )
    assert created.success, created.problems
    assert created.path == target
    copied = target / "bin" / "darling"
    assert copied.read_text() == "baseline launcher\n"
    assert copied.stat().st_ino != original.stat().st_ino
    copied.write_text("fresh launcher\n")
    assert original.read_text() == "baseline launcher\n"

    removed = remove_fresh_prefix(target, temp_root=root)
    assert removed.success, removed.problems
    assert not target.exists()

    insufficient = create_fresh_prefix(
        baseline,
        destination=root / "darling-fresh-prefix-insufficient",
        temp_root=root,
        reserve_bytes=1 << 62,
    )
    assert not insufficient.success
    assert "needs" in insufficient.problems[0]

for invalid in (Path("/tmp/not-disposable"), Path("/tmp/darling-fresh-prefix-parent/child")):
    try:
        disposable_prefix_path(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted unsafe fresh prefix path: {invalid}")


print("PASS west-fresh-prefix-contract")

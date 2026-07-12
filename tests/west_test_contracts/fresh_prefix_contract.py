"""Contracts for disposable fresh-prefix creation and removal."""

from __future__ import annotations

import errno
import sys
import socket
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import fresh_prefix
from fresh_prefix import create_fresh_prefix, disposable_prefix_path, prefix_tree_size, remove_fresh_prefix


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    baseline = root / "baseline"
    baseline.mkdir()
    (baseline / "bin").mkdir()
    original = baseline / "bin" / "darling"
    original.write_text("baseline launcher\n")
    canonical_toolchain = baseline / "Library" / "Developer" / "CommandLineTools"
    canonical_toolchain.mkdir(parents=True)
    (canonical_toolchain / "clang").write_text("canonical compiler\n")
    stale_toolchain = baseline / "Library" / "Developer" / "CommandLineTools.clt11-bak"
    stale_toolchain.mkdir()
    (stale_toolchain / "obsolete").write_bytes(b"x" * 4096)
    (baseline / "private" / "tmp").mkdir(parents=True)
    transient = baseline / "private" / "tmp" / "stale.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(transient))
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
    assert (target / "Library" / "Developer" / "CommandLineTools" / "clang").exists()
    assert not (target / "Library" / "Developer" / "CommandLineTools.clt11-bak").exists()
    assert prefix_tree_size(baseline) < 4096
    assert any(
        "using " in message and "stale CommandLineTools" in message
        for message in created.changed
    ), created.changed
    assert not (target / "private" / "tmp" / "stale.sock").exists()
    copied.write_text("fresh launcher\n")
    assert original.read_text() == "baseline launcher\n"
    server.close()

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

    fallback_source = root / "fallback-source"
    fallback_destination = root / "fallback-destination"
    fallback_source.write_text("fallback contents\n")
    original_reflink_copy = fresh_prefix._reflink_copy
    try:
        def unavailable_reflink(_source: str, _destination: str) -> str:
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")

        fresh_prefix._reflink_copy = unavailable_reflink
        fresh_prefix._copy_file(str(fallback_source), str(fallback_destination), reflink=True)
    finally:
        fresh_prefix._reflink_copy = original_reflink_copy
    assert fallback_destination.read_text() == "fallback contents\n"

for invalid in (Path("/tmp/not-disposable"), Path("/tmp/darling-fresh-prefix-parent/child")):
    try:
        disposable_prefix_path(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted unsafe fresh prefix path: {invalid}")


print("PASS west-fresh-prefix-contract")

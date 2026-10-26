"""Contracts for disposable fresh-prefix creation and removal."""

from __future__ import annotations

import errno
import sys
import shutil
import socket
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import fresh_prefix
from fresh_prefix import create_fresh_prefix, disposable_prefix_path, prefix_tree_size, remove_fresh_prefix
from runtime_install import InstalledRuntime


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    baseline = root / "baseline"
    baseline.mkdir()
    original = baseline / "payload.txt"
    original.write_text("baseline guest payload\n")
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
    (baseline / ".darling-prefix-state-v2").write_text(
        "DARLING_PREFIX_STATE_V2\n"
        "schema_version=2\n"
        "runtime_mode=rootless-eunion\n"
        "generation=1\n"
        "prefix_device=1\n"
        "prefix_inode=1\n"
        "owner_uid=0\n"
        "owner_gid=0\n"
        "provenance=darling-runtime-prefix-lifecycle-v2\n"
    )
    target = root / "darling-fresh-prefix-contract"
    runtime_root = root / "runtime"
    (runtime_root / "bin").mkdir(parents=True)
    system_root = runtime_root / "libexec" / "darling"
    system_root.mkdir(parents=True)
    launcher = runtime_root / "bin" / "darling"
    launcher.write_text(
        "#!/bin/sh\n"
        'test "$1" = "bind-clone" || exit 2\n'
        'printf "DARLING_PREFIX_STATE_V2\\nschema_version=2\\nruntime_mode=rootless-eunion\\n'
        'generation=1\\nprovenance=contract-bind\\n" > "$DPREFIX/.darling-prefix-state-v2"\n'
    )
    launcher.chmod(0o755)
    runtime = InstalledRuntime(
        root=runtime_root, launcher=launcher, system_root=system_root, identity=None
    )
    runtime_before = launcher.read_bytes()

    created = create_fresh_prefix(
        baseline,
        destination=target,
        temp_root=root,
        reserve_bytes=0,
        runtime=runtime,
    )
    assert created.success, created.problems
    assert created.path == target
    # A copied prefix is a DIFFERENT prefix: only the product's checked bind-clone
    # operation publishes its lifecycle record.
    rebound = (target / ".darling-prefix-state-v2").read_text()
    assert "provenance=contract-bind" in rebound, rebound
    assert rebound != (baseline / ".darling-prefix-state-v2").read_text()
    copied = target / "payload.txt"
    assert copied.read_text() == "baseline guest payload\n"
    assert copied.stat().st_ino != original.stat().st_ino
    assert (target / "Library" / "Developer" / "CommandLineTools" / "clang").exists()
    assert not (target / "Library" / "Developer" / "CommandLineTools.clt11-bak").exists()
    assert prefix_tree_size(baseline) < 4096
    assert any(
        "using " in message and "stale CommandLineTools" in message
        for message in created.changed
    ), created.changed
    assert not (target / "private" / "tmp" / "stale.sock").exists()
    copied.write_text("fresh guest payload\n")
    assert original.read_text() == "baseline guest payload\n"
    assert not (target / "bin" / "darling").exists()
    assert not (target / "libexec" / "darling").exists()
    assert launcher.read_bytes() == runtime_before
    server.close()

    removed = remove_fresh_prefix(target, temp_root=root)
    assert removed.success, removed.problems
    assert not target.exists()

    insufficient = create_fresh_prefix(
        baseline,
        destination=root / "darling-fresh-prefix-insufficient",
        temp_root=root,
        reserve_bytes=1 << 62,
        runtime=runtime,
    )
    assert not insufficient.success
    assert "needs" in insufficient.problems[0]

    alias = root / "baseline-alias"
    alias.symlink_to(baseline, target_is_directory=True)
    parent_alias = root / "parent-alias"
    parent_alias.symlink_to(root, target_is_directory=True)
    for index, unsafe in enumerate((
        str(alias),
        str(parent_alias / "baseline"),
        f"{root}/./baseline",
        f"{root}/baseline/../baseline",
    )):
        rejected_target = root / f"darling-fresh-prefix-unsafe-{index}"
        rejected = create_fresh_prefix(
            unsafe, destination=rejected_target, temp_root=root,
            reserve_bytes=0, runtime=runtime,
        )
        assert not rejected.success, unsafe
        assert not rejected_target.exists(), unsafe
    for unsafe in (
        f"{root}/./darling-fresh-prefix-invalid",
        f"{root}/baseline/../darling-fresh-prefix-invalid",
        str(parent_alias / "darling-fresh-prefix-invalid"),
    ):
        try:
            disposable_prefix_path(unsafe, temp_root=root)
        except ValueError:
            pass
        else:
            raise AssertionError(f"normalized an unsafe fresh-prefix destination: {unsafe}")

    legacy = root / "legacy-combined"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "darling").write_text("obsolete launcher\n")
    (legacy / "bin" / "darlingserver").write_text("obsolete server\n")
    (legacy / "libexec" / "darling").mkdir(parents=True)
    legacy_target = root / "darling-fresh-prefix-legacy"
    rejected = create_fresh_prefix(
        legacy, destination=legacy_target, temp_root=root,
        reserve_bytes=0, runtime=runtime,
    )
    assert not rejected.success
    assert not legacy_target.exists(), "legacy runtime image was copied before admission"
    assert launcher.read_bytes() == runtime_before

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
        disposable_prefix_path(invalid, temp_root=root)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted unsafe fresh prefix path: {invalid}")


print("PASS west-fresh-prefix-contract")

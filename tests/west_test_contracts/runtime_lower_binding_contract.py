"""Production deployment contract for the retained runtime lower-root binding."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.deploy_transaction import (  # noqa: E402
    RUNTIME_LOWER_BINDING_NAME,
    DeploymentTransaction,
    DeploymentTransactionError,
)


def prefix_fixture(root: Path) -> tuple[Path, Path]:
    prefix = root / "prefix"
    lower = prefix / "libexec/darling"
    controller = prefix / "bin/darlingserver"
    lower.mkdir(parents=True)
    controller.parent.mkdir(parents=True)
    controller.write_bytes(b"exact deployed darlingserver\n")
    controller.chmod(0o755)
    return prefix, lower


def parse_binding(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "DARLING_RUNTIME_LOWER_BINDING_V1"
    return dict(line.split("=", 1) for line in lines[1:])


def run_umask_case(value: int) -> None:
    os.umask(value)
    with tempfile.TemporaryDirectory(prefix="runtime-lower-umask-") as temp:
        root = Path(temp)
        prefix = root / "prefix"
        prefix.mkdir()
        lower_source = root / "dyld"
        controller_source = root / "darlingserver"
        lower_source.write_bytes(b"exact deployed dyld\n")
        controller_source.write_bytes(b"exact deployed darlingserver\n")
        controller_source.chmod(0o755)
        transaction = DeploymentTransaction(
            root / "manifest.json", prefix, normalize_modes=True
        )
        transaction.replace(lower_source, prefix / "libexec/darling/usr/lib/dyld")
        transaction.replace(controller_source, prefix / "bin/darlingserver")
        lower = prefix / "libexec/darling"
        binding = transaction.bind_runtime_lower_root(prefix_generation=41)
        fields = parse_binding(binding)
        lower_stat = lower.stat()
        assert int(fields["lower_uid"]) == os.geteuid()
        assert int(fields["lower_gid"]) == os.getegid()
        assert int(fields["lower_mode"]) == stat.S_IMODE(lower_stat.st_mode)
        assert int(fields["lower_mode"]) == 0o755
        assert fields["destination"] == "libexec/darling"
        assert fields["controller_destination"] == "bin/darlingserver"
        assert binding.stat().st_mode & 0o777 == 0o600
        manifest = json.loads((root / "manifest.json").read_text())
        assert manifest["version"] == 2
        assert manifest["transaction_id"] == fields["transaction_id"]
        assert manifest["runtime_lower_binding"] == {
            key: int(value) if key in {
                "schema_version", "prefix_generation", "prefix_device", "prefix_inode",
                "lower_device", "lower_inode", "lower_mode", "lower_uid", "lower_gid",
                "controller_device", "controller_inode", "controller_mode",
                "controller_uid", "controller_gid",
            } else value
            for key, value in fields.items()
            if key != "transaction_id"
        } | {"transaction_id": fields["transaction_id"]}
        transaction.rollback()
        assert not binding.exists()


if len(sys.argv) == 3 and sys.argv[1] == "--umask-case":
    run_umask_case(int(sys.argv[2], 8))
    raise SystemExit(0)

for mask in (0o002, 0o077):
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--umask-case", oct(mask)],
        check=True,
        timeout=20,
    )

with tempfile.TemporaryDirectory(prefix="runtime-lower-negatives-") as temp:
    root = Path(temp)
    prefix, lower = prefix_fixture(root)
    transaction = DeploymentTransaction(root / "manifest.json", prefix)
    try:
        transaction.bind_runtime_lower_root(prefix_generation=0)
    except DeploymentTransactionError:
        pass
    else:
        raise AssertionError("zero generation accepted")

    retained = prefix / "libexec/lower-retained"
    lower.rename(retained)
    lower.symlink_to(retained, target_is_directory=True)
    try:
        transaction.bind_runtime_lower_root(prefix_generation=1)
    except OSError:
        pass
    else:
        raise AssertionError("symlink runtime lower root accepted")

with tempfile.TemporaryDirectory(prefix="runtime-lower-rollback-") as temp:
    root = Path(temp)
    prefix, _ = prefix_fixture(root)
    manifest = root / "manifest.json"
    transaction = DeploymentTransaction(manifest, prefix)
    binding = transaction.bind_runtime_lower_root(prefix_generation=9)
    transaction.commit()
    retained = root / "retained-binding"
    binding.rename(retained)
    replacement = binding
    replacement.write_bytes(retained.read_bytes())
    replacement.chmod(0o600)
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    try:
        DeploymentTransaction.restore(manifest, prefix)
    except DeploymentTransactionError as error:
        assert "changed deploy destination" in str(error)
    else:
        raise AssertionError("rollback removed a replacement inode")
    assert (replacement.stat().st_dev, replacement.stat().st_ino) == replacement_identity
    assert replacement.read_bytes() == retained.read_bytes()

with tempfile.TemporaryDirectory(prefix="runtime-lower-post-publish-fault-") as temp:
    root = Path(temp)
    prefix, _ = prefix_fixture(root)

    class PostPublishFault(DeploymentTransaction):
        def _write(self, state: str) -> None:
            if (
                not getattr(self, "_faulted", False)
                and self.entries
                and self.entries[-1].destination.endswith(RUNTIME_LOWER_BINDING_NAME)
            ):
                self._faulted = True
                raise DeploymentTransactionError("injected post-publish manifest fault")
            super()._write(state)

    transaction = PostPublishFault(root / "manifest.json", prefix)
    try:
        transaction.bind_runtime_lower_root(prefix_generation=12)
    except DeploymentTransactionError as error:
        assert "post-publish" in str(error)
        transaction.rollback()
    else:
        raise AssertionError("post-publish fault did not fire")
    assert not (prefix / RUNTIME_LOWER_BINDING_NAME).exists()

print("RUNTIME_LOWER_BINDING_DEPLOYMENT_VALID")

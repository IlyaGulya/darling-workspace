"""Behavioral contract for focused deploy transaction records."""

from __future__ import annotations

import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.deploy_transaction import DeploymentTransaction, DeploymentTransactionError


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    destination = prefix / "libexec/darling/usr/libexec/shellspawn"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old shellspawn\n")
    source = root / "shellspawn"
    source.write_bytes(b"new shellspawn\n")
    manifest = root / "transaction.json"

    transaction = DeploymentTransaction(manifest, prefix)
    transaction.replace(source, destination)
    transaction.commit()
    assert destination.read_bytes() == b"new shellspawn\n"
    assert manifest.is_file()
    DeploymentTransaction.restore(manifest, prefix)
    assert destination.read_bytes() == b"old shellspawn\n"

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    destination = prefix / "libexec/darling/System/Library/LaunchDaemons/job.plist"
    destination.parent.mkdir(parents=True)
    destination.parent.chmod(0o775)
    destination.write_bytes(b"old plist\n")
    destination.chmod(0o664)
    source = root / "job.plist"
    source.write_bytes(b"new plist\n")
    source.chmod(0o664)
    manifest = root / "transaction.json"

    transaction = DeploymentTransaction(manifest, prefix, normalize_modes=True)
    transaction.replace(source, destination)
    assert destination.stat().st_mode & 0o777 == 0o644
    assert destination.parent.stat().st_mode & 0o777 == 0o755
    transaction.commit()
    DeploymentTransaction.restore(manifest, prefix)
    assert destination.read_bytes() == b"old plist\n"
    assert destination.stat().st_mode & 0o777 == 0o664
    assert destination.parent.stat().st_mode & 0o777 == 0o775

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    destination = prefix / "libexec/darling/System/Library/LaunchDaemons/job.plist"
    source = root / "job.plist"
    source.write_bytes(b"new plist\n")
    source.chmod(0o664)
    manifest = root / "transaction.json"

    transaction = DeploymentTransaction(manifest, prefix, normalize_modes=True)
    transaction.replace(source, destination)
    (prefix / "libexec/darling/private/var/tmp").mkdir(parents=True)
    transaction.commit()
    DeploymentTransaction.restore(manifest, prefix)
    assert not destination.exists()
    assert (prefix / "libexec/darling/private/var/tmp").is_dir()

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    destination = prefix / "libexec/darling/usr/libexec/shellspawn"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old\n")
    source = root / "shellspawn"
    source.write_bytes(b"new\n")
    manifest = root / "transaction.json"

    transaction = DeploymentTransaction(manifest, prefix)
    transaction.replace(source, destination)
    transaction.commit()
    destination.write_bytes(b"third party change\n")
    try:
        DeploymentTransaction.restore(manifest, prefix)
    except DeploymentTransactionError as error:
        assert "changed deploy destination" in str(error)
    else:
        raise AssertionError("restore overwrote a changed destination")

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    source = root / "shellspawn"
    source.write_bytes(b"new\n")
    outside = root / "outside"
    transaction = DeploymentTransaction(root / "transaction.json", prefix)
    try:
        transaction.replace(source, outside)
    except DeploymentTransactionError as error:
        assert "escapes allowed prefixes" in str(error)
    else:
        raise AssertionError("deploy accepted a destination outside the prefix")

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    extra_prefix = root / "extra-prefix"
    source = root / "shellspawn"
    source.write_bytes(b"new\n")
    destination = extra_prefix / "usr/lib/system/libcache.dylib"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old\n")
    manifest = root / "transaction.json"

    transaction = DeploymentTransaction(manifest, prefix, [extra_prefix])
    transaction.replace(source, destination)
    transaction.commit()
    DeploymentTransaction.restore(manifest, prefix)
    assert destination.read_bytes() == b"old\n"

print("PASS deploy-transaction-contract")

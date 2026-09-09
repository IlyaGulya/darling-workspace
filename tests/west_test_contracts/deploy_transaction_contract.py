"""Behavioral contract for focused deploy transaction records."""

from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

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

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    extra_prefix = root / "extra-prefix"
    source = root / "module.pm"
    source.write_bytes(b"new module\n")
    destination = prefix / "lib/perl5/Module.pm"
    destination.parent.mkdir(parents=True)
    destination.parent.chmod(0o775)
    destination.write_bytes(b"old module\n")
    manifest = root / "transaction.json"
    transaction = DeploymentTransaction(
        manifest, prefix, [extra_prefix], normalize_modes=True
    )
    transaction.replace(source, destination)
    sibling = destination.with_name("Sibling.pm")
    transaction.replace(source, sibling)
    unfinished = extra_prefix / "lib/perl5/Package/Module.pm"
    with patch.object(Path, "replace", side_effect=OSError("manifest interrupted")):
        try:
            transaction.replace(source, unfinished)
        except OSError:
            pass
        else:
            raise AssertionError("deploy ignored failed directory publication")
    # Retrying must publish the prepared directories even though their modes
    # are already normalized after the interrupted manifest write.
    with patch.object(
        DeploymentTransaction, "_replace_file", side_effect=OSError("copy interrupted")
    ):
        try:
            transaction.replace(source, unfinished)
        except OSError:
            pass
        else:
            raise AssertionError("deploy ignored a failed file replacement")
    # A fresh process can recover completed files and the directory preparation
    # of the failed replacement without requiring commit or the original object.
    DeploymentTransaction.restore(manifest, prefix)
    assert destination.read_bytes() == b"old module\n"
    assert destination.parent.stat().st_mode & 0o777 == 0o775
    assert not unfinished.exists()
    assert not sibling.exists()
    assert not (extra_prefix / "lib").exists()

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    prefix.mkdir()
    source = root / "module.pm"
    source.write_bytes(b"new module\n")
    first = prefix / "First.pm"
    second = prefix / "Second.pm"
    first.write_bytes(b"old first\n")
    second.write_bytes(b"old second\n")
    manifest = root / "transaction.json"
    transaction = DeploymentTransaction(manifest, prefix)
    transaction.replace(source, first)
    previous_manifest = manifest.read_bytes()
    with patch.object(Path, "replace", side_effect=OSError("manifest interrupted")):
        try:
            transaction.replace(source, second)
        except OSError:
            pass
        else:
            raise AssertionError("deploy ignored failed manifest publication")
    assert manifest.read_bytes() == previous_manifest
    transaction.rollback()
    assert first.read_bytes() == b"old first\n"
    assert second.read_bytes() == b"old second\n"

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    prefix.mkdir()
    source = root / "module.pm"
    source.write_bytes(b"new module\n")
    destination = prefix / "Module.pm"
    destination.write_bytes(b"old module\n")
    alias = prefix / "Alias.pm"
    alias.symlink_to(destination)
    outside = root / "outside"
    outside.write_bytes(b"outside\n")
    escape = prefix / "Escape.pm"
    escape.symlink_to(outside)
    manifest = root / "transaction.json"
    transaction = DeploymentTransaction(manifest, prefix)
    transaction.replace(source, destination)
    for rejected in (alias, escape):
        try:
            transaction.replace(source, rejected)
        except DeploymentTransactionError:
            pass
        else:
            raise AssertionError("deploy accepted a duplicate or escaping symlink")
    transaction.rollback()
    assert destination.read_bytes() == b"old module\n"
    assert outside.read_bytes() == b"outside\n"
    assert alias.is_symlink() and escape.is_symlink()

print("PASS deploy-transaction-contract")

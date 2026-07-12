"""Transactional deployment records for focused Darling runtime experiments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


class DeploymentTransactionError(RuntimeError):
    """A deploy transaction cannot safely continue or be restored."""


@dataclass(frozen=True)
class DeploymentEntry:
    destination: str
    backup: str | None
    previous_sha256: str | None
    deployed_sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DeploymentTransaction:
    """Record and restore a bounded set of file replacements under one prefix."""

    def __init__(
        self, manifest_path: Path, prefix: Path, additional_prefixes: list[Path] | None = None
    ):
        self.manifest_path = manifest_path.resolve()
        self.prefix = prefix.resolve()
        self.roots = (self.prefix, *(path.resolve() for path in additional_prefixes or []))
        self.backup_root = self.manifest_path.parent / f"{self.manifest_path.name}.backups"
        self.entries: list[DeploymentEntry] = []
        if self.manifest_path.exists():
            raise DeploymentTransactionError(
                f"deploy manifest already exists: {self.manifest_path}; restore or remove it first"
            )
        self._write("active")

    def replace(self, source: Path, destination: Path) -> None:
        source = source.resolve()
        destination = destination.resolve()
        self._require_destination(destination)
        if not source.is_file():
            raise DeploymentTransactionError(f"deploy source is not a regular file: {source}")
        if any(Path(entry.destination) == destination for entry in self.entries):
            raise DeploymentTransactionError(f"duplicate deploy destination: {destination}")
        backup = None
        previous_sha256 = None
        if destination.exists():
            if not destination.is_file():
                raise DeploymentTransactionError(
                    f"deploy destination is not a regular file: {destination}"
                )
            previous_sha256 = sha256_file(destination)
            backup = self.backup_root / str(len(self.entries))
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        entry = DeploymentEntry(
            destination=str(destination),
            backup=str(backup) if backup is not None else None,
            previous_sha256=previous_sha256,
            deployed_sha256=sha256_file(destination),
        )
        self.entries.append(entry)
        self._write("active")

    def commit(self) -> None:
        self._write("committed")

    def rollback(self) -> None:
        self._restore_entries(self.entries)
        self._write("restored")

    @classmethod
    def manifest_roots(cls, manifest_path: Path, prefix: Path) -> tuple[Path, ...]:
        manifest_path = manifest_path.resolve()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise DeploymentTransactionError(f"unsupported deploy manifest: {manifest_path}")
        if Path(str(payload.get("prefix", ""))).resolve() != prefix.resolve():
            raise DeploymentTransactionError(
                f"deploy manifest belongs to a different prefix: {manifest_path}"
            )
        roots = tuple(Path(path).resolve() for path in payload.get("roots", []))
        if not roots or roots[0] != prefix.resolve():
            raise DeploymentTransactionError(f"invalid deploy manifest roots: {manifest_path}")
        return roots

    @classmethod
    def restore(cls, manifest_path: Path, prefix: Path) -> None:
        manifest_path = manifest_path.resolve()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        roots = cls.manifest_roots(manifest_path, prefix)
        transaction = cls.__new__(cls)
        transaction.manifest_path = manifest_path
        transaction.prefix = prefix.resolve()
        transaction.roots = roots
        transaction.backup_root = manifest_path.parent / f"{manifest_path.name}.backups"
        transaction.entries = [DeploymentEntry(**entry) for entry in payload.get("entries", [])]
        if payload.get("state") == "restored":
            raise DeploymentTransactionError(f"deploy manifest is already restored: {manifest_path}")
        transaction._restore_entries(transaction.entries)
        transaction._write("restored")

    def _restore_entries(self, entries: list[DeploymentEntry]) -> None:
        for entry in reversed(entries):
            destination = Path(entry.destination)
            self._require_destination(destination)
            if not destination.is_file() or sha256_file(destination) != entry.deployed_sha256:
                raise DeploymentTransactionError(
                    f"refusing to restore changed deploy destination: {destination}"
                )
            if entry.backup is None:
                destination.unlink()
                continue
            backup = Path(entry.backup)
            if not backup.is_file() or sha256_file(backup) != entry.previous_sha256:
                raise DeploymentTransactionError(f"deploy backup is invalid: {backup}")
            shutil.copy2(backup, destination)
            if sha256_file(destination) != entry.previous_sha256:
                raise DeploymentTransactionError(f"restored checksum mismatch: {destination}")

    def _require_destination(self, destination: Path) -> None:
        if not any(destination == root or root in destination.parents for root in self.roots):
            raise DeploymentTransactionError(
                f"deploy manifest destination escapes allowed prefixes: {destination}"
            )

    def _write(self, state: str) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "state": state,
            "prefix": str(self.prefix),
            "roots": [str(root) for root in self.roots],
            "entries": [asdict(entry) for entry in self.entries],
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.manifest_path.name}.", dir=self.manifest_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            Path(temporary).replace(self.manifest_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

"""Transactional deployment records for focused Darling runtime experiments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
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


@dataclass(frozen=True)
class DirectoryEntry:
    path: str
    previous_mode: int | None
    deployed_mode: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DeploymentTransaction:
    """Record and restore a bounded set of file replacements under one prefix."""

    def __init__(
        self,
        manifest_path: Path,
        prefix: Path,
        additional_prefixes: list[Path] | None = None,
        *,
        normalize_modes: bool = False,
    ):
        self.manifest_path = manifest_path.resolve()
        self.prefix = prefix.resolve()
        self.roots = (self.prefix, *(path.resolve() for path in additional_prefixes or []))
        self.backup_root = self.manifest_path.parent / f"{self.manifest_path.name}.backups"
        self.entries: list[DeploymentEntry] = []
        self.directory_entries: list[DirectoryEntry] = []
        self.normalize_modes = normalize_modes
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
        self._prepare_destination_parent(destination)
        self._replace_file(source, destination)
        if self.normalize_modes:
            os.chmod(destination, stat.S_IMODE(source.stat().st_mode) & ~0o022)
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
        self._restore_directories(self.directory_entries)
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
        transaction.directory_entries = [
            DirectoryEntry(**entry) for entry in payload.get("directories", [])
        ]
        transaction.normalize_modes = bool(payload.get("normalize_modes", False))
        if payload.get("state") == "restored":
            raise DeploymentTransactionError(f"deploy manifest is already restored: {manifest_path}")
        transaction._restore_entries(transaction.entries)
        transaction._restore_directories(transaction.directory_entries)
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
            self._replace_file(backup, destination)
            if sha256_file(destination) != entry.previous_sha256:
                raise DeploymentTransactionError(f"restored checksum mismatch: {destination}")

    def _prepare_destination_parent(self, destination: Path) -> None:
        parent = destination.parent
        if not self.normalize_modes:
            parent.mkdir(parents=True, exist_ok=True)
            return

        root = max(
            (root for root in self.roots if parent == root or root in parent.parents),
            key=lambda path: len(path.parts),
        )
        root.mkdir(parents=True, exist_ok=True)
        current = root
        for part in parent.relative_to(root).parts:
            current /= part
            if current.exists():
                if not current.is_dir():
                    raise DeploymentTransactionError(
                        f"deploy destination parent is not a directory: {current}"
                    )
                previous_mode = stat.S_IMODE(current.stat().st_mode)
            else:
                current.mkdir()
                previous_mode = None
            deployed_mode = (previous_mode if previous_mode is not None else 0o755) & ~0o022
            if previous_mode != deployed_mode:
                if not any(entry.path == str(current) for entry in self.directory_entries):
                    self.directory_entries.append(
                        DirectoryEntry(str(current), previous_mode, deployed_mode)
                    )
                os.chmod(current, deployed_mode)
        self._write("active")

    def _restore_directories(self, entries: list[DirectoryEntry]) -> None:
        for entry in reversed(entries):
            path = Path(entry.path)
            if not path.exists():
                if entry.previous_mode is None:
                    continue
                raise DeploymentTransactionError(f"deploy directory disappeared: {path}")
            if not path.is_dir():
                raise DeploymentTransactionError(f"deploy directory is no longer a directory: {path}")
            current_mode = stat.S_IMODE(path.stat().st_mode)
            if current_mode != entry.deployed_mode:
                raise DeploymentTransactionError(
                    f"refusing to restore changed deploy directory: {path}"
                )
            if entry.previous_mode is None:
                try:
                    path.rmdir()
                except OSError:
                    # Prefix provisioning may add required runtime children after
                    # deployment; preserve the populated directory safely.
                    continue
            else:
                os.chmod(path, entry.previous_mode)

    def _require_destination(self, destination: Path) -> None:
        if not any(destination == root or root in destination.parents for root in self.roots):
            raise DeploymentTransactionError(
                f"deploy manifest destination escapes allowed prefixes: {destination}"
            )

    @staticmethod
    def _replace_file(source: Path, destination: Path) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.deploy-", dir=destination.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary)
        try:
            shutil.copy2(source, temporary_path)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _write(self, state: str) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "state": state,
            "prefix": str(self.prefix),
            "roots": [str(root) for root in self.roots],
            "entries": [asdict(entry) for entry in self.entries],
            "directories": [asdict(entry) for entry in self.directory_entries],
            "normalize_modes": self.normalize_modes,
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

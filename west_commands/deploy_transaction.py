"""Transactional deployment records for focused Darling runtime experiments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
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
    deployed_device: int | None = None
    deployed_inode: int | None = None


@dataclass(frozen=True)
class DirectoryEntry:
    path: str
    previous_mode: int | None
    deployed_mode: int


RUNTIME_LOWER_BINDING_NAME = ".darling-runtime-lower-binding-v1"
RUNTIME_LOWER_BINDING_MAX_BYTES = 2048
PREFIX_STATE_NAMES = (".darling-prefix-state-v3", ".darling-prefix-state-v2")


def runtime_prefix_generation(prefix: Path) -> int:
    """Read one exact typed generation through a retained prefix capability."""

    prefix_fd = os.open(
        prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        prefix_stat = os.fstat(prefix_fd)
        for name in PREFIX_STATE_NAMES:
            try:
                state_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=prefix_fd,
                )
            except FileNotFoundError:
                continue
            try:
                opened = os.fstat(state_fd)
                named = os.stat(name, dir_fd=prefix_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or (opened.st_uid, opened.st_gid)
                    != (prefix_stat.st_uid, prefix_stat.st_gid)
                    or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                ):
                    raise DeploymentTransactionError("typed prefix state identity mismatch")
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = os.read(state_fd, 256)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > RUNTIME_LOWER_BINDING_MAX_BYTES:
                        raise DeploymentTransactionError("typed prefix state exceeds budget")
                    chunks.append(chunk)
                lines = b"".join(chunks).decode("utf-8").splitlines()
                expected = (
                    "DARLING_PREFIX_STATE_V3"
                    if name.endswith("v3")
                    else "DARLING_PREFIX_STATE_V2"
                )
                if not lines or lines[0] != expected:
                    raise DeploymentTransactionError("typed prefix state schema mismatch")
                values = [line for line in lines if line.startswith("generation=")]
                if len(values) != 1:
                    raise DeploymentTransactionError("typed prefix generation is ambiguous")
                generation = int(values[0].split("=", 1)[1])
                if generation <= 0:
                    raise DeploymentTransactionError("typed prefix generation is invalid")
                return generation
            except (UnicodeError, ValueError) as error:
                raise DeploymentTransactionError("typed prefix generation is malformed") from error
            finally:
                os.close(state_fd)
        raise DeploymentTransactionError("typed prefix state is missing")
    finally:
        os.close(prefix_fd)


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
        self.transaction_id = uuid.uuid4().hex
        self.runtime_lower_binding: dict[str, object] | None = None
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
            deployed_device=destination.stat().st_dev,
            deployed_inode=destination.stat().st_ino,
        )
        self.entries.append(entry)
        self._write("active")

    def bind_runtime_lower_root(
        self,
        *,
        prefix_generation: int,
        destination: str = "libexec/darling",
        controller_destination: str = "bin/darlingserver",
    ) -> Path:
        """Publish the exact deployed lower-root authority for Rust.

        Both objects are acquired from the retained prefix descriptor.  The
        pathname strings are only bounded relative labels in the durable
        record; no absolute source/install-root path becomes authority.
        """

        if self.runtime_lower_binding is not None:
            raise DeploymentTransactionError("runtime lower binding already published")
        if prefix_generation <= 0:
            raise DeploymentTransactionError("runtime lower binding generation is invalid")
        prefix_fd = os.open(
            self.prefix,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            prefix_stat = os.fstat(prefix_fd)
            lower_fd = self._open_relative_directory(prefix_fd, destination)
            try:
                lower_stat = os.fstat(lower_fd)
                controller_fd = self._open_relative_file(prefix_fd, controller_destination)
                try:
                    controller_stat = os.fstat(controller_fd)
                finally:
                    os.close(controller_fd)
            finally:
                os.close(lower_fd)
        finally:
            os.close(prefix_fd)
        fields = {
            "schema_version": 1,
            "transaction_id": self.transaction_id,
            "prefix_generation": prefix_generation,
            "destination": destination,
            "prefix_device": prefix_stat.st_dev,
            "prefix_inode": prefix_stat.st_ino,
            "lower_device": lower_stat.st_dev,
            "lower_inode": lower_stat.st_ino,
            "lower_type": "directory",
            "lower_mode": stat.S_IMODE(lower_stat.st_mode),
            "lower_uid": lower_stat.st_uid,
            "lower_gid": lower_stat.st_gid,
            "controller_destination": controller_destination,
            "controller_device": controller_stat.st_dev,
            "controller_inode": controller_stat.st_ino,
            "controller_type": "regular",
            "controller_mode": stat.S_IMODE(controller_stat.st_mode),
            "controller_uid": controller_stat.st_uid,
            "controller_gid": controller_stat.st_gid,
            "provenance": "product-deployment-transaction-v2",
        }
        lines = ["DARLING_RUNTIME_LOWER_BINDING_V1"]
        lines.extend(f"{key}={value}" for key, value in fields.items())
        content = ("\n".join(lines) + "\n").encode()
        if len(content) > RUNTIME_LOWER_BINDING_MAX_BYTES:
            raise DeploymentTransactionError("runtime lower binding exceeds transport budget")
        with tempfile.NamedTemporaryFile(
            prefix="runtime-lower-binding-", dir=self.manifest_path.parent, delete=False
        ) as handle:
            handle.write(content)
            source = Path(handle.name)
        try:
            source.chmod(0o600)
            target = self.prefix / RUNTIME_LOWER_BINDING_NAME
            self._replace_runtime_binding(
                source=source,
                destination=target,
                expected_prefix=(prefix_stat.st_dev, prefix_stat.st_ino),
            )
        finally:
            source.unlink(missing_ok=True)
        self.runtime_lower_binding = fields
        self._write("active")
        return target

    def _replace_runtime_binding(
        self,
        *,
        source: Path,
        destination: Path,
        expected_prefix: tuple[int, int],
    ) -> None:
        """Install the direct-child binding through a retained prefix FD."""

        retained = os.open(
            self.prefix,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        temporary_name = f".{RUNTIME_LOWER_BINDING_NAME}.{self.transaction_id}.new"
        backup = None
        previous_sha256 = None
        old_fd = None
        deployed_sha256 = sha256_file(source)
        try:
            retained_stat = os.fstat(retained)
            if (retained_stat.st_dev, retained_stat.st_ino) != expected_prefix:
                raise DeploymentTransactionError("runtime prefix changed before binding publish")
            try:
                old_fd = os.open(
                    RUNTIME_LOWER_BINDING_NAME,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=retained,
                )
            except FileNotFoundError:
                pass
            if old_fd is not None:
                old_stat = os.fstat(old_fd)
                named = os.stat(
                    RUNTIME_LOWER_BINDING_NAME,
                    dir_fd=retained,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(old_stat.st_mode) or (
                    old_stat.st_dev,
                    old_stat.st_ino,
                ) != (named.st_dev, named.st_ino):
                    raise DeploymentTransactionError("changed runtime lower binding")
                backup = self.backup_root / str(len(self.entries))
                backup.parent.mkdir(parents=True, exist_ok=True)
                with backup.open("wb") as output:
                    os.lseek(old_fd, 0, os.SEEK_SET)
                    while chunk := os.read(old_fd, 1024 * 1024):
                        output.write(chunk)
                os.chmod(backup, stat.S_IMODE(old_stat.st_mode))
                previous_sha256 = sha256_file(backup)
            new_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=retained,
            )
            try:
                with source.open("rb") as input_handle:
                    while chunk := input_handle.read(1024 * 1024):
                        view = memoryview(chunk)
                        while view:
                            written = os.write(new_fd, view)
                            view = view[written:]
                os.fchmod(new_fd, 0o600)
                os.fsync(new_fd)
                deployed = os.fstat(new_fd)
            finally:
                os.close(new_fd)
            os.rename(
                temporary_name,
                RUNTIME_LOWER_BINDING_NAME,
                src_dir_fd=retained,
                dst_dir_fd=retained,
            )
            # From this point the namespace has changed.  Register exact
            # ownership immediately, without another fallible pathname open,
            # so every later exception is rollback-visible.
            self.entries.append(
                DeploymentEntry(
                    destination=str(destination),
                    backup=str(backup) if backup is not None else None,
                    previous_sha256=previous_sha256,
                    deployed_sha256=deployed_sha256,
                    deployed_device=deployed.st_dev,
                    deployed_inode=deployed.st_ino,
                )
            )
            self._write("active")
        finally:
            if old_fd is not None:
                os.close(old_fd)
            try:
                os.unlink(temporary_name, dir_fd=retained)
            except FileNotFoundError:
                pass
            os.close(retained)

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
        if payload.get("version") not in (1, 2):
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
        transaction.transaction_id = str(payload.get("transaction_id", "legacy-v1"))
        transaction.runtime_lower_binding = payload.get("runtime_lower_binding")
        if payload.get("state") == "restored":
            raise DeploymentTransactionError(f"deploy manifest is already restored: {manifest_path}")
        transaction._restore_entries(transaction.entries)
        transaction._restore_directories(transaction.directory_entries)
        transaction._write("restored")

    def _restore_entries(self, entries: list[DeploymentEntry]) -> None:
        for entry in reversed(entries):
            destination = Path(entry.destination)
            self._require_destination(destination)
            observed = destination.stat() if destination.is_file() else None
            if (
                observed is None
                or sha256_file(destination) != entry.deployed_sha256
                or (entry.deployed_device is not None and observed.st_dev != entry.deployed_device)
                or (entry.deployed_inode is not None and observed.st_ino != entry.deployed_inode)
            ):
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
    def _relative_parts(value: str) -> tuple[str, ...]:
        path = Path(value)
        if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
            raise DeploymentTransactionError(f"invalid deployment-relative path: {value}")
        return path.parts

    @classmethod
    def _open_relative_directory(cls, prefix_fd: int, value: str) -> int:
        current = os.dup(prefix_fd)
        try:
            for part in cls._relative_parts(value):
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current,
                )
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                opened = os.fstat(next_fd)
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    os.close(next_fd)
                    raise DeploymentTransactionError("runtime lower directory changed during acquisition")
                os.close(current)
                current = next_fd
            return current
        except BaseException:
            os.close(current)
            raise

    @classmethod
    def _open_relative_file(cls, prefix_fd: int, value: str) -> int:
        parts = cls._relative_parts(value)
        parent = os.dup(prefix_fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent,
                )
                os.close(parent)
                parent = next_fd
            result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(result)
            if not stat.S_ISREG(opened.st_mode) or (named.st_dev, named.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                os.close(result)
                raise DeploymentTransactionError("deployed controller identity mismatch")
            return result
        finally:
            os.close(parent)

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
            "version": 2,
            "state": state,
            "prefix": str(self.prefix),
            "roots": [str(root) for root in self.roots],
            "entries": [asdict(entry) for entry in self.entries],
            "directories": [asdict(entry) for entry in self.directory_entries],
            "normalize_modes": self.normalize_modes,
            "transaction_id": self.transaction_id,
            "runtime_lower_binding": self.runtime_lower_binding,
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

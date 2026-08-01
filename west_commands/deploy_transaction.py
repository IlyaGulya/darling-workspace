"""Transactional deployment records for focused Darling runtime experiments."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

try:
    from .test_prefix import RetainedDirectoryCapability
except ImportError:
    from test_prefix import RetainedDirectoryCapability


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


@dataclass(frozen=True)
class RootIdentity:
    path: str
    device: int
    inode: int


def _absolute(path: Path) -> Path:
    """Return a lexical absolute path without following any component."""

    return Path(os.path.abspath(os.fspath(path)))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _copy_xattrs(source_fd: int, destination_fd: int) -> None:
    try:
        names = os.listxattr(source_fd)
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM}:
            return
        raise
    for name in names:
        try:
            os.setxattr(destination_fd, name, os.getxattr(source_fd, name))
        except OSError as error:
            if error.errno not in {
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
                errno.ENODATA,
                errno.EINVAL,
                errno.EPERM,
            }:
                raise


def _copy_fd_contents(source_fd: int, destination_fd: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    os.lseek(destination_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            view = view[written:]


def _copy_fd_to_path(source_fd: int, destination: Path) -> None:
    source_status = os.fstat(source_fd)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        stat.S_IMODE(source_status.st_mode),
    )
    try:
        _copy_fd_contents(source_fd, destination_fd)
        os.fchmod(destination_fd, stat.S_IMODE(source_status.st_mode))
        _copy_xattrs(source_fd, destination_fd)
        os.utime(
            destination_fd,
            ns=(source_status.st_atime_ns, source_status.st_mtime_ns),
        )
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)


class _RetainedParent:
    """One fd-relative destination parent beneath a retained transaction root."""

    def __init__(
        self,
        root: RetainedDirectoryCapability,
        root_path: Path,
        descriptors: list[int],
        names: list[str],
        statuses: list[os.stat_result],
        leaf: str,
    ):
        self.root = root
        self.root_path = root_path
        self.descriptors = descriptors
        self.names = names
        self.statuses = statuses
        self.leaf = leaf

    @property
    def fd(self) -> int:
        return self.descriptors[-1]

    def revalidate(self, *, named_root: bool) -> None:
        if named_root:
            self.root.revalidate(metadata=False)
        else:
            opened_root = os.fstat(self.root.fd)
            if not _same_identity(opened_root, self.root.initial_status):
                raise OSError("retained deploy root FD identity changed")
        for index, name in enumerate(self.names):
            parent_fd = self.descriptors[index]
            child_fd = self.descriptors[index + 1]
            initial = self.statuses[index + 1]
            opened = os.fstat(child_fd)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or not _same_identity(initial, opened)
                or not _same_identity(opened, named)
            ):
                raise OSError(
                    f"retained deploy parent identity changed: {self.root_path / Path(*self.names[:index + 1])}"
                )

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)
        self.descriptors = []

    def __enter__(self) -> "_RetainedParent":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()


class DeploymentTransaction:
    """Record and restore file replacements through retained root capabilities."""

    MANIFEST_VERSION = 2

    def __init__(
        self,
        manifest_path: Path,
        prefix: Path,
        additional_prefixes: list[Path] | None = None,
        *,
        normalize_modes: bool = False,
        expected_root_identity: tuple[int, int] | None = None,
    ):
        self.manifest_path = _absolute(manifest_path)
        self.prefix = _absolute(prefix)
        self.roots = (
            self.prefix,
            *(_absolute(path) for path in additional_prefixes or []),
        )
        self.backup_root = self.manifest_path.parent / f"{self.manifest_path.name}.backups"
        self.entries: list[DeploymentEntry] = []
        self.directory_entries: list[DirectoryEntry] = []
        self.normalize_modes = normalize_modes
        self._allow_detached_restore = True
        self._root_capabilities: dict[Path, RetainedDirectoryCapability] = {}
        if self.manifest_path.exists():
            raise DeploymentTransactionError(
                f"deploy manifest already exists: {self.manifest_path}; restore or remove it first"
            )
        if expected_root_identity is not None and (
            len(expected_root_identity) != 2
            or not all(isinstance(value, int) for value in expected_root_identity)
        ):
            raise DeploymentTransactionError("invalid expected deploy root identity")
        try:
            for root in self.roots:
                capability = RetainedDirectoryCapability.open(root)
                if root == self.prefix and expected_root_identity is not None:
                    opened = capability.revalidate(metadata=False)
                    if (opened.st_dev, opened.st_ino) != expected_root_identity:
                        capability.close()
                        raise DeploymentTransactionError(
                            "deploy root does not match retained lifecycle identity: "
                            f"{root}"
                        )
                self._root_capabilities[root] = capability
            self._root_identities = tuple(
                RootIdentity(
                    str(root),
                    self._root_capabilities[root].initial_status.st_dev,
                    self._root_capabilities[root].initial_status.st_ino,
                )
                for root in self.roots
            )
            self._write("active")
        except BaseException:
            self._close_capabilities()
            raise

    def replace(self, source: Path, destination: Path) -> None:
        source = Path(source).resolve()
        destination = _absolute(destination)
        self._require_destination(destination)
        try:
            source_fd = os.open(
                source,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise DeploymentTransactionError(
                f"deploy source is not a regular file: {source}: {error}"
            ) from error
        try:
            source_status = os.fstat(source_fd)
            if not stat.S_ISREG(source_status.st_mode):
                raise DeploymentTransactionError(
                    f"deploy source is not a regular file: {source}"
                )
            if any(Path(entry.destination) == destination for entry in self.entries):
                raise DeploymentTransactionError(
                    f"duplicate deploy destination: {destination}"
                )
            with self._open_parent(destination, create=True) as parent:
                parent.revalidate(named_root=True)
                backup = None
                previous_sha256 = None
                previous = self._open_regular_child(parent, required=False)
                if previous is not None:
                    try:
                        previous_sha256 = _sha256_fd(previous)
                        backup = self.backup_root / str(len(self.entries))
                        _copy_fd_to_path(previous, backup)
                    finally:
                        os.close(previous)
                parent.revalidate(named_root=True)
                self._replace_file_at(source_fd, source_status, parent, destination.name)
                deployed_sha256 = _sha256_fd(source_fd)
                entry = DeploymentEntry(
                    destination=str(destination),
                    backup=str(backup) if backup is not None else None,
                    previous_sha256=previous_sha256,
                    deployed_sha256=deployed_sha256,
                )
                # Record the published destination before any further named
                # revalidation can fail. Immediate rollback can then remove or
                # restore the retained-root target without touching a swapped
                # replacement path.
                self.entries.append(entry)
                self._write("active")
                parent.revalidate(named_root=True)
                deployed_fd = self._open_regular_child(parent, required=True)
                try:
                    if _sha256_fd(deployed_fd) != deployed_sha256:
                        raise DeploymentTransactionError(
                            f"deployed checksum mismatch: {destination}"
                        )
                finally:
                    os.close(deployed_fd)
        except DeploymentTransactionError:
            raise
        except OSError as error:
            raise DeploymentTransactionError(
                f"fd-relative deploy failed for {destination}: {error}"
            ) from error
        finally:
            os.close(source_fd)

    def commit(self) -> None:
        self._write("committed")
        self._close_capabilities()

    def rollback(self) -> None:
        try:
            self._restore_entries(self.entries)
            self._restore_directories(self.directory_entries)
            self._write("restored")
        finally:
            self._close_capabilities()

    @classmethod
    def manifest_roots(cls, manifest_path: Path, prefix: Path) -> tuple[Path, ...]:
        manifest_path = _absolute(manifest_path)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        roots, identities = cls._validated_manifest_roots(payload, manifest_path, prefix)
        capabilities = []
        try:
            for root, identity in zip(roots, identities, strict=True):
                capability = RetainedDirectoryCapability.open(root)
                capabilities.append(capability)
                status = capability.revalidate(metadata=False)
                if status.st_dev != identity.device or status.st_ino != identity.inode:
                    raise DeploymentTransactionError(
                        f"deploy root identity changed since transaction: {root}"
                    )
            return roots
        finally:
            for capability in reversed(capabilities):
                capability.close()

    @classmethod
    def restore(cls, manifest_path: Path, prefix: Path) -> None:
        manifest_path = _absolute(manifest_path)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        roots, identities = cls._validated_manifest_roots(payload, manifest_path, prefix)
        transaction = cls.__new__(cls)
        transaction.manifest_path = manifest_path
        transaction.prefix = _absolute(prefix)
        transaction.roots = roots
        transaction._root_identities = identities
        transaction.backup_root = manifest_path.parent / f"{manifest_path.name}.backups"
        transaction.entries = [DeploymentEntry(**entry) for entry in payload.get("entries", [])]
        transaction.directory_entries = [
            DirectoryEntry(**entry) for entry in payload.get("directories", [])
        ]
        transaction.normalize_modes = bool(payload.get("normalize_modes", False))
        transaction._allow_detached_restore = False
        transaction._root_capabilities = {}
        if payload.get("state") == "restored":
            raise DeploymentTransactionError(
                f"deploy manifest is already restored: {manifest_path}"
            )
        try:
            for root, identity in zip(roots, identities, strict=True):
                capability = RetainedDirectoryCapability.open(root)
                status = capability.revalidate(metadata=False)
                if status.st_dev != identity.device or status.st_ino != identity.inode:
                    capability.close()
                    raise DeploymentTransactionError(
                        f"deploy root identity changed since transaction: {root}"
                    )
                transaction._root_capabilities[root] = capability
            transaction._restore_entries(transaction.entries)
            transaction._restore_directories(transaction.directory_entries)
            transaction._write("restored")
        finally:
            transaction._close_capabilities()

    @classmethod
    def _validated_manifest_roots(
        cls, payload: dict, manifest_path: Path, prefix: Path
    ) -> tuple[tuple[Path, ...], tuple[RootIdentity, ...]]:
        if payload.get("version") != cls.MANIFEST_VERSION:
            raise DeploymentTransactionError(f"unsupported deploy manifest: {manifest_path}")
        prefix = _absolute(prefix)
        if _absolute(Path(str(payload.get("prefix", "")))) != prefix:
            raise DeploymentTransactionError(
                f"deploy manifest belongs to a different prefix: {manifest_path}"
            )
        roots = tuple(_absolute(Path(path)) for path in payload.get("roots", []))
        identities = tuple(
            RootIdentity(**identity) for identity in payload.get("root_identities", [])
        )
        if (
            not roots
            or roots[0] != prefix
            or len(roots) != len(identities)
            or tuple(identity.path for identity in identities)
            != tuple(str(root) for root in roots)
        ):
            raise DeploymentTransactionError(f"invalid deploy manifest roots: {manifest_path}")
        return roots, identities

    def _restore_entries(self, entries: list[DeploymentEntry]) -> None:
        for entry in reversed(entries):
            destination = _absolute(Path(entry.destination))
            self._require_destination(destination)
            try:
                with self._open_parent(
                    destination,
                    create=False,
                    named_root=not self._allow_detached_restore,
                ) as parent:
                    parent.revalidate(named_root=not self._allow_detached_restore)
                    deployed_fd = self._open_regular_child(parent, required=True)
                    try:
                        if _sha256_fd(deployed_fd) != entry.deployed_sha256:
                            raise DeploymentTransactionError(
                                f"refusing to restore changed deploy destination: {destination}"
                            )
                    finally:
                        os.close(deployed_fd)
                    parent.revalidate(named_root=not self._allow_detached_restore)
                    if entry.backup is None:
                        os.unlink(destination.name, dir_fd=parent.fd)
                        continue
                    backup = Path(entry.backup)
                    if not backup.is_file() or sha256_file(backup) != entry.previous_sha256:
                        raise DeploymentTransactionError(f"deploy backup is invalid: {backup}")
                    backup_fd = os.open(
                        backup,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    )
                    try:
                        backup_status = os.fstat(backup_fd)
                        if not stat.S_ISREG(backup_status.st_mode):
                            raise DeploymentTransactionError(
                                f"deploy backup is invalid: {backup}"
                            )
                        self._replace_file_at(
                            backup_fd,
                            backup_status,
                            parent,
                            destination.name,
                            named_root=not self._allow_detached_restore,
                            normalize_mode=False,
                        )
                    finally:
                        os.close(backup_fd)
                    restored_fd = self._open_regular_child(parent, required=True)
                    try:
                        if _sha256_fd(restored_fd) != entry.previous_sha256:
                            raise DeploymentTransactionError(
                                f"restored checksum mismatch: {destination}"
                            )
                    finally:
                        os.close(restored_fd)
            except DeploymentTransactionError:
                raise
            except OSError as error:
                raise DeploymentTransactionError(
                    f"fd-relative restore failed for {destination}: {error}"
                ) from error

    @contextmanager
    def _open_parent(
        self,
        destination: Path,
        *,
        create: bool,
        named_root: bool = True,
    ) -> Iterator[_RetainedParent]:
        root = self._root_for(destination)
        relative = destination.relative_to(root)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise DeploymentTransactionError(
                f"unsafe deploy destination below retained root: {destination}"
            )
        capability = self._root_capabilities[root]
        if named_root:
            capability.revalidate(metadata=False)
        else:
            opened_root = os.fstat(capability.fd)
            if not _same_identity(opened_root, capability.initial_status):
                raise DeploymentTransactionError(
                    f"retained deploy root FD identity changed: {root}"
                )
        descriptors = [os.dup(capability.fd)]
        names: list[str] = []
        try:
            for component in relative.parts[:-1]:
                parent_fd = descriptors[-1]
                try:
                    named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o777, dir_fd=parent_fd)
                    named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                    previous_mode = None
                else:
                    previous_mode = stat.S_IMODE(named.st_mode)
                if not stat.S_ISDIR(named.st_mode):
                    raise DeploymentTransactionError(
                        f"deploy destination parent is not a directory: {destination.parent}"
                    )
                opened = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                opened_status = os.fstat(opened)
                named_after = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not _same_identity(named, opened_status)
                    or not _same_identity(opened_status, named_after)
                ):
                    os.close(opened)
                    raise DeploymentTransactionError(
                        f"deploy destination parent changed while opening: {destination.parent}"
                    )
                descriptors.append(opened)
                names.append(component)
                if create and self.normalize_modes:
                    deployed_mode = (
                        previous_mode if previous_mode is not None else 0o755
                    ) & ~0o022
                    component_path = root / Path(*names)
                    if previous_mode != deployed_mode:
                        if not any(
                            entry.path == str(component_path)
                            for entry in self.directory_entries
                        ):
                            self.directory_entries.append(
                                DirectoryEntry(
                                    str(component_path), previous_mode, deployed_mode
                                )
                            )
                        os.fchmod(opened, deployed_mode)
            statuses = [os.fstat(descriptor) for descriptor in descriptors]
            parent = _RetainedParent(
                capability,
                root,
                descriptors,
                names,
                statuses,
                relative.parts[-1],
            )
            descriptors = []
            if create:
                self._write("active")
            try:
                yield parent
            finally:
                parent.close()
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _open_regular_child(
        self, parent: _RetainedParent, *, required: bool
    ) -> int | None:
        try:
            named_before = os.stat(
                parent.leaf,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if required:
                raise DeploymentTransactionError(
                    f"deploy destination disappeared: {parent.root_path / parent.leaf}"
                )
            return None
        if not stat.S_ISREG(named_before.st_mode):
            raise DeploymentTransactionError(
                f"deploy destination is not a regular file: {parent.root_path / parent.leaf}"
            )
        descriptor = os.open(
            parent.leaf,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent.fd,
        )
        opened = os.fstat(descriptor)
        named_after = os.stat(
            parent.leaf,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if (
            not _same_identity(named_before, opened)
            or not _same_identity(opened, named_after)
        ):
            os.close(descriptor)
            raise DeploymentTransactionError(
                f"deploy destination changed while opening: {parent.root_path / parent.leaf}"
            )
        return descriptor

    def _replace_file_at(
        self,
        source_fd: int,
        source_status: os.stat_result,
        parent: _RetainedParent,
        destination_name: str,
        *,
        named_root: bool = True,
        normalize_mode: bool | None = None,
    ) -> None:
        temporary_name = f".{destination_name}.deploy-{os.getpid()}-{id(parent):x}"
        temporary_fd = -1
        try:
            parent.revalidate(named_root=named_root)
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                stat.S_IMODE(source_status.st_mode),
                dir_fd=parent.fd,
            )
            _copy_fd_contents(source_fd, temporary_fd)
            deployed_mode = stat.S_IMODE(source_status.st_mode)
            if self.normalize_modes if normalize_mode is None else normalize_mode:
                deployed_mode &= ~0o022
            os.fchmod(temporary_fd, deployed_mode)
            _copy_xattrs(source_fd, temporary_fd)
            os.utime(
                temporary_fd,
                ns=(source_status.st_atime_ns, source_status.st_mtime_ns),
            )
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = -1
            parent.revalidate(named_root=named_root)
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=parent.fd,
                dst_dir_fd=parent.fd,
            )
            os.fsync(parent.fd)
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=parent.fd)
            except FileNotFoundError:
                pass

    def _restore_directories(self, entries: list[DirectoryEntry]) -> None:
        for entry in reversed(entries):
            path = _absolute(Path(entry.path))
            try:
                with self._open_parent(
                    path,
                    create=False,
                    named_root=not self._allow_detached_restore,
                ) as parent:
                    parent.revalidate(named_root=not self._allow_detached_restore)
                    directory_name = parent.leaf
                    directory_status = os.stat(
                        directory_name,
                        dir_fd=parent.fd,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISDIR(directory_status.st_mode):
                        raise DeploymentTransactionError(
                            f"deploy directory is no longer a directory: {path}"
                        )
                    current_mode = stat.S_IMODE(directory_status.st_mode)
                    if current_mode != entry.deployed_mode:
                        raise DeploymentTransactionError(
                            f"refusing to restore changed deploy directory: {path}"
                        )
                    if entry.previous_mode is None:
                        try:
                            os.rmdir(directory_name, dir_fd=parent.fd)
                        except OSError as error:
                            if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                                raise
                    else:
                        directory_fd = os.open(
                            directory_name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                            dir_fd=parent.fd,
                        )
                        try:
                            os.fchmod(directory_fd, entry.previous_mode)
                        finally:
                            os.close(directory_fd)
            except FileNotFoundError:
                if entry.previous_mode is not None:
                    raise DeploymentTransactionError(
                        f"deploy directory disappeared: {path}"
                    )
            except DeploymentTransactionError:
                raise
            except OSError as error:
                raise DeploymentTransactionError(
                    f"fd-relative directory restore failed for {path}: {error}"
                ) from error

    def _root_for(self, destination: Path) -> Path:
        candidates = [
            root
            for root in self.roots
            if destination != root and root in destination.parents
        ]
        if not candidates:
            raise DeploymentTransactionError(
                f"deploy manifest destination escapes allowed prefixes: {destination}"
            )
        return max(candidates, key=lambda path: len(path.parts))

    def _require_destination(self, destination: Path) -> None:
        self._root_for(destination)

    def _close_capabilities(self) -> None:
        for capability in reversed(tuple(self._root_capabilities.values())):
            capability.close()
        self._root_capabilities.clear()

    def _write(self, state: str) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.MANIFEST_VERSION,
            "state": state,
            "prefix": str(self.prefix),
            "roots": [str(root) for root in self.roots],
            "root_identities": [asdict(identity) for identity in self._root_identities],
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
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.manifest_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

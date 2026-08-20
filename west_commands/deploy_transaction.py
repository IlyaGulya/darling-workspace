"""Transactional deployment records for focused Darling runtime experiments."""

from __future__ import annotations

import hashlib
import ctypes
import errno
import fcntl
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

    def __init__(self, message: str):
        super().__init__(message)
        self.recovery_obligations: tuple[RuntimeLowerRecoveryObligation, ...] = ()
        self.recovery_owner: RuntimeLowerRecoveryOwner | None = None


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


@dataclass
class RuntimeLowerRecoveryObligation:
    """Owning fail-closed handoff for an ambiguous binding namespace object."""

    object_fd: int
    name: str
    device: int
    inode: int
    reason: str

    def close(self) -> None:
        if self.object_fd >= 0:
            os.close(self.object_fd)
            self.object_fd = -1


@dataclass
class RuntimeLowerRecoveryOwner:
    """Single idempotent owner for the lease and exact recovery objects."""

    prefix_fd: int
    lock_fd: int
    obligations: list[RuntimeLowerRecoveryObligation]

    @property
    def closed(self) -> bool:
        return self.prefix_fd < 0

    def close(self) -> None:
        if self.closed:
            return
        for obligation in self.obligations:
            obligation.close()
        fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        os.close(self.lock_fd)
        os.close(self.prefix_fd)
        self.lock_fd = -1
        self.prefix_fd = -1

    def release_lease(self) -> tuple[int, int]:
        """Resolve all objects and return the still-locked lease exactly once."""

        if self.closed:
            raise DeploymentTransactionError("runtime lower recovery owner is closed")
        for obligation in self.obligations:
            obligation.close()
        self.obligations.clear()
        prefix_fd, lock_fd = self.prefix_fd, self.lock_fd
        self.prefix_fd = -1
        self.lock_fd = -1
        return prefix_fd, lock_fd


RUNTIME_LOWER_BINDING_NAME = ".darling-runtime-lower-binding-v1"
RUNTIME_LOWER_LOCK_NAME = ".lifecycle.lock"
RUNTIME_LOWER_BINDING_MAX_BYTES = 2048
PREFIX_STATE_NAMES = (".darling-prefix-state-v3", ".darling-prefix-state-v2")
RENAME_NOREPLACE = 1


def cohort_build_enabled(build_dir: Path) -> bool:
    """Return the authoritative compile-time cohort state from CMakeCache."""

    cache = build_dir / "CMakeCache.txt"
    try:
        values = [
            line.split("=", 1)[1].strip()
            for line in cache.read_text(encoding="utf-8").splitlines()
            if line.startswith("DARLING_LIFECYCLE_COHORT_V1:BOOL=")
        ]
    except OSError as error:
        raise DeploymentTransactionError("cohort compile-time proof is unavailable") from error
    if len(values) != 1 or values[0] not in {"ON", "OFF"}:
        raise DeploymentTransactionError("cohort compile-time proof is malformed")
    return values[0] == "ON"


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)


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
        self.runtime_lower_recovery: list[RuntimeLowerRecoveryObligation] = []
        self.runtime_lower_recovery_owner: RuntimeLowerRecoveryOwner | None = None
        self._runtime_lower_prefix_fd: int | None = None
        self._runtime_lower_lock_fd: int | None = None
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
            # Keep recovery authority in memory before the first namespace
            # mutation; it is published to the manifest only by the
            # post-mutation active-state write.
            self.runtime_lower_binding = fields
            self._replace_runtime_binding(
                source=source,
                destination=target,
                expected_prefix=(prefix_stat.st_dev, prefix_stat.st_ino),
            )
        except BaseException as error:
            if self.runtime_lower_recovery_owner is None:
                self._release_runtime_lower_lease()
                raise
            self._raise_runtime_lower_recovery(
                "runtime lower binding mutation requires recovery", error
            )
        finally:
            source.unlink(missing_ok=True)
        self._write("active")
        self._release_runtime_lower_lease()
        return target

    def _replace_runtime_binding(
        self,
        *,
        source: Path,
        destination: Path,
        expected_prefix: tuple[int, int],
    ) -> None:
        """Publish through an exact retained lease and no-clobber renames."""

        retained = self._acquire_runtime_lower_lease(expected_prefix)
        temporary_name = f".{RUNTIME_LOWER_BINDING_NAME}.{self.transaction_id}.new"
        backup_name = f".{RUNTIME_LOWER_BINDING_NAME}.{self.transaction_id}.backup"
        backup = None
        previous_sha256 = None
        old_fd = None
        staged_obligation = None
        deployed_sha256 = sha256_file(source)
        try:
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
                if not stat.S_ISREG(old_stat.st_mode):
                    raise DeploymentTransactionError("runtime lower binding is not regular")
                self._runtime_lower_fault("after_existing_validation", retained, RUNTIME_LOWER_BINDING_NAME)
                self._validate_or_recover(
                    expected_prefix, retained, RUNTIME_LOWER_BINDING_NAME,
                    "lease changed before existing binding quarantine",
                )
                current_fd = os.open(
                    RUNTIME_LOWER_BINDING_NAME,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=retained,
                )
                current_stat = os.fstat(current_fd)
                if (current_stat.st_dev, current_stat.st_ino) != (
                    old_stat.st_dev,
                    old_stat.st_ino,
                ):
                    os.close(old_fd)
                    old_fd = None
                    self._retain_runtime_lower_recovery(
                        retained,
                        current_fd,
                        RUNTIME_LOWER_BINDING_NAME,
                        "binding replaced before quarantine",
                        observed=current_stat,
                    )
                    raise DeploymentTransactionError(
                        "runtime lower binding replacement preserved; recovery required"
                    )
                os.close(current_fd)
                _rename_noreplace(retained, RUNTIME_LOWER_BINDING_NAME, backup_name)
                # old_fd already owns the exact inode. Register it before the
                # first post-rename open, checksum, staging write, or fsync.
                owned_old_fd = old_fd
                old_fd = None
                self._retain_runtime_lower_recovery(
                    retained,
                    owned_old_fd,
                    backup_name,
                    "previous binding quarantined pending durable publication",
                    observed=old_stat,
                )
                self._runtime_lower_fault("after_quarantine", retained, backup_name)
                backup = self.prefix / backup_name
                previous_sha256 = sha256_file(backup)
            new_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=retained,
            )
            deployed = os.fstat(new_fd)
            self._retain_runtime_lower_recovery(
                retained,
                new_fd,
                temporary_name,
                "candidate binding staged pending durable publication",
                observed=deployed,
            )
            staged_obligation = self.runtime_lower_recovery[-1]
            try:
                with source.open("rb") as input_handle:
                    while chunk := input_handle.read(1024 * 1024):
                        view = memoryview(chunk)
                        while view:
                            self._runtime_lower_fault(
                                "before_staging_write", retained, temporary_name
                            )
                            written = os.write(new_fd, view)
                            view = view[written:]
                os.fchmod(new_fd, 0o600)
                self._runtime_lower_fault(
                    "before_staging_fsync", retained, temporary_name
                )
                os.fsync(new_fd)
                deployed = os.fstat(new_fd)
            except BaseException:
                raise
            self._runtime_lower_fault("before_publish", retained, RUNTIME_LOWER_BINDING_NAME)
            self._validate_or_recover(
                expected_prefix, retained, temporary_name, "lease changed before publication"
            )
            try:
                _rename_noreplace(retained, temporary_name, RUNTIME_LOWER_BINDING_NAME)
            except OSError as error:
                if error.errno == errno.EEXIST:
                    raise DeploymentTransactionError(
                        "runtime lower binding publish refused; recovery required"
                    ) from error
                raise
            assert staged_obligation is not None
            staged_obligation.name = RUNTIME_LOWER_BINDING_NAME
            staged_obligation.reason = "published binding pending durable manifest commit"
            self._write("recovery_required")
            self._runtime_lower_fault(
                "after_publication", retained, RUNTIME_LOWER_BINDING_NAME
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
            self._write("active", recovery_override=[])
            self._resolve_runtime_lower_recovery()
        except BaseException as error:
            if self.runtime_lower_recovery_owner is not None:
                self._raise_runtime_lower_recovery(
                    "runtime lower namespace mutation requires recovery", error
                )
            raise
        finally:
            if old_fd is not None:
                os.close(old_fd)
            if staged_obligation is None:
                try:
                    os.unlink(temporary_name, dir_fd=retained)
                except FileNotFoundError:
                    pass

    def _runtime_lower_fault(self, checkpoint: str, parent_fd: int, name: str) -> None:
        """Test seam; production has no asynchronous callback in the lease."""

    def _acquire_runtime_lower_lease(self, expected_prefix: tuple[int, int]) -> int:
        if self.runtime_lower_recovery_owner is not None:
            self._validate_runtime_lower_lease(expected_prefix)
            return self.runtime_lower_recovery_owner.prefix_fd
        if self._runtime_lower_prefix_fd is not None:
            self._validate_runtime_lower_lease(expected_prefix)
            return self._runtime_lower_prefix_fd
        prefix_fd = os.open(
            self.prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        lock_fd = None
        try:
            lock_fd = os.open(
                RUNTIME_LOWER_LOCK_NAME,
                os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=prefix_fd,
            )
            self._validate_lock_capability(prefix_fd, lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._validate_lock_capability(prefix_fd, lock_fd)
            if (os.fstat(prefix_fd).st_dev, os.fstat(prefix_fd).st_ino) != expected_prefix:
                raise DeploymentTransactionError("runtime prefix changed before binding publish")
            self._runtime_lower_prefix_fd = prefix_fd
            self._runtime_lower_lock_fd = lock_fd
            return prefix_fd
        except BaseException:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(prefix_fd)
            raise

    @staticmethod
    def _validate_lock_capability(prefix_fd: int, lock_fd: int) -> None:
        prefix = os.fstat(prefix_fd)
        opened = os.fstat(lock_fd)
        named = os.stat(
            RUNTIME_LOWER_LOCK_NAME, dir_fd=prefix_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_uid, opened.st_gid) != (prefix.st_uid, prefix.st_gid)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise DeploymentTransactionError("invalid or split runtime lifecycle lock")

    def _validate_runtime_lower_lease(self, expected_prefix: tuple[int, int]) -> None:
        if self.runtime_lower_recovery_owner is not None:
            prefix_fd = self.runtime_lower_recovery_owner.prefix_fd
            lock_fd = self.runtime_lower_recovery_owner.lock_fd
        else:
            assert self._runtime_lower_prefix_fd is not None
            assert self._runtime_lower_lock_fd is not None
            prefix_fd = self._runtime_lower_prefix_fd
            lock_fd = self._runtime_lower_lock_fd
        prefix = os.fstat(prefix_fd)
        self._validate_lock_capability(prefix_fd, lock_fd)
        if (prefix.st_dev, prefix.st_ino) != expected_prefix:
            raise DeploymentTransactionError("runtime lower lease identity mismatch")

    def _retain_runtime_lower_recovery(
        self,
        parent_fd: int,
        object_fd: int,
        name: str,
        reason: str,
        *,
        observed: os.stat_result | None = None,
    ) -> None:
        observed = observed or os.fstat(object_fd)
        if self.runtime_lower_recovery_owner is None:
            if self._runtime_lower_prefix_fd != parent_fd or self._runtime_lower_lock_fd is None:
                os.close(object_fd)
                raise DeploymentTransactionError("recovery lease ownership mismatch")
            self.runtime_lower_recovery_owner = RuntimeLowerRecoveryOwner(
                self._runtime_lower_prefix_fd, self._runtime_lower_lock_fd, []
            )
            self._runtime_lower_prefix_fd = None
            self._runtime_lower_lock_fd = None
        obligation = RuntimeLowerRecoveryObligation(
            object_fd, name, observed.st_dev, observed.st_ino, reason
        )
        self.runtime_lower_recovery.append(obligation)
        self.runtime_lower_recovery_owner.obligations.append(obligation)
        self._write("recovery_required")

    def _raise_runtime_lower_recovery(
        self, message: str, cause: BaseException
    ) -> None:
        error = DeploymentTransactionError(f"{message}: {cause}")
        error.recovery_obligations = tuple(self.runtime_lower_recovery)
        error.recovery_owner = self.runtime_lower_recovery_owner
        raise error from cause

    def _resolve_runtime_lower_recovery(self) -> None:
        owner = self.runtime_lower_recovery_owner
        if owner is None:
            return
        self._runtime_lower_prefix_fd, self._runtime_lower_lock_fd = owner.release_lease()
        self.runtime_lower_recovery.clear()
        self.runtime_lower_recovery_owner = None

    def commit(self) -> None:
        if self.runtime_lower_recovery:
            raise DeploymentTransactionError("runtime lower recovery obligation is unresolved")
        self._write("committed")
        self._release_runtime_lower_lease()

    def rollback(self) -> None:
        try:
            self._restore_entries(self.entries)
            self._restore_directories(self.directory_entries)
            self._write("restored", recovery_override=[])
            self._resolve_runtime_lower_recovery()
        except BaseException as error:
            if self.runtime_lower_recovery_owner is not None:
                self._raise_runtime_lower_recovery(
                    "runtime lower rollback mutation requires recovery", error
                )
            raise
        finally:
            self._release_runtime_lower_lease()

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
        transaction.runtime_lower_recovery = []
        transaction.runtime_lower_recovery_owner = None
        transaction._runtime_lower_prefix_fd = None
        transaction._runtime_lower_lock_fd = None
        if payload.get("state") == "restored":
            raise DeploymentTransactionError(f"deploy manifest is already restored: {manifest_path}")
        persisted_recovery = payload.get("runtime_lower_recovery", [])
        if persisted_recovery:
            before = manifest_path.read_bytes()
            transaction._recover_persisted_runtime_lower(persisted_recovery)
            assert manifest_path.read_bytes() == before
            error = DeploymentTransactionError(
                "runtime lower recovery obligation remains unresolved"
            )
            error.recovery_obligations = tuple(transaction.runtime_lower_recovery)
            error.recovery_owner = transaction.runtime_lower_recovery_owner
            raise error
        try:
            transaction._restore_entries(transaction.entries)
            transaction._restore_directories(transaction.directory_entries)
            transaction._write("restored", recovery_override=[])
            transaction._resolve_runtime_lower_recovery()
            transaction._release_runtime_lower_lease()
        except BaseException as cause:
            if transaction.runtime_lower_recovery_owner is not None:
                transaction._raise_runtime_lower_recovery(
                    "standalone restore mutation requires recovery", cause
                )
            transaction._release_runtime_lower_lease()
            raise

    def _recover_persisted_runtime_lower(self, records: object) -> None:
        if not isinstance(records, list) or not isinstance(self.runtime_lower_binding, dict):
            raise DeploymentTransactionError("persisted runtime lower recovery is malformed")
        expected_prefix = (
            int(self.runtime_lower_binding["prefix_device"]),
            int(self.runtime_lower_binding["prefix_inode"]),
        )
        retained = self._acquire_runtime_lower_lease(expected_prefix)
        opened: list[RuntimeLowerRecoveryObligation] = []
        try:
            for record in records:
                if not isinstance(record, dict):
                    raise DeploymentTransactionError("persisted recovery record is malformed")
                name = str(record["name"])
                if not name or "/" in name or name in {".", ".."}:
                    raise DeploymentTransactionError("persisted recovery name is invalid")
                object_fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=retained
                )
                observed = os.fstat(object_fd)
                if (observed.st_dev, observed.st_ino) != (
                    int(record["device"]),
                    int(record["inode"]),
                ):
                    os.close(object_fd)
                    raise DeploymentTransactionError("persisted recovery identity mismatch")
                opened.append(
                    RuntimeLowerRecoveryObligation(
                        object_fd,
                        name,
                        observed.st_dev,
                        observed.st_ino,
                        str(record["reason"]),
                    )
                )
            assert self._runtime_lower_prefix_fd == retained
            assert self._runtime_lower_lock_fd is not None
            self.runtime_lower_recovery = opened
            self.runtime_lower_recovery_owner = RuntimeLowerRecoveryOwner(
                retained, self._runtime_lower_lock_fd, opened
            )
            self._runtime_lower_prefix_fd = None
            self._runtime_lower_lock_fd = None
        except BaseException:
            for obligation in opened:
                obligation.close()
            self._release_runtime_lower_lease()
            raise

    def _restore_entries(self, entries: list[DeploymentEntry]) -> None:
        for entry in reversed(entries):
            destination = Path(entry.destination)
            self._require_destination(destination)
            if destination.name == RUNTIME_LOWER_BINDING_NAME and destination.parent == self.prefix:
                self._restore_runtime_lower_binding(entry)
                continue
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

    def _restore_runtime_lower_binding(self, entry: DeploymentEntry) -> None:
        binding = self.runtime_lower_binding
        if not isinstance(binding, dict):
            raise DeploymentTransactionError("runtime lower binding manifest authority is missing")
        expected_prefix = (int(binding["prefix_device"]), int(binding["prefix_inode"]))
        retained = self._acquire_runtime_lower_lease(expected_prefix)
        quarantine_name = f".{RUNTIME_LOWER_BINDING_NAME}.{self.transaction_id}.rollback"
        binding_fd = os.open(
            RUNTIME_LOWER_BINDING_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=retained,
        )
        binding_stat = os.fstat(binding_fd)
        if (binding_stat.st_dev, binding_stat.st_ino) != (
            entry.deployed_device,
            entry.deployed_inode,
        ):
            self._retain_runtime_lower_recovery(
                retained,
                binding_fd,
                RUNTIME_LOWER_BINDING_NAME,
                "rollback destination is an exact replacement",
                observed=binding_stat,
            )
            raise DeploymentTransactionError(
                "runtime lower binding replacement preserved; recovery required"
            )
        try:
            _rename_noreplace(retained, RUNTIME_LOWER_BINDING_NAME, quarantine_name)
        except OSError as error:
            os.close(binding_fd)
            raise DeploymentTransactionError("runtime lower binding is unavailable for rollback") from error
        self._retain_runtime_lower_recovery(
            retained,
            binding_fd,
            quarantine_name,
            "deployed binding quarantined pending durable restore",
            observed=binding_stat,
        )
        self._runtime_lower_fault(
            "after_restore_quarantine", retained, quarantine_name
        )

        if entry.backup is not None:
            backup_name = Path(entry.backup).name
            backup_fd = os.open(
                backup_name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=retained,
            )
            backup_stat = os.fstat(backup_fd)
            self._retain_runtime_lower_recovery(
                retained,
                backup_fd,
                backup_name,
                "previous binding retained pending durable restore",
                observed=backup_stat,
            )
            backup_obligation = self.runtime_lower_recovery[-1]
            self._runtime_lower_fault("before_backup_restore", retained, backup_name)
            self._validate_or_recover(
                expected_prefix, retained, backup_name, "lease changed before backup restore"
            )
            try:
                _rename_noreplace(retained, backup_name, RUNTIME_LOWER_BINDING_NAME)
            except OSError as error:
                raise DeploymentTransactionError(
                    "runtime lower backup restore refused; recovery required"
                ) from error
            backup_obligation.name = RUNTIME_LOWER_BINDING_NAME
            backup_obligation.reason = "previous binding restored pending durable manifest"
            self._write("recovery_required")

        check_fd = os.open(
            quarantine_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=retained
        )
        check = os.fstat(check_fd)
        os.close(check_fd)
        self._runtime_lower_fault("before_quarantine_unlink", retained, quarantine_name)
        final_fd = os.open(
            quarantine_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=retained
        )
        final = os.fstat(final_fd)
        if (check.st_dev, check.st_ino) != (final.st_dev, final.st_ino):
            self._retain_runtime_lower_recovery(
                retained, final_fd, quarantine_name, "quarantine replaced before collection"
            )
            raise DeploymentTransactionError(
                "runtime lower quarantine replacement preserved; recovery required"
            )
        os.close(final_fd)
        self._validate_runtime_lower_lease(expected_prefix)
        os.unlink(quarantine_name, dir_fd=retained)

    def _release_runtime_lower_lease(self) -> None:
        if self._runtime_lower_lock_fd is not None:
            fcntl.flock(self._runtime_lower_lock_fd, fcntl.LOCK_UN)
            os.close(self._runtime_lower_lock_fd)
            self._runtime_lower_lock_fd = None
        if self._runtime_lower_prefix_fd is not None:
            os.close(self._runtime_lower_prefix_fd)
            self._runtime_lower_prefix_fd = None

    def _validate_or_recover(
        self,
        expected_prefix: tuple[int, int],
        parent_fd: int,
        owned_name: str,
        reason: str,
    ) -> None:
        try:
            self._validate_runtime_lower_lease(expected_prefix)
        except (DeploymentTransactionError, OSError) as error:
            object_fd = os.open(
                owned_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
            )
            self._retain_runtime_lower_recovery(parent_fd, object_fd, owned_name, reason)
            raise DeploymentTransactionError(f"{reason}; recovery required") from error

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

    def _manifest_fault(self, checkpoint: str) -> None:
        """Fault-injection seam; production performs no callback here."""

    def _write(
        self,
        state: str,
        *,
        recovery_override: list[RuntimeLowerRecoveryObligation] | None = None,
    ) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        recovery = (
            self.runtime_lower_recovery
            if recovery_override is None
            else recovery_override
        )
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
            "runtime_lower_recovery": [
                {
                    "name": item.name,
                    "device": item.device,
                    "inode": item.inode,
                    "reason": item.reason,
                }
                for item in recovery
            ],
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.manifest_path.name}.", dir=self.manifest_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                self._manifest_fault("before_manifest_fsync")
                os.fsync(handle.fileno())
            self._manifest_fault("before_manifest_rename")
            os.replace(temporary, self.manifest_path)
            parent_fd = os.open(
                self.manifest_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                self._manifest_fault("before_manifest_parent_fsync")
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

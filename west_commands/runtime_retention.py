"""Retention and reference protection for owned Darling runtime images.

Only a *new* normal installation whose runtime root lies inside an explicitly
declared ``DW_STATE_ROOT`` is registered.  Everything this module writes lives
outside the immutable image: a catalog record, a persistent use-lock sidecar and
the installer's existing sibling lock.  An unregistered or legacy root is never
adopted, pinned or removed.

The default retention policy keeps the newest two published images plus every
externally pinned image.  Removal additionally requires the complete install
receipt, the registered root inode, both external locks, and a read-only census
of every process mount namespace showing no open readers, executable/mapped
image references or mounts; bind aliases are resolved through filesystem
identity.  Any failed or ambiguous inspection preserves the image and reports
why.  Retirement journals the trash path and detached inode before the rename,
so an interrupted rename or deletion is retried by a later bounded prune.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


def _deploy():
    """Import the receipt module lazily.

    The isolated read-only census child runs with ``python -I``, where the
    script directory is not on ``sys.path``; it must load this module before any
    receipt helper is needed.
    """

    try:
        from . import deploy_receipt
    except ImportError:
        try:
            import deploy_receipt  # type: ignore[no-redef]
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import deploy_receipt  # type: ignore[no-redef]
    return deploy_receipt


SCHEMA = 1
KIND = "darling-runtime-image"
STATE_ROOT_ENV = "DW_STATE_ROOT"
REGISTRY_DIRNAME = "runtime-images"
CATALOG_DIRNAME = "catalog"
LOCKS_DIRNAME = "locks"
TRASH_DIRNAME = "trash"
CATALOG_LOCK_NAME = ".catalog.lock"
RECORD_SUFFIX = ".json"

STATE_INSTALLING = "installing"
STATE_PUBLISHED = "published"
STATE_FAILED = "failed"
STATE_RETIRING = "retiring"
STATES = frozenset({STATE_INSTALLING, STATE_PUBLISHED, STATE_FAILED, STATE_RETIRING})

DEFAULT_KEEP_LAST = 2
DEFAULT_FREE_FLOOR = 30 * 1024**3
INSTALL_LOCK_TEMPLATE = ".{name}.west-install.lock"
USABLE_STATES = frozenset({STATE_PUBLISHED, STATE_RETIRING})

#: Read-only native inspection roots.  Tests replace these to prove that a
#: missing or denied census preserves an image instead of guessing.
PROC_ROOT = Path("/proc")
MOUNTINFO_PATH = Path("/proc/self/mountinfo")

_KEY_LIMIT = 4096

__all__ = [
    "DEFAULT_FREE_FLOOR",
    "DEFAULT_KEEP_LAST",
    "RuntimeRetentionError",
    "STATE_ROOT_ENV",
    "begin_publication",
    "complete_publication",
    "declared_state_root",
    "fail_publication",
    "install_lock_path",
    "leased_runtime",
    "pin_runtime",
    "prune_runtimes",
    "registered_runtime",
    "require_publication_budget",
    "require_reserve",
    "reserve_check",
    "runtime_use",
    "unpin_runtime",
]


class RuntimeRetentionError(ValueError):
    """A registered image is not in a state that safely permits this operation."""


# --------------------------------------------------------------------------
# Small filesystem/path helpers.
# --------------------------------------------------------------------------


def _absolute(path: str | Path) -> Path:
    value = Path(path).expanduser()
    return Path(os.path.abspath(os.fspath(value)))


def _canonical(path: str | Path) -> Path:
    return Path(os.path.realpath(os.fspath(_absolute(path))))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _record_id(root: Path) -> str:
    return hashlib.sha256(os.fsencode(str(root))).hexdigest()


def _sha256_file(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(directory: Path) -> None:
    """Create or validate an owned, non-symlinked metadata directory."""

    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        directory.mkdir(parents=True, mode=0o700)
        metadata = directory.lstat()
    except OSError as error:
        raise RuntimeRetentionError(f"cannot inspect metadata directory {directory}: {error}") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise RuntimeRetentionError(
            f"metadata directory is not an owned real directory: {directory}"
        )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _ensure_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _tree_bytes(path: Path) -> int:
    total = 0

    def fail(error: OSError) -> None:
        raise RuntimeRetentionError(f"cannot enumerate retained image {path}: {error}") from error

    for directory, folders, files in os.walk(path, followlinks=False, onerror=fail):
        folders[:] = [
            name for name in folders if not (Path(directory) / name).is_symlink()
        ]
        for name in files:
            child = Path(directory) / name
            try:
                metadata = child.lstat()
            except OSError as error:
                raise RuntimeRetentionError(f"cannot size retained image {child}: {error}") from error
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
    return total


def install_lock_path(root: str | Path) -> Path:
    """The installer's persistent sibling lock, never placed inside the image."""

    target = _canonical(root)
    return target.with_name(INSTALL_LOCK_TEMPLATE.format(name=target.name))


def declared_state_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """The explicitly declared owner root, or ``None`` when it is undeclared."""

    source = os.environ if environ is None else environ
    value = str(source.get(STATE_ROOT_ENV, "") or "").strip()
    if not value:
        return None
    return _canonical(value)


def _registry(state_root: Path) -> Path:
    return state_root / REGISTRY_DIRNAME


def _catalog(state_root: Path) -> Path:
    return _registry(state_root) / CATALOG_DIRNAME


def _locks(state_root: Path) -> Path:
    return _registry(state_root) / LOCKS_DIRNAME


def _trash(state_root: Path) -> Path:
    return _registry(state_root) / TRASH_DIRNAME


def _record_file(state_root: Path, root: Path) -> Path:
    return _catalog(state_root) / f"{_record_id(root)}{RECORD_SUFFIX}"


def _use_lock(state_root: Path, root: Path) -> Path:
    return _locks(state_root) / f"{_record_id(root)}.use.lock"


@contextmanager
def _registry_lock(state_root: Path) -> Iterator[None]:
    registry = _registry(state_root)
    _ensure_directory(registry)
    try:
        descriptor = os.open(
            registry / CATALOG_LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as error:
        raise RuntimeRetentionError(
            f"cannot open runtime catalog lock {registry / CATALOG_LOCK_NAME}: {error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeRetentionError(
                f"runtime catalog lock is not an owned regular file: {registry / CATALOG_LOCK_NAME}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


# --------------------------------------------------------------------------
# Record parsing and locality.
# --------------------------------------------------------------------------


def _valid_record(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("schema") != SCHEMA or value.get("kind") != KIND:
        return False
    if value.get("state") not in STATES:
        return False
    root = value.get("root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        return False
    device, inode = value.get("device"), value.get("inode")
    if device is not None and (type(device) is not int or device < 0):
        return False
    if inode is not None and (type(inode) is not int or inode < 1):
        return False
    if value.get("state") in (STATE_PUBLISHED, STATE_RETIRING):
        if type(device) is not int or type(inode) is not int:
            return False
    pins = value.get("pins")
    if pins is not None and not isinstance(pins, dict):
        return False
    if pins is not None and not all(
        isinstance(key, str) and key and "\0" not in key for key in pins
    ):
        return False
    sequence = value.get("published_sequence")
    if sequence is not None and (type(sequence) is not int or sequence < 1):
        return False
    return True


def _parse_record(path: Path) -> dict[str, Any] | None:
    try:
        value = _read_json(path)
    except (OSError, ValueError):
        return None
    return value if _valid_record(value) else None


def _locate_record(root: Path) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    """Locate a registration record by walking the declared-root ancestors.

    Ancestor discovery keeps a previously managed image protected even when a
    caller forgot to export ``DW_STATE_ROOT`` for this invocation.  A record that
    exists but cannot be parsed is reported as an error, never treated as absent.
    """

    identifier = _record_id(root)
    for ancestor in (root, *root.parents):
        if ancestor == ancestor.parent:
            break
        catalog = ancestor / REGISTRY_DIRNAME / CATALOG_DIRNAME
        if catalog.is_symlink():
            return None, catalog / f"{identifier}{RECORD_SUFFIX}", f"runtime catalog is a symlink: {catalog}"
        if not catalog.is_dir():
            continue
        candidate = catalog / f"{identifier}{RECORD_SUFFIX}"
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            return None, candidate, f"cannot inspect runtime registration: {error}"
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return None, candidate, f"runtime registration is not a regular file: {candidate}"
        record = _parse_record(candidate)
        if record is None:
            return None, candidate, f"runtime registration is unreadable: {candidate}"
        if record.get("root") != str(root):
            return None, candidate, f"runtime registration names another root: {candidate}"
        return record, candidate, None
    return None, None, None


def _locate_spelling(raw: str | Path) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    """Locate a registration for either the spelled or canonical path.

    A registered image replaced by a symlink keeps the old record under the
    original spelled path.  Resolving first would miss it and wrongly treat the
    image as unmanaged, so the spelled path is checked before the target.
    """

    spelled = _absolute(raw)
    canonical = _canonical(raw)
    for candidate in (spelled, canonical) if spelled != canonical else (canonical,):
        record, record_file, error = _locate_record(candidate)
        if error is not None:
            return None, record_file, error
        if record is not None:
            if candidate != canonical:
                return (
                    record,
                    record_file,
                    f"registered runtime is addressed through a symlinked path: {spelled}",
                )
            return record, record_file, None
    return None, None, None


def _state_root_of(record_file: Path) -> Path | None:
    if record_file.parent.name != CATALOG_DIRNAME:
        return None
    registry = record_file.parent.parent
    if registry.name != REGISTRY_DIRNAME:
        return None
    return registry.parent


# --------------------------------------------------------------------------
# Image identity and integrity.
# --------------------------------------------------------------------------


def _recorded_paths(record: Mapping[str, Any]) -> list[Path]:
    values = []
    for key in ("source_directory", "build_directory", "manifest_directory"):
        value = record.get(key)
        if isinstance(value, str) and value and Path(value).is_absolute():
            values.append(_canonical(value))
    return values


def _image_problems(
    record: Mapping[str, Any], root: Path, *, full_receipt: bool
) -> list[str]:
    """Problems that make a registered image unusable or unsafe to remove."""

    problems: list[str] = []
    if record.get("root") != str(root):
        problems.append("registration names a different runtime root")
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        problems.append("registered runtime root is missing")
        return problems
    except OSError as error:
        problems.append(f"cannot inspect registered runtime root: {error}")
        return problems
    if stat.S_ISLNK(metadata.st_mode):
        problems.append("registered runtime root is a symlink")
    elif not stat.S_ISDIR(metadata.st_mode):
        problems.append("registered runtime root is not a directory")
    elif (
        type(record.get("device")) is int
        and type(record.get("inode")) is int
        and (metadata.st_dev, metadata.st_ino) != (record.get("device"), record.get("inode"))
    ):
        problems.append("registered runtime root was replaced (device/inode changed)")
    if metadata.st_uid != os.getuid():
        problems.append("registered runtime root belongs to another owner")

    for recorded in _recorded_paths(record):
        if _overlaps(root, recorded):
            problems.append(f"registration overlaps a source/build/manifest root: {recorded}")
    if (root / ".git").exists():
        problems.append("registered runtime root is a Git worktree root")

    receipt = record.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("sha256"), str):
        problems.append("registration has no published receipt identity")
        return problems
    path = _deploy().receipt_path(root)
    try:
        digest = _sha256_file(path)
    except FileNotFoundError:
        problems.append("published install receipt is missing")
        return problems
    except OSError as error:
        problems.append(f"cannot read published install receipt: {error}")
        return problems
    if digest != receipt.get("sha256"):
        problems.append("published install receipt changed after registration")
        return problems
    try:
        payload = _deploy().read_receipt(root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        problems.append(f"published install receipt is invalid: {error}")
        return problems
    if payload is None:
        problems.append("published install receipt became absent")
        return problems
    if payload.get("runtime_root") != str(root):
        problems.append("published install receipt names another root")
        return problems
    for key in ("source", "build", "workspace"):
        recorded = _record_directory(payload, key)
        if recorded is not None and _overlaps(root, recorded):
            problems.append(
                f"published install receipt overlaps a {key} root: {recorded}"
            )
    if full_receipt:
        receipt_problems, _ = _deploy().verify_receipt(payload)
        problems.extend(problem.message for problem in receipt_problems)
    return problems


# --------------------------------------------------------------------------
# Registration lifecycle (called by runtime_install).
# --------------------------------------------------------------------------


def _forbidden_overlap(root: Path, paths: list[Path | None]) -> Path | None:
    for path in paths:
        if path is None:
            continue
        if _overlaps(root, _canonical(path)):
            return _canonical(path)
    return None


def _validate_new_root(
    root: Path,
    state_root: Path,
    *,
    source_directory: Path | None,
    build_directory: Path | None,
    manifest_directory: Path | None,
) -> None:
    if root == state_root:
        raise RuntimeRetentionError("the declared state root cannot itself be a runtime image")
    registry = _registry(state_root)
    if _overlaps(root, registry):
        raise RuntimeRetentionError(
            f"runtime image {root} overlaps the retention catalog {registry}"
        )
    overlap = _forbidden_overlap(
        root, [source_directory, build_directory, manifest_directory]
    )
    if overlap is not None:
        raise RuntimeRetentionError(
            f"runtime image {root} overlaps a source/build/manifest root: {overlap}"
        )
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise RuntimeRetentionError(f"cannot inspect runtime destination {root}: {error}") from error
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeRetentionError(f"runtime destination must be a real directory: {root}")
        if metadata.st_uid != os.getuid():
            raise RuntimeRetentionError(f"runtime destination belongs to another owner: {root}")
        if (root / ".git").exists():
            raise RuntimeRetentionError(f"runtime destination is a Git worktree root: {root}")
    current = root
    while current != state_root and state_root in current.parents:
        current = current.parent
        if current == state_root:
            break
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeRetentionError(f"cannot inspect runtime parent {current}: {error}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeRetentionError(f"runtime destination has a non-directory ancestor: {current}")


def begin_publication(
    root: str | Path,
    *,
    build_directory: str | Path | None = None,
    manifest_directory: str | Path | None = None,
    source_directory: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Record ownership before a normal installation populates ``root``.

    Returns the installing record when the destination is inside the declared
    state root, or ``None`` when this is a legacy/unmanaged publication (whose
    behaviour must remain unchanged).  Callers hold the installer lock.
    """

    state_root = declared_state_root(environ)
    if state_root is None:
        return None
    if _absolute(root).is_symlink():
        raise RuntimeRetentionError(f"managed runtime root must not be a symlink: {root}")
    target = _canonical(root)
    if not _inside(target, state_root):
        return None
    source = _canonical(source_directory) if source_directory is not None else None
    build = _canonical(build_directory) if build_directory is not None else None
    manifest = _canonical(manifest_directory) if manifest_directory is not None else None
    _validate_new_root(
        target,
        state_root,
        source_directory=source,
        build_directory=build,
        manifest_directory=manifest,
    )
    with _registry_lock(state_root):
        record_file = _record_file(state_root, target)
        existing = None
        if record_file.exists():
            existing = _parse_record(record_file)
            if existing is None:
                raise RuntimeRetentionError(
                    f"existing runtime registration is unreadable: {record_file}"
                )
        if existing is not None and existing.get("state") == STATE_PUBLISHED:
            raise RuntimeRetentionError(
                f"runtime root {target} already has a published registration"
            )
        try:
            populated = target.exists() and any(target.iterdir())
        except OSError as error:
            raise RuntimeRetentionError(
                f"cannot inspect runtime destination {target}: {error}"
            ) from error
        if populated:
            raise RuntimeRetentionError(
                f"runtime destination {target} is not empty; refusing implicit adoption"
            )
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            device = inode = None
        else:
            device, inode = metadata.st_dev, metadata.st_ino
        now = _utc_now()
        record: dict[str, Any] = {
            "schema": SCHEMA,
            "kind": KIND,
            "root": str(target),
            "state": STATE_INSTALLING,
            "device": device,
            "inode": inode,
            "created": existing.get("created") if existing else now,
            "updated": now,
            "published_ns": None,
            "published_sequence": None,
            "receipt": None,
            "source_directory": str(source) if source else None,
            "build_directory": str(build) if build else None,
            "manifest_directory": str(manifest) if manifest else None,
            "pins": {},
            "error": None,
        }
        _ensure_directory(_locks(state_root))
        _ensure_lock_file(_use_lock(state_root, target))
        _write_json(record_file, record)
        return record


def complete_publication(
    root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    prune: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Mark a managed normal publication complete after its receipt exists.

    Only an existing installing record is completed; this never adopts an
    arbitrary image.  Returns ``None`` for undeclared/unmanaged roots.  A newly
    completed publication then applies the scoped bounded retention policy: the
    caller's fresh image is protected by its publication rank, and per-image
    failures are reported in the returned ``retention`` rows rather than being
    confused with the installation result.
    """

    state_root = declared_state_root(environ)
    if state_root is None:
        return None
    if _absolute(root).is_symlink():
        raise RuntimeRetentionError(f"managed runtime root must not be a symlink: {root}")
    target = _canonical(root)
    if not _inside(target, state_root):
        return None
    with _registry_lock(state_root):
        record_file = _record_file(state_root, target)
        record = _parse_record(record_file) if record_file.exists() else None
        if record is None:
            return None
        if record.get("root") != str(target):
            raise RuntimeRetentionError(f"runtime registration is inconsistent: {record_file}")
        if record.get("state") == STATE_PUBLISHED:
            return record
        receipt = _deploy().read_receipt(target)
        if receipt is None:
            raise RuntimeRetentionError(f"published runtime has no install receipt: {target}")
        receipt_digest = _sha256_file(_deploy().receipt_path(target))
        recorded_source = _record_directory(receipt, "source")
        recorded_build = _record_directory(receipt, "build")
        recorded_manifest = _record_directory(receipt, "workspace")
        _validate_new_root(
            target,
            state_root,
            source_directory=recorded_source,
            build_directory=recorded_build,
            manifest_directory=recorded_manifest,
        )
        if not install_lock_path(target).exists():
            raise RuntimeRetentionError(
                f"managed publication lost its installer lock sidecar: {install_lock_path(target)}"
            )
        metadata = target.lstat()
        now = _utc_now()
        record.update(
            state=STATE_PUBLISHED,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            updated=now,
            published_ns=time.time_ns(),
            published_sequence=_next_sequence(state_root, excluding=record_file),
            receipt={
                "path": str(_deploy().receipt_path(target)),
                "sha256": receipt_digest,
                "runtime_root": receipt.get("runtime_root"),
            },
            source_directory=str(recorded_source) if recorded_source else None,
            build_directory=str(recorded_build) if recorded_build else None,
            manifest_directory=str(recorded_manifest) if recorded_manifest else None,
            error=None,
        )
        _write_json(record_file, record)
    if not prune:
        return record
    try:
        rows = prune_runtimes(
            state_root, keep_last=DEFAULT_KEEP_LAST, progress=progress
        )
    except Exception as error:  # publication already succeeded; never relabel it
        rows = [_row(state_root, "error", f"bounded publication retention failed: {error}")]
    result = dict(record)
    result["retention"] = rows
    return result


def fail_publication(
    root: str | Path,
    error: BaseException | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Truthfully mark an interrupted managed publication as failed.

    The marker is written before population and never treats a merely released
    installer lock as proof that no installer child can still be running.
    """

    state_root = declared_state_root(environ)
    if state_root is None:
        return None
    if _absolute(root).is_symlink():
        raise RuntimeRetentionError(f"managed runtime root must not be a symlink: {root}")
    target = _canonical(root)
    if not _inside(target, state_root):
        return None
    with _registry_lock(state_root):
        record_file = _record_file(state_root, target)
        record = _parse_record(record_file) if record_file.exists() else None
        if record is None or record.get("state") == STATE_PUBLISHED:
            return record
        try:
            metadata = target.lstat()
        except OSError:
            metadata = None
        if metadata is not None and stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            record["device"], record["inode"] = metadata.st_dev, metadata.st_ino
        record.update(state=STATE_FAILED, updated=_utc_now(), error=str(error))
        _write_json(record_file, record)
        return record


def _record_directory(receipt: Mapping[str, Any], key: str) -> Path | None:
    value = receipt.get(key)
    if not isinstance(value, dict):
        return None
    directory = value.get("directory")
    if isinstance(directory, str) and directory and Path(directory).is_absolute():
        return _canonical(directory)
    return None


def _next_sequence(state_root: Path, *, excluding: Path | None = None) -> int:
    highest = 0
    catalog = _catalog(state_root)
    if catalog.is_dir():
        for path in catalog.glob(f"*{RECORD_SUFFIX}"):
            if excluding is not None and path == excluding:
                continue
            record = _parse_record(path)
            if record is None:
                continue
            sequence = record.get("published_sequence")
            if isinstance(sequence, int) and sequence > highest:
                highest = sequence
    return highest + 1


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_lock_file(path: Path) -> None:
    _ensure_directory(path.parent)
    try:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
    except OSError as error:
        raise RuntimeRetentionError(f"cannot preserve runtime lock sidecar {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeRetentionError(f"runtime lock sidecar is not an owned regular file: {path}")
    finally:
        os.close(descriptor)


# --------------------------------------------------------------------------
# Public status, use lease and external pins.
# --------------------------------------------------------------------------


def registered_runtime(root: str | Path) -> dict[str, Any] | None:
    """Return a pure status record for a managed image, or ``None``.

    An unreadable registration returns a diagnostic dict with
    ``state == "invalid"`` so status callers do not mistake it for unmanaged
    state; no lock, directory or file is created.
    """

    target = _canonical(root)
    record, record_file, error = _locate_spelling(root)
    if error is not None:
        return {
            "schema": SCHEMA,
            "kind": KIND,
            "root": str(target),
            "state": "invalid",
            "record": str(record_file) if record_file else None,
            "problems": [error],
        }
    if record is None:
        return None
    result = json.loads(json.dumps(record))
    result["record"] = str(record_file)
    result["problems"] = _image_problems(record, target, full_receipt=False)
    result["install_lock"] = str(install_lock_path(target))
    if record_file is not None:
        state_root = _state_root_of(record_file)
        if state_root is not None:
            result["use_lock"] = str(_use_lock(state_root, target))
    return result


@contextmanager
def runtime_use(root: str | Path) -> Iterator[dict[str, Any] | None]:
    """Hold a shared image-use lease for a registered runtime.

    Yields the current validated registration dict, or ``None`` for an
    unregistered legacy root, which is an explicit no-op.  A registered root
    that is replaced, corrupt, retiring or deleted raises
    ``RuntimeRetentionError`` rather than silently running against untrusted
    bytes.  The caller must re-resolve the runtime from the yielded identity
    while the lease is held.
    """

    target = _canonical(root)
    record, record_file, error = _locate_spelling(root)
    if record is None and error is None:
        yield None
        return
    if error is not None:
        raise RuntimeRetentionError(error)
    assert record is not None and record_file is not None
    if _absolute(root).is_symlink():
        raise RuntimeRetentionError(f"refusing a symlinked spelling of a registered runtime: {root}")
    state_root = _state_root_of(record_file)
    if state_root is None:
        raise RuntimeRetentionError(f"runtime registration is outside a catalog: {record_file}")
    if record.get("state") != STATE_PUBLISHED:
        raise RuntimeRetentionError(
            f"runtime {target} is not usable in state {record.get('state')!r}"
        )
    if _overlaps(target, _registry(state_root)):
        raise RuntimeRetentionError(
            f"runtime {target} overlaps the retention catalog {_registry(state_root)}"
        )
    defects = _image_problems(record, target, full_receipt=False)
    if defects:
        raise RuntimeRetentionError(
            f"refusing unsafe runtime {target}: " + "; ".join(defects)
        )
    lock = _use_lock(state_root, target)
    _ensure_lock_file(lock)
    descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        current = _parse_record(record_file)
        if current is None or current.get("state") != STATE_PUBLISHED:
            raise RuntimeRetentionError(
                f"runtime {target} was retired while acquiring its use lease"
            )
        defects = _image_problems(current, target, full_receipt=False)
        if defects:
            raise RuntimeRetentionError(
                f"runtime {target} changed while acquiring its use lease: "
                + "; ".join(defects)
            )
        yield json.loads(json.dumps(current))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _runtime_install_module():
    """Import the resolver lazily so the install/retention cycle stays boring."""

    try:
        from . import runtime_install as module
    except ImportError:
        import runtime_install as module  # type: ignore[no-redef]
    return module


@contextmanager
def leased_runtime(
    root: str | Path | None = None,
    *,
    runtime: Any | None = None,
) -> Iterator[Any]:
    """Select an installed runtime and hold its image-use lease while yielded.

    The selection is ``runtime.root`` when an already resolved object is given,
    else ``root``, else ``DARLING_RUNTIME_ROOT``; a PATH-only selection is first
    resolved to its installation root.  A registered image is re-resolved
    through the immutable receipt after the lease is acquired, so callers never
    run bytes that changed between selection and use.  An explicitly supplied
    runtime object for an *unregistered* root is preserved as-is: those roots
    are never retention targets, and callers may legitimately pass host
    protocol fixtures or package objects instead of a receipted installation.
    """

    module = _runtime_install_module()
    if runtime is not None:
        selected = Path(runtime.root)
    else:
        configured = root
        if configured is None:
            configured = os.environ.get("DARLING_RUNTIME_ROOT")
        if configured is not None:
            selected = Path(configured).expanduser()
        else:
            selected = module.resolve_runtime(None).root
    # Keep the caller's spelling: runtime_use owns symlink/replacement rejection
    # for registered images, and canonicalizing first would hide that evidence.
    with runtime_use(selected) as registration:
        resolved = runtime if runtime is not None and registration is None else module.resolve_runtime(selected)
        if registration is None and registered_runtime(selected) is not None:
            # A publication can appear between the legacy lookup and resolve.
            # Re-enter its use lease; never run that new generation unguarded.
            with runtime_use(selected) as published:
                if published is None:
                    raise RuntimeRetentionError("runtime registration changed while selecting its image")
                yield module.resolve_runtime(selected)
            return
        if registration is not None and _canonical(resolved.root) != _canonical(selected):
            raise RuntimeRetentionError(
                f"leased runtime resolved to {resolved.root}, not the leased root {selected}"
            )
        yield resolved


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or "\0" in key or len(key) > _KEY_LIMIT:
        raise ValueError("runtime pin key must be a non-empty bounded string")
    return key


def _registered_record_for_write(root: str | Path) -> tuple[dict[str, Any], Path, Path] | None:
    record, record_file, error = _locate_spelling(root)
    if error is not None:
        raise RuntimeRetentionError(f"cannot update runtime reference for {root}: {error}")
    if record is None or record_file is None:
        return None
    state_root = _state_root_of(record_file)
    if state_root is None:
        raise RuntimeRetentionError(f"runtime registration has no owning state root: {record_file}")
    return record, record_file, state_root


def pin_runtime(root: str | Path, key: str) -> bool:
    """Add an external persistent reference to a registered published image."""

    _validate_key(key)
    found = _registered_record_for_write(root)
    if found is None:
        return False
    _, record_file, state_root = found
    target = _canonical(root)
    with _registry_lock(state_root):
        record = _parse_record(record_file)
        if record is None or record.get("state") != STATE_PUBLISHED:
            raise RuntimeRetentionError(f"runtime is not a valid published image: {target}")
        problems = _image_problems(record, target, full_receipt=False)
        if problems:
            raise RuntimeRetentionError("cannot pin changed runtime: " + "; ".join(problems))
        pins = record.get("pins")
        if not isinstance(pins, dict):
            raise RuntimeRetentionError(f"runtime reference map is invalid: {target}")
        pins.setdefault(key, {"at": _utc_now()})
        record["pins"] = pins
        record["updated"] = _utc_now()
        _write_json(record_file, record)
        return True


def unpin_runtime(root: str | Path, key: str) -> bool:
    """Remove an external persistent reference; ``False`` when nothing changed."""

    _validate_key(key)
    found = _registered_record_for_write(root)
    if found is None:
        return False
    _, record_file, state_root = found
    with _registry_lock(state_root):
        record = _parse_record(record_file)
        if record is None:
            return False
        pins = record.get("pins")
        if not isinstance(pins, dict) or key not in pins:
            return False
        del pins[key]
        record["pins"] = pins
        record["updated"] = _utc_now()
        _write_json(record_file, record)
        return True


# --------------------------------------------------------------------------
# Native read-only reference inspection.
# --------------------------------------------------------------------------


def _unescape_proc_path(name: str) -> str:
    return (
        name.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\\\", "\\")
    )


def _named_within(name: str, root: str) -> bool:
    name = _unescape_proc_path(name)
    if not name.startswith("/"):
        return False
    if name.endswith(" (deleted)"):
        name = name[: -len(" (deleted)")]
    return name == root or name.startswith(root + "/")


@dataclass(frozen=True)
class _MountEntry:
    """One ``mountinfo`` row, identity-preserving across mount namespaces."""

    device: tuple[int, int]
    root: str
    mountpoint: str
    source: str


def _parse_mountinfo(text: str) -> list[_MountEntry]:
    entries: list[_MountEntry] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        try:
            major_text, minor_text = fields[2].split(":", 1)
            device = (int(major_text), int(minor_text))
        except (TypeError, ValueError):
            continue
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator < 5 or separator + 2 >= len(fields):
            continue
        entries.append(
            _MountEntry(
                device=device,
                root=_unescape_proc_path(fields[3]),
                mountpoint=_unescape_proc_path(fields[4]),
                source=_unescape_proc_path(fields[separator + 2]),
            )
        )
    return entries


def _read_mountinfo(path: Path, root: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise RuntimeRetentionError(
            f"cannot inspect mounts for {root}; read-only mount census unavailable: {error}"
        ) from error


def _mount_reference_reasons(root: Path, entries: Sequence[_MountEntry]) -> list[str]:
    spelling = str(root)
    found: list[str] = []
    for entry in entries:
        if _named_within(entry.mountpoint, spelling):
            found.append(f"mounted filesystem at {entry.mountpoint}")
        elif _named_within(entry.root, spelling):
            found.append(
                f"mount {entry.mountpoint} exposes filesystem root {entry.root}"
            )
        elif _named_within(entry.source, spelling):
            found.append(f"mount source {entry.source}")
    return found


def _mount_references(root: Path, *, mountinfo_path: Path) -> list[str]:
    """Single-namespace compatibility wrapper over the shared parser."""

    return _mount_reference_reasons(
        root, _parse_mountinfo(_read_mountinfo(mountinfo_path, root))
    )


def _image_filesystem_location(
    root: Path, entries: Sequence[_MountEntry]
) -> tuple[tuple[int, int], str | None]:
    """Return the image's device and its path within that mounted filesystem."""

    try:
        metadata = root.lstat()
    except OSError as error:
        raise RuntimeRetentionError(f"cannot inspect runtime root {root}: {error}") from error
    device = (os.major(metadata.st_dev), os.minor(metadata.st_dev))
    spelling = str(root)
    best: tuple[int, str] | None = None
    for entry in entries:
        if entry.device != device:
            continue
        prefix = entry.mountpoint.rstrip("/") or "/"
        if not (prefix == "/" or spelling == prefix or spelling.startswith(prefix + "/")):
            continue
        relative = os.path.relpath(spelling, prefix)
        base = entry.root.rstrip("/")
        fs_path = base if relative == "." else os.path.normpath(f"{base}/{relative}")
        fs_path = fs_path or "/"
        if best is None or len(prefix) > best[0]:
            best = (len(prefix), fs_path)
    return device, (best[1] if best else None)


def _path_within(path: str, parent: str) -> bool:
    parent = parent.rstrip("/") or "/"
    return parent == "/" or path == parent or path.startswith(parent + "/")


def _image_alias_paths(
    entries: Sequence[_MountEntry],
    device: tuple[int, int],
    fs_path: str | None,
) -> tuple[list[str], list[str]]:
    """Paths at which this mount namespace exposes the image filesystem.

    A bind mount keeps the filesystem device and records the bound subdirectory
    in its ``root`` field, so aliases are derived from filesystem identity
    rather than from a pathname spelled in the pruner's own namespace.  Any
    mount of the image device that cannot be located relative to the image is
    reported as uncertainty, which preserves the image.
    """

    aliases: list[str] = []
    uncertain: list[str] = []
    for entry in entries:
        if entry.device != device:
            continue
        if fs_path is None:
            uncertain.append(
                f"mount {entry.mountpoint} uses the runtime filesystem but cannot be mapped "
                "to the registered image"
            )
            continue
        mountpoint = entry.mountpoint.rstrip("/") or "/"
        mount_root = entry.root.rstrip("/") or "/"
        if _path_within(fs_path, mount_root):
            relative = os.path.relpath(fs_path, mount_root)
            if relative == ".":
                aliases.append(mountpoint)
            elif mountpoint == "/":
                aliases.append("/" + relative)
            else:
                aliases.append(os.path.normpath(f"{mountpoint}/{relative}"))
        elif _path_within(mount_root, fs_path):
            aliases.append(mountpoint)
    return aliases, uncertain


def _exited_thread_group(process: Path) -> bool:
    """A zombie has no mm/files; a zombie leader can still have live threads."""
    def state(path: Path) -> bytes | None:
        try:
            fields = (path / "stat").read_bytes().rsplit(b")", 1)[1].split()
            return fields[0]
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ESRCH):
                return None
            raise
        except IndexError as error:
            raise RuntimeRetentionError(f"cannot inspect process state: {path}") from error
    if state(process) not in (b"Z", b"X"):
        return False
    leader_seen = False
    try:
        with os.scandir(process / "task") as threads:
            for thread in threads:
                leader_seen |= thread.name == process.name
                if thread.name.isdecimal() and state(Path(thread.path)) not in (b"Z", b"X"):
                    raise RuntimeRetentionError(
                        f"exited leader {process} has a live or unreadable thread {thread.name}; "
                        "its namespace cannot be judged from the leader"
                    )
    except OSError as error:
        raise RuntimeRetentionError(f"cannot verify exited thread group {process}: {error}") from error
    if not leader_seen:
        raise RuntimeRetentionError(f"exited thread group membership is incomplete: {process}")
    return True


def _process_reference(process: Path, root_spellings: Sequence[str]) -> list[str]:
    found: list[str] = []
    for special in ("cwd", "root", "exe"):
        try:
            name = os.readlink(process / special)
        except OSError as error:
            if isinstance(error, (FileNotFoundError, ProcessLookupError)):
                continue
            if error.errno == errno.ESRCH:
                continue
            if isinstance(error, PermissionError):
                raise
            raise
        if any(_named_within(name, spelling) for spelling in root_spellings):
            found.append(f"process {process.name} {special} -> {name}")
    try:
        with (process / "maps").open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                fields = line.split(maxsplit=5)
                if len(fields) < 6:
                    continue
                name = fields[5].rstrip("\n")
                if any(_named_within(name, spelling) for spelling in root_spellings):
                    found.append(f"process {process.name} maps {name}")
    except OSError as error:
        if isinstance(error, (FileNotFoundError, ProcessLookupError)):
            pass
        elif error.errno == errno.ESRCH:
            pass
        elif isinstance(error, PermissionError):
            raise
        else:
            raise
    try:
        with os.scandir(process / "fd") as descriptors:
            for descriptor in descriptors:
                try:
                    name = os.readlink(descriptor.path)
                except OSError as error:
                    if isinstance(error, (FileNotFoundError, ProcessLookupError)):
                        continue
                    if error.errno == errno.ESRCH:
                        continue
                    if isinstance(error, PermissionError):
                        raise
                    raise
                if any(_named_within(name, spelling) for spelling in root_spellings):
                    found.append(f"process {process.name} fd {descriptor.name} -> {name}")
    except OSError as error:
        if isinstance(error, (FileNotFoundError, ProcessLookupError)):
            pass
        elif error.errno == errno.ESRCH:
            pass
        elif isinstance(error, PermissionError):
            raise
        else:
            raise
    return found


def _privileged_reference_census(root: Path, denied: list[str]) -> list[str] | None:
    """Read-only privileged completion for PIDs the natural-user census denied.

    A multi-user Linux host always has other-owner processes whose maps/fds are
    unreadable to us; treating all of them as possible image users would make
    every removal impossible.  The isolated child inspects only the denied PIDs,
    never mutates anything, and its result is rejected unless every denied PID
    could be inspected.
    """

    command = [
        "sudo",
        "-n",
        "--",
        sys.executable,
        "-B",
        "-I",
        str(Path(__file__).absolute()),
        "_reference-census",
        str(root),
        json.dumps(sorted(denied)),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("references"), list)
        or not isinstance(payload.get("denied"), list)
    ):
        return None
    if payload["denied"]:
        return None
    references = payload["references"]
    if not all(isinstance(item, str) for item in references):
        return None
    return references


def _namespace_image_paths(
    entries: Sequence[_MountEntry],
    device: tuple[int, int],
    fs_path: str | None,
    image: Path,
) -> tuple[list[str], list[str]]:
    """Paths that address the image from one process mount namespace.

    Namespaces that bind-mount the image elsewhere are handled through
    filesystem identity: every mount of the same device is translated back to
    the registered image path, so a process naming the alias is still detected.
    """

    aliases, uncertain = _image_alias_paths(entries, device, fs_path)
    return list(dict.fromkeys([str(image), *aliases])), uncertain


def _reference_inspection(
    root: Path,
    *,
    proc_root: Path | None = None,
    mountinfo_path: Path | None = None,
) -> list[str]:
    """Read-only reader/exec/mount references across mount namespaces.

    The pruner's own ``mountinfo`` cannot describe a process that reaches the
    image through a bind mount in another mount namespace.  Each process's
    namespace is therefore translated through filesystem identity, and any
    namespace that cannot be inspected is completed by the isolated privileged
    child or else fails closed.
    """

    proc = PROC_ROOT if proc_root is None else proc_root
    mounts = MOUNTINFO_PATH if mountinfo_path is None else mountinfo_path
    own_entries = _parse_mountinfo(_read_mountinfo(mounts, root))
    reasons = _mount_reference_reasons(root, own_entries)
    device, fs_path = _image_filesystem_location(root, own_entries)
    _, own_uncertain = _namespace_image_paths(own_entries, device, fs_path, root)
    reasons.extend(own_uncertain)
    try:
        processes = list(os.scandir(proc))
    except OSError as error:
        raise RuntimeRetentionError(
            f"cannot inspect process references for {root}; read-only /proc census unavailable: {error}"
        ) from error
    cache: dict[str, tuple[list[str], list[str]]] = {}
    denied: list[str] = []
    for entry in processes:
        if not entry.name.isdecimal():
            continue
        process = Path(entry.path)
        try:
            if _exited_thread_group(process):
                continue
            text = (process / "mountinfo").read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ESRCH):
                continue
            denied.append(entry.name)
            continue
        translated = cache.get(text)
        if translated is None:
            entries = _parse_mountinfo(text)
            paths, uncertain = _namespace_image_paths(entries, device, fs_path, root)
            namespace_reasons = [
                f"another mount namespace: {reason}"
                for reason in (*_mount_reference_reasons(root, entries), *uncertain)
            ]
            translated = (paths, namespace_reasons)
            cache[text] = translated
            reasons.extend(namespace_reasons)
        try:
            reasons.extend(_process_reference(process, translated[0]))
        except PermissionError:
            denied.append(entry.name)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ESRCH):
                continue
            raise RuntimeRetentionError(
                f"cannot inspect process {entry.name} for references to {root}: {error}"
            ) from error
    if denied:
        privileged = (
            _privileged_reference_census(root, denied)
            if proc_root is None and mountinfo_path is None
            else None
        )
        if privileged is None:
            raise RuntimeRetentionError(
                "read-only process census was denied for PID(s) "
                + ",".join(sorted(denied))
                + f" and the isolated privileged fallback failed; refusing to judge references to {root}"
            )
        reasons.extend(privileged)
    return list(dict.fromkeys(reasons))


# --------------------------------------------------------------------------
# Explicit pruning.
# --------------------------------------------------------------------------


def _acquire_exclusive(
    path: Path, *, require_existing: bool
) -> tuple[int | None, str | None]:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if not require_existing:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        return None, f"required lock is missing: {path}"
    except OSError as error:
        return None, f"cannot open lock {path}: {error}"
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            os.close(descriptor)
            return None, f"lock is not an owned regular file: {path}"
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None, "busy"
        except OSError as error:
            os.close(descriptor)
            return None, f"cannot lock {path}: {error}"
        return descriptor, None
    except BaseException:
        os.close(descriptor)
        raise


def _release(descriptor: int | None) -> None:
    if descriptor is None:
        return
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def _row(path: str | Path, action: str, reason: str, size: int = 0, **extra: Any) -> dict[str, Any]:
    row = {"path": str(path), "action": action, "reason": reason, "bytes": size}
    row.update(extra)
    return row


def _ranked_published(
    records: list[tuple[Path, dict[str, Any] | None]]
) -> tuple[dict[str, int], set[str]]:
    ranked: list[tuple[int, str, Path]] = []
    unranked: set[str] = set()
    for record_file, record in records:
        if record is None or record.get("state") != STATE_PUBLISHED:
            continue
        sequence = record.get("published_sequence")
        if not isinstance(sequence, int) or sequence < 1:
            unranked.add(str(record.get("root")))
            continue
        ranked.append((sequence, str(record.get("root")), record_file))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ({root: index for index, (_, root, _) in enumerate(ranked)}, unranked)


def _load_records(state_root: Path) -> list[tuple[Path, dict[str, Any] | None]]:
    catalog = _catalog(state_root)
    if not catalog.is_dir() or catalog.is_symlink():
        return []
    records: list[tuple[Path, dict[str, Any] | None]] = []
    for path in sorted(catalog.glob(f"*{RECORD_SUFFIX}")):
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            continue
        records.append((path, _parse_record(path)))
    return records


def _trash_name_prefix(image: Path) -> str:
    return f"image-{_record_id(image)}-"


def _journaled_trash(
    state_root: Path, image: Path, record: Mapping[str, Any]
) -> tuple[Path | None, str | None]:
    """Validate the journaled trash destination before touching any bytes.

    A retirement record is recoverable only when it carries the exact trash
    path and the detached inode identity written before ``rename``.  Anything
    else is preserved: this is never an adoption path for foreign directories.
    """

    raw = record.get("trash")
    device = record.get("trash_device")
    inode = record.get("trash_inode")
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        return None, "retiring registration has no journaled detachment path"
    if type(device) is not int or type(inode) is not int:
        return None, "retiring registration has no journaled detached identity"
    trash = _absolute(raw)
    if _canonical(trash.parent) != _canonical(_trash(state_root)):
        return None, "retiring registration names a trash path outside the owned trash directory"
    if not trash.name.startswith(_trash_name_prefix(image)):
        return None, "retiring registration names a trash path for another image"
    return trash, None


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _write_retiring_error(
    state_root: Path, record_file: Path, trash: Path, message: str
) -> None:
    """Keep a handled deletion failure recoverable instead of stranding it."""

    try:
        with _registry_lock(state_root):
            current = _parse_record(record_file)
            if (
                current is not None
                and current.get("state") == STATE_RETIRING
                and current.get("trash") == str(trash)
            ):
                current["error"] = message
                current["updated"] = _utc_now()
                _write_json(record_file, current)
    except (OSError, ValueError):
        pass


def _restore_published(state_root: Path, record_file: Path) -> None:
    with _registry_lock(state_root):
        restored = _parse_record(record_file)
        if restored is None or restored.get("state") != STATE_RETIRING:
            return
        previous = restored.get("previous_state")
        restored["state"] = previous if previous in (STATE_PUBLISHED, STATE_FAILED) else STATE_PUBLISHED
        restored.pop("previous_state", None)
        for key in ("trash", "trash_device", "trash_inode", "trash_size", "error"):
            restored.pop(key, None)
        restored["updated"] = _utc_now()
        _write_json(record_file, restored)


def _delete_detached(
    state_root: Path, record_file: Path, trash: Path, size: int
) -> tuple[str, str, int]:
    """Delete journaled trash bytes and clear the matching registration."""

    failure: str | None = None
    try:
        shutil.rmtree(trash)
    except OSError as error:
        failure = f"image moved to retained trash {trash} but deletion failed: {error}"
    if failure is not None:
        _write_retiring_error(state_root, record_file, trash, failure)
        try:
            remaining = _tree_bytes(trash)
        except (OSError, RuntimeRetentionError):
            remaining = size
        return "failed", failure, max(0, size - remaining)
    with _registry_lock(state_root):
        current = _parse_record(record_file)
        if current is not None and current.get("state") == STATE_RETIRING:
            try:
                record_file.unlink(missing_ok=True)
                _fsync_directory(record_file.parent)
            except OSError as error:
                return (
                    "failed",
                    f"image bytes were deleted but the registration remains: {error}",
                    size,
                )
    return "delete", "retained publication released by policy", size


def _detach_and_remove(
    state_root: Path,
    record_file: Path,
    image: Path,
    trash: Path,
    size: int,
    expected: tuple[int, int],
) -> tuple[str, str, int]:
    """Detach the journaled image and delete it; failures stay recoverable."""

    try:
        before = image.lstat()
    except OSError as error:
        _write_retiring_error(
            state_root,
            record_file,
            trash,
            f"image could not be inspected before detachment: {error}",
        )
        return "failed", f"could not inspect image before detachment: {error}", 0
    if (before.st_dev, before.st_ino) != expected:
        _restore_published(state_root, record_file)
        return "failed", "image identity changed before detachment", 0
    try:
        os.rename(image, trash)
        _fsync_directory(image.parent)
        _fsync_directory(trash.parent)
    except OSError as error:
        _write_retiring_error(
            state_root,
            record_file,
            trash,
            f"image could not be detached for removal: {error}",
        )
        return "failed", f"could not detach image for removal: {error}", 0
    try:
        moved = trash.lstat()
    except OSError as error:
        _write_retiring_error(
            state_root,
            record_file,
            trash,
            f"detached image could not be verified after rename: {error}",
        )
        return (
            "failed",
            f"image detached to {trash} but its identity could not be verified: {error}",
            0,
        )
    if (moved.st_dev, moved.st_ino) != expected:
        try:
            os.rename(trash, image)
            _fsync_directory(image.parent)
            _fsync_directory(trash.parent)
        except OSError:
            _write_retiring_error(
                state_root,
                record_file,
                trash,
                "detached inode does not match the journaled image",
            )
            return (
                "failed",
                "detached inode does not match the journaled image; preserved",
                0,
            )
        _restore_published(state_root, record_file)
        return "failed", "detached inode did not match the journaled image; restored", 0
    return _delete_detached(state_root, record_file, trash, size)


def _retire_and_remove(
    state_root: Path,
    record_file: Path,
    root: Path,
    size: int,
    keep_last: int,
) -> tuple[str, str, int]:
    """Journal the trash identity, rename the complete image aside, delete it.

    Returns ``(action, reason, freed_bytes)``.  The registration is re-checked
    and marked retiring under the catalog lock while both external locks are
    held, so a racing pin or newly published image cannot be lost.  The
    intended trash path and the image inode identity are persisted *before*
    detachment, so an interrupted rename or deletion is repaired later.
    """

    trash = _trash(state_root) / f"{_trash_name_prefix(root)}{uuid.uuid4().hex}"
    _ensure_directory(trash.parent)
    with _registry_lock(state_root):
        current = _parse_record(record_file)
        if current is None or current.get("state") != STATE_PUBLISHED:
            return "keep", "registration changed before retirement", 0
        pins = current.get("pins") if isinstance(current.get("pins"), dict) else {}
        if pins:
            return "keep", "became pinned before retirement", 0
        ranks, _ = _ranked_published(_load_records(state_root))
        rank = ranks.get(str(root))
        if rank is not None and rank < keep_last:
            return "keep", f"became newest published image (rank {rank + 1})", 0
        defects = _image_problems(current, root, full_receipt=False)
        if defects:
            return "keep", "image changed before retirement: " + "; ".join(defects), 0
        try:
            metadata = root.lstat()
        except OSError as error:
            return "keep", f"cannot inspect image before retirement: {error}", 0
        if (metadata.st_dev, metadata.st_ino) != (
            current.get("device"),
            current.get("inode"),
        ):
            return "keep", "image identity changed before retirement", 0
        current["previous_state"] = current.get("state", STATE_PUBLISHED)
        current.update(
            state=STATE_RETIRING,
            trash=str(trash),
            trash_device=metadata.st_dev,
            trash_inode=metadata.st_ino,
            trash_size=size,
            error=None,
            updated=_utc_now(),
        )
        _write_json(record_file, current)
    return _detach_and_remove(
        state_root,
        record_file,
        root,
        trash,
        size,
        (metadata.st_dev, metadata.st_ino),
    )


def _recover_retirement(
    state_root: Path,
    record_file: Path,
    record: Mapping[str, Any],
    *,
    keep_last: int,
    dry_run: bool,
) -> tuple[str, str, int]:
    """Repair a retirement interrupted between journal, rename and deletion."""

    image = _canonical(record["root"])
    trash, journal_error = _journaled_trash(state_root, image, record)
    if journal_error is not None or trash is None:
        return "keep", journal_error or "no recoverable detachment journal", 0
    if record.get("pins"):
        return "keep", "retiring registration is pinned; preserved", 0
    expected = (record["trash_device"], record["trash_inode"])
    size = record.get("trash_size")
    if type(size) is not int or size < 0:
        size = 0

    use_descriptor, use_error = _acquire_exclusive(
        _use_lock(state_root, image), require_existing=True
    )
    if use_error is not None:
        return "keep", (use_error if use_error != "busy" else "busy: an image-use lease is held"), 0
    install_descriptor, install_error = _acquire_exclusive(
        install_lock_path(image), require_existing=True
    )
    if install_error is not None:
        _release(use_descriptor)
        return "keep", (install_error if install_error != "busy" else "busy: the installation lock is held"), 0
    try:
        with _registry_lock(state_root):
            current = _parse_record(record_file)
            if (
                current is None
                or current.get("state") != STATE_RETIRING
                or current.get("trash") != str(trash)
            ):
                return "keep", "retirement registration changed during recovery", 0
            if current.get("pins"):
                return "keep", "retiring registration became pinned; preserved", 0
        try:
            attached = _lstat(image)
            detached = _lstat(trash)
        except OSError as error:
            return "keep", f"cannot inspect interrupted retirement: {error}", 0
        if attached is not None:
            if not stat.S_ISDIR(attached.st_mode) or stat.S_ISLNK(attached.st_mode):
                return "keep", "attached image was replaced during retirement", 0
            if (attached.st_dev, attached.st_ino) != expected:
                return "keep", "attached image identity no longer matches its retirement journal", 0
        if detached is not None:
            if not stat.S_ISDIR(detached.st_mode) or stat.S_ISLNK(detached.st_mode):
                return "keep", "journaled trash path is not the detached image", 0
            if detached.st_uid != os.getuid():
                return "keep", "journaled trash path belongs to another owner", 0
            if (detached.st_dev, detached.st_ino) != expected:
                return "keep", "journaled trash identity does not match the detached image", 0
        if attached is not None and detached is not None:
            return "keep", "both the attached image and its detached copy exist; preserved", 0
        if attached is not None:
            # Crashed after journaling but before rename: only re-detach when the
            # policy still selects this image and the bytes remain complete.
            sequence = record.get("published_sequence")
            if isinstance(sequence, int):
                newer = sum(
                    1
                    for _, other in _load_records(state_root)
                    if other is not None
                    and other.get("state") == STATE_PUBLISHED
                    and isinstance(other.get("published_sequence"), int)
                    and other["published_sequence"] > sequence
                )
                if newer < keep_last:
                    if not dry_run:
                        _restore_published(state_root, record_file)
                    return "keep", f"interrupted retirement is again among the newest {keep_last}", 0
            defects = _image_problems(record, image, full_receipt=True)
            if defects:
                if not dry_run:
                    _restore_published(state_root, record_file)
                return "keep", "corrupt registration: " + "; ".join(defects), 0
            if dry_run:
                return "would-delete", "recover interrupted retirement", size, 0
            action, reason, freed = _detach_and_remove(
                state_root, record_file, image, trash, size, expected
            )
            return action, f"recovered interrupted retirement: {reason}", freed
        if detached is not None:
            if dry_run:
                return "would-delete", "retry detached retirement", size, 0
            action, reason, freed = _delete_detached(
                state_root, record_file, trash, size
            )
            if action == "delete":
                reason = f"recovered detached retirement: {reason}"
            return action, reason, freed
        # Both names are gone: the journal proves this registration supervised
        # their removal, so only the stale registration is cleared.
        if dry_run:
            return "would-delete", "detached retirement already removed its bytes; stale registration remains", 0
        with _registry_lock(state_root):
            current = _parse_record(record_file)
            if current is None:
                return "keep", "retirement registration disappeared during recovery", 0
            if (
                current.get("state") != STATE_RETIRING
                or current.get("trash") != str(trash)
            ):
                return "keep", "retirement registration changed during recovery", 0
            try:
                record_file.unlink(missing_ok=True)
                _fsync_directory(record_file.parent)
            except OSError as error:
                return "failed", f"retired image is gone but its registration remains: {error}", 0
        return "delete", "detached retirement had already removed its bytes", 0
    finally:
        _release(install_descriptor)
        _release(use_descriptor)


def prune_runtimes(
    root: str | Path,
    *,
    keep_last: int = DEFAULT_KEEP_LAST,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Apply the explicit retention policy under one declared owner root.

    ``root`` is the declared state owner root, never an image and never an
    arbitrary directory.  Returns one action row per registration with
    ``path``, ``action``, ``reason`` and ``bytes``.  Only fully receipted,
    unlocked, unreferenced images beyond the newest ``keep_last`` and outside
    all pins are removed; every uncertainty is reported and preserved.
    """

    if type(keep_last) is not int or keep_last < 0:
        raise ValueError("keep_last must be a non-negative integer")
    state_root = _canonical(root)
    try:
        metadata = state_root.lstat()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise RuntimeRetentionError(f"cannot inspect declared state root {state_root}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeRetentionError(f"declared state root must be a real directory: {state_root}")

    rows: list[dict[str, Any]] = []
    records = _load_records(state_root)
    if not records:
        return rows
    ranks, unranked = _ranked_published(records)

    def report(row: dict[str, Any]) -> None:
        rows.append(row)
        if progress is not None:
            progress(
                f"runtime {row['action']}: {row['path']} "
                f"({row['reason']}, {row['bytes']} bytes)"
            )

    for record_file, snapshot in records:
        if snapshot is None:
            report(_row(record_file, "keep", "registration is unreadable; ownership is unknown"))
            continue
        image_root = snapshot.get("root")
        if not isinstance(image_root, str) or not Path(image_root).is_absolute():
            report(_row(record_file, "keep", "registration has no absolute image root"))
            continue
        image = _canonical(image_root)
        label = _absolute(image_root)
        if not _inside(image, state_root):
            report(_row(label, "keep", "registration is outside the declared state root"))
            continue
        if image == state_root or _overlaps(image, _registry(state_root)):
            report(_row(label, "keep", "registration overlaps the declared owner metadata"))
            continue
        state = snapshot.get("state")
        if state == STATE_RETIRING:
            try:
                action, reason, freed = _recover_retirement(
                    state_root, record_file, snapshot, keep_last=keep_last, dry_run=dry_run
                )
            except (OSError, RuntimeRetentionError) as error:
                report(
                    _row(
                        label,
                        "keep",
                        f"retirement recovery failed closed: {error}",
                        runtime_root=str(image),
                    )
                )
                continue
            report(_row(label, action, reason, freed, runtime_root=str(image)))
            continue
        if state not in USABLE_STATES:
            report(
                _row(
                    label,
                    "keep",
                    f"incomplete publication ({state}) is never reclaimed automatically",
                )
            )
            continue

        pinned = snapshot.get("pins") if isinstance(snapshot.get("pins"), dict) else {}
        rank = ranks.get(str(image))
        protected_for_rank = rank is not None and rank < keep_last
        if pinned:
            report(
                _row(
                    label,
                    "keep",
                    "pinned: " + ", ".join(sorted(str(key) for key in pinned)),
                )
            )
            continue
        if protected_for_rank:
            report(_row(label, "keep", f"newest published image (rank {rank + 1})"))
            continue
        if state == STATE_PUBLISHED and str(image) in unranked:
            report(_row(label, "keep", "published image has no publication sequence"))
            continue

        defects = _image_problems(snapshot, image, full_receipt=True)
        if defects:
            report(_row(label, "keep", "corrupt registration: " + "; ".join(defects)))
            continue

        use_descriptor, use_error = _acquire_exclusive(
            _use_lock(state_root, image), require_existing=True
        )
        if use_error == "busy":
            report(_row(label, "keep", "busy: an image-use lease is held"))
            continue
        if use_error is not None:
            report(_row(label, "keep", use_error))
            continue
        install_descriptor, install_error = _acquire_exclusive(
            install_lock_path(image), require_existing=True
        )
        if install_error == "busy":
            _release(use_descriptor)
            report(_row(label, "keep", "busy: the installation lock is held"))
            continue
        if install_error is not None:
            _release(use_descriptor)
            report(_row(label, "keep", install_error))
            continue
        try:
            try:
                references = _reference_inspection(image)
            except RuntimeRetentionError as error:
                report(_row(label, "keep", f"reference inspection failed closed: {error}"))
                continue
            if references:
                report(
                    _row(
                        label,
                        "keep",
                        "in use: " + "; ".join(sorted(set(references))[:4]),
                    )
                )
                continue
            try:
                size = _tree_bytes(image)
            except RuntimeRetentionError as error:
                report(_row(label, "keep", f"cannot size image: {error}"))
                continue
            if dry_run:
                report(_row(label, "would-delete", "retention policy selects this image", size))
                continue

            try:
                action, reason, freed = _retire_and_remove(
                    state_root, record_file, image, size, keep_last
                )
            except (OSError, RuntimeRetentionError) as error:
                report(
                    _row(
                        label,
                        "keep",
                        f"retirement failed closed: {error}",
                        runtime_root=str(image),
                    )
                )
                continue
            report(
                _row(
                    label,
                    action,
                    reason,
                    freed,
                    runtime_root=str(image),
                )
            )
        finally:
            _release(install_descriptor)
            _release(use_descriptor)
    return rows


def reserve_check(
    path: str | Path,
    *,
    required_bytes: int = 0,
    floor_bytes: int = DEFAULT_FREE_FLOOR,
) -> dict[str, Any]:
    """Advisory free-space observation for a caller-owned admission decision.

    This module never deletes data to satisfy the floor; refusal belongs to the
    operation that is about to allocate.
    """

    if required_bytes < 0 or floor_bytes < 0:
        raise ValueError("reserve byte counts must not be negative")
    target = Path(path)
    statistics = os.statvfs(target)
    free = statistics.f_bavail * statistics.f_frsize
    return {
        "path": str(target),
        "available_bytes": free,
        "required_bytes": required_bytes,
        "floor_bytes": floor_bytes,
        "ok": free - required_bytes >= floor_bytes,
    }


def require_reserve(
    path: str | Path,
    *,
    required_bytes: int = 0,
    floor_bytes: int | None = None,
) -> dict[str, Any]:
    """Refuse a heavy allocation that would breach the free-space floor.

    This is an admission check, never a cleanup trigger: nothing is deleted or
    trimmed to satisfy the floor.  ``None`` selects the module default at call
    time so callers and tests can lower or raise the policy explicitly.
    """

    floor = DEFAULT_FREE_FLOOR if floor_bytes is None else floor_bytes
    report = reserve_check(path, required_bytes=required_bytes, floor_bytes=floor)
    if not report["ok"]:
        raise RuntimeRetentionError(
            f"refusing allocation under {report['path']}: {report['available_bytes']} bytes free, "
            f"{report['required_bytes']} required plus a {report['floor_bytes']}-byte floor"
        )
    return report


def require_publication_budget(
    runtime_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    floor_bytes: int | None = None,
) -> dict[str, Any] | None:
    """Enforce the managed free-space floor before a heavy build or install.

    Unmanaged or outside-``DW_STATE_ROOT`` candidates are a no-op so legacy
    workflows keep their behaviour.  A managed candidate that does not exist
    yet is measured at its nearest existing ancestor (or the declared owner
    root), so admission is decided before any allocation.  ``floor_bytes``
    replaces the default policy only when a caller explicitly configures it;
    this helper never deletes data to satisfy the floor.
    """

    state_root = declared_state_root(environ)
    if state_root is None:
        return None
    target = _canonical(runtime_root)
    if not _inside(target, state_root):
        return None
    probe = target
    while True:
        try:
            probe.stat()
            break
        except FileNotFoundError:
            if probe == probe.parent:
                break
            probe = probe.parent
        except OSError as error:
            raise RuntimeRetentionError(
                f"cannot inspect budget probe {probe}: {error}"
            ) from error
    try:
        return require_reserve(probe, floor_bytes=floor_bytes)
    except OSError as error:
        raise RuntimeRetentionError(
            f"cannot inspect free space for managed allocation under {probe}: {error}"
        ) from error


def _reference_census_cli(arguments: list[str]) -> int:
    """Read-only census of explicitly named PIDs, used only through ``sudo -n``.

    The parent passes the already-canonical image root and the PIDs whose
    natural-user inspection was denied.  No image, process or directory is
    modified; the JSON result is accepted only when every requested PID was
    inspected.
    """

    if len(arguments) != 2:
        print(json.dumps({"error": "expected ROOT and PID-JSON"}))
        return 2
    root = Path(arguments[0])
    try:
        pids = json.loads(arguments[1])
    except ValueError:
        print(json.dumps({"error": "invalid PID list"}))
        return 2
    if not isinstance(pids, list) or not all(
        isinstance(pid, str) and pid.isdecimal() for pid in pids
    ):
        print(json.dumps({"error": "invalid PID list"}))
        return 2
    if not root.is_absolute():
        print(json.dumps({"error": "runtime root must be absolute"}))
        return 2
    references: list[str] = []
    denied: list[str] = []
    try:
        own_entries = _parse_mountinfo(
            (PROC_ROOT / "self" / "mountinfo").read_text(
                encoding="utf-8", errors="replace"
            )
        )
    except OSError:
        own_entries = []
    device, fs_path = _image_filesystem_location(root, own_entries)
    cache: dict[str, list[str]] = {}
    for pid in pids:
        process = PROC_ROOT / pid
        try:
            if _exited_thread_group(process):
                continue
            text = (process / "mountinfo").read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ESRCH):
                continue
            denied.append(pid)
            continue
        paths = cache.get(text)
        if paths is None:
            paths, _ = _namespace_image_paths(
                _parse_mountinfo(text), device, fs_path, root
            )
            cache[text] = paths
        try:
            references.extend(_process_reference(process, paths))
        except PermissionError:
            denied.append(pid)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ESRCH):
                continue
            denied.append(pid)
    print(json.dumps({"references": references, "denied": denied}, sort_keys=True))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "_reference-census":
        raise SystemExit(_reference_census_cli(sys.argv[2:]))
    raise SystemExit("runtime_retention is a library module")


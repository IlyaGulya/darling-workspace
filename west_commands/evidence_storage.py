"""Lossless, finalized diagnostic storage and bounded, source-bound log slices.

Explicit selection is the caller's finalization declaration. Recursive CLI
selection protects tracked payloads unless explicitly opted in, and always
excludes source/build/prefix/cache trees. Index
entries retain logical names and original digests; readers need not rewrite old
manifests or checksum lists when a payload is stored as zstd.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

ZSTD = "/usr/bin/zstd"
INDEX_NAME = "evidence-index.json"
CHUNK = 1024 * 1024
MIN_BYTES = 4096
BOUNDARY_BYTES = 4096
_BLOCKED_DIRS = {".git", ".darling", "build", "source", "src", "prefix", "cache", ".cache", "__pycache__", "node_modules", "target"}
_PAYLOAD_SUFFIXES = {".log", ".stdout", ".stderr", ".trace", ".strace", ".core", ".dmp", ".dump", ".perf", ".data", ".pcap", ".pcapng", ".out", ".err", ".txt"}
_BINARY_MAGIC = {b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca", b"!<ar"}
_METADATA_SUFFIXES = {".md", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".sha256", ".sha256sum", ".sha1", ".sum", ".c", ".h", ".cc", ".cpp", ".py", ".rs", ".sh", ".patch", ".diff", ".bundle", ".zst"}
# Native run/capture control records are metadata despite their text suffixes.
_METADATA_NAMES = {
    "command.txt", "env.txt", "pid.txt", "target-pid.txt", "exit-status.txt",
    "cleanup-status.txt", "prefix-cleanup-status.txt", "attach-strace.txt",
    "fatal-targets.txt", "evidence-manifest",
}


class EvidenceError(RuntimeError):
    """Evidence could not safely be published or captured."""


class PayloadNotFoundError(EvidenceError):
    """No raw, stored or indexed payload exists for a logical reference."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(path)))


def _no_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise EvidenceError(f"symlink path is not evidence storage: {component}")


def _blocked(path: Path, *, directory: bool = False) -> bool:
    for part in (path.parts if directory else path.parent.parts):
        lowered = part.lower()
        if lowered in _BLOCKED_DIRS or lowered.endswith("-cache") or lowered.startswith(("prefix-", "build-", "build_", "runtime-build-cache", ".git", "rescue")):
            return True
    ancestors = (path, *path.parents) if directory else path.parents
    for parent in ancestors:
        if parent == parent.parent:
            continue
        if (parent / ".west-runtime-profile.json").is_file():
            return True
        if (parent / "bin/darling").is_file() and (parent / "libexec/darling").is_dir():
            return True
    return False


def _eligible(path: Path, *, explicit: bool) -> bool:
    name = path.name.lower()
    if name in _METADATA_NAMES:
        return False
    if name.startswith(".") or name.startswith(("readme", "manifest", "checksum", "sha256", "cmakelists", "summary", "verdict", "counters", "census", "index", "rescue")):
        return False
    if path.suffix.lower() in _METADATA_SUFFIXES:
        return False
    if path.suffix.lower() in _PAYLOAD_SUFFIXES:
        return True
    if name in {"stdout", "stderr", "core", "perf.data"} or re.search(r"(?:^|[-.])core\.[0-9]+$|\.trace[^/]*\.[0-9]+$|\.log\.[0-9]+$", name):
        return True
    # Retained binary copies often have arbitrary backup suffixes. Identify
    # actual images, not arbitrary extensionless/source/build input files.
    if explicit or "diagnostics" in path.parts:
        if path.suffix.lower() in {".bin", ".raw", ".dat"}:
            return True
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as source:
                magic = source.read(4)
            return magic in _BINARY_MAGIC or magic[:2] == b"MZ"
    return False


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> Any:
    _no_symlinks(path)
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _atomic_json(path: Path, value: Any, *, replace: bool = True) -> None:
    _no_symlinks(path)
    descriptor, name = tempfile.mkstemp(prefix=".evidence-", suffix=".partial", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _hash_fd(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(CHUNK, size - offset), offset)
        if not chunk:
            raise EvidenceError("input shortened while hashing")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _hash_path(path: Path) -> str:
    with path.open("rb") as source:
        return _hash_fd(source.fileno(), os.fstat(source.fileno()).st_size)


def _run_zstd(command: list[str], *, source: BinaryIO, destination: BinaryIO) -> None:
    # Diagnostics from a failed command are disk-backed too: a corrupt multi-GB
    # frame must not accidentally turn stderr into an unbounded Python buffer.
    with tempfile.TemporaryFile() as errors:
        result = subprocess.run(command, stdin=source, stdout=destination, stderr=errors, check=False)
        if result.returncode:
            errors.seek(0)
            detail = errors.read(8192).decode("utf-8", errors="replace").strip()
            raise EvidenceError(f"zstd exited {result.returncode}: {detail}")


def _compress_zstd(source: BinaryIO, destination: BinaryIO) -> None:
    _run_zstd([ZSTD, "-q", "-3", "-T1", "-c"], source=source, destination=destination)


def _verify_zstd(path: Path, expected_hash: str, expected_size: int) -> None:
    with path.open("rb") as source, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([ZSTD, "-q", "-d", "-c"], stdin=source, stdout=subprocess.PIPE, stderr=errors)
        assert process.stdout is not None
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := process.stdout.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
                if size > expected_size:
                    raise EvidenceError("decompressed evidence exceeds original size")
            code = process.wait()
            if code:
                raise EvidenceError(f"zstd verification exited {code}")
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise EvidenceError("decompressed evidence SHA256/size differs from original")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()


@dataclass
class _WriterSnapshot:
    targets: set[tuple[int, int]]
    writers: set[tuple[int, int]]
    denied: list[str]


def _proc_writer_census(targets: set[tuple[int, int]], pids: set[str] | None = None) -> _WriterSnapshot:
    """Read /proc once, retaining only identities in this bounded batch."""
    writers: set[tuple[int, int]] = set()
    denied: list[str] = []
    with os.scandir("/proc") as processes:
        for process in processes:
            if not process.name.isdecimal() or (pids is not None and process.name not in pids):
                continue
            process_path = Path(process.path)
            try:
                with os.scandir(process_path / "fd") as descriptors:
                    for descriptor in descriptors:
                        try:
                            opened = descriptor.stat()
                            key = (opened.st_dev, opened.st_ino)
                            if key not in targets:
                                continue
                            with (process_path / "fdinfo" / descriptor.name).open() as info:
                                for line in info:
                                    if line.startswith("flags:") and int(line.split()[1], 8) & os.O_ACCMODE:
                                        writers.add(key)
                        except FileNotFoundError:
                            continue
                with (process_path / "maps").open() as mappings:
                    for line in mappings:
                        fields = line.split(maxsplit=5)
                        if len(fields) >= 5 and "w" in fields[1] and fields[1].endswith("s"):
                            major, minor = (int(part, 16) for part in fields[3].split(":"))
                            key = (os.makedev(major, minor), int(fields[4]))
                            if key in targets:
                                writers.add(key)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                denied.append(process.name)
    return _WriterSnapshot(targets, writers, denied)


def _writer_snapshot(targets: set[tuple[int, int]]) -> _WriterSnapshot:
    if not targets:
        return _WriterSnapshot(targets, set(), [])
    snapshot = _proc_writer_census(targets)
    if not snapshot.denied:
        return snapshot
    # Complete the natural-user census first: root cannot inspect descriptors
    # on user-owned FUSE mounts. Escalate only its inaccessible processes and
    # merge both observations; an inaccessible process still fails closed.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            command = ["sudo", "-n", "--", sys.executable, "-B", "-I", str(Path(__file__).absolute()), "_writer-census", json.dumps(sorted(targets)), json.dumps(snapshot.denied)]
            result = subprocess.run(command, stdout=output, stderr=errors, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EvidenceError(f"cannot establish writer ownership; read-only privileged /proc census unavailable: {error}") from error
        if result.returncode:
            errors.seek(0)
            detail = errors.read(8192).decode("utf-8", errors="replace").strip()
            denied = ",".join(snapshot.denied)
            raise EvidenceError(f"cannot inspect writer ownership for PID {denied}; privileged /proc census failed: {detail}")
        output.seek(0)
        encoded = output.read(65537)
        if len(encoded) > 65536:
            raise EvidenceError("privileged writer census exceeded its bounded result")
        data = json.loads(encoded)
    if not isinstance(data, dict) or not isinstance(data.get("writers"), list) or not isinstance(data.get("denied"), list):
        raise EvidenceError("invalid privileged writer census result")
    writers = {tuple(key) for key in data["writers"]}
    if not writers <= targets or data["denied"]:
        raise EvidenceError("privileged writer census could not establish scoped ownership")
    return _WriterSnapshot(targets, snapshot.writers | writers, [])


def _has_writer(value: os.stat_result, snapshot: _WriterSnapshot | None = None) -> bool:
    key = (value.st_dev, value.st_ino)
    if snapshot is None or key not in snapshot.targets:
        snapshot = _writer_snapshot({key})
    return key in snapshot.writers


@contextmanager
def _index_lock(directory: Path) -> Iterator[None]:
    path = directory / ".evidence-index.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def index_paths(directory: Path) -> tuple[Path, ...]:
    """List legacy and immutable addition indexes, including unsafe symlinks."""
    paths = set(directory.glob("evidence-index.*.json"))
    base = directory / INDEX_NAME
    if os.path.lexists(base):
        paths.add(base)
    return tuple(sorted(paths))


def load_index(directory: Path) -> dict[str, Any]:
    """Merge immutable index publications without accepting ambiguous mappings."""
    files: dict[str, Any] = {}
    stored_names: dict[str, str] = {}
    for path in index_paths(directory):
        index = _read_json(path)
        if not isinstance(index, dict) or index.get("schema") != 1 or not isinstance(index.get("files"), dict):
            raise EvidenceError(f"unsupported evidence index: {path}")
        for logical, record in index["files"].items():
            if (not isinstance(logical, str) or logical in {"", ".", ".."}
                    or Path(logical).name != logical or not isinstance(record, dict)
                    or record.get("stored_path") != logical + ".zst"):
                raise EvidenceError(f"invalid logical evidence index entry: {path}: {logical}")
            stored = record["stored_path"]
            if logical in files or stored in stored_names:
                raise EvidenceError(f"conflicting logical evidence index entries: {path}: {logical}")
            for field in ("original_bytes", "stored_bytes"):
                if type(record.get(field)) is not int or record[field] < 0:
                    raise EvidenceError(f"invalid {field} in evidence index: {path}: {logical}")
            for field in ("sha256", "stored_sha256"):
                if not isinstance(record.get(field), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", record[field]):
                    raise EvidenceError(f"invalid {field} in evidence index: {path}: {logical}")
            files[logical] = record
            stored_names[stored] = logical
    return {"schema": 1, "files": files}


@dataclass
class _PreparedPayload:
    path: Path
    descriptor: int
    original: os.stat_result
    temporary: Path
    report: dict[str, Any]
    stored_sha256: str

    @property
    def stored(self) -> Path:
        return self.path.with_name(self.path.name + ".zst")

    def close(self) -> None:
        os.close(self.descriptor)
        self.temporary.unlink(missing_ok=True)


def _prepare_file(path: Path, *, explicit: bool, writers: _WriterSnapshot | None = None) -> dict[str, Any] | _PreparedPayload:
    path = _absolute(path)
    result: dict[str, Any] = {"path": str(path), "action": "retained", "saved_bytes": 0}
    _no_symlinks(path)
    if _blocked(path) or not _eligible(path, explicit=explicit):
        return {**result, "reason": "not a diagnostic payload"}
    if not path.exists() and os.path.lexists(path.with_name(path.name + ".zst")):
        metadata = payload_metadata(path)
        return {**result, **metadata, "reason": "already compressed"}
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    temporary: Path | None = None
    prepared = None
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            return {**result, "reason": "not a regular file"}
        if not explicit and value.st_mode & 0o111 and "diagnostics" not in path.parts:
            return {**result, "reason": "executable requires explicit finalized selection"}
        if value.st_size < MIN_BYTES:
            return {**result, "reason": "below minimum size", "original_bytes": value.st_size}
        if _has_writer(value, writers):
            return {**result, "reason": "open writer"}
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**result, "reason": "active file lock"}
        stored = path.with_name(path.name + ".zst")
        if os.path.lexists(stored):
            raise EvidenceError(f"stored destination already exists: {stored}")
        original_hash = _hash_fd(descriptor, value.st_size)
        out_fd, name = tempfile.mkstemp(prefix=".evidence-", suffix=".partial", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(out_fd, "wb") as destination, os.fdopen(os.dup(descriptor), "rb") as source:
            _compress_zstd(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        stored_size = temporary.stat().st_size
        saved = value.st_size - stored_size
        details = {"original_bytes": value.st_size, "stored_bytes": stored_size, "sha256": original_hash}
        if saved < MIN_BYTES or saved * 10 < value.st_size:
            return {**result, **details, "reason": "savings below 10 percent and 4096 bytes"}
        _verify_zstd(temporary, original_hash, value.st_size)
        stored_hash = _hash_path(temporary)
        if _identity(os.fstat(descriptor)) != _identity(value) or _identity(path.lstat()) != _identity(value):
            raise EvidenceError(f"input changed during compression: {path}")
        os.chmod(temporary, stat.S_IMODE(value.st_mode))
        os.utime(temporary, ns=(value.st_atime_ns, value.st_mtime_ns))
        with temporary.open("rb") as durable:
            os.fsync(durable.fileno())
        report = {**result, **details, "saved_bytes": saved}
        prepared = _PreparedPayload(path, descriptor, value, temporary, report, stored_hash)
        return prepared
    finally:
        if prepared is None:
            os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _check_prepared(items: list[_PreparedPayload], writers: _WriterSnapshot | None = None) -> None:
    for item in items:
        _no_symlinks(item.path)
        if _identity(os.fstat(item.descriptor)) != _identity(item.original) or _identity(item.path.lstat()) != _identity(item.original):
            raise EvidenceError(f"input changed before unlink: {item.path}")
        if writers is not None and _has_writer(item.original, writers):
            raise EvidenceError(f"input acquired a writer before unlink: {item.path}")


def _publish_directory(items: list[_PreparedPayload], *, dry_run: bool, include_tracked: bool | None = None) -> list[dict[str, Any]]:
    """Publish a bounded directory group, with a fresh census before unlink."""
    directory = items[0].path.parent
    targets = {(item.original.st_dev, item.original.st_ino) for item in items}
    if dry_run:
        _check_prepared(items, _writer_snapshot(targets))
        return [{**item.report, "action": "would-compress"} for item in items]
    with _index_lock(directory):
        _check_prepared(items, _writer_snapshot(targets))
        index = load_index(directory)
        for item in items:
            if item.path.name in index["files"] or os.path.lexists(item.stored):
                raise EvidenceError(f"stored destination or logical index entry already exists: {item.path}")
        additions: dict[str, Any] = {"schema": 1, "files": {}}
        for item in items:
            os.link(item.temporary, item.stored)
            additions["files"][item.path.name] = {
                "stored_path": item.stored.name,
                "original_bytes": item.original.st_size,
                "stored_bytes": item.report["stored_bytes"],
                "sha256": item.report["sha256"],
                "stored_sha256": item.stored_sha256,
            }
        _fsync_directory(directory)
        # Never rewrite metadata: an earlier physical seal may hash any index.
        # A fresh immutable addition carries only this bounded publication.
        index_path = directory / INDEX_NAME
        if os.path.lexists(index_path):
            index_path = directory / f"evidence-index.{uuid.uuid4().hex}.json"
        _atomic_json(index_path, additions, replace=False)
        if include_tracked is not None:
            for item in items:
                if _tracked(item.path, include_tracked=include_tracked):
                    raise EvidenceError(f"tracked evidence acquired working/staged edits or is not opted in: {item.path}")
        _check_prepared(items, _writer_snapshot(targets))
        for item in items:
            # The refreshed writer snapshot is shared only across this bounded
            # unlink group; every pathname/FD identity is checked again in place.
            _check_prepared([item])
            item.path.unlink()
        _fsync_directory(directory)
    return [{**item.report, "action": "compressed", "stored_path": str(item.stored)} for item in items]


def compress_file(path: Path, *, finalized: bool, dry_run: bool = False, explicit: bool = True) -> dict[str, Any]:
    """Archive one finalized payload, keeping raw bytes on any publication error."""
    if not finalized:
        raise EvidenceError("only explicitly finalized evidence may be compressed")
    prepared = _prepare_file(path, explicit=explicit)
    if isinstance(prepared, dict):
        return prepared
    try:
        return _publish_directory([prepared], dry_run=dry_run)[0]
    finally:
        prepared.close()


def _tracked(path: Path, *, include_tracked: bool = False) -> bool:
    # Git remains optional. Without a tracking oracle, keep repository files.
    if not any(os.path.lexists(parent / ".git") for parent in path.parents):
        return False
    command = ["git", "--literal-pathspecs", "-C", str(path.parent)]
    try:
        result = subprocess.run([*command, "ls-files", "--error-unmatch", "--", path.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        return True
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        raise EvidenceError(f"cannot establish tracked status: {path}")
    if not include_tracked:
        return True
    # Check both edges independently: a working file can equal HEAD while the
    # index still carries a user's staged edit that archival must not swallow.
    for arguments in ([], ["--cached", "HEAD"]):
        result = subprocess.run([*command, "diff", "--quiet", "--no-ext-diff", "--no-textconv", *arguments, "--", path.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode in (1, 128):
            return True
        if result.returncode:
            raise EvidenceError(f"cannot establish clean tracked evidence: {path}")
    return False


def _finalized_subtree(path: Path) -> bool:
    directory = path if path.is_dir() else path.parent
    for parent in (directory, *directory.parents):
        if parent.name in {"evidence", "diagnostics"}:
            return True
        manifest = parent / "MANIFEST"
        if manifest.is_file() and not manifest.is_symlink():
            return True
        marker = parent / ".unit.json"
        if marker.is_file() and not marker.is_symlink() and (parent / "manifest.json").is_file():
            if _read_json(marker).get("state") == "retained":
                return True
        if os.path.lexists(parent / ".git"):
            return False
    return False


def _selected(paths: list[Path], recursive: bool, *, include_tracked: bool = False) -> Iterator[tuple[Path, bool]]:
    for raw in paths:
        path = _absolute(raw)
        _no_symlinks(path)
        if _blocked(path, directory=path.is_dir()):
            continue
        if include_tracked and not _finalized_subtree(path):
            raise EvidenceError(f"--include-tracked requires a finalized evidence subtree: {path}")
        if not path.is_dir():
            if not _tracked(path, include_tracked=include_tracked):
                yield path, True
            continue
        if not recursive:
            raise EvidenceError(f"directory requires explicit --recursive: {path}")
        if os.path.lexists(path / ".git"):
            raise EvidenceError(f"repository roots are source trees; select a finalized evidence subtree: {path}")
        # os.walk does not follow symlink directories; reject payload symlinks
        # rather than resolving them into another source/prefix/evidence owner.
        for directory, dirs, files in os.walk(path, followlinks=False):
            parent = Path(directory)
            dirs[:] = [name for name in dirs if not _blocked(parent / name, directory=True) and not (parent / name).is_symlink() and not os.path.lexists(parent / name / ".git")]
            for name in files:
                candidate = parent / name
                if candidate.is_symlink() or not _eligible(candidate, explicit=False) or _tracked(candidate, include_tracked=include_tracked):
                    continue
                yield candidate, False


def verify_stored(path: Path, sha256: str, size: int) -> None:
    """Verify an explicit stored payload against its logical original digest."""
    if not isinstance(size, int) or size < 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise EvidenceError("invalid original SHA256/size for stored evidence")
    stored = resolve_payload(path)
    if stored.suffix == ".zst":
        _verify_zstd(stored, sha256.lower(), size)
    elif stored.stat().st_size != size or _hash_path(stored) != sha256.lower():
        raise EvidenceError(f"stored raw evidence SHA256/size differs from original: {stored}")


def compress_paths(paths: list[Path], *, recursive: bool = False, dry_run: bool = False, include_tracked: bool = False) -> Iterator[dict[str, Any]]:
    """Stage bounded batches; share writer censuses and durable index updates."""
    selected = iter(_selected(paths, recursive, include_tracked=include_tracked))
    while batch := list(itertools.islice(selected, 32)):
        batch = list(dict(batch).items())
        targets = set()
        for path, explicit in batch:
            if not _eligible(path, explicit=explicit):
                continue
            try:
                value = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(value.st_mode) and value.st_size >= MIN_BYTES:
                targets.add((value.st_dev, value.st_ino))
        writers = _writer_snapshot(targets)
        prepared: list[_PreparedPayload] = []
        reports: dict[Path, dict[str, Any]] = {}
        try:
            for path, explicit in batch:
                try:
                    item = _prepare_file(path, explicit=explicit, writers=writers)
                    if isinstance(item, dict):
                        reports[path] = item
                    else:
                        prepared.append(item)
                except (OSError, ValueError, EvidenceError) as error:
                    reports[path] = {"path": str(path), "action": "error", "saved_bytes": 0, "reason": str(error)}
            directories: dict[Path, list[_PreparedPayload]] = {}
            for item in prepared:
                directories.setdefault(item.path.parent, []).append(item)
            for items in directories.values():
                try:
                    for report in _publish_directory(items, dry_run=dry_run, include_tracked=include_tracked):
                        reports[Path(report["path"])] = report
                except (OSError, ValueError, EvidenceError) as error:
                    for item in items:
                        reports[item.path] = {"path": str(item.path), "action": "error", "saved_bytes": 0, "reason": str(error)}
            for path, _ in batch:
                yield reports[path]
        finally:
            for item in prepared:
                item.close()


def resolve_payload(path: Path) -> Path:
    path = _absolute(path)
    _no_symlinks(path)
    if os.path.lexists(path) and not path.is_file():
        raise EvidenceError(f"evidence payload is not a regular file: {path}")
    index = load_index(path.parent)
    logical = path.name[:-4] if path.name.endswith(".zst") else path.name
    record = index["files"].get(logical)
    if record is not None:
        stored = path.parent / record["stored_path"]
        _no_symlinks(stored)
        if not stored.is_file():
            raise EvidenceError(f"missing indexed evidence payload: {stored}")
        if path.name == logical and path.is_file():
            if path.stat().st_size != record["original_bytes"] or _hash_path(path) != record["sha256"]:
                raise EvidenceError(f"raw evidence conflicts with logical index: {path}")
        return stored
    if path.is_file():
        return path
    stored = path.with_name(path.name + ".zst")
    _no_symlinks(stored)
    if not stored.is_file():
        if os.path.lexists(stored):
            raise EvidenceError(f"evidence payload is not a regular file: {stored}")
        raise PayloadNotFoundError(f"missing evidence payload: {path}")
    return stored


def payload_metadata(path: Path) -> dict[str, Any]:
    """Validate a logical reference; report the original size for old manifests."""
    path = _absolute(path)
    stored = resolve_payload(path)
    logical = path.name[:-4] if path.name.endswith(".zst") else path.name
    record = load_index(path.parent)["files"].get(logical)
    if stored == path and record is None:
        return {"bytes": stored.stat().st_size}
    if not isinstance(record, dict) or record.get("stored_path") != stored.name:
        raise EvidenceError(f"missing logical evidence index entry: {path}")
    expected_size, expected_hash = record.get("original_bytes"), record.get("sha256")
    if not isinstance(expected_size, int) or expected_size < 0 or not isinstance(expected_hash, str):
        raise EvidenceError(f"invalid logical evidence index entry: {path}")
    if stored.stat().st_size != record.get("stored_bytes") or _hash_path(stored) != record.get("stored_sha256"):
        raise EvidenceError(f"stored evidence digest/size differs from index: {stored}")
    _verify_zstd(stored, expected_hash, expected_size)
    return {"bytes": expected_size, "stored_bytes": stored.stat().st_size, "stored_path": stored.name, "sha256": expected_hash}


class _DecodedReader(io.RawIOBase):
    def __init__(self, source: BinaryIO) -> None:
        super().__init__()
        self.source = source
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        count = self.source.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
            self.size += count
        return count


@contextmanager
def open_payload(path: Path) -> Iterator[BinaryIO]:
    """Stream a logical payload, verifying indexed stored and decoded bytes."""
    path = _absolute(path)
    stored = resolve_payload(path)
    logical = path.name[:-4] if path.name.endswith(".zst") else path.name
    record = load_index(path.parent)["files"].get(logical)
    if record is not None:
        if stored.stat().st_size != record["stored_bytes"] or _hash_path(stored) != record["stored_sha256"]:
            raise EvidenceError(f"stored evidence digest/size differs from index: {stored}")
    with stored.open("rb") as source:
        if stored.suffix != ".zst":
            yield source
            return
        process = subprocess.Popen([ZSTD, "-q", "-d", "-c"], stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None and process.stderr is not None
        errors = bytearray()

        def drain_errors() -> None:
            # Drain concurrently even after the diagnostic bound, so a failing
            # decoder cannot deadlock on stderr or allocate unbounded memory.
            while chunk := process.stderr.read(8192):
                errors.extend(chunk[:max(0, 8192 - len(errors))])

        collector = threading.Thread(target=drain_errors)
        decoded = _DecodedReader(process.stdout)
        try:
            collector.start()
            with io.BufferedReader(decoded) as reader:
                yield reader
                while reader.read(CHUNK):
                    pass
            status = process.wait()
            collector.join()
            if status:
                raise EvidenceError(f"zstd decode failed for {stored}: {errors.decode(errors='replace')}")
            if record is not None and (decoded.size != record["original_bytes"] or decoded.digest.hexdigest() != record["sha256"]):
                raise EvidenceError(f"decoded evidence digest/size differs from index: {stored}")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
            if collector.ident is not None:
                collector.join()
            process.stderr.close()


def stream_payload(path: Path, destination: BinaryIO) -> None:
    with open_payload(path) as source:
        while chunk := source.read(CHUNK):
            destination.write(chunk)


def _source_path(source: Path) -> Path:
    source = _absolute(source)
    _no_symlinks(source)
    return source


def _open_original(original: Path) -> int:
    if not re.fullmatch(r"/proc/[0-9]+/fd/[0-9]+", str(original)):
        raise EvidenceError("original must name the collector's retained /proc/PID/fd/N")
    descriptor = os.open(original, os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise EvidenceError("retained original FD is not a regular file")
    return descriptor


def _snapshot(descriptor: int) -> dict[str, Any]:
    value = os.fstat(descriptor)
    start = max(0, value.st_size - BOUNDARY_BYTES)
    boundary = os.pread(descriptor, value.st_size - start, start)
    if len(boundary) != value.st_size - start:
        raise EvidenceError("log changed while snapshotting boundary")
    return {"dev": value.st_dev, "ino": value.st_ino, "size": value.st_size, "mtime_ns": value.st_mtime_ns, "boundary_start": start, "boundary_sha256": hashlib.sha256(boundary).hexdigest()}


def log_start(source: Path, state_path: Path, original: Path) -> dict[str, Any]:
    source = _source_path(source)
    state_path = _absolute(state_path)
    _no_symlinks(state_path)
    state: dict[str, Any] = {"schema": 1, "source": str(source), "original_handle": str(original), "original": None, "complete": False, "gap": False, "transition": "started", "reason": "snapshot recorded", "captures": [], "missing": []}
    descriptor: int | None = None
    try:
        try:
            descriptor = _open_original(original)
        except FileNotFoundError:
            if os.path.lexists(source):
                raise EvidenceError("existing source has no retained original FD")
            state["reason"] = "source absent at snapshot"
            state["missing"] = ["source-at-start"]
        if descriptor is not None:
            value = source.stat()
            if not stat.S_ISREG(value.st_mode):
                raise EvidenceError("source log is not a regular file")
            snapshot = _snapshot(descriptor)
            if (value.st_dev, value.st_ino) != (snapshot["dev"], snapshot["ino"]):
                raise EvidenceError("retained original FD does not match source")
            state["original"] = snapshot
        _atomic_json(state_path, state, replace=False)
        return state
    except Exception as error:
        state.update(complete=False, gap=True, transition="error", reason=str(error))
        if not os.path.lexists(state_path):
            _atomic_json(state_path, state, replace=False)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _same_inode(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (left["dev"], left["ino"]) == (right["dev"], right["ino"])


def _boundary_matches(descriptor: int, snapshot: dict[str, Any]) -> bool:
    length = snapshot["size"] - snapshot["boundary_start"]
    boundary = os.pread(descriptor, length, snapshot["boundary_start"])
    return len(boundary) == length and hashlib.sha256(boundary).hexdigest() == snapshot["boundary_sha256"]


def _copy_slice(descriptor: int, destination: BinaryIO, start: int, end: int, role: str) -> dict[str, Any]:
    value = os.fstat(descriptor)
    capture = {"role": role, "dev": value.st_dev, "ino": value.st_ino, "start": start, "end": end, "bytes": 0}
    offset = start
    while offset < end:
        chunk = os.pread(descriptor, min(CHUNK, end - offset), offset)
        if not chunk:
            raise EvidenceError("log shortened during finite capture; possible gap")
        destination.write(chunk)
        offset += len(chunk)
        capture["bytes"] += len(chunk)
    return capture


def log_finish(source: Path, state_path: Path, original: Path, output: Path) -> dict[str, Any]:
    source = _source_path(source)
    state_path, output = _absolute(state_path), _absolute(output)
    _no_symlinks(output)
    state = _read_json(state_path)
    if not isinstance(state, dict) or state.get("schema") != 1 or state.get("source") != str(source) or state.get("original_handle") != str(original):
        raise EvidenceError("log state is bound to a different source/original FD")
    if state.get("transition") != "started":
        raise EvidenceError("log state has already been finished")
    if os.path.lexists(output):
        raise EvidenceError(f"log output already exists: {output}")
    state.update(complete=False, gap=False, transition="append", reason="finite source-bound capture", captures=[], missing=list(state.get("missing", [])))
    descriptors: list[int] = []
    temporary: Path | None = None
    try:
        prior = state.get("original")
        old_descriptor = None
        old_now = None
        if prior is not None:
            try:
                old_descriptor = _open_original(original)
            except FileNotFoundError as error:
                state["missing"].append("original-fd-at-finish")
                raise EvidenceError("collector's retained original FD is missing") from error
            descriptors.append(old_descriptor)
            old_now = _snapshot(old_descriptor)
            if not _same_inode(prior, old_now):
                raise EvidenceError("retained original FD inode changed")
        current_descriptor = None
        current = None
        try:
            current_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            descriptors.append(current_descriptor)
            if not stat.S_ISREG(os.fstat(current_descriptor).st_mode):
                raise EvidenceError("current source log is not a regular file")
            current = _snapshot(current_descriptor)
        except FileNotFoundError:
            state.update(transition="missing", gap=True, reason="source missing at finish")
            state["missing"].append("source-at-finish")
        out_fd, name = tempfile.mkstemp(prefix=".evidence-", suffix=".partial", dir=output.parent)
        temporary = Path(name)
        with os.fdopen(out_fd, "wb") as destination:
            if prior is not None:
                assert old_descriptor is not None and old_now is not None
                reset = old_now["size"] < prior["size"] or not _boundary_matches(old_descriptor, prior)
                start = 0 if reset else prior["size"]
                if reset:
                    state.update(transition="reset", gap=True, reason="original inode truncated or boundary rewritten; pre-reset bytes may be lost")
                state["captures"].append(_copy_slice(old_descriptor, destination, start, old_now["size"], "original"))
                if not _boundary_matches(old_descriptor, old_now):
                    raise EvidenceError("original log boundary changed during capture; possible gap")
            if current is not None and (old_now is None or not _same_inode(old_now, current)):
                assert current_descriptor is not None
                state["captures"].append(_copy_slice(current_descriptor, destination, 0, current["size"], "current"))
                if not _boundary_matches(current_descriptor, current):
                    raise EvidenceError("current log boundary changed during capture; possible gap")
                if not state["gap"]:
                    state["transition"] = "created" if prior is None else "rotate"
                    state["reason"] = "source created after absent snapshot" if prior is None else "retained original tail and rotated current inode captured"
            destination.flush()
            os.fsync(destination.fileno())
        os.link(temporary, output)
        _fsync_directory(output.parent)
        state["complete"] = not state["gap"]
    except Exception as error:
        state.update(complete=False, gap=True, transition="error", reason=str(error))
        # Preserve bounded partial proof when a read failed, but never replace
        # a pre-existing output or follow a symlink to another owner.
        if temporary is not None and not os.path.lexists(output):
            os.link(temporary, output)
            _fsync_directory(output.parent)
        raise
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _atomic_json(state_path, state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compress = commands.add_parser("compress", help="archive explicitly finalized diagnostic payloads")
    compress.add_argument("paths", nargs="+", type=Path)
    compress.add_argument("--recursive", action="store_true")
    compress.add_argument("--dry-run", action="store_true")
    compress.add_argument("--include-tracked", action="store_true", help="archive only clean tracked diagnostics within a finalized evidence subtree")
    cat = commands.add_parser("cat", help="stream raw or zstd evidence without buffering the payload")
    cat.add_argument("path", type=Path)
    compact = commands.add_parser("compact-runtime", help="preserve compact runtime proof without deleting its original unit")
    compact.add_argument("unit", type=Path)
    compact.add_argument("--archive-root", required=True, type=Path)
    verify = commands.add_parser("verify", help="verify indexed payloads and logical SHA256SUMS references")
    verify.add_argument("root", type=Path)
    verify.add_argument("--recursive", action="store_true")
    verify.add_argument("--write-manifest", action="store_true", help="write a new physical SHA256SUMS after successful verification; never overwrite one")
    for name in ("log-start", "log-finish"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--state", required=True, type=Path)
        command.add_argument("--original", required=True, type=Path)
        if name == "log-finish":
            command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            if __package__:
                from .evidence_verify import verify_archive
            else:
                from evidence_verify import verify_archive
            result = verify_archive(args.root, recursive=args.recursive, write_manifest=args.write_manifest)
            print(json.dumps(result, sort_keys=True))
            return int(result["failed"])
        if args.command == "compact-runtime":
            if __package__:
                from .evidence_retention import compact_runtime_unit
            else:
                from evidence_retention import compact_runtime_unit
            print(json.dumps(compact_runtime_unit(args.unit, args.archive_root), sort_keys=True))
            return 0
        if args.command == "compress":
            saved = 0
            failed = False
            # Continue independent selected files after an error, but make the
            # archival command fail rather than advertising a complete bundle.
            for report in compress_paths(args.paths, recursive=args.recursive, dry_run=args.dry_run, include_tracked=args.include_tracked):
                saved += report["saved_bytes"]
                failed = failed or report["action"] == "error"
                print(json.dumps(report, sort_keys=True))
            print(json.dumps({"action": "summary", "dry_run": args.dry_run, "saved_bytes": saved, "failed": failed}, sort_keys=True))
            return int(failed)
        if args.command == "cat":
            stream_payload(args.path, sys.stdout.buffer)
            return 0
        if args.command == "log-start":
            state = log_start(args.source, args.state, args.original)
        else:
            state = log_finish(args.source, args.state, args.original, args.output)
        print(json.dumps(state, sort_keys=True))
        return int(bool(state["gap"]))
    except (OSError, ValueError, EvidenceError) as error:
        print(f"darling-evidence: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_writer-census":
        requested = json.loads(sys.argv[2])
        if not isinstance(requested, list) or not 0 < len(requested) <= 32 or any(
            not isinstance(key, list) or len(key) != 2 or any(not isinstance(number, int) or number < 0 for number in key)
            for key in requested
        ):
            raise SystemExit("invalid bounded writer-census inode set")
        pids = json.loads(sys.argv[3])
        if not isinstance(pids, list) or not pids or any(not isinstance(pid, str) or not pid.isdecimal() for pid in pids):
            raise SystemExit("invalid writer-census process set")
        snapshot = _proc_writer_census({tuple(key) for key in requested}, set(pids))
        print(json.dumps({"writers": sorted(snapshot.writers), "denied": snapshot.denied}, sort_keys=True))
    else:
        raise SystemExit(main())

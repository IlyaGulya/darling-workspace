"""Preserve inspectable runtime proof without retaining generated build forests."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

if __package__:
    from .evidence_storage import (
        EvidenceError, compress_file, compress_paths, open_writers,
        payload_metadata, verify_stored,
    )
    from .evidence_verify import verify_archive
else:
    from evidence_storage import (
        EvidenceError, compress_file, compress_paths, open_writers,
        payload_metadata, verify_stored,
    )
    from evidence_verify import verify_archive


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> tuple[int, ...]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _copy_verified(source: Path, target: Path) -> str:
    before = _identity(source)
    if not stat.S_ISREG(source.stat().st_mode):
        raise EvidenceError(f"cannot archive non-regular evidence: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            created = True
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        digest = _sha256(target)
        if before != _identity(source) or digest != _sha256(source):
            raise EvidenceError(f"evidence changed while preserving it: {source}")
        shutil.copystat(source, target)
        return digest
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def _runtime_image(path: Path) -> bool:
    if path.suffix in {".o", ".a", ".bc", ".class"}:
        return False
    with path.open("rb") as stream:
        header = stream.read(32)
        if header[:4] == b"\x7fELF" and len(header) >= 18:
            order = "<" if header[5] == 1 else ">"
            return struct.unpack_from(order + "H", header, 16)[0] in {2, 3}
        magic = header[:4]
        if magic in {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"} and len(header) >= 16:
            order = ">" if magic[:1] == b"\xca" else "<"
            fat64 = magic in {b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
            offset = struct.unpack_from(order + ("Q" if fat64 else "I"), header, 16)[0]
            stream.seek(offset)
            header = stream.read(16)
            magic = header[:4]
        if magic in {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"} and len(header) >= 16:
            order = "<" if magic[:1] in {b"\xcf", b"\xce"} else ">"
            return struct.unpack_from(order + "I", header, 12)[0] in {2, 6, 8}
    return False


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _preserve_images(unit: Path, images: Path, record_root: Path) -> dict[str, Any]:
    build = unit / "build"
    candidates: set[Path] = set()
    for directory, dirs, files in os.walk(build / "src", followlinks=False):
        dirs[:] = [name for name in dirs if name not in {"CMakeFiles", ".git"}]
        candidates.update(Path(directory) / name for name in files)
    pending = []
    new_images = []
    verified = set()
    for source in sorted(candidates):
        if not source.is_file() or not _runtime_image(source):
            continue
        digest = _sha256(source)
        images.mkdir(parents=True, exist_ok=True)
        image = images / (digest + ".image")
        compressed = image.with_name(image.name + ".zst")
        if not image.exists() and not compressed.exists():
            if _copy_verified(source, image) != digest:
                image.unlink()
                raise EvidenceError(f"runtime image changed before archival: {source}")
            new_images.append(image)
            verified.add(digest)
        pending.append((source.relative_to(build), digest, source.stat().st_size))
    for _ in compress_paths(new_images):
        pass
    records = {}
    for relative, digest, size in pending:
        image = images / (digest + ".image")
        compressed = image.with_name(image.name + ".zst")
        stored = compressed if compressed.exists() else image
        if digest not in verified:
            if stored == image:
                if _sha256(stored) != digest:
                    raise EvidenceError(f"existing retained runtime image is corrupt: {stored}")
            else:
                metadata = payload_metadata(image)
                if metadata["sha256"] != digest or metadata["bytes"] != size:
                    raise EvidenceError(f"retained runtime image differs from its source: {relative}")
            verified.add(digest)
        records[str(relative)] = {
            "sha256": digest, "original_bytes": size,
            "stored_path": str(stored.relative_to(record_root)),
        }
    return records


def _preserve_build_diagnostics(unit: Path, destination: Path, record_root: Path) -> dict[str, Any]:
    selected = []
    staged = []
    suffixes = {".log", ".out", ".err", ".stdout", ".stderr", ".trace", ".strace", ".core", ".dmp", ".dump", ".perf", ".pcap", ".pcapng", ".i", ".ii"}
    for directory, dirs, files in os.walk(unit / "build", followlinks=False):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in files:
            source = Path(directory) / name
            if not (source.suffix.lower() in suffixes or name.startswith("core.")
                    or name in {"core", "perf.data", "CMakeConfigureLog.yaml", "compile_commands.json"}):
                continue
            if not source.is_file():
                raise EvidenceError(f"build diagnostic is unavailable: {source}")
            digest = _sha256(source)
            suffix = ".core" if name == "core" or name.startswith("core.") else source.suffix.lower()
            if suffix in {".i", ".ii"}:
                suffix = ".log"
            target = destination / (digest + suffix)
            if not target.exists() and not target.with_name(target.name + ".zst").exists():
                if _copy_verified(source, target) != digest:
                    target.unlink()
                    raise EvidenceError(f"build diagnostic changed before archival: {source}")
                staged.append(target)
            selected.append((source.relative_to(unit / "build"), target, digest, source.stat().st_size))
    for _ in compress_paths(staged):
        pass
    records = {}
    for relative, target, digest, size in selected:
        stored = target.with_name(target.name + ".zst")
        stored = stored if stored.exists() else target
        verify_stored(stored, digest, size)
        records[str(relative)] = {"sha256": digest, "stored_path": str(stored.relative_to(record_root))}
    return records


def compact_build_payload(unit: Path) -> dict[str, Any]:
    """Called only by the locked manifest-native failure owner before publishing."""
    build = unit / "build"
    if not build.exists():
        return {}
    if build.is_symlink() or not build.is_dir():
        raise EvidenceError(f"generated build path is not a regular directory: {build}")
    records = _preserve_images(unit, unit / "runtime-images", unit)
    diagnostics = _preserve_build_diagnostics(unit, unit / "compiler-diagnostics", unit)
    metadata = unit / "CMakeCache.txt"
    if (build / "CMakeCache.txt").is_file():
        _copy_verified(build / "CMakeCache.txt", metadata)
    result = {
        "schema": 1, "runtime_images": records, "compiler_diagnostics": diagnostics,
        "omitted": ["generated build objects and resources"],
    }
    _write_json(unit / "build-artifacts.json", result)
    for directory in (unit / "runtime-images", unit / "compiler-diagnostics", unit):
        if directory.exists():
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    shutil.rmtree(build)
    return result


def compact_runtime_unit(unit: Path, archive_root: Path) -> dict[str, Any]:
    """Archive closed diagnostics, provenance and content-deduplicated runtime images.

    Source/build trees are generated inputs, not proof archives. Produced ELF and
    Mach-O runtime images remain available for symbolization, unlike object files.
    The source unit is never modified or removed; West's ownership-aware GC is a
    separate, explicit operation after successful archival.
    """
    unit = unit.absolute()
    archive_root = archive_root.absolute()
    if unit.is_symlink() or archive_root.is_symlink() or unit == archive_root or unit in archive_root.parents:
        raise EvidenceError("archive must be outside the runtime evidence unit")
    marker = unit / ".unit.json"
    manifest = unit / "manifest.json"
    owned = False
    if marker.is_file():
        value = json.loads(marker.read_text())
        owned = value.get("schema") == 1 and value.get("owner") == "runtime-evidence"
    if manifest.is_file():
        value = json.loads(manifest.read_text())
        owned = owned or (value.get("schema") == 1 and value.get("status") == "failed" and isinstance(value.get("paths"), dict))
    if not owned:
        raise EvidenceError(f"no runtime evidence ownership record: {unit}")
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / unit.name
    if destination.exists():
        raise EvidenceError(f"archive destination already exists: {destination}")
    with (unit / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise EvidenceError(f"runtime evidence is still active: {unit}") from error
        temporary = Path(tempfile.mkdtemp(prefix=".compact-", dir=archive_root))
        records: dict[str, Any] = {}
        image_records: dict[str, Any] = {}
        try:
            for directory, dirs, files in os.walk(unit, followlinks=False):
                relative = Path(directory).relative_to(unit)
                if relative == Path("."):
                    dirs[:] = [name for name in dirs if name not in {"source", "build"}]
                for name in dirs:
                    if (Path(directory) / name).is_symlink():
                        raise EvidenceError(f"cannot omit a symlinked diagnostic directory: {Path(directory) / name}")
                for name in files:
                    if name == ".lock":
                        continue
                    source = Path(directory) / name
                    relative_file = source.relative_to(unit)
                    target = temporary / ("source-SHA256SUMS" if relative_file == Path("SHA256SUMS") else relative_file)
                    digest = _copy_verified(source, target)
                    compression = compress_file(target, finalized=True)
                    stored = Path(compression.get("stored_path", target))
                    records[str(relative_file)] = {"sha256": digest, "stored_path": str(stored.relative_to(temporary))}
            image_records = _preserve_images(unit, archive_root / "runtime-images", archive_root)
            build_diagnostics = _preserve_build_diagnostics(unit, temporary / "compiler-diagnostics", temporary)
            build_cache = unit / "build" / "CMakeCache.txt"
            if build_cache.is_file():
                _copy_verified(build_cache, temporary / "CMakeCache.txt")
            result = {
                "schema": 1, "source_unit": str(unit), "archive": str(destination),
                "retained_files": records, "runtime_images": image_records, "compiler_diagnostics": build_diagnostics,
                "omitted": ["generated build objects and resources", "generated source worktrees"],
            }
            _write_json(temporary / "retention.json", result)
            checksums = []
            for path in sorted(temporary.rglob("*")):
                if path.is_file():
                    checksums.append(f"{_sha256(path)}  {path.relative_to(temporary)}\n")
            (temporary / "SHA256SUMS").write_text("".join(checksums))
            temporary.rename(destination)
            return result
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


# ---------------------------------------------------------------------------
# Finalized managed-run evidence and bounded run-store retention.
# ---------------------------------------------------------------------------

RUN_RECORD_NAME = "run.json"
RUN_LOCK_NAME = ".lock"
RUN_STORE_DIRNAME = "runs"
# Durable retirement journal and its own trash live beside the run store, never
# inside a victim run directory. The completed journal is the ledger that keeps
# a runtime pin alive until the caller acknowledges the removal.
RETIREMENT_DIRNAME = "retired-evidence"
RETIREMENT_LOCK_NAME = ".lock"
RETIREMENT_TRASH_PREFIX = ".trash-"
PAYLOAD_DIRNAME = "payload"
PAYLOAD_MANIFEST = "SHA256SUMS"
JOB_MARKER_NAME = ".west-job-owned.json"
JOB_FINALIZED_NAME = "finalized.json"
JOB_LOCK_NAME = ".west-job.lock"
# Observers hold this shared for their whole observation and final delivery;
# pruning needs it exclusive in addition to the producer's lifetime lock. The
# producer itself never takes it, so following a live job cannot wait for it.
JOB_READER_LOCK_NAME = ".west-job.readers.lock"

# Only evidence material beneath a lane's DW_STATE_ROOT is selected. Generated
# forests are named here so a state root with a large cache cannot be copied by
# accident; an unrecognised top-level entry is an ownership error, not a reason
# to guess.
_CAPTURE_GENERATED_DIRS = frozenset({
    ".cache", ".git", ".snapshotter", "__pycache__", "build", "cache", "caches",
    "node_modules", "prefix", "prefixes", "sdk", "SDK", "target", "tmp",
    "toolchains", "run",
})
_CAPTURE_STATE_DIRS = frozenset({
    "activity-logs.d", "boot-runs", "bundles", "compiler-diagnostics", "cores",
    "diagnostics", "evidence", "logs", "receipts", "scratch",
})


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(path)))


def _reject_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise EvidenceError(f"symlink path is not finalized evidence: {component}")


def _owned_directory(path: Path, label: str) -> Path:
    absolute = _absolute(path)
    _reject_symlinks(absolute)
    try:
        info = absolute.stat()
    except OSError as error:
        raise EvidenceError(f"{label} is unavailable: {absolute}: {error}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise EvidenceError(f"{label} is not a directory: {absolute}")
    if info.st_uid != os.geteuid():
        raise EvidenceError(f"{label} is not owned by this user: {absolute}")
    return absolute


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_json(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _reject_symlinks(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EvidenceError(f"{label} is unavailable: {path}: {error}") from error
    except ValueError as error:
        raise EvidenceError(f"{label} is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} is not a JSON object: {path}")
    return value


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _collect_tree(root: Path, prefix: Path, entries: list[tuple[Path, Path, bool]],
                  omitted: list[str]) -> None:
    """Collect regular files and symlink-target bytes, rejecting source forests."""
    if root.is_symlink():
        raise EvidenceError(f"cannot capture a symlinked evidence directory: {root}")
    if not root.is_dir():
        raise EvidenceError(f"evidence path is not a directory: {root}")
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        relative_parent = parent.relative_to(root)
        kept: list[str] = []
        for name in dirs:
            child = parent / name
            if child.is_symlink():
                raise EvidenceError(f"cannot capture a symlinked evidence directory: {child}")
            if os.path.lexists(child / ".git"):
                raise EvidenceError(f"cannot archive a Git worktree in evidence: {child}")
            if name == ".git":
                raise EvidenceError(f"cannot archive Git metadata in evidence: {child}")
            if name in ("source", "src", "worktrees"):
                raise EvidenceError(f"cannot archive an unowned source/worktree payload: {child}")
            if name in _CAPTURE_GENERATED_DIRS:
                omitted.append(str(prefix / relative_parent / name))
                continue
            kept.append(name)
        dirs[:] = kept
        for name in files:
            child = parent / name
            if name == ".git":
                raise EvidenceError(f"cannot archive Git metadata in evidence: {child}")
            if os.path.lexists(child / ".git"):
                raise EvidenceError(f"cannot archive a Git worktree in evidence: {child}")
            relative = prefix / relative_parent / name
            if child.is_symlink():
                try:
                    info = child.stat()
                except FileNotFoundError as error:
                    raise EvidenceError(f"required symlink target is missing: {child}") from error
                if not stat.S_ISREG(info.st_mode):
                    raise EvidenceError(f"required symlink target is not a regular file: {child}")
                entries.append((child, relative, True))
            else:
                try:
                    info = child.stat()
                except OSError as error:
                    raise EvidenceError(f"cannot read evidence payload: {child}: {error}") from error
                if not stat.S_ISREG(info.st_mode):
                    raise EvidenceError(f"evidence payload is not a regular file: {child}")
                entries.append((child, relative, False))


def _collect_state(state: Path, entries: list[tuple[Path, Path, bool]], omitted: list[str]) -> None:
    if state.is_symlink() or not state.is_dir():
        raise EvidenceError(f"lane state is not a real directory: {state}")
    for entry in sorted(state.iterdir(), key=lambda item: item.name):
        name = entry.name
        if entry.is_symlink():
            raise EvidenceError(f"cannot capture a symlinked lane-state entry: {entry}")
        if entry.is_dir():
            if name == ".git":
                raise EvidenceError(f"cannot archive Git metadata in lane state: {entry}")
            if name in ("source", "src", "worktrees"):
                raise EvidenceError(f"cannot archive an unowned source/worktree payload: {entry}")
            if name in _CAPTURE_GENERATED_DIRS:
                omitted.append(str(Path("state") / name))
                continue
            # The guest-C runner removes each temporary fixture, but leaves its
            # shared container. Only the empty container is safe to discard.
            if name == "guest-c-fixtures" and not any(entry.iterdir()):
                continue
            if name in _CAPTURE_STATE_DIRS or name.startswith(("runtime-evidence-", ".inflight-")):
                _collect_tree(entry, Path("state") / name, entries, omitted)
                continue
            raise EvidenceError(f"unknown lane state payload will not be silently omitted: {entry}")
        if not entry.is_file():
            raise EvidenceError(f"lane state contains an unsupported entry: {entry}")
        if name == ".git":
            raise EvidenceError(f"cannot archive Git metadata in lane state: {entry}")
        entries.append((entry, Path("state") / name, False))


def _lane_record(lane: Path) -> dict[str, Any]:
    marker = lane / "lane.json"
    record = _read_json(marker, "lane record")
    if not isinstance(record.get("schema"), int):
        raise EvidenceError(f"lane record has no usable schema: {marker}")
    return record


def _git_output(source: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60,
        )
    except FileNotFoundError as error:
        raise EvidenceError(f"cannot establish source identity without git: {source}") from error
    except subprocess.TimeoutExpired as error:
        raise EvidenceError(f"source identity command timed out: {source}") from error
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:2048]
        raise EvidenceError(f"cannot establish source identity for {source}: {detail}")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _source_identity(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("source")
    if raw is None:
        return {"recorded": False}
    if isinstance(raw, str):
        path = Path(raw)
    elif isinstance(raw, dict):
        value = raw.get("path")
        if not value:
            return {"recorded": False}
        path = Path(str(value))
    else:
        raise EvidenceError("lane source record is not a path or mapping")
    path = _absolute(path)
    if path.is_symlink() or not path.is_dir():
        raise EvidenceError(f"recorded source is unavailable or not a real directory: {path}")
    git_dir = _git_output(path, "rev-parse", "--git-dir")
    top = _git_output(path, "rev-parse", "--show-toplevel")
    revision = _git_output(path, "rev-parse", "HEAD")
    status = _git_output(path, "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal")
    return {
        "recorded": True, "path": str(path), "git_dir": git_dir, "top_level": top,
        "revision": revision, "dirty": bool(status), "worktree": (path / ".git").is_file(),
    }


def _payload_files(payload: Path) -> dict[str, Path]:
    """Map every sealed regular file, rejecting symlinks and special entries."""
    files: dict[str, Path] = {}
    for directory, dirs, names in os.walk(payload, followlinks=False):
        base = Path(directory)
        for name in dirs:
            child = base / name
            if child.is_symlink():
                raise EvidenceError(f"sealed payload contains a symlinked directory: {child}")
        for name in names:
            child = base / name
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise EvidenceError(f"sealed payload entry is not a regular file: {child}")
            files[child.relative_to(payload).as_posix()] = child
    return files


def _digest_text(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdefABCDEF" for character in value))


def _run_binding(run_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Prove that the managed record owns this exact run directory generation."""
    info = run_dir.lstat()
    identity = record.get("directory")
    if (record.get("schema") != 1 or record.get("kind") != "managed-lane-run"
            or record.get("path") != str(run_dir) or record.get("uid") != os.geteuid()
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or not isinstance(record.get("id"), str) or not record["id"]
            or not isinstance(record.get("root"), str) or not record["root"]
            or not isinstance(identity, dict)
            or identity.get("device") != info.st_dev or identity.get("inode") != info.st_ino):
        raise EvidenceError("managed run record does not own this directory")
    return {
        "id": record["id"], "path": str(run_dir), "root": record["root"],
        "directory": {"device": info.st_dev, "inode": info.st_ino},
        "argv": record.get("argv"), "runtime_root": record.get("runtime_root"),
    }


def _recorded_source_path(record: dict[str, Any]) -> str | None:
    raw = record.get("source")
    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw
    elif isinstance(raw, dict):
        value = raw.get("path")
        if value is None:
            return None
    else:
        raise EvidenceError("lane source record is not a path or mapping")
    if not isinstance(value, str) or not value:
        raise EvidenceError("lane source record has no usable path")
    return str(_absolute(Path(value)))


def _capture_lane_identity(lane: Path, lane_record: dict[str, Any],
                           run_record: dict[str, Any]) -> dict[str, Any]:
    """Bind a live lane record and directory generation to its managed run."""
    info = lane.lstat()
    managed = lane_record.get("managed_run")
    runtime = lane_record.get("runtime")
    selected = run_record.get("lane")
    if (lane.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or not isinstance(selected, dict) or selected.get("path") != str(lane)
            or selected.get("name") != lane.name or selected.get("root") != str(lane.parent)
            or selected.get("token") != run_record.get("id")
            or not isinstance(managed, dict)
            or managed.get("id") != run_record.get("id")
            or managed.get("path") != run_record.get("path")
            or (managed.get("device"), managed.get("inode")) != (info.st_dev, info.st_ino)
            or ("device" in selected and selected["device"] != info.st_dev)
            or ("inode" in selected and selected["inode"] != info.st_ino)
            or not isinstance(runtime, dict)
            or runtime.get("root") != run_record.get("runtime_root")):
        raise EvidenceError(f"lane record does not belong to this managed run: {lane}")
    return {
        "path": str(lane), "name": lane.name, "root": str(lane.parent),
        "token": run_record["id"], "device": info.st_dev, "inode": info.st_ino,
        "runtime_root": runtime.get("root"),
        "source_path": _recorded_source_path(lane_record),
    }


_STARTED_CAPTURE_FILES = (
    "lane.json", RUN_RECORD_NAME, "command/invocation.json", "command/outcome.json",
)
_UNSTARTED_CAPTURE_FILES = (RUN_RECORD_NAME, "command/outcome.json")
_SEAL_GENERATED_FILES = frozenset({"capture.json", PAYLOAD_MANIFEST})


def _capture_manifest(payload: Path) -> Path:
    if payload.is_symlink() or not payload.is_dir():
        raise EvidenceError(f"retained payload is not a real directory: {payload}")
    manifest = payload / PAYLOAD_MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise EvidenceError(f"retained payload has no sealed manifest: {payload}")
    verified = verify_archive(payload, recursive=True)
    if verified["failed"]:
        raise EvidenceError(f"retained payload failed verification: {verified['errors']}")
    # A checksum that matches nothing is not proof of a complete capture, and
    # verify_archive reports uncovered files separately rather than failing.
    if not verified.get("checksums"):
        raise EvidenceError(f"retained payload has an empty or unused manifest: {payload}")
    if verified.get("unsealed_files"):
        raise EvidenceError(
            "retained payload has files outside its manifest: "
            + ", ".join(verified.get("unsealed_paths", [])[:16])
        )
    return manifest


def _read_capture(payload: Path) -> dict[str, Any]:
    capture = _read_json(payload / "capture.json", "sealed capture record")
    if (capture.get("schema") != 1 or capture.get("kind") != "managed-run-evidence"
            or capture.get("complete") is not True):
        raise EvidenceError("sealed capture is not a complete managed-run record")
    return capture


def _sealed_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EvidenceError(f"{label} is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} is not a JSON object: {path}")
    return value


def _capture_files(capture: dict[str, Any], payload: Path) -> dict[str, dict[str, Any]]:
    declared = capture.get("files")
    if not isinstance(declared, list) or not declared:
        raise EvidenceError("sealed capture has no captured-file inventory")
    records: dict[str, dict[str, Any]] = {}
    for entry in declared:
        if not isinstance(entry, dict):
            raise EvidenceError("sealed capture file entry is invalid")
        relative = entry.get("path")
        digest, size, symlink = entry.get("sha256"), entry.get("bytes"), entry.get("symlink")
        if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
                or ".." in Path(relative).parts or "\\" in relative
                or not _digest_text(digest) or type(size) is not int or size < 0
                or not isinstance(symlink, bool)):
            raise EvidenceError("sealed capture file entry is invalid")
        if relative in records:
            raise EvidenceError(f"sealed capture lists a file twice: {relative}")
        path = payload / relative
        if path.is_symlink() or not path.is_file():
            raise EvidenceError(f"sealed capture file is missing: {relative}")
        info = path.stat()
        if info.st_size != size or _sha256(path) != digest.lower():
            raise EvidenceError(f"sealed capture bytes differ from its record: {relative}")
        records[relative] = entry
    present = set(_payload_files(payload))
    unexpected = present - set(records) - _SEAL_GENERATED_FILES
    if unexpected:
        raise EvidenceError(
            "sealed payload contains unrecorded files: " + ", ".join(sorted(unexpected)[:16])
        )
    missing = set(records) - present
    if missing:
        raise EvidenceError(
            "sealed capture lists missing files: " + ", ".join(sorted(missing)[:16])
        )
    return records


def _validate_control_records(payload: Path, records: dict[str, dict[str, Any]], run_dir: Path,
                              record: dict[str, Any], mode: str) -> None:
    """The sealed control records must still describe this run's real result."""
    outcome = _sealed_json(payload / "command" / "outcome.json", "sealed run outcome")
    if outcome.get("run_id") != record.get("id"):
        raise EvidenceError("sealed outcome belongs to another managed run")
    if outcome.get("workload_rc") != record.get("workload_rc"):
        raise EvidenceError("sealed outcome rc differs from the managed record")
    if mode == "unstarted":
        if outcome.get("command_started") is not False:
            raise EvidenceError("sealed unstarted outcome claims a launched command")
    else:
        if outcome.get("command_started") is not True:
            raise EvidenceError("sealed started outcome lost its command provenance")
    if "command/invocation.json" in records:
        invocation = _sealed_json(payload / "command" / "invocation.json", "sealed invocation")
        if (invocation.get("argv") != record.get("argv")
                or ("run_id" in invocation and invocation["run_id"] != record.get("id"))
                or (mode == "unstarted" and "command_started" in invocation
                    and invocation["command_started"] is not False)):
            raise EvidenceError("sealed invocation does not match the managed run argv")
    sealed_record = _sealed_json(payload / RUN_RECORD_NAME, "sealed run record")
    binding = _run_binding(run_dir, record)
    if (sealed_record.get("id") != binding["id"] or sealed_record.get("path") != binding["path"]
            or sealed_record.get("directory") != binding["directory"]
            or sealed_record.get("argv") != record.get("argv")):
        raise EvidenceError("sealed run record is not this run generation")
    for name in ("command.stdout", "command.stderr"):
        live = run_dir / name
        if not live.is_file() or live.is_symlink():
            continue
        entry = records.get(f"command/{name}")
        info = live.lstat()
        if entry is None or entry["bytes"] != info.st_size or entry["sha256"] != _sha256(live):
            raise EvidenceError(f"sealed {name} does not preserve the closed command bytes")


def _validate_lane_capture(payload: Path, capture: dict[str, Any],
                           record: dict[str, Any],
                           lane: Path | None, lane_record: dict[str, Any] | None) -> None:
    selected = record.get("lane")
    identity = capture.get("lane_identity")
    if not isinstance(selected, dict) or not isinstance(identity, dict):
        raise EvidenceError("sealed capture has no lane identity")
    if (capture.get("lane") != selected.get("path")
            or identity.get("path") != selected.get("path")
            or identity.get("name") != selected.get("name")
            or identity.get("root") != selected.get("root")
            or identity.get("token") != selected.get("token")
            or identity.get("token") != record.get("id")
            or identity.get("device") != selected.get("device")
            or identity.get("inode") != selected.get("inode")
            or identity.get("runtime_root") != record.get("runtime_root")):
        raise EvidenceError("sealed capture is bound to another lane generation")
    sealed_lane = _sealed_json(payload / "lane.json", "sealed lane record")
    managed = sealed_lane.get("managed_run")
    runtime = sealed_lane.get("runtime")
    if (not isinstance(sealed_lane.get("schema"), int) or not isinstance(managed, dict)
            or managed.get("id") != record.get("id")
            or managed.get("path") != record.get("path")
            or (managed.get("device"), managed.get("inode"))
                != (selected.get("device"), selected.get("inode"))
            or not isinstance(runtime, dict)
            or runtime.get("root") != record.get("runtime_root")):
        raise EvidenceError("sealed lane record is not this run's lane generation")
    source_path = _recorded_source_path(sealed_lane)
    if identity.get("source_path") != source_path:
        raise EvidenceError("sealed lane source binding differs from its capture")
    capture_source = capture.get("source")
    if source_path is None:
        if capture_source != {"recorded": False}:
            raise EvidenceError("sealed capture records an unexpected source identity")
    elif (not isinstance(capture_source, dict) or capture_source.get("recorded") is not True
          or capture_source.get("path") != source_path):
        raise EvidenceError("sealed capture source identity is not bound to the lane")
    if lane is not None:
        if lane_record is None:
            raise EvidenceError("live lane generation has no readable record")
        live = _capture_lane_identity(lane, lane_record, record)
        for field in ("path", "name", "root", "token", "device", "inode",
                      "runtime_root", "source_path"):
            if live.get(field) != identity.get(field):
                raise EvidenceError("live lane generation differs from its sealed capture")


def _verified_payload(payload: Path, *, run_dir: Path, record: dict[str, Any],
                      lane: Path | None = None, lane_record: dict[str, Any] | None = None,
                      mode: str | None = None) -> dict[str, Any]:
    """Validate a sealed payload against the run, lane and bytes it claims to prove.

    Checksum verification alone accepts an empty manifest, uncovered files and a
    payload copied from another run. Recovery also proves capture completeness,
    this run's id/path/directory, the lane owner token and directory generation,
    the runtime/source binding and the required closed control records.
    """
    binding = _run_binding(run_dir, record)
    manifest = _capture_manifest(payload)
    capture = _read_capture(payload)
    records = _capture_files(capture, payload)
    never_started = capture.get("never_started") is True
    if mode is None:
        mode = "unstarted" if never_started else "started"
    elif (mode == "unstarted") != never_started:
        raise EvidenceError(f"sealed capture is not usable {mode} evidence")
    if capture.get("run") != binding:
        raise EvidenceError("sealed capture is bound to another managed run generation")
    sealed_sha = capture.get("record_sha256")
    sealed_record = records.get(RUN_RECORD_NAME)
    if (not _digest_text(sealed_sha) or sealed_record is None
            or sealed_record["sha256"].lower() != sealed_sha.lower()):
        raise EvidenceError("sealed run record identity is not proven by the capture")
    if mode == "unstarted":
        if (capture.get("lane") is not None or capture.get("lane_identity") is not None
                or "lane.json" in records
                or capture.get("source") != {"lane": None, "never_started": True}):
            raise EvidenceError("sealed capture does not prove an unstarted run")
        required = _UNSTARTED_CAPTURE_FILES
    else:
        _validate_lane_capture(payload, capture, record, lane, lane_record)
        required = _STARTED_CAPTURE_FILES
    missing = [name for name in required if name not in records]
    if missing:
        raise EvidenceError("sealed capture lacks required records: " + ", ".join(missing))
    _validate_control_records(payload, records, run_dir, record, mode)
    omitted = capture.get("omitted")
    return {
        "complete": True, "manifest": str(manifest), "payload": str(payload),
        "entries": [], "omitted": omitted if isinstance(omitted, list) else [],
        "source": capture.get("source"), "verified": True,
    }


def _seal_run_payload(run_dir: Path, populate) -> dict[str, Any]:
    payload = run_dir / PAYLOAD_DIRNAME
    for stale in run_dir.glob(".evidence-payload-*"):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)
    staging = Path(tempfile.mkdtemp(prefix=".evidence-payload-", dir=run_dir))
    try:
        outcome = populate(staging)
        _write_new_json(staging / "capture.json", outcome["capture"])
        verified = verify_archive(staging, recursive=True, write_manifest=True)
        if verified["failed"]:
            raise EvidenceError(f"cannot seal captured evidence: {verified['errors']}")
        staging.rename(payload)
        _fsync_directory(run_dir)
        result = {
            "complete": True, "manifest": str(payload / PAYLOAD_MANIFEST),
            "payload": str(payload), "entries": outcome["entries"],
            "omitted": outcome["omitted"], "source": outcome["source"], "verified": False,
        }
        result.update(outcome.get("extra", {}))
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _run_control_entries(run_dir: Path, entries: list[tuple[Path, Path, bool]],
                         *, include_record: bool) -> None:
    for name in ("command.stdout", "command.stderr", "invocation.json", "outcome.json", "payload-owner.json"):
        candidate = run_dir / name
        if not os.path.lexists(candidate):
            continue
        if candidate.is_symlink():
            raise EvidenceError(f"managed run entry is a symlink: {candidate}")
        info = candidate.stat()
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceError(f"managed run entry is not a regular file: {candidate}")
        entries.append((candidate, Path("command") / name, False))
    if include_record:
        candidate = run_dir / RUN_RECORD_NAME
        if os.path.lexists(candidate):
            if candidate.is_symlink():
                raise EvidenceError(f"managed run record is a symlink: {candidate}")
            info = candidate.stat()
            if not stat.S_ISREG(info.st_mode):
                raise EvidenceError(f"managed run record is not a regular file: {candidate}")
            entries.append((candidate, Path(RUN_RECORD_NAME), False))


def _copy_entries(entries: list[tuple[Path, Path, bool]], staging: Path) -> list[dict[str, Any]]:
    writers = open_writers([source_path for source_path, _, _ in entries])
    if writers:
        raise EvidenceError("selected evidence still has an open writer: " + ", ".join(writers))
    captured: list[dict[str, Any]] = []
    for source_path, relative, symlink in entries:
        target = staging / relative
        digest = _copy_verified(source_path, target)
        captured.append({
            "path": str(relative), "sha256": digest,
            "bytes": target.stat().st_size, "symlink": symlink,
        })
    return captured


def _record_generated(staging: Path, captured: list[dict[str, Any]], relative: str) -> None:
    path = staging / relative
    captured.append({
        "path": relative, "sha256": _sha256(path),
        "bytes": path.stat().st_size, "symlink": False,
    })


def finalize_run_evidence(lane: Path, run_dir: Path) -> dict[str, Any]:
    """Seal closed lane evidence into ``run_dir/payload`` before lane removal.

    The caller owns the run lock and must have stopped guest scope, FUSE view,
    and all producers. This reads only the lane's own evidence directories and
    selected DW_STATE_ROOT subdirectories; the merged guest view and generated
    forests are never promised or copied. Any uncertainty raises and leaves the
    lane raw state untouched. Recovery validates an existing seal against the
    current run and lane generation before it can authorize destruction.
    """
    lane = _owned_directory(lane, "lane")
    run_dir = _owned_directory(run_dir, "managed run directory")
    if lane == run_dir or lane in run_dir.parents or run_dir in lane.parents:
        raise EvidenceError("managed run directory and lane must be disjoint")
    record_path = run_dir / RUN_RECORD_NAME
    record = _read_json(record_path, "managed run record")
    lane_record = _lane_record(lane)
    payload = run_dir / PAYLOAD_DIRNAME
    if os.path.lexists(payload):
        return _verified_payload(
            payload, run_dir=run_dir, record=record, lane=lane,
            lane_record=lane_record, mode="started",
        )
    run_identity = _run_binding(run_dir, record)
    lane_identity = _capture_lane_identity(lane, lane_record, record)
    record_sha256 = _sha256(record_path)
    source = _source_identity(lane_record)

    def populate(staging: Path) -> dict[str, Any]:
        entries: list[tuple[Path, Path, bool]] = [
            (lane / "lane.json", Path("lane.json"), False),
        ]
        omitted: list[str] = []
        for name in ("logs", "cores", "receipts"):
            candidate = lane / name
            if os.path.lexists(candidate):
                _collect_tree(candidate, Path(name), entries, omitted)
            else:
                omitted.append(f"{name} (absent)")
        state = lane / "state"
        if os.path.lexists(state):
            _collect_state(state, entries, omitted)
        else:
            omitted.append("state (absent)")
        _run_control_entries(run_dir, entries, include_record=True)
        captured = _copy_entries(entries, staging)
        _write_new_json(staging / "source-identity.json", source)
        _record_generated(staging, captured, "source-identity.json")
        sealed_record = next((item for item in captured if item["path"] == RUN_RECORD_NAME), None)
        if sealed_record is None or sealed_record["sha256"] != record_sha256:
            raise EvidenceError("managed run record changed while sealing evidence")
        capture = {
            "schema": 1, "kind": "managed-run-evidence", "created": _utc_now(),
            "lane": str(lane), "lane_identity": lane_identity,
            "run": run_identity, "record_sha256": record_sha256,
            "complete": True, "source": source,
            "files": captured, "omitted": omitted,
        }
        return {"capture": capture, "entries": captured, "omitted": omitted, "source": source}

    return _seal_run_payload(run_dir, populate)


def finalize_unstarted_run_evidence(run_dir: Path) -> dict[str, Any]:
    """Seal a run that failed before any command or lane existed.

    The managed parent may fail budget/admission/create (or roll a create back
    to a tombstone) before launching a command. That run has no lane identity
    and no workload result; only closed command logs and the truthful parent
    record identity are sealed. A sealed payload is accepted on recovery only
    when its own capture proves that unstarted provenance.
    """
    run_dir = _owned_directory(run_dir, "managed run directory")
    record_path = run_dir / RUN_RECORD_NAME
    record = _read_json(record_path, "managed run record")
    planned = record.get("lane")
    lane_present = False
    if planned not in (None, {}):
        if not isinstance(planned, dict) or not isinstance(planned.get("path"), str):
            raise EvidenceError("invalid planned lane in unstarted run")
        lane_present = os.path.lexists(Path(planned["path"]) / "lane.json")
    if (record.get("schema") != 1 or record.get("kind") != "managed-lane-run"
            or record.get("path") != str(run_dir)
            or record.get("workload_rc") is not None
            or record.get("phase") not in {"preparing", "creating"}
            or record.get("command_started", False) is not False or lane_present):
        raise EvidenceError(
            "unstarted run evidence requires a managed record with no lane, "
            "no workload rc and phase preparing/creating"
        )
    payload = run_dir / PAYLOAD_DIRNAME
    if os.path.lexists(payload):
        result = _verified_payload(
            payload, run_dir=run_dir, record=record, mode="unstarted",
        )
        result["never_started"] = True
        return result
    run_identity = _run_binding(run_dir, record)
    record_sha256 = _sha256(record_path)

    def populate(staging: Path) -> dict[str, Any]:
        entries: list[tuple[Path, Path, bool]] = []
        _run_control_entries(run_dir, entries, include_record=True)
        captured = _copy_entries(entries, staging)
        sealed_record = next((item for item in captured if item["path"] == RUN_RECORD_NAME), None)
        if sealed_record is None or sealed_record["sha256"] != record_sha256:
            raise EvidenceError("managed run record changed while sealing unstarted evidence")
        source = {"lane": None, "never_started": True}
        capture = {
            "schema": 1, "kind": "managed-run-evidence", "created": _utc_now(),
            "lane": None, "lane_identity": None, "run": run_identity,
            "complete": True, "source": source, "never_started": True,
            "phase": record.get("phase"), "workload_rc": record.get("workload_rc"),
            "record_sha256": record_sha256,
            "record": {
                key: record.get(key) for key in (
                    "schema", "kind", "id", "root", "created", "phase",
                    "workload_rc", "final_rc", "keep", "pin",
                )
            },
            "files": captured,
        }
        return {
            "capture": capture, "entries": captured, "omitted": [],
            "source": source, "extra": {"never_started": True},
        }

    return _seal_run_payload(run_dir, populate)


def _directory_bytes(path: Path) -> int:
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            try:
                info = os.lstat(Path(directory) / name)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _protected_reason(path: Path, protect: Iterable[Path]) -> str | None:
    for raw in protect:
        candidate = _absolute(Path(raw))
        if path == candidate or _under(path, candidate) or _under(candidate, path):
            return f"explicitly protected by {candidate}"
    return None


def _try_lock(lock: Path) -> int | None:
    if lock.is_symlink():
        raise EvidenceError(f"evidence lock is a symlink: {lock}")
    try:
        info = lock.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise EvidenceError(f"evidence lock is not an owned regular file: {lock}")
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def _run_state_confined(state: Path) -> str | None:
    """Refuse to delete a run directory containing source or unknown payloads."""
    for entry in sorted(state.iterdir(), key=lambda item: item.name):
        name = entry.name
        info = entry.lstat()
        if name in (RUN_RECORD_NAME, RUN_LOCK_NAME, "command.stdout", "command.stderr",
                    "invocation.json", "outcome.json", "payload-owner.json"):
            if not stat.S_ISREG(info.st_mode):
                return f"owned run entry is not a regular file: {entry}"
            continue
        if name == PAYLOAD_DIRNAME:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return f"run payload is not a real directory: {entry}"
            for directory, dirs, files in os.walk(entry, followlinks=False):
                if any((Path(directory) / child).is_symlink() for child in dirs):
                    return f"symlinked directory below run payload is preserved: {directory}"
                if any(child in {"source", "src", "worktrees"} for child in dirs):
                    return f"source/worktree directory below run payload is preserved: {directory}"
                if os.path.lexists(Path(directory) / ".git"):
                    return f"Git payload below run evidence is preserved: {directory}"
            continue
        return f"unknown run-state entry is preserved: {entry}"
    return None


def _completed_key(record: dict[str, Any]) -> str:
    """Stable completion order: the finalized field, never mutable timestamps."""
    for key in ("completed", "created"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _run_rows(root: Path, runs: Path, protect: Iterable[Path],
              skip: Iterable[str] = ()) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    skipped = set(skip)
    for entry in sorted(runs.iterdir(), key=lambda item: item.name):
        if str(entry) in skipped:
            # The durable retirement journal already reports this run, either
            # as a resumed/completed removal or as a preserved ambiguity.
            continue
        row: dict[str, Any] = {"path": str(entry), "action": "retained", "reason": "", "bytes": 0}
        if entry.is_symlink() or not entry.is_dir():
            rows.append({**row, "reason": "not an owned run directory"})
            continue
        row["bytes"] = _directory_bytes(entry)
        record_path = entry / RUN_RECORD_NAME
        lock_path = entry / RUN_LOCK_NAME
        if not record_path.is_file() or record_path.is_symlink() or not lock_path.is_file() or lock_path.is_symlink():
            rows.append({**row, "reason": "missing owned record or lifetime lock"})
            continue
        try:
            record = _read_json(record_path, "managed run record")
        except EvidenceError as error:
            rows.append({**row, "reason": str(error)})
            continue
        row["runtime_root"] = record.get("runtime_root")
        row["runtime_pin_key"] = f"evidence:{entry}"
        identity = record.get("directory")
        info = entry.stat()
        if (record.get("schema") != 1 or record.get("kind") != "managed-lane-run"
                or record.get("path") != str(entry) or record.get("root") != str(root)
                or record.get("uid") != os.geteuid() or info.st_uid != os.geteuid()
                or not isinstance(identity, dict)
                or identity.get("device") != info.st_dev or identity.get("inode") != info.st_ino):
            rows.append({**row, "reason": "run record does not own this directory"})
            continue
        if record.get("keep") is True or record.get("pin") is True:
            rows.append({**row, "reason": "kept or pinned"})
            continue
        protected = _protected_reason(entry, protect)
        if protected:
            rows.append({**row, "reason": protected})
            continue
        evidence = record.get("evidence")
        cleanup = record.get("cleanup")
        if (record.get("phase") != "completed" or not isinstance(cleanup, dict)
                or cleanup.get("success") is not True or not isinstance(evidence, dict)
                or evidence.get("complete") is not True):
            rows.append({**row, "reason": "run is not completed with complete evidence"})
            continue
        confined = _run_state_confined(entry)
        if confined:
            rows.append({**row, "reason": confined})
            continue
        lock = _try_lock(lock_path)
        if lock is None:
            rows.append({**row, "reason": "run owner or reader still holds the lifetime lock"})
            continue
        try:
            try:
                _verified_payload(entry / PAYLOAD_DIRNAME, run_dir=entry, record=record)
            except EvidenceError as error:
                os.close(lock)
                rows.append({**row, "reason": str(error)})
                continue
            row["_record"] = record
            row["_lock"] = lock
            row["reason"] = "completed finalized run"
            rows.append(row)
        except BaseException:
            os.close(lock)
            raise
    return rows


def prune_run_evidence(root: Path, *, keep_last: int = 5, max_bytes: int = 2**30,
                       protect: Iterable[Path] = (), dry_run: bool = False) -> list[dict[str, Any]]:
    """Prune only owned, completed, sealed run evidence under ``root/runs``.

    Every removal is first journaled under ``root/retired-evidence`` with the
    run and payload identity, then detached and removed. An interrupted journal
    resumes only for that exact identity, and a completed journal keeps
    reappearing as a deleted row (with the original ``runtime_pin_key``) until
    the caller releases the pins and calls :func:`ack_run_retirement`.
    """
    root = _owned_directory(root, "declared run store root")
    if not isinstance(keep_last, int) or keep_last < 0:
        raise ValueError("keep_last must be a non-negative integer")
    if not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    retired = _reconcile_retirements(root, dry_run=dry_run)
    runs = root / RUN_STORE_DIRNAME
    if not os.path.lexists(runs):
        return retired
    if runs.is_symlink() or not runs.is_dir() or runs.stat().st_uid != os.geteuid():
        raise EvidenceError(f"run store is not an owned real directory: {runs}")
    protect = [_absolute(Path(item)) for item in protect]
    rows = _run_rows(root, runs, protect, skip=(row["path"] for row in retired))
    candidates = [row for row in rows if "_record" in row]
    candidates.sort(key=lambda row: (_completed_key(row["_record"]), row["path"]), reverse=True)
    for index, row in enumerate(candidates):
        row["_newest"] = index == 0
        row["_index"] = index
        if index == 0:
            row["reason"] = "newest completed evidence is retained"
    total = sum(int(row["bytes"]) for row in rows)
    # Count is the first bound; byte pressure may then evict older unpinned
    # candidates inside that count window, but never the newest completed run.
    planned: dict[str, str] = {}
    for row in candidates:
        if row["_index"] >= keep_last and not row.get("_newest"):
            planned[row["path"]] = "beyond count bound"
    projected = total - sum(int(row["bytes"]) for row in candidates if row["path"] in planned)
    if projected > max_bytes:
        for row in sorted(candidates, key=lambda item: item["_index"], reverse=True):
            if projected <= max_bytes:
                break
            if row.get("_newest") or row["path"] in planned:
                continue
            planned[row["path"]] = "over byte bound"
            projected -= int(row["bytes"])
    store = None
    for row in rows:
        row.pop("_newest", None)
        row.pop("_index", None)
        row.setdefault("runtime_root", None)
        reason = planned.get(row["path"])
        if reason is None:
            row.pop("_record", None)
            if "_lock" in row:
                os.close(row.pop("_lock"))
            row["reason"] = row["reason"] or "retained"
            continue
        if dry_run:
            row["action"] = "would-delete"
            row["reason"] = reason
            row.pop("_record", None)
            os.close(row.pop("_lock"))
            total -= int(row["bytes"])
            continue
        lock = row.pop("_lock", None)
        record = row["_record"]
        try:
            if lock is None:
                raise EvidenceError("run lock was not held during planning")
            if store is None:
                store = _retirement_store(root, create=True)
            _revalidate_run(root, Path(row["path"]), record)
            deleted = _retire_run(root, store, Path(row["path"]), record,
                                  reason, int(row["bytes"]))
            row["action"] = "deleted"
            row["reason"] = reason
            row["journal"] = deleted["journal"]
            row["id"] = deleted.get("id")
            row["root"] = str(root)
            row["runtime_pin_key"] = deleted["runtime_pin_key"]
            total -= int(row["bytes"])
        except (OSError, EvidenceError) as error:
            row["action"] = "error"
            row["reason"] = str(error)
        finally:
            if lock is not None:
                os.close(lock)
        row.pop("_record", None)
    if total > max_bytes:
        rows.append({
            "path": str(runs), "action": "retained",
            "reason": "byte bound cannot be met without deleting protected or newest evidence",
            "bytes": total, "runtime_root": None,
        })
    return retired + rows


def _revalidate_run(root: Path, path: Path, record: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
        raise EvidenceError(f"run directory changed before deletion: {path}")
    current = _read_json(path / RUN_RECORD_NAME, "managed run record")
    if current != record:
        raise EvidenceError(f"run record changed before deletion: {path}")
    if (current.get("phase") != "completed" or current.get("keep") is True
            or current.get("pin") is True or current.get("root") != str(root)):
        raise EvidenceError(f"run is no longer an automatic deletion candidate: {path}")
    _verified_payload(path / PAYLOAD_DIRNAME, run_dir=path, record=current)


def _retirement_store(root: Path, *, create: bool) -> Path | None:
    store = root / RETIREMENT_DIRNAME
    if not os.path.lexists(store):
        if not create:
            return None
        try:
            os.mkdir(store, 0o700)
        except FileExistsError:
            pass
        else:
            _fsync_directory(root)
    if store.is_symlink() or not store.is_dir() or store.stat().st_uid != os.geteuid():
        raise EvidenceError(f"retirement journal is not an owned real directory: {store}")
    return store


def _lock_retirement_store(store: Path) -> int:
    descriptor = os.open(store / RETIREMENT_LOCK_NAME,
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise EvidenceError(f"retirement journal lock is not owned: {store}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _replace_json(path: Path, value: Any) -> None:
    temporary = path.with_name("." + path.name + "." + os.urandom(8).hex() + ".tmp")
    try:
        _write_new_json(temporary, value)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _journal_path(store: Path, run_path: Path) -> Path:
    name = run_path.name
    if (not name or name.startswith(".") or name in {".", ".."}
            or os.sep in name or (os.altsep is not None and os.altsep in name)):
        raise EvidenceError(f"run directory name cannot identify a retirement journal: {run_path}")
    return store / (name + ".json")


def _completed_retirement_row(journal: Path, value: dict[str, Any], reason: str) -> dict[str, Any]:
    run = Path(str(value["run"]))
    return {
        "path": str(run), "id": value.get("id"), "action": "deleted",
        "reason": reason, "bytes": int(value.get("bytes", 0)),
        "runtime_root": value.get("runtime_root"),
        "runtime_pin_key": "evidence:" + str(run),
        "journal": str(journal), "root": value.get("root"),
    }


def _retention_payload_identity(payload: Path) -> dict[str, Any]:
    manifest = payload / PAYLOAD_MANIFEST
    info = payload.lstat()
    return {
        "path": str(payload), "directory": {"device": info.st_dev, "inode": info.st_ino},
        "manifest_sha256": _sha256(manifest), "manifest_bytes": manifest.stat().st_size,
    }


def _retire_run(root: Path, store: Path, run_path: Path, record: dict[str, Any],
                reason: str, size: int) -> dict[str, Any]:
    """Journal, detach and remove one already-validated completed run."""
    journal = _journal_path(store, run_path)
    if os.path.lexists(journal):
        raise EvidenceError(f"run already has a retirement journal: {journal}")
    info = run_path.lstat()
    identity = {"device": info.st_dev, "inode": info.st_ino}
    recorded = record.get("directory")
    if (run_path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or not isinstance(recorded, dict)
            or recorded.get("device") != info.st_dev
            or recorded.get("inode") != info.st_ino):
        raise EvidenceError(f"run generation changed before retirement: {run_path}")
    payload_identity = _retention_payload_identity(run_path / PAYLOAD_DIRNAME)
    trash = store / (RETIREMENT_TRASH_PREFIX + run_path.name + "-" + os.urandom(8).hex())
    now = _utc_now()
    value = {
        "schema": 1, "kind": "managed-run-retirement", "phase": "planned",
        "root": str(root), "uid": os.geteuid(), "run": str(run_path),
        "name": run_path.name, "id": record.get("id"), "directory": identity,
        "record_sha256": _sha256(run_path / RUN_RECORD_NAME),
        "payload": payload_identity, "trash": str(trash), "bytes": int(size),
        "runtime_root": record.get("runtime_root"),
        "runtime_pin_key": "evidence:" + str(run_path),
        "created": now, "updated": now, "reason": reason,
    }
    descriptor = _lock_retirement_store(store)
    detached = False
    try:
        if os.path.lexists(journal):
            raise EvidenceError(f"run already has a retirement journal: {journal}")
        # Atomic publication: a crash leaves either no journal or a complete
        # planned one, both of which the next collection can resume or retry.
        _replace_json(journal, value)
        if os.path.lexists(trash):
            raise EvidenceError(f"retirement trash already exists: {trash}")
        os.rename(run_path, trash)
        detached = True
        moved = trash.lstat()
        if (moved.st_dev, moved.st_ino) != (info.st_dev, info.st_ino):
            raise EvidenceError(f"run generation changed during retirement: {run_path}")
        value = {**value, "phase": "detached", "updated": _utc_now()}
        _replace_json(journal, value)
        shutil.rmtree(trash)
        value = {**value, "phase": "completed", "removed": _utc_now(), "updated": _utc_now()}
        _replace_json(journal, value)
    except BaseException:
        if not detached:
            journal.unlink(missing_ok=True)
            _fsync_directory(store)
        raise
    finally:
        os.close(descriptor)
    return _completed_retirement_row(journal, value, reason)


def _validate_retirement(value: dict[str, Any], *, root: Path,
                         store: Path) -> tuple[Path, Path]:
    run_path = Path(str(value.get("run", "")))
    trash = Path(str(value.get("trash", "")))
    identity = value.get("directory")
    payload = value.get("payload")
    if (value.get("schema") != 1 or value.get("kind") != "managed-run-retirement"
            or value.get("root") != str(root) or value.get("uid") != os.geteuid()
            or run_path.parent != root / RUN_STORE_DIRNAME
            or trash.parent != store
            or not trash.name.startswith(RETIREMENT_TRASH_PREFIX)
            or value.get("runtime_pin_key") != "evidence:" + str(run_path)
            or not isinstance(identity, dict)
            or not _digest_text(value.get("record_sha256"))
            or not isinstance(payload, dict)
            or not _digest_text(payload.get("manifest_sha256"))
            or type(payload.get("manifest_bytes")) is not int
            or type(value.get("bytes")) is not int or value["bytes"] < 0):
        raise EvidenceError("retirement journal does not own this evidence")
    return run_path, trash


def _remove_detached(journal: Path, value: dict[str, Any],
                     trash: Path) -> dict[str, Any]:
    identity = value["directory"]
    if os.path.lexists(trash):
        info = trash.lstat()
        if (trash.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != (identity["device"], identity["inode"])):
            raise EvidenceError("retirement trash does not match its journal identity")
        shutil.rmtree(trash)
    value = {**value, "phase": "completed", "removed": _utc_now(), "updated": _utc_now()}
    _replace_json(journal, value)
    return _completed_retirement_row(journal, value, "retirement journal resumed and removed the run")


def _reconcile_retirement(root: Path, store: Path, journal: Path, *,
                          dry_run: bool) -> dict[str, Any]:
    value = _read_json(journal, "run retirement journal")
    run_path, trash = _validate_retirement(value, root=root, store=store)
    pending = {
        "path": str(run_path), "id": value.get("name"), "action": "retained",
        "bytes": int(value["bytes"]), "runtime_root": value.get("runtime_root"),
        "runtime_pin_key": value["runtime_pin_key"], "journal": str(journal),
        "root": str(root),
    }
    phase = value.get("phase")
    if dry_run:
        pending["reason"] = f"retirement journal is {phase!r}; dry run makes no change"
        return pending
    if phase == "completed":
        return _completed_retirement_row(
            journal, value, "retirement completed; awaiting pin acknowledgement")
    if phase == "blocked":
        pending["reason"] = str(value.get("problem") or "retirement journal is blocked")
        return pending
    if phase == "detached":
        return _remove_detached(journal, value, trash)
    if phase != "planned":
        raise EvidenceError(f"unknown retirement journal phase: {phase!r}")
    lock = None
    if os.path.lexists(run_path):
        lock = _try_lock(run_path / RUN_LOCK_NAME)
        if lock is None:
            pending["reason"] = "run is being retired by another owner"
            return pending
    try:
        if os.path.lexists(run_path):
            info = run_path.lstat()
            identity = value["directory"]
            if (run_path.is_symlink() or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or (info.st_dev, info.st_ino) != (identity["device"], identity["inode"])):
                raise EvidenceError("run generation changed before retirement resumed")
            if _sha256(run_path / RUN_RECORD_NAME) != value["record_sha256"]:
                raise EvidenceError("run record changed before retirement resumed")
            record = _read_json(run_path / RUN_RECORD_NAME, "managed run record")
            payload = run_path / PAYLOAD_DIRNAME
            _verified_payload(payload, run_dir=run_path, record=record)
            current = _retention_payload_identity(payload)
            if (current["manifest_sha256"] != value["payload"]["manifest_sha256"]
                    or current["directory"] != value["payload"]["directory"]):
                raise EvidenceError("run payload changed before retirement resumed")
            if os.path.lexists(trash):
                raise EvidenceError(f"retirement trash already exists: {trash}")
            os.rename(run_path, trash)
            moved = trash.lstat()
            if (moved.st_dev, moved.st_ino) != (identity["device"], identity["inode"]):
                raise EvidenceError("run generation changed during retirement")
            value = {**value, "phase": "detached", "updated": _utc_now()}
            _replace_json(journal, value)
        elif not os.path.lexists(trash):
            raise EvidenceError("retirement journal has neither the run nor its trash")
        return _remove_detached(journal, value, trash)
    finally:
        if lock is not None:
            os.close(lock)


def _reconcile_retirements(root: Path, *, dry_run: bool) -> list[dict[str, Any]]:
    """Resume prior retirements and report completed ones until acknowledged."""
    store = _retirement_store(root, create=False)
    if store is None:
        return []
    descriptor = _lock_retirement_store(store)
    rows: list[dict[str, Any]] = []
    try:
        for entry in sorted(store.iterdir(), key=lambda item: item.name):
            if entry.name == RETIREMENT_LOCK_NAME:
                continue
            if (entry.name.startswith(".") and entry.name.endswith(".tmp")
                    and not entry.is_symlink()):
                # Our own atomic-journal temporary from a crashed writer: the
                # store lock we hold proves no writer is still publishing it.
                if not dry_run:
                    entry.unlink(missing_ok=True)
                continue
            if entry.name.startswith(RETIREMENT_TRASH_PREFIX):
                rows.append({
                    "path": str(entry), "action": "retained",
                    "reason": "retirement trash has no readable journal; preserved",
                    "bytes": _directory_bytes(entry) if entry.is_dir() and not entry.is_symlink() else 0,
                    "runtime_root": None, "runtime_pin_key": None, "journal": None,
                })
                continue
            if entry.is_symlink() or not entry.is_file() or not entry.name.endswith(".json"):
                rows.append({
                    "path": str(entry), "action": "retained",
                    "reason": "unknown retirement journal entry is preserved",
                    "bytes": 0, "runtime_root": None, "runtime_pin_key": None,
                    "journal": str(entry) if entry.is_file() else None,
                })
                continue
            try:
                rows.append(_reconcile_retirement(root, store, entry, dry_run=dry_run))
            except (EvidenceError, OSError, ValueError) as error:
                rows.append({
                    "path": str(entry), "action": "retained",
                    "reason": f"retirement journal is not resumable: {error}",
                    "bytes": 0, "runtime_root": None, "runtime_pin_key": None,
                    "journal": str(entry),
                })
    finally:
        os.close(descriptor)
    return rows


def ack_run_retirement(root_or_row, row: Any = None) -> bool:
    """Delete a completed retirement journal after the caller released its pins.

    Accepts ``(root, row)``, ``(root, run_path_or_journal)`` or a single result
    row. Idempotent: an already acknowledged journal returns True. A journal
    that is not completed, not owned by this root, or not resumable raises
    rather than erasing the only ownership record.
    """
    if isinstance(root_or_row, dict):
        row = root_or_row
        root_value = root_or_row.get("root")
        if not root_value:
            raise EvidenceError("retirement acknowledgement needs the owner root")
        root = Path(str(root_value)).expanduser()
    else:
        root = Path(root_or_row).expanduser()
    root = _owned_directory(root, "declared run store root")
    if row is None:
        raise EvidenceError("retirement acknowledgement needs a result row or journal")
    store = _retirement_store(root, create=False)
    if store is None:
        # No journal directory means there is nothing left to acknowledge.
        return True
    if isinstance(row, dict):
        target = row.get("journal")
        target = (Path(str(target)) if target is not None
                  else _journal_path(store, Path(str(row.get("path", "")))))
    else:
        candidate = Path(str(row))
        target = candidate if candidate.parent == store else _journal_path(store, candidate)
    if target.parent != store:
        raise EvidenceError(f"retirement journal is outside this run store: {target}")
    descriptor = _lock_retirement_store(store)
    try:
        if not os.path.lexists(target):
            return True
        if target.is_symlink() or not target.is_file():
            raise EvidenceError(f"retirement journal is not a regular file: {target}")
        value = _read_json(target, "run retirement journal")
        if (value.get("schema") != 1 or value.get("kind") != "managed-run-retirement"
                or value.get("root") != str(root) or value.get("uid") != os.geteuid()):
            raise EvidenceError(f"retirement journal does not own this root: {target}")
        if value.get("phase") != "completed":
            return False
        target.unlink()
        _fsync_directory(store)
        return True
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------
# Generic west-job producer state: ownership marker, lifetime lock, bounded
# finalized retention. These records are deliberately separate from the parent
# managed-lane-run schema.
# ---------------------------------------------------------------------------

def _job_marker(state: Path) -> dict[str, Any]:
    marker = _read_json(state / JOB_MARKER_NAME, "west-job ownership marker")
    info = state.stat()
    if (marker.get("schema") != 1 or marker.get("kind") != "west-job"
            or marker.get("path") != str(state) or marker.get("uid") != os.geteuid()
            or info.st_uid != os.geteuid() or marker.get("store") != str(state.parent)
            or not isinstance(marker.get("directory"), dict)
            or marker["directory"].get("device") != info.st_dev
            or marker["directory"].get("inode") != info.st_ino):
        raise EvidenceError(f"west-job ownership marker does not own this state: {state}")
    if not (state / JOB_LOCK_NAME).is_file() or (state / JOB_LOCK_NAME).is_symlink():
        raise EvidenceError(f"west-job state has no lifetime lock: {state}")
    reader = marker.get("reader_lock")
    reader_path = state / JOB_READER_LOCK_NAME
    try:
        reader_info = reader_path.lstat()
    except OSError as error:
        raise EvidenceError(f"west-job state has no reader lease: {state}") from error
    if (not isinstance(reader, dict) or not stat.S_ISREG(reader_info.st_mode)
            or reader_info.st_uid != os.geteuid()
            or reader.get("device") != reader_info.st_dev
            or reader.get("inode") != reader_info.st_ino):
        raise EvidenceError(f"west-job reader lease does not match its marker: {state}")
    return marker


def _process_identity(pid: int) -> dict[str, Any] | None:
    try:
        process = Path("/proc") / str(pid)
        fields = (process / "stat").read_bytes().rsplit(b")", 1)[1].split()
        if fields[0] in {b"Z", b"X", b"x"}:
            return None
        return {"start_time": fields[19].decode("ascii"), "ppid": int(fields[1]),
                "uid": process.stat().st_uid}
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError, UnicodeError) as error:
        raise EvidenceError(f"cannot inspect managed job owner {pid}: {error}") from error


def register_job_owner(state_dir: Path, run_dir: Path) -> Path | None:
    """Bind an actual descendant controller to its job's cancellation domain."""
    state = _owned_directory(state_dir, "west-job state")
    if not os.path.lexists(state / JOB_MARKER_NAME):
        return None
    marker = _job_marker(state)
    if os.path.lexists(state / "rc"):
        raise EvidenceError("cannot register a controller under a completed job")
    run = _owned_directory(run_dir, "managed run")
    record = _read_json(run / RUN_RECORD_NAME, "managed run record")
    binding = _run_binding(run, record)
    identifier = record["id"]
    if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
        raise EvidenceError("managed job owner has an invalid run identifier")
    owner = record.get("owner")
    current = _process_identity(os.getpid())
    if (not isinstance(owner, dict) or current is None
            or owner != {"pid": os.getpid(), "start_time": current["start_time"]}):
        raise EvidenceError("only the current managed controller may register its ownership")
    try:
        runner_pid = int((state / "runner-pid").read_text().strip())
        runner_birth = (state / "runner-start-time").read_text().strip()
    except (OSError, ValueError) as error:
        raise EvidenceError(f"job has no runner generation: {error}") from error
    ancestor = os.getpid()
    visited = set()
    matched = False
    while ancestor > 0 and ancestor not in visited:
        visited.add(ancestor)
        identity = _process_identity(ancestor)
        if identity is None:
            break
        if ancestor == runner_pid and identity["start_time"] == runner_birth:
            matched = True
            break
        ancestor = identity["ppid"]
    if not matched:
        raise EvidenceError("managed controller is not descended from this job runner")
    owners = state / "managed-run-owners"
    owners.mkdir(mode=0o700, exist_ok=True)
    _owned_directory(owners, "managed job owner references")
    target = owners / (identifier + ".json")
    value = {
        "schema": 1, "kind": "managed-run-job-owner", "uid": os.geteuid(),
        "job": str(state), "job_directory": marker["directory"],
        "run": str(run), "run_directory": binding["directory"], "id": identifier,
        "owner": owner, "runner_pid": runner_pid, "runner_start_time": runner_birth,
    }
    if os.path.lexists(target):
        previous = _read_json(target, "managed job owner reference")
        for key in ("schema", "kind", "uid", "job", "job_directory", "run", "run_directory", "id"):
            if previous.get(key) != value[key]:
                raise EvidenceError("managed job owner reference belongs to another generation")
    _replace_json(target, value)
    return target


def _live_job_owners(state: Path) -> list[dict[str, Any]]:
    marker = _job_marker(state)
    store = state / "managed-run-owners"
    if not os.path.lexists(store):
        return []
    _owned_directory(store, "managed job owner references")
    result = []
    for entry in store.iterdir():
        parts = entry.name.split(".")
        if (len(parts) == 5 and parts[0] == "" and parts[2] == "json" and parts[4] == "tmp"
                and len(parts[1]) == 32 and len(parts[3]) == 16
                and all(char in "0123456789abcdef" for char in parts[1] + parts[3])):
            continue
        value = _read_json(entry, "managed job owner reference")
        identifier = value.get("id")
        owner = value.get("owner")
        if (value.get("schema") != 1 or value.get("kind") != "managed-run-job-owner"
                or value.get("uid") != os.geteuid() or value.get("job") != str(state)
                or value.get("job_directory") != marker["directory"]
                or not isinstance(identifier, str) or entry.name != identifier + ".json"
                or not isinstance(owner, dict) or type(owner.get("pid")) is not int
                or owner["pid"] <= 0):
            raise EvidenceError("managed job owner reference is not bound to this job")
        identity = _process_identity(owner["pid"])
        if identity is None or identity["start_time"] != owner.get("start_time"):
            continue
        if identity["uid"] != os.geteuid():
            raise EvidenceError("managed job owner changed user identity")
        run = _owned_directory(Path(value["run"]), "managed job owner run")
        record = _read_json(run / RUN_RECORD_NAME, "managed run record")
        binding = _run_binding(run, record)
        if binding["id"] != identifier or binding["directory"] != value.get("run_directory"):
            raise EvidenceError("managed job owner run generation changed")
        if record.get("owner") != owner or record.get("phase") in {"completed", "retained"}:
            continue
        lock_path = run / RUN_LOCK_NAME
        if not lock_path.is_file() or lock_path.is_symlink():
            raise EvidenceError("managed job owner lost its persistent run lease")
        lock = _try_lock(lock_path)
        if lock is not None:
            os.close(lock)
            continue
        result.append(value)
    return result


def interrupt_job_owners(state_dir: Path) -> list[int]:
    """Interrupt only PID-pinned, leased controllers registered by this job."""
    state = _owned_directory(state_dir, "west-job state")
    if not os.path.lexists(state / JOB_MARKER_NAME):
        return []
    interrupted = []
    for value in _live_job_owners(state):
        owner = value["owner"]
        try:
            descriptor = os.pidfd_open(owner["pid"])
        except ProcessLookupError:
            continue
        try:
            current = _process_identity(owner["pid"])
            if current is not None and current["start_time"] == owner["start_time"]:
                signal.pidfd_send_signal(descriptor, signal.SIGINT)
                interrupted.append(owner["pid"])
        finally:
            os.close(descriptor)
    return interrupted


def mark_job_state(state_dir: Path, root: Path | None = None) -> dict[str, Any]:
    """Publish ownership for a new job state before populating it."""
    state = _owned_directory(state_dir, "west-job state directory")
    marker = state / JOB_MARKER_NAME
    if os.path.lexists(marker):
        raise EvidenceError(f"west-job state is already marked: {state}")
    if not (state / JOB_LOCK_NAME).is_file() or (state / JOB_LOCK_NAME).is_symlink():
        raise EvidenceError(f"west-job state has no lifetime lock: {state}")
    reader = state / JOB_READER_LOCK_NAME
    if not reader.is_file() or reader.is_symlink():
        raise EvidenceError(f"west-job state has no reader lease: {state}")
    declared = _absolute(Path(root)) if root is not None else None
    if declared is not None:
        try:
            declared = _owned_directory(declared, "declared job retention root")
        except EvidenceError:
            declared = None
        else:
            if not _under(state, declared):
                declared = None
    else:
        for variable in ("WEST_JOB_RETENTION_ROOT", "DW_STATE_ROOT"):
            value = os.environ.get(variable)
            if not value:
                continue
            candidate = _absolute(Path(value))
            try:
                candidate = _owned_directory(candidate, "declared job retention root")
            except EvidenceError:
                continue
            if _under(state, candidate):
                declared = candidate
                break
    info = state.stat()
    reader_info = reader.lstat()
    value = {
        "schema": 1, "kind": "west-job", "path": str(state),
        "store": str(state.parent), "root": str(declared) if declared else None,
        "uid": os.geteuid(), "directory": {"device": info.st_dev, "inode": info.st_ino},
        "reader_lock": {"device": reader_info.st_dev, "inode": reader_info.st_ino},
        "created": _utc_now(),
    }
    _write_new_json(marker, value)
    return value


def _job_state_confined(state: Path) -> str | None:
    """Refuse to delete a state that contains source or unrecognised payloads."""
    allowed_dirs = {"activity-logs.d", "evidence-boundaries", "evidence-logs", "managed-run-owners"}
    for entry in sorted(state.iterdir(), key=lambda item: item.name):
        name = entry.name
        info = entry.lstat()
        if name in (JOB_MARKER_NAME, JOB_FINALIZED_NAME, JOB_LOCK_NAME, JOB_READER_LOCK_NAME):
            continue
        if stat.S_ISLNK(info.st_mode):
            return f"symlinked job-state entry is preserved: {entry}"
        if stat.S_ISDIR(info.st_mode):
            if name not in allowed_dirs:
                return f"unknown job-state directory is preserved: {entry}"
            for directory, dirs, files in os.walk(entry, followlinks=False):
                if any((Path(directory) / child).is_symlink() for child in dirs):
                    return f"symlinked directory below job state is preserved: {directory}"
                if os.path.lexists(Path(directory) / ".git"):
                    return f"Git payload below job state is preserved: {directory}"
            continue
        if not stat.S_ISREG(info.st_mode):
            return f"special job-state entry is preserved: {entry}"
        if name.startswith("evidence-index.") and name.endswith(".json"):
            continue
        if name not in {
            "log", "log.zst", "command", "command-argv", "rc", "pid", "start-time",
            "command-pid", "command-start-time", "runner-pid", "runner-start-time",
            "cancel-requested", "cancel-descendants", "activity-logs", "evidence-manifest",
            "evidence.lock", ".evidence-index.lock", "instruments-sliced.log",
            "pin", "keep", "retention.json",
        }:
            return f"unknown job-state file is preserved: {entry}"
    return None


def _job_row(state: Path, *, root: Path, current: Path | None, protect: Iterable[Path]) -> dict[str, Any]:
    row: dict[str, Any] = {"path": str(state), "action": "retained", "reason": "", "bytes": 0}
    if state.is_symlink() or not state.is_dir() or state.stat().st_uid != os.geteuid():
        row["reason"] = "not an owned job state"
        return row
    row["bytes"] = _directory_bytes(state)
    if current is not None and state == current:
        row["reason"] = "current job state"
        return row
    if not os.path.lexists(state / JOB_MARKER_NAME):
        row["reason"] = "unmarked job state is never adopted"
        return row
    try:
        marker = _job_marker(state)
        if _live_job_owners(state):
            row["reason"] = "a registered managed controller is still active"
            return row
    except EvidenceError as error:
        row["reason"] = str(error)
        return row
    if marker.get("root") is None:
        row["reason"] = "job state has no declared retention root"
        return row
    if marker.get("root") != str(root):
        row["reason"] = "job state belongs to another declared retention root"
        return row
    for name in ("pin", "keep"):
        if os.path.lexists(state / name):
            row["reason"] = f"job state has an explicit {name} marker"
            return row
    protected = _protected_reason(state, protect)
    if protected:
        row["reason"] = protected
        return row
    if not (state / JOB_FINALIZED_NAME).is_file() or (state / JOB_FINALIZED_NAME).is_symlink():
        row["reason"] = "job state is not finalized"
        return row
    try:
        _read_json(state / JOB_FINALIZED_NAME, "job finalization record")
    except EvidenceError as error:
        row["reason"] = str(error)
        return row
    rc_path = state / "rc"
    try:
        text = rc_path.read_text().strip()
        if not text.isascii() or not text.isdecimal():
            raise ValueError
    except (OSError, ValueError):
        row["reason"] = "finalized job has no usable workload rc"
        return row
    lock = _try_lock(state / JOB_LOCK_NAME)
    if lock is None:
        row["reason"] = "job owner still holds the lifetime lock"
        return row
    reader = _try_lock(state / JOB_READER_LOCK_NAME)
    if reader is None:
        os.close(lock)
        row["reason"] = "job observer holds the shared follow lease"
        return row
    blocked = _job_state_confined(state)
    if blocked:
        os.close(reader)
        os.close(lock)
        row["reason"] = blocked
        return row
    row["_marker"] = marker
    row["_lock"] = lock
    row["_reader_lock"] = reader
    row["reason"] = "finalized job state"
    return row


def _compact_job_log(state: Path, *, dry_run: bool) -> dict[str, Any]:
    log = state / "log"
    stored = state / "log.zst"
    if not os.path.lexists(log):
        if os.path.lexists(stored):
            try:
                metadata = payload_metadata(log)
            except EvidenceError as error:
                return {"action": "error", "reason": str(error)}
            return {"action": "verified", "reason": "already compressed", **metadata}
        return {"action": "retained", "reason": "no log"}
    if log.is_symlink():
        return {"action": "retained", "reason": "job log is a symlink"}
    try:
        return compress_file(log, finalized=True, explicit=True, dry_run=dry_run, role="job-log")
    except (EvidenceError, OSError, ValueError) as error:
        return {"action": "retained", "reason": f"log retained raw: {error}"}


def prune_job_states(root: Path, *, store: Path | None = None, current: Path | None = None,
                     keep_last: int = 5, max_bytes: int = 2**30,
                     protect: Iterable[Path] = (), dry_run: bool = False) -> list[dict[str, Any]]:
    """Prune only this script's own finalized, marked, unlocked job states."""
    root = _owned_directory(root, "declared job retention root")
    store = _owned_directory(store if store is not None else root / "jobs", "job store directory")
    if not _under(store, root):
        raise EvidenceError(f"job store is outside its declared root: {store}")
    if not isinstance(keep_last, int) or keep_last < 0:
        raise ValueError("keep_last must be a non-negative integer")
    if not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    current = _absolute(current) if current is not None else None
    protect = [_absolute(Path(item)) for item in protect]
    rows = [_job_row(entry, root=root, current=current, protect=protect)
            for entry in sorted(store.iterdir(), key=lambda item: item.name)]
    candidates = [row for row in rows if "_marker" in row]
    candidates.sort(key=lambda row: (str(row["_marker"].get("created", "")), row["path"]), reverse=True)
    for index, row in enumerate(candidates):
        row["_newest"] = index == 0
        row["_index"] = index
        if row["_newest"]:
            row["reason"] = "newest finalized job state is retained"
    total = sum(int(row["bytes"]) for row in rows)
    planned: dict[str, str] = {}
    for row in candidates:
        if row["_index"] >= keep_last and not row["_newest"]:
            planned[row["path"]] = "beyond count bound"
    # Compact logs for states that are being kept, then let the resulting bytes
    # decide whether older unpinned states inside the count window must go.
    for row in candidates:
        if row["path"] in planned:
            continue
        state = Path(row["path"])
        before = int(row["bytes"])
        compaction = _compact_job_log(state, dry_run=dry_run)
        row["_log"] = compaction
        if not dry_run:
            row["bytes"] = _directory_bytes(state)
            total += int(row["bytes"]) - before
    projected = total - sum(int(row["bytes"]) for row in candidates if row["path"] in planned)
    if projected > max_bytes:
        for row in sorted(candidates, key=lambda item: item["_index"], reverse=True):
            if projected <= max_bytes:
                break
            if row["_newest"] or row["path"] in planned:
                continue
            planned[row["path"]] = "over byte bound"
            projected -= int(row["bytes"])
    for row in rows:
        row.pop("_newest", None)
        row.pop("_index", None)
        reason = planned.get(row["path"])
        if reason is None:
            row.pop("_marker", None)
            if "_reader_lock" in row:
                os.close(row.pop("_reader_lock"))
            if "_lock" in row:
                os.close(row.pop("_lock"))
            row["reason"] = row["reason"] or "retained"
            continue
        row["reason"] = reason
        if dry_run:
            row["action"] = "would-delete"
            row.pop("_marker", None)
            if "_reader_lock" in row:
                os.close(row.pop("_reader_lock"))
            if "_lock" in row:
                os.close(row.pop("_lock"))
            total -= int(row["bytes"])
            continue
        lock = row.pop("_lock", None)
        reader = row.pop("_reader_lock", None)
        try:
            if lock is None:
                raise EvidenceError("job lock was not held during planning")
            marker = _job_marker(Path(row["path"]))
            if marker != row.get("_marker"):
                raise EvidenceError("job ownership marker changed before deletion")
            if os.path.lexists(Path(row["path"]) / "pin") or os.path.lexists(Path(row["path"]) / "keep"):
                raise EvidenceError("job acquired an explicit pin before deletion")
            shutil.rmtree(row["path"])
            row["action"] = "deleted"
            total -= int(row["bytes"])
        except (OSError, EvidenceError) as error:
            row["action"] = "error"
            row["reason"] = str(error)
        finally:
            if reader is not None:
                os.close(reader)
            if lock is not None:
                os.close(lock)
        row.pop("_marker", None)
    if total > max_bytes:
        rows.append({
            "path": str(store), "action": "retained", "bytes": total,
            "reason": "byte bound cannot be met without deleting protected or newest job state",
        })
    return rows


def finalize_job_state(state_dir: Path, *, workload_rc: int, keep_last: int = 5,
                       max_bytes: int = 2**30, protect: Iterable[Path] = (),
                       dry_run: bool = False) -> dict[str, Any]:
    """Record the true result of a closing job and bound its prior states."""
    state = _owned_directory(state_dir, "west-job state directory")
    marker = _job_marker(state)
    if not isinstance(workload_rc, int) or workload_rc < 0:
        raise ValueError("workload_rc must be a non-negative integer")
    finalized = state / JOB_FINALIZED_NAME
    verified = False
    if os.path.lexists(finalized):
        existing = _read_json(finalized, "job finalization record")
        if existing.get("workload_rc") != workload_rc:
            raise EvidenceError("job finalization already recorded a different workload rc")
        verified = True
    else:
        metadata: dict[str, Any] = {}
        for name in ("command", "command-argv", "evidence-manifest"):
            path = state / name
            if path.is_file() and not path.is_symlink():
                metadata[name] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
        record = {
            "schema": 1, "kind": "west-job-finalized", "state": str(state),
            "finalized": _utc_now(), "workload_rc": workload_rc,
            "cancel_requested": os.path.lexists(state / "cancel-requested"),
            "metadata": metadata,
        }
        _write_new_json(finalized, record)
    pruned: list[dict[str, Any]] = []
    root_value = marker.get("root")
    if root_value:
        pruned = prune_job_states(
            Path(root_value), store=Path(marker["store"]), current=state,
            keep_last=keep_last, max_bytes=max_bytes, protect=protect, dry_run=dry_run,
        )
    return {"state": str(state), "finalized": str(finalized), "workload_rc": workload_rc,
            "verified": verified, "pruned": pruned}


def check_job_follow_lease(state_dir: Path, *, directory: tuple[int, int] | None = None,
                           reader: tuple[int, int] | None = None) -> dict[str, Any]:
    """Validate the marked generation an observer leased before it reads it.

    The observer takes a shared flock on ``.west-job.readers.lock`` first, then
    confirms the directory generation, the ownership marker, and the lock inode
    it actually holds. A state pruned or replaced in between is refused instead
    of being followed as if it were the same job.
    """
    state = _owned_directory(state_dir, "west-job state directory")
    marker = _job_marker(state)
    info = state.stat()
    if directory is not None:
        if (int(directory[0]), int(directory[1])) != (info.st_dev, info.st_ino):
            raise EvidenceError(f"west-job state generation changed: {state}")
        if marker["directory"] != {"device": info.st_dev, "inode": info.st_ino}:
            raise EvidenceError(f"west-job marker generation changed: {state}")
    if reader is not None:
        expected = (int(reader[0]), int(reader[1]))
        try:
            current = (state / JOB_READER_LOCK_NAME).stat()
        except OSError as error:
            raise EvidenceError(f"west-job reader lease disappeared: {state}") from error
        recorded = marker.get("reader_lock", {})
        if ((current.st_dev, current.st_ino) != expected
                or (recorded.get("device"), recorded.get("inode")) != expected):
            raise EvidenceError(f"west-job reader lease is not the leased generation: {state}")
    return marker


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="finalized evidence retention")
    actions = parser.add_subparsers(dest="action", required=True)
    job_mark = actions.add_parser("job-mark")
    job_mark.add_argument("--state-dir", type=Path, required=True)
    job_mark.add_argument("--root", type=Path)
    job_finalize = actions.add_parser("job-finalize")
    job_finalize.add_argument("--state-dir", type=Path, required=True)
    job_finalize.add_argument("--rc", type=int, required=True)
    job_finalize.add_argument("--keep-last", type=int, default=5)
    job_finalize.add_argument("--max-bytes", type=int, default=2**30)
    job_finalize.add_argument("--dry-run", action="store_true")
    job_prune = actions.add_parser("job-prune")
    job_prune.add_argument("--root", type=Path, required=True)
    job_prune.add_argument("--store", type=Path)
    job_prune.add_argument("--state-dir", type=Path)
    job_prune.add_argument("--keep-last", type=int, default=5)
    job_prune.add_argument("--max-bytes", type=int, default=2**30)
    job_prune.add_argument("--dry-run", action="store_true")
    follow_check = actions.add_parser("job-follow-check")
    follow_check.add_argument("--state-dir", type=Path, required=True)
    follow_check.add_argument("--directory-device", type=int)
    follow_check.add_argument("--directory-inode", type=int)
    follow_check.add_argument("--reader-device", type=int)
    follow_check.add_argument("--reader-inode", type=int)
    interrupt = actions.add_parser("job-interrupt-owners")
    interrupt.add_argument("--state-dir", type=Path, required=True)
    run_finalize = actions.add_parser("run-finalize")
    run_finalize.add_argument("--lane", type=Path, required=True)
    run_finalize.add_argument("--run-dir", type=Path, required=True)
    unstarted = actions.add_parser("run-finalize-unstarted")
    unstarted.add_argument("--run-dir", type=Path, required=True)
    run_prune = actions.add_parser("run-prune")
    run_prune.add_argument("--root", type=Path, required=True)
    run_prune.add_argument("--keep-last", type=int, default=5)
    run_prune.add_argument("--max-bytes", type=int, default=2**30)
    run_prune.add_argument("--protect", type=Path, action="append", default=[])
    run_prune.add_argument("--dry-run", action="store_true")
    run_ack = actions.add_parser("run-ack")
    run_ack.add_argument("--root", type=Path, required=True)
    run_ack.add_argument("--journal", type=Path)
    run_ack.add_argument("--run", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.action == "job-mark":
        print(json.dumps(mark_job_state(arguments.state_dir, arguments.root), sort_keys=True))
        return 0
    if arguments.action == "job-finalize":
        print(json.dumps(finalize_job_state(
            arguments.state_dir, workload_rc=arguments.rc, keep_last=arguments.keep_last,
            max_bytes=arguments.max_bytes, dry_run=arguments.dry_run,
        ), sort_keys=True))
        return 0
    if arguments.action == "job-interrupt-owners":
        try:
            interrupted = interrupt_job_owners(arguments.state_dir)
        except (EvidenceError, OSError) as error:
            print(f"managed job cancellation unavailable: {error}", file=sys.stderr)
            return 2
        print(json.dumps({"interrupted": interrupted}, sort_keys=True))
        return 0 if interrupted else 3
    if arguments.action == "job-follow-check":
        pairs = (
            (arguments.directory_device, arguments.directory_inode),
            (arguments.reader_device, arguments.reader_inode),
        )
        if any((device is None) != (inode is None) for device, inode in pairs):
            print("evidence retention: lease identity needs both device and inode", file=sys.stderr)
            return 2
        try:
            marker = check_job_follow_lease(
                arguments.state_dir,
                directory=(arguments.directory_device, arguments.directory_inode)
                if arguments.directory_device is not None else None,
                reader=(arguments.reader_device, arguments.reader_inode)
                if arguments.reader_device is not None else None,
            )
        except EvidenceError as error:
            print(f"evidence retention: {error}", file=sys.stderr)
            return 2
        print(json.dumps(marker, sort_keys=True))
        return 0
    if arguments.action == "job-prune":
        print(json.dumps(prune_job_states(
            arguments.root, store=arguments.store, current=arguments.state_dir,
            keep_last=arguments.keep_last, max_bytes=arguments.max_bytes,
            dry_run=arguments.dry_run,
        ), sort_keys=True))
        return 0
    if arguments.action == "run-finalize":
        print(json.dumps(finalize_run_evidence(arguments.lane, arguments.run_dir), sort_keys=True))
        return 0
    if arguments.action == "run-finalize-unstarted":
        print(json.dumps(finalize_unstarted_run_evidence(arguments.run_dir), sort_keys=True))
        return 0
    if arguments.action == "run-ack":
        target = arguments.journal if arguments.journal is not None else arguments.run
        if target is None:
            print("evidence retention: run-ack needs --journal or --run", file=sys.stderr)
            return 2
        print(json.dumps({
            "acknowledged": ack_run_retirement(arguments.root, target),
            "journal": str(target),
        }, sort_keys=True))
        return 0
    print(json.dumps(prune_run_evidence(
        arguments.root, keep_last=arguments.keep_last, max_bytes=arguments.max_bytes,
        protect=arguments.protect, dry_run=arguments.dry_run,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

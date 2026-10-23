"""Preserve inspectable runtime proof without retaining generated build forests."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import struct
import tempfile
from pathlib import Path
from typing import Any

if __package__:
    from .evidence_storage import EvidenceError, compress_file, compress_paths, payload_metadata, verify_stored
    from .runtime_artifacts import COMPONENTS
else:
    from evidence_storage import EvidenceError, compress_file, compress_paths, payload_metadata, verify_stored
    from runtime_artifacts import COMPONENTS


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
    candidates = {build / component.source for component in COMPONENTS.values()}
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

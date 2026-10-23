"""Verify indexed payloads and legacy checksums without restoring raw files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

if __package__:
    from .evidence_storage import EvidenceError, payload_metadata, verify_stored
else:
    from evidence_storage import EvidenceError, payload_metadata, verify_stored


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _checksum_line(line: str) -> tuple[str, str]:
    escaped = line.startswith("\\")
    if escaped:
        line = line[1:]
    digest, separator, name = line[:64], line[64:66], line[66:]
    if len(digest) != 64 or any(character not in "0123456789abcdefABCDEF" for character in digest) or separator not in {"  ", " *"}:
        raise EvidenceError("invalid SHA256SUMS row")
    if escaped:
        decoded = []
        position = 0
        while position < len(name):
            if name[position] == "\\":
                position += 1
                if position >= len(name) or name[position] not in "\\nr":
                    raise EvidenceError("invalid escaped checksum filename")
                decoded.append({"\\": "\\", "n": "\n", "r": "\r"}[name[position]])
            else:
                decoded.append(name[position])
            position += 1
        name = "".join(decoded)
    if not name:
        raise EvidenceError("empty checksum filename")
    return digest.lower(), name


def verify_archive(root: Path, *, recursive: bool, write_manifest: bool = False) -> dict[str, Any]:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise EvidenceError(f"verification requires a real evidence directory: {root}")
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {".git", "__pycache__"} and not (Path(directory) / name).is_symlink()]
        if not recursive:
            dirs[:] = []
        files.extend(Path(directory) / name for name in names if (Path(directory) / name).is_file() and not (Path(directory) / name).is_symlink())
    payloads = checksums = 0
    errors = []
    for index_path in (path for path in files if path.name == "evidence-index.json"):
        try:
            index = json.loads(index_path.read_text())
            if index.get("schema") != 1 or not isinstance(index.get("files"), dict):
                raise EvidenceError("invalid evidence index schema")
            for logical, record in index["files"].items():
                stored_name = record["stored_path"]
                if Path(stored_name).name != stored_name or Path(logical).name != logical:
                    raise EvidenceError("index payload escapes its directory")
                stored = index_path.parent / stored_name
                if stored.is_symlink() or stored.stat().st_size != record["stored_bytes"] or _hash(stored) != record["stored_sha256"]:
                    raise EvidenceError(f"stored digest/size mismatch: {stored}")
                verify_stored(stored, record["sha256"], record["original_bytes"])
                payloads += 1
        except (EvidenceError, OSError, ValueError, KeyError, TypeError) as error:
            errors.append({"path": str(index_path), "error": str(error)})
    for manifest in (path for path in files if path.name == "SHA256SUMS"):
        try:
            for line in manifest.read_text().splitlines():
                if not line or line.startswith("#"):
                    continue
                digest, name = _checksum_line(line)
                path = manifest.parent / name
                # Some historical receipts were emitted from the workspace root,
                # rather than from their own directory. Resolve only an explicit
                # selected-root prefix, never guess outside the chosen archive.
                if (not path.is_file() and not path.with_name(path.name + ".zst").is_file()
                        and Path(name).parts[:1] == (root.name,)):
                    path = root.parent / name
                if path.resolve() == manifest.resolve():
                    raise EvidenceError("self-referential checksum receipt: it cannot hash its own contents")
                if not path.resolve().is_relative_to(root.resolve()):
                    raise EvidenceError(f"checksum target is outside selected archive: {name}")
                if path.is_file():
                    if _hash(path) != digest:
                        raise EvidenceError(f"checksum mismatch: {path}")
                else:
                    metadata = payload_metadata(path)
                    verify_stored(path.with_name(path.name + ".zst"), digest, metadata["bytes"])
                checksums += 1
        except (EvidenceError, OSError, ValueError) as error:
            errors.append({"path": str(manifest), "error": str(error)})
    result = {"root": str(root), "indexed_payloads": payloads, "checksums": checksums, "failed": bool(errors), "errors": errors}
    if write_manifest and not errors:
        destination = root / "SHA256SUMS"
        with destination.open("x", encoding="utf-8") as stream:
            for path in sorted(files):
                name = str(path.relative_to(root))
                escaped = any(character in name for character in "\\\n\r")
                name = name.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
                stream.write(("\\" if escaped else "") + _hash(path) + "  " + name + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        result["manifest"] = str(destination)
    return result

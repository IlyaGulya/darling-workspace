"""Immutable normal-install provenance, independent of mutable build and guest trees."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

RECEIPT_FILENAME = ".darling-deploy-receipt.json"
SCHEMA_VERSION = 2


class ReceiptProblem:
    """One actionable installed-image integrity problem."""
    __slots__ = ("label", "message")

    def __init__(self, label: str, message: str) -> None:
        self.label = label
        self.message = message


def receipt_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / RECEIPT_FILENAME


def _git(directory: Path, *arguments: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True,
        text=True, check=False,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def _worktree_identity(directory: Path) -> dict[str, Any]:
    if not (directory / ".git").exists():
        return {"revision": None, "dirty": None, "untracked": None}
    status = _git(directory, "status", "--porcelain", "--untracked-files=normal")
    entries = status.splitlines() if status is not None else []
    return {
        "revision": _git(directory, "rev-parse", "HEAD"),
        "dirty": any(not line.startswith("??") for line in entries) if status is not None else None,
        "untracked": sum(line.startswith("??") for line in entries) if status is not None else None,
    }


def workspace_identity(manifest_repo: Path) -> dict[str, Any]:
    identity = _worktree_identity(Path(manifest_repo))
    return {"directory": str(manifest_repo), "manifest_commit": identity.pop("revision"), **identity}


def _source_identity(source: Path) -> dict[str, Any]:
    gitlinks: list[dict[str, Any]] = []

    def visit(repository: Path, relative: Path) -> None:
        if not (repository / ".git").exists():
            return
        tree = _git(repository, "ls-tree", "-rz", "HEAD")
        if tree is None:
            raise ValueError(f"cannot record source gitlinks at {repository}")
        for record in tree.split("\0"):
            if not record:
                continue
            metadata, path = record.split("\t", 1)
            mode, kind, revision = metadata.split(" ")
            if mode != "160000" or kind != "commit":
                continue
            child = repository / path
            child_relative = relative / path
            gitlinks.append({
                "path": child_relative.as_posix(), "gitlink_revision": revision,
                **_worktree_identity(child),
            })
            visit(child, child_relative)

    visit(source, Path())
    return {"directory": str(source), **_worktree_identity(source), "gitlinks": gitlinks}


def _cache_configuration(cache: Path) -> dict[str, str]:
    configuration: dict[str, str] = {}
    for line in cache.read_text(encoding="utf-8").splitlines():
        if line.startswith(("#", "//")) or ":" not in line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        configuration[key.split(":", 1)[0]] = value
    return configuration


def _relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("inventory path must be a nonempty relative pathname")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"unsafe inventory path: {value!r}")
    return path


def _installed_entry(runtime_root: Path, relative: str) -> dict[str, Any]:
    """Walk directory capabilities without following any installed guest symlink."""
    parts = _relative_path(relative).parts
    descriptor = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for component in parts[:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        entry: dict[str, Any] = {"path": relative, "mode": stat.S_IMODE(metadata.st_mode)}
        if stat.S_ISLNK(metadata.st_mode):
            entry.update(kind="symlink", target=os.readlink(parts[-1], dir_fd=descriptor))
        elif stat.S_ISREG(metadata.st_mode):
            file_descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor,
            )
            with os.fdopen(file_descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise ValueError(f"installed file changed while recording {relative}")
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            entry.update(kind="file", sha256=digest, size=metadata.st_size)
        else:
            raise ValueError(f"install manifest names a non-file artifact: {relative}")
        return entry
    except OSError as error:
        raise OSError(error.errno, error.strerror, str(runtime_root / relative)) from error
    finally:
        os.close(descriptor)


def write_receipt(runtime_root: Path, payload: dict[str, Any]) -> Path:
    """Publish one schema-2 receipt atomically; caller owns installation lifecycle."""
    root = Path(runtime_root)
    path = receipt_path(root)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_receipt(runtime_root: Path) -> dict[str, Any] | None:
    path = receipt_path(runtime_root)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported installed-runtime receipt schema at {path}; expected {SCHEMA_VERSION}")
    if payload.get("runtime_root") != str(Path(runtime_root).resolve()):
        raise ValueError(f"receipt installation root differs from {runtime_root}")
    return payload


def _installed_paths(root: Path) -> set[str]:
    """Enumerate the complete image, including normal install-hook outputs."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    paths: set[str] = set()
    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        for directory, directories, files, directory_fd in os.fwalk(
            ".", dir_fd=descriptor, follow_symlinks=False, onerror=raise_walk_error,
        ):
            base = Path(directory)
            for name in directories:
                if stat.S_ISLNK(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                    paths.add((base / name).as_posix())
            for name in files:
                relative = (base / name).as_posix()
                if relative != RECEIPT_FILENAME:
                    paths.add(relative)
    finally:
        os.close(descriptor)
    return paths


def publish_install_receipt(
    runtime_root: Path, build_dir: Path, manifest_repo: Path,
    *, install_manifests: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """Bind complete installed bytes to the actual normal CMake install manifests."""
    root = Path(runtime_root).resolve()
    build = Path(build_dir).resolve()
    manifest = Path(manifest_repo).resolve()
    cache = build / "CMakeCache.txt"
    configuration = _cache_configuration(cache)
    configured = configuration.get("CMAKE_INSTALL_PREFIX")
    source_directory = configuration.get("CMAKE_HOME_DIRECTORY")
    if not configured or Path(configured).resolve() != root:
        raise ValueError(f"CMake build is not configured to install into {root}")
    if not source_directory or not Path(source_directory).is_dir():
        raise ValueError("CMake cache does not identify an available source directory")
    manifests = install_manifests or (build / "install_manifest.txt",)
    paths: set[str] = set()
    manifest_identities = []
    for install_manifest in manifests:
        contents = Path(install_manifest).read_bytes()
        manifest_identities.append({
            "name": Path(install_manifest).name,
            "sha256": hashlib.sha256(contents).hexdigest(),
        })
        for line in contents.decode("utf-8").splitlines():
            installed = Path(line)
            if not installed.is_absolute():
                raise ValueError(f"install manifest entry is not absolute: {line!r}")
            try:
                relative = installed.relative_to(root).as_posix()
            except ValueError as error:
                raise ValueError(f"install manifest entry escapes runtime root: {line}") from error
            _relative_path(relative)
            if relative == RECEIPT_FILENAME:
                raise ValueError("install manifest must not include its own provenance receipt")
            paths.add(relative)
    if not paths:
        raise ValueError("normal install manifest contains no installed files")
    paths.update(_installed_paths(root))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "runtime_root": str(root),
        "workspace": workspace_identity(manifest),
        "source": _source_identity(Path(source_directory).resolve()),
        "build": {
            "directory": str(build),
            "cache_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
            "configuration": configuration,
            "install_manifests": manifest_identities,
        },
        "artifacts": [_installed_entry(root, relative) for relative in sorted(paths)],
    }
    write_receipt(root, payload)
    return payload


def verify_receipt(payload: dict[str, Any]) -> tuple[list[ReceiptProblem], list[str]]:
    """Verify installed bytes only; rebuilding or removing the build never invalidates an image."""
    problems: list[ReceiptProblem] = []
    notes: list[str] = []
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return [ReceiptProblem("install receipt", "unsupported installed-runtime receipt schema")], notes
    raw_root = payload.get("runtime_root")
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        return [ReceiptProblem("install receipt", "receipt records no absolute runtime root")], notes
    root = Path(raw_root)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return [ReceiptProblem("install receipt", "receipt records no installed artifacts")], notes
    seen: set[str] = set()
    for row in artifacts:
        if not isinstance(row, dict):
            problems.append(ReceiptProblem("install receipt", "malformed inventory entry"))
            continue
        relative = row.get("path")
        label = f"installed {relative}"
        try:
            _relative_path(relative)
            if relative in seen:
                raise ValueError("duplicate inventory pathname")
            seen.add(relative)
            actual = _installed_entry(root, relative)
            if row != actual:
                problems.append(ReceiptProblem(label, f"installed kind, mode, target or content differs from receipt: {root / relative}"))
        except (OSError, ValueError) as error:
            problems.append(ReceiptProblem(label, str(error)))
    try:
        unexpected = _installed_paths(root) - seen
        for relative in sorted(unexpected):
            problems.append(ReceiptProblem(f"installed {relative}", f"unreceipted artifact in immutable runtime: {root / relative}"))
    except OSError as error:
        problems.append(ReceiptProblem("install receipt", f"cannot enumerate installed image {root}: {error}"))
    if not problems:
        notes.append(f"{len(artifacts)} installed files/symlinks match schema-{SCHEMA_VERSION} receipt at {root}")
    return problems, notes

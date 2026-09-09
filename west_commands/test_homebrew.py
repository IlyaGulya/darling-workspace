"""Pinned, stock Homebrew inputs for the rootless lz4 source-build scenario."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import stat
import tarfile
import tempfile
from typing import Iterator
import urllib.request

try:
    from .test_execution import run_bounded
except ImportError:
    from test_execution import run_bounded


BREW_COMMIT = "c4a3482c22876114a8c3cf8244815541e47b684f"
CORE_COMMIT = "ad6d3bbf8f5eac27a5ce90e695c6b41765d40bb7"
RUBY_VERSION = "4.0.6"
RUBY_SHA256 = "ef0bf45e34c07a111674b976e47da3f5e9d5be52eae127b714898ca6949a3c18"
LZ4_URL = "https://github.com/lz4/lz4/archive/refs/tags/v1.10.0.tar.gz"


@dataclass(frozen=True)
class PinnedInput:
    filename: str
    url: str
    sha256: str


BREW = PinnedInput(
    f"brew-{BREW_COMMIT}.tar.gz",
    f"https://codeload.github.com/Homebrew/brew/tar.gz/{BREW_COMMIT}",
    "6644f34c5e7b98134dcef3945eb2e379aefd72c8e19adf15ad5ab3235283fa74",
)
RUBY = PinnedInput(
    f"portable-ruby-{RUBY_VERSION}.x86_64_catalina.bottle.tar.gz",
    f"https://ghcr.io/v2/homebrew/core/portable-ruby/blobs/sha256:{RUBY_SHA256}",
    RUBY_SHA256,
)
FORMULA = PinnedInput(
    f"lz4-{CORE_COMMIT}.rb",
    f"https://raw.githubusercontent.com/Homebrew/homebrew-core/{CORE_COMMIT}/Formula/l/lz4.rb",
    "fd3222d24e20ec501ac116622330cf4c650e0c7926c337c8e864328851c438ca",
)
LZ4 = PinnedInput(
    "lz4-1.10.0.tar.gz",
    "https://codeload.github.com/lz4/lz4/tar.gz/refs/tags/v1.10.0",
    "537512904744b35e232912055ccf8ec66d768639ff3abe5788d90d792ec5f48b",
)
INPUTS = (BREW, RUBY, FORMULA, LZ4)


def _verify(path: Path, spec: PinnedInput) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"homebrew-lz4: expected regular cached input: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != spec.sha256:
        raise ValueError(
            f"homebrew-lz4: checksum mismatch for {path}: "
            f"expected {spec.sha256}, got {digest.hexdigest()}"
        )


def _acquire(cache: Path, spec: PinnedInput) -> Path:
    path = cache / spec.filename
    if path.exists() or path.is_symlink():
        _verify(path, spec)
        return path
    headers = {"User-Agent": "darling-west-homebrew-lz4"}
    if spec == RUBY:
        token_url = "https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/portable-ruby:pull"
        with urllib.request.urlopen(token_url, timeout=120) as response:
            headers["Authorization"] = "Bearer " + json.load(response)["token"]
    descriptor, temporary = tempfile.mkstemp(prefix=f".{spec.filename}.", dir=cache)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            request = urllib.request.Request(spec.url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                shutil.copyfileobj(response, output)
        _verify(temporary_path, spec)
        # A concurrent downloader may publish only the same verified input.
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def _archive_members(archive: tarfile.TarFile, root: str) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    names: set[str] = set()
    links: dict[str, str] = {}
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != root
            or (str(path) == root and not member.isdir())
            or member.name.rstrip("/") != str(path)
            or str(path) in names
            or not (member.isdir() or member.isfile() or member.issym())
        ):
            raise ValueError(f"homebrew-lz4: unsafe archive member: {member.name!r}")
        names.add(str(path))
        if member.issym():
            target = PurePosixPath(member.linkname)
            resolved = PurePosixPath(posixpath.normpath(str(path.parent / target)))
            if target.is_absolute() or not resolved.parts or resolved.parts[0] != root:
                raise ValueError(f"homebrew-lz4: escaping archive symlink: {member.name!r}")
            links[str(path)] = member.linkname
    for member in members:
        if any(str(parent) in links for parent in PurePosixPath(member.name).parents):
            raise ValueError(f"homebrew-lz4: archive member traverses a symlink: {member.name!r}")
    # Resolve link chains against archive metadata, including links in target
    # parents, before either host or guest extraction can encounter them.
    for name in links:
        pending = list(reversed(PurePosixPath(name).parts))
        resolved_parts: list[str] = []
        expansions = 0
        while pending:
            part = pending.pop()
            if part == "..":
                if len(resolved_parts) <= 1:
                    raise ValueError(f"homebrew-lz4: escaping archive symlink chain: {name!r}")
                resolved_parts.pop()
                continue
            candidate = "/".join([*resolved_parts, part])
            if candidate in links:
                expansions += 1
                if expansions > 40:
                    raise ValueError(f"homebrew-lz4: cyclic archive symlink chain: {name!r}")
                pending.extend(reversed(PurePosixPath(links[candidate]).parts))
            else:
                resolved_parts.append(part)
    return members


def _extract(path: Path, destination: Path, root: str) -> None:
    # Extract only into our new private staging directory. Never follow links
    # while writing files, preserve ordinary executable modes, ignore ownership.
    with tarfile.open(path, "r:gz") as archive:
        members = _archive_members(archive, root)
        for member in members:
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.isfile():
                with archive.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
        for member in members:
            if member.issym():
                (destination / member.name).symlink_to(member.linkname)
        for member in members:
            if member.issym():
                target = destination / member.name
                if not target.resolve().is_relative_to(destination / root):
                    raise ValueError(f"homebrew-lz4: escaping archive symlink chain: {member.name!r}")


def _directory_beneath(prefix: Path, relative: str) -> Path:
    directory = prefix
    for part in PurePosixPath(relative).parts:
        directory = directory / part
        if directory.is_symlink():
            raise ValueError(f"homebrew-lz4: refusing symlinked staging parent: {directory}")
        directory.mkdir(exist_ok=True)
    return directory


def _template_digest(template: Path) -> str:
    """Fingerprint immutable lower contents and metadata, excluding read atime."""
    if template.is_symlink() or not template.is_dir():
        raise ValueError(f"homebrew-lz4: missing regular lower template: {template}")
    digest = hashlib.sha256()
    for directory, directories, files in os.walk(template, followlinks=False):
        directories.sort()
        for name in sorted([*directories, *files]):
            path = Path(directory) / name
            metadata = path.lstat()
            record = [
                path.relative_to(template).as_posix(),
                metadata.st_mode, metadata.st_uid, metadata.st_gid,
                metadata.st_mtime_ns, metadata.st_nlink,
            ]
            if stat.S_ISLNK(metadata.st_mode):
                record.append(os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                content = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        content.update(block)
                record.append(content.hexdigest())
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"homebrew-lz4: unexpected lower object: {path}")
            digest.update(json.dumps(record, ensure_ascii=True).encode() + b"\n")
    return digest.hexdigest()


def _register_build_logs(environment: dict[str, str], work: Path) -> None:
    state = environment.get("WEST_JOB_STATE_DIR")
    if not state:
        return
    directory = Path(state) / "activity-logs.d"
    directory.mkdir(exist_ok=True)
    # Immutable NUL-delimited kind/path pairs; only the atomic .logs rename
    # makes a record visible. Register future formula logs before guest launch.
    descriptor, name = tempfile.mkstemp(prefix="homebrew-", suffix=".tmp", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            for path in (work / "logs", work / "logs/lz4"):
                output.write(b"directory\0" + os.fsencode(path.absolute()) + b"\0")
        temporary.replace(temporary.with_suffix(".logs"))
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def homebrew_lz4_context(env: dict[str, str] | None) -> Iterator[dict[str, str]]:
    environment = dict(os.environ if env is None else env)
    if not environment.get("DPREFIX") or not environment.get("DARLING_LAUNCHER"):
        raise ValueError("homebrew-lz4 requires West DPREFIX and DARLING_LAUNCHER")
    prefix = Path(environment["DPREFIX"]).resolve(strict=True)
    if prefix == Path("/") or not prefix.is_dir():
        raise ValueError("homebrew-lz4 requires a disposable guest prefix, not the host root")
    # Do not merge into a previous install or shadow an installation in the base.
    for relative in ("usr/local/Homebrew", "usr/local/bin/brew", "usr/local/Cellar/lz4"):
        for base in (prefix, prefix / "libexec/darling"):
            existing = base / relative
            if existing.exists() or existing.is_symlink():
                raise ValueError(f"homebrew-lz4 requires a fresh installation: {existing}")
    preflight = Path(__file__).resolve().parents[1] / "tests/run-homebrew-build-tools-preflight.sh"
    result = run_bounded(
        ["bash", str(preflight)],
        cwd=preflight.parent.parent,
        env=environment,
        timeout_seconds=135,
    )
    if result.returncode:
        raise ValueError(
            "homebrew-lz4: native build-tools preflight failed; "
            "provision the complete Homebrew runtime component before staging brew"
        )
    template = prefix / "libexec/darling"
    before = _template_digest(template)
    cache = Path(environment.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "west/darling-homebrew"
    cache.mkdir(parents=True, exist_ok=True)
    inputs = {spec.filename: _acquire(cache, spec) for spec in INPUTS}
    # Even though the guest, not the host, unpacks lz4, reject unsafe source input.
    with tarfile.open(inputs[LZ4.filename], "r:gz") as archive:
        _archive_members(archive, "lz4-1.10.0")
    local = _directory_beneath(prefix, "usr/local")
    binaries = _directory_beneath(prefix, "usr/local/bin")
    temporary = _directory_beneath(prefix, "private/var/tmp")
    work = Path(tempfile.mkdtemp(prefix="west-homebrew-lz4-", dir=temporary))
    _extract(inputs[BREW.filename], work, f"brew-{BREW_COMMIT}")
    brew = work / f"brew-{BREW_COMMIT}"
    vendor = brew / "Library/Homebrew/vendor"
    if (vendor / "portable-ruby-version").read_text().strip() != RUBY_VERSION:
        raise ValueError("homebrew-lz4: pinned brew and Ruby versions disagree")
    _extract(inputs[RUBY.filename], vendor, "portable-ruby")
    (vendor / "portable-ruby/current").symlink_to(RUBY_VERSION)
    formula = brew / "Library/Taps/homebrew/homebrew-core/Formula/l/lz4.rb"
    formula.parent.mkdir(parents=True)
    shutil.copyfile(inputs[FORMULA.filename], formula)
    downloads = work / "cache/downloads"
    downloads.mkdir(parents=True)
    cache_name = hashlib.sha256(LZ4_URL.encode()).hexdigest() + "--v1.10.0.tar.gz"
    shutil.copyfile(inputs[LZ4.filename], downloads / cache_name)
    for directory in ("home", "logs", "tmp"):
        (work / directory).mkdir()
    _register_build_logs(environment, work)
    (work / "inputs.json").write_text(json.dumps({
        "brew-commit": BREW_COMMIT,
        "homebrew-core-commit": CORE_COMMIT,
        "inputs": [vars(spec) for spec in INPUTS],
        "lower-template-sha256": before,
    }, indent=2) + "\n")
    # Reserve the destination without replacing even an empty existing directory,
    # then publish staged input. Failures leave only owned diagnostic files.
    destination = local / "Homebrew"
    destination.mkdir()
    for child in brew.iterdir():
        child.rename(destination / child.name)
    (binaries / "brew").symlink_to("../Homebrew/bin/brew")
    brew.rmdir()
    guest_work = "/" + work.relative_to(prefix).as_posix()
    environment["DARLING_HOMEBREW_LZ4_WORK"] = guest_work
    environment["DARLING_HOMEBREW_LZ4_INPUTS"] = str(work / "inputs.json")
    try:
        yield environment
    finally:
        after = _template_digest(template)
        (work / "lower-template-final.sha256").write_text(after + "\n")
        if after != before:
            raise ValueError(f"homebrew-lz4: lower template drift: {before} -> {after}")
    # Retain installed products and evidence; West owns prefix process cleanup.

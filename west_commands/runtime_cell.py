"""Fail-closed identity model for one deployed lifecycle acceptance cell."""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import subprocess
import yaml
from dataclasses import dataclass
from pathlib import Path

from prefix_state import PrefixState, PrefixStateError, read_prefix_state_model

MAX_BINDING_BYTES = 4096
BINDING_NAME = ".darling-runtime-lower-binding-v1"
LOCK_NAME = ".lifecycle.lock"


class RuntimeCellError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class SecurityMetadata:
    object_type: str
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class PathObservation:
    identity: ObjectIdentity
    security: SecurityMetadata
    nlink: int


@dataclass(frozen=True)
class RuntimeArtifact:
    name: str
    build_path: Path
    deployed_path: Path
    observation: PathObservation
    sha256: str


@dataclass(frozen=True)
class RuntimeCell:
    forest: Path
    workspace: Path
    build_dir: Path
    install_prefix: Path
    runtime_prefix: Path
    cohort_enabled: bool
    profile: str
    state: PrefixState
    binding: dict[str, str] | None
    artifacts: tuple[RuntimeArtifact, ...]


ARTIFACT_PATHS = {
    "darling": ("src/startup/darling", "bin/darling"),
    "darlingserver": ("src/external/darlingserver/darlingserver", "bin/darlingserver"),
    "launchd": ("src/launchd/src/launchd", "libexec/darling/sbin/launchd"),
    "shellspawn": ("src/shellspawn/shellspawn", "libexec/darling/usr/libexec/shellspawn"),
    "mldr": ("src/startup/mldr/mldr", "libexec/darling/usr/libexec/darling/mldr"),
    "mldr32": ("src/startup/mldr/mldr32", "libexec/darling/usr/libexec/darling/mldr32"),
    "dyld": ("src/external/dyld/dyld", "libexec/darling/usr/lib/dyld"),
    "libsystem_kernel": (
        "src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib",
        "libexec/darling/usr/lib/system/libsystem_kernel.dylib",
    ),
    "vchroot": ("src/vchroot/vchroot", "libexec/darling/usr/libexec/darling/vchroot"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def observe(path: Path) -> PathObservation:
    value = path.lstat()
    if stat.S_ISREG(value.st_mode):
        kind = "regular"
    elif stat.S_ISDIR(value.st_mode):
        kind = "directory"
    elif stat.S_ISSOCK(value.st_mode):
        kind = "socket"
    elif stat.S_ISFIFO(value.st_mode):
        kind = "fifo"
    elif stat.S_ISLNK(value.st_mode):
        kind = "symlink"
    else:
        kind = "other"
    return PathObservation(
        ObjectIdentity(value.st_dev, value.st_ino),
        SecurityMetadata(kind, stat.S_IMODE(value.st_mode), value.st_uid, value.st_gid),
        value.st_nlink,
    )


def same_object(left: PathObservation, right: PathObservation) -> bool:
    """Identity excludes security metadata and mutable link topology."""
    return left.identity == right.identity


def _observe_beneath(root: Path, relative: str) -> PathObservation:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeCellError("binding destination is not a strict relative path")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            if index + 1 < len(parts):
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        value = os.fstat(descriptor)
    except OSError as error:
        raise RuntimeCellError("binding destination cannot be retained beneath prefix") from error
    finally:
        os.close(descriptor)
    if stat.S_ISREG(value.st_mode):
        kind = "regular"
    elif stat.S_ISDIR(value.st_mode):
        kind = "directory"
    else:
        kind = "other"
    return PathObservation(
        ObjectIdentity(value.st_dev, value.st_ino),
        SecurityMetadata(kind, stat.S_IMODE(value.st_mode), value.st_uid, value.st_gid),
        value.st_nlink,
    )


def _cache_values(build_dir: Path, key: str) -> list[tuple[str, str]]:
    cache = build_dir / "CMakeCache.txt"
    try:
        lines = cache.read_text().splitlines()
    except OSError as error:
        raise RuntimeCellError(f"CMake cache unavailable: {cache}") from error
    result = []
    prefix = f"{key}:"
    for line in lines:
        if line.startswith(prefix) and "=" in line:
            typed, value = line.split("=", 1)
            result.append((typed[len(prefix) :], value))
    return result


def _cache_path(build_dir: Path, key: str, *, types: tuple[str, ...] = ("PATH",)) -> Path:
    values = _cache_values(build_dir, key)
    if len(values) != 1 or values[0][0] not in types or not values[0][1]:
        raise RuntimeCellError(f"CMake cache has no unique {key}:{'|'.join(types)}")
    return Path(values[0][1]).resolve(strict=False)


def _cache_bool(build_dir: Path, key: str) -> bool:
    values = _cache_values(build_dir, key)
    if len(values) != 1 or values[0][0] != "BOOL" or values[0][1] not in {"ON", "OFF"}:
        raise RuntimeCellError(f"CMake cache has no unique {key}:BOOL=ON|OFF")
    return values[0][1] == "ON"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeCellError(f"incomplete Git forest at {repository}: {' '.join(arguments)}")
    return result.stdout.strip()


def _validate_forest(forest: Path, workspace: Path) -> None:
    if not (forest / ".west/config").is_file():
        raise RuntimeCellError("runtime cell is not a West forest")
    manifest_workspace = forest / "workspace"
    if not manifest_workspace.is_dir() or not (forest / "darling").is_dir():
        raise RuntimeCellError("runtime cell West topology is incomplete")
    for repository in (manifest_workspace, forest / "darling", workspace):
        head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}")
        if len(head) != 40:
            raise RuntimeCellError(f"invalid Git HEAD at {repository}")
    lock = manifest_workspace / "west.lock.yml"
    if not lock.is_file() or lock.stat().st_size == 0:
        raise RuntimeCellError("authoritative West lock is unavailable")
    try:
        document = yaml.safe_load(lock.read_text())
        projects = document["manifest"]["projects"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as error:
        raise RuntimeCellError("authoritative West lock is malformed") from error
    if not isinstance(projects, list) or not projects:
        raise RuntimeCellError("authoritative West lock has no projects")
    for project in projects:
        if not isinstance(project, dict):
            raise RuntimeCellError("authoritative West project is malformed")
        path = project.get("path", project.get("name"))
        revision = project.get("revision")
        if not isinstance(path, str) or not isinstance(revision, str):
            raise RuntimeCellError("authoritative West project identity is malformed")
        repository = forest / path
        if not repository.is_dir():
            raise RuntimeCellError(f"West project is missing: {path}")
        _git(repository, "rev-parse", "--verify", "HEAD^{commit}")
        _git(repository, "cat-file", "-e", f"{revision}^{{commit}}")


def _read_binding(prefix: Path, state: PrefixState, required: bool) -> dict[str, str] | None:
    path = prefix / BINDING_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        if required:
            raise RuntimeCellError("runtime lower binding is missing")
        return None
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise RuntimeCellError("runtime lower binding identity is hostile")
        data = os.read(descriptor, MAX_BINDING_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_BINDING_BYTES:
        raise RuntimeCellError("runtime lower binding exceeds budget")
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeError as error:
        raise RuntimeCellError("runtime lower binding is malformed") from error
    if not lines or lines[0] != "DARLING_RUNTIME_LOWER_BINDING_V2":
        raise RuntimeCellError("runtime lower binding schema mismatch")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if "=" not in line:
            raise RuntimeCellError("runtime lower binding is malformed")
        key, value = line.split("=", 1)
        if not key or key in fields:
            raise RuntimeCellError("runtime lower binding is ambiguous")
        fields[key] = value
    required_fields = {
        "schema_version", "prefix_generation", "session_prefix_device",
        "session_prefix_inode", "destination", "prefix_device", "prefix_inode",
        "lower_device", "lower_inode", "lower_type", "lower_mode", "lower_uid",
        "lower_gid", "controller_destination", "controller_device", "controller_inode",
        "controller_type", "controller_mode", "controller_uid", "controller_gid",
        "provenance", "transaction_id",
    }
    if set(fields) != required_fields:
        raise RuntimeCellError("runtime lower binding field set is not exact")
    try:
        numeric_names = {
            name for name in required_fields
            if name.endswith(("device", "inode", "mode", "uid", "gid"))
            or name in {"schema_version", "prefix_generation"}
        }
        numeric = {name: int(fields[name]) for name in numeric_names}
    except ValueError as error:
        raise RuntimeCellError("runtime lower binding numeric field is malformed") from error
    prefix_observation = observe(prefix)
    if fields["destination"] != "libexec/darling" or fields["controller_destination"] != "bin/darlingserver":
        raise RuntimeCellError("runtime lower binding destination mismatch")
    lower = _observe_beneath(prefix, fields["destination"])
    controller = _observe_beneath(prefix, fields["controller_destination"])
    if (
        numeric["schema_version"] != 2
        or numeric["prefix_generation"] != state.generation
        or (numeric["session_prefix_device"], numeric["session_prefix_inode"])
        != (state.prefix_device, state.prefix_inode)
        or (numeric["prefix_device"], numeric["prefix_inode"])
        != (prefix_observation.identity.device, prefix_observation.identity.inode)
        or (numeric["lower_device"], numeric["lower_inode"])
        != (lower.identity.device, lower.identity.inode)
        or (numeric["controller_device"], numeric["controller_inode"])
        != (controller.identity.device, controller.identity.inode)
        or fields["lower_type"] != lower.security.object_type
        or numeric["lower_mode"] != lower.security.mode
        or numeric["lower_uid"] != lower.security.uid
        or numeric["lower_gid"] != lower.security.gid
        or fields["controller_type"] != controller.security.object_type
        or numeric["controller_mode"] != controller.security.mode
        or numeric["controller_uid"] != controller.security.uid
        or numeric["controller_gid"] != controller.security.gid
        or fields["provenance"] != "product-deployment-transaction-v2"
    ):
        raise RuntimeCellError("runtime lower binding identity mismatch")
    return fields


def _validate_lock(prefix: Path) -> None:
    prefix_value = prefix.stat()
    path = prefix / LOCK_NAME
    try:
        named = path.lstat()
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise RuntimeCellError("lifecycle lock is missing or cannot be retained") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_uid, opened.st_gid) != (prefix_value.st_uid, prefix_value.st_gid)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise RuntimeCellError("lifecycle lock metadata or identity mismatch")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeCellError("lifecycle lock is active") from error
        after = path.lstat()
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeCellError("lifecycle lock replaced after acquisition")
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def load_runtime_cell(
    *, forest: Path, workspace: Path, build_dir: Path, runtime_prefix: Path,
    cohort_enabled: bool, profile: str,
) -> RuntimeCell:
    forest = forest.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    build_dir = build_dir.resolve(strict=True)
    runtime_prefix = runtime_prefix.resolve(strict=True)
    if profile != "homebrew-rootless-bootstrap-minimal":
        raise RuntimeCellError(f"wrong runtime profile: {profile}")
    _validate_forest(forest, workspace)
    source_root = _cache_path(
        build_dir, "CMAKE_HOME_DIRECTORY", types=("INTERNAL", "PATH")
    )
    if source_root != (forest / "darling").resolve(strict=True):
        raise RuntimeCellError("build belongs to a stale or foreign Darling source tree")
    controller_crate = _cache_path(build_dir, "DARLING_LIFECYCLE_CONTROLLER_CRATE")
    if controller_crate != (workspace / "lifecycle/operation-boundary").resolve(strict=True):
        raise RuntimeCellError("build belongs to a stale or foreign lifecycle controller")
    install_prefix = _cache_path(build_dir, "CMAKE_INSTALL_PREFIX")
    if install_prefix != runtime_prefix:
        raise RuntimeCellError("build/runtime prefix mismatch")
    if _cache_bool(build_dir, "DARLING_LIFECYCLE_COHORT_V1") != cohort_enabled:
        raise RuntimeCellError("build/runtime cohort mode mismatch")
    try:
        state_model = read_prefix_state_model(runtime_prefix)
    except PrefixStateError as error:
        raise RuntimeCellError(str(error)) from error
    if state_model.kind != "v3":
        raise RuntimeCellError("runtime cell requires authoritative prefix state v3")
    state = state_model.state
    if state.runtime_mode != "rootless-eunion":
        raise RuntimeCellError("runtime prefix has wrong profile mode")
    _validate_lock(runtime_prefix)
    binding = _read_binding(runtime_prefix, state, cohort_enabled)
    artifacts = []
    for name, (build_relative, deployed_relative) in ARTIFACT_PATHS.items():
        build_path = build_dir / build_relative
        deployed_path = runtime_prefix / deployed_relative
        try:
            build_observation = observe(build_path)
            deployed_observation = observe(deployed_path)
        except OSError as error:
            raise RuntimeCellError(f"runtime artifact is missing: {name}") from error
        if (
            build_observation.security.object_type != "regular"
            or deployed_observation.security.object_type != "regular"
            or _sha256(build_path) != _sha256(deployed_path)
        ):
            raise RuntimeCellError(f"runtime artifact is stale or mixed: {name}")
        artifacts.append(RuntimeArtifact(name, build_path, deployed_path, deployed_observation, _sha256(deployed_path)))
    return RuntimeCell(
        forest, workspace, build_dir, install_prefix, runtime_prefix,
        cohort_enabled, profile, state, binding, tuple(artifacts),
    )

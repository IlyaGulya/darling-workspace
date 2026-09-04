from __future__ import annotations

import concurrent.futures
import contextlib
import ctypes
import errno
import copy
import fcntl
import hashlib
import json
import re
import os
import shutil
import shlex
import signal
import stat
import subprocess
import threading
import sys
import time
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

import patch_stack_lock_first
import patch_stack_materialize
import yaml
from test_runtime_build import RuntimeBuildService

SCHEMA_VERSION = 1
TIER_ORDER = ("quick", "canonical", "acceptance")
_TIER_RANK = {name: rank for rank, name in enumerate(TIER_ORDER)}
_CAPTURE_LIMIT = 16 * 1024
_JSON_LIMIT = 8 * 1024 * 1024
_SNAPSHOT_DIFF_LIMIT = 64 * 1024 * 1024
_UNTRACKED_LIST_LIMIT = 1024 * 1024
_GENERATED_LOCK_LIMIT = 1_000_000
_UNTRACKED_FILE_LIMIT = 16 * 1024 * 1024
_UNTRACKED_TOTAL_LIMIT = 64 * 1024 * 1024
_UNTRACKED_COUNT_LIMIT = 4096
_TERMINATE_GRACE_SECONDS = 5.0
_CHECKPOINT_SCHEMA_VERSION = 1
_INITIAL_CLONE_STEP_NAMES = (
    "acceptance-clone-control",
    "acceptance-clone-candidate",
)
_CHECKPOINT_TOOL_VERSION = "west-dev-acceptance-v1"
_CHECKPOINT_STEP_NAMES = (
    "patch-verify",
    "host-materialized-test",
    "immutable-oracle",
)
_FINAL_TIER_STEP_NAMES = (
    "acceptance-clone-guest-candidate",
    "acceptance-host-tier",
    "acceptance-guest-smoke",
)
_CHECKPOINT_LIMIT = 8 * 1024 * 1024
_CANDIDATE_CACHE_PRUNED_STEPS = frozenset(
    {
        "acceptance-seed-candidate-refs",
        "acceptance-capture",
        "acceptance-publish-candidate-cache",
    }
)
_PACKAGE_HASH_WORKERS = min(8, max(1, os.cpu_count() or 1))
_ACCEPTANCE_BOOTSTRAP_JOBS = 8
_PACKAGE_CACHE_SCHEMA_VERSION = 1
_FICLONE = 0x40049409
_PARALLEL_ENVIRONMENT_NAMES = frozenset(
    {
        "CC",
        "CFLAGS",
        "CMAKE_PREFIX_PATH",
        "CMAKE_C_COMPILER_LAUNCHER",
        "CMAKE_CXX_COMPILER_LAUNCHER",
        "CMAKE_GENERATOR",
        "CMAKE_MAKE_PROGRAM",
        "CMAKE_TOOLCHAIN_FILE",
        "CODEX_CI",
        "COLORTERM",
        "CXX",
        "CXXFLAGS",
        "DEV_CHECK_FAKE_LOG",
        "DEV_CHECK_FAKE_MODE",
        "DEV_CHECK_INTERRUPT_MARKERS",
        "DEV_CHECK_INTERRUPT_READY",
        "DEV_CHECK_PARALLEL_BARRIER",
        "DEV_CHECK_FINAL_TIER_BARRIER",
        "DEV_CHECK_FINAL_TIER_CLEANUP",
        "DEV_CHECK_PARALLEL_CLEANUP",
        "FORCE_COLOR",
        "LANG",
        "LC_ALL",
        "LDFLAGS",
        "MAKEFLAGS",
        "NINJA_STATUS",
        "NO_COLOR",
        "PATH",
        "PKG_CONFIG_PATH",
        "SOURCE_DATE_EPOCH",
        "TERM",
        "TZ",
    }
)



_CHECK_TIMEOUTS = {
    "patch-check": 900,
    "patch-export-check": 1800,
    "test-list": 900,
    "patch-verify": 3600,
    "host-materialized-test": 7200,
    "doctor": 300,
    "clone": 1800,
    "checkout": 300,
    "bootstrap": 3600,
    "identity": 300,
    "seed-source-refs": 1800,
    "immutable-oracle": 3600,
    "candidate-apply": 3600,
    "acceptance-capture": 300,
    "acceptance-compare": 1800,
    "acceptance-host-tier": 10800,
    "acceptance-guest-smoke": 10800,
    "clone-tier-workspace": 1800,
    "acceptance-final-cleanup": 1800,
    "candidate-cache-publish": 1800,
}
_PACKAGE_TIMEOUT_SECONDS = 3600


class DevCheckError(RuntimeError):
    """A malformed plan, receipt, or produced package."""


class _BoundedCapture:
    def __init__(
        self, limit: int = _CAPTURE_LIMIT, full_limit: int = 0
    ) -> None:
        self._limit = limit
        self._full_limit = full_limit
        self._tail = bytearray()
        self._full = bytearray()
        self._full_overflow = False
        self._digest = hashlib.sha256()
        self._count = 0
        self.error: str | None = None

    def consume(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                self._digest.update(chunk)
                self._count += len(chunk)
                self._tail.extend(chunk)
                if len(self._tail) > self._limit:
                    del self._tail[: len(self._tail) - self._limit]
                if self._full_limit and not self._full_overflow:
                    if len(self._full) + len(chunk) <= self._full_limit:
                        self._full.extend(chunk)
                    else:
                        self._full.clear()
                        self._full_overflow = True
        except BaseException as error:  # The owner still must reap the process.
            self.error = f"{type(error).__name__}: {error}"
        finally:
            stream.close()

    def full(self) -> bytes | None:
        if not self._full_limit or self._full_overflow:
            return None
        return bytes(self._full)

    def result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "bytes": self._count,
            "sha256": self._digest.hexdigest(),
            "tail": bytes(self._tail).decode("utf-8", errors="replace"),
            "truncated": self._count > self._limit,
        }
        if self._full_limit:
            result["full_capture_overflow"] = self._full_overflow
        if self.error is not None:
            result["capture_error"] = self.error
        return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _absolute(path: Path, name: str) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_absolute():
        raise DevCheckError(f"{name} must be absolute")
    return value


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()
def _is_proc_fd_root(path: Path) -> bool:
    parts = path.absolute().parts
    return (
        len(parts) == 5
        and parts[:4] == ("/", "proc", "self", "fd")
        and parts[4].isdigit()
    )




def _validate_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise DevCheckError(f"{name} must be a non-empty string")
    return value


def _validate_west_argv(west_argv: list[str]) -> list[str]:
    if not isinstance(west_argv, list) or not west_argv:
        raise DevCheckError("west_argv must be a non-empty list")
    return [_validate_text(arg, "west_argv entry") for arg in west_argv]


def _tier(value: str, name: str = "tier") -> str:
    if value not in _TIER_RANK:
        raise DevCheckError(f"{name} must be one of: {', '.join(TIER_ORDER)}")
    return value


def _step(
    name: str,
    argv: list[str],
    effect: str,
    timeout_seconds: int,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "argv": argv,
        "cwd": str(cwd),
        "env": dict(sorted((env or {}).items())),
        "effect": effect,
        "read_only": effect == "read-only",
        "mutating": effect != "read-only",
        "timeout_seconds": timeout_seconds,
    }


def _test_selectors(profile: str, bead: str | None, patch: str | None) -> list[str]:
    result = ["--profile", profile]
    if bead is not None:
        result.extend(("--bead", bead))
    if patch is not None:
        result.extend(("--patch", patch))
    return result


def _validate_checkpoint_binding(
    value: object, profile: str, west_argv: list[str]
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "key",
        "path",
        "identity",
    }:
        raise DevCheckError("acceptance checkpoint binding is invalid")
    key = value.get("key")
    path_value = value.get("path")
    identity = value.get("identity")
    if (
        value.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION
        or not isinstance(key, str)
        or len(key) != 64
        or any(character not in "0123456789abcdef" for character in key)
        or not isinstance(path_value, str)
        or not isinstance(identity, dict)
    ):
        raise DevCheckError("acceptance checkpoint binding is malformed")
    path = Path(path_value)
    if (
        not path.is_absolute()
        or path.name != f"acceptance-{key}.json"
        or path.parent.name != "west-dev-checkpoints"
    ):
        raise DevCheckError("acceptance checkpoint path is invalid")
    if identity.get("west_argv") != west_argv:
        raise DevCheckError("acceptance checkpoint launcher identity differs")
    tool_files = identity.get("tool_files")
    if not isinstance(tool_files, list) or not tool_files:
        raise DevCheckError("acceptance checkpoint tool identity is missing")
    for row in tool_files:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "sha256", "bytes"}
            or not isinstance(row.get("path"), str)
            or not Path(row["path"]).is_absolute()
            or not _is_lower_hex(row.get("sha256"), 64)
            or not isinstance(row.get("bytes"), int)
            or isinstance(row.get("bytes"), bool)
            or row["bytes"] <= 0
        ):
            raise DevCheckError("acceptance checkpoint tool identity is invalid")
    host_tools = identity.get("host_tools")
    if not isinstance(host_tools, list) or not host_tools:
        raise DevCheckError(
            "acceptance checkpoint host tool identity is missing"
        )
    for row in host_tools:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"name", "command", "path", "sha256", "bytes"}
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or not isinstance(row.get("command"), str)
            or not row["command"]
            or not isinstance(row.get("path"), str)
            or not Path(row["path"]).is_absolute()
            or not _is_lower_hex(row.get("sha256"), 64)
            or not isinstance(row.get("bytes"), int)
            or isinstance(row.get("bytes"), bool)
            or row["bytes"] <= 0
        ):
            raise DevCheckError(
                "acceptance checkpoint host tool identity is invalid"
            )
    if (
        not isinstance(identity.get("west_version"), str)
        or not identity["west_version"]
    ):
        raise DevCheckError("acceptance checkpoint West version is invalid")
    parallel_environment = identity.get("parallel_environment")
    if (
        not isinstance(parallel_environment, dict)
        or any(
            name not in _PARALLEL_ENVIRONMENT_NAMES
            or not isinstance(environment_value, str)
            for name, environment_value in parallel_environment.items()
        )
    ):
        raise DevCheckError(
            "acceptance checkpoint parallel environment is invalid"
        )
    west_package = identity.get("west_package")
    if west_package is not None and (
        not isinstance(west_package, dict)
        or set(west_package) != {
            "path",
            "sha256",
            "file_count",
            "bytes",
        }
        or not isinstance(west_package.get("path"), str)
        or not Path(west_package["path"]).is_absolute()
        or not _is_lower_hex(west_package.get("sha256"), 64)
        or any(
            not isinstance(west_package.get(field), int)
            or isinstance(west_package.get(field), bool)
            or west_package[field] < 0
            for field in ("file_count", "bytes")
        )
    ):
        raise DevCheckError("acceptance checkpoint West package is invalid")
    if (
        identity.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION
        or identity.get("tool_version") != _CHECKPOINT_TOOL_VERSION
        or identity.get("profile") != profile
    ):
        raise DevCheckError("acceptance checkpoint identity version differs")
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != key:
        raise DevCheckError("acceptance checkpoint identity digest differs")
    return value


def _validate_acceptance_selection(inputs: dict[str, Any]) -> None:
    if inputs["profile"] != "homebrew":
        raise DevCheckError("acceptance checks require profile 'homebrew'")
    if inputs["bead"] is not None or inputs["patch"] is not None:
        raise DevCheckError("acceptance checks cannot be narrowed by bead or patch")
    if inputs["prefix"] is None or inputs["build_dir"] is None:
        raise DevCheckError(
            "acceptance checks require both prefix and build_dir runtime prerequisites"
        )


def _validate_acceptance_inputs(inputs: dict[str, Any]) -> None:
    if inputs["tier"] != "acceptance":
        if inputs.get("acceptance_checkpoint") is not None:
            raise DevCheckError("non-acceptance checks cannot use checkpoints")
        return
    _validate_acceptance_selection(inputs)
    _validate_checkpoint_binding(
        inputs.get("acceptance_checkpoint"),
        inputs["profile"],
        inputs["west_argv"],
    )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise DevCheckError(f"cannot hash package input {path}: {error}") from error
    return digest.hexdigest(), size


def _content_snapshot(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise DevCheckError(f"package input cannot be a symlink: {path}")
    if path.is_file():
        digest, size = _hash_file(path)
        return {"path": str(path), "sha256": digest, "file_count": 1, "bytes": size}
    if not path.is_dir():
        raise DevCheckError(f"required package input is missing: {path}")
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        if child.is_symlink():
            raise DevCheckError(f"package input cannot contain a symlink: {child}")
        if child.is_dir():
            continue
        if not child.is_file():
            raise DevCheckError(f"package input is not a regular file: {child}")
        relative = child.relative_to(path).as_posix().encode("utf-8")
        child_digest, child_size = _hash_file(child)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(child_digest))
        digest.update(child_size.to_bytes(8, "big"))
        file_count += 1
        byte_count += child_size
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "bytes": byte_count,
    }


def _collect_package_snapshot(manifest_repo: Path, profile: str) -> dict[str, Any]:
    head_argv = ["git", "-C", str(manifest_repo), "rev-parse", "--verify", "HEAD"]
    tree_argv = [
        "git",
        "-C",
        str(manifest_repo),
        "rev-parse",
        "--verify",
        "HEAD^{tree}",
    ]
    status_argv = [
        "git",
        "-C",
        str(manifest_repo),
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ]
    diff_argv = ["git", "-C", str(manifest_repo), "diff", "--binary", "HEAD", "--"]
    staged_argv = [
        "git",
        "-C",
        str(manifest_repo),
        "diff",
        "--binary",
        "--cached",
        "HEAD",
        "--",
    ]
    untracked_argv = [
        "git",
        "-C",
        str(manifest_repo),
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
    ]
    head_result = _run_process(head_argv, manifest_repo, 60, {})
    tree_result = _run_process(tree_argv, manifest_repo, 60, {})
    for name, result in (("HEAD", head_result), ("HEAD tree", tree_result)):
        if (
            result["returncode"] != 0
            or result["stdout"]["truncated"]
            or "capture_error" in result["stdout"]
            or "capture_error" in result["stderr"]
        ):
            raise DevCheckError(
                f"cannot resolve manifest repository {name} for package snapshot"
            )
    head = head_result["stdout_tail"].strip()
    tree = tree_result["stdout_tail"].strip()
    for name, oid in (("HEAD", head), ("HEAD tree", tree)):
        if len(oid) != 40 or any(
            character not in "0123456789abcdef" for character in oid
        ):
            raise DevCheckError(f"manifest repository {name} is not a full object ID")
    status_result = _run_process(status_argv, manifest_repo, 60, {})
    diff_result = _run_process(diff_argv, manifest_repo, 300, {})
    staged_result = _run_process(staged_argv, manifest_repo, 300, {})
    untracked_result = _run_process(
        untracked_argv, manifest_repo, 60, {}, _UNTRACKED_LIST_LIMIT
    )
    for name, result in (
        ("status", status_result),
        ("working-tree diff", diff_result),
        ("staged diff", staged_result),
        ("untracked file list", untracked_result),
    ):
        if (
            result["returncode"] != 0
            or "capture_error" in result["stdout"]
            or "capture_error" in result["stderr"]
        ):
            raise DevCheckError(f"cannot capture manifest repository {name}")
    if status_result["stdout"]["bytes"] > _UNTRACKED_LIST_LIMIT:
        raise DevCheckError("manifest repository status exceeds snapshot bound")
    if (
        diff_result["stdout"]["bytes"] > _SNAPSHOT_DIFF_LIMIT
        or staged_result["stdout"]["bytes"] > _SNAPSHOT_DIFF_LIMIT
    ):
        raise DevCheckError("manifest repository binary diff exceeds snapshot bound")
    raw_untracked = untracked_result.get("_stdout_full")
    if not isinstance(raw_untracked, bytes):
        raise DevCheckError("untracked file list exceeds snapshot bound")
    raw_paths = raw_untracked.split(b"\0")
    if raw_paths and raw_paths[-1] == b"":
        raw_paths.pop()
    if len(raw_paths) > _UNTRACKED_COUNT_LIMIT:
        raise DevCheckError("untracked file count exceeds snapshot bound")
    untracked_rows: list[dict[str, Any]] = []
    untracked_digest = hashlib.sha256()
    untracked_bytes = 0
    seen_paths: set[bytes] = set()
    for raw_path in raw_paths:
        if not raw_path or raw_path in seen_paths:
            raise DevCheckError("untracked file list contains an invalid duplicate path")
        seen_paths.add(raw_path)
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise DevCheckError("untracked file list contains an unsafe path")
        path = manifest_repo / relative
        try:
            metadata = path.lstat()
        except OSError as error:
            raise DevCheckError(f"cannot inspect untracked file {relative}: {error}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise DevCheckError(f"untracked input is not a regular file: {relative}")
        if metadata.st_size > _UNTRACKED_FILE_LIMIT:
            raise DevCheckError(f"untracked input exceeds per-file bound: {relative}")
        content_digest, size = _hash_file(path)
        if size != metadata.st_size:
            raise DevCheckError(f"untracked input changed while hashing: {relative}")
        untracked_bytes += size
        if untracked_bytes > _UNTRACKED_TOTAL_LIMIT:
            raise DevCheckError("untracked input bytes exceed snapshot bound")
        untracked_digest.update(len(raw_path).to_bytes(8, "big"))
        untracked_digest.update(raw_path)
        untracked_digest.update(bytes.fromhex(content_digest))
        untracked_digest.update(size.to_bytes(8, "big"))
        untracked_rows.append(
            {"path": os.fsdecode(raw_path), "sha256": content_digest, "bytes": size}
        )
    targets = {
        "west.yml": manifest_repo / "west.yml",
        "west.lock.yml": manifest_repo / "west.lock.yml",
        f"patches/{profile}": manifest_repo / "patches" / profile,
        "locks/patch-stack": manifest_repo / "locks" / "patch-stack",
    }
    return {
        "manifest_repo": str(manifest_repo),
        "manifest_head": head,
        "manifest_tree": tree,
        "dirty": (
            status_result["stdout"]["bytes"] != 0
            or diff_result["stdout"]["bytes"] != 0
            or staged_result["stdout"]["bytes"] != 0
            or bool(untracked_rows)
        ),
        "status": {
            "sha256": status_result["stdout"]["sha256"],
            "bytes": status_result["stdout"]["bytes"],
        },
        "working_tree_diff": {
            "sha256": diff_result["stdout"]["sha256"],
            "bytes": diff_result["stdout"]["bytes"],
        },
        "staged_diff": {
            "sha256": staged_result["stdout"]["sha256"],
            "bytes": staged_result["stdout"]["bytes"],
        },
        "untracked": {
            "sha256": untracked_digest.hexdigest(),
            "bytes": untracked_bytes,
            "count": len(untracked_rows),
            "files": untracked_rows,
        },
        "commands": [
            head_argv,
            tree_argv,
            status_argv,
            diff_argv,
            staged_argv,
            untracked_argv,
        ],
        "content": {
            name: _content_snapshot(path) for name, path in sorted(targets.items())
        },
    }
def _safe_profile_component(value: object, label: str) -> str:
    profile = _validate_text(value, label)
    if Path(profile).name != profile or profile in {".", ".."}:
        raise DevCheckError(f"{label} must be one safe path component")
    return profile


def _load_checkpoint_profile(manifest_repo: Path, profile: str) -> tuple[Path, dict[str, Any]]:
    path = manifest_repo / "patches" / profile / "patches.yml"
    if path.is_symlink() or not path.is_file():
        raise DevCheckError(f"checkpoint profile manifest is unavailable: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DevCheckError(f"checkpoint profile manifest is invalid: {path}: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("patches"), list):
        raise DevCheckError(f"checkpoint profile manifest has no patch list: {path}")
    return path, value


def _acceptance_tool_files(west_argv: list[str]) -> list[dict[str, Any]]:
    candidates = [west_argv[0], "git", sys.executable]
    candidates.extend(
        argument
        for argument in west_argv[1:]
        if Path(argument).is_absolute()
    )
    resolved: set[Path] = set()
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        if Path(candidate).is_absolute():
            selected = Path(candidate)
        else:
            located = shutil.which(candidate)
            if located is None:
                raise DevCheckError(
                    f"checkpoint tool is unavailable: {candidate}"
                )
            selected = Path(located)
        try:
            path = selected.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise DevCheckError(
                f"checkpoint tool is unavailable: {candidate}: {error}"
            ) from error
        if not path.is_file():
            raise DevCheckError(f"checkpoint tool is not a file: {path}")
        if path in resolved:
            continue
        resolved.add(path)
        digest, size = _hash_file(path)
        rows.append({"path": str(path), "sha256": digest, "bytes": size})
    return sorted(rows, key=lambda row: row["path"])


def _acceptance_parallel_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in sorted(_PARALLEL_ENVIRONMENT_NAMES)
        if name in os.environ
    }
    if "PATH" in environment:
        environment["PATH"] = os.pathsep.join(
            component
            for component in environment["PATH"].split(os.pathsep)
            if component
            and Path(component).name != "shims"
            and Path(component).resolve().name != "shims"
        )
    return environment


def _acceptance_host_tools() -> list[dict[str, Any]]:
    specifications = [
        ("cc", os.environ.get("CC", "cc"), True),
        ("cxx", os.environ.get("CXX", "c++"), True),
        ("cmake", "cmake", True),
        ("ctest", "ctest", True),
        ("ninja", "ninja", False),
        ("make", "make", False),
        ("ccache", "ccache", False),
        ("pkg-config", "pkg-config", False),
        ("ld", "ld", False),
        ("ar", "ar", False),
        ("ranlib", "ranlib", False),
        ("bash", "bash", True),
    ]
    for name in (
        "CMAKE_C_COMPILER_LAUNCHER",
        "CMAKE_CXX_COMPILER_LAUNCHER",
        "CMAKE_MAKE_PROGRAM",
    ):
        value = os.environ.get(name)
        if value:
            specifications.append((name.lower(), value, True))
    toolchain_file = os.environ.get("CMAKE_TOOLCHAIN_FILE")
    if toolchain_file:
        specifications.append(
            ("cmake_toolchain_file", toolchain_file, True)
        )
    search_path = _acceptance_parallel_environment().get(
        "PATH", os.defpath
    )
    rows: list[dict[str, Any]] = []
    for name, command, required in specifications:
        try:
            words = shlex.split(command)
        except ValueError as error:
            raise DevCheckError(
                f"checkpoint host tool command is invalid: {name}: {error}"
            ) from error
        if not words:
            raise DevCheckError(
                f"checkpoint host tool command is empty: {name}"
            )
        candidate = Path(words[0])
        if not candidate.is_absolute():
            located = shutil.which(words[0], path=search_path)
            if located is None:
                if required:
                    raise DevCheckError(
                        f"checkpoint host tool is unavailable: {name}"
                    )
                continue
            candidate = Path(located)
        try:
            path = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise DevCheckError(
                f"checkpoint host tool is unavailable: {name}: {error}"
            ) from error
        if not path.is_file():
            raise DevCheckError(
                f"checkpoint host tool is not a file: {name}: {path}"
            )
        digest, size = _hash_file(path)
        rows.append(
            {
                "name": name,
                "command": command,
                "path": str(path),
                "sha256": digest,
                "bytes": size,
            }
        )
    return rows


def _acceptance_west_package(
    manifest_repo: Path, west_argv: list[str]
) -> dict[str, Any] | None:
    launcher = Path(west_argv[0])
    if not launcher.is_absolute():
        located = shutil.which(west_argv[0])
        if located is None:
            return None
        launcher = Path(located)
    interpreter: Path | None = None
    if (
        west_argv[1:3] == ["-m", "west"]
        or (
            len(west_argv) >= 2
            and Path(west_argv[1]).is_absolute()
            and Path(west_argv[1]).is_file()
        )
    ):
        interpreter = launcher
    else:
        try:
            first_line = launcher.read_bytes().splitlines()[0].decode("utf-8")
        except (OSError, IndexError, UnicodeDecodeError):
            return None
        if first_line.startswith("#!"):
            words = first_line[2:].strip().split()
            if words and Path(words[0]).name == "env" and len(words) >= 2:
                located = shutil.which(words[1])
                interpreter = Path(located) if located is not None else None
            elif words:
                interpreter = Path(words[0])
    if interpreter is None:
        return None
    probe = (
        "import importlib.util,json;"
        "s=importlib.util.find_spec('west');"
        "print(json.dumps(None if s is None else "
        "{'origin':s.origin,'roots':list(s.submodule_search_locations or [])}))"
    )
    outcome = _run_process(
        [str(interpreter), "-I", "-c", probe],
        manifest_repo,
        60,
        {},
        sanitize_environment=True,
    )
    if (
        outcome["returncode"] != 0
        or outcome["stdout"]["truncated"]
        or "capture_error" in outcome["stdout"]
        or "capture_error" in outcome["stderr"]
    ):
        raise DevCheckError("cannot resolve installed West package for checkpoint")
    try:
        discovered = json.loads(outcome["stdout_tail"])
    except json.JSONDecodeError as error:
        raise DevCheckError(
            "installed West package identity is invalid"
        ) from error
    if discovered is None:
        return None
    if (
        not isinstance(discovered, dict)
        or set(discovered) != {"origin", "roots"}
        or not isinstance(discovered["roots"], list)
        or len(discovered["roots"]) != 1
    ):
        raise DevCheckError("installed West package identity is invalid")
    package_root = Path(discovered["roots"][0]).resolve(strict=True)
    snapshot = _content_snapshot(package_root)
    return {
        "path": str(package_root),
        "sha256": snapshot["sha256"],
        "file_count": snapshot["file_count"],
        "bytes": snapshot["bytes"],
    }


def _acceptance_west_version(
    manifest_repo: Path, west_argv: list[str]
) -> str:
    outcome = _run_process(
        [*west_argv, "--version"],
        manifest_repo,
        60,
        {},
    )
    version = outcome["stdout_tail"].strip()
    if (
        outcome["returncode"] != 0
        or outcome["stdout"]["truncated"]
        or "capture_error" in outcome["stdout"]
        or "capture_error" in outcome["stderr"]
        or not version
    ):
        raise DevCheckError("cannot resolve West tool version for checkpoint")
    return version


def _acceptance_checkpoint_identity(
    manifest_repo: Path,
    profile: str,
    snapshot: dict[str, Any],
    west_argv: list[str],
    dynamic_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected = _safe_profile_component(profile, "checkpoint profile")
    try:
        phases = patch_stack_lock_first.profile_dependency_chain(
            manifest_repo, selected
        )
    except patch_stack_lock_first.LockFirstError as error:
        raise DevCheckError(f"checkpoint profile graph is invalid: {error}") from error
    graph_rows: list[dict[str, Any]] = []
    patch_rows: list[dict[str, str]] = []
    mapping_rows: list[dict[str, str]] = []
    lock_rows: list[dict[str, str]] = []
    seen_patches: set[str] = set()
    locks_parent = manifest_repo / "locks"
    locks_root = locks_parent / "patch-stack"
    if (
        locks_parent.is_symlink()
        or not locks_parent.is_dir()
        or locks_root.is_symlink()
        or not locks_root.is_dir()
    ):
        raise DevCheckError("checkpoint lock root must be real and contained")
    registry = manifest_repo / "locks" / "patch-stack" / "lock-first-profiles-v1.yml"
    for phase in phases:
        manifest_path, manifest = _load_checkpoint_profile(manifest_repo, phase)
        manifest_digest, _manifest_size = _hash_file(manifest_path)
        base = manifest.get("base-profile")
        graph_rows.append(
            {
                "profile": phase,
                "base_profile": base,
                "manifest": manifest_path.relative_to(manifest_repo).as_posix(),
                "sha256": manifest_digest,
            }
        )
        for index, row in enumerate(manifest["patches"]):
            if not isinstance(row, dict):
                raise DevCheckError(
                    f"checkpoint profile patch {phase}[{index}] is invalid"
                )
            relative_value = row.get("path")
            declared = row.get("sha256sum")
            if (
                not isinstance(relative_value, str)
                or not relative_value
                or not isinstance(declared, str)
                or len(declared) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in declared
                )
            ):
                raise DevCheckError(
                    f"checkpoint profile patch {phase}[{index}] has invalid identity"
                )
            relative = Path(relative_value)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != relative_value
            ):
                raise DevCheckError(
                    f"checkpoint profile patch {phase}[{index}] has unsafe path"
                )
            profile_root = manifest_repo / "patches" / phase
            patch = profile_root / relative
            candidate = profile_root
            for component in relative.parts:
                candidate = candidate / component
                if candidate.is_symlink():
                    raise DevCheckError(
                        f"checkpoint patch path contains a symlink: {patch}"
                    )
            try:
                resolved_patch = patch.resolve(strict=True)
                resolved_root = profile_root.resolve(strict=True)
            except OSError as error:
                raise DevCheckError(
                    f"checkpoint patch is unavailable: {patch}: {error}"
                ) from error
            if (
                resolved_root not in resolved_patch.parents
                or not resolved_patch.is_file()
            ):
                raise DevCheckError(f"checkpoint patch is unavailable: {patch}")
            observed, _patch_size = _hash_file(resolved_patch)
            if observed != declared:
                raise DevCheckError(f"checkpoint patch checksum differs: {patch}")
            logical = patch.relative_to(manifest_repo).as_posix()
            if logical in seen_patches:
                raise DevCheckError(f"checkpoint patch is duplicated: {logical}")
            seen_patches.add(logical)
            patch_rows.append(
                {"profile": phase, "path": logical, "sha256": observed}
            )
        try:
            mapping_path = patch_stack_lock_first.mapping_for_profile(
                phase, registry
            )
            mapping = patch_stack_lock_first.load_mapping(mapping_path, phase)
        except patch_stack_lock_first.LockFirstError as error:
            raise DevCheckError(
                f"checkpoint mapping for {phase} is invalid: {error}"
            ) from error
        mapping_digest, _mapping_size = _hash_file(mapping_path)
        mapping_rows.append(
            {
                "profile": phase,
                "path": mapping_path.relative_to(manifest_repo).as_posix(),
                "sha256": mapping_digest,
            }
        )
        for entry in mapping["series"]:
            lock_relative = Path(entry["lock"])
            if (
                lock_relative.is_absolute()
                or ".." in lock_relative.parts
                or lock_relative.as_posix() != entry["lock"]
            ):
                raise DevCheckError(
                    f"checkpoint lock path is unsafe: {entry['lock']}"
                )
            lock_path = mapping_path.parent / lock_relative
            candidate = mapping_path.parent
            for component in lock_relative.parts:
                candidate = candidate / component
                if candidate.is_symlink():
                    raise DevCheckError(
                        f"checkpoint lock path contains a symlink: {lock_path}"
                    )
            if not lock_path.is_file():
                raise DevCheckError(f"checkpoint lock is unavailable: {lock_path}")
            logical = lock_path.relative_to(manifest_repo).as_posix()
            digest, _lock_size = _hash_file(lock_path)
            previous = next(
                (row for row in lock_rows if row["path"] == logical), None
            )
            if previous is not None:
                if previous["sha256"] != digest:
                    raise DevCheckError(
                        f"checkpoint lock identity is inconsistent: {logical}"
                    )
                continue
            lock_rows.append({"path": logical, "sha256": digest})
    if dynamic_identity is None:
        dynamic = {
            "tool_files": _acceptance_tool_files(west_argv),
            "host_tools": _acceptance_host_tools(),
            "west_version": _acceptance_west_version(
                manifest_repo, west_argv
            ),
            "parallel_environment": _acceptance_parallel_environment(),
            "west_package": _acceptance_west_package(
                manifest_repo, west_argv
            ),
        }
    else:
        dynamic = {
            field: copy.deepcopy(dynamic_identity[field])
            for field in (
                "tool_files",
                "host_tools",
                "west_version",
                "parallel_environment",
                "west_package",
            )
        }
    identity = {
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "tool_version": _CHECKPOINT_TOOL_VERSION,
        "workspace_commit": snapshot["manifest_head"],
        "workspace_tree": snapshot["manifest_tree"],
        "west_argv": list(west_argv),
        "profile": selected,
        "profile_graph": graph_rows,
        "mappings": mapping_rows,
        "patches": patch_rows,
        "locks": lock_rows,
        "tool_files": dynamic["tool_files"],
        "host_tools": dynamic["host_tools"],
        "frozen_manifest_sha256": snapshot["content"]["west.lock.yml"]["sha256"],
        "west_version": dynamic["west_version"],
        "parallel_environment": dynamic["parallel_environment"],
        "west_package": dynamic["west_package"],
    }
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {"key": hashlib.sha256(encoded).hexdigest(), "identity": identity}


def _git_common_directory(manifest_repo: Path) -> Path:
    outcome = _run_process(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        manifest_repo,
        60,
        {},
    )
    if (
        outcome["returncode"] != 0
        or outcome["stdout"]["truncated"]
        or "capture_error" in outcome["stdout"]
        or "capture_error" in outcome["stderr"]
    ):
        raise DevCheckError("cannot resolve Git common directory for checkpoints")
    path = Path(outcome["stdout_tail"].strip())
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise DevCheckError("Git common directory for checkpoints is invalid")
    return path


def _acceptance_checkpoint_plan(
    manifest_repo: Path,
    profile: str,
    snapshot: dict[str, Any],
    west_argv: list[str],
    dynamic_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bound = _acceptance_checkpoint_identity(
        manifest_repo,
        profile,
        snapshot,
        west_argv,
        dynamic_identity,
    )
    root = _git_common_directory(manifest_repo) / "west-dev-checkpoints"
    return {
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "key": bound["key"],
        "path": str(root / f"acceptance-{bound['key']}.json"),
        "identity": bound["identity"],
    }

def _reject_ignored_package_inputs(manifest_repo: Path, profile: str) -> None:
    result = _run_process(
        [
            "git",
            "-C",
            str(manifest_repo),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            "west.yml",
            "west.lock.yml",
            f"patches/{profile}",
            "locks/patch-stack",
        ],
        manifest_repo,
        60,
        {},
        _UNTRACKED_LIST_LIMIT,
    )
    ignored = result.get("_stdout_full")
    if (
        result["returncode"] != 0
        or result["stdout"]["bytes"] > _UNTRACKED_LIST_LIMIT
        or not isinstance(ignored, bytes)
    ):
        raise DevCheckError("cannot inspect ignored package inputs")
    if ignored:
        paths = [
            os.fsdecode(value)
            for value in ignored.split(b"\0")
            if value
        ]
        detail = ", ".join(paths[:8])
        if len(paths) > 8:
            detail += f", ... ({len(paths) - 8} more)"
        raise DevCheckError(
            f"acceptance package inputs contain ignored files: {detail}"
        )




def _check_steps(inputs: dict[str, Any], transaction_id: str) -> list[dict[str, Any]]:
    west = list(inputs["west_argv"])
    profile = inputs["profile"]
    bead = inputs["bead"]
    patch = inputs["patch"]
    tier = inputs["tier"]
    manifest_repo = Path(inputs["manifest_repo"])
    selectors = _test_selectors(profile, bead, patch)
    export_argv = [*west, "patch", "export", "--profile", profile]
    if patch is not None:
        export_argv.extend(("--patch", patch))
    export_argv.append("--check")
    steps = [
        _step(
            "patch-check",
            [
                *west,
                "patch",
                "check",
                "--profile",
                profile,
                "--strict",
                "--strict-quality",
            ],
            "read-only",
            _CHECK_TIMEOUTS["patch-check"],
            manifest_repo,
        ),
        _step(
            "patch-export-check",
            export_argv,
            "read-only",
            _CHECK_TIMEOUTS["patch-export-check"],
            manifest_repo,
        ),
        _step(
            "test-list",
            [*west, "test", *selectors, "--list"],
            "read-only",
            _CHECK_TIMEOUTS["test-list"],
            manifest_repo,
        ),
    ]
    if tier == "quick":
        return steps
    if tier == "canonical":
        steps.extend(
            (
                _step(
                    "patch-verify",
                    [*west, "patch", "verify", "--profile", profile],
                    "temporary/local-output",
                    _CHECK_TIMEOUTS["patch-verify"],
                    manifest_repo,
                ),
                _step(
                    "host-materialized-test",
                    [
                        *west,
                        "test",
                        *selectors,
                        "--env",
                        "host",
                        "--materialize-profile",
                    ],
                    "temporary/local-output",
                    _CHECK_TIMEOUTS["host-materialized-test"],
                    manifest_repo,
                ),
            )
        )
        return steps

    scratch = Path(inputs["evidence"]).parent / f".dev-check-{transaction_id}"
    control_parent = scratch / "control"
    control = control_parent / "darling-workspace"
    candidate_parent = scratch / "lock-first"
    candidate = candidate_parent / "darling-workspace"
    materialized_root = (
        Path(inputs["acceptance_checkpoint"]["path"]).parent
        / "materialized-v1"
        / inputs["acceptance_checkpoint"]["key"]
    )
    guest_parent = materialized_root / "guest"
    guest = guest_parent / "darling-workspace"
    artifacts = scratch / "evidence"

    def stage_env(name: str) -> dict[str, str]:
        environment = dict(
            inputs["acceptance_checkpoint"]["identity"][
                "parallel_environment"
            ]
        )
        environment.update(
            {
                "CCACHE_DIR": str(scratch / "cache" / name / "ccache"),
                "GIT_CONFIG_COUNT": "0",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": str(scratch / "home" / name),
                "TMPDIR": "/tmp",
                "XDG_CACHE_HOME": str(scratch / "cache" / name),
                "XDG_CONFIG_HOME": str(scratch / "config" / name),
            }
        )
        return environment

    host_env = stage_env("host")
    oracle_env = stage_env("oracle")
    candidate_env = stage_env("candidate")
    guest_prepare_env = dict(candidate_env)
    guest_workspace_lock = materialized_root / ".guest.lock"
    guest_prepare_env["WEST_MATERIALIZED_WORKSPACE_LOCK"] = str(
        guest_workspace_lock
    )
    guest_env = stage_env("guest")
    guest_env.update(RuntimeBuildService.derive_ccache_environment(guest_env))
    guest_env["CCACHE_DIR"] = str(
        Path(inputs["acceptance_checkpoint"]["path"]).parent
        / "runtime-ccache-v1"
    )
    guest_env["CCACHE_MAXSIZE"] = "4G"
    guest_env["DARLING_TIER_DEFER_GLOBAL_CLEANUP"] = "1"
    guest_env["WEST_RUNTIME_BUILD_CACHE_DIR"] = str(
        Path(inputs["acceptance_checkpoint"]["path"]).parent
        / "runtime-build-v1"
        / inputs["acceptance_checkpoint"]["key"]
    )
    guest_env["WEST_RUNTIME_BUILD_CACHE_KEY"] = inputs["acceptance_checkpoint"][
        "key"
    ]
    guest_env["WEST_MATERIALIZED_WORKSPACE_LOCK"] = str(guest_workspace_lock)
    guest_env["WEST_PREMATERIALIZED_RUNTIME_SOURCE_ROOT"] = str(
        guest_parent / "darling"
    )
    guest_env["DARLING_SMOKE_PREFIX"] = (
        f"/tmp/darling-rootless-smoke-{inputs['acceptance_checkpoint']['key'][:16]}"
    )
    final_host_env = stage_env("final-host")
    bootstrap_env = dict(candidate_env)
    bootstrap_env.update(
        {
            "DARLING_WEST_UPDATE_JOBS": str(_ACCEPTANCE_BOOTSTRAP_JOBS),
            "DARLING_WEST_UPDATE_PATH_CACHE": str(manifest_repo.parent),
        }
    )
    head = inputs["package_snapshot"]["manifest_head"]
    mapping = control / "locks" / "patch-stack" / "lock-first-series-v2.yml"
    oracle = artifacts / "immutable-oracle.json"
    modules = artifacts / "lock-first-modules.json"
    manifest = artifacts / "lock-first-manifest.json"
    candidate_cache = (
        Path(inputs["acceptance_checkpoint"]["path"]).parent
        / f"candidate-{inputs['acceptance_checkpoint']['key']}"
    )
    lock_evidence = artifacts / "lock-first-evidence.json"
    comparison = artifacts / "acceptance-result.json"
    steps.extend(
        (
            _step(
                "doctor",
                [
                    *west,
                    "darling-doctor",
                    "--prefix",
                    inputs["prefix"],
                    "--build-dir",
                    inputs["build_dir"],
                    "--full",
                ],
                "read-only",
                _CHECK_TIMEOUTS["doctor"],
                manifest_repo,
            ),
            _step(
                "acceptance-clone-control",
                [
                    "git",
                    "clone",
                    "--no-local",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(manifest_repo),
                    str(control),
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["clone"],
                control_parent,
                oracle_env,
            ),
            _step(
                "acceptance-clone-candidate",
                [
                    "git",
                    "clone",
                    "--no-local",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(manifest_repo),
                    str(candidate),
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["clone"],
                candidate_parent,
                candidate_env,
            ),
            _step(
                "acceptance-checkout-control",
                ["git", "-C", str(control), "checkout", "--detach", head],
                "temporary/local-output",
                _CHECK_TIMEOUTS["checkout"],
                control,
                oracle_env,
            ),
            _step(
                "acceptance-checkout-candidate",
                ["git", "-C", str(candidate), "checkout", "--detach", head],
                "temporary/local-output",
                _CHECK_TIMEOUTS["checkout"],
                candidate,
                candidate_env,
            ),
            _step(
                "acceptance-bootstrap-candidate",
                [str(candidate / "ci" / "bootstrap-west.sh")],
                "temporary/local-output",
                _CHECK_TIMEOUTS["bootstrap"],
                candidate,
                bootstrap_env,
            ),
            _step(
                "acceptance-configure-candidate-identity",
                [
                    *west,
                    "forall",
                    "-c",
                    "git config user.name 'West Dev Acceptance' && "
                    "git config user.email 'west-dev-acceptance@example.invalid'",
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["identity"],
                candidate,
                candidate_env,
            ),
            _step(
                "acceptance-seed-candidate-refs",
                [
                    str(Path(sys.executable).resolve()),
                    str(
                        candidate
                        / "ci"
                        / "patch_stack_lock_first_acceptance.py"
                    ),
                    "seed-source-refs",
                    "--source-workspace",
                    str(manifest_repo.parent),
                    "--candidate-workspace",
                    str(candidate_parent),
                    "--profile",
                    profile,
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["seed-source-refs"],
                candidate,
                candidate_env,
            ),
            _step(
                "patch-verify",
                [*west, "patch", "verify", "--profile", profile],
                "temporary/local-output",
                _CHECK_TIMEOUTS["patch-verify"],
                candidate,
                candidate_env,
            ),
            _step(
                "host-materialized-test",
                [
                    *west,
                    "test",
                    *selectors,
                    "--env",
                    "host",
                    "--materialize-profile",
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["host-materialized-test"],
                manifest_repo,
                host_env,
            ),
            _step(
                "immutable-oracle",
                [
                    str(Path(sys.executable).resolve()),
                    str(control / "tests" / "patch_stack_immutable_oracle.py"),
                    "--workspace",
                    str(control),
                    "--profile",
                    profile,
                    "--mapping",
                    str(mapping),
                    "--output",
                    str(oracle),
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["immutable-oracle"],
                control,
                oracle_env,
            ),
            _step(
                "acceptance-candidate-apply",
                [
                    str(Path(sys.executable).resolve()),
                    str(
                        candidate
                        / "ci"
                        / "patch_stack_lock_first_acceptance.py"
                    ),
                    "materialize-candidate",
                    "--manifest-workspace",
                    str(candidate),
                    "--candidate-workspace",
                    str(candidate_parent),
                    "--profile",
                    profile,
                    "--cache",
                    str(candidate_cache),
                    "--key",
                    inputs["acceptance_checkpoint"]["key"],
                    "--lock-evidence",
                    str(lock_evidence),
                    "--modules",
                    str(modules),
                    "--candidate-manifest",
                    str(manifest),
                    "--west-command",
                    *west,
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["candidate-apply"],
                candidate,
                candidate_env,
            ),
            _step(
                "acceptance-capture",
                [
                    str(Path(sys.executable).resolve()),
                    str(candidate / "ci" / "patch_stack_acceptance.py"),
                    "capture",
                    "--workspace",
                    str(candidate),
                    "--profile",
                    profile,
                    "--modules",
                    str(modules),
                    "--manifest",
                    str(manifest),
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-capture"],
                candidate,
                candidate_env,
            ),
            _step(
                "acceptance-compare",
                [
                    str(Path(sys.executable).resolve()),
                    str(
                        candidate
                        / "ci"
                        / "patch_stack_lock_first_acceptance.py"
                    ),
                    "compare-immutable-oracle",
                    "--oracle",
                    str(oracle),
                    "--candidate",
                    str(modules),
                    "--candidate-manifest",
                    str(manifest),
                    "--evidence",
                    str(lock_evidence),
                    "--mapping",
                    str(
                        candidate
                        / "locks"
                        / "patch-stack"
                        / "lock-first-series-v2.yml"
                    ),
                    "--candidate-workspace",
                    str(candidate_parent),
                    "--manifest-workspace",
                    str(candidate),
                    "--transaction-root",
                    str(scratch),
                    "--result",
                    str(comparison),
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-compare"],
                candidate,
                candidate_env,
            ),
            _step(
                "acceptance-publish-candidate-cache",
                [
                    str(Path(sys.executable).resolve()),
                    str(
                        candidate
                        / "ci"
                        / "patch_stack_lock_first_acceptance.py"
                    ),
                    "publish-candidate-cache",
                    "--manifest-workspace",
                    str(candidate),
                    "--candidate-workspace",
                    str(candidate_parent),
                    "--profile",
                    profile,
                    "--cache",
                    str(candidate_cache),
                    "--key",
                    inputs["acceptance_checkpoint"]["key"],
                    "--lock-evidence",
                    str(lock_evidence),
                    "--modules",
                    str(modules),
                    "--candidate-manifest",
                    str(manifest),
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["candidate-cache-publish"],
                candidate,
                candidate_env,
            ),
            _step(
                "acceptance-clone-guest-candidate",
                [
                    str(Path(sys.executable).resolve()),
                    str(
                        candidate
                        / "ci"
                        / "patch_stack_lock_first_acceptance.py"
                    ),
                    "clone-tier-workspace",
                    "--source-workspace",
                    str(candidate_parent),
                    "--destination-workspace",
                    str(guest_parent),
                    "--profile",
                    profile,
                    "--candidate-manifest",
                    str(manifest),
                ],
                "temporary/local-output",
                _CHECK_TIMEOUTS["clone-tier-workspace"],
                candidate,
                guest_prepare_env,
            ),
            _step(
                "acceptance-host-tier",
                [str(candidate / "ci" / "run-test-tier.sh"), "host"],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-host-tier"],
                candidate,
                final_host_env,
            ),
            _step(
                "acceptance-guest-smoke",
                [str(guest / "ci" / "run-test-tier.sh"), "guest-smoke"],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-guest-smoke"],
                guest,
                guest_env,
            ),
            _step(
                "acceptance-final-cleanup",
                [str(candidate / "ci" / "run-test-tier.sh"), "acceptance-cleanup"],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-final-cleanup"],
                candidate,
                candidate_env,
            ),
        )
    )
    return steps


def build_check_plan(
    manifest_repo: Path,
    west_argv: list[str],
    tier: str,
    profile: str,
    bead: str | None,
    patch: str | None,
    evidence: Path,
    prefix: Path | None,
    build_dir: Path | None,
) -> dict[str, Any]:
    """Build a frozen, serializable check plan without running or writing anything."""
    selected_tier = _tier(tier)
    selected_profile = _validate_text(profile, "profile")
    selected_bead = None if bead is None else _validate_text(bead, "bead")
    selected_patch = None if patch is None else _validate_text(patch, "patch")
    repo = _absolute(manifest_repo, "manifest_repo")
    evidence_path = _absolute(evidence, "evidence")
    if not evidence_path.parent.is_dir() or evidence_path.parent.is_symlink():
        raise DevCheckError("check evidence parent must be a real existing directory")
    if not repo.is_dir():
        raise DevCheckError(f"manifest_repo is not a directory: {repo}")
    if _is_within(evidence_path, repo):
        raise DevCheckError("check evidence must be outside the active manifest repository")
    if _path_exists(evidence_path):
        raise DevCheckError(f"check evidence already exists: {evidence_path}")
    transaction_id = uuid.uuid4().hex
    inputs = {
        "manifest_repo": str(repo),
        "west_argv": _validate_west_argv(west_argv),
        "tier": selected_tier,
        "profile": selected_profile,
        "bead": selected_bead,
        "patch": selected_patch,
        "evidence": str(evidence_path),
        "evidence_parent_identity": _path_identity(evidence_path.parent),
        "prefix": str(_absolute(prefix, "prefix")) if prefix is not None else None,
        "build_dir": (
            str(_absolute(build_dir, "build_dir"))
            if build_dir is not None
            else None
        ),
        "acceptance_checkpoint": None,
    }
    inputs["package_snapshot"] = _collect_package_snapshot(repo, selected_profile)
    if selected_tier == "acceptance":
        _validate_acceptance_selection(inputs)
        _reject_ignored_package_inputs(repo, selected_profile)
        if inputs["package_snapshot"]["dirty"]:
            raise DevCheckError("acceptance checks require a clean manifest repository")
        inputs["acceptance_checkpoint"] = _acceptance_checkpoint_plan(
            repo,
            selected_profile,
            inputs["package_snapshot"],
            inputs["west_argv"],
        )
    _validate_acceptance_inputs(inputs)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "check",
        "transaction_id": transaction_id,
        "state": "planned",
        "returncode": None,
        "created_at": _utc_now(),
        "inputs": inputs,
        "steps": _check_steps(inputs, transaction_id),
        "results": [],
        "next_safe_action": "Execute this frozen check plan.",
    }


def _terminate_process_group(
    process: subprocess.Popen[bytes], first_signal: signal.Signals
) -> None:
    try:
        os.killpg(process.pid, first_signal)
    except ProcessLookupError:
        if process.poll() is None:
            process.wait()
        return
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)


class _ProcessCancellation:
    def __init__(self) -> None:
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()

    def register(self, process: subprocess.Popen[bytes]) -> bool:
        with self._lock:
            if self.event.is_set():
                return False
            self._processes.add(process)
            return True

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self, first_signal: signal.Signals) -> None:
        self.event.set()
        with self._lock:
            processes = tuple(self._processes)
        for process in processes:
            _terminate_process_group(process, first_signal)


def _run_process(
    argv: list[str],
    cwd: Path,
    timeout_seconds: int,
    env_overrides: dict[str, str],
    stdout_full_limit: int = 0,
    cancellation: _ProcessCancellation | None = None,
    sanitize_environment: bool = False,
) -> dict[str, Any]:
    """Run one process with bounded capture and an independently killable group."""
    started_at = _utc_now()
    started = time.monotonic()
    stdout_capture = _BoundedCapture(full_limit=stdout_full_limit)
    stderr_capture = _BoundedCapture()
    if sanitize_environment:
        process_env: dict[str, str] = {}
    else:
        process_env = os.environ.copy()
    process_env.update(env_overrides)
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=process_env,
        )
    except OSError as error:
        encoded = f"{type(error).__name__}: {error}".encode()
        stderr_capture._digest.update(encoded)
        stderr_capture._tail.extend(encoded[-_CAPTURE_LIMIT:])
        stderr_capture._count = len(encoded)
        finished_at = _utc_now()
        duration_ns = round((time.monotonic() - started) * 1_000_000_000)
        stdout_result = stdout_capture.result()
        stderr_result = stderr_capture.result()
        result = {
            "returncode": 127,
            "timed_out": False,
            "interrupted": False,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": round(duration_ns / 1_000_000),
            "duration_ns": duration_ns,
            "stdout": stdout_result,
            "stderr": stderr_result,
            "stdout_tail": stdout_result["tail"],
            "stderr_tail": stderr_result["tail"],
            "launch_error": f"{type(error).__name__}: {error}",
        }
        if stdout_full_limit:
            result["_stdout_full"] = stdout_capture.full()
        return result

    registered = cancellation is None or cancellation.register(process)
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=stdout_capture.consume, args=(process.stdout,), daemon=True
    )
    stderr_thread = threading.Thread(
        target=stderr_capture.consume, args=(process.stderr,), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    interrupted = False
    cancelled_by_peer = False
    process_group_quiescent = True
    returncode: int
    try:
        try:
            if not registered:
                cancelled_by_peer = True
                _terminate_process_group(process, signal.SIGINT)
                returncode = 125
            elif cancellation is None:
                returncode = process.wait(timeout=timeout_seconds)
            else:
                deadline = time.monotonic() + timeout_seconds
                while True:
                    if cancellation.event.is_set():
                        cancelled_by_peer = True
                        _terminate_process_group(process, signal.SIGINT)
                        returncode = 125
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout_seconds)
                    try:
                        returncode = process.wait(timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process, signal.SIGTERM)
            returncode = 124
        except KeyboardInterrupt:
            interrupted = True
            if cancellation is not None:
                cancellation.cancel(signal.SIGINT)
            else:
                _terminate_process_group(process, signal.SIGINT)
            returncode = 130
        if (
            cancellation is not None
            and cancellation.event.is_set()
            and returncode != 0
            and not timed_out
            and not interrupted
        ):
            cancelled_by_peer = True
            returncode = 125
        if not timed_out and not interrupted and not cancelled_by_peer:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                process_group_quiescent = False
                _terminate_process_group(process, signal.SIGTERM)
                if returncode == 0:
                    returncode = 125
    except BaseException:
        _terminate_process_group(process, signal.SIGTERM)
        raise
    finally:
        if cancellation is not None and registered:
            cancellation.unregister(process)
        stdout_thread.join(timeout=_TERMINATE_GRACE_SECONDS)
        stderr_thread.join(timeout=_TERMINATE_GRACE_SECONDS)

    finished_at = _utc_now()
    duration_ns = round((time.monotonic() - started) * 1_000_000_000)
    stdout_result = stdout_capture.result()
    stderr_result = stderr_capture.result()
    result = {
        "returncode": returncode,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": round(duration_ns / 1_000_000),
        "duration_ns": duration_ns,
        "stdout": stdout_result,
        "stderr": stderr_result,
        "stdout_tail": stdout_result["tail"],
        "stderr_tail": stderr_result["tail"],
        "process_group_quiescent": process_group_quiescent,
    }
    if cancelled_by_peer:
        result["cancelled_by_peer"] = True
    if stdout_full_limit:
        result["_stdout_full"] = stdout_capture.full()
    return result


def _identity_from_stat(metadata: os.stat_result) -> dict[str, int]:
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _identity_at(directory_fd: int, name: str) -> dict[str, int]:
    return _identity_from_stat(
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    )

def _identity_at_or_none(
    directory_fd: int, name: str
) -> dict[str, int] | None:
    try:
        return _identity_at(directory_fd, name)
    except FileNotFoundError:
        return None


def _directory_identity(directory_fd: int) -> dict[str, int]:
    return _identity_from_stat(os.fstat(directory_fd))

def _exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _open_pinned_directory(
    path: Path, expected_identity: dict[str, int]
) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    if _directory_identity(descriptor) != expected_identity:
        os.close(descriptor)
        raise DevCheckError(f"directory identity changed while opening: {path}")
    return descriptor


def _json_bytes(payload: dict[str, Any]) -> bytes:
    public_payload = {
        key: value for key, value in payload.items() if not key.startswith("_")
    }
    return (json.dumps(public_payload, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _write_json_temp(directory_fd: int, name: str, data: bytes) -> int:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
        dir_fd=directory_fd,
    )
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while creating JSON evidence")
            remaining = remaining[written:]
        os.fsync(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise

def _rename_exchange(directory_fd: int, left: str, right: str) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise DevCheckError("atomic evidence exchange is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(left),
        directory_fd,
        os.fsencode(right),
        2,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise DevCheckError(
            f"atomic evidence exchange failed: {os.strerror(error_number)}"
        )


def _atomic_json(
    path: Path,
    payload: dict[str, Any],
    directory_fd: int,
    expected_identity: dict[str, int],
) -> dict[str, int]:
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    if not _same_path_identity(path.parent, _directory_identity(directory_fd)):
        raise DevCheckError(f"evidence parent changed before update: {path.parent}")
    exchanged = False
    committed = False
    new_identity: dict[str, int] | None = None
    new_descriptor: int | None = None
    expected_descriptor: int | None = None
    try:
        new_descriptor = _write_json_temp(
            directory_fd, temporary_name, _json_bytes(payload)
        )
        new_identity = _identity_from_stat(os.fstat(new_descriptor))
        if not _same_path_identity(
            path.parent, _directory_identity(directory_fd)
        ):
            raise DevCheckError(
                f"evidence parent changed during update: {path.parent}"
            )
        expected_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        if _identity_from_stat(os.fstat(expected_descriptor)) != expected_identity:
            raise DevCheckError(
                f"evidence identity changed before update: {path}"
            )
        if _identity_at_or_none(directory_fd, temporary_name) != new_identity:
            raise DevCheckError(
                f"evidence temporary identity changed before update: {path}"
            )
        _rename_exchange(directory_fd, temporary_name, path.name)
        exchanged = True
        previous_matches = (
            _identity_at_or_none(directory_fd, temporary_name)
            == expected_identity
        )
        destination_matches = (
            _identity_at_or_none(directory_fd, path.name) == new_identity
        )
        if not previous_matches or not destination_matches:
            if destination_matches:
                _rename_exchange(directory_fd, temporary_name, path.name)
                exchanged = False
            raise DevCheckError(
                f"evidence identity changed during update: {path}"
            )
        if not _same_path_identity(
            path.parent, _directory_identity(directory_fd)
        ):
            _rename_exchange(directory_fd, temporary_name, path.name)
            exchanged = False
            raise DevCheckError(
                f"evidence parent changed during update: {path.parent}"
            )
        os.fsync(directory_fd)
        previous_matches = (
            _identity_at_or_none(directory_fd, temporary_name)
            == expected_identity
        )
        destination_matches = (
            _identity_at_or_none(directory_fd, path.name) == new_identity
        )
        parent_matches = _same_path_identity(
            path.parent, _directory_identity(directory_fd)
        )
        if not previous_matches or not destination_matches or not parent_matches:
            if previous_matches and destination_matches:
                _rename_exchange(directory_fd, temporary_name, path.name)
                exchanged = False
                os.fsync(directory_fd)
            raise DevCheckError(
                f"evidence destination changed before durable update: {path}"
            )
        committed = True
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
            exchanged = False
            os.fsync(directory_fd)
        except OSError:
            # The exchange itself is already durable; stale owned temp is harmless.
            pass
        return new_identity
    finally:
        if exchanged and not committed and new_identity is not None:
            try:
                if _identity_at(directory_fd, path.name) == new_identity:
                    _rename_exchange(directory_fd, temporary_name, path.name)
                    exchanged = False
            except (FileNotFoundError, DevCheckError):
                pass
        try:
            temporary_identity = _identity_at(directory_fd, temporary_name)
            if temporary_identity in (new_identity, expected_identity):
                os.unlink(temporary_name, dir_fd=directory_fd)
        except OSError:
            pass
        if expected_descriptor is not None:
            os.close(expected_descriptor)
        if new_descriptor is not None:
            os.close(new_descriptor)


def _create_json(
    path: Path, payload: dict[str, Any], directory_fd: int
) -> dict[str, int]:
    """Atomically create initial evidence without replacing any existing path."""
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    if not _same_path_identity(path.parent, _directory_identity(directory_fd)):
        raise DevCheckError(
            f"evidence parent changed before initial publication: {path.parent}"
        )
    created_identity: dict[str, int] | None = None
    temporary_descriptor: int | None = None
    temporary_identity: dict[str, int] | None = None
    committed = False
    try:
        temporary_descriptor = _write_json_temp(
            directory_fd, temporary_name, _json_bytes(payload)
        )
        temporary_identity = _identity_from_stat(os.fstat(temporary_descriptor))
        if not _same_path_identity(
            path.parent, _directory_identity(directory_fd)
        ):
            raise DevCheckError(
                f"evidence parent changed during initial publication: {path.parent}"
            )
        if (
            _identity_at_or_none(directory_fd, temporary_name)
            != temporary_identity
        ):
            raise DevCheckError(
                f"evidence temporary identity changed before publication: {path}"
            )
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise DevCheckError(
                f"evidence appeared before initial publication: {path}"
            ) from error
        created_identity = _identity_at(directory_fd, path.name)
        if created_identity != temporary_identity:
            raise DevCheckError(
                f"evidence temporary identity changed during publication: {path}"
            )
        if not _same_path_identity(
            path.parent, _directory_identity(directory_fd)
        ):
            raise DevCheckError(
                f"evidence parent changed during initial publication: {path.parent}"
            )
        os.fsync(directory_fd)
        if not _same_path_identity(
            path.parent, _directory_identity(directory_fd)
        ):
            raise DevCheckError(
                f"evidence parent changed before durable publication: {path.parent}"
            )
        committed = True
        return created_identity
    finally:
        if (
            created_identity is not None
            and created_identity == temporary_identity
            and not committed
        ):
            try:
                if _identity_at(directory_fd, path.name) == created_identity:
                    os.unlink(path.name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)


def _validate_plan_envelope(plan: dict[str, Any], operation: str) -> None:
    if not isinstance(plan, dict):
        raise DevCheckError("plan must be an object")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise DevCheckError("unsupported plan schema_version")
    if plan.get("operation") != operation or plan.get("state") != "planned":
        raise DevCheckError(f"expected a planned {operation} operation")
    if "returncode" not in plan or plan["returncode"] is not None:
        raise DevCheckError("planned operation returncode must be null")
    _validate_text(plan.get("transaction_id"), "transaction_id")
    if not isinstance(plan.get("inputs"), dict):
        raise DevCheckError("plan inputs must be an object")
    if not isinstance(plan.get("steps"), list) or not plan["steps"]:
        raise DevCheckError("plan steps must be a non-empty list")


def _validate_check_plan(plan: dict[str, Any]) -> tuple[Path, Path]:
    _validate_plan_envelope(plan, "check")
    inputs = plan["inputs"]
    repo = Path(_validate_text(inputs.get("manifest_repo"), "manifest_repo"))
    evidence = Path(_validate_text(inputs.get("evidence"), "evidence"))
    if not repo.is_absolute() or not evidence.is_absolute():
        raise DevCheckError("plan paths must be absolute")
    if _is_within(evidence, repo):
        raise DevCheckError(
            "check evidence must be outside the active manifest repository"
        )
    if not repo.is_dir():
        raise DevCheckError(f"manifest_repo is not a directory: {repo}")
    if _path_identity(evidence.parent) != inputs.get("evidence_parent_identity"):
        raise DevCheckError("check evidence parent identity changed after planning")
    if _path_exists(evidence):
        raise DevCheckError(f"check evidence already exists: {evidence}")
    _tier(inputs.get("tier"))
    _validate_text(inputs.get("profile"), "profile")
    _validate_west_argv(inputs.get("west_argv"))
    for name in ("bead", "patch", "prefix", "build_dir"):
        value = inputs.get(name)
        if value is not None:
            _validate_text(value, name)
    _validate_acceptance_inputs(inputs)
    snapshot = inputs.get("package_snapshot")
    if not isinstance(snapshot, dict):
        raise DevCheckError("check plan package snapshot is missing")
    if _collect_package_snapshot(repo, inputs["profile"]) != snapshot:
        raise DevCheckError("manifest repository changed after check planning")
    if inputs["tier"] == "acceptance" and snapshot.get("dirty") is not False:
        raise DevCheckError("acceptance checks require a clean manifest repository")
    if (
        inputs["tier"] == "acceptance"
        and _acceptance_checkpoint_plan(
            repo, inputs["profile"], snapshot, inputs["west_argv"]
        )
        != inputs["acceptance_checkpoint"]
    ):
        raise DevCheckError("acceptance checkpoint identity changed after planning")
    expected = _check_steps(inputs, plan["transaction_id"])
    if plan["steps"] != expected:
        raise DevCheckError("check steps do not match the frozen inputs")
    return repo, evidence


def _active_record(plan: dict[str, Any]) -> dict[str, Any]:
    record = copy.deepcopy(plan)
    record["state"] = "planned"
    record["results"] = []
    record["returncode"] = None
    record["next_safe_action"] = "Resume only by executing this complete plan."
    return record


def _finish_record(
    record: dict[str, Any], state: str, returncode: int, next_safe_action: str
) -> dict[str, Any]:
    record["state"] = state
    record["returncode"] = returncode
    record["finished_at"] = _utc_now()
    started = record.pop("_started_monotonic", None)
    if isinstance(started, (int, float)):
        record["duration_ms"] = round((time.monotonic() - started) * 1000)
    record["next_safe_action"] = next_safe_action
    return record


def _scratch_for_check(plan: dict[str, Any]) -> Path:
    return (
        Path(plan["inputs"]["evidence"]).parent
        / f".dev-check-{plan['transaction_id']}"
    )


def _path_identity(path: Path) -> dict[str, int]:
    metadata = path.lstat()
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _same_path_identity(path: Path, identity: dict[str, int]) -> bool:
    try:
        return _path_identity(path) == identity
    except OSError:
        return False



def _check_scratch_marker(scratch: Path) -> Path:
    return scratch / ".west-dev-check-owner"


def _create_check_scratch(plan: dict[str, Any]) -> tuple[Path, dict[str, int]]:
    scratch = _scratch_for_check(plan)
    if _path_exists(scratch):
        raise DevCheckError(f"check transaction scratch already exists: {scratch}")
    identity: dict[str, int] | None = None
    try:
        scratch.mkdir(mode=0o700)
        identity = _path_identity(scratch)
        marker = _check_scratch_marker(scratch)
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(plan["transaction_id"] + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        for name in (
            "control",
            "lock-first",
            "evidence",
            "home",
            "cache",
            "config",
        ):
            (scratch / name).mkdir(mode=0o700)
        for parent in ("home", "cache", "config"):
            for stage in ("host", "oracle", "candidate"):
                (scratch / parent / stage).mkdir(mode=0o700)
        return scratch, identity
    except BaseException:
        if identity is not None and _same_path_identity(scratch, identity):
            shutil.rmtree(scratch)
        raise


def _cleanup_check_scratch(
    scratch: Path, identity: dict[str, int], transaction_id: str
) -> None:
    if not _same_path_identity(scratch, identity):
        raise DevCheckError("check scratch identity changed; refusing cleanup")
    marker = _check_scratch_marker(scratch)
    try:
        metadata = marker.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise DevCheckError("check scratch ownership marker is not a regular file")
        owner = marker.read_text().strip()
    except OSError as error:
        raise DevCheckError(f"cannot verify check scratch ownership: {error}") from error
    if owner != transaction_id:
        raise DevCheckError("check scratch ownership marker changed; refusing cleanup")
    shutil.rmtree(scratch)


def _listed_test_count(stdout_tail: str) -> int:
    return sum(
        1
        for line in stdout_tail.splitlines()
        if not line[:1].isspace()
        and ": " in line
        and "["
        in line
        and "env:" in line
        and "diag:" in line
        and "kind:" in line
    )


def _acceptance_artifact_receipt(scratch: Path) -> dict[str, Any]:
    paths = {
        "immutable_oracle": scratch / "evidence" / "immutable-oracle.json",
        "lock_first_evidence": scratch / "evidence" / "lock-first-evidence.json",
        "comparison": scratch / "evidence" / "acceptance-result.json",
        "module_map": scratch / "evidence" / "lock-first-modules.json",
        "candidate_manifest": scratch / "evidence" / "lock-first-manifest.json",
    }
    result: dict[str, Any] = {}
    for name, path in paths.items():
        value, data = _read_json_file(path, f"acceptance artifact {name}")
        if name == "immutable_oracle":
            valid_schema = (
                value.get("oracle_schema_version") == 2
                and value.get("mode") == "immutable-cherry-pick-oracle"
                and value.get("profile") == "homebrew"
                and value.get("verdict") == "VALID"
            )
        elif name in {"lock_first_evidence", "comparison"}:
            valid_schema = (
                value.get("evidence_schema_version") == 2
                and value.get("verdict") == "VALID"
            )
        elif name == "module_map":
            valid_schema = (
                value.get("profile") == "homebrew"
                and isinstance(value.get("modules"), list)
            )
        else:
            valid_schema = (
                isinstance(value.get("workspace_commit"), str)
                and isinstance(value.get("frozen_manifest_sha256"), str)
                and isinstance(value.get("generated_profile_locks"), list)
            )
        if not valid_schema:
            raise DevCheckError(f"acceptance artifact {name} schema is invalid")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DevCheckError(
                f"acceptance artifact {name} is not UTF-8 JSON"
            ) from error
        result[name] = {
            "source_path": str(path),
            "content": content,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    manifest_value = json.loads(result["candidate_manifest"]["content"])
    generated_rows = manifest_value.get("generated_profile_locks")
    if not isinstance(generated_rows, list) or not generated_rows:
        raise DevCheckError("acceptance generated lock closure is empty")
    generated_locks = []
    seen_generated: set[str] = set()
    candidate_manifest = scratch / "lock-first" / "darling-workspace"
    candidate_top = candidate_manifest.parent
    for row in generated_rows:
        if not isinstance(row, dict) or set(row) != {
            "profile",
            "path",
            "size",
            "sha256",
            "semantic_sha256",
        }:
            raise DevCheckError("acceptance generated lock row is invalid")
        relative = _safe_package_relative(row.get("path"), "generated lock path")
        parts = Path(relative).parts
        if (
            relative in seen_generated
            or len(parts) < 3
            or parts[0] != "patches"
            or parts[1] != row.get("profile")
            or Path(relative).name != "west.lock.yml"
            or not isinstance(row.get("size"), int)
            or isinstance(row.get("size"), bool)
            or row["size"] <= 0
            or not _is_lower_hex(row.get("sha256"), 64)
            or not _is_lower_hex(row.get("semantic_sha256"), 64)
        ):
            raise DevCheckError("acceptance generated lock row is invalid")
        seen_generated.add(relative)
        path = candidate_manifest / relative
        data = path.read_bytes()
        if (
            len(data) > _GENERATED_LOCK_LIMIT
            or row.get("size") != len(data)
            or row.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise DevCheckError(f"acceptance generated lock differs: {relative}")
        generated_locks.append(
            {
                "profile": row["profile"],
                "path": relative,
                "size": len(data),
                "sha256": row["sha256"],
                "semantic_sha256": row["semantic_sha256"],
                "content": data.decode("utf-8"),
            }
        )
    lock_value = json.loads(result["lock_first_evidence"]["content"])
    module_value = json.loads(result["module_map"]["content"])
    module_rows = {
        row["module"]: row
        for row in module_value.get("modules", [])
        if isinstance(row, dict) and isinstance(row.get("module"), str)
    }
    previous: dict[str, str] = {}
    for row in lock_value.get("series", []):
        if not isinstance(row, dict):
            raise DevCheckError("acceptance lock-first evidence row is invalid")
        module = row.get("module")
        mapped = module_rows.get(module)
        if mapped is None:
            raise DevCheckError("acceptance applied commit module is missing")
        commit = _require_oid(
            row.get("applied_commit"), f"acceptance {module} applied commit"
        )
        tree = _require_oid(
            row.get("applied_tree"), f"acceptance {module} applied tree"
        )
        relative_repo = _safe_package_relative(
            mapped.get("path"), f"acceptance {module} repository"
        )
        repository = candidate_top / relative_repo
        if repository.is_symlink() or not repository.is_dir():
            raise DevCheckError(f"acceptance {module} repository is unavailable")
        if _run_git(["rev-parse", f"{commit}^{{tree}}"], repository) != tree:
            raise DevCheckError(f"acceptance {module} applied commit/tree differs")
        integration = _require_oid(
            mapped.get("integration_oid"),
            f"acceptance {module} integration commit",
        )
        ancestry = [(commit, integration)]
        if module in previous:
            ancestry.append((previous[module], commit))
        for ancestor, descendant in ancestry:
            completed = subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant],
                cwd=repository,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=300,
                check=False,
            )
            if completed.returncode != 0:
                raise DevCheckError(
                    f"acceptance {module} applied commit ancestry is invalid"
                )
        previous[module] = commit
    result["candidate_manifest"]["generated_locks"] = generated_locks
    return result


def _validate_embedded_acceptance_artifacts(
    artifacts: object, profile: str
) -> None:
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "immutable_oracle",
        "lock_first_evidence",
        "comparison",
        "module_map",
        "candidate_manifest",
    }:
        raise DevCheckError("acceptance receipt artifacts are incomplete")
    for name, row in artifacts.items():
        if not isinstance(row, dict):
            raise DevCheckError(f"embedded acceptance artifact {name} is invalid")
        expected_row_fields = {"source_path", "content", "sha256", "bytes"}
        if name == "candidate_manifest":
            expected_row_fields.add("generated_locks")
        if set(row) != expected_row_fields:
            raise DevCheckError(
                f"embedded acceptance artifact {name} fields are invalid"
            )
        content = row.get("content")
        if not isinstance(content, str):
            raise DevCheckError(f"embedded acceptance artifact {name} has no content")
        data = content.encode("utf-8")
        if (
            len(data) > _JSON_LIMIT
            or row.get("bytes") != len(data)
            or row.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise DevCheckError(f"embedded acceptance artifact {name} digest is invalid")
        try:
            value = json.loads(content)
        except json.JSONDecodeError as error:
            raise DevCheckError(
                f"embedded acceptance artifact {name} is invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise DevCheckError(f"embedded acceptance artifact {name} is invalid")
        if name == "immutable_oracle":
            valid_schema = (
                value.get("oracle_schema_version") == 2
                and value.get("mode") == "immutable-cherry-pick-oracle"
                and value.get("profile") == profile
                and value.get("verdict") == "VALID"
            )
        elif name in {"lock_first_evidence", "comparison"}:
            valid_schema = (
                value.get("evidence_schema_version") == 2
                and value.get("verdict") == "VALID"
            )
        elif name == "module_map":
            valid_schema = (
                value.get("profile") == profile
                and isinstance(value.get("modules"), list)
            )
        else:
            valid_schema = (
                isinstance(value.get("workspace_commit"), str)
                and isinstance(value.get("frozen_manifest_sha256"), str)
                and isinstance(value.get("generated_profile_locks"), list)
            )
        if not valid_schema:
            raise DevCheckError(f"embedded acceptance artifact {name} schema is invalid")
        if name == "candidate_manifest":
            outer = row["generated_locks"]
            declared = value["generated_profile_locks"]
            if (
                not isinstance(outer, list)
                or not outer
                or not isinstance(declared, list)
                or len(outer) != len(declared)
            ):
                raise DevCheckError("candidate generated lock extension is invalid")
            projected = []
            for generated in outer:
                if (
                    not isinstance(generated, dict)
                    or set(generated)
                    != {
                        "profile",
                        "path",
                        "size",
                        "sha256",
                        "semantic_sha256",
                        "content",
                    }
                    or not isinstance(generated.get("content"), str)
                ):
                    raise DevCheckError("candidate generated lock extension row is invalid")
                generated_data = generated["content"].encode("utf-8")
                if (
                    not isinstance(generated.get("size"), int)
                    or isinstance(generated.get("size"), bool)
                    or generated["size"] <= 0
                    or len(generated_data) != generated["size"]
                    or hashlib.sha256(generated_data).hexdigest()
                    != generated.get("sha256")
                    or not _is_lower_hex(generated.get("semantic_sha256"), 64)
                ):
                    raise DevCheckError(
                        "candidate generated lock extension digest is invalid"
                    )
                projected.append(
                    {
                        field: generated[field]
                        for field in (
                            "profile",
                            "path",
                            "size",
                            "sha256",
                            "semantic_sha256",
                        )
                    }
                )
            if projected != declared:
                raise DevCheckError(
                    "candidate generated lock extension differs from artifact"
                )


def _checkpoint_capture(data: bytes = b"") -> dict[str, Any]:
    tail = data[-_CAPTURE_LIMIT:].decode("utf-8", errors="replace")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "tail": tail,
        "truncated": len(data) > _CAPTURE_LIMIT,
    }


def _validate_checkpoint_capture(value: object, name: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"sha256", "bytes", "tail", "truncated"}
        or not _is_lower_hex(value.get("sha256"), 64)
        or not isinstance(value.get("bytes"), int)
        or isinstance(value.get("bytes"), bool)
        or value["bytes"] < 0
        or not isinstance(value.get("tail"), str)
        or len(value["tail"].encode("utf-8")) > _CAPTURE_LIMIT
        or not isinstance(value.get("truncated"), bool)
        or value["truncated"] != (value["bytes"] > _CAPTURE_LIMIT)
    ):
        raise DevCheckError(f"acceptance checkpoint {name} capture is invalid")




def _validate_checkpoint_oracle(
    payload: object, identity: dict[str, Any]
) -> None:
    fields = {
        "oracle_schema_version",
        "mode",
        "profile",
        "profile_order",
        "batches",
        "modules",
        "generated_profile_locks",
        "frozen_manifest_sha256",
        "clean_odb",
        "cleanup",
        "verdict",
    }
    profiles = [row["profile"] for row in identity["profile_graph"]]
    if (
        not isinstance(payload, dict)
        or set(payload) != fields
        or payload.get("oracle_schema_version") != 2
        or payload.get("mode") != "immutable-cherry-pick-oracle"
        or payload.get("profile") != identity["profile"]
        or payload.get("profile_order") != profiles
        or payload.get("frozen_manifest_sha256")
        != identity["frozen_manifest_sha256"]
        or payload.get("cleanup")
        != {"root": "removed", "worktrees": "removed", "refs": "removed"}
        or payload.get("verdict") != "VALID"
    ):
        raise DevCheckError("acceptance checkpoint oracle semantics differ")
    clean_odb = payload.get("clean_odb")
    if (
        not isinstance(clean_odb, dict)
        or set(clean_odb)
        != {
            "module_count",
            "immutable_fetch_transactions",
            "alternates",
            "shallow",
            "partial",
        }
        or not isinstance(clean_odb.get("module_count"), int)
        or isinstance(clean_odb.get("module_count"), bool)
        or clean_odb["module_count"] < 1
        or clean_odb.get("immutable_fetch_transactions")
        != clean_odb["module_count"]
        or any(
            clean_odb.get(name) != 0
            for name in ("alternates", "shallow", "partial")
        )
    ):
        raise DevCheckError("acceptance checkpoint oracle clean ODB is invalid")

    batches = payload.get("batches")
    if not isinstance(batches, list) or len(batches) != len(profiles):
        raise DevCheckError("acceptance checkpoint oracle batches are invalid")
    batch_modules: set[str] = set()
    for expected_profile, batch in zip(profiles, batches, strict=True):
        if (
            not isinstance(batch, dict)
            or set(batch)
            != {
                "profile",
                "batch_id",
                "expected_count",
                "module_order",
                "series_order",
                "series",
                "verdict",
            }
            or batch.get("profile") != expected_profile
            or not isinstance(batch.get("batch_id"), str)
            or not batch["batch_id"]
            or batch.get("verdict") != "VALID"
            or not isinstance(batch.get("expected_count"), int)
            or isinstance(batch.get("expected_count"), bool)
            or batch["expected_count"] < 1
            or not isinstance(batch.get("module_order"), list)
            or not batch["module_order"]
            or not all(
                isinstance(module, str) and module
                for module in batch["module_order"]
            )
            or len(set(batch["module_order"])) != len(batch["module_order"])
            or not isinstance(batch.get("series_order"), list)
            or not isinstance(batch.get("series"), list)
            or len(batch["series_order"]) != batch["expected_count"]
            or len(batch["series"]) != batch["expected_count"]
        ):
            raise DevCheckError("acceptance checkpoint oracle batch is invalid")
        observed_order: list[dict[str, str]] = []
        for order, row in zip(
            batch["series_order"], batch["series"], strict=True
        ):
            if (
                not isinstance(order, dict)
                or set(order) != {"module", "patch"}
                or not isinstance(row, dict)
                or set(row)
                != {
                    "module",
                    "patch",
                    "base",
                    "source",
                    "canonical_tree",
                    "applied_commit",
                    "applied_tree",
                    "verdict",
                }
                or row.get("verdict") != "VALID"
            ):
                raise DevCheckError(
                    "acceptance checkpoint oracle series is invalid"
                )
            module = _validate_text(row.get("module"), "checkpoint oracle module")
            patch = _validate_text(row.get("patch"), "checkpoint oracle patch")
            for field in (
                "base",
                "source",
                "canonical_tree",
                "applied_commit",
                "applied_tree",
            ):
                _require_oid(
                    row.get(field),
                    f"checkpoint oracle {module}/{patch} {field}",
                )
            observed = {"module": module, "patch": patch}
            if order != observed:
                raise DevCheckError(
                    "acceptance checkpoint oracle series order differs"
                )
            observed_order.append(observed)
        if (
            len({(row["module"], row["patch"]) for row in observed_order})
            != batch["expected_count"]
            or list(dict.fromkeys(row["module"] for row in observed_order))
            != batch["module_order"]
        ):
            raise DevCheckError(
                "acceptance checkpoint oracle series closure is invalid"
            )
        batch_modules.update(batch["module_order"])

    modules = payload.get("modules")
    if not isinstance(modules, list) or len(modules) != clean_odb["module_count"]:
        raise DevCheckError("acceptance checkpoint oracle modules are invalid")
    observed_modules: set[str] = set()
    for row in modules:
        if not isinstance(row, dict) or set(row) != {"module", "commit", "tree"}:
            raise DevCheckError("acceptance checkpoint oracle module is invalid")
        module = _validate_text(row.get("module"), "checkpoint oracle module")
        if module in observed_modules:
            raise DevCheckError(
                "acceptance checkpoint oracle module is duplicated"
            )
        observed_modules.add(module)
        _require_oid(row.get("commit"), f"checkpoint oracle {module} commit")
        _require_oid(row.get("tree"), f"checkpoint oracle {module} tree")
    if observed_modules != batch_modules:
        raise DevCheckError(
            "acceptance checkpoint oracle module closure differs"
        )

    generated = payload.get("generated_profile_locks")
    if not isinstance(generated, list) or len(generated) != len(profiles):
        raise DevCheckError(
            "acceptance checkpoint generated lock closure is invalid"
        )
    for expected_profile, row in zip(profiles, generated, strict=True):
        if (
            not isinstance(row, dict)
            or set(row) != {"profile", "path", "semantic_sha256"}
            or row.get("profile") != expected_profile
            or row.get("path")
            != f"patches/{expected_profile}/west.lock.yml"
            or not _is_lower_hex(row.get("semantic_sha256"), 64)
        ):
            raise DevCheckError(
                "acceptance checkpoint generated lock row is invalid"
            )
def _validate_checkpoint_payload(
    value: object, binding: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "kind",
        "key",
        "identity",
        "steps",
        "oracle",
    }:
        raise DevCheckError("acceptance checkpoint fields are invalid")
    if (
        value.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION
        or value.get("kind") != "west-dev-acceptance"
        or value.get("key") != binding["key"]
        or value.get("identity") != binding["identity"]
    ):
        raise DevCheckError("acceptance checkpoint identity differs")
    steps = value.get("steps")
    if not isinstance(steps, dict) or set(steps) != set(_CHECKPOINT_STEP_NAMES):
        raise DevCheckError("acceptance checkpoint step closure is invalid")
    for name in _CHECKPOINT_STEP_NAMES:
        row = steps[name]
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "returncode",
                "timed_out",
                "interrupted",
                "started_at",
                "finished_at",
                "duration_ms",
                "duration_ns",
                "stdout",
                "stderr",
                "stdout_tail",
                "stderr_tail",
                "process_group_quiescent",
            }
            or row.get("returncode") != 0
            or row.get("timed_out") is not False
            or row.get("interrupted") is not False
            or row.get("process_group_quiescent") is not True
            or not isinstance(row.get("duration_ms"), int)
            or isinstance(row.get("duration_ms"), bool)
            or row["duration_ms"] < 0
            or not isinstance(row.get("duration_ns"), int)
            or isinstance(row.get("duration_ns"), bool)
            or row["duration_ns"] < 0
            or not isinstance(row.get("started_at"), str)
            or not row["started_at"]
            or not isinstance(row.get("finished_at"), str)


            or not row["finished_at"]
            or not isinstance(row.get("stdout"), dict)
            or not isinstance(row.get("stderr"), dict)
            or row.get("stdout_tail") != row.get("stdout", {}).get("tail")
            or row.get("stderr_tail") != row.get("stderr", {}).get("tail")
        ):
            raise DevCheckError(f"acceptance checkpoint step is invalid: {name}")
        _validate_checkpoint_capture(row["stdout"], f"{name} stdout")
        _validate_checkpoint_capture(row["stderr"], f"{name} stderr")
    oracle = value.get("oracle")
    if not isinstance(oracle, dict) or set(oracle) != {
        "bytes",
        "sha256",
        "content",
    }:
        raise DevCheckError("acceptance checkpoint oracle binding is invalid")
    content = oracle.get("content")
    if not isinstance(content, str):
        raise DevCheckError("acceptance checkpoint oracle content is invalid")
    data = content.encode("utf-8")
    if (
        not isinstance(oracle.get("bytes"), int)
        or isinstance(oracle.get("bytes"), bool)
        or oracle["bytes"] != len(data)
        or len(data) > _CHECKPOINT_LIMIT
        or not _is_lower_hex(oracle.get("sha256"), 64)
        or oracle["sha256"] != hashlib.sha256(data).hexdigest()
    ):
        raise DevCheckError("acceptance checkpoint oracle digest differs")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise DevCheckError("acceptance checkpoint oracle JSON is invalid") from error
    _validate_checkpoint_oracle(payload, binding["identity"])
    return value


def _checkpoint_root(path: Path, create: bool) -> Path | None:
    root = path.parent
    if not root.exists():
        if not create:
            return None
        root.mkdir(mode=0o700)
        _fsync_directory(root.parent)
    metadata = root.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise DevCheckError("acceptance checkpoint root is unsafe")
    return root


@contextlib.contextmanager
def _locked_checkpoint(
    path: Path, create: bool
) -> Iterator[tuple[Path, int] | None]:
    root = _checkpoint_root(path, create)
    if root is None:
        yield None
        return
    expected_identity = _identity_from_stat(root.lstat())
    root_descriptor = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    lock_descriptor: int | None = None
    try:
        if _identity_from_stat(os.fstat(root_descriptor)) != expected_identity:
            raise DevCheckError("acceptance checkpoint root identity changed")
        lock_descriptor = os.open(
            ".lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
        lock_metadata = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.getuid()
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise DevCheckError("acceptance checkpoint lock is unsafe")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        if _identity_from_stat(os.fstat(root_descriptor)) != expected_identity:
            raise DevCheckError("acceptance checkpoint root identity changed")
        yield root, root_descriptor
    finally:
        if lock_descriptor is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
        os.close(root_descriptor)


def _prepare_checkpoint_subdirectory(path: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise DevCheckError("acceptance checkpoint subdirectory name is invalid")
    with _locked_checkpoint(path, True) as locked:
        assert locked is not None
        root, root_descriptor = locked
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
        except FileExistsError:
            pass
        try:
            metadata = os.stat(
                name, dir_fd=root_descriptor, follow_symlinks=False
            )
        except OSError as error:
            raise DevCheckError(
                f"acceptance checkpoint subdirectory is unavailable: {error}"
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise DevCheckError("acceptance checkpoint subdirectory is unsafe")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
        os.close(descriptor)
        return root / name
def _read_checkpoint_descriptor(
    root_descriptor: int, name: str
) -> tuple[int, os.stat_result] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DevCheckError(
            f"acceptance checkpoint path is unsafe: {error}"
        ) from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise DevCheckError("acceptance checkpoint path is unsafe")
    return descriptor, metadata


def _read_checkpoint_file(
    path: Path, binding: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    with _locked_checkpoint(path, False) as locked:
        if locked is None:
            return None, "missing"
        _root, root_descriptor = locked
        opened = _read_checkpoint_descriptor(root_descriptor, path.name)
        if opened is None:
            return None, "missing"
        descriptor, metadata = opened
        try:
            if metadata.st_size > _CHECKPOINT_LIMIT:
                return None, "oversized"
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                data = stream.read(_CHECKPOINT_LIMIT + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(data) > _CHECKPOINT_LIMIT:
            return None, "oversized"
        try:
            value = json.loads(data)
            return _validate_checkpoint_payload(value, binding), "valid"
        except (UnicodeError, json.JSONDecodeError, DevCheckError):
            return None, "invalid"


def _publish_checkpoint_file(
    path: Path, binding: dict[str, Any], payload: dict[str, Any]
) -> str:
    validated = _validate_checkpoint_payload(payload, binding)
    encoded = (
        json.dumps(validated, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > _CHECKPOINT_LIMIT:
        raise DevCheckError("acceptance checkpoint exceeds size bound")
    with _locked_checkpoint(path, True) as locked:
        assert locked is not None
        _root, root_descriptor = locked
        opened = _read_checkpoint_descriptor(root_descriptor, path.name)
        if opened is not None:
            descriptor, metadata = opened
            try:
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    existing_data = stream.read(_CHECKPOINT_LIMIT + 1)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            try:
                if len(existing_data) > _CHECKPOINT_LIMIT:
                    raise DevCheckError(
                        "existing acceptance checkpoint exceeds size bound"
                    )
                existing = json.loads(existing_data)
                _validate_checkpoint_payload(existing, binding)
                return "existing"
            except (UnicodeError, json.JSONDecodeError, DevCheckError):
                current = os.stat(
                    path.name,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                if _identity_from_stat(current) != _identity_from_stat(metadata):
                    raise DevCheckError(
                        "acceptance checkpoint identity changed before replacement"
                    )
                os.unlink(path.name, dir_fd=root_descriptor)
        temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if _identity_at_or_none(root_descriptor, path.name) is not None:
                raise DevCheckError(
                    "acceptance checkpoint appeared before publication"
                )
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
            os.fsync(root_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=root_descriptor)
    return "published"


def _materialize_checkpoint_oracle(
    checkpoint: dict[str, Any], destination: Path
) -> None:
    if _path_exists(destination):
        raise DevCheckError("immutable oracle output appeared before checkpoint reuse")
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = checkpoint["oracle"]["content"].encode("utf-8")
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(destination.parent)


def _checkpoint_result(
    planned_step: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    timestamp = _utc_now()
    return {
        **copy.deepcopy(planned_step),
        "returncode": 0,
        "timed_out": False,
        "interrupted": False,
        "started_at": timestamp,
        "finished_at": timestamp,
        "duration_ms": 0,
        "duration_ns": 0,
        "stdout": copy.deepcopy(source["stdout"]),
        "stderr": copy.deepcopy(source["stderr"]),
        "stdout_tail": source["stdout_tail"],
        "stderr_tail": source["stderr_tail"],
        "process_group_quiescent": True,
        "checkpoint_reused": True,
        "checkpoint_source_started_at": source["started_at"],
        "checkpoint_source_finished_at": source["finished_at"],
        "checkpoint_source_duration_ms": source["duration_ms"],
        "checkpoint_source_duration_ns": source["duration_ns"],
    }


def _warm_cache_result(planned_step: dict[str, Any]) -> dict[str, Any]:
    timestamp = _utc_now()
    stdout = _checkpoint_capture(b"candidate cache pruned redundant step\n")
    empty = _checkpoint_capture()
    return {
        **copy.deepcopy(planned_step),
        "returncode": 0,
        "timed_out": False,
        "interrupted": False,
        "started_at": timestamp,
        "finished_at": timestamp,
        "duration_ms": 0,
        "duration_ns": 0,
        "stdout": stdout,
        "stderr": empty,
        "stdout_tail": stdout["tail"],
        "stderr_tail": empty["tail"],
        "process_group_quiescent": True,
        "warm_cache_pruned": True,
    }


def _checkpoint_payload(
    binding: dict[str, Any],
    results: dict[str, dict[str, Any]],
    oracle_path: Path,
) -> dict[str, Any]:
    data = oracle_path.read_bytes()
    if len(data) > _CHECKPOINT_LIMIT:
        raise DevCheckError("immutable oracle output exceeds checkpoint bound")
    return {
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "kind": "west-dev-acceptance",
        "key": binding["key"],
        "identity": binding["identity"],
        "steps": {
            name: {
                field: copy.deepcopy(results[name][field])
                for field in (
                    "returncode",
                    "timed_out",
                    "interrupted",
                    "started_at",
                    "finished_at",
                    "duration_ms",
                    "duration_ns",
                    "stdout",
                    "stderr",
                    "stdout_tail",
                    "stderr_tail",
                    "process_group_quiescent",
                )
            }
            for name in _CHECKPOINT_STEP_NAMES
        },
        "oracle": {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content": data.decode("utf-8"),
        },
    }

class _ParallelInterrupted(KeyboardInterrupt):
    def __init__(self, results: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.results = results


class _StepFailure(DevCheckError):
    def __init__(self, name: str, returncode: int, interrupted: bool = False) -> None:
        super().__init__(f"{name} failed with returncode {returncode}")
        self.returncode = returncode
        self.interrupted = interrupted

def _normalized_returncode(returncode: int) -> int:
    return 128 + (-returncode) if returncode < 0 else returncode


def _progress(
    callback: Callable[[dict[str, Any]], None] | None,
    phase: str,
    planned_step: dict[str, Any],
    index: int,
    total: int,
    started: float,
    **fields: Any,
) -> None:
    if callback is None:
        return
    callback(
        {
            "phase": phase,
            "name": planned_step["name"],
            "index": index,
            "total": total,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            **fields,
        }
    )


def _dependency_cancelled_result(
    planned_step: dict[str, Any], dependency: str
) -> dict[str, Any]:
    timestamp = _utc_now()
    stdout = _checkpoint_capture()
    stderr = _checkpoint_capture(
        f"not run because dependency failed: {dependency}\n".encode()
    )
    return {
        **copy.deepcopy(planned_step),
        "returncode": 125,
        "timed_out": False,
        "interrupted": False,
        "started_at": timestamp,
        "finished_at": timestamp,
        "duration_ms": 0,
        "duration_ns": 0,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_tail": stdout["tail"],
        "stderr_tail": stderr["tail"],
        "process_group_quiescent": True,
        "cancelled_by_peer": True,
    }


def _run_parallel_acceptance_steps(
    planned_steps: list[dict[str, Any]],
    indices: dict[str, int],
    total: int,
    started: float,
    progress: Callable[[dict[str, Any]], None] | None,
    dependencies: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, dict[str, Any]]:
    cancellation = _ProcessCancellation()
    progress_lock = threading.Lock()
    dependency_names = dependencies or {}
    futures_by_name: dict[
        str, concurrent.futures.Future[dict[str, Any]]
    ] = {}

    def notify(
        phase: str, step: dict[str, Any], **fields: Any
    ) -> None:
        with progress_lock:
            _progress(
                progress,
                phase,
                step,
                indices[step["name"]],
                total,
                started,
                **fields,
            )

    def run(step: dict[str, Any]) -> dict[str, Any]:
        for dependency in dependency_names.get(step["name"], ()):
            dependency_result = futures_by_name[dependency].result()
            if dependency_result["returncode"] != 0:
                result = _dependency_cancelled_result(step, dependency)
                notify(
                    "finish",
                    step,
                    duration_ms=0,
                    returncode=result["returncode"],
                )
                return result
        notify("start", step)
        outcome = _run_process(
            list(step["argv"]),
            Path(step["cwd"]),
            int(step["timeout_seconds"]),
            dict(step["env"]),
            cancellation=cancellation,
            sanitize_environment=True,
        )
        result = copy.deepcopy(step)
        result.update(outcome)
        notify(
            "finish",
            step,
            duration_ms=result["duration_ms"],
            returncode=result["returncode"],
        )
        if result["returncode"] != 0 or any(
            "capture_error" in result[stream] for stream in ("stdout", "stderr")
        ):
            cancellation.cancel(signal.SIGINT)
        return result

    futures: dict[
        concurrent.futures.Future[dict[str, Any]], dict[str, Any]
    ] = {}
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=len(planned_steps), thread_name_prefix="west-dev-check"
    )
    try:
        for step in planned_steps:
            future = executor.submit(run, step)
            futures[future] = step
            futures_by_name[step["name"]] = future
        results = {
            futures[future]["name"]: future.result()
            for future in concurrent.futures.as_completed(futures)
        }
    except KeyboardInterrupt:
        cancellation.cancel(signal.SIGINT)
        interrupted_results: dict[str, dict[str, Any]] = {}
        for future, step in futures.items():
            with contextlib.suppress(BaseException):
                interrupted_results[step["name"]] = future.result()
        raise _ParallelInterrupted(interrupted_results)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return results


def _raise_step_failure(result: dict[str, Any]) -> None:
    if result["returncode"] != 0:
        raise _StepFailure(
            result["name"],
            result["returncode"],
            result["interrupted"]
            or result["returncode"] == -int(signal.SIGINT),
        )
    if (
        "capture_error" in result["stdout"]
        or "capture_error" in result["stderr"]
    ):
        raise _StepFailure(f"{result['name']}-capture", 1)


def _parallel_failure(
    ordered_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for include_cancelled in (False, True):
        for result in ordered_results:
            failed = result["returncode"] != 0 or any(
                "capture_error" in result[stream] for stream in ("stdout", "stderr")
            )
            if failed and (
                include_cancelled or not result.get("cancelled_by_peer", False)
            ):
                return result
    return None


def execute_check(
    plan: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute a frozen check plan and return its durable receipt."""
    repo, evidence = _validate_check_plan(plan)
    evidence_fd = _open_pinned_directory(
        evidence.parent, plan["inputs"]["evidence_parent_identity"]
    )
    record = _active_record(plan)
    record["_started_monotonic"] = time.monotonic()
    started = record["_started_monotonic"]
    try:
        evidence_identity = _create_json(evidence, record, evidence_fd)
        record["state"] = "active"
        record["started_at"] = _utc_now()
        record["next_safe_action"] = "Wait for the active check or interrupt it once."
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
    except BaseException:
        os.close(evidence_fd)
        raise
    scratch: Path | None = None
    scratch_identity: dict[str, int] | None = None
    checkpoint: dict[str, Any] | None = None
    candidate_cache_available = False
    total = len(plan["steps"])
    indices = {
        step["name"]: index
        for index, step in enumerate(plan["steps"], start=1)
    }

    def publish_result(result: dict[str, Any]) -> None:
        nonlocal evidence_identity
        record["results"].append(result)
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )

    try:
        if plan["inputs"]["tier"] == "acceptance":
            scratch, scratch_identity = _create_check_scratch(plan)
            record["scratch"] = {
                "path": str(scratch),
                "identity": scratch_identity,
                "state": "active",
            }
            binding = plan["inputs"]["acceptance_checkpoint"]
            checkpoint_path = Path(binding["path"])
            candidate_cache = (
                checkpoint_path.parent / f"candidate-{binding['key']}"
            )
            candidate_cache_available = (
                candidate_cache.exists() or candidate_cache.is_symlink()
            )
            _prepare_checkpoint_subdirectory(
                checkpoint_path, "runtime-ccache-v1"
            )
            checkpoint, checkpoint_reason = _read_checkpoint_file(
                checkpoint_path, binding
            )
            record["checkpoint"] = {
                "schema_version": _CHECKPOINT_SCHEMA_VERSION,
                "key": binding["key"],
                "path": str(checkpoint_path),
                "state": "reused" if checkpoint is not None else "miss",
                "reason": checkpoint_reason,
            }
            evidence_identity = _atomic_json(
                evidence, record, evidence_fd, evidence_identity
            )
        position = 0
        while position < len(plan["steps"]):
            planned_step = plan["steps"][position]
            if (
                plan["inputs"]["tier"] == "acceptance"
                and planned_step["name"] == _INITIAL_CLONE_STEP_NAMES[0]
            ):
                parallel_steps = plan["steps"][
                    position : position + len(_INITIAL_CLONE_STEP_NAMES)
                ]
                if [step["name"] for step in parallel_steps] != list(
                    _INITIAL_CLONE_STEP_NAMES
                ):
                    raise DevCheckError(
                        "acceptance initial clones are not one contiguous wave"
                    )
                try:
                    parallel_results = _run_parallel_acceptance_steps(
                        parallel_steps,
                        indices,
                        total,
                        started,
                        progress,
                    )
                except _ParallelInterrupted as error:
                    for step in parallel_steps:
                        result = error.results.get(step["name"])
                        if result is not None:
                            publish_result(result)
                    raise KeyboardInterrupt from None
                ordered_results = [
                    parallel_results[step["name"]] for step in parallel_steps
                ]
                for result in ordered_results:
                    publish_result(result)
                failed = _parallel_failure(ordered_results)
                if failed is not None:
                    _raise_step_failure(failed)
                position += len(_INITIAL_CLONE_STEP_NAMES)
                continue

            if (
                plan["inputs"]["tier"] == "acceptance"
                and planned_step["name"] == _CHECKPOINT_STEP_NAMES[0]
            ):
                parallel_steps = plan["steps"][
                    position : position + len(_CHECKPOINT_STEP_NAMES)
                ]
                if [step["name"] for step in parallel_steps] != list(
                    _CHECKPOINT_STEP_NAMES
                ):
                    raise DevCheckError(
                        "acceptance checkpoint steps are not one contiguous wave"
                    )
                if checkpoint is not None:
                    assert scratch is not None
                    _materialize_checkpoint_oracle(
                        checkpoint,
                        scratch / "evidence" / "immutable-oracle.json",
                    )
                    for step in parallel_steps:
                        source = checkpoint["steps"][step["name"]]
                        result = _checkpoint_result(step, source)
                        _progress(
                            progress,
                            "reuse",
                            step,
                            indices[step["name"]],
                            total,
                            started,
                            source_duration_ms=source["duration_ms"],
                        )
                        publish_result(result)
                else:
                    try:
                        parallel_results = _run_parallel_acceptance_steps(
                            parallel_steps,
                            indices,
                            total,
                            started,
                            progress,
                        )
                    except _ParallelInterrupted as error:
                        for step in parallel_steps:
                            result = error.results.get(step["name"])
                            if result is not None:
                                publish_result(result)
                        raise KeyboardInterrupt from None
                    ordered_results = [
                        parallel_results[step["name"]] for step in parallel_steps
                    ]
                    for result in ordered_results:
                        publish_result(result)
                    failed = _parallel_failure(ordered_results)
                    if failed is not None:
                        _raise_step_failure(failed)
                    assert scratch is not None
                    binding = plan["inputs"]["acceptance_checkpoint"]
                    publication = _publish_checkpoint_file(
                        Path(binding["path"]),
                        binding,
                        _checkpoint_payload(
                            binding,
                            parallel_results,
                            scratch / "evidence" / "immutable-oracle.json",
                        ),
                    )
                    record["checkpoint"]["state"] = "published"
                    record["checkpoint"]["reason"] = publication
                    evidence_identity = _atomic_json(
                        evidence, record, evidence_fd, evidence_identity
                    )
                position += len(_CHECKPOINT_STEP_NAMES)
                continue

            if (
                candidate_cache_available
                and planned_step["name"] in _CANDIDATE_CACHE_PRUNED_STEPS
            ):
                result = _warm_cache_result(planned_step)
                _progress(
                    progress,
                    "reuse",
                    planned_step,
                    indices[planned_step["name"]],
                    total,
                    started,
                    source_duration_ms=0,
                )
                publish_result(result)
                position += 1
                continue

            if (
                plan["inputs"]["tier"] == "acceptance"
                and planned_step["name"] == _FINAL_TIER_STEP_NAMES[0]
            ):
                parallel_steps = plan["steps"][
                    position : position + len(_FINAL_TIER_STEP_NAMES)
                ]
                cleanup_position = position + len(_FINAL_TIER_STEP_NAMES)
                if (
                    [step["name"] for step in parallel_steps]
                    != list(_FINAL_TIER_STEP_NAMES)
                    or cleanup_position >= len(plan["steps"])
                    or plan["steps"][cleanup_position]["name"]
                    != "acceptance-final-cleanup"
                ):
                    raise DevCheckError(
                        "acceptance final tiers are not one cleanup-bound wave"
                    )
                interrupted = False
                try:
                    parallel_results = _run_parallel_acceptance_steps(
                        parallel_steps,
                        indices,
                        total,
                        started,
                        progress,
                        dependencies={
                            "acceptance-guest-smoke": (
                                "acceptance-clone-guest-candidate",
                            )
                        },
                    )
                except _ParallelInterrupted as error:
                    parallel_results = error.results
                    interrupted = True
                ordered_results = [
                    parallel_results[step["name"]]
                    for step in parallel_steps
                    if step["name"] in parallel_results
                ]
                for result in ordered_results:
                    publish_result(result)
                failed = _parallel_failure(ordered_results)
                cleanup_step = plan["steps"][cleanup_position]
                _progress(
                    progress,
                    "start",
                    cleanup_step,
                    indices[cleanup_step["name"]],
                    total,
                    started,
                )
                cleanup_outcome = _run_process(
                    list(cleanup_step["argv"]),
                    Path(cleanup_step["cwd"]),
                    int(cleanup_step["timeout_seconds"]),
                    dict(cleanup_step["env"]),
                    sanitize_environment=True,
                )
                cleanup_result = copy.deepcopy(cleanup_step)
                cleanup_result.update(cleanup_outcome)
                _progress(
                    progress,
                    "finish",
                    cleanup_step,
                    indices[cleanup_step["name"]],
                    total,
                    started,
                    duration_ms=cleanup_result["duration_ms"],
                    returncode=cleanup_result["returncode"],
                )
                publish_result(cleanup_result)
                cleanup_failed = _parallel_failure([cleanup_result])
                if cleanup_failed is not None:
                    if failed is not None:
                        raise _StepFailure(
                            f"{failed['name']} and {cleanup_result['name']}",
                            cleanup_result["returncode"] or 1,
                        )
                    _raise_step_failure(cleanup_failed)
                if interrupted:
                    raise KeyboardInterrupt
                if failed is not None:
                    _raise_step_failure(failed)
                position = cleanup_position + 1
                continue

            _progress(
                progress,
                "start",
                planned_step,
                indices[planned_step["name"]],
                total,
                started,
            )
            outcome = _run_process(
                list(planned_step["argv"]),
                Path(planned_step["cwd"]),
                int(planned_step["timeout_seconds"]),
                dict(planned_step["env"]),
            )
            result = copy.deepcopy(planned_step)
            result.update(outcome)
            if (
                planned_step["name"] == "test-list"
                and (
                    plan["inputs"]["bead"] is not None
                    or plan["inputs"]["patch"] is not None
                )
                and outcome["returncode"] == 0
            ):
                count = _listed_test_count(
                    outcome["stdout_tail"] + "\n" + outcome["stderr_tail"]
                )
                result["selection_oracle"] = {
                    "required": True,
                    "observed_listing_rows": count,
                    "selected": count > 0,
                }
            _progress(
                progress,
                "finish",
                planned_step,
                indices[planned_step["name"]],
                total,
                started,
                duration_ms=result["duration_ms"],
                returncode=result["returncode"],
            )
            publish_result(result)
            _raise_step_failure(result)
            oracle = result.get("selection_oracle")
            if isinstance(oracle, dict) and not oracle["selected"]:
                raise _StepFailure("test-list-selection-oracle", 1)
            position += 1
        if scratch is not None:
            record["acceptance_artifacts"] = _acceptance_artifact_receipt(scratch)
        current_snapshot = _collect_package_snapshot(
            repo, plan["inputs"]["profile"]
        )
        if current_snapshot != plan["inputs"]["package_snapshot"]:
            raise _StepFailure("package-input-snapshot", 1)
        if scratch is not None and scratch_identity is not None:
            _cleanup_check_scratch(
                scratch, scratch_identity, plan["transaction_id"]
            )
            record["scratch"]["state"] = "cleaned"
            scratch = None
            scratch_identity = None
        _finish_record(
            record,
            "committed",
            0,
            "Use this receipt for packaging or run a higher check tier.",
        )
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
        return record
    except BaseException as error:
        cleanup_error: BaseException | None = None
        if scratch is not None and scratch_identity is not None:
            try:
                _cleanup_check_scratch(
                    scratch, scratch_identity, plan["transaction_id"]
                )
                record["scratch"]["state"] = "cleaned"
            except BaseException as observed:
                cleanup_error = observed
                record["scratch"]["state"] = "cleanup-failed"
        if isinstance(error, _StepFailure):
            returncode = _normalized_returncode(error.returncode)
            interrupted = error.interrupted
        elif isinstance(error, KeyboardInterrupt):
            returncode = 130
            interrupted = True
        else:
            returncode = 1
            interrupted = False
        detail = f"{type(error).__name__}: {error}"
        if cleanup_error is not None:
            detail += (
                f"; cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
            )
        record["error"] = detail
        state = "interrupted" if interrupted and cleanup_error is None else "failed"
        _finish_record(
            record,
            state,
            returncode,
            "Inspect the receipt, then rerun the complete check.",
        )
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
        return record
    finally:
        os.close(evidence_fd)


def _read_json_file(path: Path, description: str) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise DevCheckError(f"{description} does not exist: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DevCheckError(f"{description} must be a regular non-symlink file")
    if metadata.st_size > _JSON_LIMIT:
        raise DevCheckError(f"{description} exceeds {_JSON_LIMIT} bytes")
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DevCheckError(f"cannot read {description}: {error}") from error
    if not isinstance(value, dict):
        raise DevCheckError(f"{description} must contain a JSON object")
    return value, data


def _validate_checkpoint_receipt(
    receipt: dict[str, Any],
    inputs: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    binding = _validate_checkpoint_binding(
        inputs.get("acceptance_checkpoint"),
        inputs["profile"],
        inputs["west_argv"],
    )
    checkpoint = receipt.get("checkpoint")
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint)
        != {"schema_version", "key", "path", "state", "reason"}
        or checkpoint.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("key") != binding["key"]
        or checkpoint.get("path") != binding["path"]
    ):
        raise DevCheckError("check receipt checkpoint binding is invalid")
    state = checkpoint.get("state")
    reason = checkpoint.get("reason")
    if (
        state == "reused"
        and reason != "valid"
        or state == "published"
        and reason not in {"published", "existing"}
        or state not in {"reused", "published"}
    ):
        raise DevCheckError("check receipt checkpoint verdict is invalid")

    checkpoint_fields = {
        "checkpoint_reused",
        "checkpoint_source_started_at",
        "checkpoint_source_finished_at",
        "checkpoint_source_duration_ms",
        "checkpoint_source_duration_ns",
    }
    by_name = {result.get("name"): result for result in results}
    if len(by_name) != len(results):
        raise DevCheckError("check receipt result names are duplicated")
    for name, result in by_name.items():
        present = checkpoint_fields.intersection(result)
        if name not in _CHECKPOINT_STEP_NAMES:
            if present:
                raise DevCheckError(
                    "non-checkpoint result claims checkpoint provenance"
                )
            continue
        if state == "published":
            if present:
                raise DevCheckError(
                    "executed checkpoint result claims reuse provenance"
                )
            continue
        if (
            present != checkpoint_fields
            or result.get("checkpoint_reused") is not True
            or result.get("duration_ms") != 0
            or result.get("duration_ns") != 0
            or not isinstance(result.get("checkpoint_source_started_at"), str)
            or not result["checkpoint_source_started_at"]
            or not isinstance(result.get("checkpoint_source_finished_at"), str)
            or not result["checkpoint_source_finished_at"]
            or not isinstance(result.get("checkpoint_source_duration_ms"), int)
            or isinstance(result.get("checkpoint_source_duration_ms"), bool)
            or result["checkpoint_source_duration_ms"] < 0
            or not isinstance(result.get("checkpoint_source_duration_ns"), int)
            or isinstance(result.get("checkpoint_source_duration_ns"), bool)
            or result["checkpoint_source_duration_ns"] < 0
        ):
            raise DevCheckError(
                "reused checkpoint result provenance is invalid"
            )
    if state == "reused" and not all(
        name in by_name for name in _CHECKPOINT_STEP_NAMES
    ):
        raise DevCheckError("reused checkpoint result closure is incomplete")


def _validate_check_receipt(
    receipt: Path,
    expected_manifest_repo: Path,
    profile: str,
    required_tier: str,
) -> tuple[dict[str, Any], str]:
    value, data = _read_json_file(receipt, "check receipt")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise DevCheckError("check receipt has an unsupported schema_version")
    if value.get("operation") != "check":
        raise DevCheckError("receipt is not a check receipt")
    if value.get("state") != "committed" or value.get("returncode") != 0:
        raise DevCheckError("check receipt is not committed successfully")
    transaction_id = _validate_text(
        value.get("transaction_id"), "check receipt transaction_id"
    )
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise DevCheckError("check receipt transaction_id is invalid")
    inputs = value.get("inputs")
    if not isinstance(inputs, dict):
        raise DevCheckError("check receipt inputs are missing")
    if inputs.get("profile") != profile:
        raise DevCheckError("check receipt profile does not match package profile")
    observed_tier = _tier(inputs.get("tier"), "check receipt tier")
    if _TIER_RANK[observed_tier] < _TIER_RANK[required_tier]:
        raise DevCheckError(
            f"check receipt tier {observed_tier!r} is below required tier {required_tier!r}"
        )
    if observed_tier != "acceptance":
        raise DevCheckError("review package requires an acceptance check receipt")
    try:
        recorded_manifest_repo = Path(
            _validate_text(inputs.get("manifest_repo"), "check receipt manifest_repo")
        )
        recorded_evidence = Path(
            _validate_text(inputs.get("evidence"), "check receipt evidence")
        )
        _validate_west_argv(inputs.get("west_argv"))
        for name in ("bead", "patch", "prefix", "build_dir"):
            selected = inputs.get(name)
            if selected is not None:
                _validate_text(selected, f"check receipt {name}")
    except (TypeError, DevCheckError) as error:
        raise DevCheckError(f"check receipt inputs are invalid: {error}") from error
    if not recorded_manifest_repo.is_absolute() or not recorded_evidence.is_absolute():
        raise DevCheckError("check receipt paths must be absolute")
    if recorded_evidence != receipt:
        raise DevCheckError("check receipt path does not match its recorded evidence path")
    if recorded_manifest_repo != expected_manifest_repo:
        raise DevCheckError("check receipt manifest_repo does not match package input")
    if inputs.get("bead") is not None or inputs.get("patch") is not None:
        raise DevCheckError("whole-profile package rejects narrowed check receipts")
    snapshot = inputs.get("package_snapshot")
    if not isinstance(snapshot, dict):
        raise DevCheckError("check receipt package snapshot is missing")
    _validate_package_snapshot(snapshot, profile)
    if _collect_package_snapshot(expected_manifest_repo, profile) != snapshot:
        raise DevCheckError("package inputs changed after the committed check")
    recorded_checkpoint = _validate_checkpoint_binding(
        inputs.get("acceptance_checkpoint"),
        profile,
        inputs["west_argv"],
    )
    current_checkpoint = _acceptance_checkpoint_plan(
        expected_manifest_repo,
        profile,
        snapshot,
        inputs["west_argv"],
        recorded_checkpoint["identity"],
    )
    for field in (
        "workspace_commit",
        "workspace_tree",
        "profile",
        "profile_graph",
        "mappings",
        "patches",
        "locks",
        "frozen_manifest_sha256",
    ):
        if (
            recorded_checkpoint["identity"].get(field)
            != current_checkpoint["identity"].get(field)
        ):
            raise DevCheckError(
                "check receipt acceptance checkpoint content differs"
            )
    try:
        _validate_acceptance_inputs(inputs)
        expected_steps = _check_steps(inputs, transaction_id)
    except (KeyError, TypeError, DevCheckError) as error:
        raise DevCheckError(f"check receipt inputs are invalid: {error}") from error
    steps = value.get("steps")
    results = value.get("results")
    if steps != expected_steps:
        raise DevCheckError("check receipt steps do not match its frozen inputs")
    if not isinstance(results, list) or len(results) != len(expected_steps):
        raise DevCheckError("check receipt results are incomplete")
    for planned, result in zip(expected_steps, results, strict=True):
        if not isinstance(result, dict):
            raise DevCheckError("check receipt result is not an object")
        if any(result.get(key) != planned[key] for key in planned):
            raise DevCheckError("check receipt result does not match its planned step")
        if (
            result.get("returncode") != 0
            or result.get("timed_out") is not False
            or result.get("interrupted") is not False
        ):
            raise DevCheckError("committed check receipt contains a failed step")
        if result.get("process_group_quiescent") is not True:
            raise DevCheckError("committed check receipt retained process descendants")
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        if not isinstance(stdout, dict) or not isinstance(stderr, dict):
            raise DevCheckError("check receipt result capture is missing")
        for capture in (stdout, stderr):
            digest = capture.get("sha256")
            tail = capture.get("tail")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or not isinstance(tail, str)
                or len(tail.encode("utf-8")) > _CAPTURE_LIMIT
                or not isinstance(capture.get("bytes"), int)
                or isinstance(capture.get("bytes"), bool)
                or capture["bytes"] < 0
                or not isinstance(capture.get("truncated"), bool)
            ):
                raise DevCheckError("check receipt result capture is invalid")
        if (
            result.get("stdout_tail") != stdout["tail"]
            or result.get("stderr_tail") != stderr["tail"]
        ):
            raise DevCheckError("check receipt result capture tail differs")
    _validate_checkpoint_receipt(value, inputs, results)
    _validate_embedded_acceptance_artifacts(
        value.get("acceptance_artifacts"), profile
    )
    scratch = value.get("scratch")
    if not isinstance(scratch, dict) or scratch.get("state") != "cleaned":
        raise DevCheckError("check receipt cleanup is incomplete")
    return value, hashlib.sha256(data).hexdigest()


def _package_step(inputs: dict[str, Any]) -> dict[str, Any]:
    return _step(
        "patch-export-locks",
        [
            *inputs["west_argv"],
            "patch",
            "export-locks",
            "--profile",
            inputs["profile"],
            "--output",
            inputs["staging"],
        ],
        "temporary/local-output",
        _PACKAGE_TIMEOUT_SECONDS,
        Path(inputs["manifest_repo"]),
    )


def _package_export_cache_path(
    manifest_repo: Path, receipt: dict[str, Any]
) -> tuple[Path, str]:
    checkpoint = receipt["inputs"].get("acceptance_checkpoint")
    if not isinstance(checkpoint, dict):
        raise DevCheckError("acceptance receipt has no package cache identity")
    key = _require_digest(checkpoint.get("key"), "acceptance checkpoint key")
    return (
        _git_common_directory(manifest_repo)
        / "west-dev-package-export-v1"
        / key,
        key,
    )


def _reflink_or_copy(source: str, destination: str) -> str:
    source_path = Path(source)
    destination_path = Path(destination)
    with source_path.open("rb") as source_stream:
        with destination_path.open("xb") as destination_stream:
            try:
                fcntl.ioctl(
                    destination_stream.fileno(),
                    _FICLONE,
                    source_stream.fileno(),
                )
            except OSError:
                destination_stream.seek(0)
                destination_stream.truncate()
                shutil.copyfileobj(
                    source_stream, destination_stream, length=1024 * 1024
                )
    shutil.copystat(source_path, destination_path, follow_symlinks=False)
    return destination


def _verify_package_export_cache(entry: Path, key: str) -> Path:
    if entry.is_symlink() or not entry.is_dir():
        raise DevCheckError("package export cache entry is not a real directory")
    index, _data = _read_json_file(
        entry / "cache-index.json", "package export cache index"
    )
    if (
        index.get("schema_version") != _PACKAGE_CACHE_SCHEMA_VERSION
        or index.get("kind") != "west-dev-package-export"
        or index.get("key") != key
        or not isinstance(index.get("files"), list)
    ):
        raise DevCheckError("package export cache identity is invalid")
    payload = entry / "payload"
    files, _directories = _scan_package_tree(payload)
    observed = _package_file_rows(payload)
    if index["files"] != observed or set(files) != {
        row["path"] for row in observed
    }:
        raise DevCheckError("package export cache content differs from its index")
    return payload


def _hydrate_package_export_cache(entry: Path, key: str, destination: Path) -> None:
    payload = _verify_package_export_cache(entry, key)
    shutil.copytree(payload, destination, copy_function=_reflink_or_copy)


def _fsync_package_cache_tree(cache: Path) -> None:
    files, directories = _scan_package_tree(cache)
    for path in files.values():
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for relative in sorted(
        directories, key=lambda value: len(Path(value).parts), reverse=True
    ):
        _fsync_directory(cache / relative)
    _fsync_directory(cache)


def _publish_package_export_cache(
    entry: Path, key: str, package: Path
) -> str:
    root = entry.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise DevCheckError("package export cache root is not a real directory")
    if entry.exists():
        _verify_package_export_cache(entry, key)
        return "existing"
    temporary = root / f".{key}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.mkdir(mode=0o700)
        payload = temporary / "payload"
        shutil.copytree(package, payload, copy_function=_reflink_or_copy)
        _write_package_payload(
            temporary / "cache-index.json",
            _json_bytes(
                {
                    "schema_version": _PACKAGE_CACHE_SCHEMA_VERSION,
                    "kind": "west-dev-package-export",
                    "key": key,
                    "files": _package_file_rows(payload),
                }
            ),
        )
        _fsync_package_cache_tree(temporary)
        _verify_package_export_cache(temporary, key)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            try:
                _rename_noreplace(
                    root_fd,
                    temporary.name,
                    root_fd,
                    entry.name,
                )
            except DevCheckError as error:
                if "appeared before publication" not in str(error):
                    raise
                shutil.rmtree(temporary)
                _verify_package_export_cache(entry, key)
                return "existing"
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        return "published"
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def build_package_plan(
    manifest_repo: Path,
    west_argv: list[str],
    profile: str,
    receipt: Path,
    output: Path,
    evidence: Path,
    required_tier: str,
) -> dict[str, Any]:
    """Validate the gate and build a frozen package plan without mutation."""
    repo = _absolute(manifest_repo, "manifest_repo")
    selected_profile = _validate_text(profile, "profile")
    selected_tier = _tier(required_tier, "required_tier")
    receipt_path = _absolute(receipt, "receipt")
    output_path = _absolute(output, "output")
    evidence_path = _absolute(evidence, "evidence")
    if not repo.is_dir():
        raise DevCheckError(f"manifest_repo is not a directory: {repo}")
    if not output_path.parent.is_dir() or output_path.parent.is_symlink():
        raise DevCheckError("package output parent must be a real existing directory")
    if not evidence_path.parent.is_dir() or evidence_path.parent.is_symlink():
        raise DevCheckError("package evidence parent must be a real existing directory")
    if _is_within(output_path, repo) or _is_within(evidence_path, repo):
        raise DevCheckError(
            "package output and evidence must be outside the active manifest repository"
        )
    if _path_exists(evidence_path):
        raise DevCheckError(f"package evidence already exists: {evidence_path}")
    if receipt_path == evidence_path:
        raise DevCheckError("check receipt and package evidence must be distinct")
    if _path_exists(output_path):
        raise DevCheckError(f"package output already exists: {output_path}")
    if _is_within(receipt_path, output_path) or _is_within(evidence_path, output_path):
        raise DevCheckError("receipt and package evidence must be outside package output")
    receipt_value, receipt_digest = _validate_check_receipt(
        receipt_path, repo, selected_profile, selected_tier
    )
    transaction_id = uuid.uuid4().hex
    staging_root = output_path.with_name(
        f".{output_path.name}.west-dev-package-{transaction_id}"
    )
    staging = staging_root / "package"
    marker = staging_root / "owner.json"
    if _path_exists(staging_root):
        raise DevCheckError("package transaction root already exists")
    inputs = {
        "manifest_repo": str(repo),
        "west_argv": _validate_west_argv(west_argv),
        "profile": selected_profile,
        "receipt": str(receipt_path),
        "receipt_sha256": receipt_digest,
        "receipt_transaction_id": receipt_value["transaction_id"],
        "receipt_tier": receipt_value["inputs"]["tier"],
        "required_tier": selected_tier,
        "package_snapshot": receipt_value["inputs"]["package_snapshot"],
        "output": str(output_path),
        "staging_root": str(staging_root),
        "staging": str(staging),
        "marker": str(marker),
        "output_parent_identity": _path_identity(output_path.parent),
        "evidence": str(evidence_path),
        "evidence_parent_identity": _path_identity(evidence_path.parent),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "package",
        "transaction_id": transaction_id,
        "state": "planned",
        "returncode": None,
        "created_at": _utc_now(),
        "inputs": inputs,
        "steps": [_package_step(inputs)],
        "results": [],
        "next_safe_action": "Execute this frozen package plan.",
    }


def _validate_package_plan(
    plan: dict[str, Any],
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    _validate_plan_envelope(plan, "package")
    inputs = plan["inputs"]
    repo = Path(_validate_text(inputs.get("manifest_repo"), "manifest_repo"))
    receipt = Path(_validate_text(inputs.get("receipt"), "receipt"))
    output = Path(_validate_text(inputs.get("output"), "output"))
    staging_root = Path(_validate_text(inputs.get("staging_root"), "staging_root"))
    staging = Path(_validate_text(inputs.get("staging"), "staging"))
    marker = Path(_validate_text(inputs.get("marker"), "marker"))
    evidence = Path(_validate_text(inputs.get("evidence"), "evidence"))
    paths = (repo, receipt, output, staging_root, staging, marker, evidence)
    if not all(path.is_absolute() for path in paths):
        raise DevCheckError("package plan paths must be absolute")
    if not repo.is_dir():
        raise DevCheckError(f"manifest_repo is not a directory: {repo}")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise DevCheckError("package output parent must be a real existing directory")
    if _path_identity(output.parent) != inputs.get("output_parent_identity"):
        raise DevCheckError("package output parent identity changed after planning")
    if _path_identity(evidence.parent) != inputs.get("evidence_parent_identity"):
        raise DevCheckError("package evidence parent identity changed after planning")
    if _is_within(output, repo) or _is_within(evidence, repo):
        raise DevCheckError(
            "package output and evidence must be outside the active manifest repository"
        )
    if _path_exists(evidence):
        raise DevCheckError(f"package evidence already exists: {evidence}")
    profile = _validate_text(inputs.get("profile"), "profile")
    required_tier = _tier(inputs.get("required_tier"), "required_tier")
    _validate_west_argv(inputs.get("west_argv"))
    if receipt == evidence:
        raise DevCheckError("check receipt and package evidence must be distinct")
    if _is_within(receipt, output) or _is_within(evidence, output):
        raise DevCheckError("receipt and package evidence must be outside package output")
    receipt_value, receipt_digest = _validate_check_receipt(
        receipt, repo, profile, required_tier
    )
    if receipt_digest != inputs.get("receipt_sha256"):
        raise DevCheckError("check receipt changed after the package plan was built")
    if receipt_value.get("transaction_id") != inputs.get("receipt_transaction_id"):
        raise DevCheckError("check receipt transaction does not match the package plan")
    if receipt_value["inputs"].get("tier") != inputs.get("receipt_tier"):
        raise DevCheckError("check receipt tier does not match the package plan")
    if receipt_value["inputs"].get("package_snapshot") != inputs.get("package_snapshot"):
        raise DevCheckError("package input snapshot does not match the check receipt")
    expected_root = output.with_name(
        f".{output.name}.west-dev-package-{plan['transaction_id']}"
    )
    if (
        staging_root != expected_root
        or staging != staging_root / "package"
        or marker != staging_root / "owner.json"
    ):
        raise DevCheckError("package transaction paths do not match transaction identity")
    if plan["steps"] != [_package_step(inputs)]:
        raise DevCheckError("package step does not match the frozen inputs")
    if _path_exists(output) or _path_exists(staging_root):
        raise DevCheckError("package output or transaction root already exists")
    return repo, receipt, output, staging_root, staging, marker, evidence


def _is_lower_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_oid(value: object, label: str) -> str:
    if not _is_lower_hex(value, 40):
        raise DevCheckError(f"{label} is not a full lowercase object ID")
    return value


def _require_digest(value: object, label: str) -> str:
    if not _is_lower_hex(value, 64):
        raise DevCheckError(f"{label} is not a lowercase SHA-256 digest")
    return value
def _validate_package_snapshot(snapshot: object, profile: str) -> dict[str, Any]:
    expected_fields = {
        "manifest_repo",
        "manifest_head",
        "manifest_tree",
        "dirty",
        "status",
        "working_tree_diff",
        "staged_diff",
        "untracked",
        "commands",
        "content",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != expected_fields:
        raise DevCheckError("check receipt package snapshot schema is invalid")
    _validate_text(snapshot.get("manifest_repo"), "snapshot manifest_repo")
    _require_oid(snapshot.get("manifest_head"), "snapshot manifest commit")
    _require_oid(snapshot.get("manifest_tree"), "snapshot manifest tree")
    if not isinstance(snapshot.get("dirty"), bool):
        raise DevCheckError("snapshot dirty field is invalid")

    def digest_size(row: object, label: str) -> None:
        if (
            not isinstance(row, dict)
            or set(row) != {"sha256", "bytes"}
            or not _is_lower_hex(row.get("sha256"), 64)
            or not isinstance(row.get("bytes"), int)
            or isinstance(row.get("bytes"), bool)
            or row["bytes"] < 0
        ):
            raise DevCheckError(f"{label} snapshot is invalid")

    for field in ("status", "working_tree_diff", "staged_diff"):
        digest_size(snapshot[field], field)
    untracked = snapshot.get("untracked")
    if (
        not isinstance(untracked, dict)
        or set(untracked) != {"sha256", "bytes", "count", "files"}
        or not _is_lower_hex(untracked.get("sha256"), 64)
        or not isinstance(untracked.get("bytes"), int)
        or isinstance(untracked.get("bytes"), bool)
        or untracked["bytes"] < 0
        or not isinstance(untracked.get("count"), int)
        or isinstance(untracked.get("count"), bool)
        or untracked["count"] < 0
        or not isinstance(untracked.get("files"), list)
        or untracked["count"] != len(untracked["files"])
    ):
        raise DevCheckError("untracked snapshot is invalid")
    seen_untracked: set[str] = set()
    for row in untracked["files"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "sha256", "bytes"}
            or _safe_package_relative(row.get("path"), "untracked snapshot path")
            in seen_untracked
            or not _is_lower_hex(row.get("sha256"), 64)
            or not isinstance(row.get("bytes"), int)
            or isinstance(row.get("bytes"), bool)
            or row["bytes"] < 0
        ):
            raise DevCheckError("untracked snapshot row is invalid")
        seen_untracked.add(row["path"])
    commands = snapshot.get("commands")
    if (
        not isinstance(commands, list)
        or not commands
        or any(
            not isinstance(command, list)
            or not command
            or any(not isinstance(argument, str) or not argument for argument in command)
            for command in commands
        )
    ):
        raise DevCheckError("snapshot commands are invalid")
    content = snapshot.get("content")
    expected_content = {
        "west.yml",
        "west.lock.yml",
        f"patches/{profile}",
        "locks/patch-stack",
    }
    if not isinstance(content, dict) or set(content) != expected_content:
        raise DevCheckError("snapshot content closure is invalid")
    for logical, row in content.items():
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "sha256", "file_count", "bytes"}
            or not isinstance(row.get("path"), str)
            or not row["path"]
            or not _is_lower_hex(row.get("sha256"), 64)
            or any(
                not isinstance(row.get(field), int)
                or isinstance(row.get(field), bool)
                or row[field] < 0
                for field in ("file_count", "bytes")
            )
        ):
            raise DevCheckError(f"snapshot content row is invalid: {logical}")
    return snapshot




def _safe_package_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DevCheckError(f"{label} is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise DevCheckError(f"{label} is not a canonical safe relative path")
    return value


def _embedded_acceptance_values(
    receipt: dict[str, Any], profile: str
) -> dict[str, dict[str, Any]]:
    artifacts = receipt.get("acceptance_artifacts")
    _validate_embedded_acceptance_artifacts(artifacts, profile)
    assert isinstance(artifacts, dict)
    result: dict[str, dict[str, Any]] = {}
    for name, row in artifacts.items():
        assert isinstance(row, dict)
        value = json.loads(row["content"])
        assert isinstance(value, dict)
        result[name] = value
    return result


def _validate_acceptance_closure(
    receipt: dict[str, Any], profile: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    scratch = receipt.get("scratch")
    if not isinstance(scratch, dict) or scratch.get("state") != "cleaned":
        raise DevCheckError("acceptance cleanup verdict is incomplete")
    values = _embedded_acceptance_values(receipt, profile)
    oracle = values["immutable_oracle"]
    lock_evidence = values["lock_first_evidence"]
    comparison = values["comparison"]
    module_map = values["module_map"]
    candidate_manifest = values["candidate_manifest"]

    if set(oracle) != {
        "oracle_schema_version",
        "mode",
        "profile",
        "profile_order",
        "batches",
        "modules",
        "generated_profile_locks",
        "frozen_manifest_sha256",
        "clean_odb",
        "cleanup",
        "verdict",
    }:
        raise DevCheckError("immutable oracle fields are invalid")
    if oracle.get("cleanup") != {
        "root": "removed",
        "worktrees": "removed",
        "refs": "removed",
    }:
        raise DevCheckError("immutable oracle cleanup is incomplete")
    clean_odb = oracle.get("clean_odb")
    if (
        not isinstance(clean_odb, dict)
        or set(clean_odb)
        != {
            "module_count",
            "immutable_fetch_transactions",
            "alternates",
            "shallow",
            "partial",
        }
        or not isinstance(clean_odb.get("module_count"), int)
        or isinstance(clean_odb.get("module_count"), bool)
        or clean_odb["module_count"] < 1
        or clean_odb.get("immutable_fetch_transactions")
        != clean_odb["module_count"]
        or any(
            clean_odb.get(name) != 0
            for name in ("alternates", "shallow", "partial")
        )
    ):
        raise DevCheckError("immutable oracle clean-ODB closure is invalid")
    batches = oracle.get("batches")
    if (
        not isinstance(batches, list)
        or not batches
        or not isinstance(batches[-1], dict)
    ):
        raise DevCheckError("immutable oracle batch closure is missing")
    if oracle.get("profile_order") != [
        batch.get("profile") if isinstance(batch, dict) else None for batch in batches
    ]:
        raise DevCheckError("immutable oracle profile order differs from batches")
    target = batches[-1]
    batch_id = _validate_text(target.get("batch_id"), "acceptance batch_id")
    expected_count = target.get("expected_count")
    module_order = target.get("module_order")
    series_order = target.get("series_order")
    oracle_series = target.get("series")
    if (
        set(target)
        != {
            "profile",
            "batch_id",
            "expected_count",
            "module_order",
            "series_order",
            "series",
            "verdict",
        }
        or target.get("profile") != profile
        or target.get("verdict") != "VALID"
        or not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 1
        or not isinstance(module_order, list)
        or not module_order
        or len(set(module_order)) != len(module_order)
        or not all(isinstance(item, str) and item for item in module_order)
        or not isinstance(series_order, list)
        or not isinstance(oracle_series, list)
        or len(series_order) != expected_count
        or len(oracle_series) != expected_count
    ):
        raise DevCheckError("immutable oracle target batch is invalid")
    observed_order: list[dict[str, str]] = []
    for row in oracle_series:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "module",
                "patch",
                "base",
                "source",
                "canonical_tree",
                "applied_commit",
                "applied_tree",
                "verdict",
            }
            or row.get("verdict") != "VALID"
        ):
            raise DevCheckError("immutable oracle series row is invalid")
        module = _validate_text(row.get("module"), "oracle module")
        patch = _validate_text(row.get("patch"), "oracle patch")
        for field in (
            "base",
            "source",
            "canonical_tree",
            "applied_commit",
            "applied_tree",
        ):
            _require_oid(row.get(field), f"oracle {module}/{patch} {field}")
        observed_order.append({"module": module, "patch": patch})
    if (
        observed_order != series_order
        or len({(row["module"], row["patch"]) for row in observed_order})
        != expected_count
        or list(dict.fromkeys(row["module"] for row in observed_order))
        != module_order
    ):
        raise DevCheckError("immutable oracle series order is invalid")

    modules = module_map.get("modules")
    oracle_modules = oracle.get("modules")
    if (
        set(module_map) != {"profile", "modules"}
        or module_map.get("profile") != profile
        or not isinstance(modules, list)
        or not isinstance(oracle_modules, list)
    ):
        raise DevCheckError("acceptance module maps are invalid")
    candidate_trees: dict[str, str] = {}
    candidate_rows: dict[str, dict[str, Any]] = {}
    for row in modules:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "module",
                "west_name",
                "path",
                "integration_profile",
                "integration_oid",
                "tree",
                "status",
            }
        ):
            raise DevCheckError("candidate module row is invalid")
        module = _validate_text(row.get("module"), "candidate module")
        for field in ("west_name", "path", "integration_profile"):
            _validate_text(row.get(field), f"candidate {module} {field}")
        if module in candidate_trees:
            raise DevCheckError("candidate module row is duplicated")
        _require_oid(row.get("integration_oid"), f"candidate {module} commit")
        candidate_trees[module] = _require_oid(
            row.get("tree"), f"candidate {module} tree"
        )
        candidate_rows[module] = row
        if row.get("status") != "":
            raise DevCheckError(f"candidate module is dirty: {module}")
    oracle_trees: dict[str, str] = {}
    for row in oracle_modules:
        if not isinstance(row, dict) or set(row) != {"module", "commit", "tree"}:
            raise DevCheckError("oracle module row is invalid")
        module = _validate_text(row.get("module"), "oracle module")
        if module in oracle_trees:
            raise DevCheckError("oracle module row is duplicated")
        _require_oid(row.get("commit"), f"oracle {module} commit")
        oracle_trees[module] = _require_oid(row.get("tree"), f"oracle {module} tree")

    inputs = receipt.get("inputs")
    snapshot = inputs.get("package_snapshot") if isinstance(inputs, dict) else None
    if not isinstance(snapshot, dict):
        raise DevCheckError("check receipt package snapshot is missing")
    manifest_commit = _require_oid(
        snapshot.get("manifest_head"), "manifest snapshot commit"
    )
    manifest_tree = _require_oid(
        snapshot.get("manifest_tree"), "manifest snapshot tree"
    )
    if (
        set(candidate_manifest)
        != {
            "workspace_commit",
            "frozen_manifest_sha256",
            "generated_profile_locks",
            "validated_nested_children",
        }
        or not isinstance(candidate_manifest.get("validated_nested_children"), dict)
    ):
        raise DevCheckError("candidate manifest fields are invalid")
    if candidate_manifest.get("workspace_commit") != manifest_commit:
        raise DevCheckError("candidate manifest commit differs from check receipt")
    content = snapshot.get("content")
    frozen = content.get("west.lock.yml") if isinstance(content, dict) else None
    if (
        not isinstance(frozen, dict)
        or candidate_manifest.get("frozen_manifest_sha256") != frozen.get("sha256")
        or oracle.get("frozen_manifest_sha256") != frozen.get("sha256")
    ):
        raise DevCheckError("acceptance frozen manifest differs from check receipt")
    generated = candidate_manifest.get("generated_profile_locks")
    oracle_generated = oracle.get("generated_profile_locks")
    if (
        not isinstance(generated, list)
        or not generated
        or not isinstance(oracle_generated, list)
        or [
            {
                key: row.get(key)
                for key in ("profile", "path", "semantic_sha256")
            }
            for row in generated
            if isinstance(row, dict)
        ]
        != oracle_generated
    ):
        raise DevCheckError("acceptance generated lock closure differs")
    seen_generated: set[str] = set()
    for row in generated:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"profile", "path", "size", "sha256", "semantic_sha256"}
            or not isinstance(row.get("profile"), str)
            or not row["profile"]
            or not isinstance(row.get("size"), int)
            or isinstance(row.get("size"), bool)
            or row["size"] <= 0
            or row["size"] > _GENERATED_LOCK_LIMIT
            or not _is_lower_hex(row.get("sha256"), 64)
            or not _is_lower_hex(row.get("semantic_sha256"), 64)
        ):
            raise DevCheckError("acceptance generated lock row is invalid")
        relative = _safe_package_relative(row.get("path"), "generated lock path")
        parts = Path(relative).parts
        if (
            relative in seen_generated
            or len(parts) < 3
            or parts[0] != "patches"
            or parts[1] != row["profile"]
            or Path(relative).name != "west.lock.yml"
        ):
            raise DevCheckError("acceptance generated lock path is invalid or duplicated")
        seen_generated.add(relative)

    crosslinks = {
        "batch_id": batch_id,
        "expected_count": expected_count,
        "module_order": module_order,
        "series_order": series_order,
    }
    for field, expected in crosslinks.items():
        if lock_evidence.get(field) != expected:
            raise DevCheckError(
                f"lock-first evidence {field} differs from immutable oracle"
            )
    if set(comparison) != {
        "evidence_schema_version",
        "verdict",
        "batch_id",
        "expected_count",
        "module_order",
        "module_count",
        "control_mode",
        "candidate_mode",
        "lock_first_evidence",
    }:
        raise DevCheckError("acceptance comparison fields are invalid")
    for field in ("batch_id", "expected_count", "module_order"):
        if comparison.get(field) != crosslinks[field]:
            raise DevCheckError(
                f"acceptance comparison {field} differs from immutable oracle"
            )
    if (
        comparison.get("evidence_schema_version") != 2
        or comparison.get("verdict") != "VALID"
        or comparison.get("control_mode") != "immutable-cherry-pick-oracle"
        or comparison.get("candidate_mode") != "default-lock-first"
        or comparison.get("lock_first_evidence") != "lock-first-evidence.json"
        or comparison.get("module_count") != len(candidate_trees)
    ):
        raise DevCheckError("acceptance comparison semantics are invalid")
    lock_series = lock_evidence.get("series")
    if not isinstance(lock_series, list) or len(lock_series) != expected_count:
        raise DevCheckError("lock-first evidence series is incomplete")
    entry_fields = {
        "module",
        "patch",
        "base",
        "source",
        "canonical_tree",
        "applied_commit",
        "applied_tree",
        "verdict",
    }
    compared_fields = entry_fields - {"applied_commit"}
    for expected, observed in zip(oracle_series, lock_series, strict=True):
        if not isinstance(observed, dict) or set(observed) != entry_fields:
            raise DevCheckError("lock-first evidence series row is invalid")
        _require_oid(
            observed.get("applied_commit"),
            f"lock-first {observed.get('module')} applied commit",
        )
        for field in compared_fields:
            if observed.get(field) != expected.get(field):
                raise DevCheckError(
                    f"lock-first evidence {field} differs from immutable oracle"
                )
    if set(candidate_trees) != set(oracle_trees):
        raise DevCheckError("candidate and immutable-oracle module sets differ")
    mismatched_trees = {
        module
        for module, tree in oracle_trees.items()
        if candidate_trees[module] != tree
    }
    if mismatched_trees:
        oracle_content = {
            row["module"]: row["applied_tree"] for row in oracle_series
        }
        candidate_content = {
            row["module"]: row["applied_tree"] for row in lock_series
        }
        validated_nested = candidate_manifest["validated_nested_children"]
        for module in mismatched_trees:
            parent = candidate_rows[module]
            parent_path = Path(parent["path"])
            children = {
                child_module: row
                for child_module, row in candidate_rows.items()
                if child_module != module
                and Path(row["path"]).is_relative_to(parent_path)
            }
            parent_evidence = validated_nested.get(parent["path"])
            evidence_valid = (
                parent["path"] in validated_nested
                and isinstance(parent_evidence, list)
            )
            if evidence_valid:
                observed_paths: set[str] = set()
                expected_status = {
                    "modified_gitlink": " M",
                    "untracked_nested_repo": "??",
                }
                for evidence_row in parent_evidence:
                    if not isinstance(evidence_row, dict) or set(evidence_row) != {
                        "xy",
                        "path",
                        "kind",
                    }:
                        evidence_valid = False
                        break
                    evidence_path = evidence_row.get("path")
                    normalized = (
                        evidence_path.rstrip("/")
                        if isinstance(evidence_path, str)
                        else ""
                    )
                    relative = Path(normalized)
                    kind = evidence_row.get("kind")
                    if (
                        not normalized
                        or relative.is_absolute()
                        or ".." in relative.parts
                        or relative.as_posix() != normalized
                        or normalized in observed_paths
                        or expected_status.get(kind) != evidence_row.get("xy")
                    ):
                        evidence_valid = False
                        break
                    observed_paths.add(normalized)
            if (
                not children
                or not evidence_valid
                or oracle_content.get(module) != candidate_content.get(module)
                or any(
                    oracle_trees.get(child_module) != child["tree"]
                    for child_module, child in children.items()
                )
            ):
                raise DevCheckError(
                    "candidate and immutable-oracle module trees differ"
                )
    return values, {
        "cleanup": "complete",
        "batch_id": batch_id,
        "expected_count": expected_count,
        "module_order": module_order,
        "series_order": series_order,
        "module_trees": candidate_trees,
        "manifest_commit": manifest_commit,
        "manifest_tree": manifest_tree,
        "frozen_manifest_sha256": frozen["sha256"],
    }


def _verify_mbox_patch_ids(
    path: Path, ordered_commits: list[str], declared_patch_ids: list[str]
) -> None:
    try:
        with path.open("rb") as stream:
            completed = subprocess.run(
                ["git", "patch-id", "--stable"],
                stdin=stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DevCheckError(f"cannot verify recovery mbox semantics: {path}: {error}") from error
    try:
        rows = [line.split() for line in completed.stdout.decode("ascii").splitlines()]
    except UnicodeDecodeError as error:
        raise DevCheckError(f"recovery mbox patch-id output is invalid: {path}") from error
    if (
        completed.returncode != 0
        or len(rows) != len(ordered_commits)
        or any(len(row) != 2 for row in rows)
        or [row[0] for row in rows] != declared_patch_ids
        or [row[1] for row in rows] != ordered_commits
    ):
        detail = completed.stderr[-_CAPTURE_LIMIT:].decode(errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise DevCheckError(f"recovery mbox patch identities differ{suffix}")



def _verify_export_evidence(
    output: Path, profile: str, receipt: dict[str, Any]
) -> tuple[dict[str, Any], set[str]]:
    if (
        output.is_symlink() and not _is_proc_fd_root(output)
    ) or not output.is_dir():
        raise DevCheckError("export-locks did not create a regular output directory")
    _values, acceptance = _validate_acceptance_closure(receipt, profile)
    evidence_path = output / "evidence.json"
    value, data = _read_json_file(evidence_path, "export-locks evidence")
    if set(value) != {
        "export_schema_version",
        "mode",
        "profile",
        "batch_id",
        "expected_count",
        "module_order",
        "series_order",
        "series",
        "clean_odb",
        "verdict",
    }:
        raise DevCheckError("export-locks evidence fields are invalid")
    if (
        value.get("export_schema_version") != 1
        or value.get("mode") != "immutable-lock-format-patch"
        or value.get("profile") != profile
        or value.get("verdict") != "VALID"
    ):
        raise DevCheckError("export-locks evidence identity is invalid")
    for field in ("batch_id", "expected_count", "module_order", "series_order"):
        if value.get(field) != acceptance[field]:
            raise DevCheckError(
                f"export-locks {field} differs from validated acceptance"
            )
    clean_odb = value.get("clean_odb")
    if (
        not isinstance(clean_odb, dict)
        or set(clean_odb)
        != {
            "module_count",
            "immutable_fetch_transactions",
            "alternates",
            "shallow",
            "partial",
        }
        or any(clean_odb.get(key) != 0 for key in ("alternates", "shallow", "partial"))
        or clean_odb.get("module_count") != len(acceptance["module_order"])
        or clean_odb.get("immutable_fetch_transactions")
        != len(acceptance["module_order"])
    ):
        raise DevCheckError("export-locks clean-ODB evidence is invalid")
    series = value.get("series")
    if not isinstance(series, list) or len(series) != acceptance["expected_count"]:
        raise DevCheckError("export-locks series closure is incomplete")
    acceptance_values = _embedded_acceptance_values(receipt, profile)
    oracle_series = acceptance_values["immutable_oracle"]["batches"][-1]["series"]
    observed_order: list[dict[str, str]] = []
    observed_mboxes: set[str] = set()
    for row, oracle_row in zip(series, oracle_series, strict=True):
        if not isinstance(row, dict) or set(row) != {
            "module",
            "patch",
            "lock",
            "base",
            "source",
            "ordered_commits",
            "commit_count",
            "resulting_tree",
            "mbox",
            "sha256",
            "stable_patch_ids",
        }:
            raise DevCheckError("export-locks evidence series row fields are invalid")
        module = _validate_text(row.get("module"), "export module")
        patch = _validate_text(row.get("patch"), "export patch")
        relative_text = _safe_package_relative(row.get("mbox"), "export mbox path")
        expected_digest = _require_digest(
            row.get("sha256"), f"export mbox {relative_text}"
        )
        identity = {"module": module, "patch": patch}
        if identity != {"module": oracle_row["module"], "patch": oracle_row["patch"]}:
            raise DevCheckError("export-locks series identity differs from acceptance")
        if (
            row.get("base") != oracle_row.get("base")
            or row.get("source") != oracle_row.get("source")
            or row.get("resulting_tree") != oracle_row.get("canonical_tree")
        ):
            raise DevCheckError(
                f"export-locks semantic identity differs: {module}/{patch}"
            )
        ordered = row.get("ordered_commits")
        patch_ids = row.get("stable_patch_ids")
        if (
            not isinstance(ordered, list)
            or not ordered
            or not all(_is_lower_hex(item, 40) for item in ordered)
            or len(set(ordered)) != len(ordered)
            or ordered[-1] != row["source"]
            or row.get("commit_count") != len(ordered)
            or not isinstance(patch_ids, list)
            or len(patch_ids) != len(ordered)
            or not all(_is_lower_hex(item, 40) for item in patch_ids)
        ):
            raise DevCheckError(
                f"export-locks commit closure is invalid: {module}/{patch}"
            )
        if Path(relative_text).parts[:1] != ("mbox",):
            raise DevCheckError("export-locks mbox is outside the canonical mbox tree")
        if relative_text in observed_mboxes:
            raise DevCheckError("export-locks mbox path is duplicated")
        observed_mboxes.add(relative_text)
        mbox = output / relative_text
        try:
            metadata = mbox.lstat()
        except FileNotFoundError as error:
            raise DevCheckError(
                f"export-locks mbox is missing: {relative_text}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise DevCheckError(
                f"export-locks mbox is not a regular file: {relative_text}"
            )
        digest, _size = _hash_file(mbox)
        if digest != expected_digest:
            raise DevCheckError(f"export-locks mbox digest mismatch: {relative_text}")
        _verify_mbox_patch_ids(mbox, ordered, patch_ids)
        observed_order.append(identity)
    if observed_order != value["series_order"]:
        raise DevCheckError("export-locks series order does not match its evidence")
    return {
        "evidence_path": "evidence.json",
        "evidence_sha256": hashlib.sha256(data).hexdigest(),
        "batch_id": value["batch_id"],
        "expected_count": value["expected_count"],
        "module_order": value["module_order"],
        "series_order": value["series_order"],
        "mboxes": sorted(observed_mboxes),
    }, {"evidence.json", *observed_mboxes}

_ACCEPTANCE_PACKAGE_PATHS = {
    "immutable_oracle": "acceptance/immutable-oracle.json",
    "lock_first_evidence": "acceptance/lock-first-evidence.json",
    "comparison": "acceptance/acceptance-result.json",
    "module_map": "acceptance/lock-first-modules.json",
    "candidate_manifest": "acceptance/lock-first-manifest.json",
}


def _scan_package_tree(package: Path) -> tuple[dict[str, Path], set[str]]:
    try:
        root_metadata = package.stat()
    except FileNotFoundError as error:
        raise DevCheckError(f"review package does not exist: {package}") from error
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise DevCheckError("review package must be a directory")
    files: dict[str, Path] = {}
    directories: set[str] = set()
    try:
        children = sorted(package.rglob("*"), key=lambda item: item.as_posix())
    except OSError as error:
        raise DevCheckError(f"cannot enumerate review package: {error}") from error
    for child in children:
        relative = child.relative_to(package).as_posix()
        metadata = child.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise DevCheckError(f"review package contains a symlink: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(relative)
        elif stat.S_ISREG(metadata.st_mode):
            files[relative] = child
        else:
            raise DevCheckError(f"review package contains a special file: {relative}")
    return files, directories


def _implied_directories(files: set[str]) -> set[str]:
    result: set[str] = set()
    for name in files:
        parent = Path(name).parent
        while parent != Path("."):
            result.add(parent.as_posix())
            parent = parent.parent
    return result


def _assert_exact_package_tree(package: Path, expected_files: set[str]) -> None:
    files, directories = _scan_package_tree(package)
    observed_files = set(files)
    missing = sorted(expected_files - observed_files)
    extra = sorted(observed_files - expected_files)
    if missing:
        raise DevCheckError(f"review package files are missing: {', '.join(missing)}")
    if extra:
        raise DevCheckError(f"review package has extra files: {', '.join(extra)}")
    expected_directories = _implied_directories(expected_files)
    extra_directories = sorted(directories - expected_directories)
    if extra_directories:
        raise DevCheckError(
            f"review package has extra directories: {', '.join(extra_directories)}"
        )


def _capture_binding(result: dict[str, Any], stream: str) -> dict[str, Any]:
    capture = result[stream]
    return {
        "sha256": capture["sha256"],
        "bytes": capture["bytes"],
        "truncated": capture["truncated"],
        "tail": capture["tail"],
    }


def _validate_packaged_check_receipt(
    receipt: dict[str, Any], profile: str
) -> dict[str, Any]:
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("operation") != "check"
        or receipt.get("state") != "committed"
        or receipt.get("returncode") != 0
    ):
        raise DevCheckError("packaged check receipt identity is invalid")
    transaction_id = receipt.get("transaction_id")
    if not _is_lower_hex(transaction_id, 32):
        raise DevCheckError("packaged check receipt transaction_id is invalid")
    inputs = receipt.get("inputs")
    if not isinstance(inputs, dict):
        raise DevCheckError("packaged check receipt inputs are missing")
    if (
        inputs.get("profile") != profile
        or inputs.get("tier") != "acceptance"
        or inputs.get("bead") is not None
        or inputs.get("patch") is not None
    ):
        raise DevCheckError("packaged check receipt scope is invalid")
    _validate_package_snapshot(inputs.get("package_snapshot"), profile)
    try:
        _validate_acceptance_inputs(inputs)
        expected_steps = _check_steps(inputs, transaction_id)
    except (KeyError, TypeError, DevCheckError) as error:
        raise DevCheckError(f"packaged check receipt inputs are invalid: {error}") from error
    if receipt.get("steps") != expected_steps:
        raise DevCheckError("packaged check receipt steps differ from frozen inputs")
    results = receipt.get("results")
    if not isinstance(results, list) or len(results) != len(expected_steps):
        raise DevCheckError("packaged check receipt results are incomplete")
    for planned, result in zip(expected_steps, results, strict=True):
        if (
            not isinstance(result, dict)
            or any(result.get(key) != value for key, value in planned.items())
            or result.get("returncode") != 0
            or result.get("timed_out") is not False
            or result.get("interrupted") is not False
            or result.get("process_group_quiescent") is not True
        ):
            raise DevCheckError("packaged check result differs from its planned step")
        for stream in ("stdout", "stderr"):
            capture = result.get(stream)
            if not isinstance(capture, dict):
                raise DevCheckError("packaged check result capture is missing")
            tail = capture.get("tail")
            if (
                not _is_lower_hex(capture.get("sha256"), 64)
                or not isinstance(capture.get("bytes"), int)
                or isinstance(capture.get("bytes"), bool)
                or capture["bytes"] < 0
                or not isinstance(capture.get("truncated"), bool)
                or not isinstance(tail, str)
                or len(tail.encode("utf-8")) > _CAPTURE_LIMIT
                or result.get(f"{stream}_tail") != tail
            ):
                raise DevCheckError("packaged check result capture is invalid")
    _validate_checkpoint_receipt(receipt, inputs, results)
    _validate_acceptance_closure(receipt, profile)
    return inputs


def _parallel_file_hashes(
    files: dict[str, Path],
) -> dict[str, tuple[str, int]]:
    ordered = sorted(files.items())
    if len(ordered) <= 1:
        return {
            relative: _hash_file(path)
            for relative, path in ordered
        }
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_PACKAGE_HASH_WORKERS, len(ordered))
    ) as executor:
        hashes = executor.map(
            _hash_file, (path for _relative, path in ordered)
        )
        return {
            relative: binding
            for (relative, _path), binding in zip(
                ordered, hashes, strict=True
            )
        }


def _package_file_rows(package: Path) -> list[dict[str, Any]]:
    files, _directories = _scan_package_tree(package)
    selected = {
        relative: path
        for relative, path in files.items()
        if relative not in {"package-index.json", "SHA256SUMS"}
    }
    return [
        {"path": relative, "sha256": digest, "bytes": size}
        for relative, (digest, size) in _parallel_file_hashes(selected).items()
    ]


def _proc_fds_for_subprocess(*values: object) -> tuple[int, ...]:
    marker = "/proc/self/fd/"
    descriptors: set[int] = set()
    for value in values:
        text = str(value)
        if text.startswith(marker):
            component = text[len(marker) :].split("/", 1)[0]
            if component.isdigit():
                descriptors.add(int(component))
    return tuple(sorted(descriptors))


def _run_git(argv: list[str], cwd: Path) -> str:
    inherited_fds = _proc_fds_for_subprocess(cwd, *argv)
    completed = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
        pass_fds=inherited_fds,
    )
    if completed.returncode:
        raise DevCheckError(
            f"git {' '.join(argv)} failed ({completed.returncode}): "
            f"{completed.stderr[-_CAPTURE_LIMIT:].strip()}"
        )
    return completed.stdout.strip()


def _create_bundle(source: Path, destination: Path, refs: dict[str, str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="west-dev-package-bundle-") as temporary:
        bare = Path(temporary) / "objects.git"
        _run_git(["init", "--bare", "-q", str(bare)], Path(temporary))
        for ref, oid in refs.items():
            _run_git(
                ["fetch", "--no-tags", str(source), f"{oid}:{ref}"],
                bare,
            )
        temporary_bundle = Path(temporary) / "package.bundle"
        _run_git(
            ["bundle", "create", str(temporary_bundle), *refs],
            bare,
        )
        try:
            with temporary_bundle.open("rb") as source_stream:
                with destination.open("xb") as destination_stream:
                    shutil.copyfileobj(
                        source_stream, destination_stream, length=1024 * 1024
                    )
                    destination_stream.flush()
                    os.fsync(destination_stream.fileno())
        except FileExistsError as error:
            raise DevCheckError(
                f"exporter created reserved package path: {destination}"
            ) from error


def _snapshot_matches(path: Path, expected: dict[str, Any]) -> bool:
    observed = _content_snapshot(path)
    return all(
        observed.get(field) == expected.get(field)
        for field in ("sha256", "file_count", "bytes")
    )


def _load_packaged_mapping(
    package: Path, profile: str
) -> tuple[Path, dict[str, Any]]:
    registry = (
        package / "source" / "locks" / "lock-first-profiles-v1.yml"
    ).resolve(strict=True)
    try:
        mapping = patch_stack_lock_first.mapping_for_profile(
            profile, registry_path=registry
        )
        return mapping, patch_stack_lock_first.load_mapping(mapping, profile)
    except patch_stack_lock_first.LockFirstError as error:
        raise DevCheckError(f"packaged lock mapping is invalid: {error}") from error


def _validate_export_locks(
    package: Path, profile: str, export_evidence: dict[str, Any]
) -> list[dict[str, Any]]:
    mapping_path, mapping = _load_packaged_mapping(package, profile)
    mapped = mapping["series"]
    exported = export_evidence["series"]
    if len(mapped) != len(exported):
        raise DevCheckError("packaged mapping/export count differs")
    bindings = []
    for ordinal, (expected, observed) in enumerate(
        zip(mapped, exported, strict=True)
    ):
        if (
            observed.get("module") != expected.get("module")
            or observed.get("patch") != expected.get("patch")
        ):
            raise DevCheckError("packaged mapping/export identity differs")
        lock_name = _safe_package_relative(expected.get("lock"), "mapping lock")
        if observed.get("lock") != lock_name:
            raise DevCheckError("exported lock field differs from packaged mapping")
        lock_path = mapping_path.parent / lock_name
        try:
            lock = patch_stack_materialize.load_lock(lock_path)
        except (OSError, ValueError, patch_stack_materialize.MaterializeError) as error:
            raise DevCheckError(f"packaged immutable lock is invalid: {lock_name}: {error}") from error
        if (
            lock["upstream"]["base_commit"] != observed.get("base")
            or lock["mirror"]["base_oid"] != observed.get("base")
            or lock["source_commit"] != observed.get("source")
            or lock["mirror"]["source_oid"] != observed.get("source")
            or lock["ordered_commits"] != observed.get("ordered_commits")
            or lock["expected_tree"] != observed.get("resulting_tree")
        ):
            raise DevCheckError(
                f"exported lock semantics differ from packaged lock: {lock_name}"
            )
        bindings.append(
            {
                "ordinal": ordinal,
                "module": observed["module"],
                "patch": observed["patch"],
                "lock": lock_name,
                "base": observed["base"],
                "source": observed["source"],
                "resulting_tree": observed["resulting_tree"],
            }
        )
    return bindings


def _source_paths(profile: str) -> dict[str, str]:
    return {
        "west.yml": "source/manifest/west.yml",
        "west.lock.yml": "source/manifest/west.lock.yml",
        f"patches/{profile}": f"source/patches/{profile}",
        "locks/patch-stack": "source/locks",
    }


def _build_source_closure(
    manifest_repo: Path,
    package: Path,
    receipt: dict[str, Any],
    export_evidence: dict[str, Any],
) -> dict[str, Any]:
    profile = receipt["inputs"]["profile"]
    snapshot = receipt["inputs"]["package_snapshot"]
    paths = _source_paths(profile)
    for logical, relative in paths.items():
        source = manifest_repo / logical
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        if not _snapshot_matches(destination, snapshot["content"][logical]):
            raise DevCheckError(f"packaged source bytes differ from snapshot: {logical}")

    embedded_locks = receipt["acceptance_artifacts"]["candidate_manifest"][
        "generated_locks"
    ]
    declared_generated = json.loads(
        receipt["acceptance_artifacts"]["candidate_manifest"]["content"]
    )["generated_profile_locks"]
    projected_generated = [
        {
            field: row[field]
            for field in ("profile", "path", "size", "sha256", "semantic_sha256")
        }
        for row in embedded_locks
    ]
    if projected_generated != declared_generated:
        raise DevCheckError(
            "generated locks differ between candidate manifest and receipt closure"
        )
    generated_rows = []
    for row in embedded_locks:
        relative = f"source/generated/{row['path']}"
        source_copy = f"source/candidate/{row['path']}"
        embedded_data = row["content"].encode("utf-8")
        _write_package_payload(package / source_copy, embedded_data)
        _write_package_payload(package / relative, embedded_data)
        generated_rows.append(
            {
                "profile": row["profile"],
                "path": relative,
                "source_path": row["path"],
                "size": row["size"],
                "sha256": row["sha256"],
                "semantic_sha256": row["semantic_sha256"],
                "source_copy": source_copy,
            }
        )

    manifest_bundle = "bundles/manifest.bundle"
    manifest_ref = "refs/package/manifest"
    bundle_tasks = [
        (
            manifest_repo,
            package / manifest_bundle,
            {manifest_ref: snapshot["manifest_head"]},
        )
    ]
    acceptance_values = _embedded_acceptance_values(receipt, profile)
    module_rows = {
        row["module"]: row for row in acceptance_values["module_map"]["modules"]
    }
    module_bundles = []
    series_by_module: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for ordinal, row in enumerate(export_evidence["series"]):
        series_by_module.setdefault(row["module"], []).append((ordinal, row))
    for module_index, module in enumerate(export_evidence["module_order"]):
        module_row = module_rows[module]
        source_repo = manifest_repo.parent / module_row["path"]
        refs: dict[str, str] = {}
        series_refs = []
        for ordinal, row in series_by_module[module]:
            base_ref = f"refs/package/series/{ordinal}/base"
            source_ref = f"refs/package/series/{ordinal}/source"
            refs[base_ref] = row["base"]
            refs[source_ref] = row["source"]
            series_refs.append(
                {
                    "ordinal": ordinal,
                    "module": module,
                    "patch": row["patch"],
                    "base_ref": base_ref,
                    "source_ref": source_ref,
                }
            )
        relative = f"bundles/modules/{module_index:04d}.bundle"
        bundle_tasks.append((source_repo, package / relative, refs))
        module_bundles.append(
            {
                "module": module,
                "path": relative,
                "candidate_integration_commit": module_row["integration_oid"],
                "candidate_integration_tree": module_row["tree"],
                "candidate_object_authority": "validated-acceptance-receipt",
                "series": series_refs,
            }
        )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_PACKAGE_HASH_WORKERS, len(bundle_tasks))
    ) as pool:
        futures = [
            pool.submit(_create_bundle, source, destination, refs)
            for source, destination, refs in bundle_tasks
        ]
        for future in futures:
            future.result()
    lock_bindings = _validate_export_locks(package, profile, export_evidence)
    return {
        "paths": [
            {
                "logical": logical,
                "path": relative,
                **{
                    field: snapshot["content"][logical][field]
                    for field in ("sha256", "file_count", "bytes")
                },
            }
            for logical, relative in paths.items()
        ],
        "generated_locks": generated_rows,
        "manifest_bundle": {
            "path": manifest_bundle,
            "ref": manifest_ref,
            "commit": snapshot["manifest_head"],
            "tree": snapshot["manifest_tree"],
        },
        "module_bundles": module_bundles,
        "lock_bindings": lock_bindings,
    }




def _stable_patch_ids_for_commits(repo: Path, commits: list[str]) -> list[str]:
    identities: list[str] = []
    for commit in commits:
        formatted = subprocess.run(
            [
                "git",
                "format-patch",
                "--stdout",
                "--no-stat",
                "--full-index",
                f"{commit}^!",
            ],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if formatted.returncode:
            raise DevCheckError(
                "cannot derive bundled patch identity: "
                + formatted.stderr[-_CAPTURE_LIMIT:].decode(errors="replace").strip()
            )
        identified = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=formatted.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        fields = identified.stdout.decode(errors="replace").split()
        if (
            identified.returncode
            or len(fields) != 2
            or not _is_lower_hex(fields[0], 40)
            or fields[1] != commit
        ):
            raise DevCheckError("cannot derive bundled stable patch identity")
        identities.append(fields[0])
    return identities


def _bundle_repository(
    bundle: Path, destination: Path, expected_refs: dict[str, str]
) -> Path:
    bare = destination / "objects.git"
    _run_git(["init", "--bare", "-q", str(bare)], destination)
    try:
        _run_git(["bundle", "verify", str(bundle)], bare)
        heads = _run_git(["bundle", "list-heads", str(bundle)], bare).splitlines()
        observed_refs: dict[str, str] = {}
        for row in heads:
            fields = row.split()
            if (
                len(fields) != 2
                or not _is_lower_hex(fields[0], 40)
                or not fields[1].startswith("refs/package/")
                or fields[1] in observed_refs
            ):
                raise DevCheckError("bundle heads are invalid")
            observed_refs[fields[1]] = fields[0]
        if observed_refs != expected_refs:
            raise DevCheckError("bundle refs differ from closure")
        _run_git(
            [
                "fetch",
                "--no-tags",
                str(bundle),
                *(f"{ref}:{ref}" for ref in expected_refs),
            ],
            bare,
        )
    except DevCheckError as error:
        raise DevCheckError(f"package Git bundle is invalid: {error}") from error
    return bare


def _verify_source_closure(
    package: Path,
    receipt: dict[str, Any],
    recorded: object,
    export_evidence: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(recorded, dict):
        raise DevCheckError("package source closure is missing")
    profile = receipt["inputs"]["profile"]
    snapshot = receipt["inputs"]["package_snapshot"]
    paths = _source_paths(profile)
    path_rows = []
    for logical, relative in paths.items():
        expected = snapshot["content"][logical]
        if not _snapshot_matches(package / relative, expected):
            raise DevCheckError(f"packaged source bytes differ from snapshot: {logical}")
        path_rows.append(
            {
                "logical": logical,
                "path": relative,
                **{
                    field: expected[field]
                    for field in ("sha256", "file_count", "bytes")
                },
            }
        )

    embedded = receipt["acceptance_artifacts"]["candidate_manifest"].get(
        "generated_locks"
    )
    if not isinstance(embedded, list) or not embedded:
        raise DevCheckError("packaged generated lock closure is empty")
    declared_generated = json.loads(
        receipt["acceptance_artifacts"]["candidate_manifest"]["content"]
    )["generated_profile_locks"]
    projected_generated = [
        {
            field: row[field]
            for field in ("profile", "path", "size", "sha256", "semantic_sha256")
        }
        for row in embedded
        if isinstance(row, dict)
    ]
    if projected_generated != declared_generated:
        raise DevCheckError(
            "generated locks differ between candidate manifest and receipt closure"
        )
    generated_rows = []
    seen_generated: set[str] = set()
    for row in embedded:
        if not isinstance(row, dict) or set(row) != {
            "profile",
            "path",
            "size",
            "sha256",
            "semantic_sha256",
            "content",
        }:
            raise DevCheckError("packaged generated lock row is invalid")
        source_path = _safe_package_relative(row.get("path"), "generated lock path")
        relative = f"source/generated/{source_path}"
        source_copy = f"source/candidate/{source_path}"
        if relative in seen_generated:
            raise DevCheckError("packaged generated lock path is duplicated")
        seen_generated.add(relative)
        data = (package / relative).read_bytes()
        source_data = (package / source_copy).read_bytes()
        if (
            len(data) > _GENERATED_LOCK_LIMIT
            or len(data) != row.get("size")
            or hashlib.sha256(data).hexdigest() != row.get("sha256")
            or not _is_lower_hex(row.get("semantic_sha256"), 64)
            or data != row.get("content", "").encode("utf-8")
            or source_data != data
        ):
            raise DevCheckError(f"packaged generated lock differs: {source_path}")
        generated_rows.append(
            {
                "profile": row["profile"],
                "path": relative,
                "source_path": source_path,
                "size": row["size"],
                "sha256": row["sha256"],
                "semantic_sha256": row["semantic_sha256"],
                "source_copy": source_copy,
            }
        )

    manifest_bundle = {
        "path": "bundles/manifest.bundle",
        "ref": "refs/package/manifest",
        "commit": snapshot["manifest_head"],
        "tree": snapshot["manifest_tree"],
    }
    with tempfile.TemporaryDirectory(prefix="west-dev-verify-manifest-") as temporary:
        bare = _bundle_repository(
            package / manifest_bundle["path"],
            Path(temporary),
            {manifest_bundle["ref"]: manifest_bundle["commit"]},
        )
        commit = _run_git(
            ["rev-parse", f"{manifest_bundle['ref']}^{{commit}}"], bare
        )
        tree = _run_git(["rev-parse", f"{commit}^{{tree}}"], bare)
        if commit != manifest_bundle["commit"] or tree != manifest_bundle["tree"]:
            raise DevCheckError("manifest bundle commit/tree differs from receipt")
        manifest_work = Path(temporary) / "manifest"
        _run_git(["init", "-q", str(manifest_work)], Path(temporary))
        _run_git(
            [
                "fetch",
                "--no-tags",
                str(bare),
                f"{manifest_bundle['ref']}:refs/package/manifest",
            ],
            manifest_work,
        )
        _run_git(["checkout", "-q", "--detach", commit], manifest_work)
        for logical in paths:
            if not _snapshot_matches(
                manifest_work / logical, snapshot["content"][logical]
            ):
                raise DevCheckError(
                    f"committed source bytes differ from manifest bundle: {logical}"
                )

    acceptance_values = _embedded_acceptance_values(receipt, profile)
    module_rows = {
        row["module"]: row for row in acceptance_values["module_map"]["modules"]
    }
    series_by_module: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for ordinal, row in enumerate(export_evidence["series"]):
        series_by_module.setdefault(row["module"], []).append((ordinal, row))
    module_bundles = []
    for module_index, module in enumerate(export_evidence["module_order"]):
        module_row = module_rows[module]
        relative = f"bundles/modules/{module_index:04d}.bundle"
        series_refs = []
        expected_bundle_refs: dict[str, str] = {}
        for ordinal, row in series_by_module[module]:
            expected_bundle_refs[f"refs/package/series/{ordinal}/base"] = row["base"]
            expected_bundle_refs[f"refs/package/series/{ordinal}/source"] = row[
                "source"
            ]
        with tempfile.TemporaryDirectory(
            prefix="west-dev-verify-module-"
        ) as temporary:
            bare = _bundle_repository(
                package / relative, Path(temporary), expected_bundle_refs
            )
            for ordinal, row in series_by_module[module]:
                base_ref = f"refs/package/series/{ordinal}/base"
                source_ref = f"refs/package/series/{ordinal}/source"
                if (
                    _run_git(["rev-parse", f"{base_ref}^{{commit}}"], bare)
                    != row["base"]
                    or _run_git(["rev-parse", f"{source_ref}^{{commit}}"], bare)
                    != row["source"]
                ):
                    raise DevCheckError(f"module bundle series differs: {module}")
                derived_order = _run_git(
                    ["rev-list", "--reverse", f"{base_ref}..{source_ref}"], bare
                ).splitlines()
                if derived_order != row["ordered_commits"]:
                    raise DevCheckError(
                        f"bundled ordered commits differ: {module}/{row['patch']}"
                    )
                if (
                    _stable_patch_ids_for_commits(bare, derived_order)
                    != row["stable_patch_ids"]
                ):
                    raise DevCheckError(
                        f"bundled stable patch IDs differ: {module}/{row['patch']}"
                    )
                series_refs.append(
                    {
                        "ordinal": ordinal,
                        "module": module,
                        "patch": row["patch"],
                        "base_ref": base_ref,
                        "source_ref": source_ref,
                    }
                )
                work = Path(temporary) / f"replay-{ordinal}"
                _run_git(["init", "-q", str(work)], Path(temporary))
                _run_git(
                    [
                        "fetch",
                        "--no-tags",
                        str(bare),
                        f"{base_ref}:refs/package/replay/base",
                        f"{source_ref}:refs/package/replay/source",
                    ],
                    work,
                )
                _run_git(["checkout", "-q", "--detach", row["base"]], work)
                _run_git(["config", "user.name", "West Dev Package Verify"], work)
                _run_git(
                    ["config", "user.email", "west-dev-package@example.invalid"],
                    work,
                )
                _run_git(
                    [
                        "am",
                        "--3way",
                        "--committer-date-is-author-date",
                        str(package / row["mbox"]),
                    ],
                    work,
                )
                if _run_git(["rev-parse", "HEAD^{tree}"], work) != row["resulting_tree"]:
                    raise DevCheckError(
                        f"recovery mbox replay tree differs: {module}/{row['patch']}"
                    )
        module_bundles.append(
            {
                "module": module,
                "path": relative,
                "candidate_integration_commit": module_row["integration_oid"],
                "candidate_integration_tree": module_row["tree"],
                "candidate_object_authority": "validated-acceptance-receipt",
                "series": series_refs,
            }
        )
    derived = {
        "paths": path_rows,
        "generated_locks": generated_rows,
        "manifest_bundle": manifest_bundle,
        "module_bundles": module_bundles,
        "lock_bindings": _validate_export_locks(
            package, profile, export_evidence
        ),
    }
    if recorded != derived:
        raise DevCheckError("package source closure index differs from packaged bytes")
    return derived
def _derived_package_index(
    package: Path,
    receipt: dict[str, Any],
    receipt_digest: str,
    export: dict[str, Any],
    source_closure: dict[str, Any],
) -> dict[str, Any]:
    inputs = receipt["inputs"]
    profile = inputs["profile"]
    snapshot = inputs["package_snapshot"]
    _validate_package_snapshot(snapshot, profile)
    content = snapshot.get("content")
    if not isinstance(content, dict) or set(content) != {
        "west.yml",
        "west.lock.yml",
        f"patches/{profile}",
        "locks/patch-stack",
    }:
        raise DevCheckError("check receipt source snapshot closure is invalid")
    _values, acceptance = _validate_acceptance_closure(receipt, profile)
    artifact_rows = []
    embedded = receipt["acceptance_artifacts"]
    for name, relative in _ACCEPTANCE_PACKAGE_PATHS.items():
        row = embedded[name]
        artifact_rows.append(
            {
                "name": name,
                "path": relative,
                "sha256": row["sha256"],
                "bytes": row["bytes"],
            }
        )
    checks = [
        {
            "name": result["name"],
            "returncode": result["returncode"],
            "stdout": _capture_binding(result, "stdout"),
            "stderr": _capture_binding(result, "stderr"),
        }
        for result in receipt["results"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "review-package",
        "profile": profile,
        "check_receipt": {
            "path": "check-receipt.json",
            "sha256": receipt_digest,
            "transaction_id": receipt["transaction_id"],
            "tier": inputs["tier"],
        },
        "manifest": {
            "commit": snapshot["manifest_head"],
            "tree": snapshot["manifest_tree"],
            "snapshot": snapshot,
        },
        "source_closure": source_closure,
        "checks": checks,
        "acceptance": {
            **acceptance,
            "artifacts": artifact_rows,
        },
        "export": export,
        "integrity_boundary": {
            "mode": "local-owner-mutable",
            "publication": (
                "content-verified-before-rename-and-identity-verified-after"
            ),
            "consumer_requirement": "verify-package immediately before use",
            "verify_operation": "package-verify",
        },
        "files": _package_file_rows(package),
    }


def _write_package_payload(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise DevCheckError(f"exporter created reserved package path: {path}") from error


def _complete_review_package(
    manifest_repo: Path,
    package: Path,
    receipt_path: Path,
    receipt: dict[str, Any],
    receipt_digest: str,
    export: dict[str, Any],
    export_files: set[str],
) -> dict[str, Any]:
    _assert_exact_package_tree(package, export_files)
    receipt_data = receipt_path.read_bytes()
    if hashlib.sha256(receipt_data).hexdigest() != receipt_digest:
        raise DevCheckError("check receipt changed while completing review package")
    _write_package_payload(package / "check-receipt.json", receipt_data)
    for name, relative in _ACCEPTANCE_PACKAGE_PATHS.items():
        data = receipt["acceptance_artifacts"][name]["content"].encode("utf-8")
        _write_package_payload(package / relative, data)
    export_evidence, _data = _read_json_file(
        package / "evidence.json", "export-locks evidence"
    )
    source_closure = _build_source_closure(
        manifest_repo, package, receipt, export_evidence
    )
    index = _derived_package_index(
        package, receipt, receipt_digest, export, source_closure
    )
    _write_package_payload(package / "package-index.json", _json_bytes(index))
    files, _directories = _scan_package_tree(package)
    sums = [
        f"{digest}  {relative}\n"
        for relative, (digest, _size) in _parallel_file_hashes(files).items()
    ]
    _write_package_payload(
        package / "SHA256SUMS", "".join(sums).encode("utf-8")
    )
    return index


def _parse_sha256sums(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DevCheckError(f"cannot read SHA256SUMS: {error}") from error
    result: dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    if not lines or any(not line.endswith("\n") for line in lines):
        raise DevCheckError("SHA256SUMS must contain newline-terminated entries")
    for line in lines:
        fields = line[:-1].split("  ", 1)
        if len(fields) != 2:
            raise DevCheckError("SHA256SUMS entry is malformed")
        digest = _require_digest(fields[0], "SHA256SUMS digest")
        relative = _safe_package_relative(fields[1], "SHA256SUMS path")
        if relative == "SHA256SUMS" or relative in result:
            raise DevCheckError(
                "SHA256SUMS inventory is duplicated or recursive"
            )
        result[relative] = digest
    if list(result) != sorted(result):
        raise DevCheckError("SHA256SUMS inventory is not sorted")
    return result


def _verify_owned_package_integrity(
    package: Path, expected_index: dict[str, Any]
) -> dict[str, Any]:
    """Verify bytes written by this process without replaying accepted semantics."""

    rows = expected_index["files"]
    listed = {row["path"]: row for row in rows}
    expected_files = set(listed) | {"package-index.json", "SHA256SUMS"}
    _assert_exact_package_tree(package, expected_files)
    index, index_data = _read_json_file(
        package / "package-index.json", "owned review package index"
    )
    if index != expected_index:
        raise DevCheckError("owned review package index changed after construction")
    sums = _parse_sha256sums(package / "SHA256SUMS")
    if set(sums) != expected_files - {"SHA256SUMS"}:
        raise DevCheckError("owned package SHA256SUMS allowlist differs")
    hashes = _parallel_file_hashes(
        {
            relative: package / relative
            for relative in expected_files - {"SHA256SUMS"}
        }
    )
    for relative, (digest, size) in hashes.items():
        if sums[relative] != digest:
            raise DevCheckError(f"owned package digest mismatch: {relative}")
        if relative in listed and (
            listed[relative]["sha256"] != digest
            or listed[relative]["bytes"] != size
        ):
            raise DevCheckError(f"owned package index mismatch: {relative}")
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "package-integrity",
        "state": "valid",
        "returncode": 0,
        "package": str(package),
        "profile": expected_index["profile"],
        "file_count": len(expected_files),
        "package_index_sha256": hashlib.sha256(index_data).hexdigest(),
        "package_index": index,
    }


def verify_package(package: Path) -> dict[str, Any]:
    """Validate a published review package without consulting or mutating a workspace."""
    root = Path(package).expanduser().absolute()
    if root.is_symlink() and not _is_proc_fd_root(root):
        raise DevCheckError("review package must be a regular non-symlink directory")
    files, _directories = _scan_package_tree(root)
    if "package-index.json" not in files or "SHA256SUMS" not in files:
        raise DevCheckError("review package index or SHA256SUMS is missing")
    index, index_data = _read_json_file(
        root / "package-index.json", "review package index"
    )
    if (
        index.get("schema_version") != SCHEMA_VERSION
        or index.get("operation") != "review-package"
        or not isinstance(index.get("profile"), str)
    ):
        raise DevCheckError("review package index identity is invalid")
    rows = index.get("files")
    if not isinstance(rows, list):
        raise DevCheckError("review package inventory is missing")
    listed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise DevCheckError("review package inventory row is invalid")
        relative = _safe_package_relative(row.get("path"), "package inventory path")
        if relative in {"package-index.json", "SHA256SUMS"} or relative in listed:
            raise DevCheckError("review package inventory path is reserved or duplicated")
        _require_digest(row.get("sha256"), f"package inventory {relative}")
        if (
            not isinstance(row.get("bytes"), int)
            or isinstance(row.get("bytes"), bool)
            or row["bytes"] < 0
        ):
            raise DevCheckError(f"review package inventory size is invalid: {relative}")
        listed[relative] = row
    if list(listed) != sorted(listed):
        raise DevCheckError("review package inventory is not sorted")
    expected_files = set(listed) | {"package-index.json", "SHA256SUMS"}
    _assert_exact_package_tree(root, expected_files)
    sums = _parse_sha256sums(root / "SHA256SUMS")
    if set(sums) != expected_files - {"SHA256SUMS"}:
        raise DevCheckError("SHA256SUMS allowlist differs from package inventory")
    hashes = _parallel_file_hashes(
        {
            relative: root / relative
            for relative in expected_files - {"SHA256SUMS"}
        }
    )
    for relative, (digest, size) in hashes.items():
        if sums[relative] != digest:
            raise DevCheckError(f"SHA256SUMS digest mismatch: {relative}")
        if relative in listed and (
            listed[relative]["sha256"] != digest
            or listed[relative]["bytes"] != size
        ):
            raise DevCheckError(f"package index digest mismatch: {relative}")
    receipt, receipt_data = _read_json_file(
        root / "check-receipt.json", "packaged check receipt"
    )
    profile = index["profile"]
    _validate_packaged_check_receipt(receipt, profile)
    receipt_digest = hashlib.sha256(receipt_data).hexdigest()
    export, _export_files = _verify_export_evidence(root, profile, receipt)
    export_evidence, _export_data = _read_json_file(
        root / "evidence.json", "export-locks evidence"
    )
    source_closure = _verify_source_closure(
        root, receipt, index.get("source_closure"), export_evidence
    )
    for name, relative in _ACCEPTANCE_PACKAGE_PATHS.items():
        row = receipt["acceptance_artifacts"][name]
        data = (root / relative).read_bytes()
        if (
            len(data) != row["bytes"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
            or data != row["content"].encode("utf-8")
        ):
            raise DevCheckError(f"packaged acceptance artifact differs: {name}")
    derived = _derived_package_index(
        root, receipt, receipt_digest, export, source_closure
    )
    if index != derived:
        raise DevCheckError("review package index bindings do not match package closure")
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "package-verify",
        "state": "valid",
        "returncode": 0,
        "package": str(root),
        "profile": profile,
        "file_count": len(expected_files),
        "package_index_sha256": hashlib.sha256(index_data).hexdigest(),
        "package_index": index,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_package_tree(package: Path) -> None:
    files, directories = _scan_package_tree(package)
    for path in files.values():
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for relative in sorted(
        directories, key=lambda value: len(Path(value).parts), reverse=True
    ):
        _fsync_directory(package / relative)
    _fsync_directory(package)


def _package_tree_identity_snapshot(
    package: Path,
) -> dict[str, dict[str, tuple[int, ...]]]:
    files, directories = _scan_package_tree(package)

    def binding(path: Path) -> tuple[int, ...]:
        metadata = path.lstat()
        return (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    return {
        "files": {
            relative: binding(path)
            for relative, path in sorted(files.items())
        },
        "directories": {
            relative: binding(package / relative)
            for relative in sorted(directories)
        },
    }


def _proc_fd_path(directory_fd: int, name: str) -> Path:
    return Path(f"/proc/self/fd/{directory_fd}") / name


def _create_package_marker(
    plan: dict[str, Any],
    staging_root: Path,
    marker: Path,
    output_parent_fd: int,
) -> tuple[dict[str, int], int]:
    root_fd: int | None = None
    marker_identity: dict[str, int] | None = None
    try:
        os.mkdir(staging_root.name, mode=0o700, dir_fd=output_parent_fd)
        root_fd = os.open(
            staging_root.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=output_parent_fd,
        )
        root_identity = _directory_identity(root_fd)
        payload = {
            "transaction_id": plan["transaction_id"],
            "staging_root": plan["inputs"]["staging_root"],
            "staging": plan["inputs"]["staging"],
            "output_parent_identity": plan["inputs"]["output_parent_identity"],
        }
        descriptor = os.open(
            marker.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
            dir_fd=root_fd,
        )
        marker_identity = _identity_from_stat(os.fstat(descriptor))
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(root_fd)
        os.fsync(output_parent_fd)
        return root_identity, root_fd
    except BaseException:
        if root_fd is not None:
            if marker_identity is not None:
                try:
                    if _identity_at(root_fd, marker.name) == marker_identity:
                        os.unlink(marker.name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
            os.close(root_fd)
        try:
            os.rmdir(staging_root.name, dir_fd=output_parent_fd)
        except OSError:
            # An unexpected child is not transaction-owned and must survive.
            pass
        raise


def _package_marker_matches(
    plan: dict[str, Any],
    root_fd: int,
    marker: Path,
    root_identity: dict[str, int],
) -> bool:
    if _directory_identity(root_fd) != root_identity:
        return False
    try:
        metadata = os.stat(marker.name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return False
        descriptor = os.open(
            marker.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=root_fd
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return False
    return value == {
        "transaction_id": plan["transaction_id"],
        "staging_root": plan["inputs"]["staging_root"],
        "staging": plan["inputs"]["staging"],
        "output_parent_identity": plan["inputs"]["output_parent_identity"],
    }


def _cleanup_package_staging(
    plan: dict[str, Any],
    output_parent_fd: int,
    root_fd: int,
    staging_root: Path,
    staging: Path,
    marker: Path,
    root_identity: dict[str, int],
    staging_identity: dict[str, int] | None,
    staging_owned: bool,
) -> None:
    if _directory_identity(output_parent_fd) != plan["inputs"]["output_parent_identity"]:
        raise DevCheckError("package parent descriptor changed; refusing cleanup")
    if not _package_marker_matches(plan, root_fd, marker, root_identity):
        raise DevCheckError("package ownership marker or root changed; refusing cleanup")
    staging_access = _proc_fd_path(root_fd, staging.name)
    if _path_exists(staging_access):
        if (
            not staging_owned
            or staging_identity is None
            or not _same_path_identity(staging_access, staging_identity)
        ):
            raise DevCheckError("package staging is not proven owned; refusing cleanup")
        if staging_access.is_symlink() or not staging_access.is_dir():
            os.unlink(staging.name, dir_fd=root_fd)
        else:
            shutil.rmtree(staging_access)
    os.unlink(marker.name, dir_fd=root_fd)
    os.rmdir(staging_root.name, dir_fd=output_parent_fd)
    os.fsync(output_parent_fd)


def _rename_noreplace(
    source_fd: int, source_name: str, destination_fd: int, destination_name: str
) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise DevCheckError("atomic no-replace publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise DevCheckError("package output appeared before publication")
        raise DevCheckError(
            f"atomic package publication failed: {os.strerror(error_number)}"
        )


def execute_package(plan: dict[str, Any]) -> dict[str, Any]:
    """Execute export-locks through owned staging and atomically publish it."""
    (
        repo,
        receipt,
        output,
        staging_root,
        staging,
        marker,
        evidence,
    ) = _validate_package_plan(plan)
    receipt_value, _receipt_data = _read_json_file(receipt, "check receipt")
    package_cache, package_cache_key = _package_export_cache_path(
        repo, receipt_value
    )
    evidence_fd = _open_pinned_directory(
        evidence.parent, plan["inputs"]["evidence_parent_identity"]
    )
    try:
        output_parent_fd = _open_pinned_directory(
            output.parent, plan["inputs"]["output_parent_identity"]
        )
    except BaseException:
        os.close(evidence_fd)
        raise
    record = _active_record(plan)
    record["_started_monotonic"] = time.monotonic()
    record["phases"] = []
    record["package_cache"] = {
        "path": str(package_cache),
        "key": package_cache_key,
        "state": "miss",
    }

    def record_phase(name: str, started: float) -> None:
        record["phases"].append(
            {
                "name": name,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
    try:
        evidence_identity = _create_json(evidence, record, evidence_fd)
        record["state"] = "active"
        record["started_at"] = _utc_now()
        record["next_safe_action"] = "Wait for export-locks or interrupt it once."
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
    except BaseException:
        os.close(output_parent_fd)
        os.close(evidence_fd)
        raise
    marker_created = False
    staging_root_fd: int | None = None
    staging_root_identity: dict[str, int] | None = None
    staging_identity: dict[str, int] | None = None
    staging_owned = False
    published_identity: dict[str, int] | None = None
    package_fd: int | None = None
    receipt_committed = False
    try:
        staging_root_identity, staging_root_fd = _create_package_marker(
            plan, staging_root, marker, output_parent_fd
        )
        marker_created = True
        staging_access = _proc_fd_path(staging_root_fd, staging.name)
        output_access = _proc_fd_path(output_parent_fd, output.name)
        planned_step = plan["steps"][0]
        record["ownership"] = {
            "root": str(staging_root),
            "root_identity": staging_root_identity,
            "marker": str(marker),
            "marker_identity": _identity_at(staging_root_fd, marker.name),
            "output_parent_identity": plan["inputs"]["output_parent_identity"],
            "state": "active",
        }
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
        phase_started = time.monotonic()
        if package_cache.exists():
            _hydrate_package_export_cache(
                package_cache, package_cache_key, staging_access
            )
            timestamp = _utc_now()
            stdout = _checkpoint_capture(b"package export cache hit\n")
            empty = _checkpoint_capture()
            outcome = {
                "returncode": 0,
                "timed_out": False,
                "interrupted": False,
                "started_at": timestamp,
                "finished_at": timestamp,
                "duration_ms": 0,
                "duration_ns": 0,
                "stdout": stdout,
                "stderr": empty,
                "stdout_tail": stdout["tail"],
                "stderr_tail": empty["tail"],
                "process_group_quiescent": True,
                "package_cache_reused": True,
            }
            record["package_cache"]["state"] = "reused"
        else:
            outcome = _run_process(
                list(planned_step["argv"]),
                Path(planned_step["cwd"]),
                int(planned_step["timeout_seconds"]),
                dict(planned_step["env"]),
            )
        record_phase("export-locks", phase_started)
        result = copy.deepcopy(planned_step)
        result.update(outcome)
        record["results"].append(result)
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
        if outcome["returncode"] != 0:
            raise _StepFailure(
                planned_step["name"],
                outcome["returncode"],
                outcome["interrupted"]
                or outcome["returncode"] == -int(signal.SIGINT),
            )
        if (
            "capture_error" in outcome["stdout"]
            or "capture_error" in outcome["stderr"]
        ):
            raise _StepFailure(f"{planned_step['name']}-capture", 1)
        if not _same_path_identity(
            output.parent, plan["inputs"]["output_parent_identity"]
        ):
            raise DevCheckError("package output parent changed during export")
        if not _path_exists(staging_access):
            raise DevCheckError("export-locks did not create package staging")
        staging_identity = _path_identity(staging_access)
        if staging_access.is_symlink() or not staging_access.is_dir():
            raise DevCheckError("export-locks staging is not a regular directory")
        staging_owned = True
        package_fd = os.open(
            staging.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=staging_root_fd,
        )
        if _directory_identity(package_fd) != staging_identity:
            raise DevCheckError("package staging changed while pinning its directory")
        package_access = Path(f"/proc/self/fd/{package_fd}")
        record["staging"] = {
            "path": str(staging),
            "identity": staging_identity,
            "state": "active",
        }
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
        phase_started = time.monotonic()
        exported, export_files = _verify_export_evidence(
            staging_access, plan["inputs"]["profile"], receipt_value
        )
        if record["package_cache"]["state"] == "miss":
            record["package_cache"]["state"] = _publish_package_export_cache(
                package_cache,
                package_cache_key,
                staging_access,
            )
        if (
            _collect_package_snapshot(repo, plan["inputs"]["profile"])
            != plan["inputs"]["package_snapshot"]
        ):
            raise DevCheckError("package inputs changed during export")
        record_phase("validate-export-and-inputs", phase_started)
        phase_started = time.monotonic()
        package_index = _complete_review_package(
            repo,
            package_access,
            receipt,
            receipt_value,
            plan["inputs"]["receipt_sha256"],
            exported,
            export_files,
        )
        record_phase("build-source-closure-and-index", phase_started)
        phase_started = time.monotonic()
        _fsync_package_tree(package_access)
        verification = _verify_owned_package_integrity(
            package_access, package_index
        )
        package_index_sha256 = verification["package_index_sha256"]
        package_identity = _package_tree_identity_snapshot(package_access)
        record_phase("durable-prepublication-integrity", phase_started)
        phase_started = time.monotonic()
        if _directory_identity(package_fd) != staging_identity:
            raise DevCheckError("package staging identity changed before publication")
        if _exists_at(output_parent_fd, output.name):
            raise DevCheckError("package output appeared before publication")
        _rename_noreplace(
            staging_root_fd,
            staging.name,
            output_parent_fd,
            output.name,
        )
        published_identity = _identity_at(output_parent_fd, output.name)
        os.fsync(output_parent_fd)
        if _package_tree_identity_snapshot(package_access) != package_identity:
            raise DevCheckError("published package identity changed at publication")
        record_phase("atomic-publication-identity-check", phase_started)
        if not _same_path_identity(
            output.parent, plan["inputs"]["output_parent_identity"]
        ):
            raise DevCheckError("package output parent changed during publication")
        record["staging"]["state"] = "published"
        record["ownership"]["state"] = "retained-through-durable-commit"
        verify_argv = [
            *plan["inputs"]["west_argv"],
            "dev",
            "verify-package",
            str(output),
            "--json",
        ]
        verify_command = shlex.join(verify_argv)
        record["package"] = {
            "output": str(output),
            "output_identity": published_identity,
            "export_evidence": {
                **exported,
                "path": str(output / "evidence.json"),
            },
            "check_receipt": {
                "path": str(output / "check-receipt.json"),
                "source_path": str(receipt),
                "sha256": plan["inputs"]["receipt_sha256"],
                "transaction_id": plan["inputs"]["receipt_transaction_id"],
                "tier": plan["inputs"]["receipt_tier"],
            },
            "package_snapshot": plan["inputs"]["package_snapshot"],
            "package_index": package_index,
            "verification": {
                **verification,
                "package": str(output),
            },
            "publication_verification": {
                "verified_after_rename": True,
                "package_index_sha256": verification["package_index_sha256"],
                "output_identity": published_identity,
            },
            "verify_before_use": {
                "argv": verify_argv,
                "command": verify_command,
                "reason": "local package files remain mutable by their owner",
            },
        }
        _finish_record(
            record,
            "committed",
            0,
            f"Run {verify_command} immediately before consuming package files.",
        )
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT}
        )
        try:
            evidence_identity = _atomic_json(
                evidence, record, evidence_fd, evidence_identity
            )
            if (
                not _same_path_identity(
                    output.parent, plan["inputs"]["output_parent_identity"]
                )
                or not _same_path_identity(output_access, published_identity)
            ):
                raise DevCheckError(
                    "package publication changed before durable receipt commit"
                )
            receipt_committed = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        marker_created = False
        try:
            _cleanup_package_staging(
                plan,
                output_parent_fd,
                staging_root_fd,
                staging_root,
                staging,
                marker,
                staging_root_identity,
                staging_identity,
                staging_owned,
            )
        except BaseException:
            # The durable committed receipt makes retained ownership metadata harmless.
            pass
        return record
    except BaseException as error:
        cleanup_error: BaseException | None = None
        if receipt_committed:
            try:
                if (
                    marker_created
                    and staging_root_identity is not None
                    and staging_root_fd is not None
                ):
                    _cleanup_package_staging(
                        plan,
                        output_parent_fd,
                        staging_root_fd,
                        staging_root,
                        staging,
                        marker,
                        staging_root_identity,
                        staging_identity,
                        staging_owned,
                    )
            except BaseException:
                pass
            return record
        if (
            marker_created
            and staging_owned
            and staging_identity is None
            and staging_root_fd is not None
            and _path_exists(staging_access)
        ):
            try:
                staging_identity = _path_identity(staging_access)
            except OSError:
                pass
        if marker_created and published_identity is not None:
            if not _same_path_identity(output_access, published_identity):
                cleanup_error = DevCheckError(
                    "published package identity changed; refusing rollback"
                )
            else:
                try:
                    _rename_noreplace(
                        output_parent_fd,
                        output.name,
                        staging_root_fd,
                        staging.name,
                    )
                    os.fsync(output_parent_fd)
                    os.fsync(staging_root_fd)
                    record["staging"]["state"] = "rolled-back-from-output"
                    published_identity = None
                except BaseException as observed:
                    cleanup_error = observed
        if (
            marker_created
            and cleanup_error is None
            and staging_root_identity is not None
            and staging_root_fd is not None
        ):
            try:
                _cleanup_package_staging(
                    plan,
                    output_parent_fd,
                    staging_root_fd,
                    staging_root,
                    staging,
                    marker,
                    staging_root_identity,
                    staging_identity,
                    staging_owned,
                )
                if "ownership" in record:
                    record["ownership"]["state"] = "cleaned"
                if "staging" in record:
                    record["staging"]["state"] = "cleaned"
            except BaseException as observed:
                cleanup_error = observed
                if "ownership" in record:
                    record["ownership"]["state"] = "cleanup-failed"
                if "staging" in record:
                    record["staging"]["state"] = "cleanup-failed"
        if isinstance(error, _StepFailure):
            returncode = _normalized_returncode(error.returncode)
            interrupted = error.interrupted
        elif isinstance(error, KeyboardInterrupt):
            returncode = 130
            interrupted = True
        else:
            returncode = 1
            interrupted = False
        detail = f"{type(error).__name__}: {error}"
        if cleanup_error is not None:
            detail += (
                f"; cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
            )
        record["error"] = detail
        state = "interrupted" if interrupted and cleanup_error is None else "failed"
        _finish_record(
            record,
            state,
            returncode,
            "Inspect the receipt, then rebuild and rerun the package plan.",
        )
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
        return record
    finally:
        if package_fd is not None:
            os.close(package_fd)
        if staging_root_fd is not None:
            os.close(staging_root_fd)
        os.close(output_parent_fd)
        os.close(evidence_fd)

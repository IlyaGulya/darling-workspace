from __future__ import annotations

import concurrent.futures
import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import shlex
import signal
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import quote

try:
    from .runtime_install import RuntimeInstallationError, cmake_cache, resolve_runtime
except ImportError:
    from runtime_install import RuntimeInstallationError, cmake_cache, resolve_runtime

SCHEMA_VERSION = 1
TIER_ORDER = ("quick", "canonical", "acceptance")
_TIER_RANK = {name: rank for rank, name in enumerate(TIER_ORDER)}
_CAPTURE_LIMIT = 16 * 1024
_JSON_LIMIT = 8 * 1024 * 1024
_SNAPSHOT_DIFF_LIMIT = 64 * 1024 * 1024
_SOURCE_LIST_LIMIT = 1024 * 1024
_UNTRACKED_POLICY = {
    "coverage": "excluded",
    "inventory": "not-collected",
    "new_source": "stage-before-review",
}
_TERMINATE_GRACE_SECONDS = 5.0
_DEFAULT_ACCEPTANCE_WORKLOADS = (
    "homebrew-prepare",
    "homebrew-preflight",
    "homebrew-source",
)
_EXACT_CAPTURE_WORKLOAD = "exact-capture"
_CHECK_TIMEOUTS = {
    "test-list": 900,
    "host-test": 10800,
    "workload": 10800,
    "source-identity": 60,
}
_PACKAGE_TIMEOUT_SECONDS = 300
_PACKAGE_INDEX_FIELDS = {
    "schema_version",
    "operation",
    "manifest_repo",
    "manifest_head",
    "manifest_tree",
    "check_receipt_sha256",
    "check_transaction_id",
    "tier",
    "source_snapshot_sha256",
    "files",
}
_SNAPSHOT_FIELDS = {
    "manifest_repo",
    "manifest_head",
    "manifest_tree",
    "dirty",
    "status",
    "working_tree_diff",
    "staged_diff",
    "untracked",
    "projects",
    "west_yml",
    "source_changes",
    "project_changes",
    "commands",
}


class DevCheckError(RuntimeError):
    """A malformed plan, receipt, or produced package."""


class _BoundedCapture:
    def __init__(self, limit: int = _CAPTURE_LIMIT, full_limit: int = 0) -> None:
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
        except BaseException as error:
            self.error = f"{type(error).__name__}: {error}"
        finally:
            stream.close()

    def full(self) -> bytes | None:
        if not self._full_limit or self._full_overflow:
            return None
        return bytes(self._full)

    def result(self) -> dict[str, Any]:
        tail = self._tail.decode("utf-8", errors="replace")
        result = {
            "sha256": self._digest.hexdigest(),
            "bytes": self._count,
            "tail": tail,
            "truncated": self._count > self._limit,
        }
        if self.error is not None:
            result["capture_error"] = self.error
        return result


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _absolute(path: Path, name: str) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise DevCheckError(f"{name} must be absolute: {selected}")
    return selected


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _is_proc_fd_root(path: Path) -> bool:
    return str(path).startswith("/proc/self/fd/") or str(path).startswith("/proc/thread-self/fd/")


def _validate_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DevCheckError(f"{name} must be a non-empty string")
    return value


def _validate_west_argv(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(argument, str) or not argument for argument in value)
    ):
        raise DevCheckError("west_argv must be a non-empty argv list")
    return list(value)


def _tier(value: object, name: str = "tier") -> str:
    if value not in _TIER_RANK:
        raise DevCheckError(f"{name} must be one of {', '.join(TIER_ORDER)}")
    return str(value)


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


def _safe_package_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DevCheckError(f"{label} is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise DevCheckError(f"{label} is not a canonical safe relative path")
    return value


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


def _json_digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_workloads(value: object, tier: str) -> list[str]:
    if value is None:
        selected: list[str] = []
    elif isinstance(value, (list, tuple)):
        selected = []
        for item in value:
            if not isinstance(item, str) or not item:
                raise DevCheckError("workload names must be non-empty strings")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item):
                raise DevCheckError(f"invalid workload name: {item}")
            selected.append(item)
    else:
        raise DevCheckError("workloads must be a list of names")
    if len(selected) != len(set(selected)):
        raise DevCheckError("workload names must be unique")
    if tier == "acceptance" and not selected:
        selected = list(_DEFAULT_ACCEPTANCE_WORKLOADS)
    if _EXACT_CAPTURE_WORKLOAD in selected and len(selected) != 1:
        raise DevCheckError(
            "exact-capture requires an exclusive workload selection"
        )
    return selected


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
    process_env = {} if sanitize_environment else os.environ.copy()
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
            "process_group_quiescent": True,
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


def _directory_identity(directory_fd: int) -> dict[str, int]:
    return _identity_from_stat(os.fstat(directory_fd))


def _exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _open_pinned_directory(path: Path, expected_identity: dict[str, int]) -> int:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    if _directory_identity(descriptor) != expected_identity:
        os.close(descriptor)
        raise DevCheckError(f"directory identity changed while opening: {path}")
    return descriptor


def _path_identity(path: Path) -> dict[str, int]:
    metadata = path.lstat()
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _same_path_identity(path: Path, identity: dict[str, int]) -> bool:
    try:
        return _path_identity(path) == identity
    except OSError:
        return False


def _json_bytes(payload: dict[str, Any]) -> bytes:
    public_payload = {
        key: value for key, value in payload.items() if not key.startswith("_")
    }
    return (json.dumps(public_payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


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


def _create_json(path: Path, payload: dict[str, Any], directory_fd: int) -> dict[str, int]:
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    created_identity: dict[str, int] | None = None
    temporary_descriptor: int | None = None
    committed = False
    try:
        if not _same_path_identity(path.parent, _directory_identity(directory_fd)):
            raise DevCheckError(
                f"evidence parent changed before initial publication: {path.parent}"
            )
        temporary_descriptor = _write_json_temp(
            directory_fd, temporary_name, _json_bytes(payload)
        )
        temporary_identity = _identity_from_stat(os.fstat(temporary_descriptor))
        if not _same_path_identity(path.parent, _directory_identity(directory_fd)):
            raise DevCheckError(
                f"evidence parent changed during initial publication: {path.parent}"
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
        if not _same_path_identity(path.parent, _directory_identity(directory_fd)):
            raise DevCheckError(
                f"evidence parent changed before durable publication: {path.parent}"
            )
        os.fsync(directory_fd)
        committed = True
        return created_identity
    finally:
        if created_identity is not None and not committed:
            try:
                current = _identity_at(directory_fd, path.name)
            except FileNotFoundError:
                current = None
            if current == created_identity:
                try:
                    os.unlink(path.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _atomic_json(
    path: Path,
    payload: dict[str, Any],
    directory_fd: int,
    expected_identity: dict[str, int],
) -> dict[str, int]:
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    new_descriptor: int | None = None
    try:
        if not _same_path_identity(path.parent, _directory_identity(directory_fd)):
            raise DevCheckError(f"evidence parent changed before update: {path.parent}")
        new_descriptor = _write_json_temp(
            directory_fd, temporary_name, _json_bytes(payload)
        )
        new_identity = _identity_from_stat(os.fstat(new_descriptor))
        current = _identity_at(directory_fd, path.name)
        if current != expected_identity:
            raise DevCheckError(f"evidence identity changed before update: {path}")
        if not _same_path_identity(path.parent, _directory_identity(directory_fd)):
            raise DevCheckError(f"evidence parent changed during update: {path.parent}")
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        if _identity_at(directory_fd, path.name) != new_identity:
            raise DevCheckError(f"evidence identity changed during update: {path}")
        if not _same_path_identity(path.parent, _directory_identity(directory_fd)):
            raise DevCheckError(f"evidence parent changed after update: {path.parent}")
        return new_identity
    finally:
        if new_descriptor is not None:
            os.close(new_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _validate_plan_envelope(plan: dict[str, Any], operation: str) -> None:
    if not isinstance(plan, dict):
        raise DevCheckError("plan must be an object")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise DevCheckError("unsupported plan schema_version")
    if plan.get("operation") != operation or plan.get("state") != "planned":
        raise DevCheckError(f"expected a planned {operation} operation")
    if "returncode" not in plan or plan["returncode"] is not None:
        raise DevCheckError("planned operation returncode must be null")
    if not _is_lower_hex(plan.get("transaction_id"), 32):
        raise DevCheckError("planned operation transaction_id is invalid")
    if not isinstance(plan.get("inputs"), dict):
        raise DevCheckError("plan inputs must be an object")
    if not isinstance(plan.get("steps"), list) or not plan["steps"]:
        raise DevCheckError("plan steps must be a non-empty list")


def _active_record(plan: dict[str, Any]) -> dict[str, Any]:
    record = copy.deepcopy(plan)
    record["state"] = "planned"
    record["results"] = []
    record["returncode"] = None
    record["next_safe_action"] = "Execute this frozen plan."
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


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _run_git(argv: list[str], cwd: Path, timeout: int = 60) -> dict[str, Any]:
    return _run_process([*argv], cwd, timeout, {})


def _parse_west_projects(
    manifest_repo: Path, west_argv: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    argv = [
        *west_argv,
        "list",
        "--format={name}\t{abspath}\t{revision}\t{cloned}",
    ]
    result = _run_process(
        argv,
        manifest_repo,
        120,
        {},
        stdout_full_limit=_SOURCE_LIST_LIMIT,
    )
    if (
        result["returncode"] != 0
        or "capture_error" in result["stdout"]
        or "capture_error" in result["stderr"]
    ):
        raise DevCheckError("cannot list active West projects for source snapshot")
    raw = result.get("_stdout_full")
    if not isinstance(raw, bytes):
        raise DevCheckError("active West project list exceeds snapshot bound")
    projects: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise DevCheckError("active West project list row is malformed")
        name, abspath, revision, cloned = fields
        if not name or not revision or cloned not in {"cloned", "not-cloned"}:
            raise DevCheckError("active West project list row is invalid")
        path = Path(abspath)
        if not path.is_absolute():
            raise DevCheckError("active West project path is not absolute")
        if name in seen_names or abspath in seen_paths:
            raise DevCheckError("active West project list is duplicated")
        seen_names.add(name)
        seen_paths.add(abspath)
        projects.append(
            {
                "name": name,
                "path": abspath,
                "revision": revision,
                "cloned": cloned,
            }
        )
    projects.sort(key=lambda row: (row["name"], row["path"]))
    return projects, argv


def _tracked_diff_commands(
    path: Path,
) -> tuple[tuple[str, str, list[str]], ...]:
    base = [
        "git", "-C", str(path), "diff", "--binary", "--ignore-submodules=untracked"
    ]
    return (
        ("working", "working_tree_diff", [*base, "HEAD", "--"]),
        ("staged", "staged_diff", [*base, "--cached", "HEAD", "--"]),
    )


def _git_changes_snapshot(path: Path, label: str) -> dict[str, Any]:
    """Capture committed refs and tracked/index changes, never untracked inputs."""
    head_argv = ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"]
    tree_argv = ["git", "-C", str(path), "rev-parse", "--verify", "HEAD^{tree}"]
    head_result = _run_git(head_argv, path)
    tree_result = _run_git(tree_argv, path)
    for name, result in (("HEAD", head_result), ("HEAD tree", tree_result)):
        if result["returncode"] != 0 or result["stdout"]["truncated"]:
            raise DevCheckError(f"cannot resolve {label} {name}")
    head = _require_oid(head_result["stdout_tail"].strip(), f"{label} HEAD")
    tree = _require_oid(tree_result["stdout_tail"].strip(), f"{label} HEAD tree")
    status_argv = [
        "git", "-C", str(path), "status", "--porcelain=v1",
        "--untracked-files=no", "--ignore-submodules=untracked",
    ]
    results = {"status": _run_git(status_argv, path)}
    commands = [head_argv, tree_argv, status_argv]
    for _suffix, field, argv in _tracked_diff_commands(path):
        results[field] = _run_git(argv, path, 300)
        commands.append(argv)
    for field, result in results.items():
        if result["returncode"] != 0 or "capture_error" in result["stdout"]:
            raise DevCheckError(f"cannot capture {label} {field}")
        bound = _SOURCE_LIST_LIMIT if field == "status" else _SNAPSHOT_DIFF_LIMIT
        if result["stdout"]["bytes"] > bound:
            raise DevCheckError(f"{label} {field} exceeds snapshot bound")
    return {
        "path": str(path),
        "head": head,
        "tree": tree,
        "dirty": any(result["stdout"]["bytes"] != 0 for result in results.values()),
        **{
            field: {
                "sha256": result["stdout"]["sha256"],
                "bytes": result["stdout"]["bytes"],
            }
            for field, result in results.items()
        },
        "untracked": dict(_UNTRACKED_POLICY),
        "commands": commands,
    }


def _source_project_path(projects: list[dict[str, Any]]) -> str:
    candidates = [
        row["path"]
        for row in projects
        if row["name"] == "darling" or Path(row["path"]).name == "darling"
    ]
    if len(candidates) != 1:
        raise DevCheckError(
            "active West manifest must name exactly one Darling source project"
        )
    return candidates[0]


def _source_gitlinks(source: Path) -> tuple[set[Path], list[list[str]]]:
    """Discover only committed product Gitlinks, never unrelated nested West repos."""
    links: set[Path] = set()
    seen_repositories: set[Path] = set()
    commands: list[list[str]] = []
    pending = [Path()]
    while pending:
        relative = pending.pop()
        repository = source / relative
        physical = repository.resolve()
        if not physical.is_relative_to(source) or physical in seen_repositories:
            raise DevCheckError(f"selected product Gitlink escapes or repeats a repository: {repository}")
        seen_repositories.add(physical)
        if not (repository / ".git").exists():
            raise DevCheckError(f"selected product Gitlink repository is unavailable: {repository}")
        argv = ["git", "-C", str(repository), "ls-tree", "-rz", "HEAD"]
        outcome = _run_process(
            argv, repository, 60, {}, stdout_full_limit=_SNAPSHOT_DIFF_LIMIT
        )
        data = outcome.get("_stdout_full")
        if outcome["returncode"] != 0 or not isinstance(data, bytes):
            raise DevCheckError(f"cannot inspect selected product Gitlinks: {repository}")
        commands.append(argv)
        for record in data.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            if metadata.split(b" ", 1)[0] != b"160000":
                continue
            child = relative / os.fsdecode(raw_path)
            if child.is_absolute() or ".." in child.parts or child in links:
                raise DevCheckError("selected product Gitlink path is unsafe or repeated")
            links.add(child)
            pending.append(child)
    return links, commands


def _collect_source_snapshot(
    manifest_repo: Path, west_argv: list[str], source_root: Path | None = None
) -> dict[str, Any]:
    """Snapshot active West pins and every selected project's tracked changes."""
    manifest_changes = _git_changes_snapshot(manifest_repo, "manifest repository")
    projects, west_list_argv = _parse_west_projects(manifest_repo, west_argv)
    canonical_product = Path(_source_project_path(projects))
    selected_product = (
        canonical_product.resolve() if source_root is None else source_root.expanduser().resolve()
    )
    links, closure_commands = _source_gitlinks(selected_product)
    snapshots = {str(manifest_repo): manifest_changes}
    project_changes: list[dict[str, Any]] = []
    commands = [*manifest_changes["commands"], west_list_argv, *closure_commands]
    for project in projects:
        canonical = Path(project["path"])
        selected = canonical
        try:
            relative = canonical.relative_to(canonical_product)
        except ValueError:
            pass
        else:
            if relative == Path() or relative in links:
                selected = selected_product / relative
        selected_text = str(selected)
        if selected_text not in snapshots:
            snapshots[selected_text] = _git_changes_snapshot(selected, project["name"])
            commands.extend(snapshots[selected_text]["commands"])
        changes = snapshots[selected_text]
        revision = project["revision"]
        pin_argv = [
            "git", "-C", selected_text, "rev-parse", "--verify", f"{revision}^{{commit}}"
        ]
        pin_result = _run_git(pin_argv, selected)
        if pin_result["returncode"] != 0 or pin_result["stdout"]["truncated"]:
            raise DevCheckError(f"cannot resolve active West pin for {project['name']}")
        pinned = _require_oid(pin_result["stdout_tail"].strip(), "active West pin")
        commands.append(pin_argv)
        project.update({
            "canonical_path": str(canonical),
            "path": selected_text,
            "sha": pinned,
            "cloned": "cloned",
            "head": changes["head"],
            "tree": changes["tree"],
            "dirty": changes["dirty"],
        })
        if (
            changes["dirty"]
            and selected != manifest_repo
            and selected != selected_product
        ):
            project_changes.append({"project": project["name"], "changes": changes})
    projects.sort(key=lambda row: (row["name"], row["path"]))
    source_changes = snapshots[str(selected_product)]
    west_yml = manifest_repo / "west.yml"
    if west_yml.is_symlink() or not west_yml.is_file():
        raise DevCheckError(f"active West manifest is unavailable: {west_yml}")
    manifest_argv = [
        "git", "-C", str(manifest_repo), "ls-files", "--error-unmatch", "--", "west.yml"
    ]
    if _run_git(manifest_argv, manifest_repo)["returncode"] != 0:
        raise DevCheckError("active West manifest must be tracked; stage west.yml for review")
    west_digest, west_size = _hash_file(west_yml)
    return {
        "manifest_repo": str(manifest_repo),
        "manifest_head": manifest_changes["head"],
        "manifest_tree": manifest_changes["tree"],
        **{
            field: manifest_changes[field]
            for field in (
                "dirty", "status", "working_tree_diff", "staged_diff", "untracked"
            )
        },
        "projects": projects,
        "west_yml": {"sha256": west_digest, "bytes": west_size},
        "source_changes": source_changes,
        "project_changes": project_changes,
        "commands": [*commands, manifest_argv],
    }


def _validate_changes(value: object, label: str) -> dict[str, Any]:
    fields = {
        "path",
        "head",
        "tree",
        "dirty",
        "status",
        "working_tree_diff",
        "staged_diff",
        "untracked",
        "commands",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise DevCheckError(f"{label} schema is invalid")
    if not isinstance(value.get("path"), str) or not Path(value["path"]).is_absolute():
        raise DevCheckError(f"{label} path is invalid")
    _require_oid(value.get("head"), f"{label} HEAD")
    _require_oid(value.get("tree"), f"{label} tree")
    if not isinstance(value.get("dirty"), bool):
        raise DevCheckError(f"{label} dirty field is invalid")
    for field in ("status", "working_tree_diff", "staged_diff"):
        row = value.get(field)
        if (
            not isinstance(row, dict)
            or set(row) != {"sha256", "bytes"}
            or not _is_lower_hex(row.get("sha256"), 64)
            or not isinstance(row.get("bytes"), int)
            or isinstance(row.get("bytes"), bool)
            or row["bytes"] < 0
        ):
            raise DevCheckError(f"{label} {field} is invalid")
    if value.get("untracked") != _UNTRACKED_POLICY:
        raise DevCheckError(f"{label} untracked exclusion policy is invalid")
    commands = value.get("commands")
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
        raise DevCheckError(f"{label} commands are invalid")
    return value


def _validate_source_snapshot(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise DevCheckError("check receipt source snapshot schema is invalid")
    _validate_text(snapshot.get("manifest_repo"), "snapshot manifest_repo")
    _validate_changes(
        {
            "path": snapshot["manifest_repo"],
            "head": snapshot["manifest_head"],
            "tree": snapshot["manifest_tree"],
            **{
                field: snapshot[field]
                for field in (
                    "dirty", "status", "working_tree_diff", "staged_diff",
                    "untracked", "commands",
                )
            },
        },
        "manifest changes",
    )
    projects = snapshot.get("projects")
    if not isinstance(projects, list):
        raise DevCheckError("snapshot projects are missing")
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    for row in projects:
        if (
            not isinstance(row, dict)
            or set(row) != {
                "name", "path", "canonical_path", "sha", "revision", "cloned",
                "head", "tree", "dirty",
            }
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or not isinstance(row.get("path"), str)
            or not Path(row["path"]).is_absolute()
            or not _is_lower_hex(row.get("sha"), 40)
            or not isinstance(row.get("canonical_path"), str)
            or not Path(row["canonical_path"]).is_absolute()
            or not _is_lower_hex(row.get("head"), 40)
            or not _is_lower_hex(row.get("tree"), 40)
            or not isinstance(row.get("dirty"), bool)
            or not isinstance(row.get("revision"), str)
            or not row["revision"]
            or row.get("cloned") != "cloned"
            or row["name"] in seen_names
            or row["path"] in seen_paths
        ):
            raise DevCheckError("snapshot project row is invalid")
        seen_names.add(row["name"])
        seen_paths.add(row["path"])
    if projects != sorted(projects, key=lambda row: (row["name"], row["path"])):
        raise DevCheckError("snapshot projects are not sorted")
    source_changes = _validate_changes(
        snapshot.get("source_changes"), "source changes"
    )
    by_name = {row["name"]: row for row in projects}
    roots = {snapshot["manifest_repo"], source_changes["path"]}
    source_projects = [
        row for row in projects if row["path"] == source_changes["path"]
    ]
    if (
        len(source_projects) != 1
        or source_projects[0]["head"] != source_changes["head"]
        or source_projects[0]["tree"] != source_changes["tree"]
        or source_projects[0]["dirty"] != source_changes["dirty"]
    ):
        raise DevCheckError("snapshot source changes do not match the selected product")
    changed = snapshot.get("project_changes")
    if not isinstance(changed, list):
        raise DevCheckError("snapshot project changes are missing")
    seen_changes: set[str] = set()
    for entry in changed:
        if not isinstance(entry, dict) or set(entry) != {"project", "changes"}:
            raise DevCheckError("snapshot project change schema is invalid")
        name = entry.get("project")
        if not isinstance(name, str) or name not in by_name or name in seen_changes:
            raise DevCheckError("snapshot changed project is unknown or repeated")
        changes = _validate_changes(entry["changes"], f"project {name}")
        project = by_name[name]
        if (
            changes["path"] in roots
            or not changes["dirty"]
            or any(changes[field] != project[field] for field in ("path", "head", "tree", "dirty"))
        ):
            raise DevCheckError("snapshot project changes do not match the selected project")
        seen_changes.add(name)
    expected_changes = {
        row["name"] for row in projects if row["dirty"] and row["path"] not in roots
    }
    if seen_changes != expected_changes or [entry["project"] for entry in changed] != sorted(seen_changes):
        raise DevCheckError("snapshot changed project coverage is incomplete or unsorted")
    west = snapshot.get("west_yml")
    if (
        not isinstance(west, dict)
        or set(west) != {"sha256", "bytes"}
        or not _is_lower_hex(west.get("sha256"), 64)
        or not isinstance(west.get("bytes"), int)
        or isinstance(west.get("bytes"), bool)
        or west["bytes"] < 0
    ):
        raise DevCheckError("snapshot west.yml identity is invalid")
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
    return snapshot


def _runtime_inputs(
    tier: str,
    workloads: list[str],
    runtime_root: Path | None,
    prefix: Path | None,
) -> dict[str, Any]:
    if tier == "acceptance" and not workloads:
        raise DevCheckError("acceptance checks require at least one workload")
    selected_runtime = runtime_root or os.environ.get("DARLING_RUNTIME_ROOT")
    if workloads and (selected_runtime is None or prefix is None):
        raise DevCheckError("named workloads require both runtime_root and prefix")
    if selected_runtime is None:
        return {
            "runtime_root": None,
            "runtime_launcher": None,
            "runtime_identity_sha256": None,
            "prefix": None if prefix is None else str(prefix),
            "source_directory": None,
        }
    try:
        runtime = resolve_runtime(selected_runtime)
        if prefix is not None:
            runtime.environment(prefix)
    except (OSError, RuntimeInstallationError, ValueError) as error:
        raise DevCheckError(f"installed runtime is invalid: {error}") from error
    source = runtime.identity.get("source") if runtime.identity is not None else None
    return {
        "runtime_root": str(runtime.root),
        "runtime_launcher": str(runtime.launcher),
        "runtime_identity_sha256": (
            _json_digest(runtime.identity) if runtime.identity is not None else None
        ),
        "prefix": None if prefix is None else str(prefix),
        "source_directory": source.get("directory") if isinstance(source, dict) else None,
    }


def _selected_check_source(
    explicit: Path | None, build_dir: Path | None, runtime_source: object
) -> Path | None:
    if explicit is not None:
        return _absolute(explicit, "source_root").expanduser().resolve()
    if build_dir is not None:
        try:
            selected = cmake_cache(build_dir).get("CMAKE_HOME_DIRECTORY")
        except RuntimeInstallationError as error:
            raise DevCheckError(str(error)) from error
        if not selected:
            raise DevCheckError(f"selected build has no CMAKE_HOME_DIRECTORY: {build_dir}")
        return _absolute(Path(selected), "CMAKE_HOME_DIRECTORY").resolve()
    if runtime_source is not None:
        if not isinstance(runtime_source, str):
            raise DevCheckError("selected runtime receipt source directory is invalid")
        return _absolute(Path(runtime_source), "runtime source directory").resolve()
    return None


def _source_root_from_snapshot(snapshot: dict[str, Any]) -> str:
    selected = snapshot["source_changes"]["path"]
    if not Path(selected).is_dir():
        raise DevCheckError(f"Darling source root is unavailable: {selected}")
    return selected


def _bead_args(bead: str | None) -> list[str]:
    return [] if bead is None else ["--bead", bead]


def _check_steps(inputs: dict[str, Any], transaction_id: str) -> list[dict[str, Any]]:
    west = list(inputs["west_argv"])
    bead = _bead_args(inputs["bead"])
    source = ["--source-root", inputs["source_root"]]
    repo = Path(inputs["manifest_repo"])
    tier = inputs["tier"]
    steps = [
        _step(
            "test-list",
            [*west, "test", *source, *bead, "--list"],
            "read-only",
            _CHECK_TIMEOUTS["test-list"],
            repo,
        )
    ]
    if tier in {"canonical", "acceptance"}:
        steps.append(
            _step(
                "host-test",
                [*west, "test", *source, *bead, "--env", "host"],
                "temporary/local-output",
                _CHECK_TIMEOUTS["host-test"],
                repo,
            )
        )
    runtime_root = inputs.get("runtime_root")
    prefix = inputs.get("prefix")
    for name in inputs["workloads"]:
        argv = [
            *west,
            "test",
            *source,
            "--workload",
            name,
            "--runtime-root",
            str(runtime_root),
            "--prefix",
            str(prefix),
        ]
        steps.append(
            _step(
                f"workload:{name}",
                argv,
                "temporary/local-output",
                _CHECK_TIMEOUTS["workload"],
                repo,
            )
        )
    return steps


def _validate_check_inputs(inputs: dict[str, Any]) -> None:
    expected = {
        "manifest_repo",
        "source_root",
        "west_argv",
        "tier",
        "workloads",
        "runtime_root",
        "runtime_launcher",
        "runtime_identity_sha256",
        "bead",
        "evidence",
        "evidence_parent_identity",
        "prefix",
        "build_dir",
        "package_snapshot",
    }
    if set(inputs) != expected:
        raise DevCheckError("check plan inputs have unsupported fields")
    _validate_text(inputs.get("source_root"), "source_root")
    if not Path(inputs["source_root"]).is_absolute():
        raise DevCheckError("check plan source_root must be absolute")
    _tier(inputs.get("tier"))
    _validate_west_argv(inputs.get("west_argv"))
    workloads = inputs.get("workloads")
    if not isinstance(workloads, list) or any(
        not isinstance(name, str) or not name for name in workloads
    ):
        raise DevCheckError("check plan workloads are invalid")
    for name in ("bead", "runtime_root", "runtime_launcher", "prefix", "build_dir"):
        value = inputs.get(name)
        if value is not None:
            _validate_text(value, name)
    workloads = inputs["workloads"]
    for name in workloads:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise DevCheckError(f"check receipt workload is invalid: {name}")
        if name == _EXACT_CAPTURE_WORKLOAD and len(workloads) != 1:
            raise DevCheckError("check receipt exact-capture selection is not exclusive")
    for name in ("runtime_root", "runtime_launcher", "prefix", "build_dir"):
        value = inputs.get(name)
        if value is not None and not Path(value).is_absolute():
            raise DevCheckError(f"check receipt {name} must be absolute")
    if workloads and (
        inputs.get("runtime_root") is None or inputs.get("prefix") is None
    ):
        raise DevCheckError("check receipt workloads lack runtime prerequisites")
    if (inputs.get("runtime_root") is None) != (inputs.get("runtime_launcher") is None):
        raise DevCheckError("check receipt runtime selection fields are incomplete")
    if inputs.get("runtime_identity_sha256") is not None and inputs.get("runtime_root") is None:
        raise DevCheckError("check receipt runtime identity has no selected installation")
    if inputs.get("runtime_identity_sha256") is not None:
        _require_digest(
            inputs.get("runtime_identity_sha256"),
            "check receipt runtime identity",
        )
    if inputs["tier"] == "acceptance" and not workloads:
        raise DevCheckError("acceptance check receipt has no workloads")
    if inputs["tier"] == "acceptance" and inputs.get("bead") is not None:
        raise DevCheckError("acceptance checks cannot be narrowed by bead")
    _validate_source_snapshot(inputs.get("package_snapshot"))
    snapshot = inputs["package_snapshot"]
    expected_source = {
        row["path"]
        for row in snapshot["projects"]
        if row["name"] == "darling" or Path(row["path"]).name == "darling"
    }
    if inputs["source_root"] not in expected_source:
        raise DevCheckError(
            "check receipt source_root does not name the active Darling project"
        )
    source_changes = snapshot["source_changes"]
    source_projects = [
        row for row in snapshot["projects"] if row["path"] == source_changes["path"]
    ]
    if (
        len(source_projects) != 1
        or source_projects[0]["head"] != source_changes["head"]
    ):
        raise DevCheckError(
            "check receipt source revision does not match its checkout HEAD"
        )


def _validate_check_plan(plan: dict[str, Any]) -> tuple[Path, Path]:
    _validate_plan_envelope(plan, "check")
    inputs = plan["inputs"]
    _validate_check_inputs(inputs)
    repo = Path(_validate_text(inputs.get("manifest_repo"), "manifest_repo"))
    evidence = Path(_validate_text(inputs.get("evidence"), "evidence"))
    if not repo.is_absolute() or not evidence.is_absolute():
        raise DevCheckError("check plan paths must be absolute")
    if _is_within(evidence, repo):
        raise DevCheckError("check evidence must be outside the active manifest repository")
    if not repo.is_dir():
        raise DevCheckError(f"manifest_repo is not a directory: {repo}")
    if _path_identity(evidence.parent) != inputs.get("evidence_parent_identity"):
        raise DevCheckError("check evidence parent identity changed after planning")
    if _path_exists(evidence):
        raise DevCheckError(f"check evidence already exists: {evidence}")
    current = _collect_source_snapshot(repo, inputs["west_argv"], Path(inputs["source_root"]))
    if current != inputs["package_snapshot"]:
        raise DevCheckError("manifest repository or active source changed after planning")
    if inputs.get("runtime_launcher") is not None:
        try:
            runtime = resolve_runtime(Path(inputs["runtime_root"]))
            if inputs["prefix"] is not None:
                runtime.environment(Path(inputs["prefix"]))
        except (OSError, RuntimeInstallationError, ValueError) as error:
            raise DevCheckError(f"installed runtime is invalid: {error}") from error
        observed = (
            _json_digest(runtime.identity) if runtime.identity is not None else None
        )
        if observed != inputs.get("runtime_identity_sha256"):
            raise DevCheckError("installed runtime identity changed after planning")
        if str(runtime.launcher) != inputs.get("runtime_launcher"):
            raise DevCheckError("installed runtime launcher changed after planning")
    if plan["steps"] != _check_steps(inputs, plan["transaction_id"]):
        raise DevCheckError("check steps do not match the frozen inputs")
    return repo, evidence


def build_check_plan(
    manifest_repo: Path,
    west_argv: list[str],
    tier: str,
    workloads: list[str],
    runtime_root: Path | None,
    bead: str | None,
    evidence: Path,
    prefix: Path | None,
    build_dir: Path | None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Build a frozen, serializable check plan without running or writing anything."""
    selected_tier = _tier(tier)
    selected_bead = None if bead is None else _validate_text(bead, "bead")
    if selected_tier == "acceptance" and selected_bead is not None:
        raise DevCheckError("acceptance checks cannot be narrowed by bead")
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
    selected_workloads = _normalized_workloads(workloads, selected_tier)
    selected_prefix = _absolute(prefix, "prefix") if prefix is not None else None
    runtime = _runtime_inputs(
        selected_tier,
        selected_workloads,
        _absolute(runtime_root, "runtime_root") if runtime_root is not None else None,
        selected_prefix,
    )
    transaction_id = uuid.uuid4().hex
    selected_source = _selected_check_source(
        source_root,
        _absolute(build_dir, "build_dir") if build_dir is not None else None,
        runtime["source_directory"],
    )
    snapshot = _collect_source_snapshot(
        repo, _validate_west_argv(west_argv), selected_source
    )
    inputs = {
        "manifest_repo": str(repo),
        "source_root": _source_root_from_snapshot(snapshot),
        "west_argv": _validate_west_argv(west_argv),
        "tier": selected_tier,
        "workloads": selected_workloads,
        "runtime_root": runtime["runtime_root"],
        "runtime_launcher": runtime["runtime_launcher"],
        "runtime_identity_sha256": runtime["runtime_identity_sha256"],
        "bead": selected_bead,
        "evidence": str(evidence_path),
        "evidence_parent_identity": _path_identity(evidence_path.parent),
        "prefix": selected_prefix if selected_prefix is None else str(selected_prefix),
        "build_dir": (
            str(_absolute(build_dir, "build_dir")) if build_dir is not None else None
        ),
        "package_snapshot": snapshot,
    }
    _validate_check_inputs(inputs)
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


def _listed_test_selection(stdout_tail: str, stderr_tail: str) -> bool:
    text = stdout_tail + "\n" + stderr_tail
    return any(
        not line[:1].isspace()
        and ": " in line
        and "[" in line
        and "env:" in line
        and "diag:" in line
        and "kind:" in line
        for line in text.splitlines()
    )


class _StepFailure(DevCheckError):
    def __init__(self, name: str, returncode: int, interrupted: bool = False) -> None:
        super().__init__(f"{name} failed with returncode {returncode}")
        self.returncode = returncode
        self.interrupted = interrupted


def _normalized_returncode(returncode: int) -> int:
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


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
        evidence_identity = _atomic_json(evidence, record, evidence_fd, evidence_identity)
    except BaseException:
        os.close(evidence_fd)
        raise
    try:
        total = len(plan["steps"])
        for index, planned_step in enumerate(plan["steps"], start=1):
            _progress(progress, "start", planned_step, index, total, started)
            outcome = _run_process(
                list(planned_step["argv"]),
                Path(planned_step["cwd"]),
                int(planned_step["timeout_seconds"]),
                dict(planned_step["env"]),
            )
            result = copy.deepcopy(planned_step)
            result.update(outcome)
            if planned_step["name"] == "test-list" and outcome["returncode"] == 0:
                selected = _listed_test_selection(
                    outcome["stdout_tail"], outcome["stderr_tail"]
                )
                result["selection_oracle"] = {
                    "required": True,
                    "selected": selected,
                }
            record["results"].append(result)
            _progress(
                progress,
                "finish",
                planned_step,
                index,
                total,
                started,
                duration_ms=result["duration_ms"],
                returncode=result["returncode"],
            )
            evidence_identity = _atomic_json(
                evidence, record, evidence_fd, evidence_identity
            )
            if result["returncode"] != 0:
                raise _StepFailure(
                    planned_step["name"],
                    result["returncode"],
                    result["interrupted"],
                )
            if result["timed_out"] or result["interrupted"]:
                raise _StepFailure(planned_step["name"], result["returncode"] or 1)
            if result.get("process_group_quiescent") is not True:
                raise _StepFailure(f"{planned_step['name']}-descendants", 125)
            oracle = result.get("selection_oracle")
            if isinstance(oracle, dict) and not oracle["selected"]:
                raise _StepFailure("test-list-selection-oracle", 1)
        current = _collect_source_snapshot(
            repo, plan["inputs"]["west_argv"], Path(plan["inputs"]["source_root"])
        )
        if current != plan["inputs"]["package_snapshot"]:
            raise _StepFailure("source-snapshot", 1)
        _finish_record(
            record,
            "committed",
            0,
            "Use this receipt for packaging or run a higher check tier.",
        )
        evidence_identity = _atomic_json(evidence, record, evidence_fd, evidence_identity)
        return record
    except BaseException as error:
        if isinstance(error, _StepFailure):
            returncode = _normalized_returncode(error.returncode)
            interrupted = error.interrupted
        elif isinstance(error, KeyboardInterrupt):
            returncode = 130
            interrupted = True
        else:
            returncode = 1
            interrupted = False
        record["error"] = f"{type(error).__name__}: {error}"
        _finish_record(
            record,
            "interrupted" if interrupted else "failed",
            returncode,
            "Inspect the receipt, then rerun the complete check.",
        )
        _atomic_json(evidence, record, evidence_fd, evidence_identity)
        return record
    finally:
        os.close(evidence_fd)


def _validate_receipt_structure(
    receipt: dict[str, Any], required_tier: str
) -> tuple[dict[str, Any], str]:
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise DevCheckError("check receipt has an unsupported schema_version")
    if receipt.get("operation") != "check":
        raise DevCheckError("receipt is not a check receipt")
    if receipt.get("state") != "committed" or receipt.get("returncode") != 0:
        raise DevCheckError("check receipt is not committed successfully")
    transaction_id = receipt.get("transaction_id")
    if not _is_lower_hex(transaction_id, 32):
        raise DevCheckError("check receipt transaction_id is invalid")
    inputs = receipt.get("inputs")
    if not isinstance(inputs, dict):
        raise DevCheckError("check receipt inputs are missing")
    _validate_check_inputs(inputs)
    observed_tier = _tier(inputs.get("tier"), "check receipt tier")
    if _TIER_RANK[observed_tier] < _TIER_RANK[required_tier]:
        raise DevCheckError(
            f"check receipt tier {observed_tier!r} is below required tier {required_tier!r}"
        )
    if observed_tier != "acceptance":
        raise DevCheckError("review package requires an acceptance check receipt")
    if not inputs.get("workloads"):
        raise DevCheckError("acceptance receipt has no workloads")
    _plus = Path(_validate_text(inputs.get("evidence"), "check receipt evidence"))
    if not _plus.is_absolute():
        raise DevCheckError("check receipt evidence path must be absolute")
    if receipt.get("steps") != _check_steps(inputs, transaction_id):
        raise DevCheckError("check receipt steps do not match its frozen inputs")
    results = receipt.get("results")
    if not isinstance(results, list) or len(results) != len(receipt["steps"]):
        raise DevCheckError("check receipt results are incomplete")
    for planned, result in zip(receipt["steps"], results, strict=True):
        if not isinstance(result, dict):
            raise DevCheckError("check receipt result is not an object")
        if any(result.get(key) != planned[key] for key in planned):
            raise DevCheckError("check receipt result does not match its planned step")
        if (
            result.get("returncode") != 0
            or result.get("timed_out") is not False
            or result.get("interrupted") is not False
            or result.get("process_group_quiescent") is not True
        ):
            raise DevCheckError("committed check receipt contains a failed step")
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        if not isinstance(stdout, dict) or not isinstance(stderr, dict):
            raise DevCheckError("check receipt result capture is missing")
        for capture in (stdout, stderr):
            if (
                not _is_lower_hex(capture.get("sha256"), 64)
                or not isinstance(capture.get("bytes"), int)
                or isinstance(capture.get("bytes"), bool)
                or capture["bytes"] < 0
                or not isinstance(capture.get("tail"), str)
                or len(capture["tail"].encode("utf-8")) > _CAPTURE_LIMIT
                or not isinstance(capture.get("truncated"), bool)
            ):
                raise DevCheckError("check receipt result capture is invalid")
        if (
            result.get("stdout_tail") != stdout["tail"]
            or result.get("stderr_tail") != stderr["tail"]
        ):
            raise DevCheckError("check receipt result capture tail differs")
    return inputs, transaction_id


def _validate_check_receipt(
    receipt: Path, expected_manifest_repo: Path, required_tier: str
) -> tuple[dict[str, Any], str]:
    value, data = _read_json_file(receipt, "check receipt")
    inputs, _transaction_id = _validate_receipt_structure(value, required_tier)
    recorded_manifest_repo = Path(
        _validate_text(inputs.get("manifest_repo"), "check receipt manifest_repo")
    )
    recorded_evidence = Path(_validate_text(inputs.get("evidence"), "check receipt evidence"))
    if not recorded_manifest_repo.is_absolute():
        raise DevCheckError("check receipt manifest_repo must be absolute")
    if recorded_evidence != receipt:
        raise DevCheckError("check receipt path does not match its recorded evidence path")
    if recorded_manifest_repo != expected_manifest_repo:
        raise DevCheckError("check receipt manifest_repo does not match package input")
    if _collect_source_snapshot(expected_manifest_repo, inputs["west_argv"], Path(inputs["source_root"])) != inputs[
        "package_snapshot"
    ]:
        raise DevCheckError("package inputs changed after the committed check")
    return value, hashlib.sha256(data).hexdigest()


def _validate_packaged_check_receipt(
    receipt: dict[str, Any], required_tier: str
) -> tuple[dict[str, Any], str]:
    inputs, transaction_id = _validate_receipt_structure(receipt, required_tier)
    _validate_source_snapshot(inputs.get("package_snapshot"))
    return inputs, transaction_id


def _package_source_step(inputs: dict[str, Any]) -> dict[str, Any]:
    repo = Path(inputs["manifest_repo"])
    return _step(
        "package-source-identity",
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        "read-only",
        _CHECK_TIMEOUTS["source-identity"],
        repo,
    )


def build_package_plan(
    manifest_repo: Path,
    west_argv: list[str],
    receipt: Path,
    output: Path,
    evidence: Path,
    required_tier: str,
) -> dict[str, Any]:
    """Validate the gate and build a frozen package plan without mutation."""
    repo = _absolute(manifest_repo, "manifest_repo")
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
        receipt_path, repo, selected_tier
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
        "steps": [_package_source_step(inputs)],
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
    required_tier = _tier(inputs.get("required_tier"), "required_tier")
    _validate_west_argv(inputs.get("west_argv"))
    if receipt == evidence:
        raise DevCheckError("check receipt and package evidence must be distinct")
    if _is_within(receipt, output) or _is_within(evidence, output):
        raise DevCheckError("receipt and package evidence must be outside package output")
    receipt_value, receipt_digest = _validate_check_receipt(
        receipt, repo, required_tier
    )
    if receipt_digest != inputs.get("receipt_sha256"):
        raise DevCheckError("check receipt changed after the package plan was built")
    if receipt_value.get("transaction_id") != inputs.get("receipt_transaction_id"):
        raise DevCheckError("check receipt transaction does not match the package plan")
    if receipt_value["inputs"].get("tier") != inputs.get("receipt_tier"):
        raise DevCheckError("check receipt tier does not match the package plan")
    if receipt_value["inputs"].get("package_snapshot") != inputs.get("package_snapshot"):
        raise DevCheckError("package source snapshot does not match the check receipt")
    expected_root = output.with_name(
        f".{output.name}.west-dev-package-{plan['transaction_id']}"
    )
    if (
        staging_root != expected_root
        or staging != staging_root / "package"
        or marker != staging_root / "owner.json"
    ):
        raise DevCheckError("package transaction paths do not match transaction identity")
    if plan["steps"] != [_package_source_step(inputs)]:
        raise DevCheckError("package step does not match the frozen inputs")
    if _path_exists(output) or _path_exists(staging_root):
        raise DevCheckError("package output or transaction root already exists")
    return repo, receipt, output, staging_root, staging, marker, evidence


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


def _parallel_file_hashes(files: dict[str, Path]) -> dict[str, tuple[str, int]]:
    if not files:
        return {}
    result: dict[str, tuple[str, int]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, max(1, os.cpu_count() or 1))
    ) as executor:
        futures = {
            executor.submit(_hash_file, path): relative
            for relative, path in files.items()
        }
        for future, relative in futures.items():
            result[relative] = future.result()
    return result


def _package_file_rows(package: Path) -> list[dict[str, Any]]:
    files, _directories = _scan_package_tree(package)
    hashes = _parallel_file_hashes(files)
    return [
        {"path": relative, "sha256": hashes[relative][0], "bytes": hashes[relative][1]}
        for relative in sorted(files)
    ]


def _write_package_payload(path: Path, data: bytes) -> None:
    if _path_exists(path):
        raise DevCheckError(f"package payload already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while creating package payload")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
            raise DevCheckError("SHA256SUMS inventory is duplicated or recursive")
        result[relative] = digest
    if list(result) != sorted(result):
        raise DevCheckError("SHA256SUMS inventory is not sorted")
    return result


def _source_snapshot_digest(snapshot: dict[str, Any]) -> str:
    return _json_digest(snapshot)


def _package_index(
    inputs: dict[str, Any],
    receipt: dict[str, Any],
    receipt_sha256: str,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    snapshot = inputs["package_snapshot"]
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "review-package",
        "manifest_repo": inputs["manifest_repo"],
        "manifest_head": snapshot["manifest_head"],
        "manifest_tree": snapshot["manifest_tree"],
        "check_receipt_sha256": receipt_sha256,
        "check_transaction_id": receipt["transaction_id"],
        "tier": receipt["inputs"]["tier"],
        "source_snapshot_sha256": _source_snapshot_digest(snapshot),
        "files": files,
    }


def _verify_owned_package_integrity(
    package: Path, expected_index: dict[str, Any]
) -> dict[str, Any]:
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
        {relative: package / relative for relative in expected_files - {"SHA256SUMS"}}
    )
    for relative, (digest, size) in hashes.items():
        if sums[relative] != digest:
            raise DevCheckError(f"owned package digest mismatch: {relative}")
        if relative in listed and (
            listed[relative]["sha256"] != digest or listed[relative]["bytes"] != size
        ):
            raise DevCheckError(f"owned package index mismatch: {relative}")
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "package-integrity",
        "state": "valid",
        "returncode": 0,
        "package": str(package),
        "file_count": len(expected_files),
        "package_index_sha256": hashlib.sha256(index_data).hexdigest(),
        "package_index": expected_index,
    }


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
        "files": {relative: binding(path) for relative, path in sorted(files.items())},
        "directories": {
            relative: binding(package / relative) for relative in sorted(directories)
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


def _review_changes(
    snapshot: dict[str, Any],
) -> list[tuple[str, Path, dict[str, Any]]]:
    changes = [
        ("manifest", Path(snapshot["manifest_repo"]), snapshot),
        ("source", Path(snapshot["source_changes"]["path"]), snapshot["source_changes"]),
    ]
    changes.extend(
        (
            f"projects/project-{quote(entry['project'], safe='')}",
            Path(entry["changes"]["path"]),
            entry["changes"],
        )
        for entry in snapshot["project_changes"]
    )
    return changes


def _write_package_contents(
    repo: Path,
    package: Path,
    receipt: Path,
    receipt_value: dict[str, Any],
    receipt_sha256: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    receipt_data = receipt.read_bytes()
    if hashlib.sha256(receipt_data).hexdigest() != receipt_sha256:
        raise DevCheckError("check receipt changed while completing review package")
    _write_package_payload(package / "check-receipt.json", receipt_data)
    _write_package_payload(
        package / "source-review.json",
        (json.dumps(snapshot, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    west_yml = repo / "west.yml"
    west_data = west_yml.read_bytes()
    if hashlib.sha256(west_data).hexdigest() != snapshot["west_yml"]["sha256"]:
        raise DevCheckError("manifest west.yml changed while completing review package")
    _write_package_payload(package / "manifest" / "west.yml", west_data)
    for owner, source_path, changes in _review_changes(snapshot):
        for suffix, snapshot_name, argv in _tracked_diff_commands(source_path):
            relative = f"changes/{owner}-{suffix}.diff"
            outcome = _run_process(
                argv,
                source_path,
                300,
                {},
                stdout_full_limit=_SNAPSHOT_DIFF_LIMIT,
            )
            data = outcome.get("_stdout_full")
            if outcome["returncode"] != 0 or not isinstance(data, bytes):
                raise DevCheckError(f"cannot capture {relative} for review package")
            if hashlib.sha256(data).hexdigest() != changes[snapshot_name]["sha256"]:
                raise DevCheckError(f"{relative} changed while completing review package")
            _write_package_payload(package / relative, data)
    files = _package_file_rows(package)
    index = _package_index(
        {"manifest_repo": str(repo), "package_snapshot": snapshot},
        receipt_value,
        receipt_sha256,
        files,
    )
    _write_package_payload(package / "package-index.json", _json_bytes(index))
    files = _package_file_rows(package)
    sums = "".join(
        f"{row['sha256']}  {row['path']}\n" for row in files
    )
    _write_package_payload(package / "SHA256SUMS", sums.encode("utf-8"))
    return index


def execute_package(plan: dict[str, Any]) -> dict[str, Any]:
    """Create a source-review package through owned staging and atomic publication."""
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
    receipt_sha256 = plan["inputs"]["receipt_sha256"]
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
    try:
        evidence_identity = _create_json(evidence, record, evidence_fd)
        record["state"] = "active"
        record["started_at"] = _utc_now()
        record["next_safe_action"] = "Wait for package publication or interrupt it once."
        evidence_identity = _atomic_json(evidence, record, evidence_fd, evidence_identity)
    except BaseException:
        os.close(output_parent_fd)
        os.close(evidence_fd)
        raise
    marker_created = False
    staging_root_fd: int | None = None
    staging_root_identity: dict[str, int] | None = None
    staging_identity: dict[str, int] | None = None
    staging_owned = False
    package_fd: int | None = None
    published_identity: dict[str, int] | None = None
    receipt_committed = False
    try:
        planned_step = plan["steps"][0]
        outcome = _run_process(
            list(planned_step["argv"]),
            Path(planned_step["cwd"]),
            int(planned_step["timeout_seconds"]),
            dict(planned_step["env"]),
        )
        result = copy.deepcopy(planned_step)
        result.update(outcome)
        record["results"].append(result)
        evidence_identity = _atomic_json(evidence, record, evidence_fd, evidence_identity)
        if outcome["returncode"] != 0:
            raise _StepFailure(planned_step["name"], outcome["returncode"])
        if (
            _collect_source_snapshot(
                repo, plan["inputs"]["west_argv"],
                Path(plan["inputs"]["package_snapshot"]["source_changes"]["path"]),
            )
            != plan["inputs"]["package_snapshot"]
        ):
            raise DevCheckError("package inputs changed during publication")
        if hashlib.sha256(receipt.read_bytes()).hexdigest() != receipt_sha256:
            raise DevCheckError("check receipt changed during publication")
        staging_root_identity, staging_root_fd = _create_package_marker(
            plan, staging_root, marker, output_parent_fd
        )
        marker_created = True
        staging_access = _proc_fd_path(staging_root_fd, staging.name)
        os.mkdir(staging.name, mode=0o700, dir_fd=staging_root_fd)
        staging_identity = _path_identity(staging_access)
        staging_owned = True
        package_fd = os.open(
            staging.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=staging_root_fd,
        )
        if _directory_identity(package_fd) != staging_identity:
            raise DevCheckError("package staging changed while pinning its directory")
        package_access = Path(f"/proc/self/fd/{package_fd}")
        index = _write_package_contents(
            repo,
            package_access,
            receipt,
            receipt_value,
            receipt_sha256,
            plan["inputs"]["package_snapshot"],
        )
        _fsync_package_tree(package_access)
        verification = _verify_owned_package_integrity(package_access, index)
        package_identity = _package_tree_identity_snapshot(package_access)
        output_access = _proc_fd_path(output_parent_fd, output.name)
        if _exists_at(output_parent_fd, output.name):
            raise DevCheckError("package output appeared before publication")
        _rename_noreplace(
            staging_root_fd, staging.name, output_parent_fd, output.name
        )
        published_identity = _identity_at(output_parent_fd, output.name)
        os.fsync(output_parent_fd)
        if _package_tree_identity_snapshot(package_access) != package_identity:
            raise DevCheckError("published package identity changed at publication")
        record["package"] = {
            "output": str(output),
            "output_identity": published_identity,
            "check_receipt": {
                "path": str(output / "check-receipt.json"),
                "source_path": str(receipt),
                "sha256": receipt_sha256,
                "transaction_id": plan["inputs"]["receipt_transaction_id"],
                "tier": plan["inputs"]["receipt_tier"],
            },
            "package_snapshot": plan["inputs"]["package_snapshot"],
            "package_index": index,
            "verification": {**verification, "package": str(output)},
            "verify_before_use": {
                "argv": [*plan["inputs"]["west_argv"], "dev", "verify-package", str(output), "--json"],
                "reason": "local package files remain mutable by their owner",
            },
        }
        _finish_record(
            record,
            "committed",
            0,
            f"Run {shlex.join(record['package']['verify_before_use']['argv'])} immediately before consuming package files.",
        )
        evidence_identity = _atomic_json(evidence, record, evidence_fd, evidence_identity)
        receipt_committed = True
        if not _same_path_identity(output, published_identity):
            raise DevCheckError("published package changed before durable receipt commit")
        record["next_safe_action"] = "Package committed; verify before use."
        evidence_identity = _atomic_json(evidence, record, evidence_fd, evidence_identity)
        return record
    except BaseException as error:
        if (
            not receipt_committed
            and published_identity is not None
            and staging_root_fd is not None
            and _same_path_identity(output, published_identity)
            and not _exists_at(staging_root_fd, staging.name)
        ):
            try:
                _rename_noreplace(
                    output_parent_fd,
                    output.name,
                    staging_root_fd,
                    staging.name,
                )
                os.fsync(output_parent_fd)
                os.fsync(staging_root_fd)
            except BaseException:
                pass
        if isinstance(error, _StepFailure):
            returncode = _normalized_returncode(error.returncode)
            interrupted = error.interrupted
        elif isinstance(error, KeyboardInterrupt):
            returncode = 130
            interrupted = True
        else:
            returncode = 1
            interrupted = False
        record["error"] = f"{type(error).__name__}: {error}"
        _finish_record(
            record,
            "interrupted" if interrupted else "failed",
            returncode,
            "Inspect the receipt, then rerun the complete package plan.",
        )
        _atomic_json(evidence, record, evidence_fd, evidence_identity)
        return record
    finally:
        if package_fd is not None:
            os.close(package_fd)
        if marker_created and staging_root_fd is not None and staging_root_identity is not None:
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
                pass
        if staging_root_fd is not None:
            os.close(staging_root_fd)
        os.close(output_parent_fd)
        os.close(evidence_fd)


def verify_package(package: Path) -> dict[str, Any]:
    """Validate a published source-review package without consulting a workspace."""
    root = Path(package).expanduser().absolute()
    if root.is_symlink() and not _is_proc_fd_root(root):
        raise DevCheckError("review package must be a regular non-symlink directory")
    files, _directories = _scan_package_tree(root)
    if "package-index.json" not in files or "SHA256SUMS" not in files:
        raise DevCheckError("review package index or SHA256SUMS is missing")
    index, index_data = _read_json_file(root / "package-index.json", "review package index")
    if set(index) != _PACKAGE_INDEX_FIELDS or index.get("schema_version") != SCHEMA_VERSION or index.get("operation") != "review-package":
        raise DevCheckError("review package index identity is invalid")
    _validate_text(index.get("manifest_repo"), "package manifest_repo")
    if not Path(index["manifest_repo"]).is_absolute():
        raise DevCheckError("package manifest_repo must be absolute")
    _require_oid(index.get("manifest_head"), "package manifest HEAD")
    _require_oid(index.get("manifest_tree"), "package manifest tree")
    _require_digest(index.get("check_receipt_sha256"), "package receipt digest")
    _require_digest(index.get("source_snapshot_sha256"), "package source digest")
    _validate_text(index.get("check_transaction_id"), "package transaction")
    if not _is_lower_hex(index["check_transaction_id"], 32):
        raise DevCheckError("package transaction_id is invalid")
    _tier(index.get("tier"), "package tier")
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
        {relative: root / relative for relative in expected_files - {"SHA256SUMS"}}
    )
    for relative, (digest, size) in hashes.items():
        if sums[relative] != digest:
            raise DevCheckError(f"SHA256SUMS digest mismatch: {relative}")
        if relative in listed and (
            listed[relative]["sha256"] != digest or listed[relative]["bytes"] != size
        ):
            raise DevCheckError(f"package index digest mismatch: {relative}")
    receipt, receipt_data = _read_json_file(root / "check-receipt.json", "packaged check receipt")
    inputs, transaction_id = _validate_packaged_check_receipt(receipt, index["tier"])
    receipt_digest = hashlib.sha256(receipt_data).hexdigest()
    if receipt_digest != index["check_receipt_sha256"]:
        raise DevCheckError("packaged check receipt differs from package index")
    if transaction_id != index["check_transaction_id"]:
        raise DevCheckError("packaged check transaction differs from package index")
    snapshot = inputs["package_snapshot"]
    if _source_snapshot_digest(snapshot) != index["source_snapshot_sha256"]:
        raise DevCheckError("packaged source snapshot differs from package index")
    source_review, _source_data = _read_json_file(root / "source-review.json", "source review")
    if source_review != snapshot:
        raise DevCheckError("packaged source review differs from check receipt")
    west_digest, _west_size = _hash_file(root / "manifest" / "west.yml")
    if west_digest != snapshot["west_yml"]["sha256"]:
        raise DevCheckError("packaged west.yml digest differs from source snapshot")
    for owner, source_path, changes in _review_changes(snapshot):
        for suffix, snapshot_name, _argv in _tracked_diff_commands(source_path):
            relative = f"changes/{owner}-{suffix}.diff"
            digest, size = _hash_file(root / relative)
            if digest != changes[snapshot_name]["sha256"] or size != changes[snapshot_name]["bytes"]:
                raise DevCheckError(f"packaged {relative} differs from source snapshot")
    derived = _package_index(
        {"manifest_repo": index["manifest_repo"], "package_snapshot": snapshot},
        receipt,
        receipt_digest,
        [
            row
            for row in _package_file_rows(root)
            if row["path"] not in {"package-index.json", "SHA256SUMS"}
        ],
    )
    if derived != index:
        raise DevCheckError("review package index bindings do not match package closure")
    if index["manifest_repo"] != inputs["manifest_repo"]:
        raise DevCheckError("review package manifest repository differs from receipt")
    if (
        index["manifest_head"] != snapshot["manifest_head"]
        or index["manifest_tree"] != snapshot["manifest_tree"]
        or index["tier"] != inputs["tier"]
    ):
        raise DevCheckError("review package source identity differs from receipt")
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "package-verify",
        "state": "valid",
        "returncode": 0,
        "package": str(root),
        "manifest_head": snapshot["manifest_head"],
        "file_count": len(expected_files),
        "package_index_sha256": hashlib.sha256(index_data).hexdigest(),
        "package_index": index,
    }

from __future__ import annotations

import ctypes
import errno
import copy
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

SCHEMA_VERSION = 1
TIER_ORDER = ("quick", "canonical", "acceptance")
_TIER_RANK = {name: rank for rank, name in enumerate(TIER_ORDER)}
_CAPTURE_LIMIT = 16 * 1024
_JSON_LIMIT = 8 * 1024 * 1024
_SNAPSHOT_DIFF_LIMIT = 64 * 1024 * 1024
_UNTRACKED_LIST_LIMIT = 1024 * 1024
_UNTRACKED_FILE_LIMIT = 16 * 1024 * 1024
_UNTRACKED_TOTAL_LIMIT = 64 * 1024 * 1024
_UNTRACKED_COUNT_LIMIT = 4096
_TERMINATE_GRACE_SECONDS = 5.0

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
    "immutable-oracle": 3600,
    "candidate-apply": 3600,
    "acceptance-capture": 300,
    "acceptance-compare": 1800,
    "acceptance-host-tier": 10800,
    "acceptance-guest-smoke": 10800,
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


def _validate_acceptance_inputs(inputs: dict[str, Any]) -> None:
    if inputs["tier"] != "acceptance":
        return
    if inputs["profile"] != "homebrew":
        raise DevCheckError("acceptance checks require profile 'homebrew'")
    if inputs["bead"] is not None or inputs["patch"] is not None:
        raise DevCheckError("acceptance checks cannot be narrowed by bead or patch")
    if inputs["prefix"] is None or inputs["build_dir"] is None:
        raise DevCheckError(
            "acceptance checks require both prefix and build_dir runtime prerequisites"
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
    if (
        head_result["returncode"] != 0
        or head_result["stdout"]["truncated"]
        or "capture_error" in head_result["stdout"]
        or "capture_error" in head_result["stderr"]
    ):
        raise DevCheckError("cannot resolve manifest repository HEAD for package snapshot")
    head = head_result["stdout_tail"].strip()
    if len(head) != 40 or any(
        character not in "0123456789abcdef" for character in head
    ):
        raise DevCheckError("manifest repository HEAD is not a full commit OID")
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
            status_argv,
            diff_argv,
            staged_argv,
            untracked_argv,
        ],
        "content": {
            name: _content_snapshot(path) for name, path in sorted(targets.items())
        },
    }


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
            [*west, "patch", "check", "--profile", profile, "--strict", "--strict-quality"],
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
    if _TIER_RANK[tier] >= _TIER_RANK["canonical"]:
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
                    [*west, "test", *selectors, "--env", "host", "--materialize-profile"],
                    "temporary/local-output",
                    _CHECK_TIMEOUTS["host-materialized-test"],
                    manifest_repo,
                ),
            )
        )
    if tier != "acceptance":
        return steps

    scratch = Path(inputs["evidence"]).parent / f".dev-check-{transaction_id}"
    control_parent = scratch / "control"
    control = control_parent / "darling-workspace"
    candidate_parent = scratch / "lock-first"
    candidate = candidate_parent / "darling-workspace"
    artifacts = scratch / "evidence"
    scratch_env = {
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
    }
    head = inputs["package_snapshot"]["manifest_head"]
    mapping = control / "locks" / "patch-stack" / "lock-first-series-v2.yml"
    oracle = artifacts / "immutable-oracle.json"
    modules = artifacts / "lock-first-modules.json"
    manifest = artifacts / "lock-first-manifest.json"
    lock_evidence = artifacts / "lock-first-evidence.json"
    comparison = artifacts / "acceptance-result.json"
    steps.extend(
        (
            _step(
                "doctor",
                [*west, "darling-doctor", "--prefix", inputs["prefix"], "--build-dir", inputs["build_dir"]],
                "read-only",
                _CHECK_TIMEOUTS["doctor"],
                manifest_repo,
            ),
            _step(
                "acceptance-clone-control",
                ["git", "clone", "--no-local", "--no-hardlinks", "--no-checkout", str(manifest_repo), str(control)],
                "temporary/local-output",
                _CHECK_TIMEOUTS["clone"],
                control_parent,
                scratch_env,
            ),
            _step(
                "acceptance-checkout-control",
                ["git", "-C", str(control), "checkout", "--detach", head],
                "temporary/local-output",
                _CHECK_TIMEOUTS["checkout"],
                control,
                scratch_env,
            ),
            _step(
                "acceptance-clone-candidate",
                ["git", "clone", "--no-local", "--no-hardlinks", "--no-checkout", str(manifest_repo), str(candidate)],
                "temporary/local-output",
                _CHECK_TIMEOUTS["clone"],
                candidate_parent,
                scratch_env,
            ),
            _step(
                "acceptance-checkout-candidate",
                ["git", "-C", str(candidate), "checkout", "--detach", head],
                "temporary/local-output",
                _CHECK_TIMEOUTS["checkout"],
                candidate,
                scratch_env,
            ),
            _step(
                "acceptance-bootstrap-candidate",
                [str(candidate / "ci" / "bootstrap-west.sh")],
                "temporary/local-output",
                _CHECK_TIMEOUTS["bootstrap"],
                candidate,
                scratch_env,
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
                scratch_env,
            ),
            _step(
                "immutable-oracle",
                ["python3", str(control / "tests" / "patch_stack_immutable_oracle.py"), "--workspace", str(control), "--profile", profile, "--mapping", str(mapping), "--output", str(oracle)],
                "temporary/local-output",
                _CHECK_TIMEOUTS["immutable-oracle"],
                control,
                scratch_env,
            ),
            _step(
                "acceptance-candidate-apply",
                [*west, "patch", "apply", "--profile", profile, "--lock-first-evidence", str(lock_evidence)],
                "temporary/local-output",
                _CHECK_TIMEOUTS["candidate-apply"],
                candidate,
                scratch_env,
            ),
            _step(
                "acceptance-capture",
                ["python3", str(candidate / "ci" / "patch_stack_acceptance.py"), "capture", "--workspace", str(candidate), "--profile", profile, "--modules", str(modules), "--manifest", str(manifest)],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-capture"],
                candidate,
                scratch_env,
            ),
            _step(
                "acceptance-compare",
                ["python3", str(candidate / "ci" / "patch_stack_lock_first_acceptance.py"), "compare-immutable-oracle", "--oracle", str(oracle), "--candidate", str(modules), "--candidate-manifest", str(manifest), "--evidence", str(lock_evidence), "--mapping", str(candidate / "locks" / "patch-stack" / "lock-first-series-v2.yml"), "--candidate-workspace", str(candidate_parent), "--manifest-workspace", str(candidate), "--transaction-root", str(scratch), "--result", str(comparison)],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-compare"],
                candidate,
                scratch_env,
            ),
            _step(
                "acceptance-host-tier",
                [str(candidate / "ci" / "run-test-tier.sh"), "host"],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-host-tier"],
                candidate,
                scratch_env,
            ),
            _step(
                "acceptance-guest-smoke",
                [str(candidate / "ci" / "run-test-tier.sh"), "guest-smoke"],
                "temporary/local-output",
                _CHECK_TIMEOUTS["acceptance-guest-smoke"],
                candidate,
                scratch_env,
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
    }
    inputs["package_snapshot"] = _collect_package_snapshot(repo, selected_profile)
    if selected_tier == "acceptance" and inputs["package_snapshot"]["dirty"]:
        raise DevCheckError("acceptance checks require a clean manifest repository")
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


def _run_process(
    argv: list[str],
    cwd: Path,
    timeout_seconds: int,
    env_overrides: dict[str, str],
    stdout_full_limit: int = 0,
) -> dict[str, Any]:
    """Run one process with bounded capture and an independently killable group."""
    started_at = _utc_now()
    started = time.monotonic()
    stdout_capture = _BoundedCapture(full_limit=stdout_full_limit)
    stderr_capture = _BoundedCapture()
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
    returncode: int
    try:
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process, signal.SIGTERM)
            returncode = 124
        except KeyboardInterrupt:
            interrupted = True
            _terminate_process_group(process, signal.SIGINT)
            returncode = 130
    except BaseException:
        _terminate_process_group(process, signal.SIGTERM)
        raise
    finally:
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
    }
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
        for name in ("control", "lock-first", "evidence", "home", "tmp", "cache"):
            (scratch / name).mkdir(mode=0o700)
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
    }
    result: dict[str, Any] = {}
    for name, path in paths.items():
        value, data = _read_json_file(path, f"acceptance artifact {name}")
        if value.get("verdict") != "VALID":
            raise DevCheckError(f"acceptance artifact {name} verdict is not VALID")
        if name == "immutable_oracle":
            valid_schema = value.get("oracle_schema_version") == 2
            valid_schema = valid_schema and value.get("mode") == "immutable-cherry-pick-oracle"
            valid_schema = valid_schema and value.get("profile") == "homebrew"
        else:
            valid_schema = value.get("evidence_schema_version") == 2
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
    return result


def _validate_embedded_acceptance_artifacts(
    artifacts: object, profile: str
) -> None:
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "immutable_oracle",
        "lock_first_evidence",
        "comparison",
    }:
        raise DevCheckError("acceptance receipt artifacts are incomplete")
    for name, row in artifacts.items():
        if not isinstance(row, dict):
            raise DevCheckError(f"embedded acceptance artifact {name} is invalid")
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
        if not isinstance(value, dict) or value.get("verdict") != "VALID":
            raise DevCheckError(f"embedded acceptance artifact {name} verdict is invalid")
        if name == "immutable_oracle":
            valid_schema = (
                value.get("oracle_schema_version") == 2
                and value.get("mode") == "immutable-cherry-pick-oracle"
                and value.get("profile") == profile
            )
        else:
            valid_schema = value.get("evidence_schema_version") == 2
        if not valid_schema:
            raise DevCheckError(f"embedded acceptance artifact {name} schema is invalid")


class _StepFailure(DevCheckError):
    def __init__(self, name: str, returncode: int, interrupted: bool = False) -> None:
        super().__init__(f"{name} failed with returncode {returncode}")
        self.returncode = returncode
        self.interrupted = interrupted

def _normalized_returncode(returncode: int) -> int:
    return 128 + (-returncode) if returncode < 0 else returncode


def execute_check(plan: dict[str, Any]) -> dict[str, Any]:
    """Execute a frozen check plan and return its durable receipt."""
    repo, evidence = _validate_check_plan(plan)
    evidence_fd = _open_pinned_directory(
        evidence.parent, plan["inputs"]["evidence_parent_identity"]
    )
    record = _active_record(plan)
    record["_started_monotonic"] = time.monotonic()
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
    try:
        if plan["inputs"]["tier"] == "acceptance":
            scratch, scratch_identity = _create_check_scratch(plan)
            record["scratch"] = {
                "path": str(scratch),
                "identity": scratch_identity,
                "state": "active",
            }
            evidence_identity = _atomic_json(
                evidence, record, evidence_fd, evidence_identity
            )
        for planned_step in plan["steps"]:
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
            oracle = result.get("selection_oracle")
            if isinstance(oracle, dict) and not oracle["selected"]:
                raise _StepFailure("test-list-selection-oracle", 1)
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
    if _collect_package_snapshot(expected_manifest_repo, profile) != snapshot:
        raise DevCheckError("package inputs changed after the committed check")
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
        if result.get("returncode") != 0:
            raise DevCheckError("committed check receipt contains a failed step")
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        if not isinstance(stdout, dict) or not isinstance(stderr, dict):
            raise DevCheckError("check receipt result capture is missing")
        for capture in (stdout, stderr):
            digest = capture.get("sha256")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or not isinstance(capture.get("tail"), str)
                or not isinstance(capture.get("bytes"), int)
                or capture["bytes"] < 0
            ):
                raise DevCheckError("check receipt result capture is invalid")
    if observed_tier == "acceptance":
        _validate_embedded_acceptance_artifacts(
            value.get("acceptance_artifacts"), profile
        )
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


def _verify_export_evidence(output: Path, profile: str) -> dict[str, Any]:
    if output.is_symlink() or not output.is_dir():
        raise DevCheckError("export-locks did not create a regular output directory")
    evidence_path = output / "evidence.json"
    value, data = _read_json_file(evidence_path, "export-locks evidence")
    if value.get("export_schema_version") != 1:
        raise DevCheckError("export-locks evidence has an unsupported schema version")
    if value.get("profile") != profile:
        raise DevCheckError("export-locks evidence profile does not match")
    if value.get("verdict") != "VALID":
        raise DevCheckError("export-locks evidence verdict is not VALID")
    if value.get("mode") != "immutable-lock-format-patch":
        raise DevCheckError("export-locks evidence mode is not immutable-lock-format-patch")
    batch_id = value.get("batch_id")
    expected_count = value.get("expected_count")
    module_order = value.get("module_order")
    series_order = value.get("series_order")
    series = value.get("series")
    clean_odb = value.get("clean_odb")
    if (
        not isinstance(batch_id, str)
        or not batch_id
        or not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 0
        or not isinstance(module_order, list)
        or not all(isinstance(item, str) and item for item in module_order)
        or not isinstance(series_order, list)
        or not isinstance(series, list)
        or len(series_order) != expected_count
        or len(series) != expected_count
        or not isinstance(clean_odb, dict)
        or any(clean_odb.get(key) != 0 for key in ("alternates", "shallow", "partial"))
    ):
        raise DevCheckError("export-locks evidence structure is invalid")
    observed_order: list[dict[str, str]] = []
    observed_mboxes: set[str] = set()
    for row in series:
        if not isinstance(row, dict):
            raise DevCheckError("export-locks evidence series row is invalid")
        module = row.get("module")
        patch = row.get("patch")
        relative_text = row.get("mbox")
        expected_digest = row.get("sha256")
        if (
            not isinstance(module, str)
            or not module
            or not isinstance(patch, str)
            or not patch
            or not isinstance(relative_text, str)
            or not relative_text
            or not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_digest
            )
        ):
            raise DevCheckError("export-locks evidence series identity is invalid")
        relative = Path(relative_text)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative_text in observed_mboxes
        ):
            raise DevCheckError(
                "export-locks evidence mbox path is unsafe or duplicated"
            )
        observed_mboxes.add(relative_text)
        mbox = output / relative
        try:
            metadata = mbox.lstat()
        except FileNotFoundError as error:
            raise DevCheckError(f"export-locks mbox is missing: {relative_text}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise DevCheckError(f"export-locks mbox is not a regular file: {relative_text}")
        digest = hashlib.sha256()
        try:
            with mbox.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise DevCheckError(f"cannot read export-locks mbox {relative_text}: {error}") from error
        if digest.hexdigest() != expected_digest:
            raise DevCheckError(f"export-locks mbox digest mismatch: {relative_text}")
        observed_order.append({"module": module, "patch": patch})
    if observed_order != series_order:
        raise DevCheckError("export-locks series order does not match its evidence")
    return {
        "path": str(evidence_path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "export_schema_version": value["export_schema_version"],
        "profile": value["profile"],
        "batch_id": batch_id,
        "verdict": value["verdict"],
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        outcome = _run_process(
            list(planned_step["argv"]),
            Path(planned_step["cwd"]),
            int(planned_step["timeout_seconds"]),
            dict(planned_step["env"]),
        )
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
        record["staging"] = {
            "path": str(staging),
            "identity": staging_identity,
            "state": "active",
        }
        evidence_identity = _atomic_json(
            evidence, record, evidence_fd, evidence_identity
        )
        exported = _verify_export_evidence(
            staging_access, plan["inputs"]["profile"]
        )
        if _collect_package_snapshot(repo, plan["inputs"]["profile"]) != plan["inputs"]["package_snapshot"]:
            raise DevCheckError("package inputs changed during export")
        if not _same_path_identity(staging_access, staging_identity):
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
        if not _same_path_identity(
            output.parent, plan["inputs"]["output_parent_identity"]
        ):
            raise DevCheckError("package output parent changed during publication")
        record["staging"]["state"] = "published"
        record["ownership"]["state"] = "retained-through-durable-commit"
        record["package"] = {
            "output": str(output),
            "output_identity": published_identity,
            "export_evidence": {
                **exported,
                "path": str(output / "evidence.json"),
            },
            "check_receipt": {
                "path": str(receipt),
                "sha256": plan["inputs"]["receipt_sha256"],
                "transaction_id": plan["inputs"]["receipt_transaction_id"],
                "tier": plan["inputs"]["receipt_tier"],
            },
            "package_snapshot": plan["inputs"]["package_snapshot"],
        }
        _finish_record(
            record,
            "committed",
            0,
            "Review or publish the immutable package at the recorded output path.",
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
        if staging_root_fd is not None:
            os.close(staging_root_fd)
        os.close(output_parent_fd)
        os.close(evidence_fd)

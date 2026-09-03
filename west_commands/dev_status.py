"""Bounded, read-only status collection for the ``west dev`` facade."""

from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

try:
    from .test_worktrees import path_has_west_temp_component
except ImportError:  # West also loads extension modules outside their package.
    from test_worktrees import path_has_west_temp_component


SCHEMA_VERSION = 1
PROCESS_TIMEOUT_SECONDS = 20.0
GIT_TIMEOUT_SECONDS = 8.0
JOB_STATUS_TIMEOUT_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 0.5
DISCOVERY_TIMEOUT_SECONDS = 1.0
MAX_CAPTURE_BYTES = 64 * 1024
MAX_PROJECTS = 256
MAX_STATUS_ENTRIES = 64
MAX_WORKTREES = 512
MAX_DISCOVERY_FILES = 128
MAX_DISCOVERY_ENTRIES_SCANNED = 1024
MAX_REGISTRY_ENTRIES_SCANNED = 512
MAX_REGISTRY_ENTRIES = 128
MAX_JSON_FILE_BYTES = 256 * 1024
MAX_REGISTRY_FIELD_BYTES = 16 * 1024


class StatusError(ValueError):
    """The core West workspace is not valid enough to inspect."""


def _absolute(path: Path) -> Path:
    return path.expanduser().resolve()


def _append_capture(buffer: bytearray, chunk: bytes) -> bool:
    available = MAX_CAPTURE_BYTES - len(buffer)
    if available > 0:
        buffer.extend(chunk[:available])
    return len(chunk) > available


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _run_bounded(
    argv: Iterable[str],
    *,
    cwd: Path,
    timeout: float = PROCESS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one read-only authority without permitting unbounded pipe capture."""

    command = [str(part) for part in argv]
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={
                **os.environ,
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except OSError as error:
        encoded_error = str(error).encode("utf-8", errors="replace")
        return {
            "argv": command,
            "rc": 127,
            "stdout": "",
            "stderr": encoded_error[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace"),
            "stdout_truncated": False,
            "stderr_truncated": len(encoded_error) > MAX_CAPTURE_BYTES,
            "timed_out": False,
        }
    stdout = bytearray()
    stderr = bytearray()
    stdout_truncated = False
    stderr_truncated = False
    timed_out = False
    selector = selectors.DefaultSelector()
    try:
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = started + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not timed_out:
                timed_out = True
                _stop_process_group(process)
                break
            events = selector.select(max(0.0, min(remaining, 0.1)))
            if not events and process.poll() is not None:
                events = selector.select(0)
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 16 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if key.data == "stdout":
                    stdout_truncated |= _append_capture(stdout, chunk)
                else:
                    stderr_truncated |= _append_capture(stderr, chunk)
        returncode = process.poll() if timed_out else process.wait()
    except BaseException:
        _stop_process_group(process)
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    return {
        "argv": command,
        "rc": 124 if timed_out else returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "timed_out": timed_out,
    }


def _command_section(authority: str, result: dict[str, Any]) -> dict[str, Any]:
    complete = (
        result["rc"] == 0
        and not result["stdout_truncated"]
        and not result["stderr_truncated"]
    )
    return {
        "authority": authority,
        "health": "healthy" if complete else "degraded",
        **result,
    }


def _json_command_section(authority: str, result: dict[str, Any]) -> dict[str, Any]:
    section = _command_section(authority, result)
    if result["rc"] != 0:
        return section
    if result["stdout_truncated"]:
        section["health"] = "degraded"
        section["json_error"] = "JSON output exceeded the capture limit"
        return section
    try:
        section["data"] = json.loads(result["stdout"])
    except json.JSONDecodeError as error:
        section["health"] = "degraded"
        section["json_error"] = f"invalid JSON at line {error.lineno} column {error.colno}"
    return section


def _unavailable(authority: str, reason: str) -> dict[str, Any]:
    return {"authority": authority, "health": "unavailable", "reason": reason}


def _project_active(manifest: Any, project: Any) -> bool:
    checker = getattr(manifest, "is_active", None)
    if not callable(checker):
        raise StatusError("West manifest has no is_active() authority")
    try:
        return bool(checker(project))
    except Exception as error:
        raise StatusError(f"West manifest cannot determine active projects: {error}") from error


def _manifest_snapshot(topdir: Path, manifest_repo: Path, manifest: Any) -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    try:
        raw_projects = list(manifest.projects)
    except Exception as error:
        raise StatusError(f"invalid West manifest projects: {error}") from error

    records: list[dict[str, Any]] = []
    repositories: list[tuple[str, Path]] = [("manifest", manifest_repo)]
    for project in raw_projects:
        name = getattr(project, "name", None)
        revision = getattr(project, "revision", None)
        project_path = getattr(project, "abspath", None)
        if not isinstance(name, str) or not name or not isinstance(revision, str) or not revision:
            raise StatusError("West manifest contains a project without a valid name or revision")
        if project_path is None:
            relative = getattr(project, "path", None)
            if not isinstance(relative, str) or not relative:
                raise StatusError(f"West project {name!r} has no valid path")
            project_path = topdir / relative
        path = _absolute(Path(project_path))
        active = _project_active(manifest, project)
        groups = getattr(project, "groups", ()) or ()
        if isinstance(groups, str):
            groups = (groups,)
        record = {
            "name": name,
            "path": str(path),
            "revision": revision,
            "active": active,
            "groups": sorted(str(group) for group in groups),
        }
        records.append(record)
        if active and name != "manifest":
            repositories.append((name, path))

    records.sort(key=lambda item: (item["path"], item["name"]))
    truncated = len(records) > MAX_PROJECTS
    records = records[:MAX_PROJECTS]

    unique_repositories: dict[str, tuple[str, Path]] = {}
    for name, path in repositories:
        unique_repositories.setdefault(str(path), (name, path))
    ordered_repositories = sorted(unique_repositories.values(), key=lambda item: (str(item[1]), item[0]))
    repositories_truncated = len(ordered_repositories) > MAX_PROJECTS
    ordered_repositories = ordered_repositories[:MAX_PROJECTS]

    health = "degraded" if truncated or repositories_truncated else "healthy"
    section = {
        "authority": "West manifest",
        "health": health,
        "manifest_repo": str(manifest_repo),
        "project_count": len(raw_projects),
        "projects": records,
        "truncated": truncated,
    }
    return section, ordered_repositories


def _parse_git_status(output: str) -> dict[str, Any]:
    head: str | None = None
    branch: str | None = None
    entries: list[str] = []
    entry_count = 0
    for record in output.split("\0"):
        if not record:
            continue
        if record.startswith("# branch.oid "):
            value = record.removeprefix("# branch.oid ")
            head = None if value == "(initial)" else value
        elif record.startswith("# branch.head "):
            value = record.removeprefix("# branch.head ")
            branch = None if value == "(detached)" else value
        elif not record.startswith("# "):
            entry_count += 1
            if len(entries) < MAX_STATUS_ENTRIES:
                entries.append(record)
    return {
        "head": head,
        "branch": branch,
        "dirty": bool(entry_count),
        "entry_count": entry_count,
        "entries": entries,
        "entries_truncated": entry_count > len(entries),
    }


def _git_snapshots(repositories: list[tuple[str, Path]]) -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    degraded = False
    for name, path in repositories:
        if not path.is_dir():
            degraded = True
            snapshots.append({
                "authority": "git status --porcelain=v2 --branch -z",
                "health": "degraded",
                "name": name,
                "path": str(path),
                "reason": "repository path is unavailable",
            })
            continue
        result = _run_bounded(
            ["git", "-C", str(path), "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal"],
            cwd=path,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        item = {
            **_command_section("git status --porcelain=v2 --branch -z", result),
            "name": name,
            "path": str(path),
        }
        if item["health"] == "healthy":
            item.update(_parse_git_status(result["stdout"]))
        else:
            degraded = True
        snapshots.append(item)
    return {
        "authority": "Git porcelain v2",
        "health": "degraded" if degraded else "healthy",
        "repositories": snapshots,
        "truncated": False,
    }


def _parse_worktree_porcelain(output: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                records.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = str(_absolute(Path(value)))
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key in {"bare", "detached", "locked", "prunable"}:
            current[key] = value or True
    return records


def _temporary_worktrees(repositories: list[tuple[str, Path]]) -> dict[str, Any]:
    found: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    for name, repo in repositories:
        result = _run_bounded(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            cwd=repo,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result["rc"] != 0 or result["stdout_truncated"]:
            failures.append({"name": name, "path": str(repo), **result})
            continue
        for record in _parse_worktree_porcelain(result["stdout"]):
            path_text = record.get("path")
            if path_text and path_has_west_temp_component(Path(path_text)):
                record["repository"] = str(repo)
                found.setdefault(path_text, record)
    worktrees = [found[key] for key in sorted(found)[:MAX_WORKTREES]]
    health = "degraded" if failures else ("healthy" if worktrees else "empty")
    return {
        "authority": "git worktree list --porcelain and west test temporary-worktree naming",
        "health": health,
        "worktrees": worktrees,
        "failures": failures,
        "truncated": len(found) > len(worktrees),
    }


def _candidate_roots(
    topdir: Path,
    manifest_repo: Path,
    prefix: Path | None,
    build_dir: Path | None,
) -> list[Path]:
    candidates = {topdir, topdir.parent, manifest_repo, manifest_repo.parent}
    if prefix is not None:
        candidates.update((prefix, prefix.parent))
    if build_dir is not None:
        candidates.update((build_dir, build_dir.parent))
    return sorted(candidates, key=str)


def _bounded_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None, "not a regular non-symlink file"
        if metadata.st_size > MAX_JSON_FILE_BYTES:
            return None, f"file exceeds {MAX_JSON_FILE_BYTES} bytes"
        with path.open("rb") as handle:
            raw = handle.read(MAX_JSON_FILE_BYTES + 1)
        if len(raw) > MAX_JSON_FILE_BYTES:
            return None, f"file exceeds {MAX_JSON_FILE_BYTES} bytes"
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, str(error)
    if not isinstance(payload, dict):
        return None, "JSON root is not an object"
    return payload, None


def _discover_paths(roots: list[Path], patterns: tuple[str, ...]) -> tuple[list[Path], bool]:
    discovered: dict[str, Path] = {}
    scanned = 0
    truncated = False
    deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
    stop = False
    for root in roots:
        if stop or not root.is_dir() or root.is_symlink():
            continue
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if (
                        scanned >= MAX_DISCOVERY_ENTRIES_SCANNED
                        or time.monotonic() >= deadline
                    ):
                        truncated = True
                        stop = True
                        break
                    scanned += 1
                    if not any(fnmatch.fnmatchcase(entry.name, pattern) for pattern in patterns):
                        continue
                    absolute = Path(os.path.abspath(entry.path))
                    if str(absolute) in discovered:
                        continue
                    if len(discovered) >= MAX_DISCOVERY_FILES:
                        truncated = True
                        stop = True
                        break
                    discovered[str(absolute)] = absolute
        except OSError:
            continue
    if truncated:
        return [], True
    return [discovered[key] for key in sorted(discovered)], False


def _source_worktree_records(roots: list[Path]) -> dict[str, Any]:
    paths, truncated = _discover_paths(roots, (".west-source-worktree-*.json",))
    records: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for path in paths:
        payload, error = _bounded_json(path)
        if error is not None or payload is None:
            invalid.append({"path": str(path), "error": error or "invalid record"})
            continue
        if payload.get("version") != 1 or not isinstance(payload.get("gitlinks"), list):
            invalid.append({"path": str(path), "error": "unsupported source-worktree record"})
            continue
        records.append({"path": str(path), "record": payload})
    health = "degraded" if invalid or truncated else ("healthy" if records else "empty")
    return {
        "authority": "west_commands.source_worktree record version 1",
        "health": health,
        "records": records,
        "invalid": invalid,
        "truncated": truncated,
    }


def _red_proof_deploy_manifests() -> tuple[list[Path], list[dict[str, str]], bool]:
    """Discover runtime proof manifests without following attacker-owned paths."""

    invalid: list[dict[str, str]] = []
    try:
        temp_root = _absolute(Path(os.environ.get("TMPDIR") or "/tmp"))
    except (OSError, RuntimeError, ValueError) as error:
        invalid.append({"path": os.environ.get("TMPDIR") or "/tmp", "error": str(error)})
        return [], invalid, False
    paths: list[Path] = []
    scanned = 0
    truncated = False
    deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
    try:
        with os.scandir(temp_root) as entries:
            for entry in entries:
                if (
                    scanned >= MAX_DISCOVERY_ENTRIES_SCANNED
                    or time.monotonic() >= deadline
                ):
                    truncated = True
                    break
                scanned += 1
                if not fnmatch.fnmatchcase(entry.name, "west-red-proof-deploy-*"):
                    continue
                directory = Path(os.path.abspath(entry.path))
                try:
                    metadata = directory.lstat()
                except OSError as error:
                    invalid.append({"path": str(directory), "error": str(error)})
                    continue
                if metadata.st_uid != os.getuid():
                    continue
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    invalid.append({
                        "path": str(directory),
                        "error": "owner-held proof deployment path is not a real directory",
                    })
                    continue
                manifest = directory / "manifest.json"
                try:
                    manifest_metadata = manifest.lstat()
                except FileNotFoundError:
                    continue
                except OSError as error:
                    invalid.append({"path": str(manifest), "error": str(error)})
                    continue
                if manifest_metadata.st_uid != os.getuid():
                    continue
                if (
                    stat.S_ISLNK(manifest_metadata.st_mode)
                    or not stat.S_ISREG(manifest_metadata.st_mode)
                ):
                    invalid.append({
                        "path": str(manifest),
                        "error": "owner-held proof deployment manifest is not a regular file",
                    })
                    continue
                if len(paths) >= MAX_DISCOVERY_FILES:
                    truncated = True
                    break
                paths.append(manifest)
    except OSError:
        pass
    if truncated:
        return [], [], True
    return sorted(paths, key=str), sorted(invalid, key=lambda item: item["path"]), False


def _deployment_transactions(roots: list[Path], prefix: Path | None) -> dict[str, Any]:
    general_paths, truncated = _discover_paths(roots, ("*.json",))
    proof_paths, proof_invalid, proof_truncated = _red_proof_deploy_manifests()
    combined = {str(path): path for path in (*general_paths, *proof_paths)}
    ordered_paths = [combined[key] for key in sorted(combined)]
    if len(ordered_paths) > MAX_DISCOVERY_FILES:
        ordered_paths = []
        truncated = True
    truncated |= proof_truncated
    if truncated:
        ordered_paths = []
        proof_invalid = []
    transactions: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = list(proof_invalid)
    active: list[str] = []
    for path in ordered_paths:
        payload, error = _bounded_json(path)
        if error is not None or payload is None:
            # All JSON names are considered because deploy manifests are caller-named.
            # Only conventional names are classifiable when their content is unreadable.
            lowered_name = path.name.lower()
            if (
                path in proof_paths
                or "deploy" in lowered_name
                or "transaction" in lowered_name
            ):
                invalid.append({"path": str(path), "error": error or "invalid manifest"})
            continue
        identifying_keys = {"version", "state", "prefix", "roots", "entries", "directories"}
        if not identifying_keys.issubset(payload):
            continue
        if (
            payload.get("version") != 1
            or not isinstance(payload.get("entries"), list)
            or not isinstance(payload.get("state"), str)
            or not isinstance(payload.get("prefix"), str)
        ):
            invalid.append({"path": str(path), "error": "unsupported deployment transaction"})
            continue
        try:
            recorded_prefix = _absolute(Path(payload["prefix"]))
        except (OSError, RuntimeError, ValueError) as error:
            invalid.append({"path": str(path), "error": f"invalid prefix: {error}"})
            continue
        if prefix is not None and recorded_prefix != prefix:
            continue
        if payload["state"] not in {"active", "committed", "restored"}:
            invalid.append({
                "path": str(path),
                "error": f"unsupported deployment transaction state: {payload['state']!r}",
            })
            continue
        is_active = payload["state"] == "active"
        transaction_health = "busy" if is_active else "healthy"
        transactions.append({
            "authority": "west_commands.deploy_transaction manifest version 1",
            "health": transaction_health,
            "state": "in_progress" if is_active else payload["state"],
            "path": str(path),
            "manifest": payload,
        })
        if is_active:
            active.append(str(path))
    health = (
        "degraded"
        if invalid or truncated
        else ("busy" if active else ("healthy" if transactions else "empty"))
    )
    return {
        "authority": "west_commands.deploy_transaction manifest version 1",
        "health": health,
        "in_progress": bool(active),
        "active": active,
        "transactions": transactions,
        "invalid": invalid,
        "truncated": truncated,
    }


def _read_small_text(path: Path) -> tuple[str | None, str | None]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None, "not a regular non-symlink file"
        if metadata.st_size > MAX_REGISTRY_FIELD_BYTES:
            return None, f"field exceeds {MAX_REGISTRY_FIELD_BYTES} bytes"
        with path.open("rb") as handle:
            raw = handle.read(MAX_REGISTRY_FIELD_BYTES + 1)
        if len(raw) > MAX_REGISTRY_FIELD_BYTES:
            return None, f"field exceeds {MAX_REGISTRY_FIELD_BYTES} bytes"
        return raw.decode("utf-8", errors="replace").rstrip("\n"), None
    except OSError as error:
        return None, str(error)




def _proc_start_time(pid: int) -> str | None:
    try:
        with Path(f"/proc/{pid}/stat").open("r", encoding="utf-8", errors="replace") as handle:
            fields = handle.read(MAX_REGISTRY_FIELD_BYTES).split()
    except OSError:
        return None
    return fields[21] if len(fields) > 21 else None


def _job_registry_roots(
    topdir: Path,
    manifest_repo: Path,
    prefix: Path | None,
    build_dir: Path | None,
) -> tuple[list[Path], list[str], list[dict[str, str]]]:
    roots = set(_candidate_roots(topdir, manifest_repo, prefix, build_dir))
    configured: list[str] = []
    invalid: list[dict[str, str]] = []
    try:
        roots.add(_absolute(Path(os.environ.get("TMPDIR") or "/tmp")))
    except (OSError, RuntimeError, ValueError):
        pass
    registry_root = os.environ.get("WEST_JOB_REGISTRY_ROOT")
    if registry_root:
        try:
            roots.add(_absolute(Path(registry_root)))
        except (OSError, RuntimeError, ValueError) as error:
            invalid.append({"path": registry_root, "error": str(error)})
    state_dir = os.environ.get("WEST_JOB_STATE_DIR")
    if state_dir:
        try:
            roots.add(_absolute(Path(state_dir)).parent)
        except (OSError, RuntimeError, ValueError):
            pass
    for value in (os.environ.get("WEST_DEV_JOB_STATE_ROOTS") or "").split(os.pathsep):
        if not value:
            continue
        try:
            candidate = Path(os.path.abspath(Path(value).expanduser()))
            metadata = candidate.lstat()
        except (OSError, RuntimeError, ValueError) as error:
            invalid.append({"path": value, "error": str(error)})
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            invalid.append({"path": str(candidate), "error": "configured state root is not a real directory"})
            continue
        path = candidate
        roots.add(path)
        configured.append(str(path))
    return sorted(roots, key=str), sorted(set(configured)), invalid


def _long_jobs(
    roots: list[Path],
    manifest_repo: Path,
    configured_roots: list[str],
    root_errors: list[dict[str, str]],
) -> dict[str, Any]:
    registries: list[str] = []
    snapshots: list[tuple[Path, Path, dict[str, str]]] = []
    jobs: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = list(root_errors)
    busy_registries: list[str] = []
    truncated = False
    coverage_degraded = bool(root_errors)
    status_script = manifest_repo / "scripts" / "west-job.sh"
    discovery_deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
    scanned = 0
    stop = False
    owner_uid = os.getuid()
    for root in roots:
        if stop:
            break
        registry = root / ".west-job-registry"
        registry_text = str(_absolute(registry))
        try:
            registry_metadata = registry.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            invalid.append({"path": registry_text, "error": str(error)})
            coverage_degraded = True
            continue
        if (
            stat.S_ISLNK(registry_metadata.st_mode)
            or not stat.S_ISDIR(registry_metadata.st_mode)
            or registry_metadata.st_uid != owner_uid
        ):
            coverage_degraded = True
            invalid.append({
                "path": registry_text,
                "error": "registry is not an owner-held real directory",
            })
            continue

        lock_path = registry / ".lock"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            lock_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_uid != owner_uid
            ):
                raise OSError("registry lock is not an owner-held regular file")
            lock_handle = os.fdopen(descriptor, "rb")
            descriptor = None
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            invalid.append({"path": str(lock_path), "error": str(error)})
            coverage_degraded = True
            continue
        try:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                busy_registries.append(registry_text)
                continue
            except OSError as error:
                invalid.append({"path": str(lock_path), "error": str(error)})
                coverage_degraded = True
                continue

            registries.append(registry_text)
            entry_paths: list[Path] = []
            try:
                with os.scandir(registry) as entries:
                    for directory_entry in entries:
                        if (
                            scanned >= MAX_REGISTRY_ENTRIES_SCANNED
                            or time.monotonic() >= discovery_deadline
                        ):
                            truncated = True
                            stop = True
                            break
                        scanned += 1
                        if directory_entry.name == ".lock":
                            continue
                        if len(snapshots) + len(entry_paths) >= MAX_REGISTRY_ENTRIES:
                            truncated = True
                            stop = True
                            break
                        entry_paths.append(Path(os.path.abspath(directory_entry.path)))
            except OSError as error:
                invalid.append({"path": registry_text, "error": str(error)})
                coverage_degraded = True
                continue

            for entry in sorted(entry_paths, key=str):
                try:
                    entry_metadata = entry.lstat()
                except OSError as error:
                    invalid.append({"path": str(entry), "error": str(error)})
                    continue
                if (
                    stat.S_ISLNK(entry_metadata.st_mode)
                    or not stat.S_ISDIR(entry_metadata.st_mode)
                    or entry_metadata.st_uid != owner_uid
                ):
                    invalid.append({
                        "path": str(entry),
                        "error": "registry entry is not an owner-held real directory",
                    })
                    continue
                values: dict[str, str] = {}
                entry_error: str | None = None
                for field in ("state-dir", "pid", "start-time", "command"):
                    value, error = _read_small_text(entry / field)
                    if error is not None or value is None:
                        entry_error = f"{field}: {error or 'missing'}"
                        break
                    values[field] = value
                if entry_error is not None:
                    invalid.append({"path": str(entry), "error": entry_error})
                    continue
                snapshots.append((registry, entry, values))
        finally:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()

    if truncated:
        registries = []
        snapshots = []
        invalid = list(root_errors)
        busy_registries = []

    live_jobs: list[str] = []
    item_degraded = False
    status_degraded = False
    for registry, entry, values in sorted(snapshots, key=lambda item: str(item[1])):
        try:
            pid = int(values["pid"])
        except ValueError:
            invalid.append({"path": str(entry), "error": "pid is not an integer"})
            continue
        raw_state_path = Path(values["state-dir"])
        try:
            state_path = (
                _absolute(raw_state_path)
                if raw_state_path.is_absolute()
                else _absolute(registry.parent / raw_state_path.name)
            )
        except (OSError, RuntimeError, ValueError) as error:
            invalid.append({"path": str(entry), "error": f"invalid state-dir: {error}"})
            continue
        status = _command_section(
            "scripts/west-job.sh status",
            _run_bounded(
                [str(status_script), "status", "--state-dir", str(state_path)],
                cwd=manifest_repo,
                timeout=JOB_STATUS_TIMEOUT_SECONDS,
            ),
        )
        status_degraded |= status["health"] == "degraded"
        rc_text, _rc_error = _read_small_text(state_path / "rc")
        recorded_rc: int | None = None
        if rc_text is not None:
            try:
                recorded_rc = int(rc_text)
            except ValueError:
                pass
        live = pid > 0 and _proc_start_time(pid) == values["start-time"]
        state = "in_progress" if live else ("completed" if recorded_rc is not None else "unknown")
        if live:
            item_health = "busy"
        elif status["health"] == "degraded" or recorded_rc != 0:
            item_health = "degraded"
        else:
            item_health = "healthy"
        item_degraded |= item_health == "degraded"
        if live:
            live_jobs.append(str(state_path))
        jobs.append({
            "authority": "scripts/west-job.sh registry entry and status command",
            "health": item_health,
            "state": state,
            "entry": str(entry),
            "state_dir": str(state_path),
            "pid": pid,
            "start_time": values["start-time"],
            "command": values["command"],
            "recorded_rc": recorded_rc,
            "status": status,
        })
    jobs.sort(key=lambda item: (item["state_dir"], item["entry"]))
    health = (
        "degraded"
        if invalid or truncated or item_degraded or status_degraded
        else ("busy" if live_jobs or busy_registries else ("healthy" if jobs else "empty"))
    )
    in_progress = bool(live_jobs or busy_registries)
    coverage_incomplete = coverage_degraded or truncated or bool(busy_registries)
    result: dict[str, Any] = {
        "authority": "scripts/west-job.sh global registry and status command",
        "health": health,
        "in_progress": in_progress,
        "active": sorted([*live_jobs, *busy_registries]),
        "busy_registries": sorted(busy_registries),
        "configured_state_roots": configured_roots,
        "coverage": "incomplete" if coverage_incomplete else "complete",
        "coverage_health": (
            "degraded"
            if coverage_degraded or truncated
            else ("busy" if busy_registries else "healthy")
        ),
        "registries": sorted(registries),
        "jobs": jobs,
        "invalid": invalid,
        "truncated": truncated,
    }
    if not registries and not invalid and not busy_registries:
        result["reason"] = "no registry exists in a discoverable or explicitly configured state root"
    return result


def collect_status(
    topdir: Path,
    manifest_repo: Path,
    manifest: Any,
    profile: str,
    bead: str | None,
    prefix: Path | None,
    build_dir: Path | None,
    west_argv: list[str],
) -> dict[str, Any]:
    """Collect a deterministic, JSON-serializable, read-only workspace snapshot."""

    topdir = _absolute(Path(topdir))
    manifest_repo = _absolute(Path(manifest_repo))
    prefix = _absolute(Path(prefix)) if prefix is not None else None
    build_dir = _absolute(Path(build_dir)) if build_dir is not None else None
    if not topdir.is_dir():
        raise StatusError(f"invalid West topdir: {topdir}")
    if not manifest_repo.is_dir():
        raise StatusError(f"invalid West manifest repository: {manifest_repo}")
    if manifest is None:
        raise StatusError("West manifest is unavailable")
    if not isinstance(profile, str) or not profile:
        raise StatusError("profile must be a non-empty string")
    if not west_argv or not all(isinstance(item, str) and item for item in west_argv):
        raise StatusError("west_argv must be a non-empty argv list")

    manifest_section, repositories = _manifest_snapshot(topdir, manifest_repo, manifest)
    git_section = _git_snapshots(repositories)

    if bead is None:
        bead_section = _unavailable("west dw beads show --json", "no bead was requested")
    else:
        bead_result = _run_bounded(
            [*west_argv, "dw", "beads", "show", bead, "--json"],
            cwd=manifest_repo,
        )
        bead_section = _json_command_section("west dw beads routed Bead JSON", bead_result)

    handoff_section = _json_command_section(
        "west dw handoff --dry-run --json",
        _run_bounded(
            [*west_argv, "dw", "handoff", "--dry-run", "--json"],
            cwd=manifest_repo,
        ),
    )
    patch_section = _command_section(
        "west patch status typed profile composition",
        _run_bounded(
            [*west_argv, "patch", "status", "--profile", profile, "--strict"],
            cwd=manifest_repo,
        ),
    )

    doctor_argv = [*west_argv, "darling-doctor"]
    if prefix is not None:
        doctor_argv.extend(("--prefix", str(prefix)))
    if build_dir is not None:
        doctor_argv.extend(("--build-dir", str(build_dir)))
    doctor_section = _command_section(
        "west darling-doctor authoritative defaults with explicit overrides",
        _run_bounded(doctor_argv, cwd=manifest_repo),
    )

    roots = _candidate_roots(topdir, manifest_repo, prefix, build_dir)
    job_roots, configured_job_roots, job_root_errors = _job_registry_roots(
        topdir, manifest_repo, prefix, build_dir
    )
    results = {
        "manifest": manifest_section,
        "git": git_section,
        "bead": bead_section,
        "handoff": handoff_section,
        "patch": patch_section,
        "doctor": doctor_section,
        "source_worktrees": _source_worktree_records(roots),
        "deployment_transactions": _deployment_transactions(roots, prefix),
        "temporary_worktrees": _temporary_worktrees(repositories),
        "long_jobs": _long_jobs(
            job_roots,
            manifest_repo,
            configured_job_roots,
            job_root_errors,
        ),
    }
    degraded = [name for name, section in results.items() if section["health"] == "degraded"]
    in_progress = [
        name for name, section in results.items() if section.get("in_progress") is True
    ]
    if in_progress:
        state = "in_progress"
        next_safe_action = (
            "wait for registered long jobs and active deployments; recover interrupted "
            "deployment transactions; then rerun west dev status"
        )
    elif degraded:
        state = "degraded"
        next_safe_action = "resolve degraded status authorities, then rerun west dev status"
    else:
        state = "healthy"
        next_safe_action = f"west dev check quick --profile {profile}"
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "status",
        "transaction_id": None,
        "state": state,
        "inputs": {
            "topdir": str(topdir),
            "manifest_repo": str(manifest_repo),
            "profile": profile,
            "bead": bead,
            "prefix": str(prefix) if prefix is not None else None,
            "build_dir": str(build_dir) if build_dir is not None else None,
            "west_argv": list(west_argv),
        },
        "limits": {
            "process_timeout_seconds": PROCESS_TIMEOUT_SECONDS,
            "git_timeout_seconds": GIT_TIMEOUT_SECONDS,
            "job_status_timeout_seconds": JOB_STATUS_TIMEOUT_SECONDS,
            "terminate_grace_seconds": TERMINATE_GRACE_SECONDS,
            "discovery_timeout_seconds": DISCOVERY_TIMEOUT_SECONDS,
            "capture_bytes": MAX_CAPTURE_BYTES,
            "projects": MAX_PROJECTS,
            "status_entries": MAX_STATUS_ENTRIES,
            "worktrees": MAX_WORKTREES,
            "discovery_files": MAX_DISCOVERY_FILES,
            "discovery_entries_scanned": MAX_DISCOVERY_ENTRIES_SCANNED,
            "registry_entries_scanned": MAX_REGISTRY_ENTRIES_SCANNED,
            "registry_entries": MAX_REGISTRY_ENTRIES,
            "json_file_bytes": MAX_JSON_FILE_BYTES,
            "registry_field_bytes": MAX_REGISTRY_FIELD_BYTES,
        },
        "results": results,
        "in_progress_sections": in_progress,
        "degraded_sections": degraded,
        "next_safe_action": next_safe_action,
    }
